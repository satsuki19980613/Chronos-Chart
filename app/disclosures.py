"""EDINET 開示の取得範囲・差分・ジョブ（SPEC §2.4.2・§2.4.4）。

- 対象日の範囲（`fetch_range`）と、確定済み判定（`is_finalized`）、取得対象日の一覧（`pending_dates`）を扱う
- 実際の HTTP 取得・キャッシュ保存・`fetch_log` への記録は `app.sources.edinet.fetch_day` が行う
  （このモジュールはどの日付をどの順で叩くかだけを決める）
- 書類のパース・銘柄との突合・分類（§2.4.3・§2.4.6）は P4-3 で追加する
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .errors import Cancelled, UserFacingError
from .jobs import JobContext
from .sources import edinet
from .sources.base import HttpError

JST = ZoneInfo("Asia/Tokyo")

_MAX_CONSECUTIVE_FAILURES = 3  # 一般的なエラーがこの回数連続したらジョブを中止する（SPEC §2.4.4）
_TODAY_MIN_INTERVAL_SEC = 60.0  # SPEC §2.4.5: 当日分の再取得は1分に1回まで
_MAX_RANGE_YEARS = 10  # SPEC §2.4.4: API 制約により当日以前かつ10年以内


def today_jst() -> str:
    """日本時間の「今日」を `YYYY-MM-DD` で返す。

    EDINET は日本の日付基準で書類を区切るため、PC のタイムゾーン設定に関係なく日本時間で判断する。
    """
    return datetime.now(JST).strftime("%Y-%m-%d")


def _finalize_deadline(date: str) -> datetime:
    """対象日の翌日 00:30（日本時間）を、fetch_log の fetched_at と同じローカルの素朴な時刻に直す。"""
    jst = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=JST) + timedelta(days=1, minutes=30)
    return jst.astimezone().replace(tzinfo=None)


def _fetch_logs(db) -> dict:
    """`fetch_log` の EDINET 分を一度に読んで `{日付: 行}` にする。

    `pending_dates` は最大10年ぶん（約3650日）を1日ずつ判定するので、日付ごとに
    `db.get_fetch`（＝接続を1本ずつ開く）を呼ぶと接続の開閉だけで数千回になる。
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT key, fetched_at, result FROM fetch_log WHERE source = ?", (edinet.SOURCE,)
        ).fetchall()
    return {row["key"]: row for row in rows}


def _row_is_finalized(row, date: str) -> bool:
    """`fetch_log` の1行から確定済みかを判定する（`is_finalized` と `pending_dates` の共通部分）。"""
    if row is None or row["result"] not in ("ok", "empty"):
        return False
    try:
        fetched_at = datetime.strptime(row["fetched_at"], "%Y-%m-%d %H:%M:%S")
        deadline = _finalize_deadline(date)
    except (TypeError, ValueError):
        return False
    return fetched_at >= deadline


def is_finalized(db, date: str) -> bool:
    """SPEC §2.4.4 の確定済み判定。

    `fetch_log(source='edinet', key=date)` の `result` が `ok`/`empty` で、`fetched_at` が
    対象日の翌日 00:30（日本時間）以降であること。記録が無い・`result` が `error:...`・
    `fetched_at` が解釈できない場合はすべて未確定（False）として扱う。
    """
    return _row_is_finalized(db.get_fetch(edinet.SOURCE, date), date)


