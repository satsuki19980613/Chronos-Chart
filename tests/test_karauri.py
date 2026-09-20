"""空売り残高の取得とパース（SPEC §2.2）。ネットワークは使わない（合成 HTML と fake session）。"""

import logging
from pathlib import Path

import pytest

from app.database import Database
from app.errors import UserFacingError
from app.settings import Settings
from app.sources import base
from app.sources.base import HttpClient
from app.sources.karauri import compute_totals, fetch_html, make_client, parse, save

FIXTURE = Path(__file__).parent / "fixtures" / "karauri_synthetic.html"


@pytest.fixture(autouse=True)
def fresh_source_state():
    base._states.clear()


@pytest.fixture
def html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def rows(html) -> list[dict]:
    return parse(html)


# ---------- parse: 構造 ----------

def test_finds_the_sort_table_not_the_company_info_table(rows):
    """ページ内にもう1枚 mtb1 テーブルがあっても id="sort" で正しく引けること。"""
    assert len(rows) == 10  # 11 データ行のうち1組が重複（後述）


def test_missing_sort_table_raises_and_returns_nothing():
    html = "<html><body><table class='mtb1'><tr><td>x</td></tr></table></body></html>"
    with pytest.raises(UserFacingError):
        parse(html)


def test_wrong_header_column_count_raises():
    html = """
    <table id="sort" class="mtb2">
    <tr><th>計算日</th><th>空売り者</th><th>残高割合</th></tr>
    <tr class="obb"><td>x</td><td>y</td><td>z</td></tr>
    </table>
    """
    with pytest.raises(UserFacingError):
        parse(html)


def test_wrong_data_row_column_count_raises_and_returns_nothing():
    html = """
    <table id="sort" class="mtb2">
    <tr><th>計算日</th><th>空売り者</th><th>残高割合</th><th>増減率</th><th>残高数量</th><th>増減量</th><th>備考</th></tr>
    <tr class="obb"><td>2026/09/16</td><td><a href="/1234/?f=1">A</a></td><td>1.0%</td><td>+0.1%</td><td>100株</td></tr>
    </table>
    """
    with pytest.raises(UserFacingError):
        parse(html)


def test_unexpected_calc_date_format_raises():
    """calc_date は PK の一部。書式が変わったら黙って別物を入れず構造変更として止める。"""
    html = """
    <table id="sort" class="mtb2">
    <tr><th>計算日</th><th>空売り者</th><th>残高割合</th><th>増減率</th><th>残高数量</th><th>増減量</th><th>備考</th></tr>
    <tr class="obb"><td>2026年9月16日</td><td><a href="/1234/?f=1">A</a></td><td>1.0%</td><td>+0.1%</td><td>100株</td><td>+1</td><td></td></tr>
    </table>
    """
    with pytest.raises(UserFacingError):
        parse(html)


# ---------- parse: 数値・NULL ----------

def test_normal_increase_row(rows):
    row = next(r for r in rows if r["calc_date"] == "2026-09-16" and r["holder_id"] == "901")
    assert row["holder"] == "アルファ・キャピタル"
    assert row["ratio"] == pytest.approx(2.040)
    assert row["ratio_delta"] == pytest.approx(0.060)
    assert row["quantity"] == 1_931_142
    assert row["qty_delta"] == 60_277
    assert row["note"] == ""


def test_normal_decrease_row(rows):
    row = next(r for r in rows if r["calc_date"] == "2026-09-15" and r["holder_id"] == "902")
    assert row["ratio_delta"] == pytest.approx(-0.030)
    assert row["quantity"] == 800_000
    assert row["qty_delta"] == -15_213


def test_report_obligation_lost_note_is_kept(rows):
    row = next(r for r in rows if r["holder_id"] == "905")
    assert row["note"] == "報告義務消失"
    assert row["ratio"] == pytest.approx(0.450)


def test_re_in_note_is_kept(rows):
    row = next(r for r in rows if r["holder_id"] == "906")
    assert row["note"] == "再IN（前回2026-01-15）"


def test_zero_percent_delta_and_empty_qty_delta_are_parsed(rows):
    """増減率 0% と、増減量が空文字の行（td にクラスが付かない）。"""
    row = next(r for r in rows if r["holder_id"] == "907")
    assert row["ratio_delta"] == 0.0
    assert row["qty_delta"] is None


