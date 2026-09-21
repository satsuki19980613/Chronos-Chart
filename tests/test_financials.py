"""財務数値の保存・取り込み記録・読み出し（SPEC §2.9・§3）。ネットワークは使わない。"""

import threading

import pytest

from app.database import Database
from app.errors import Cancelled, UserFacingError
from app.financials import (
    DEFAULT_MAX_DOCS,
    FINANCIAL_DOC_TYPES,
    delete_financials,
    estimate_financials,
    financials_job,
    imported_doc_ids,
    load_series,
    pending_financial_docs,
    record_doc,
    save_financials,
)
from app.jobs import JobManager
from app.sources import edinet
from app.sources.base import HttpError

SYMBOL = "7203.T"

WAIT = 5  # JobManager を使うテストでスレッドの終了を待つ上限（秒）


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(tmp_path / "test.db")
    database.init_schema()
    database.upsert_stock(SYMBOL, "7203", "トヨタ自動車", "TSE", "JPY")
    return database


def doc(doc_id: str, submit_at: str, period_end: str = "2024-03-31", standard: str | None = "jgaap") -> dict:
    return {"doc_id": doc_id, "submit_at": submit_at, "period_end": period_end, "standard": standard}


def row(
    item: str,
    value: float | None,
    period_end: str = "2024-03-31",
    basis: str = "consolidated",
    unit: str | None = "JPY",
    period_type: str = "FY",
) -> dict:
    return {
        "period_end": period_end,
        "item": item,
        "value": value,
        "unit": unit,
        "basis": basis,
        "period_type": period_type,
    }


# ---------- save_financials / load_series の基本 ----------

def test_new_save_returns_ascending_periods(db):
    result = save_financials(
        db,
        SYMBOL,
        doc("S100AAA", "2024-06-20 09:00"),
        [
            row("revenue", 1000.0, period_end="2023-03-31"),
            row("revenue", 1200.0, period_end="2024-03-31"),
            row("net_income", 100.0, period_end="2024-03-31"),
        ],
    )
    assert result == {"saved": 3, "skipped": 0}

    series = load_series(db, SYMBOL)
    assert series is not None
    assert [p["period_end"] for p in series["periods"]] == ["2023-03-31", "2024-03-31"]
    assert series["periods"][0]["items"] == {"revenue": 1000.0}
    assert series["periods"][1]["items"] == {"revenue": 1200.0, "net_income": 100.0}
    assert series["basis"] == "consolidated"
    assert series["standard"] == "jgaap"
    assert series["source_docs"] == [{"doc_id": "S100AAA", "submit_at": "2024-06-20 09:00"}]


def test_empty_rows_is_not_an_error(db):
    assert save_financials(db, SYMBOL, doc("S100AAA", "2024-06-20 09:00"), []) == {
        "saved": 0,
        "skipped": 0,
    }
    assert load_series(db, SYMBOL) is None


# ---------- 訂正報告書の扱い（SPEC §2.9.5） ----------

def test_newer_correction_overwrites_but_keeps_items_not_in_correction(db):
    """訂正が古い値を上書きし、訂正に無い項目（net_income）は残る。"""
    save_financials(
        db,
        SYMBOL,
        doc("S100AAA", "2024-06-20 09:00"),
        [
            row("revenue", 1000.0),
            row("net_income", 100.0),
        ],
    )
    result = save_financials(
        db,
        SYMBOL,
        doc("S100BBB", "2024-07-01 10:00"),
        [row("revenue", 1050.0)],  # 訂正報告書。net_income には触れない
    )
    assert result == {"saved": 1, "skipped": 0}

    series = load_series(db, SYMBOL)
    items = series["periods"][0]["items"]
    assert items["revenue"] == 1050.0
    assert items["net_income"] == 100.0  # 訂正に無い項目は残る


def test_older_document_imported_later_does_not_clobber_newer_value(db):
    """古い書類を後から取り込んでも新しい値を壊さない（skipped に数える）。"""
    save_financials(
        db,
        SYMBOL,
        doc("S100BBB", "2024-07-01 10:00"),
        [row("revenue", 1050.0)],
    )
    result = save_financials(
        db,
        SYMBOL,
        doc("S100AAA", "2024-06-20 09:00"),  # 古い書類
        [row("revenue", 1000.0)],
    )
    assert result == {"saved": 0, "skipped": 1}

    series = load_series(db, SYMBOL)
    assert series["periods"][0]["items"]["revenue"] == 1050.0  # 新しい値のまま


