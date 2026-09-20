"""日証金 zandaka.csv（貸借取引残高）の取得・パース・保存（SPEC §2.3、§10.2）。

ネットワークは使わない。フィクスチャは合成データ（tests/fixtures/taisyaku_zandaka_*.csv）。
"""

import csv
import io
from pathlib import Path

import pytest

from app.database import Database
from app.errors import UserFacingError
from app.sources import taisyaku
from app.sources.base import HttpClient

FIXTURES = Path(__file__).parent / "fixtures"
FINAL_CSV = FIXTURES / "taisyaku_zandaka_final.csv"
PRELIM_CSV = FIXTURES / "taisyaku_zandaka_prelim.csv"

# SPEC §2.3.1 の実ヘッダ（全36列・順序どおり）。欠落列テスト用に自前の最小 CSV を組み立てるのに使う。
HEADERS = [
    "申込日", "決済日", "銘柄コード", "銘柄名", "取引所区分名", "上場区分", "速報／確報",
    "融資新規株数", "融資返済株数", "融資残高株数",
    "貸株新規株数", "貸株返済株数", "貸株残高株数",
    "差引残高株数",
    "融資新規金額", "融資返済金額", "融資残高金額",
    "貸株新規金額", "貸株返済金額", "貸株残高金額", "差引残高金額",
    "制度信用・買残高株数", "制度信用・売残高株数",
    "融資権利落額", "貸株権利落額",
    "合計・更新差金融資値上り", "合計・更新差金融資値下り",
    "合計・更新差金貸株値下り", "合計・更新差金貸株値上り",
    "総合回転日数",
    "融資・新規回転日数", "融資・返済回転日数", "融資・残高回転日数",
    "貸株・新規回転日数", "貸株・返済回転日数", "貸株・残高回転日数",
]


def _csv_bytes(rows: list[dict], headers: list[str] = HEADERS) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({h: row.get(h, "") for h in headers})
    return buf.getvalue().encode("cp932")


def _minimal_row(**overrides) -> dict:
    row = {h: "" for h in HEADERS}
    row["申込日"] = "2026/09/17"
    row["決済日"] = "2026/09/19"
    row["銘柄コード"] = "1234"
    row["銘柄名"] = "テスト"
    row["取引所区分名"] = "東証およびＰＴＳ"
    row["速報／確報"] = "確報"
    row["融資新規株数"] = "1,000"
    row["融資返済株数"] = "900"
    row["融資残高株数"] = "5,000"
    row["貸株新規株数"] = "500"
    row["貸株返済株数"] = "400"
    row["貸株残高株数"] = "2,000"
    row["差引残高株数"] = "3,000"
    row.update(overrides)
    return row


def _db_with_stocks(tmp_path, symbols) -> Database:
    db = Database(tmp_path / "taisyaku.db")
    db.init_schema()
    for symbol in symbols:
        code = taisyaku.code_from_symbol(symbol)
        db.upsert_stock(symbol, code, f"銘柄{code}", "東証", "JPY")
    return db


def _fetch_margin_rows(db: Database, symbol: str) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM margin_balances WHERE symbol = ? ORDER BY date", (symbol,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- フィクスチャ自体の性質 ----------


@pytest.mark.parametrize("path", [FINAL_CSV, PRELIM_CSV])
def test_fixture_keeps_cp932_and_crlf(path):
    """合成 CSV は実物どおり cp932・CRLF であること（SPEC §10.1）。

    `.gitattributes` で `-text` にしていないと、チェックアウト時に改行が LF へ正規化されて
    実物と構造が変わってしまう。それを検知するためのテスト。
    """
    raw = path.read_bytes()
    assert b"\r\n" in raw
    assert raw.count(b"\n") == raw.count(b"\r\n"), "CRLF でない改行が混ざっている"
    raw.decode("cp932")  # cp932 で読めること（読めなければここで落ちる）
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")  # UTF-8 ではないこと（全角文字を含むため）


# ---------- parse ----------


def test_parse_decodes_cp932_and_reads_by_header_name():
    content = FINAL_CSV.read_bytes()
    rows = taisyaku.parse(content)
    assert rows, "行が1つも取れていない"
    by_code = {r["code"]: r for r in rows}
    row = by_code["1301"]
    assert row["date"] == "2026-09-17"
    assert row["settle_date"] == "2026-09-19"
    assert row["kind"] == "final"
    assert row["yushi_new"] == 12300
    assert row["net_balance"] == 30000


