"""空売り残高の取得・パース・保存（SPEC §2.2）。

- 取得元: https://karauri.net/<証券コード4桁>/
- 報告者の同一性は表示名ではなく holder_id（リンクの f= の値）で判定する
- 保存は「取得した行の最小計算日以降だけを置換」（銘柄単位の全置換はしない。§2.2.2a）
- 残高合計（short_totals）は holder_id ごとの最新の1件を採用して算出する（§2.2.3）
"""

from __future__ import annotations

import logging
import re
import threading
import warnings
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from ..database import Database
from ..errors import Cancelled, UserFacingError
from ..fetcher import code_from_symbol
from ..jobs import JobContext
from .base import HttpClient, HttpError, user_agent

log = logging.getLogger(__name__)

_MIN_INTERVAL_FLOOR = 5.0
_LOST_NOTE_TOKEN = "消失"
_LOST_RATIO_THRESHOLD = 0.5
_KNOWN_NOTE_RE = re.compile(r"^再IN（前回\d{4}-\d{2}-\d{2}）$")
_EXPECTED_COLUMNS = 7
_MAX_CONSECUTIVE_FAILURES = 3  # 5xx・タイムアウトが連続でこの回数に達したらバッチを中止する（SPEC §2.2.4）


def fetch_html(client: HttpClient, code: str, cancel: threading.Event | None = None) -> str:
    """https://karauri.net/<code>/ を取得して本文（HTML）を返す。"""
    response = client.get(f"https://karauri.net/{code}/", cancel=cancel)
    return response.text


def make_client(settings) -> HttpClient:
    """karauri.net 用の HttpClient を組み立てる（SPEC §2.2.4・§4.1）。

    scrape_contact が空だと連絡先を名乗れないため、クライアントを作らずエラーにする。
    """
    contact = str(settings.get("scrape_contact") or "").strip()
    if not contact:
        raise UserFacingError(
            "scrape_contact（連絡先）が未設定です。連絡先を名乗れない自動取得は行いません。"
            "設定タブで karauri.net への連絡先を入力してください。"
        )

    def min_interval() -> float:
        configured = float(settings.get("scrape_interval_sec") or 0)
        return max(_MIN_INTERVAL_FLOOR, configured)

    return HttpClient(
        source="karauri",
        min_interval=min_interval,
        agent=user_agent(contact),
        no_retry_statuses=frozenset({403, 429}),
    )


def parse(html: str) -> list[dict]:
    """SPEC §2.2.2 のとおりに空売り残高テーブルをパースする。

    テーブルが見つからない、または列数が7でない行がある場合は UserFacingError を送出し、
    何も返さない（サイト構造の変更を検知して保存を止めるため）。
    """
    with warnings.catch_warnings():
        # karauri.net は XHTML 1.0 Strict を text/html で返す（先頭に XML 宣言がある）。
        # ブラウザと同じく HTML として読むのが正しいので、bs4 の「XML では？」警告は出さない。
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id="sort")
    if table is None:
        raise UserFacingError(
            "karauri.net のページ構造が想定と異なります（テーブルが見つかりません）"
        )

    rows = table.find_all("tr")
    if not rows:
        raise UserFacingError(
            "karauri.net のページ構造が想定と異なります（データがありません）"
        )

    header_cells = rows[0].find_all(["th", "td"])
    if len(header_cells) != _EXPECTED_COLUMNS:
        raise UserFacingError(
            "karauri.net のページ構造が想定と異なります（列数が一致しません）"
        )

    data_rows = [tr for tr in rows[1:] if _row_classes(tr) & {"obb", "occ"}]

    order: list[tuple[str, str]] = []
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for tr in data_rows:
        cells = tr.find_all("td")
        if len(cells) != _EXPECTED_COLUMNS:
            raise UserFacingError(
                "karauri.net のページ構造が想定と異なります（列数が一致しません）"
            )

        calc_date = _parse_date(cells[0])
        holder_id, holder = _parse_holder(cells[1])
        ratio = _parse_number(cells[2].get_text(strip=True), pct=True)
        ratio_delta = _parse_number(cells[3].get_text(strip=True), pct=True)
        quantity = _parse_number(cells[4].get_text(strip=True), suffix="株")
        qty_delta = _parse_number(cells[5].get_text(strip=True))
        note = cells[6].get_text(strip=True)
        _check_note(note)

        key = (calc_date, holder_id)
        if key in by_key:
            log.warning(
                "karauri: duplicate row for calc_date=%s holder_id=%s; keeping the later one",
                calc_date, holder_id,
            )
        else:
            order.append(key)
        by_key[key] = {
            "calc_date": calc_date,
            "holder_id": holder_id,
            "holder": holder,
            "ratio": ratio,
            "ratio_delta": ratio_delta,
            "quantity": quantity,
            "qty_delta": qty_delta,
            "note": note,
        }

    return [by_key[key] for key in order]