def test_same_submit_at_overwrites(db):
    """新しい行の submit_at が既存行と同じ（以上）ときは上書きする。"""
    save_financials(db, SYMBOL, doc("S100AAA", "2024-06-20 09:00"), [row("revenue", 1000.0)])
    result = save_financials(db, SYMBOL, doc("S100AAA", "2024-06-20 09:00"), [row("revenue", 1111.0)])
    assert result == {"saved": 1, "skipped": 0}
    assert load_series(db, SYMBOL)["periods"][0]["items"]["revenue"] == 1111.0


# ---------- 連結・単体の選択（SPEC §2.9.4） ----------

def test_consolidated_wins_when_both_present(db):
    save_financials(
        db,
        SYMBOL,
        doc("S100AAA", "2024-06-20 09:00"),
        [
            row("revenue", 1000.0, basis="consolidated"),
            row("revenue", 900.0, basis="nonconsolidated"),
        ],
    )
    series = load_series(db, SYMBOL)
    assert series["basis"] == "consolidated"
    assert series["periods"][0]["items"]["revenue"] == 1000.0


def test_nonconsolidated_used_when_no_consolidated_rows(db):
    save_financials(
        db,
        SYMBOL,
        doc("S100AAA", "2024-06-20 09:00"),
        [row("revenue", 900.0, basis="nonconsolidated")],
    )
    series = load_series(db, SYMBOL)
    assert series["basis"] == "nonconsolidated"
    assert series["periods"][0]["items"]["revenue"] == 900.0


# ---------- load_series の欠損時の振る舞い ----------

def test_load_series_returns_none_when_no_financials(db):
    assert load_series(db, SYMBOL) is None


def test_null_value_row_is_absent_from_items(db):
    save_financials(
        db,
        SYMBOL,
        doc("S100AAA", "2024-06-20 09:00"),
        [row("revenue", 1000.0), row("dps", None, unit="JPY/share")],
    )
    series = load_series(db, SYMBOL)
    assert "dps" not in series["periods"][0]["items"]
    assert series["periods"][0]["items"]["revenue"] == 1000.0


def test_standard_is_none_when_all_rows_have_no_standard(db):
    save_financials(
        db,
        SYMBOL,
        doc("S100AAA", "2024-06-20 09:00", standard=None),
        [row("revenue", 1000.0)],
    )
    series = load_series(db, SYMBOL)
    assert series["standard"] is None


def test_standard_taken_from_most_recently_submitted_doc(db):
    save_financials(
        db,
        SYMBOL,
        doc("S100AAA", "2024-06-20 09:00", period_end="2023-03-31", standard="jgaap"),
        [row("revenue", 1000.0, period_end="2023-03-31")],
    )
    save_financials(
        db,
        SYMBOL,
        doc("S100BBB", "2025-06-20 09:00", period_end="2024-03-31", standard="ifrs"),
        [row("revenue", 1200.0, period_end="2024-03-31")],
    )
    series = load_series(db, SYMBOL)
    assert series["standard"] == "ifrs"
    assert series["source_docs"] == [
        {"doc_id": "S100BBB", "submit_at": "2025-06-20 09:00"},
        {"doc_id": "S100AAA", "submit_at": "2024-06-20 09:00"},
    ]


# ---------- 中間期（HY）の扱い（SPEC §2.9.4a） ----------

def test_hy_rows_are_excluded_from_periods(db):
    """FY と HY が混在する保存 → periods に HY が混ざらない。"""
    save_financials(
        db,
        SYMBOL,
        doc("S100AAA", "2024-06-20 09:00", period_end="2024-03-31"),
        [
            row("revenue", 1200.0, period_end="2024-03-31", period_type="FY"),
            row("revenue", 600.0, period_end="2024-09-30", period_type="HY"),
        ],
    )
    series = load_series(db, SYMBOL)
    assert [p["period_end"] for p in series["periods"]] == ["2024-03-31"]
    assert series["interim"]["period_end"] == "2024-09-30"
    assert series["interim"]["items"] == {"revenue": 600.0}


