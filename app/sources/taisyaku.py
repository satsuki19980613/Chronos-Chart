"""日証金 zandaka.csv（貸借取引残高）の取得・パース・保存（SPEC §2.3）。

- 過去分を取り直す手段が無い蓄積型データ。取得した申込日の分だけが貯まる
- 同じ銘柄コードが上場市場ごとに複数行現れるため、`東証` を含む行だけを採用する
- 確報（final）は速報（prelim）を上書きするが、逆は起きない（PK は (symbol, date)）
"""

from __future__ import annotations

import csv
import io
import logging
import threading
from datetime import datetime

from ..database import Database
from ..errors import UserFacingError
from ..fetcher import code_from_symbol
from .base import HttpClient, user_agent

log = logging.getLogger(__name__)

SOURCE = "taisyaku"
ZANDAKA_URL = "https://www.taisyaku.jp/data/zandaka.csv"
MIN_INTERVAL = 5.0

# ヘッダ名（SPEC §2.3.1 の実ヘッダ）。1つでも欠けていたら構造変更とみなしエラーにする。
REQUIRED_COLUMNS = (
    "申込日",
    "決済日",
    "銘柄コード",
    "取引所区分名",
    "速報／確報",
    "融資新規株数",
    "融資返済株数",
    "融資残高株数",
    "貸株新規株数",
    "貸株返済株数",
    "貸株残高株数",
    "差引残高株数",
)

_INT_FIELDS = (
    ("yushi_new", "融資新規株数"),
    ("yushi_repay", "融資返済株数"),
    ("yushi_balance", "融資残高株数"),
    ("kashi_new", "貸株新規株数"),
    ("kashi_repay", "貸株返済株数"),
    ("kashi_balance", "貸株残高株数"),
    ("net_balance", "差引残高株数"),
)


def make_client(settings=None) -> HttpClient:
    """taisyaku.jp 用の `HttpClient` を作る。

    連絡先（`scrape_contact`）は設定されていれば名乗るが、karauri と違い必須ではない。
    日証金はスクレイピングではなく公開 CSV の直接取得で、リクエストも1回だけのため。
    """
    contact = str(settings.get("scrape_contact") or "").strip() if settings is not None else ""
    return HttpClient(source=SOURCE, min_interval=MIN_INTERVAL, agent=user_agent(contact))


def fetch_zandaka(client: HttpClient, cancel: threading.Event | None = None) -> bytes:
    """`zandaka.csv` を取得し、生のバイト列を返す。"""
    response = client.get(ZANDAKA_URL, cancel=cancel)
    return response.content


def _to_int(value: str | None) -> int | None:
    value = (value or "").strip()
    if value in ("", "-"):
        return None
    return int(value.replace(",", ""))


def _normalize_date(value: str | None, *, required: bool) -> str | None:
    """`YYYY/MM/DD` を `YYYY-MM-DD` にする。

    `date` は PK の一部なので、書式が変わっていたら黙って別物を入れずに構造変更として止める。
    `settle_date` は表示用なので、読めなければ None にして先へ進む。
    """
    value = (value or "").strip()
    if not value:
        if required:
            raise UserFacingError("日証金の zandaka.csv の申込日が空です")
        return None
    try:
        return datetime.strptime(value, "%Y/%m/%d").strftime("%Y-%m-%d")
    except ValueError:
        if required:
            raise UserFacingError(
                f"日証金の zandaka.csv の申込日の書式が想定と違います（{value!r}）"
            ) from None
        log.warning("taisyaku: 決済日を読めませんでした: %r", value)
        return None


def _normalize_kind(raw: str | None) -> str:
    text = (raw or "").strip()
    if text == "確報":
        return "final"
    if text == "速報":
        return "prelim"
    log.warning("taisyaku: 未知の「速報／確報」区分値を prelim として扱います: %r", text)
    return "prelim"


def parse(content: bytes) -> list[dict]:
    """`zandaka.csv` のバイト列を dict のリストにする。

    列はヘッダ名で引く。必要な列が1つでも欠けていれば `UserFacingError` を送出し、
    何も返さない（サイト構造変更の検知）。`取引所区分名` に「東証」を含む行だけを採用する。
    """
    text = content.decode("cp932")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = set(reader.fieldnames or [])
    missing = [col for col in REQUIRED_COLUMNS if col not in fieldnames]
    if missing:
        raise UserFacingError(
            "日証金の zandaka.csv の列構成が変わっています（不足している列: " + "、".join(missing) + "）"
        )

    rows: list[dict] = []
    for raw in reader:
        exchange = (raw.get("取引所区分名") or "").strip()
        if "東証" not in exchange:
            continue
        record = {
            "code": (raw.get("銘柄コード") or "").strip(),
            "date": _normalize_date(raw.get("申込日"), required=True),
            "settle_date": _normalize_date(raw.get("決済日"), required=False),
            "kind": _normalize_kind(raw.get("速報／確報")),
        }
        for key, column in _INT_FIELDS:
            record[key] = _to_int(raw.get(column))
        rows.append(record)
    return rows


def save(db: Database, rows: list[dict], symbols: list[str], fetched_at: str | None = None) -> dict:
    """登録銘柄の行だけを `margin_balances` に保存する。

    確報は速報を上書きするが、速報は確報を上書きしない（PK は (symbol, date)）。
    貸借銘柄でない（行が無い）銘柄があってもエラーにしない。
    """
    fetched_at = fetched_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    by_code = {row["code"]: row for row in rows}

    saved = 0
    skipped = 0
    missing: list[str] = []
    date_seen: str | None = None

    with db.write() as conn:
        for symbol in symbols:
            code = code_from_symbol(symbol)
            row = by_code.get(code)
            if row is None:
                missing.append(symbol)
                continue
            date_seen = date_seen or row["date"]
            cur = conn.execute(
                """
                INSERT INTO margin_balances
                    (symbol, date, settle_date, kind, yushi_new, yushi_repay, yushi_balance,
                     kashi_new, kashi_repay, kashi_balance, net_balance, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, date) DO UPDATE SET
                    settle_date   = excluded.settle_date,
                    kind          = excluded.kind,
                    yushi_new     = excluded.yushi_new,
                    yushi_repay   = excluded.yushi_repay,
                    yushi_balance = excluded.yushi_balance,
                    kashi_new     = excluded.kashi_new,
                    kashi_repay   = excluded.kashi_repay,
                    kashi_balance = excluded.kashi_balance,
                    net_balance   = excluded.net_balance,
                    fetched_at    = excluded.fetched_at
                WHERE margin_balances.kind != 'final' OR excluded.kind = 'final'
                """,
                (
                    symbol,
                    row["date"],
                    row["settle_date"],
                    row["kind"],
                    row["yushi_new"],
                    row["yushi_repay"],
                    row["yushi_balance"],
                    row["kashi_new"],
                    row["kashi_repay"],
                    row["kashi_balance"],
                    row["net_balance"],
                    fetched_at,
                ),
            )
            if cur.rowcount:
                saved += 1
            else:
                skipped += 1

    return {"saved": saved, "skipped": skipped, "date": date_seen, "missing": missing}