def compute_totals(rows: list[dict]) -> list[dict]:
    """SPEC §2.2.3 の手順で銘柄合計を算出する。date 昇順で返す。"""
    if not rows:
        return []

    by_holder: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_holder[row["holder_id"]].append(row)
    for holder_rows in by_holder.values():
        holder_rows.sort(key=lambda r: r["calc_date"])

    dates = sorted({row["calc_date"] for row in rows})
    totals: list[dict] = []
    for d in dates:
        total_ratio = 0.0
        total_qty = 0
        holders = 0
        for holder_rows in by_holder.values():
            latest = _latest_as_of(holder_rows, d)
            if latest is None or _is_lost(latest):
                continue
            holders += 1
            total_ratio += latest["ratio"] or 0.0
            total_qty += latest["quantity"] or 0
        # 0.5 + 1.2 が 1.7000000000000002 になるような誤差をそのまま保存しない
        totals.append(
            {"date": d, "total_ratio": round(total_ratio, 6), "total_qty": total_qty, "holders": holders}
        )
    return totals


def save(db: Database, symbol: str, rows: list[dict]) -> dict:
    """SPEC §2.2.2a のとおりに保存する。銘柄単位の全置換はしない。

    rows が空なら何もしない（short_totals も消さない）。
    """
    if not rows:
        return {"rows": 0, "dates": 0, "since": None}

    d_min = min(row["calc_date"] for row in rows)

    with db.write() as conn:
        conn.execute(
            "DELETE FROM short_positions WHERE symbol = ? AND calc_date >= ?",
            (symbol, d_min),
        )
        conn.executemany(
            """
            INSERT INTO short_positions
                (symbol, calc_date, holder_id, holder, ratio, ratio_delta, quantity, qty_delta, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, calc_date, holder_id) DO UPDATE SET
                holder      = excluded.holder,
                ratio       = excluded.ratio,
                ratio_delta = excluded.ratio_delta,
                quantity    = excluded.quantity,
                qty_delta   = excluded.qty_delta,
                note        = excluded.note
            """,
            [
                (
                    symbol, row["calc_date"], row["holder_id"], row["holder"],
                    row["ratio"], row["ratio_delta"], row["quantity"], row["qty_delta"], row["note"],
                )
                for row in rows
            ],
        )

        all_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT calc_date, holder_id, holder, ratio, ratio_delta, quantity, qty_delta, note "
                "FROM short_positions WHERE symbol = ? ORDER BY calc_date",
                (symbol,),
            ).fetchall()
        ]
        totals = compute_totals(all_rows)

        conn.execute("DELETE FROM short_totals WHERE symbol = ?", (symbol,))
        conn.executemany(
            "INSERT INTO short_totals (symbol, date, total_ratio, total_qty, holders) VALUES (?, ?, ?, ?, ?)",
            [(symbol, t["date"], t["total_ratio"], t["total_qty"], t["holders"]) for t in totals],
        )

    return {"rows": len(rows), "dates": len(totals), "since": d_min}


# ---------- 取得の入口（画面から呼ぶ。SPEC §2.2.4・§2.8.1）----------