def test_interim_is_latest_hy_with_prior_being_the_one_before(db):
    save_financials(
        db,
        SYMBOL,
        doc("S100AAA", "2024-06-20 09:00"),
        [
            row("revenue", 500.0, period_end="2023-09-30", period_type="HY"),
            row("revenue", 600.0, period_end="2024-09-30", period_type="HY"),
            row("revenue", 450.0, period_end="2022-09-30", period_type="HY"),
        ],
    )
    series = load_series(db, SYMBOL)
    assert series["interim"]["period_end"] == "2024-09-30"
    assert series["interim"]["items"] == {"revenue": 600.0}
    assert series["interim"]["prior"] == {"period_end": "2023-09-30", "items": {"revenue": 500.0}}


def test_interim_prior_is_none_when_only_one_hy_period(db):
    save_financials(
        db,
        SYMBOL,
        doc("S100AAA", "2024-06-20 09:00"),
        [row("revenue", 600.0, period_end="2024-09-30", period_type="HY")],
    )
    series = load_series(db, SYMBOL)
    assert series["interim"]["period_end"] == "2024-09-30"
    assert series["interim"]["prior"] is None


def test_interim_is_none_when_no_hy_rows(db):
    save_financials(
        db,
        SYMBOL,
        doc("S100AAA", "2024-06-20 09:00"),
        [row("revenue", 1200.0, period_type="FY")],
    )
    series = load_series(db, SYMBOL)
    assert series["interim"] is None


def test_hy_only_symbol_returns_empty_periods_with_interim(db):
    """FY が無く HY だけの銘柄でも None にはならない（periods: [] かつ interim あり）。"""
    save_financials(
        db,
        SYMBOL,
        doc("S100AAA", "2024-06-20 09:00"),
        [row("revenue", 600.0, period_end="2024-09-30", period_type="HY")],
    )
    series = load_series(db, SYMBOL)
    assert series is not None
    assert series["periods"] == []
    assert series["interim"]["period_end"] == "2024-09-30"


def test_period_type_defaults_to_fy_when_omitted(db):
    """period_type を省略した行は 'FY' として保存される。"""
    bare_row = {"period_end": "2024-03-31", "item": "revenue", "value": 1200.0, "unit": "JPY", "basis": "consolidated"}
    save_financials(db, SYMBOL, doc("S100AAA", "2024-06-20 09:00"), [bare_row])

    series = load_series(db, SYMBOL)
    assert [p["period_end"] for p in series["periods"]] == ["2024-03-31"]
    assert series["interim"] is None

    with db.connect() as conn:
        stored = conn.execute(
            "SELECT period_type FROM financials WHERE symbol = ? AND period_end = ? AND item = ?",
            (SYMBOL, "2024-03-31", "revenue"),
        ).fetchone()
    assert stored["period_type"] == "FY"


# ---------- financial_docs（取り込み済み書類の記録） ----------

def test_imported_doc_ids_returns_both_ok_and_failed(db):
    record_doc(db, SYMBOL, "S100AAA", "2024-06-20 09:00", "2024-03-31", "ok")
    record_doc(db, SYMBOL, "S100BBB", "2024-07-01 10:00", None, "error:parse failed")
    record_doc(db, SYMBOL, "S100CCC", "2024-08-01 10:00", "2024-03-31", "empty")

    assert imported_doc_ids(db, SYMBOL) == {"S100AAA", "S100BBB", "S100CCC"}


def test_record_doc_upserts_on_conflict(db):
    record_doc(db, SYMBOL, "S100AAA", "2024-06-20 09:00", None, "error:boom")
    record_doc(db, SYMBOL, "S100AAA", "2024-06-20 09:00", "2024-03-31", "ok")

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT result, period_end FROM financial_docs WHERE symbol = ? AND doc_id = ?",
            (SYMBOL, "S100AAA"),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["result"] == "ok"
    assert rows[0]["period_end"] == "2024-03-31"


# ---------- 削除（CASCADE と delete_financials） ----------