def test_parse_missing_required_column_raises_and_returns_nothing():
    rows = [_minimal_row()]
    headers_without_net_balance = [h for h in HEADERS if h != "差引残高株数"]
    content = _csv_bytes(rows, headers=headers_without_net_balance)
    with pytest.raises(UserFacingError):
        taisyaku.parse(content)


def test_parse_unexpected_application_date_format_raises():
    """date は PK の一部。書式が変わったら黙って別物を入れず構造変更として止める。"""
    content = _csv_bytes([_minimal_row(**{"申込日": "20260917"})])
    with pytest.raises(UserFacingError):
        taisyaku.parse(content)


def test_parse_unreadable_settlement_date_becomes_none(caplog):
    """決済日は表示用なので、読めなくても止めずに None にする。"""
    import logging

    content = _csv_bytes([_minimal_row(**{"決済日": "20260919"})])
    with caplog.at_level(logging.WARNING):
        rows = taisyaku.parse(content)
    assert rows[0]["settle_date"] is None
    assert rows[0]["date"] == "2026-09-17"
    assert "決済日" in caplog.text


def test_parse_uses_only_tokyo_row_for_duplicate_code():
    content = FINAL_CSV.read_bytes()
    rows = taisyaku.parse(content)
    matches = [r for r in rows if r["code"] == "1302"]
    assert len(matches) == 1
    # 東証側の値（8000）が採用され、名証側の値（999）は使われない
    assert matches[0]["yushi_new"] == 8000


def test_parse_drops_code_with_no_tokyo_row():
    content = FINAL_CSV.read_bytes()
    rows = taisyaku.parse(content)
    codes = {r["code"] for r in rows}
    assert "1400" not in codes  # 名証のみのコードは行なし扱い


def test_parse_negative_net_balance():
    content = FINAL_CSV.read_bytes()
    rows = taisyaku.parse(content)
    by_code = {r["code"]: r for r in rows}
    assert by_code["1500"]["net_balance"] == -1234


def test_parse_alnum_and_five_digit_codes():
    content = FINAL_CSV.read_bytes()
    rows = taisyaku.parse(content)
    codes = {r["code"] for r in rows}
    assert "130A" in codes
    assert "13001" in codes


def test_parse_empty_and_dash_share_fields_become_none():
    rows = [_minimal_row(銘柄コード="9001", 融資新規株数="", 融資返済株数="-")]
    content = _csv_bytes(rows)
    parsed = taisyaku.parse(content)
    row = parsed[0]
    assert row["yushi_new"] is None
    assert row["yushi_repay"] is None


def test_parse_unknown_kind_value_is_kept_as_prelim_with_warning(caplog):
    content = PRELIM_CSV.read_bytes()
    with caplog.at_level("WARNING"):
        rows = taisyaku.parse(content)
    by_code = {r["code"]: r for r in rows}
    assert by_code["1305"]["kind"] == "prelim"
    assert "速  報" in caplog.text  # 実値が警告ログに出ていること


def test_parse_normal_prelim_kind():
    content = PRELIM_CSV.read_bytes()
    rows = taisyaku.parse(content)
    by_code = {r["code"]: r for r in rows}
    assert by_code["1700"]["kind"] == "prelim"


# ---------- save ----------


def test_save_basic_insert(tmp_path):
    db = _db_with_stocks(tmp_path, ["1301.T"])
    rows = taisyaku.parse(FINAL_CSV.read_bytes())
    result = taisyaku.save(db, rows, ["1301.T"], fetched_at="2026-09-18 10:47:00")
    assert result["saved"] == 1
    assert result["skipped"] == 0
    assert result["date"] == "2026-09-17"
    assert result["missing"] == []

    saved_rows = _fetch_margin_rows(db, "1301.T")
    assert len(saved_rows) == 1
    assert saved_rows[0]["kind"] == "final"
    assert saved_rows[0]["net_balance"] == 30000
    assert saved_rows[0]["fetched_at"] == "2026-09-18 10:47:00"


