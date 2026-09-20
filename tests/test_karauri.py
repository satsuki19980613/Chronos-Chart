"""空売り残高の取得とパース（SPEC §2.2）。ネットワークは使わない（合成 HTML と fake session）。"""

import logging
import threading
from pathlib import Path

import pytest
import requests

from app.database import Database
from app.errors import Cancelled, UserFacingError
from app.settings import Settings
from app.sources import base, karauri
from app.sources.base import HttpClient, HttpError
from app.sources.karauri import (
    compute_totals,
    estimate,
    fetch_html,
    fetch_one,
    make_client,
    parse,
    save,
    select_targets,
    short_all_job,
)

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


def test_position_closed_note_is_not_logged_as_unknown(caplog):
    """'ポジション解消' は報告義務消失を示す既知の文言なので警告しない（実サイトの 9984 で観測。SPEC §2.2.2/§2.2.3）。"""
    html = """
    <table id="sort" class="mtb2">
    <tr><th>計算日</th><th>空売り者</th><th>残高割合</th><th>増減率</th><th>残高数量</th><th>増減量</th><th>備考</th></tr>
    <tr class="obb"><td><a href="/1234/?date=2026-09-01">2026/09/01</a></td><td><a href="/1234/?f=1">A</a></td><td>0.0%</td><td>-1.0%</td><td>0株</td><td>-10</td><td>ポジション解消</td></tr>
    </table>
    """
    with caplog.at_level(logging.WARNING):
        rows = parse(html)
    assert "unknown note" not in caplog.text
    assert rows[0]["note"] == "ポジション解消"


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


# ---------- 取得の入口（P2-6）: fetch_one / short_all_job / estimate ----------

class _ScriptedSession:
    """呼び出しごとに事前に決めた挙動を返すフェイクセッション。

    plan の要素は ("html", text) / ("status", code) / ("raise",) のいずれか。
    """

    def __init__(self, plan):
        self.plan = list(plan)
        self.calls: list[str] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(url)
        if not self.plan:
            raise AssertionError(f"想定外の追加リクエスト: {url}")
        kind, *rest = self.plan.pop(0)
        if kind == "raise":
            raise requests.exceptions.ConnectionError("boom")
        if kind == "status":
            return _StatusResponse(rest[0])
        if kind == "html":
            return _HtmlResponse(rest[0])
        raise AssertionError(f"unknown plan step: {kind}")


class _HtmlResponse:
    def __init__(self, text):
        self.status_code = 200
        self.text = text


class _StatusResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.text = ""


def _scripted_client(plan):
    """待機なしの HttpClient と、呼び出しを記録するセッションを返す。"""
    session = _ScriptedSession(plan)
    client = HttpClient(
        source="karauri",
        min_interval=0,
        agent="ChronosChart-test",
        no_retry_statuses=frozenset({403, 429}),
        session=session,
        clock=lambda: 0.0,
        sleep=lambda s: None,
    )
    return client, session


class FakeCtx:
    """app.jobs.JobContext の代わり（tests/test_autoupdate.py と同じ方針）。"""

    def __init__(self):
        self.cancel = threading.Event()
        self.labels: list[str] = []

    def progress(self, current, total, label=""):
        self.labels.append(label)

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()


@pytest.fixture
def env(tmp_path):
    """`short_all_job` 用の db・settings（scrape_contact 設定済み）。"""
    database = Database(tmp_path / "job.db")
    database.init_schema()
    settings = Settings(database, keyring_backend=object())
    settings.update({"scrape_contact": "me@example.test", "scrape_interval_sec": 5, "short_recheck_hours": 24})
    return database, settings


def _add_stock(db, code, exchange="東証", currency="JPY"):
    db.upsert_stock(f"{code}.T", code, f"テスト{code}", exchange, currency)


# ---- fetch_one ----

def test_fetch_one_skips_non_domestic_symbol(db):
    result = fetch_one(db, None, "AAPL")
    assert result == {"symbol": "AAPL", "status": "skipped", "reason": "not_domestic"}