def test_deleting_stock_cascades_to_financials(db):
    """PRAGMA foreign_keys が有効で、stocks の DELETE が financials/financial_docs に伝播すること。"""
    save_financials(db, SYMBOL, doc("S100AAA", "2024-06-20 09:00"), [row("revenue", 1000.0)])
    record_doc(db, SYMBOL, "S100AAA", "2024-06-20 09:00", "2024-03-31", "ok")

    db.delete_stock(SYMBOL)

    with db.connect() as conn:
        fk_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk_on == 1
        remaining = conn.execute("SELECT COUNT(*) FROM financials WHERE symbol = ?", (SYMBOL,)).fetchone()[0]
        remaining_docs = conn.execute(
            "SELECT COUNT(*) FROM financial_docs WHERE symbol = ?", (SYMBOL,)
        ).fetchone()[0]
    assert remaining == 0
    assert remaining_docs == 0


def test_delete_financials_removes_rows_and_docs(db):
    save_financials(db, SYMBOL, doc("S100AAA", "2024-06-20 09:00"), [row("revenue", 1000.0)])
    record_doc(db, SYMBOL, "S100AAA", "2024-06-20 09:00", "2024-03-31", "ok")

    delete_financials(db, SYMBOL)

    assert load_series(db, SYMBOL) is None
    assert imported_doc_ids(db, SYMBOL) == set()


# ================================================================
# 取得ジョブと導線（P11-3。SPEC §2.9.1）
# ================================================================


def _disclosure(
    db,
    doc_id: str,
    symbol: str = SYMBOL,
    *,
    submit_at: str = "2024-06-20 09:00",
    doc_type_code: str = "120",
    period_end: str | None = "2024-03-31",
    withdrawal: int | None = None,
    role: str = "filer",
) -> None:
    """`disclosures` / `disclosure_links` に1件だけ書類を仕込む（EDINET を突合済みの状態を模す）。"""
    with db.write() as conn:
        conn.execute(
            """
            INSERT INTO disclosures (doc_id, doc_type_code, submit_at, period_end, withdrawal, category)
            VALUES (?, ?, ?, ?, ?, 'report')
            ON CONFLICT(doc_id) DO UPDATE SET
                doc_type_code = excluded.doc_type_code,
                submit_at     = excluded.submit_at,
                period_end    = excluded.period_end,
                withdrawal    = excluded.withdrawal
            """,
            (doc_id, doc_type_code, submit_at, period_end, withdrawal),
        )
        conn.execute(
            "INSERT INTO disclosure_links (doc_id, symbol, role) VALUES (?, ?, ?) "
            "ON CONFLICT(doc_id, symbol, role) DO NOTHING",
            (doc_id, symbol, role),
        )


class _FakeSettings:
    """`Settings` の代わり（tests/test_disclosures.py の `_FakeSettings` と同じ方針）。"""

    def __init__(self, api_key: str = "test-edinet-api-key"):
        self._api_key = api_key

    def get(self, key: str):
        return None

    def get_secret(self, key: str) -> str:
        assert key == "edinet_api_key"
        return self._api_key


class FakeCtx:
    """`app.jobs.JobContext` の代わり（tests/test_disclosures.py の `FakeCtx` と同じ方針）。"""

    def __init__(self):
        self.cancel = threading.Event()
        self.labels: list[str] = []

    def progress(self, current, total, label=""):
        self.labels.append(label)

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()


def _parsed(rows: list[dict], standard: str | None = "jgaap") -> dict:
    """`edinet.parse_financial_csv` の戻り値を模す。"""
    return {"standard": standard, "consolidated_available": True, "period_type": "FY", "rows": rows}


# ---------- pending_financial_docs ----------


def test_pending_only_returns_financial_doc_types(db):
    _disclosure(db, "S100FY0", doc_type_code="120")  # 有報
    _disclosure(db, "S100HY0", doc_type_code="160")  # 半期報
    _disclosure(db, "S100CF0", doc_type_code="130")  # 有報の訂正
    _disclosure(db, "S100CH0", doc_type_code="170")  # 半期報の訂正
    _disclosure(db, "S100Q00", doc_type_code="140")  # 四半期報告書（対象外）
    _disclosure(db, "S100OT0", doc_type_code="010")  # 全く関係ない書類（対象外）

    pending = pending_financial_docs(db, SYMBOL, limit=None)
    doc_ids = {d["doc_id"] for d in pending}
    assert doc_ids == {"S100FY0", "S100HY0", "S100CF0", "S100CH0"}