def _fetched_at_of(row) -> datetime | None:
    if row is None:
        return None
    try:
        return datetime.strptime(row["fetched_at"], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def fetch_range(db, today: str | None = None) -> tuple[str | None, str]:
    """対象日の範囲（SPEC §2.4.4）。

    開始日は登録銘柄の株価の最古日（`MIN(prices.date)`）。株価が1件も無ければ `(None, today)`。
    当日以前かつ10年以内に丸める。`end` は常に `today`（省略時は `today_jst()`）。
    """
    end = today if today is not None else today_jst()
    with db.connect() as conn:
        row = conn.execute("SELECT MIN(date) AS d FROM prices").fetchone()
    start = row["d"] if row is not None else None
    if not start:
        return None, end
    if start > end:
        return None, end

    end_dt = datetime.strptime(end, "%Y-%m-%d")
    try:
        floor_dt = end_dt.replace(year=end_dt.year - _MAX_RANGE_YEARS)
    except ValueError:
        # 2月29日の10年前が非うるう年の場合（ValueError）は2月28日に繰り上げる
        floor_dt = end_dt.replace(year=end_dt.year - _MAX_RANGE_YEARS, day=28)
    floor = floor_dt.strftime("%Y-%m-%d")
    if start < floor:
        start = floor
    return start, end


def pending_dates(
    db,
    *,
    today: str | None = None,
    max_days: int | None = None,
    redo_days: int = 0,
    now: datetime | None = None,
) -> list[str]:
    """取得対象の日付を新しい順（降順）で返す（SPEC §2.4.4）。

    降順なのは、中断しても直近の開示が先に揃うようにするため。
    """
    start, end = fetch_range(db, today=today)
    if start is None:
        return []

    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")

    redo_floor = None
    if redo_days > 0:
        redo_floor = (end_dt - timedelta(days=redo_days - 1)).strftime("%Y-%m-%d")

    logs = _fetch_logs(db)
    targets: list[str] = []
    d = start_dt
    while d <= end_dt:
        date = d.strftime("%Y-%m-%d")
        if (redo_floor is not None and date >= redo_floor) or not _row_is_finalized(logs.get(date), date):
            targets.append(date)
        d += timedelta(days=1)

    if end in targets:
        now_ = now if now is not None else datetime.now()
        fetched_at = _fetched_at_of(logs.get(end))
        if fetched_at is not None and (now_ - fetched_at).total_seconds() < _TODAY_MIN_INTERVAL_SEC:
            targets.remove(end)

    targets.sort(reverse=True)
    if max_days is not None:
        targets = targets[:max_days]
    return targets


def estimate(db, settings, *, max_days: int | None = None, redo_days: int = 0) -> dict:
    """一括取得の事前見積り（画面の確認ダイアログ用。SPEC §2.4.4）。"""
    start, end = fetch_range(db)
    targets = pending_dates(db, today=end, max_days=max_days, redo_days=redo_days)
    interval = edinet.MIN_INTERVAL
    return {
        "targets": len(targets),
        "interval_sec": interval,
        "eta_sec": len(targets) * interval,
        "start": start,
        "end": end,
        "api_key_ok": bool(settings.get_secret("edinet_api_key")),
    }


def _parse_int(value, default: int | None) -> int | None:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def disclosures_job(db, settings, base_dir: Path | None = None) -> Callable[[JobContext, dict], dict]:
    """`jobs.register("disclosures", disclosures_job(db, settings))` に渡すジョブ関数を組み立てる。

    - `edinet_api_key` 未設定なら `UserFacingError`
    - 対象日は `pending_dates`（新しい日付から古い日付へ）
    - HTTP 401 / 403 / 429 を受けたらその時点でジョブを中止する（`aborted = "forbidden"`）
    - それ以外の例外はその日付を `errors` に積んで次へ進み、連続 `_MAX_CONSECUTIVE_FAILURES` 回で中止する
      （`aborted = "failures"`）
    - いずれの場合も、それまでに保存したキャッシュと `fetch_log` は保持される（`fetch_day` 側の責務）
    """

    def job(ctx: JobContext, params: dict) -> dict:
        api_key = settings.get_secret("edinet_api_key")
        if not api_key:
            raise UserFacingError("EDINET の API キーが設定されていません。設定タブで登録してください")

        max_days = _parse_int(params.get("max_days"), None)
        redo_days = _parse_int(params.get("redo_days"), 0)

        targets = pending_dates(db, max_days=max_days, redo_days=redo_days)
        client = edinet.make_client(settings)

        fetched: list[str] = []
        empty: list[str] = []
        errors: list[str] = []
        aborted: str | None = None
        consecutive_failures = 0
        total = len(targets)

        for i, date in enumerate(targets):
            ctx.progress(i, total, f"開示を取得中 {i + 1}/{total}（{date}）")
            ctx.check()
            try:
                result = edinet.fetch_day(db, date, api_key, cancel=ctx.cancel, client=client, base_dir=base_dir)
            except Cancelled:
                raise
            except HttpError as exc:
                errors.append(f"{date}: {exc}")
                if exc.status in (401, 403, 429):
                    aborted = "forbidden"
                    break
                consecutive_failures += 1
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    aborted = "failures"
                    break
                continue
            except Exception as exc:
                errors.append(f"{date}: {exc}")
                consecutive_failures += 1
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    aborted = "failures"
                    break
                continue

            consecutive_failures = 0
            if result["result"] == "empty":
                empty.append(date)
            else:
                fetched.append(date)

        ctx.progress(total, total, "開示の取得が完了")

        done = len(fetched) + len(empty)
        if aborted == "forbidden":
            summary = f"開示: アクセスを拒否されたため中止しました（{done}日分取得後）"
        elif aborted == "failures":
            summary = f"開示: エラーが続いたため中止しました（{done}日分取得後）"
        elif total == 0:
            summary = "開示: 取得が必要な日付はありません"
        else:
            summary = f"開示 {done}日分を取得（うち書類あり {len(fetched)}日）"
            if errors:
                summary += f"・{len(errors)}日分失敗"

        return {
            "days": done,                  # 取得に成功した日数（書類の有無を問わない）
            "with_documents": fetched,     # 書類が1件以上あった日付。P4-3 の突合はここを走査する
            "empty": empty,                # 書類が0件だった日付（土日祝など）
            "errors": errors,
            "aborted": aborted,
            "summary": summary,
        }

    return job
