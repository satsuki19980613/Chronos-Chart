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
import gzip
import io
import json
import logging
import os
import re
import tempfile
import threading
import zipfile
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

from .. import config
from ..database import Database
from ..errors import Cancelled, UserFacingError
from ..fetcher import code_from_symbol
from .base import HttpClient, ResponseTooLarge, user_agent

log = logging.getLogger(__name__)

SOURCE = "edinet"
DOCUMENTS_URL = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
CODE_LIST_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"
MIN_INTERVAL = 1.0  # SPEC §2.4.5: 1リクエストあたり1秒以上空ける

CODE_LIST_MEMBER = "EdinetcodeDlInfo.csv"

# ---------- 書類本文の取得（P9-1。SPEC §2.4.8） ----------

DOCUMENT_URL = "https://api.edinet-fsa.go.jp/api/v2/documents/{doc_id}"
DOCUMENT_MAX_BYTES = 20 * 1024 * 1024  # 実測: 有報 1.2MB。桁違いの異常値だけ弾ければよいので余裕を持たせる
DOCUMENT_MAX_CHARS = 200_000  # モーダルに出す分量の上限。これ以上はリンク誘導があるので切り捨てて構わない
DOCUMENT_READ_TIMEOUT = 60.0  # 一覧取得より大きいファイルを扱うので、この用途だけ読み取りを長めにする

# `docID` は URL に埋め込むので、英数字だけであることを確かめてから組み立てる。
# `app.disclosures._DOC_ID_RE` と同じ考え方だが、import すると
# app.disclosures → app.sources.edinet → app.disclosures の循環になるためここに複製する。
_DOC_ID_RE = re.compile(r"^[A-Za-z0-9]+$")

_DOCUMENT_CACHE_MAX = 16  # プロセス内 LRU キャッシュの件数上限（SPEC §2.4.8: data/ には書かない）
_document_cache: "OrderedDict[str, dict]" = OrderedDict()

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


# ---------- 日次キャッシュ（SPEC §2.4.2） ----------


def cache_dir(base_dir: Path | None = None) -> Path:
    """キャッシュの置き場所。`base_dir` を渡さなければ `config.EDINET_CACHE_DIR`。"""
    return Path(base_dir) if base_dir is not None else config.EDINET_CACHE_DIR


def cache_path(date: str, base_dir: Path | None = None) -> Path:
    """`<キャッシュ置き場>/YYYY-MM-DD.json.gz` を返す（フォルダは作らない）。

    日付はファイル名になるので、`YYYY-MM-DD` ちょうどの書式だけを通す。`'../evil'` のような値で
    フォルダの外に書けてしまうのを防ぐのに加え、`'2026-9-1'` のようなゼロ詰めなしの表記を弾く
    （通してしまうと同じ日のキャッシュが2つのファイル名で作られ、`fetch_log` のキーともずれる）。
    """
    try:
        parsed = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise UserFacingError(f"日付の形式が不正です: {date!r}") from None
    if parsed.strftime("%Y-%m-%d") != date:
        raise UserFacingError(f"日付の形式が不正です: {date!r}")
    return cache_dir(base_dir) / f"{date}.json.gz"