def test_pending_excludes_withdrawn(db):
    _disclosure(db, "S100AAA", withdrawal=None)
    _disclosure(db, "S100BBB", withdrawal=0)
    _disclosure(db, "S100CCC", withdrawal=1)  # 取下げ

    doc_ids = {d["doc_id"] for d in pending_financial_docs(db, SYMBOL, limit=None)}
    assert doc_ids == {"S100AAA", "S100BBB"}


def test_pending_excludes_already_recorded_docs_regardless_of_result(db):
    _disclosure(db, "S100AAA")
    _disclosure(db, "S100BBB")
    record_doc(db, SYMBOL, "S100AAA", "2024-06-20 09:00", "2024-03-31", "ok")
    record_doc(db, SYMBOL, "S100BBB", "2024-06-20 09:00", None, "error:boom")

    assert pending_financial_docs(db, SYMBOL, limit=None) == []


def test_pending_respects_limit_and_orders_newest_first(db):
    _disclosure(db, "S100OLD", submit_at="2022-06-20 09:00")
    _disclosure(db, "S100MID", submit_at="2023-06-20 09:00")
    _disclosure(db, "S100NEW", submit_at="2024-06-20 09:00")

    pending = pending_financial_docs(db, SYMBOL, limit=2)
    assert [d["doc_id"] for d in pending] == ["S100NEW", "S100MID"]


def test_pending_csv_flag_from_daily_cache(db, tmp_path):
    base_dir = tmp_path / "edinet_cache"
    _disclosure(db, "S100YES", submit_at="2024-06-20 09:00")
    _disclosure(db, "S100NO", submit_at="2024-06-20 09:00")
    _disclosure(db, "S100UNKNOWN_DOC", submit_at="2024-06-20 09:00")  # キャッシュに無い docID
    _disclosure(db, "S100NOCACHE", submit_at="2024-07-01 09:00")  # その日付のキャッシュ自体が無い

    edinet.write_cache(
        "2024-06-20",
        {
            "results": [
                {"docID": "S100YES", "csvFlag": "1"},
                {"docID": "S100NO", "csvFlag": "0"},
            ]
        },
        base_dir=base_dir,
    )

    pending = {d["doc_id"]: d for d in pending_financial_docs(db, SYMBOL, base_dir=base_dir, limit=None)}
    assert pending["S100YES"]["csv_flag"] == 1
    assert pending["S100NO"]["csv_flag"] == 0
    assert pending["S100UNKNOWN_DOC"]["csv_flag"] is None
    assert pending["S100NOCACHE"]["csv_flag"] is None


def test_pending_reads_cache_once_per_date(db, tmp_path, monkeypatch):
    base_dir = tmp_path / "edinet_cache"
    _disclosure(db, "S100AAA", submit_at="2024-06-20 09:00")
    _disclosure(db, "S100BBB", submit_at="2024-06-20 10:00")
    _disclosure(db, "S100CCC", submit_at="2024-06-20 11:00")
    edinet.write_cache("2024-06-20", {"results": []}, base_dir=base_dir)

    calls: list[str] = []
    real_read_cache = edinet.read_cache

    def counting_read_cache(date, base_dir=None):
        calls.append(date)
        return real_read_cache(date, base_dir=base_dir)

    monkeypatch.setattr(edinet, "read_cache", counting_read_cache)

    pending_financial_docs(db, SYMBOL, base_dir=base_dir, limit=None)

    assert calls == ["2024-06-20"]  # 3件とも同じ日付なので1回だけ


# ---------- financials_job ----------


def test_financials_job_requires_api_key(db):
    settings = _FakeSettings(api_key="")
    job = financials_job(db, settings)
    with pytest.raises(UserFacingError):
        job(FakeCtx(), {})