def test_fetch_one_success_saves_and_logs_ok(db, html, rows):
    client, session = _scripted_client([("html", html)])
    result = fetch_one(db, None, "1234.T", client=client)
    assert result["status"] == "ok"
    assert result["rows"] == len(rows)
    assert session.calls == ["https://karauri.net/1234/"]
    logged = db.get_fetch("karauri", "1234.T")
    assert logged["result"] == "ok"


def test_fetch_one_failure_logs_error_and_reraises(db):
    client, _ = _scripted_client([("raise",)])
    with pytest.raises(HttpError):
        fetch_one(db, None, "1234.T", client=client)
    logged = db.get_fetch("karauri", "1234.T")
    assert logged["result"].startswith("error:")


def test_fetch_one_cancelled_does_not_log_as_error(db):
    cancel = threading.Event()
    cancel.set()
    client, session = _scripted_client([])
    with pytest.raises(Cancelled):
        fetch_one(db, None, "1234.T", cancel=cancel, client=client)
    assert session.calls == []
    assert db.get_fetch("karauri", "1234.T") is None


# ---- select_targets / estimate ----

def test_select_targets_filters_to_domestic_symbols(env):
    database, settings = env
    _add_stock(database, "1111")
    database.upsert_stock("AAPL", "AAPL", "Apple", "NASDAQ", "USD")
    targets, skipped = select_targets(database, settings)
    assert targets == ["1111.T"]
    assert skipped == 0


def test_select_targets_skips_recently_fetched(env):
    database, settings = env
    _add_stock(database, "1111")
    _add_stock(database, "2222")
    database.log_fetch("karauri", "1111.T", "ok")
    targets, skipped = select_targets(database, settings)
    assert targets == ["2222.T"]
    assert skipped == 1


def test_select_targets_force_ignores_recheck(env):
    database, settings = env
    _add_stock(database, "1111")
    database.log_fetch("karauri", "1111.T", "ok")
    targets, skipped = select_targets(database, settings, force=True)
    assert targets == ["1111.T"]
    assert skipped == 0


def test_estimate_reports_targets_skipped_interval_and_contact(env):
    database, settings = env
    _add_stock(database, "1111")
    _add_stock(database, "2222")
    database.log_fetch("karauri", "1111.T", "ok")
    result = estimate(database, settings)
    assert result == {
        "targets": 1,
        "skipped": 1,
        "interval_sec": 5.0,
        "eta_sec": 5.0,
        "contact_ok": True,
    }


def test_estimate_contact_not_ok_when_scrape_contact_missing(tmp_path):
    database = Database(tmp_path / "e.db")
    database.init_schema()
    settings = Settings(database, keyring_backend=object())
    assert estimate(database, settings)["contact_ok"] is False


# ---- short_all_job ----

def test_short_all_job_requires_scrape_contact(tmp_path):
    database = Database(tmp_path / "noc.db")
    database.init_schema()
    _add_stock(database, "1111")
    settings = Settings(database, keyring_backend=object())  # scrape_contact 未設定
    job = short_all_job(database, settings)
    with pytest.raises(UserFacingError):
        job(FakeCtx(), {})


def test_short_all_job_aborts_immediately_on_403_without_retry(monkeypatch, env):
    database, settings = env
    _add_stock(database, "1111")
    _add_stock(database, "2222")
    client, session = _scripted_client([("status", 403)])
    monkeypatch.setattr(karauri, "make_client", lambda s: client)

    result = short_all_job(database, settings)(FakeCtx(), {})

    assert result["aborted"] == "forbidden"
    assert result["updated"] == []
    assert len(result["errors"]) == 1
    assert session.calls == ["https://karauri.net/1111/"]  # 2件目は試みられない


def test_short_all_job_aborts_immediately_on_429_without_retry(monkeypatch, env):
    database, settings = env
    _add_stock(database, "1111")
    client, session = _scripted_client([("status", 429)])
    monkeypatch.setattr(karauri, "make_client", lambda s: client)

    result = short_all_job(database, settings)(FakeCtx(), {})

    assert result["aborted"] == "forbidden"
    assert len(session.calls) == 1  # リトライしない