def test_quantity_dash_and_empty_are_none(rows):
    dash_row = next(r for r in rows if r["holder_id"] == "908")
    empty_row = next(r for r in rows if r["holder_id"] == "909")
    assert dash_row["quantity"] is None
    assert empty_row["quantity"] is None


def test_calc_date_uses_cell_text_not_href(rows):
    """href 側の日付ではなく、セルのテキストの YYYY/MM/DD を使う。"""
    for row in rows:
        assert "/" not in row["calc_date"]
        assert len(row["calc_date"]) == 10  # YYYY-MM-DD


# ---------- parse: holder_id ----------

def test_holder_id_comes_from_link_f_param(rows):
    row = next(r for r in rows if r["calc_date"] == "2026-09-16" and r["holder_id"] == "901")
    assert row["holder"] == "アルファ・キャピタル"


def test_same_holder_id_with_different_display_name_is_not_merged_by_name(rows):
    """名称ゆれ: 同じ holder_id (901) で表示名が違う行が、別の計算日として両方残る。"""
    matches = [r for r in rows if r["holder_id"] == "901"]
    assert len(matches) == 2
    names = {r["holder"] for r in matches}
    assert names == {"アルファ・キャピタル", "アルファキャピタル証券"}


def test_holder_without_link_uses_name_as_holder_id(rows, caplog):
    with caplog.at_level(logging.WARNING):
        rows2 = parse(FIXTURE.read_text(encoding="utf-8"))
    row = next(r for r in rows2 if r["holder"] == "ガンマ・パートナーズ")
    assert row["holder_id"] == "ガンマ・パートナーズ"
    assert "no link" in caplog.text or "holder" in caplog.text


def test_duplicate_calc_date_and_holder_id_keeps_the_later_row(rows, caplog):
    """同一 (計算日, holder_id) の重複行は後の行を採用して警告する。"""
    matches = [r for r in rows if r["calc_date"] == "2026-09-16" and r["holder_id"] == "904"]
    assert len(matches) == 1
    row = matches[0]
    # フィクスチャの2つ目（後の）行の値
    assert row["ratio"] == pytest.approx(1.550)
    assert row["quantity"] == 705_000


def test_duplicate_row_logs_a_warning(html, caplog):
    with caplog.at_level(logging.WARNING):
        parse(html)
    assert "duplicate" in caplog.text


def test_unknown_note_is_logged(caplog):
    html = """
    <table id="sort" class="mtb2">
    <tr><th>計算日</th><th>空売り者</th><th>残高割合</th><th>増減率</th><th>残高数量</th><th>増減量</th><th>備考</th></tr>
    <tr class="obb"><td><a href="/1234/?date=2026-09-01">2026/09/01</a></td><td><a href="/1234/?f=1">A</a></td><td>1.0%</td><td>+0.1%</td><td>100株</td><td>+10</td><td>謎の注記</td></tr>
    </table>
    """
    with caplog.at_level(logging.WARNING):
        parse(html)
    assert "unknown note" in caplog.text


# ---------- make_client ----------

@pytest.fixture
def settings(tmp_path) -> Settings:
    db = Database(tmp_path / "test.db")
    db.init_schema()
    return Settings(db)


def test_make_client_requires_scrape_contact(settings):
    with pytest.raises(UserFacingError):
        make_client(settings)


def test_make_client_builds_a_karauri_client(settings):
    settings.update({"scrape_contact": "me@example.test", "scrape_interval_sec": 12})
    client = make_client(settings)
    assert isinstance(client, HttpClient)
    assert client.source == "karauri"
    assert client.no_retry_statuses == frozenset({403, 429})
    assert client.min_interval == 12
    assert client._agent.endswith("(+me@example.test)")


def test_make_client_enforces_the_5_second_floor(settings):
    # scrape_interval_sec の Spec 自体が最小値 5 を強制するが、念のため make_client 側でも下限を守る
    settings.db.set_setting("scrape_interval_sec", "1")
    settings.db.set_setting("scrape_contact", "me@example.test")
    client = make_client(settings)
    assert client.min_interval == 5


# ---------- fetch_html ----------