def test_financials_job_processes_oldest_first_and_saves(db, monkeypatch):
    _disclosure(db, "S100NEW", submit_at="2024-06-20 09:00", period_end="2024-03-31")
    _disclosure(db, "S100OLD", submit_at="2023-06-20 09:00", period_end="2023-03-31")

    calls: list[str] = []

    def fake_fetch(client, doc_id, api_key, cancel=None):
        calls.append(doc_id)
        return b"zip-bytes"

    def fake_parse(content):
        period_end = "2023-03-31" if calls[-1] == "S100OLD" else "2024-03-31"
        return _parsed([row("revenue", 1000.0, period_end=period_end)])

    monkeypatch.setattr(edinet, "fetch_financial_csv", fake_fetch)
    monkeypatch.setattr(edinet, "parse_financial_csv", fake_parse)

    result = financials_job(db, _FakeSettings())(FakeCtx(), {"symbol": SYMBOL})

    assert calls == ["S100OLD", "S100NEW"]  # 古い順に処理
    assert result == {
        "symbols": 1,
        "docs": 2,
        "saved": 2,
        "skipped": 0,
        "no_csv": 0,
        "errors": [],
        "aborted": None,
    }
    series = load_series(db, SYMBOL)
    assert [p["period_end"] for p in series["periods"]] == ["2023-03-31", "2024-03-31"]
    assert imported_doc_ids(db, SYMBOL) == {"S100NEW", "S100OLD"}


def test_financials_job_records_empty_when_no_rows_parsed(db, monkeypatch):
    _disclosure(db, "S100AAA")
    monkeypatch.setattr(edinet, "fetch_financial_csv", lambda client, doc_id, api_key, cancel=None: b"zip")
    monkeypatch.setattr(edinet, "parse_financial_csv", lambda content: _parsed([]))

    result = financials_job(db, _FakeSettings())(FakeCtx(), {"symbol": SYMBOL})

    assert result["saved"] == 0
    with db.connect() as conn:
        stored = conn.execute(
            "SELECT result FROM financial_docs WHERE symbol = ? AND doc_id = ?", (SYMBOL, "S100AAA")
        ).fetchone()
    assert stored["result"] == "empty"


def test_financials_job_skips_http_when_csv_flag_is_zero(db, tmp_path, monkeypatch):
    base_dir = tmp_path / "edinet_cache"
    _disclosure(db, "S100AAA", submit_at="2024-06-20 09:00")
    edinet.write_cache(
        "2024-06-20", {"results": [{"docID": "S100AAA", "csvFlag": "0"}]}, base_dir=base_dir
    )

    calls: list[str] = []
    monkeypatch.setattr(
        edinet, "fetch_financial_csv", lambda client, doc_id, api_key, cancel=None: calls.append(doc_id)
    )

    result = financials_job(db, _FakeSettings(), base_dir=base_dir)(FakeCtx(), {"symbol": SYMBOL})

    assert calls == []  # HTTP を出さない
    assert result["no_csv"] == 1
    with db.connect() as conn:
        stored = conn.execute(
            "SELECT result FROM financial_docs WHERE symbol = ? AND doc_id = ?", (SYMBOL, "S100AAA")
        ).fetchone()
    assert stored["result"] == "no_csv"


def test_financials_job_aborts_forbidden_on_403_and_keeps_previous_saves(db, monkeypatch):
    _disclosure(db, "S100OLD", submit_at="2023-06-20 09:00", period_end="2023-03-31")
    _disclosure(db, "S100NEW", submit_at="2024-06-20 09:00", period_end="2024-03-31")

    calls: list[str] = []

    def fake_fetch(client, doc_id, api_key, cancel=None):
        calls.append(doc_id)
        if doc_id == "S100NEW":
            raise HttpError("アクセス拒否", status=403)
        return b"zip"

    monkeypatch.setattr(edinet, "fetch_financial_csv", fake_fetch)
    monkeypatch.setattr(edinet, "parse_financial_csv", lambda content: _parsed([row("revenue", 1000.0)]))

    result = financials_job(db, _FakeSettings())(FakeCtx(), {"symbol": SYMBOL})

    assert result["aborted"] == "forbidden"
    assert calls == ["S100OLD", "S100NEW"]
    # 先に処理した S100OLD の保存は残る
    assert load_series(db, SYMBOL) is not None
    assert imported_doc_ids(db, SYMBOL) == {"S100OLD"}


def test_financials_job_aborts_after_five_consecutive_failures(db, monkeypatch):
    for i in range(6):
        _disclosure(db, f"S100D0{i}", submit_at=f"2024-06-2{i} 09:00")

    calls: list[str] = []

    def fake_fetch(client, doc_id, api_key, cancel=None):
        calls.append(doc_id)
        raise RuntimeError("boom")

    monkeypatch.setattr(edinet, "fetch_financial_csv", fake_fetch)

    result = financials_job(db, _FakeSettings())(FakeCtx(), {"symbol": SYMBOL})

    assert result["aborted"] == "failures"
    assert len(calls) == 5
    assert len(result["errors"]) == 5


