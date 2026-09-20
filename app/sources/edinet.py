"""EDINET API v2（書類一覧）とコードリストの取得・パース・保存（SPEC §2.4）。

- 書類一覧（`documents.json`）は API v2 のみから取る。`documents.json` のパースや
  `disclosures` への登録はこのモジュールの範囲外（P4-2・P4-3）
- コードリスト（`EdinetcodeDlInfo.csv`）は API では取得できないので、規約の但し書き（§2.4.7）に
  従ってダウンロード機能を使う。全置換で `edinet_codes` に取り込む
- API キーはクエリパラメータ `Subscription-Key` で送る。ログ・例外メッセージに出さないよう、
  `HttpClient` 側の `mask_secrets` に必ず任せる（自前で URL を組み立てない）
"""

from __future__ import annotations

import csv
import io
import logging
import threading
import zipfile

from ..database import Database
from ..errors import Cancelled, UserFacingError
from ..fetcher import code_from_symbol
from .base import HttpClient, user_agent

log = logging.getLogger(__name__)

SOURCE = "edinet"
DOCUMENTS_URL = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
CODE_LIST_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"
MIN_INTERVAL = 1.0  # SPEC §2.4.5: 1リクエストあたり1秒以上空ける

CODE_LIST_MEMBER = "EdinetcodeDlInfo.csv"

# ヘッダ名（SPEC §2.4.1a）。1つでも欠けていたら構造変更とみなしエラーにする。
REQUIRED_CODE_LIST_COLUMNS = ("ＥＤＩＮＥＴコード", "証券コード", "提出者名")


def make_client(settings=None) -> HttpClient:
    """EDINET 用の `HttpClient` を作る。

    書類一覧・コードリストのどちらも同じ `source` を使い、レート制限（1秒間隔・直列）を共有する。
    429 は karauri と違い、`HttpClient` の既定どおりリトライ対象のままにする
    （`no_retry_statuses` に 429 を入れない）。
    """
    contact = str(settings.get("scrape_contact") or "").strip() if settings is not None else ""
    return HttpClient(source=SOURCE, min_interval=MIN_INTERVAL, agent=user_agent(contact))


def fetch_documents(client: HttpClient, date: str, api_key: str, cancel: threading.Event | None = None) -> dict:
    """`documents.json` を取得して JSON を dict で返す（SPEC §2.4.1）。

    パースや `disclosures` への登録はこの関数の範囲外。`type=2`（書類一覧及びメタデータ）で呼ぶ。
    キーは `params` で渡すので、`HttpClient.get` のログでは `mask_secrets` によりマスクされる。
    """
    api_key = (api_key or "").strip()
    if not api_key:
        raise UserFacingError("EDINET の API キーが設定されていません")

    params = {"date": date, "type": 2, "Subscription-Key": api_key}
    response = client.get(DOCUMENTS_URL, params=params, cancel=cancel)
    data = response.json()

    metadata = data.get("metadata") or {}
    status = str(metadata.get("status") or "")
    if status != "200":
        message = metadata.get("message") or "不明なエラー"
        raise UserFacingError(f"EDINET が書類一覧の取得でエラーを返しました（{message}）")
    return data


def fetch_code_list(client: HttpClient, cancel: threading.Event | None = None) -> bytes:
    """EDINET コードリスト（ZIP）を取得し、生のバイト列を返す（SPEC §2.4.1a）。

    このファイルは API では取得できないため、ダウンロード機能を使う。キーは不要。
    """
    response = client.get(CODE_LIST_URL, cancel=cancel)
    return response.content


def parse_code_list(content: bytes) -> list[dict]:
    """コードリストの ZIP バイト列を `{"edinet_code", "sec_code", "name"}` の dict のリストにする。

    - ZIP の中の `EdinetcodeDlInfo.csv` を `cp932` で読む
    - 1行目はダウンロード情報の行なので読み飛ばし、2行目をヘッダとして使う
    - 列はヘッダ名で引く。必要な列が1つでも欠けていれば `UserFacingError`
    - `sec_code` は5桁の文字列のまま返す（英字を含み得るので int にしない）。空文字は None
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            if CODE_LIST_MEMBER not in zf.namelist():
                raise UserFacingError(
                    f"EDINET コードリストの ZIP に {CODE_LIST_MEMBER} が見つかりません"
                )
            raw = zf.read(CODE_LIST_MEMBER)
    except zipfile.BadZipFile:
        raise UserFacingError("EDINET コードリストの ZIP を読み取れませんでした") from None

    # テキストを行で切ってから繋ぎ直すとクォート内の改行を壊すので、csv.reader にそのまま読ませ、
    # 1行目（ダウンロード情報）を1レコードとして読み飛ばす
    reader = csv.reader(io.StringIO(raw.decode("cp932")))
    next(reader, None)  # ダウンロード実行日・件数の行
    header = next(reader, None)
    if not header:
        raise UserFacingError("EDINET コードリストの CSV にヘッダがありません")

    index = {name: i for i, name in enumerate(header)}
    missing = [col for col in REQUIRED_CODE_LIST_COLUMNS if col not in index]
    if missing:
        raise UserFacingError(
            "EDINET コードリストの列構成が変わっています（不足している列: " + "、".join(missing) + "）"
        )

    def cell(row: list[str], column: str) -> str:
        i = index[column]
        return row[i].strip() if i < len(row) else ""

    rows: list[dict] = []
    for raw_row in reader:
        if not any(value.strip() for value in raw_row):
            continue  # 末尾の空行
        rows.append(
            {
                "edinet_code": cell(raw_row, "ＥＤＩＮＥＴコード"),
                "sec_code": cell(raw_row, "証券コード") or None,
                "name": cell(raw_row, "提出者名"),
            }
        )
    return rows


def save_code_list(db: Database, rows: list[dict]) -> dict:
    """`edinet_codes` を全置換で入れ替える（元ファイルを何度でも取り直せるため。SPEC §2.4.1a）。"""
    with db.write() as conn:
        conn.execute("DELETE FROM edinet_codes")
        conn.executemany(
            "INSERT INTO edinet_codes (edinet_code, sec_code, name) VALUES (?, ?, ?)",
            [(row["edinet_code"], row["sec_code"], row["name"]) for row in rows],
        )
    return {"saved": len(rows)}


def edinet_code_for(db: Database, symbol: str) -> str | None:
    """`stocks.symbol`（'7203.T'）から EDINET コードを引く（SPEC §2.4.1a・§2.4.3）。

    突合は「4桁コード + '0' = 5桁の証券コード」。国内銘柄（`.T`）以外は None。
    """
    if not symbol.endswith(".T"):
        return None
    sec_code = f"{code_from_symbol(symbol)}0"
    with db.connect() as conn:
        row = conn.execute(
            "SELECT edinet_code FROM edinet_codes WHERE sec_code = ?", (sec_code,)
        ).fetchone()
    return row["edinet_code"] if row else None


def fetch_and_save_code_list(
    db: Database,
    settings=None,
    cancel: threading.Event | None = None,
    client: HttpClient | None = None,
) -> dict:
    """コードリストを取得・パース・保存する（ブロッキング）。

    失敗したら `fetch_log` に記録して例外を投げ直す（中断 `Cancelled` は記録しない）。
    """
    if client is None:
        client = make_client(settings)
    try:
        content = fetch_code_list(client, cancel=cancel)
        rows = parse_code_list(content)
        result = save_code_list(db, rows)
    except Cancelled:
        raise
    except Exception as exc:
        db.log_fetch("edinet_codes", "list", f"error:{exc}")
        raise
    db.log_fetch("edinet_codes", "list", "ok")
    return result