def test_save_missing_symbol_not_error(tmp_path):
    """貸借銘柄でない（行が無い）銘柄があってもエラーにしない。"""
    db = _db_with_stocks(tmp_path, ["1301.T", "9999.T"])
    rows = taisyaku.parse(FINAL_CSV.read_bytes())
    result = taisyaku.save(db, rows, ["1301.T", "9999.T"])
    assert result["missing"] == ["9999.T"]
    assert _fetch_margin_rows(db, "9999.T") == []


def test_save_code_with_only_non_tokyo_row_is_missing(tmp_path):
    db = _db_with_stocks(tmp_path, ["1400.T"])
    rows = taisyaku.parse(FINAL_CSV.read_bytes())
    result = taisyaku.save(db, rows, ["1400.T"])
    assert result["missing"] == ["1400.T"]
    assert _fetch_margin_rows(db, "1400.T") == []


def test_save_final_overwrites_prelim(tmp_path):
    """速報が先に入っていても、後から来る確報で上書きされる。"""
    db = _db_with_stocks(tmp_path, ["1301.T"])
    prelim_rows = taisyaku.parse(PRELIM_CSV.read_bytes())
    final_rows = taisyaku.parse(FINAL_CSV.read_bytes())

    first = taisyaku.save(db, prelim_rows, ["1301.T"], fetched_at="2026-09-17 15:00:00")
    assert first["saved"] == 1
    assert _fetch_margin_rows(db, "1301.T")[0]["kind"] == "prelim"

    second = taisyaku.save(db, final_rows, ["1301.T"], fetched_at="2026-09-18 10:47:00")
    assert second["saved"] == 1
    assert second["skipped"] == 0
    rows_after = _fetch_margin_rows(db, "1301.T")
    assert len(rows_after) == 1
    assert rows_after[0]["kind"] == "final"
    assert rows_after[0]["yushi_new"] == 12300  # 確報側の値
    assert rows_after[0]["fetched_at"] == "2026-09-18 10:47:00"


def test_save_prelim_does_not_overwrite_final(tmp_path):
    """確報が先に入っている場合、後から来る速報では上書きされない。"""
    db = _db_with_stocks(tmp_path, ["1301.T"])
    prelim_rows = taisyaku.parse(PRELIM_CSV.read_bytes())
    final_rows = taisyaku.parse(FINAL_CSV.read_bytes())

    first = taisyaku.save(db, final_rows, ["1301.T"], fetched_at="2026-09-18 10:47:00")
    assert first["saved"] == 1
    assert _fetch_margin_rows(db, "1301.T")[0]["kind"] == "final"

    second = taisyaku.save(db, prelim_rows, ["1301.T"], fetched_at="2026-09-19 09:00:00")
    assert second["saved"] == 0
    assert second["skipped"] == 1
    rows_after = _fetch_margin_rows(db, "1301.T")
    assert len(rows_after) == 1
    assert rows_after[0]["kind"] == "final"
    assert rows_after[0]["yushi_new"] == 12300  # 確報側の値のまま
    assert rows_after[0]["fetched_at"] == "2026-09-18 10:47:00"  # 上書きされていない


def test_save_only_registered_symbols(tmp_path):
    """symbols に無い（未登録の）銘柄コードの行は保存されない。"""
    db = _db_with_stocks(tmp_path, ["1301.T"])
    rows = taisyaku.parse(FINAL_CSV.read_bytes())
    result = taisyaku.save(db, rows, ["1301.T"])
    assert result["saved"] == 1
    # 1302 は symbols に含めていないので保存されていないはず
    assert _fetch_margin_rows(db, "1302.T") == []


# ---------- fetch_zandaka ----------


class _FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code
        self.text = ""


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers, timeout))
        return self.response


def test_fetch_zandaka_returns_raw_bytes():
    payload = FINAL_CSV.read_bytes()
    session = _FakeSession(_FakeResponse(payload))
    client = HttpClient(
        source="taisyaku",
        min_interval=0,
        agent="ChronosChart-test",
        session=session,
        clock=lambda: 0.0,
        sleep=lambda s: None,
    )
    result = taisyaku.fetch_zandaka(client)
    assert result == payload
    assert session.calls[0][0] == taisyaku.ZANDAKA_URL


def test_make_client_uses_taisyaku_source_and_min_interval():
    client = taisyaku.make_client()
    assert isinstance(client, HttpClient)
    assert client.source == "taisyaku"
    assert client.min_interval >= 5.0