def test_financials_job_failure_counter_resets_on_success(db, monkeypatch):
    _disclosure(db, "S100D00", submit_at="2024-06-20 09:00")
    _disclosure(db, "S100D01", submit_at="2024-06-21 09:00")
    _disclosure(db, "S100D02", submit_at="2024-06-22 09:00")
    _disclosure(db, "S100D03", submit_at="2024-06-23 09:00")
    _disclosure(db, "S100D04", submit_at="2024-06-24 09:00")
    _disclosure(db, "S100D05", submit_at="2024-06-25 09:00")
    _disclosure(db, "S100D06", submit_at="2024-06-26 09:00")

    calls: list[str] = []

    def fake_fetch(client, doc_id, api_key, cancel=None):
        calls.append(doc_id)
        # 2回失敗 → 1回成功 → 4回失敗、という並び。成功でカウンタが戻るので
        # 一度も5連続失敗にならず、7件すべてを処理しきる
        if doc_id == "S100D02":
            return b"zip"
        raise RuntimeError("boom")

    monkeypatch.setattr(edinet, "fetch_financial_csv", fake_fetch)
    monkeypatch.setattr(edinet, "parse_financial_csv", lambda content: _parsed([row("revenue", 1000.0)]))

    result = financials_job(db, _FakeSettings())(FakeCtx(), {"symbol": SYMBOL, "max_docs": 10})

    assert result["aborted"] is None
    assert len(calls) == 7
    assert len(result["errors"]) == 6
    assert result["saved"] == 1


def test_financials_job_cancelled_keeps_previous_saves(db, monkeypatch):
    _disclosure(db, "S100OLD", submit_at="2023-06-20 09:00")
    _disclosure(db, "S100NEW", submit_at="2024-06-20 09:00")

    calls: list[str] = []

    def fake_fetch(client, doc_id, api_key, cancel=None):
        calls.append(doc_id)
        return b"zip"

    monkeypatch.setattr(edinet, "fetch_financial_csv", fake_fetch)
    monkeypatch.setattr(edinet, "parse_financial_csv", lambda content: _parsed([row("revenue", 1000.0)]))

    manager = JobManager()

    def job(ctx, params):
        # 1件目の保存の直後に中断フラグを立てる。ctx.check() は次のループの先頭で見る
        original_progress = ctx.progress

        def progress_then_maybe_cancel(current, total, label=""):
            original_progress(current, total, label)
            if current == 1:
                ctx.cancel.set()

        ctx.progress = progress_then_maybe_cancel
        return financials_job(db, _FakeSettings())(ctx, params)

    manager.register("financials", job)
    started = manager.start("financials", {"symbol": SYMBOL})
    final = manager.join(started["id"], WAIT)

    assert final["state"] == "cancelled"
    assert calls == ["S100OLD"]  # 2件目には進まない
    assert load_series(db, SYMBOL) is not None  # 1件目の保存は残る
    assert imported_doc_ids(db, SYMBOL) == {"S100OLD"}


# ---------- estimate_financials ----------


def test_estimate_financials_without_api_key_does_not_raise(db):
    result = estimate_financials(db, _FakeSettings(api_key=""), SYMBOL)
    assert result["can_run"] is False
    assert result["reason"]


def test_estimate_financials_requests_excludes_csv_flag_zero(db, tmp_path):
    base_dir = tmp_path / "edinet_cache"
    _disclosure(db, "S100YES", submit_at="2024-06-20 09:00")
    _disclosure(db, "S100NO", submit_at="2024-06-20 09:00")
    _disclosure(db, "S100UNKNOWN_DOC", submit_at="2024-06-20 09:00")
    edinet.write_cache(
        "2024-06-20",
        {
            "results": [
                {"docID": "S100YES", "csvFlag": "1"},
                {"docID": "S100NO", "csvFlag": "0"},
            ]
        },
        base_dir=base_dir,
    )

    result = estimate_financials(db, _FakeSettings(), SYMBOL, base_dir=base_dir, max_docs=None)

    assert result["can_run"] is True
    assert result["docs"] == 3
    assert result["requests"] == 2  # csvFlag "0" の1件を除く（不明はカウントする）