class _FakeResponse:
    def __init__(self, text):
        self.status_code = 200
        self.text = text


class _FakeSession:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        return _FakeResponse(self.text)


def test_fetch_html_requests_the_symbol_page(html):
    session = _FakeSession(html)
    client = HttpClient("karauri", 0, session=session)
    result = fetch_html(client, "1234")
    assert result == html
    assert session.calls[0][0] == "https://karauri.net/1234/"


# ---------- save ----------

@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(tmp_path / "chronos.db")
    database.init_schema()
    database.upsert_stock("1234.T", "1234", "架空株式会社", "東証", "JPY")
    return database


def test_save_inserts_rows_and_totals(db, rows):
    result = save(db, "1234.T", rows)
    assert result["rows"] == len(rows)
    assert result["since"] == "2026-09-05"

    with db.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM short_positions WHERE symbol = ?", ("1234.T",)
        ).fetchone()[0]
        totals_count = conn.execute(
            "SELECT COUNT(*) FROM short_totals WHERE symbol = ?", ("1234.T",)
        ).fetchone()[0]
    assert count == len(rows)
    assert totals_count == result["dates"]
    assert totals_count == len(compute_totals(rows))


def test_save_only_replaces_rows_from_the_minimum_calc_date_onward(db, rows):
    """§2.2.2a: 最小計算日より古い、既存の収集済み行は消さない。"""
    old_date = "2026-01-01"  # rows の最小計算日 (2026-09-05) より前
    with db.write() as conn:
        conn.execute(
            "INSERT INTO short_positions (symbol, calc_date, holder_id, holder, ratio, "
            "ratio_delta, quantity, qty_delta, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("1234.T", old_date, "old-holder", "旧・報告者", 1.0, None, 100000, None, ""),
        )

    save(db, "1234.T", rows)

    with db.connect() as conn:
        old_row = conn.execute(
            "SELECT * FROM short_positions WHERE symbol = ? AND calc_date = ?",
            ("1234.T", old_date),
        ).fetchone()
    assert old_row is not None
    assert old_row["holder_id"] == "old-holder"

    # short_totals は short_positions の全行（古い行も含む）から再算出される
    with db.connect() as conn:
        all_rows = [dict(r) for r in conn.execute(
            "SELECT calc_date, holder_id, holder, ratio, ratio_delta, quantity, qty_delta, note "
            "FROM short_positions WHERE symbol = ?", ("1234.T",)
        ).fetchall()]
    expected_totals = compute_totals(all_rows)
    with db.connect() as conn:
        totals = [dict(r) for r in conn.execute(
            "SELECT date, total_ratio, total_qty, holders FROM short_totals WHERE symbol = ? ORDER BY date",
            ("1234.T",),
        ).fetchall()]
    assert totals == expected_totals


def test_save_replaces_rows_within_the_new_range_but_keeps_the_row_count_bounded(db, rows):
    """取得範囲内（calc_date >= d_min）の既存行は、パース結果で完全に置き換わる。"""
    d_min = min(r["calc_date"] for r in rows)
    with db.write() as conn:
        conn.execute(
            "INSERT INTO short_positions (symbol, calc_date, holder_id, holder, ratio, "
            "ratio_delta, quantity, qty_delta, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("1234.T", d_min, "stale-holder", "消えるはずの報告者", 9.9, None, 999999, None, ""),
        )

    save(db, "1234.T", rows)

    with db.connect() as conn:
        stale = conn.execute(
            "SELECT * FROM short_positions WHERE symbol = ? AND holder_id = 'stale-holder'",
            ("1234.T",),
        ).fetchone()
    assert stale is None


def test_save_with_no_rows_does_nothing(db):
    with db.write() as conn:
        conn.execute(
            "INSERT INTO short_totals (symbol, date, total_ratio, total_qty, holders) VALUES (?, ?, ?, ?, ?)",
            ("1234.T", "2026-01-01", 1.0, 100, 1),
        )
    result = save(db, "1234.T", [])
    assert result == {"rows": 0, "dates": 0, "since": None}
    with db.connect() as conn:
        totals_count = conn.execute(
            "SELECT COUNT(*) FROM short_totals WHERE symbol = ?", ("1234.T",)
        ).fetchone()[0]
    assert totals_count == 1  # 消されていない