def select_targets(
    db: Database, settings, symbols: list[str] | None = None, force: bool = False
) -> tuple[list[str], int]:
    """対象銘柄（国内銘柄のみ）と、再取得抑止でスキップされる件数を返す。

    symbols を指定すればその中から国内銘柄（`.T`）だけに絞る。指定しなければ登録銘柄すべて。
    force が真なら `short_recheck_hours` による抑止を無視する
    （ユーザーが単一銘柄を明示的に取得する操作のための例外。SPEC §2.2.4）。
    """
    if symbols:
        candidates = [s for s in symbols if s.endswith(".T")]
    else:
        candidates = [s["symbol"] for s in db.list_stocks() if s["symbol"].endswith(".T")]
    if force:
        return candidates, 0
    recheck_hours = float(settings.get("short_recheck_hours") or 0)
    targets = [s for s in candidates if not _recently_fetched(db, s, recheck_hours)]
    return targets, len(candidates) - len(targets)


def _recently_fetched(db: Database, symbol: str, hours: float) -> bool:
    if hours <= 0:
        return False
    row = db.get_fetch("karauri", symbol)
    if row is None or row["result"] != "ok":
        return False
    try:
        fetched_at = datetime.strptime(row["fetched_at"], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return datetime.now() - fetched_at < timedelta(hours=hours)


def estimate(db: Database, settings, symbols: list[str] | None = None) -> dict:
    """一括取得の事前見積り（画面の確認ダイアログ用。SPEC §2.2.4）。"""
    contact_ok = bool(str(settings.get("scrape_contact") or "").strip())
    targets, skipped = select_targets(db, settings, symbols=symbols, force=False)
    interval = max(_MIN_INTERVAL_FLOOR, float(settings.get("scrape_interval_sec") or 0))
    return {
        "targets": len(targets),
        "skipped": skipped,
        "interval_sec": interval,
        "eta_sec": len(targets) * interval,
        "contact_ok": contact_ok,
    }


def fetch_one(
    db: Database,
    settings,
    symbol: str,
    cancel: threading.Event | None = None,
    client: HttpClient | None = None,
) -> dict:
    """1銘柄ぶんの取得〜保存（取得 → パース → 保存の順。ブロッキング）。

    国内銘柄（`.T`）以外は取得せずスキップする。`client` を渡さなければ `make_client(settings)` で
    作る（`scrape_contact` 未設定なら `UserFacingError`）。失敗したら `fetch_log` に記録して例外を
    投げ直す（中断 `Cancelled` は「失敗」ではないので記録しない）。
    """
    if not symbol.endswith(".T"):
        return {"symbol": symbol, "status": "skipped", "reason": "not_domestic"}
    if client is None:
        client = make_client(settings)
    code = code_from_symbol(symbol)
    try:
        html = fetch_html(client, code, cancel=cancel)
        rows = parse(html)
        result = save(db, symbol, rows)
    except Cancelled:
        raise
    except Exception as exc:
        db.log_fetch("karauri", symbol, f"error:{exc}")
        raise
    db.log_fetch("karauri", symbol, "ok")
    return {"symbol": symbol, "status": "ok", **result}


def short_all_job(db: Database, settings) -> Callable[[JobContext, dict], dict]:
    """`jobs.register("short_all", ...)` に渡すジョブ関数を組み立てる（SPEC §2.2.4）。

    - `scrape_contact` 未設定なら `make_client` が `UserFacingError` を送出する
    - 対象は国内銘柄のみ。`params["symbols"]` があればそれだけ、無ければ登録銘柄すべて
    - `short_recheck_hours` 以内に成功している銘柄はスキップする（`params["force"]` で無視できる）
    - HTTP 403 / 429 を受けたらその時点でバッチ全体を中止する（リトライしない）
    - 5xx・タイムアウトはその銘柄を中止して次へ進み、連続 `_MAX_CONSECUTIVE_FAILURES` 回でバッチを中止する
    """

    def job(ctx: JobContext, params: dict) -> dict:
        client = make_client(settings)
        force = bool(params.get("force"))
        targets, skipped = select_targets(db, settings, symbols=params.get("symbols"), force=force)

        updated: list[str] = []
        errors: list[str] = []
        aborted: str | None = None
        consecutive_failures = 0
        total = len(targets)
        for i, symbol in enumerate(targets):
            ctx.progress(i, total, f"空売り残高を取得中 {i + 1}/{total}")
            ctx.check()
            try:
                fetch_one(db, settings, symbol, cancel=ctx.cancel, client=client)
            except Cancelled:
                raise
            except HttpError as exc:
                errors.append(f"{symbol}: {exc}")
                if exc.status in (403, 429):
                    aborted = "forbidden"
                    break
                consecutive_failures += 1
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    aborted = "failures"
                    break
                continue
            except Exception as exc:
                errors.append(f"{symbol}: {exc}")
                consecutive_failures += 1
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    aborted = "failures"
                    break
                continue
            consecutive_failures = 0
            updated.append(symbol)
        ctx.progress(total, total, "空売り残高の取得が完了")

        if aborted == "forbidden":
            summary = f"空売り残高: アクセスを拒否されたため中止しました（{len(updated)}件取得後）"
        elif aborted == "failures":
            summary = f"空売り残高: エラーが続いたため中止しました（{len(updated)}件取得後）"
        elif total == 0:
            summary = "空売り残高: 取得が必要な銘柄はありません（すべて取得済み）" if skipped else "空売り残高: 対象銘柄がありません"
        else:
            summary = f"空売り残高 {len(updated)}件更新" + (f"・{len(errors)}件失敗" if errors else "")

        return {
            "updated": updated,
            "skipped": skipped,
            "errors": errors,
            "aborted": aborted,
            "summary": summary,
        }

    return job


# ---------- 内部ヘルパ ----------

def _row_classes(tr) -> set[str]:
    return set(tr.get("class") or [])


def _parse_date(cell) -> str:
    """テキストの YYYY/MM/DD を YYYY-MM-DD に正規化する（href 側の日付は使わない）。

    calc_date は PK の一部なので、書式が変わっていたら黙って別物を入れずに構造変更として止める。
    """
    text = cell.get_text(strip=True)
    try:
        return datetime.strptime(text, "%Y/%m/%d").strftime("%Y-%m-%d")
    except ValueError:
        raise UserFacingError(
            f"karauri.net の計算日の書式が想定と違います（{text!r}）"
        ) from None


def _parse_holder(cell) -> tuple[str, str]:
    """holder_id はリンクの f= の値。リンクが無ければ名称を代用する。"""
    anchor = cell.find("a")
    if anchor is None:
        text = cell.get_text(strip=True)
        log.warning("karauri: holder has no link (%r); using the name as holder_id", text)
        return text, text

    holder = anchor.get_text(strip=True)
    href = anchor.get("href", "")
    holder_id = parse_qs(urlparse(href).query).get("f", [None])[0]
    if not holder_id:
        log.warning("karauri: could not find f= in href %r; using the name as holder_id", href)
        holder_id = holder
    return holder_id, holder


def _parse_number(text: str, pct: bool = False, suffix: str = "") -> float | int | None:
    """%・カンマ・単位（株など）を除去して数値にする。空文字・'-' は None。符号は文字列から読む。"""
    text = text.strip()
    if text in ("", "-"):
        return None
    text = text.replace(",", "")
    if pct:
        return float(text.rstrip("%"))
    if suffix:
        text = text.rstrip(suffix)
    return int(text)


def _check_note(note: str) -> None:
    if note and note != "報告義務消失" and not _KNOWN_NOTE_RE.fullmatch(note):
        log.warning("karauri: unknown note %r", note)


def _latest_as_of(holder_rows_sorted_by_date: list[dict], date: str) -> dict | None:
    """calc_date <= date を満たす最新の1件（holder_rows_sorted_by_date は日付昇順）。"""
    latest = None
    for row in holder_rows_sorted_by_date:
        if row["calc_date"] > date:
            break
        latest = row
    return latest


def _is_lost(record: dict) -> bool:
    """報告義務消失の判定。note に '消失' を含む、または ratio が 0.5 未満（SPEC §2.2.3）。"""
    note = record.get("note") or ""
    ratio = record.get("ratio")
    return _LOST_NOTE_TOKEN in note or (ratio is not None and ratio < _LOST_RATIO_THRESHOLD)