def write_cache(date: str, data: dict, base_dir: Path | None = None) -> int:
    """`documents.json` のレスポンスをそのまま gzip+JSON で保存し、書いたバイト数を返す。"""
    path = cache_path(date, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = gzip.compress(json.dumps(data, ensure_ascii=False).encode("utf-8"), mtime=0)

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise
    return len(payload)


def read_cache(date: str, base_dir: Path | None = None) -> dict | None:
    """キャッシュを読む。無い・壊れているときは None（例外にしない）。"""
    path = cache_path(date, base_dir)
    if not path.exists():
        return None
    try:
        with gzip.open(path, "rb") as f:
            raw = f.read()
        return json.loads(raw.decode("utf-8"))
    # 途中で切れた gzip は EOFError、gzip でなければ BadGzipFile（OSError の一種）。
    # どれも「壊れている＝無いものとして扱う」（SPEC §2.4.2）。
    except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("EDINET キャッシュを読み取れませんでした（%s）: %s", path, exc)
        return None


def cached_dates(base_dir: Path | None = None) -> list[str]:
    """キャッシュがある日付を昇順で返す。"""
    directory = cache_dir(base_dir)
    if not directory.is_dir():
        return []
    dates = []
    for entry in directory.glob("*.json.gz"):
        name = entry.name[: -len(".json.gz")]
        try:
            datetime.strptime(name, "%Y-%m-%d")
        except ValueError:
            continue
        dates.append(name)
    return sorted(dates)


def fetch_day(
    db: Database,
    date: str,
    api_key: str,
    cancel: threading.Event | None = None,
    client: HttpClient | None = None,
    base_dir: Path | None = None,
) -> dict:
    """1日ぶんの書類一覧を取得し、キャッシュへ保存して `fetch_log` に記録する。"""
    if client is None:
        client = make_client()
    try:
        data = fetch_documents(client, date, api_key, cancel=cancel)
        count = len(data.get("results") or [])
        result = "ok" if count > 0 else "empty"
        written = write_cache(date, data, base_dir=base_dir)
    except Cancelled:
        raise
    except Exception as exc:
        db.log_fetch(SOURCE, date, f"error:{exc}")
        raise
    db.log_fetch(SOURCE, date, result)
    return {"date": date, "result": result, "documents": count, "bytes": written}


# ---------- 書類本文の取得・テキスト化（P9-1。SPEC §2.4.8） ----------


class DocumentTooLarge(UserFacingError):
    """書類 ZIP が `DOCUMENT_MAX_BYTES` を超えていたため、本体を読まずに中止した。"""

    def __init__(self, size_bytes: int):
        mb = size_bytes / (1024 * 1024)
        super().__init__(f"書類が大きすぎます（約 {mb:.1f} MB）")
        self.size_bytes = size_bytes


def clear_document_cache() -> None:
    """プロセス内キャッシュを空にする（テスト用）。"""
    _document_cache.clear()


def fetch_document_zip(
    client: HttpClient, doc_id: str, api_key: str, cancel: threading.Event | None = None
) -> bytes:
    """書類取得 API（`documents/<docID>?type=1`）から ZIP のバイト列を取る（SPEC §2.4.8）。

    `doc_id` は URL に直接埋め込むため、英数字だけであることを先に確かめる
    （`disclosures.viewer_url` と同じ考え方。ここでは import せず正規表現を複製している）。
    キーは自前で URL に組み込まず `params` で渡す（`HttpClient` の `mask_secrets` に任せる）。
    `Content-Length` が `DOCUMENT_MAX_BYTES` を超える場合は本体を読まずに `DocumentTooLarge` にする。
    """
    api_key = (api_key or "").strip()
    if not api_key:
        raise UserFacingError("EDINET の API キーが設定されていません")
    if not doc_id or not _DOC_ID_RE.match(doc_id):
        raise UserFacingError(f"不正な書類番号です: {doc_id!r}")

    url = DOCUMENT_URL.format(doc_id=doc_id)
    params = {"type": 1, "Subscription-Key": api_key}
    try:
        response = client.get(
            url,
            params=params,
            cancel=cancel,
            read_timeout=DOCUMENT_READ_TIMEOUT,
            max_bytes=DOCUMENT_MAX_BYTES,
        )
    except ResponseTooLarge as exc:
        raise DocumentTooLarge(exc.size_bytes) from None
    return response.content


# 空行の連続（改行だけの行が2つ以上）を1つに潰す
_BLANK_LINES_RE = re.compile(r"\n\s*\n+")


def _text_from_html(content: bytes) -> str:
    """1つの inline XBRL の HTML から本文テキストを取り出す。

    `script` / `style` はテキスト化前に取り除く。表は行ごとに改行、セルは全角スペースで区切る
    （体裁は問わない。読めれば十分という方針。SPEC §2.4.8）。
    """
    soup = BeautifulSoup(content, "lxml")
    # head ごと落とす。get_text() は <title> の中身も拾うので、残すと本文の先頭に
    # ファイル名（例: 0000000_header_0339814703806.htm）が混ざる（実データで確認）
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    return _BLANK_LINES_RE.sub("\n\n", text).strip()


def _title_from_html(content: bytes) -> str | None:
    soup = BeautifulSoup(content, "lxml")
    if soup.title:
        title_text = soup.title.get_text(strip=True)
        if title_text:
            return title_text
    # <title> が無ければ、最初の見出しらしい行（h1〜h3）を拾う
    heading = soup.find(["h1", "h2", "h3"])
    if heading:
        text = heading.get_text(strip=True)
        return text or None
    return None


def extract_document_text(content: bytes) -> dict:
    """書類 ZIP のバイト列から本文テキストを取り出す（SPEC §2.4.8）。

    `XBRL/PublicDoc/` 配下の `.htm`/`.html` だけを**ファイル名の昇順**で連結する
    （表紙 `0000000_header_…` → 本文 `0101010_honbun_…` の順に並ぶ実測に基づく）。
    ZIP として開けない・対象ファイルが1つも無い場合は、例外にせず空のテキストを返す
    （呼び出し側がこれを「テキストにできない」と判断してリンク誘導する。SPEC §2.4.8）。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            htm_names = [n for n in zf.namelist() if n.lower().endswith((".htm", ".html"))]
            # 本文の置き場所は書類によって `XBRL/PublicDoc/…` と `PublicDoc/…` の2通りがある
            # （実測: 有報・臨報・大量保有は前者、確認書は後者）。どちらも拾えるよう
            # パスに `PublicDoc/` を含むかで判定する。1つも無ければ、監査報告書などしか
            # 入っていない ZIP でもテキストを出せるよう .htm 全部にフォールバックする
            names = sorted(n for n in htm_names if "PublicDoc/" in n) or sorted(htm_names)
            if not names:
                return {"title": None, "text": "", "truncated": False}

            title: str | None = None
            parts: list[str] = []
            for name in names:
                raw = zf.read(name)
                if title is None:
                    title = _title_from_html(raw)
                text = _text_from_html(raw)
                if text:
                    parts.append(text)
    except zipfile.BadZipFile:
        return {"title": None, "text": "", "truncated": False}

    full_text = "\n\n".join(parts)
    truncated = len(full_text) > DOCUMENT_MAX_CHARS
    if truncated:
        full_text = full_text[:DOCUMENT_MAX_CHARS]
    return {"title": title, "text": full_text, "truncated": truncated}


def fetch_document_text(
    client: HttpClient, doc_id: str, api_key: str, cancel: threading.Event | None = None
) -> dict:
    """書類本文を取得してテキストにする（SPEC §2.4.8）。ディスクには一切書かない。

    プロセス内 LRU キャッシュ（最大 `_DOCUMENT_CACHE_MAX` 件）を使うので、同じ `doc_id` を
    何度表示しても HTTP は1回しか呼ばない。表示のたびに叩く操作であって取得履歴ではないため、
    `fetch_log` には書かない。
    """
    cached = _document_cache.get(doc_id)
    if cached is not None:
        _document_cache.move_to_end(doc_id)  # LRU: 直近アクセスを最後尾に
        return cached

    content = fetch_document_zip(client, doc_id, api_key, cancel=cancel)
    result = extract_document_text(content)

    _document_cache[doc_id] = result
    _document_cache.move_to_end(doc_id)
    if len(_document_cache) > _DOCUMENT_CACHE_MAX:
        _document_cache.popitem(last=False)  # 一番古いものを落とす
    return result


# ---------- 財務数値（type=5 CSV）の取得・パース（P11-1。SPEC §2.9） ----------

FINANCIAL_DOCUMENT_MAX_BYTES = DOCUMENT_MAX_BYTES  # 実測: 有報 ZIP は最大でも 248KB 程度。type=1 と同じ上限で足りる

# 本体 CSV の見分け方（SPEC §2.9.2、実測 2026-09-21）。ZIP の中は `XBRL_TO_CSV/` 配下で、
# 有報は `jpcrp<6桁>-asr-...`、半期報は `jpcrp<6桁>-ssr-...`。
# 半期報の実物は `jpcrp040300-ssr-001_...` だった（SPEC に書かれている `jpcrp050000-ssr` は誤り。
# メインが SPEC を直す前提で、ここでは実測どおりに実装する）。監査報告書 `jpaud-*` は対象外。
_FINANCIAL_CSV_MEMBER_RE = re.compile(r"^jpcrp\d{6}-(asr|ssr)-")


def fetch_financial_csv(
    client: HttpClient, doc_id: str, api_key: str, cancel: threading.Event | None = None
) -> bytes:
    """財務数値の書類取得 API（`documents/<docID>?type=5`）から ZIP のバイト列を取る（SPEC §2.9.1）。

    `fetch_document_zip`（type=1）とほぼ同じ処理だが、type=1 の既存の挙動を変えないために
    あえて共通化せず複製している。`doc_id` の英数字チェック・`DocumentTooLarge` は type=1 と同じ。
    この関数自体にはプロセス内キャッシュを付けない（キャッシュは表示用の `fetch_document_text` だけのもの）。
    """
    api_key = (api_key or "").strip()
    if not api_key:
        raise UserFacingError("EDINET の API キーが設定されていません")
    if not doc_id or not _DOC_ID_RE.match(doc_id):
        raise UserFacingError(f"不正な書類番号です: {doc_id!r}")

    url = DOCUMENT_URL.format(doc_id=doc_id)
    params = {"type": 5, "Subscription-Key": api_key}
    try:
        response = client.get(
            url,
            params=params,
            cancel=cancel,
            read_timeout=DOCUMENT_READ_TIMEOUT,
            max_bytes=FINANCIAL_DOCUMENT_MAX_BYTES,
        )
    except ResponseTooLarge as exc:
        raise DocumentTooLarge(exc.size_bytes) from None
    return response.content


# 要素ID（`jpcrp_cor:` を除く）→ (item, is_fallback)。
# `is_fallback=True` は「同じ item に他の要素IDが当たっていれば、そちらを優先する」印
# （メインの指示: revenue の `OperatingRevenue1`、net_income の `NetIncomeLoss` の2つだけ）。
# 表に無い要素IDは黙って捨てる（メインの指示）。
_ELEMENT_ITEMS: dict[str, tuple[str, bool]] = {
    # ---- 日本基準・共通 ----
    "NetSalesSummaryOfBusinessResults": ("revenue", False),
    "OperatingRevenue1SummaryOfBusinessResults": ("revenue", True),
    "OrdinaryIncomeLossSummaryOfBusinessResults": ("ordinary_income", False),
    "ProfitLossAttributableToOwnersOfParentSummaryOfBusinessResults": ("net_income", False),
    "NetIncomeLossSummaryOfBusinessResults": ("net_income", True),
    "ComprehensiveIncomeSummaryOfBusinessResults": ("comprehensive_income", False),
    "NetAssetsSummaryOfBusinessResults": ("net_assets", False),
    "TotalAssetsSummaryOfBusinessResults": ("total_assets", False),
    "NetAssetsPerShareSummaryOfBusinessResults": ("bps", False),
    "BasicEarningsLossPerShareSummaryOfBusinessResults": ("eps", False),
    "DilutedEarningsPerShareSummaryOfBusinessResults": ("diluted_eps", False),
    "EquityToAssetRatioSummaryOfBusinessResults": ("equity_ratio", False),
    "RateOfReturnOnEquitySummaryOfBusinessResults": ("roe", False),
    "PriceEarningsRatioSummaryOfBusinessResults": ("per", False),
    "PayoutRatioSummaryOfBusinessResults": ("payout_ratio", False),
    "NetCashProvidedByUsedInOperatingActivitiesSummaryOfBusinessResults": ("operating_cf", False),
    "NetCashProvidedByUsedInInvestingActivitiesSummaryOfBusinessResults": ("investing_cf", False),
    "NetCashProvidedByUsedInFinancingActivitiesSummaryOfBusinessResults": ("financing_cf", False),
    "CashAndCashEquivalentsSummaryOfBusinessResults": ("cash_and_equivalents", False),
    "CapitalStockSummaryOfBusinessResults": ("capital_stock", False),
    "TotalNumberOfIssuedSharesSummaryOfBusinessResults": ("shares_outstanding", False),
    "DividendPaidPerShareSummaryOfBusinessResults": ("dps", False),
    # ---- IFRS ----
    "RevenueIFRSSummaryOfBusinessResults": ("revenue", False),
    "ProfitLossBeforeTaxIFRSSummaryOfBusinessResults": ("pretax_income", False),
    "ProfitLossAttributableToOwnersOfParentIFRSSummaryOfBusinessResults": ("net_income", False),
    "ComprehensiveIncomeAttributableToOwnersOfParentIFRSSummaryOfBusinessResults": (
        "comprehensive_income",
        False,
    ),
    "EquityAttributableToOwnersOfParentIFRSSummaryOfBusinessResults": ("equity", False),
    "TotalAssetsIFRSSummaryOfBusinessResults": ("total_assets", False),
    # 罠: 要素IDは「自己資本比率」に見えるが、実物の項目名は「１株当たり親会社所有者帰属持分（IFRS）」
    # （単位 `JPYPerShares`）。メインが実測で確認済み（SoftBank グループ有報）。equity_ratio に
    # 入れると値の桁が全く合わなくなるので、必ず bps に割り当てる。
    "EquityToAssetRatioIFRSSummaryOfBusinessResults": ("bps", False),
    "RatioOfOwnersEquityToGrossAssetsIFRSSummaryOfBusinessResults": ("equity_ratio", False),
    "RateOfReturnOnEquityIFRSSummaryOfBusinessResults": ("roe", False),
    "PriceEarningsRatioIFRSSummaryOfBusinessResults": ("per", False),
    "BasicEarningsLossPerShareIFRSSummaryOfBusinessResults": ("eps", False),
    "DilutedEarningsLossPerShareIFRSSummaryOfBusinessResults": ("diluted_eps", False),
    "CashFlowsFromUsedInOperatingActivitiesIFRSSummaryOfBusinessResults": ("operating_cf", False),
    "CashFlowsFromUsedInInvestingActivitiesIFRSSummaryOfBusinessResults": ("investing_cf", False),
    "CashFlowsFromUsedInFinancingActivitiesIFRSSummaryOfBusinessResults": ("financing_cf", False),
    "CashAndCashEquivalentsIFRSSummaryOfBusinessResults": ("cash_and_equivalents", False),
    # ---- 米国基準（未観測。docs/research/xbrl-edinet.md からの推定。実物での確認はまだ無い） ----
    "RevenuesUSGAAPSummaryOfBusinessResults": ("revenue", False),
    "NetIncomeLossAttributableToOwnersOfParentUSGAAPSummaryOfBusinessResults": ("net_income", False),
    "TotalAssetsUSGAAPSummaryOfBusinessResults": ("total_assets", False),
}

_ACCOUNTING_STANDARD_MAP = {
    "japan gaap": "jgaap",
    "ifrs": "ifrs",
    "us gaap": "usgaap",
}

# 発行体そのものの数字（会社の株式数・資本金・1株配当・配当性向）は「提出会社の状況」にしかなく、
# 連結側の表には存在しない（IFRS・日本基準とも実測）。連結を採用していてもこれらだけは単体側から補う。
_NONCONSOLIDATED_ONLY_ITEMS = {"dps", "payout_ratio", "shares_outstanding", "capital_stock"}

# `pure` 単位を100倍して `%` にする item（自己資本比率・ROE・配当性向。実測値で確認済み）。
# PER も `pure` だが100倍しない（実測: 11.7 = 11.7倍）。
_PERCENT_ITEMS = {"roe", "equity_ratio", "payout_ratio"}

_NONCONSOLIDATED_SUFFIX = "_NonConsolidatedMember"

# 接尾辞なしのコンテキストID（連結・全社）。`Prior([1-4])` の数字が取れれば N 期前
_YEAR_CONTEXT_RE = re.compile(r"^(?:Current|Prior([1-4]))Year(?:Duration|Instant)$")
# 半期報の当期・前期中間期
_INTERIM_CONTEXT_RE = re.compile(r"^(Prior1)?Interim(?:Duration|Instant)$")

# 値が無い行の印。全角ハイフン（U+FF0D）が実物での表記（SPEC §2.9 補足。メインの実測）。
# 素朴な半角ハイフンや空文字も念のため同じ扱いにする。
_MISSING_FINANCIAL_VALUE_TOKENS = {"－", "-", ""}


def _last_day_of_month(year: int, month: int) -> int:
    """`calendar` に依存せず、月末日を返す（このファイルの他の関数と同じく標準ライブラリのみで書く）。"""
    if month == 2:
        is_leap = (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0)
        return 29 if is_leap else 28
    if month in (4, 6, 9, 11):
        return 30
    return 31


def _shift_fiscal_year_end(base: str, years_back: int) -> str | None:
    """`base`（'YYYY-MM-DD'）の `years_back` 年前を、月末を保ったまま返す。

    うるう年でずれるのを防ぐため：`base` がその月の末日なら、結果もその月の末日にする
    （例: 2026-02-28 の2年前は 2024-02-29。メインの指示・SPEC §2.9 補足のテスト事例）。
    `base` が読めない・不正なときは None（呼び出し側がその行を捨てる）。
    """
    try:
        year_s, month_s, day_s = base.split("-")
        year, month, day = int(year_s), int(month_s), int(day_s)
    except (AttributeError, ValueError):
        return None
    new_year = year - years_back
    if new_year < 1:
        return None
    if day == _last_day_of_month(year, month):
        new_day = _last_day_of_month(new_year, month)
    else:
        new_day = day
    return f"{new_year:04d}-{month:02d}-{new_day:02d}"


def _clean_financial_value(raw: str | None) -> float | None:
    """`値` 列を float にする。無い・読めない行は None（呼び出し側が行ごと捨てる）。"""
    if raw is None:
        return None
    text = raw.strip()
    if text in _MISSING_FINANCIAL_VALUE_TOKENS:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _normalize_financial_unit(unit_id: str, item: str) -> tuple[str, float] | None:
    """`ユニットID` を出力用の `(unit, 倍率)` にする。対象外の組み合わせは None（行を捨てる）。

    `単位` 列は空のことがある実測のため見ない。`pure` は比率系（ROE・自己資本比率・配当性向）
    だけ100倍して `%` にし、PER は100倍しない（実測値で確認済み。SPEC §2.9 補足）。
    """
    if unit_id == "JPY":
        return "JPY", 1.0
    if unit_id == "JPYPerShares":
        return "JPY/share", 1.0
    if unit_id == "shares":
        return "shares", 1.0
    if unit_id == "pure":
        if item in _PERCENT_ITEMS:
            return "%", 100.0
        if item == "per":
            return "times", 1.0
        log.warning("想定外の item に pure 単位の行がありました（item=%s）。行を捨てます", item)
        return None
    return None


def _resolve_financial_context(
    ctx: str, doc_period_type: str, fy_base: str | None, interim_end: str | None, prior_interim_end: str | None
) -> tuple[str, str] | None:
    """接尾辞を除いたコンテキストIDから `(period_end, period_type)` を決める（SPEC §2.9.4）。

    対象外のコンテキスト（セグメント別など、下の2つの正規表現のどちらにも一致しないもの）や、
    元になる日付が DEI から取れなかった場合は None（呼び出し側がその行を捨てる）。
    """
    m = _YEAR_CONTEXT_RE.match(ctx)
    if m:
        n = int(m.group(1)) if m.group(1) else 0
        # 半期報の Prior1Year は「前事業年度」＝ fy_base そのもの（offset 0）。
        # 半期報の PriorNYear は N-1 期前、有報の PriorNYear は N 期前（メインの実測に基づく指示）。
        offset = n if doc_period_type == "FY" else max(n - 1, 0)
        if not fy_base:
            return None
        period_end = _shift_fiscal_year_end(fy_base, offset)
        if not period_end:
            return None
        return period_end, "FY"

    m = _INTERIM_CONTEXT_RE.match(ctx)
    if m:
        is_prior = m.group(1) is not None
        period_end = prior_interim_end if is_prior else interim_end
        if not period_end:
            return None
        return period_end, "HY"

    return None


def parse_financial_csv(content: bytes) -> dict:
    """財務数値 CSV（type=5 の ZIP）を正規化した dict にする（SPEC §2.9.2〜§2.9.4）。

    戻り値: `{"standard", "consolidated_available", "period_type", "rows"}`。
    ZIP が壊れている・本体 CSV が無い・対象行が1行も無い、といった異常はすべて例外にせず
    `rows: []` で返す（1銘柄の1書類が変でも取り込みジョブ全体を止めないため。メインの指示）。
    """
    result: dict = {
        "standard": None,
        "consolidated_available": False,
        "period_type": None,
        "rows": [],
    }

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            member = next(
                (
                    name
                    for name in sorted(zf.namelist())
                    if name.startswith("XBRL_TO_CSV/")
                    and _FINANCIAL_CSV_MEMBER_RE.match(name.rsplit("/", 1)[-1])
                ),
                None,
            )
            if member is None:
                return result
            raw = zf.read(member)
    except (zipfile.BadZipFile, KeyError):
        return result

    try:
        text = raw.decode("utf-16")  # BOM 付き UTF-16LE（SPEC §2.9.2、実測）
    except UnicodeDecodeError:
        return result

    rows = list(csv.DictReader(io.StringIO(text), delimiter="\t"))
    if not rows:
        return result

    # ---- DEI（会計基準・期間種別・各期末日）を先に集める。実測ではすべて FilingDateInstant ----
    dei: dict[str, str] = {}
    for row in rows:
        elid = row.get("要素ID") or ""
        if elid.startswith("jpdei_cor:") and elid not in dei:
            dei[elid] = (row.get("値") or "").strip()

    standard_raw = dei.get("jpdei_cor:AccountingStandardsDEI", "")
    result["standard"] = _ACCOUNTING_STANDARD_MAP.get(standard_raw.strip().lower())

    doc_period_type = dei.get("jpdei_cor:TypeOfCurrentPeriodDEI")
    if doc_period_type not in ("FY", "HY"):
        # 対象は有報・半期報だけのはず（SPEC §2.9.1）。それ以外は解釈できないので rows: [] のまま返す
        return result
    result["period_type"] = doc_period_type

    if doc_period_type == "FY":
        fy_base = dei.get("jpdei_cor:CurrentFiscalYearEndDateDEI")
    else:
        fy_base = dei.get("jpdei_cor:PreviousFiscalYearEndDateDEI")
    interim_end = dei.get("jpdei_cor:CurrentPeriodEndDateDEI")
    prior_interim_end = dei.get("jpdei_cor:ComparativePeriodEndDateDEI")

    # ---- 本体行を集計。key: (period_end, period_type, item, 単体か) ----
    facts: dict[tuple[str, str, str, bool], dict] = {}
    for row in rows:
        elid = row.get("要素ID") or ""
        if not elid.startswith("jpcrp_cor:"):
            continue
        mapping = _ELEMENT_ITEMS.get(elid[len("jpcrp_cor:") :])
        if mapping is None:
            continue
        item, is_fallback = mapping

        ctx = row.get("コンテキストID") or ""
        is_nonconsolidated = ctx.endswith(_NONCONSOLIDATED_SUFFIX)
        base_ctx = ctx[: -len(_NONCONSOLIDATED_SUFFIX)] if is_nonconsolidated else ctx
        # `_NonConsolidatedMember` 以外の接尾辞（セグメント別など）は base_ctx が正規表現に
        # 一致しなくなるので、ここで自動的に捨てられる（SPEC §2.9.4）
        resolved = _resolve_financial_context(base_ctx, doc_period_type, fy_base, interim_end, prior_interim_end)
        if resolved is None:
            continue
        period_end, period_type_row = resolved

        value = _clean_financial_value(row.get("値"))
        if value is None:
            continue

        unit_id = (row.get("ユニットID") or "").strip()
        normalized = _normalize_financial_unit(unit_id, item)
        if normalized is None:
            continue
        unit, scale = normalized
        # 100倍したあとの2進浮動小数の端数（0.29 * 100 = 28.999999999999996 など）を消す。
        # 10桁は元の CSV の有効数字（実測で最大3〜4桁）を落とさず、端数だけを消せる桁数
        # （メインのレビュー指摘・2026-09-21）。倍率が 1.0 のときも同じ経路を通してよい。
        value = round(value * scale, 10)

        key = (period_end, period_type_row, item, is_nonconsolidated)
        existing = facts.get(key)
        if existing is None or (existing["is_fallback"] and not is_fallback):
            facts[key] = {"value": value, "unit": unit, "is_fallback": is_fallback}

    consolidated_available = any(not is_nc for (_, _, _, is_nc) in facts)
    result["consolidated_available"] = consolidated_available
    basis = "consolidated" if consolidated_available else "nonconsolidated"
    want_nonconsolidated = not consolidated_available

    out_rows: list[dict] = []
    present_keys: set[tuple[str, str, str]] = set()
    for (period_end, period_type_row, item, is_nc), fact in facts.items():
        if is_nc != want_nonconsolidated:
            continue
        out_rows.append(
            {
                "period_end": period_end,
                "period_type": period_type_row,
                "item": item,
                "value": fact["value"],
                "unit": fact["unit"],
                "basis": basis,
            }
        )
        present_keys.add((period_end, period_type_row, item))

    if basis == "consolidated":
        # dps / payout_ratio / shares_outstanding / capital_stock だけは単体側から補う
        # （SPEC §2.9 補足。会社そのものの数字なので連結・単体で意味が変わらない）。
        # 補って入れる行の basis は、採用した basis（consolidated）にする。
        for (period_end, period_type_row, item, is_nc), fact in facts.items():
            if not is_nc or item not in _NONCONSOLIDATED_ONLY_ITEMS:
                continue
            if (period_end, period_type_row, item) in present_keys:
                continue  # 連結側に同じ項目が既にあれば重複させない（実測では起きないはずの防御）
            out_rows.append(
                {
                    "period_end": period_end,
                    "period_type": period_type_row,
                    "item": item,
                    "value": fact["value"],
                    "unit": fact["unit"],
                    "basis": basis,
                }
            )

    out_rows.sort(key=lambda r: (r["period_end"], r["item"]))
    result["rows"] = out_rows
    return result
