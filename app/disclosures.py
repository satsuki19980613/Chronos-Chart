"""EDINET 開示の取得範囲・差分・ジョブ（SPEC §2.4.2・§2.4.4）。

- 対象日の範囲（`fetch_range`）と、確定済み判定（`is_finalized`）、取得対象日の一覧（`pending_dates`）を扱う
- 実際の HTTP 取得・キャッシュ保存・`fetch_log` への記録は `app.sources.edinet.fetch_day` が行う
  （このモジュールはどの日付をどの順で叩くかだけを決める）
- 書類のパース・銘柄との突合・分類（§2.4.3・§2.4.6）は P4-3 で追加する
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .errors import Cancelled, UserFacingError
from .fetcher import code_from_symbol
from .jobs import JobContext
from .sources import edinet
from .sources.base import HttpError

log = logging.getLogger(__name__)

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
        cancelled: Cancelled | None = None

        try:
            for i, date in enumerate(targets):
                ctx.progress(i, total, f"開示を取得中 {i + 1}/{total}（{date}）")
                ctx.check()
                try:
                    result = edinet.fetch_day(
                        db, date, api_key, cancel=ctx.cancel, client=client, base_dir=base_dir
                    )
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
        except Cancelled as exc:
            cancelled = exc

        # SPEC §2.4.2: 中断・エラーの別を問わず、取得できた日付はここで必ず走査する。
        # 走査しないとキャッシュと DB がずれ、その日付は確定済みなので二度と走査されない。
        registered = {"documents": 0, "links": 0}
        if fetched:
            try:
                scan_result = scan_cache(db, dates=fetched, base_dir=base_dir)
                registered = {"documents": scan_result["documents"], "links": scan_result["links"]}
            except Exception:
                log.exception("開示のキャッシュ再走査に失敗しました")

        if cancelled is not None:
            raise cancelled

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
        if registered["documents"]:
            summary += f"・開示 {registered['documents']}件を登録"

        return {
            "days": done,                  # 取得に成功した日数（書類の有無を問わない）
            "with_documents": fetched,     # 書類が1件以上あった日付。P4-3 の突合はここを走査する
            "empty": empty,                # 書類が0件だった日付（土日祝など）
            "errors": errors,
            "aborted": aborted,
            "registered": registered,      # scan_cache で disclosures / disclosure_links に登録した件数
            "summary": summary,
        }

    return job


# ---------- 突合・分類・キャッシュ再走査（SPEC §2.4.3・§2.4.6） ----------

# SPEC §2.4.6: 表示上の分類（`disclosures.category`）
_REPORT_CODES = {"120", "130", "140", "150", "160", "170"}  # 有報・四半期（過去分のみ）・半期報
_SUPPLY_EXACT_CODES = {"350", "360", "220", "230"}  # 大量保有・自己株買付
_MAJOR_HOLDING_CODES = {"350", "360"}  # 大量保有報告書
# 公開買付関連。分類（§2.4.6 で supply）と突合（§2.4.3 で subject / filer の両方）の両方で使うので、
# 定義は1か所だけにする
_TENDER_OFFER_RANGE = (240, 320)


def classify(doc_type_code: str | None) -> str:
    """`docTypeCode` を表示分類にする（SPEC §2.4.6）。

    `report` / `supply` / `other` のいずれか。空・None・数字でない値はすべて `other`。
    """
    code = str(doc_type_code).strip() if doc_type_code is not None else ""
    if not code:
        return "other"
    if code in _REPORT_CODES:
        return "report"
    if code in _SUPPLY_EXACT_CODES:
        return "supply"
    try:
        n = int(code)
    except ValueError:
        return "other"
    if _TENDER_OFFER_RANGE[0] <= n <= _TENDER_OFFER_RANGE[1]:
        return "supply"
    return "other"


# `documents.json` の `results` の1要素 → `disclosures` の列名
_STRING_FIELDS = {
    "docID": "doc_id",
    "edinetCode": "edinet_code",
    "secCode": "sec_code",  # 英字を含み得るので文字列のまま扱う（int にしない）
    "filerName": "filer_name",
    "issuerEdinetCode": "issuer_edinet_code",
    "subjectEdinetCode": "subject_edinet_code",
    "docTypeCode": "doc_type_code",
    "formCode": "form_code",
    "ordinanceCode": "ordinance_code",
    "docDescription": "description",
    "currentReportReason": "reason",
    "periodStart": "period_start",
    "periodEnd": "period_end",
    "submitDateTime": "submit_at",
    "parentDocID": "parent_doc_id",
}
_INT_FIELDS = {
    "withdrawalStatus": "withdrawal",
    "disclosureStatus": "disclosure",
}
# `disclosures` の NOT NULL 列。1つでも欠けていたらこの書類は捨てる
_REQUIRED_COLUMNS = ("doc_id", "doc_type_code", "submit_at")

# `disclosures` テーブルの列（`doc_id` を含む。upsert で使う並び順の基準）
_DISCLOSURE_COLUMNS = [
    "doc_id",
    "edinet_code",
    "sec_code",
    "filer_name",
    "issuer_edinet_code",
    "subject_edinet_code",
    "doc_type_code",
    "form_code",
    "ordinance_code",
    "description",
    "reason",
    "period_start",
    "period_end",
    "submit_at",
    "parent_doc_id",
    "withdrawal",
    "disclosure",
    "category",
]

def _clean_str(value) -> str | None:
    """空文字・None を None にする。それ以外は文字列のまま返す（`sec_code` を int にしないため）。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_int(value) -> int | None:
    """int に直せない値（None・空文字・変な文字列）は None にする。"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_document(raw: dict) -> dict | None:
    """`documents.json` の `results` の1要素を `disclosures` の1行にする（SPEC §2.4.3・§2.4.6）。

    `doc_id` / `doc_type_code` / `submit_at`（NOT NULL 列）のいずれかが欠けていれば None を返す。
    """
    doc: dict = {}
    for json_key, column in _STRING_FIELDS.items():
        doc[column] = _clean_str(raw.get(json_key))
    for json_key, column in _INT_FIELDS.items():
        doc[column] = _clean_int(raw.get(json_key))

    missing = [column for column in _REQUIRED_COLUMNS if not doc.get(column)]
    if missing:
        log.warning(
            "EDINET 書類の必須項目が欠けているため捨てます（欠けている列: %s、docID=%r）",
            "、".join(missing),
            raw.get("docID"),
        )
        return None

    doc["category"] = classify(doc["doc_type_code"])
    return doc


def link_targets(db, symbols: list[str] | None = None) -> dict:
    """突合用の索引を作る（SPEC §2.4.3）。

    `symbols` 省略時は登録銘柄すべて（`db.list_stocks()`）。銘柄ごとに `edinet.edinet_code_for` で
    EDINET コードを引き、引けた銘柄は `by_edinet_code`（EDINET コード → 銘柄シンボルのリスト）に、
    引けなかった銘柄は `by_sec_code`（`{4桁コード}0` → 銘柄シンボルのリスト。`secCode` 補助突合用）に分けて持つ。
    """
    syms = symbols if symbols is not None else [row["symbol"] for row in db.list_stocks()]

    by_edinet_code: dict[str, list[str]] = {}
    by_sec_code: dict[str, list[str]] = {}
    for symbol in syms:
        edinet_code = edinet.edinet_code_for(db, symbol)
        if edinet_code:
            by_edinet_code.setdefault(edinet_code, []).append(symbol)
        else:
            sec_code = f"{code_from_symbol(symbol)}0"
            by_sec_code.setdefault(sec_code, []).append(symbol)

    return {"by_edinet_code": by_edinet_code, "by_sec_code": by_sec_code}


def _doc_type_as_int(doc_type_code: str | None) -> int | None:
    try:
        return int(doc_type_code)
    except (TypeError, ValueError):
        return None


def match_roles(doc: dict, targets: dict) -> list[tuple[str, str]]:
    """1件の書類（`parse_document` の戻り値）が、どの登録銘柄とどんな関係にあるかを決める（SPEC §2.4.3）。

    戻り値は `[(symbol, role), ...]`。1つの書類が複数の登録銘柄・複数の role に対応することがある
    （公開買付の対象会社と買付者が両方登録済み、自己株式の公開買付で同じ銘柄に `subject` と `filer` の両方、など）。
    """
    by_edinet_code = targets.get("by_edinet_code", {})
    by_sec_code = targets.get("by_sec_code", {})
    doc_type_code = doc.get("doc_type_code")
    roles: list[tuple[str, str]] = []

    if doc_type_code in _MAJOR_HOLDING_CODES:
        # 大量保有報告書: issuerEdinetCode（発行会社）でのみ突合する。
        # edinetCode（提出者）や secCode で突合すると、他社株を保有して提出しただけの銘柄が
        # 自分自身の需給イベントとして登録されてしまう（CLAUDE.md 不変条件5）。
        issuer_code = doc.get("issuer_edinet_code")
        if issuer_code:
            for symbol in by_edinet_code.get(issuer_code, []):
                roles.append((symbol, "issuer"))
        return roles

    n = _doc_type_as_int(doc_type_code)
    if n is not None and _TENDER_OFFER_RANGE[0] <= n <= _TENDER_OFFER_RANGE[1]:
        # 公開買付関連: 対象会社（subject）と買付者・意見表明者（filer）の両方が成立し得る。
        subject_code = doc.get("subject_edinet_code")
        if subject_code:
            for symbol in by_edinet_code.get(subject_code, []):
                roles.append((symbol, "subject"))
        filer_code = doc.get("edinet_code")
        if filer_code:
            for symbol in by_edinet_code.get(filer_code, []):
                roles.append((symbol, "filer"))
        sec_code = doc.get("sec_code")
        if sec_code:
            for symbol in by_sec_code.get(sec_code, []):
                roles.append((symbol, "filer"))
        return roles

    # それ以外すべて（有報・半期報・臨時報告書など）: edinetCode（提出者）で filer として突合する。
    filer_code = doc.get("edinet_code")
    if filer_code:
        for symbol in by_edinet_code.get(filer_code, []):
            roles.append((symbol, "filer"))
    sec_code = doc.get("sec_code")
    if sec_code:
        for symbol in by_sec_code.get(sec_code, []):
            roles.append((symbol, "filer"))
    return roles


def save_documents(db, items: list[tuple[dict, list[tuple[str, str]]]]) -> dict:
    """突合済みの書類を保存する（SPEC §2.4.3・§3）。

    どの登録銘柄にも紐づかない書類（`roles` が空）は保存しない。`disclosures` は `doc_id` の upsert
    （取下げ・書類情報修正で内容が変わるため上書きする）、`disclosure_links` は
    `(doc_id, symbol, role)` の upsert（`DO NOTHING`。再走査で行が増えないようにする）。
    """
    to_save = [(doc, roles) for doc, roles in items if roles]
    if not to_save:
        return {"documents": 0, "links": 0}

    doc_columns_sql = ", ".join(_DISCLOSURE_COLUMNS)
    placeholders = ", ".join("?" for _ in _DISCLOSURE_COLUMNS)
    update_clause = ", ".join(
        f"{column} = excluded.{column}" for column in _DISCLOSURE_COLUMNS if column != "doc_id"
    )

    doc_rows = [tuple(doc.get(column) for column in _DISCLOSURE_COLUMNS) for doc, _ in to_save]
    link_rows = [(doc["doc_id"], symbol, role) for doc, roles in to_save for symbol, role in roles]

    with db.write() as conn:
        before_docs = conn.total_changes
        conn.executemany(
            f"""
            INSERT INTO disclosures ({doc_columns_sql})
            VALUES ({placeholders})
            ON CONFLICT(doc_id) DO UPDATE SET {update_clause}
            """,
            doc_rows,
        )
        saved_documents = conn.total_changes - before_docs

        before_links = conn.total_changes
        conn.executemany(
            "INSERT INTO disclosure_links (doc_id, symbol, role) VALUES (?, ?, ?) "
            "ON CONFLICT(doc_id, symbol, role) DO NOTHING",
            link_rows,
        )
        saved_links = conn.total_changes - before_links

    return {"documents": saved_documents, "links": saved_links}


def scan_cache(
    db,
    *,
    symbols: list[str] | None = None,
    dates: list[str] | None = None,
    base_dir: Path | None = None,
) -> dict:
    """日次キャッシュを走査して `disclosures` / `disclosure_links` を埋める（API は呼ばない。SPEC §2.4.2）。

    `symbols` 省略時は登録銘柄すべて、`dates` 省略時は `edinet.cached_dates()` すべて。
    登録銘柄・対象日のどちらかが0件なら何もしない。1日ぶんずつ「キャッシュを読む→短いトランザクションで
    保存」を繰り返す（CLAUDE.md 不変条件10: 長時間 DB の書き込みロックを持たない）。
    壊れている・存在しないキャッシュの日付は読み飛ばして次へ進む（例外にしない）。
    戻り値は `{"dates": 走査した日数, "documents": 保存した書類数, "links": 保存した関係数}`。
    """
    syms = symbols if symbols is not None else [row["symbol"] for row in db.list_stocks()]
    target_dates = dates if dates is not None else edinet.cached_dates(base_dir=base_dir)

    if not syms or not target_dates:
        return {"dates": 0, "documents": 0, "links": 0}

    targets = link_targets(db, syms)  # 日付ごとに引き直さない

    scanned_dates = 0
    total_documents = 0
    total_links = 0
    for date in target_dates:
        data = edinet.read_cache(date, base_dir=base_dir)
        if data is None:
            continue  # 無い・壊れているキャッシュは無いものとして扱う（SPEC §2.4.2）
        scanned_dates += 1

        items = []
        for raw in data.get("results") or []:
            doc = parse_document(raw)
            if doc is None:
                continue
            roles = match_roles(doc, targets)
            items.append((doc, roles))

        result = save_documents(db, items)
        total_documents += result["documents"]
        total_links += result["links"]

    return {"dates": scanned_dates, "documents": total_documents, "links": total_links}


def cleanup_orphans(db) -> int:
    """どの登録銘柄にも紐づかなくなった `disclosures` の行を削除し、件数を返す（SPEC §3）。

    銘柄を削除すると `disclosure_links` が CASCADE で消えるため、その後に呼ばれる想定。
    """
    with db.write() as conn:
        # NOT IN は副問い合わせに NULL が混ざると1行も消さなくなるので NOT EXISTS を使う
        cursor = conn.execute(
            "DELETE FROM disclosures WHERE NOT EXISTS ("
            "  SELECT 1 FROM disclosure_links WHERE disclosure_links.doc_id = disclosures.doc_id"
            ")"
        )
        deleted = cursor.rowcount
    return deleted