def test_short_all_job_aborts_after_three_consecutive_failures(monkeypatch, env):
    database, settings = env
    for code in ("1111", "2222", "3333", "4444"):
        _add_stock(database, code)
    client, session = _scripted_client([("raise",), ("raise",), ("raise",)])
    monkeypatch.setattr(karauri, "make_client", lambda s: client)

    result = short_all_job(database, settings)(FakeCtx(), {})

    assert result["aborted"] == "failures"
    assert result["updated"] == []
    assert len(result["errors"]) == 3
    assert len(session.calls) == 3  # 4件目は試みられない


def test_short_all_job_continues_after_one_or_two_failures(monkeypatch, env, html):
    database, settings = env
    for code in ("1111", "2222", "3333"):
        _add_stock(database, code)
    client, session = _scripted_client([("raise",), ("raise",), ("html", html)])
    monkeypatch.setattr(karauri, "make_client", lambda s: client)

    result = short_all_job(database, settings)(FakeCtx(), {})

    assert result["aborted"] is None
    assert result["updated"] == ["3333.T"]
    assert len(result["errors"]) == 2


def test_short_all_job_skips_recently_fetched_symbols(monkeypatch, env, html):
    database, settings = env
    _add_stock(database, "1111")
    _add_stock(database, "2222")
    database.log_fetch("karauri", "1111.T", "ok")
    client, session = _scripted_client([("html", html)])
    monkeypatch.setattr(karauri, "make_client", lambda s: client)

    result = short_all_job(database, settings)(FakeCtx(), {})

    assert result["skipped"] == 1
    assert result["updated"] == ["2222.T"]
    assert session.calls == ["https://karauri.net/2222/"]


def test_short_all_job_force_ignores_recheck(monkeypatch, env, html):
    database, settings = env
    _add_stock(database, "1111")
    database.log_fetch("karauri", "1111.T", "ok")
    client, session = _scripted_client([("html", html)])
    monkeypatch.setattr(karauri, "make_client", lambda s: client)

    result = short_all_job(database, settings)(FakeCtx(), {"force": True})

    assert result["skipped"] == 0
    assert result["updated"] == ["1111.T"]


def test_short_all_job_skips_non_domestic_symbols(monkeypatch, env, html):
    database, settings = env
    _add_stock(database, "1111")
    database.upsert_stock("AAPL", "AAPL", "Apple", "NASDAQ", "USD")
    client, session = _scripted_client([("html", html)])
    monkeypatch.setattr(karauri, "make_client", lambda s: client)

    result = short_all_job(database, settings)(FakeCtx(), {})

    assert result["updated"] == ["1111.T"]
    assert session.calls == ["https://karauri.net/1111/"]


def test_short_all_job_can_be_cancelled(monkeypatch, env):
    database, settings = env
    _add_stock(database, "1111")
    _add_stock(database, "2222")
    ctx = FakeCtx()
    ctx.cancel.set()
    client, session = _scripted_client([])
    monkeypatch.setattr(karauri, "make_client", lambda s: client)

    with pytest.raises(Cancelled):
        short_all_job(database, settings)(ctx, {})
    assert session.calls == []


def test_short_all_job_logs_ok_in_fetch_log_on_success(monkeypatch, env, html):
    database, settings = env
    _add_stock(database, "1111")
    client, _ = _scripted_client([("html", html)])
    monkeypatch.setattr(karauri, "make_client", lambda s: client)

    short_all_job(database, settings)(FakeCtx(), {})

    logged = database.get_fetch("karauri", "1111.T")
    assert logged["result"] == "ok"


def test_short_all_job_respects_symbols_param(monkeypatch, env, html):
    database, settings = env
    _add_stock(database, "1111")
    _add_stock(database, "2222")
    client, session = _scripted_client([("html", html)])
    monkeypatch.setattr(karauri, "make_client", lambda s: client)

    result = short_all_job(database, settings)(FakeCtx(), {"symbols": ["1111.T"]})

    assert result["updated"] == ["1111.T"]
    assert session.calls == ["https://karauri.net/1111/"]
