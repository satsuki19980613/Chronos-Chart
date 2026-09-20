"""EDINET 開示の取得範囲・差分・ジョブ（SPEC §2.4.2・§2.4.4・§2.4.5・§2.8.1）。

ネットワークは使わない。`edinet.fetch_day` は monkeypatch でフェイクに差し替える。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app import disclosures
from app.database import Database
from app.errors import Cancelled, UserFacingError
from app.jobs import JobManager
from app.sources import edinet
from app.sources.base import HttpClient, HttpError

WAIT = 5  # JobManager を使うテストでスレッドの終了を待つ上限（秒）


def _db(tmp_path) -> Database:
    db = Database(tmp_path / "disclosures.db")
    db.init_schema()
    return db


def _insert_price(db: Database, date: str, symbol: str = "1111.T", code: str = "1111") -> None:
    """`prices` に1行だけ入れる（外部キーがあるので銘柄も登録する）。"""
    db.upsert_stock(symbol, code, f"テスト{code}", "東証", "JPY")
    with db.write() as conn:
        conn.execute(
            "INSERT INTO prices (symbol, date, open, high, low, close, volume) "
            "VALUES (?, ?, 100, 100, 100, 100, 1000) "
            "ON CONFLICT(symbol, date) DO NOTHING",
            (symbol, date),
        )


def _set_fetch_log(db: Database, date: str, result: str, fetched_at: str) -> None:
    """`fetched_at` を明示的な値にして `fetch_log` へ書く（`log_fetch` は現在時刻しか使えないため）。"""
    with db.write() as conn:
        conn.execute(
            "INSERT INTO fetch_log (source, key, fetched_at, result) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(source, key) DO UPDATE SET fetched_at = excluded.fetched_at, result = excluded.result",
            (edinet.SOURCE, date, fetched_at, result),
        )


class _FakeSettings:
    """`Settings` の代わり。`edinet.make_client` が読む `get("scrape_contact")` にも応える。"""

    def __init__(self, api_key: str = "test-edinet-api-key"):
        self._api_key = api_key

    def get(self, key: str):
        return None

    def get_secret(self, key: str) -> str:
        assert key == "edinet_api_key"
        return self._api_key


class FakeCtx:
    """`app.jobs.JobContext` の代わり（tests/test_karauri.py と同じ方針）。"""

    def __init__(self):
        self.cancel = threading.Event()
        self.labels: list[str] = []

    def progress(self, current, total, label=""):
        self.labels.append(label)

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()


def _ok(date: str, documents: int = 1) -> dict:
    return {"date": date, "result": "ok", "documents": documents, "bytes": 10}


# ---------- is_finalized ----------


def test_is_finalized_true_only_after_next_day_0030_jst(tmp_path):
    db = _db(tmp_path)
    date = "2026-01-10"
    deadline = disclosures._finalize_deadline(date)

    before = (deadline - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    _set_fetch_log(db, date, "ok", before)
    assert disclosures.is_finalized(db, date) is False

    after = deadline.strftime("%Y-%m-%d %H:%M:%S")
    _set_fetch_log(db, date, "ok", after)
    assert disclosures.is_finalized(db, date) is True


def test_is_finalized_false_for_error_result_even_long_after(tmp_path):
    db = _db(tmp_path)
    date = "2026-01-10"
    deadline = disclosures._finalize_deadline(date)
    long_after = (deadline + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    _set_fetch_log(db, date, "error:何かおかしい", long_after)
    assert disclosures.is_finalized(db, date) is False


def test_is_finalized_false_when_no_record(tmp_path):
    db = _db(tmp_path)
    assert disclosures.is_finalized(db, "2026-01-10") is False


def test_is_finalized_false_when_fetched_at_is_corrupted(tmp_path):
    db = _db(tmp_path)
    _set_fetch_log(db, "2026-01-10", "ok", "not-a-timestamp")
    assert disclosures.is_finalized(db, "2026-01-10") is False


# ---------- fetch_range ----------


def test_fetch_range_no_prices_returns_none_start(tmp_path):
    db = _db(tmp_path)
    assert disclosures.fetch_range(db, today="2026-09-20") == (None, "2026-09-20")


def test_fetch_range_floors_oldest_price_to_ten_years(tmp_path):
    db = _db(tmp_path)
    _insert_price(db, "2010-01-01")
    start, end = disclosures.fetch_range(db, today="2026-09-20")
    assert end == "2026-09-20"
    assert start == "2016-09-20"  # 今日の10年前


def test_fetch_range_keeps_oldest_price_within_ten_years(tmp_path):
    db = _db(tmp_path)
    _insert_price(db, "2020-05-01")
    start, end = disclosures.fetch_range(db, today="2026-09-20")
    assert (start, end) == ("2020-05-01", "2026-09-20")


def test_fetch_range_start_after_today_returns_none_start(tmp_path):
    db = _db(tmp_path)
    _insert_price(db, "2030-01-01")
    assert disclosures.fetch_range(db, today="2026-09-20") == (None, "2026-09-20")


# ---------- pending_dates ----------


def test_pending_dates_returns_descending_order(tmp_path):
    db = _db(tmp_path)
    _insert_price(db, "2026-09-16")
    dates = disclosures.pending_dates(db, today="2026-09-20")
    assert dates == ["2026-09-20", "2026-09-19", "2026-09-18", "2026-09-17", "2026-09-16"]


def test_pending_dates_excludes_finalized_but_keeps_error_dates(tmp_path):
    db = _db(tmp_path)
    _insert_price(db, "2026-09-16")
    finalized_at = disclosures._finalize_deadline("2026-09-17").strftime("%Y-%m-%d %H:%M:%S")
    _set_fetch_log(db, "2026-09-17", "ok", finalized_at)  # 確定済み → 除外
    _set_fetch_log(db, "2026-09-18", "error:boom", finalized_at)  # エラーは確定しない → 残る

    dates = disclosures.pending_dates(db, today="2026-09-20")
    assert "2026-09-17" not in dates
    assert "2026-09-18" in dates
    assert dates == ["2026-09-20", "2026-09-19", "2026-09-18", "2026-09-16"]


def test_pending_dates_max_days_keeps_newest_n(tmp_path):
    db = _db(tmp_path)
    start = (datetime(2026, 9, 20) - timedelta(days=40)).strftime("%Y-%m-%d")
    _insert_price(db, start)

    dates = disclosures.pending_dates(db, today="2026-09-20", max_days=30)
    assert len(dates) == 30
    assert dates[0] == "2026-09-20"
    assert dates[-1] == "2026-08-22"


def test_pending_dates_redo_days_reincludes_recent_finalized_but_not_91_days_ago(tmp_path):
    db = _db(tmp_path)
    today = "2026-09-20"
    start = (datetime(2026, 9, 20) - timedelta(days=120)).strftime("%Y-%m-%d")
    _insert_price(db, start)

    recent = (datetime(2026, 9, 20) - timedelta(days=89)).strftime("%Y-%m-%d")  # 今日を含めて90日前
    too_old = (datetime(2026, 9, 20) - timedelta(days=90)).strftime("%Y-%m-%d")  # 91日前
    _set_fetch_log(db, recent, "ok", disclosures._finalize_deadline(recent).strftime("%Y-%m-%d %H:%M:%S"))
    _set_fetch_log(db, too_old, "ok", disclosures._finalize_deadline(too_old).strftime("%Y-%m-%d %H:%M:%S"))

    without_redo = disclosures.pending_dates(db, today=today)
    assert recent not in without_redo
    assert too_old not in without_redo

    with_redo = disclosures.pending_dates(db, today=today, redo_days=90)
    assert recent in with_redo
    assert too_old not in with_redo


def test_pending_dates_excludes_today_within_60_seconds_even_with_redo_days(tmp_path):
    db = _db(tmp_path)
    today = "2026-09-20"
    _insert_price(db, "2026-09-18")
    now = datetime(2026, 9, 20, 10, 0, 0)

    _set_fetch_log(db, today, "ok", (now - timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S"))
    assert today not in disclosures.pending_dates(db, today=today, now=now)
    assert today not in disclosures.pending_dates(db, today=today, redo_days=90, now=now)

    _set_fetch_log(db, today, "ok", (now - timedelta(seconds=61)).strftime("%Y-%m-%d %H:%M:%S"))
    assert today in disclosures.pending_dates(db, today=today, now=now)


# ---------- estimate ----------


def test_estimate_reports_targets_interval_and_api_key(tmp_path):
    db = _db(tmp_path)
    _insert_price(db, "2026-09-16")
    settings = _FakeSettings(api_key="a-key")
    result = disclosures.estimate(db, settings)
    assert result["interval_sec"] == edinet.MIN_INTERVAL
    assert result["eta_sec"] == result["targets"] * edinet.MIN_INTERVAL
    assert result["api_key_ok"] is True
    assert result["end"] == disclosures.today_jst()


def test_estimate_api_key_not_ok_when_missing(tmp_path):
    db = _db(tmp_path)
    settings = _FakeSettings(api_key="")
    assert disclosures.estimate(db, settings)["api_key_ok"] is False


# ---------- disclosures_job ----------


def test_job_requires_api_key(tmp_path):
    db = _db(tmp_path)
    settings = _FakeSettings(api_key="")
    job = disclosures.disclosures_job(db, settings)
    with pytest.raises(UserFacingError):
        job(FakeCtx(), {})


def test_job_progress_updates_per_fetch_call_and_forwards_base_dir(monkeypatch, tmp_path):
    db = _db(tmp_path)
    today = disclosures.today_jst()
    start = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=4)).strftime("%Y-%m-%d")
    _insert_price(db, start)
    settings = _FakeSettings()

    calls = []
    seen_base_dirs = []

    def fake_fetch_day(db_, date, api_key, cancel=None, client=None, base_dir=None):
        calls.append(date)
        seen_base_dirs.append(base_dir)
        return _ok(date)

    monkeypatch.setattr(edinet, "fetch_day", fake_fetch_day)
    ctx = FakeCtx()
    job = disclosures.disclosures_job(db, settings, base_dir=tmp_path / "cache")
    result = job(ctx, {})

    assert len(calls) == 5
    assert result["with_documents"] == calls
    assert result["empty"] == []
    assert ctx.labels[-1] == "開示の取得が完了"
    assert len(ctx.labels) == len(calls) + 1
    assert all(d == tmp_path / "cache" for d in seen_base_dirs)


def test_job_aborts_forbidden_on_429_and_keeps_previously_fetched(monkeypatch, tmp_path):
    db = _db(tmp_path)
    today = disclosures.today_jst()
    start = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=4)).strftime("%Y-%m-%d")
    _insert_price(db, start)
    settings = _FakeSettings()

    calls = []

    def fake_fetch_day(db_, date, api_key, cancel=None, client=None, base_dir=None):
        calls.append(date)
        if len(calls) == 2:
            raise HttpError("レート制限", status=429)
        return _ok(date)

    monkeypatch.setattr(edinet, "fetch_day", fake_fetch_day)
    result = disclosures.disclosures_job(db, settings)(FakeCtx(), {})

    assert result["aborted"] == "forbidden"
    assert len(calls) == 2
    assert len(result["with_documents"]) == 1
    assert "アクセスを拒否" in result["summary"]


def test_job_aborts_after_three_consecutive_generic_failures(monkeypatch, tmp_path):
    db = _db(tmp_path)
    today = disclosures.today_jst()
    start = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=5)).strftime("%Y-%m-%d")
    _insert_price(db, start)
    settings = _FakeSettings()

    calls = []

    def fake_fetch_day(db_, date, api_key, cancel=None, client=None, base_dir=None):
        calls.append(date)
        raise RuntimeError("boom")

    monkeypatch.setattr(edinet, "fetch_day", fake_fetch_day)
    result = disclosures.disclosures_job(db, settings)(FakeCtx(), {})

    assert result["aborted"] == "failures"
    assert len(calls) == 3
    assert len(result["errors"]) == 3
    assert result["with_documents"] == []
    assert "エラーが続いた" in result["summary"]


def test_job_continues_after_two_failures_then_succeeds(monkeypatch, tmp_path):
    db = _db(tmp_path)
    today = disclosures.today_jst()
    start = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=5)).strftime("%Y-%m-%d")
    _insert_price(db, start)
    settings = _FakeSettings()

    calls = []

    def fake_fetch_day(db_, date, api_key, cancel=None, client=None, base_dir=None):
        calls.append(date)
        if len(calls) <= 2:
            raise RuntimeError("boom")
        return _ok(date)

    monkeypatch.setattr(edinet, "fetch_day", fake_fetch_day)
    result = disclosures.disclosures_job(db, settings)(FakeCtx(), {})

    assert result["aborted"] is None
    assert len(result["errors"]) == 2
    assert len(result["with_documents"]) == len(calls) - 2


def test_job_cancelled_stops_but_keeps_previous_fetch_calls(monkeypatch, tmp_path):
    db = _db(tmp_path)
    today = disclosures.today_jst()
    start = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=5)).strftime("%Y-%m-%d")
    _insert_price(db, start)
    settings = _FakeSettings()

    calls = []

    def fake_fetch_day(db_, date, api_key, cancel=None, client=None, base_dir=None):
        calls.append(date)
        if len(calls) == 2:
            cancel.set()
        return _ok(date)

    monkeypatch.setattr(edinet, "fetch_day", fake_fetch_day)

    manager = JobManager()
    manager.register("disclosures", disclosures.disclosures_job(db, settings))
    started = manager.start("disclosures", {})
    final = manager.join(started["id"], WAIT)

    assert final["state"] == "cancelled"
    assert len(calls) == 2  # 3回目には進まない


def test_job_counts_empty_days_separately(monkeypatch, tmp_path):
    """書類0件の日は `empty` に入り、`days` には数えられる（土日祝を特別扱いしない SPEC §2.4.4）。"""
    db = _db(tmp_path)
    today = disclosures.today_jst()
    start = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=3)).strftime("%Y-%m-%d")
    _insert_price(db, start)
    settings = _FakeSettings()

    calls = []

    def fake_fetch_day(db_, date, api_key, cancel=None, client=None, base_dir=None):
        calls.append(date)
        if len(calls) % 2 == 0:
            return {"date": date, "result": "empty", "documents": 0, "bytes": 10}
        return _ok(date)

    monkeypatch.setattr(edinet, "fetch_day", fake_fetch_day)
    result = disclosures.disclosures_job(db, settings)(FakeCtx(), {})

    assert len(result["with_documents"]) == 2
    assert len(result["empty"]) == 2
    assert result["days"] == 4
    assert "うち書類あり 2日" in result["summary"]


def test_job_respects_max_days_param(monkeypatch, tmp_path):
    db = _db(tmp_path)
    today = disclosures.today_jst()
    start = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
    _insert_price(db, start)
    settings = _FakeSettings()

    calls = []

    def fake_fetch_day(db_, date, api_key, cancel=None, client=None, base_dir=None):
        calls.append(date)
        return _ok(date)

    monkeypatch.setattr(edinet, "fetch_day", fake_fetch_day)
    result = disclosures.disclosures_job(db, settings)(FakeCtx(), {"max_days": 3})

    assert len(calls) == 3
    assert len(result["with_documents"]) == 3


# ---------- ジョブと取得層のつなぎ目（fetch_day をフェイクに差し替えない） ----------


class _FakeResponse:
    def __init__(self, json_data):
        self._json = json_data
        self.content = b""
        self.status_code = 200
        self.text = ""

    def json(self):
        return self._json


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        return self.response


def test_job_writes_cache_and_fetch_log_end_to_end(monkeypatch, tmp_path):
    """ジョブ → 実物の `edinet.fetch_day` → キャッシュ書き込み → `fetch_log` までを通す。

    個々のテストは `fetch_day` をフェイクに差し替えているので、つなぎ目はここで1回だけ確かめる。
    HTTP はフェイクのセッションで止めるのでネットワークには出ない。
    """
    db = _db(tmp_path)
    today = disclosures.today_jst()
    oldest = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")
    _insert_price(db, oldest)
    settings = _FakeSettings()

    payload = {"metadata": {"status": "200", "message": ""}, "results": [{"docID": "S100ABCD"}]}
    session = _FakeSession(_FakeResponse(payload))
    monkeypatch.setattr(
        edinet,
        "make_client",
        lambda *a, **kw: HttpClient(
            source="edinet-test",
            min_interval=0,
            agent="ChronosChart-test",
            session=session,
            clock=lambda: 0.0,
            sleep=lambda s: None,
        ),
    )

    cache = tmp_path / "cache"
    result = disclosures.disclosures_job(db, settings, base_dir=cache)(FakeCtx(), {})

    assert result["days"] == 3
    assert edinet.cached_dates(base_dir=cache) == sorted(result["with_documents"])
    assert edinet.read_cache(oldest, base_dir=cache) == payload
    assert db.get_fetch(edinet.SOURCE, oldest)["result"] == "ok"

    # 2日前は「翌日00:30」を必ず過ぎているので確定済みになり、次回の対象から外れる
    assert disclosures.is_finalized(db, oldest) is True
    assert oldest not in disclosures.pending_dates(db)

    # キーはクエリパラメータで送られ、URL 文字列は自前で組み立てていない
    assert all(params.get("Subscription-Key") == "test-edinet-api-key" for _, params in session.calls)


# ---------- 突合・分類（SPEC §2.4.3・§2.4.6） ----------

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "edinet_documents_synthetic.json"


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _result_by_doc_id(data: dict, doc_id: str) -> dict:
    for row in data["results"]:
        if row.get("docID") == doc_id:
            return row
    raise KeyError(doc_id)


def _register_stock(db: Database, symbol: str, code: str, name: str, edinet_code: str | None = None) -> None:
    """登録銘柄を作り、必要なら `edinet_codes` にも対応する行を足す。"""
    db.upsert_stock(symbol, code, name, "東証", "JPY")
    if edinet_code:
        with db.write() as conn:
            conn.execute(
                "INSERT INTO edinet_codes (edinet_code, sec_code, name) VALUES (?, ?, ?)",
                (edinet_code, f"{code}0", name),
            )


def _register_all_fixture_stocks(db: Database) -> None:
    """合成フィクスチャに登場する4銘柄を登録する（`3333.T` だけ EDINET コード未登録）。"""
    _register_stock(db, "1111.T", "1111", "架空製作所", edinet_code="E90001")
    _register_stock(db, "2222.T", "2222", "架空商事", edinet_code="E90002")
    _register_stock(db, "3333.T", "3333", "架空繊維")  # secCode 補助突合のテスト用
    _register_stock(db, "4444.T", "4444", "架空電機", edinet_code="E90003")


# ---------- classify ----------


@pytest.mark.parametrize(
    "doc_type_code, expected",
    [
        ("120", "report"),
        ("130", "report"),
        ("140", "report"),
        ("150", "report"),
        ("160", "report"),
        ("170", "report"),
        ("220", "supply"),
        ("230", "supply"),
        ("350", "supply"),
        ("360", "supply"),
        ("240", "supply"),  # 公開買付関連の下限
        ("320", "supply"),  # 公開買付関連の上限
        ("280", "supply"),
        ("239", "other"),  # 240未満は範囲外
        ("321", "other"),  # 320超は範囲外
        ("180", "other"),
        ("190", "other"),
        ("030", "other"),
        ("040", "other"),
        ("235", "other"),
        ("236", "other"),
        ("999", "other"),
        (None, "other"),
        ("", "other"),
        ("abc", "other"),
    ],
)
def test_classify_boundaries(doc_type_code, expected):
    assert disclosures.classify(doc_type_code) == expected


# ---------- parse_document ----------


def test_parse_document_maps_all_columns_and_empty_strings_to_none():
    data = _load_fixture()
    raw = _result_by_doc_id(data, "S9000001")
    doc = disclosures.parse_document(raw)
    assert doc == {
        "doc_id": "S9000001",
        "edinet_code": "E90001",
        "sec_code": "11110",
        "filer_name": "架空製作所株式会社",
        "issuer_edinet_code": None,
        "subject_edinet_code": None,
        "doc_type_code": "120",
        "form_code": "030000",
        "ordinance_code": "010",
        "description": "有価証券報告書－第10期(2025年4月1日－2026年3月31日)",
        "reason": None,
        "period_start": "2025-04-01",
        "period_end": "2026-03-31",
        "submit_at": "2026-06-25 09:00",
        "parent_doc_id": None,
        "withdrawal": 0,
        "disclosure": 0,
        "category": "report",
    }


def test_parse_document_keeps_sec_code_as_string_even_with_letters():
    raw = {
        "docID": "S1",
        "docTypeCode": "120",
        "submitDateTime": "2026-01-01 00:00",
        "secCode": "409A0",
    }
    doc = disclosures.parse_document(raw)
    assert doc["sec_code"] == "409A0"
    assert isinstance(doc["sec_code"], str)


def test_parse_document_withdrawal_and_disclosure_become_int_from_str_or_number():
    base = {"docID": "S1", "docTypeCode": "120", "submitDateTime": "2026-01-01 00:00"}
    from_str = disclosures.parse_document({**base, "withdrawalStatus": "1", "disclosureStatus": "2"})
    from_number = disclosures.parse_document({**base, "withdrawalStatus": 1, "disclosureStatus": 2})
    assert (from_str["withdrawal"], from_str["disclosure"]) == (1, 2)
    assert (from_number["withdrawal"], from_number["disclosure"]) == (1, 2)


@pytest.mark.parametrize("missing_key", ["docID", "docTypeCode", "submitDateTime"])
def test_parse_document_returns_none_when_a_required_field_is_missing(missing_key):
    raw = {"docID": "S1", "docTypeCode": "120", "submitDateTime": "2026-01-01 00:00"}
    raw.pop(missing_key)
    assert disclosures.parse_document(raw) is None


def test_parse_document_drops_broken_fixture_items():
    data = _load_fixture()
    missing_doc_id = next(row for row in data["results"] if "docID" not in row)
    missing_submit_at = _result_by_doc_id(data, "S9000011")
    assert disclosures.parse_document(missing_doc_id) is None
    assert disclosures.parse_document(missing_submit_at) is None


# ---------- link_targets ----------


def test_link_targets_separates_resolved_and_unresolved_symbols(tmp_path):
    db = _db(tmp_path)
    _register_stock(db, "1111.T", "1111", "架空製作所", edinet_code="E90001")
    _register_stock(db, "3333.T", "3333", "架空繊維")  # EDINET コードは未登録

    targets = disclosures.link_targets(db)

    assert targets["by_edinet_code"] == {"E90001": ["1111.T"]}
    assert targets["by_sec_code"] == {"33330": ["3333.T"]}


# ---------- match_roles ----------


def test_match_roles_major_holding_is_not_matched_by_filer_edinet_code():
    """CLAUDE.md 不変条件: 大量保有(350/360)を edinetCode（提出者）で突合してはいけない。"""
    targets = {"by_edinet_code": {"E90001": ["1111.T"]}, "by_sec_code": {}}
    doc = {
        "doc_type_code": "350",
        "edinet_code": "E90001",
        "issuer_edinet_code": "E90099",  # 別会社
        "sec_code": "11110",
    }
    assert disclosures.match_roles(doc, targets) == []


def test_match_roles_major_holding_matches_issuer_edinet_code():
    targets = {"by_edinet_code": {"E90002": ["2222.T"]}, "by_sec_code": {}}
    doc = {
        "doc_type_code": "360",
        "edinet_code": "E90099",
        "issuer_edinet_code": "E90002",
        "sec_code": None,
    }
    assert disclosures.match_roles(doc, targets) == [("2222.T", "issuer")]


def test_match_roles_tender_offer_matches_both_subject_and_filer():
    targets = {"by_edinet_code": {"E90001": ["1111.T"], "E90002": ["2222.T"]}, "by_sec_code": {}}
    doc = {
        "doc_type_code": "240",
        "edinet_code": "E90002",
        "subject_edinet_code": "E90001",
        "sec_code": None,
    }
    assert set(disclosures.match_roles(doc, targets)) == {("1111.T", "subject"), ("2222.T", "filer")}


def test_match_roles_sec_code_fallback_only_helps_unresolved_symbol_and_never_350():
    targets = {"by_edinet_code": {}, "by_sec_code": {"33330": ["3333.T"]}}

    other_doc = {"doc_type_code": "180", "edinet_code": "E90999", "sec_code": "33330"}
    assert disclosures.match_roles(other_doc, targets) == [("3333.T", "filer")]

    major_holding_doc = {
        "doc_type_code": "350",
        "edinet_code": "E90999",
        "issuer_edinet_code": "E90999",
        "sec_code": "33330",
    }
    assert disclosures.match_roles(major_holding_doc, targets) == []


# ---------- save_documents / scan_cache（統合） ----------


def test_scan_cache_matches_classifies_and_saves_per_fixture(tmp_path):
    db = _db(tmp_path)
    _register_all_fixture_stocks(db)
    edinet.write_cache("2026-06-25", _load_fixture(), base_dir=tmp_path)

    result = disclosures.scan_cache(db, base_dir=tmp_path)

    assert result == {"dates": 1, "documents": 6, "links": 7, "withdrawn": 1}

    with db.connect() as conn:
        doc_ids = {row["doc_id"] for row in conn.execute("SELECT doc_id FROM disclosures")}
        links = {
            (row["doc_id"], row["symbol"], row["role"])
            for row in conn.execute("SELECT doc_id, symbol, role FROM disclosure_links")
        }
        categories = dict(conn.execute("SELECT doc_id, category FROM disclosures").fetchall())
        withdrawal = dict(conn.execute("SELECT doc_id, withdrawal FROM disclosures").fetchall())

    # 登録される書類（保存されないものは含まれない）
    assert doc_ids == {"S9000001", "S9000002", "S9000004", "S9000005", "S9000006", "S9000008"}
    # 大量保有を提出者コードで拾ってしまう(S9000003)・無関係(S9000007)・
    # secCode一致でも350(S9000009)・壊れた要素は登録されない
    assert "S9000003" not in doc_ids
    assert "S9000007" not in doc_ids
    assert "S9000009" not in doc_ids

    assert links == {
        ("S9000001", "1111.T", "filer"),
        ("S9000002", "2222.T", "issuer"),
        ("S9000004", "1111.T", "subject"),
        ("S9000004", "2222.T", "filer"),
        ("S9000005", "4444.T", "filer"),
        ("S9000006", "1111.T", "filer"),
        ("S9000008", "3333.T", "filer"),  # secCode 補助突合
    }

    assert categories["S9000001"] == "report"
    assert categories["S9000002"] == "supply"
    assert categories["S9000004"] == "supply"
    assert categories["S9000005"] == "other"
    assert categories["S9000008"] == "other"
    assert withdrawal["S9000006"] == 1


def test_scan_cache_is_idempotent_and_does_not_duplicate_links(tmp_path):
    db = _db(tmp_path)
    _register_all_fixture_stocks(db)
    edinet.write_cache("2026-06-25", _load_fixture(), base_dir=tmp_path)

    first = disclosures.scan_cache(db, base_dir=tmp_path)
    with db.connect() as conn:
        count_after_first = conn.execute("SELECT COUNT(*) AS c FROM disclosure_links").fetchone()["c"]

    second = disclosures.scan_cache(db, base_dir=tmp_path)
    with db.connect() as conn:
        count_after_second = conn.execute("SELECT COUNT(*) AS c FROM disclosure_links").fetchone()["c"]

    assert count_after_first == count_after_second == first["links"]
    assert second["links"] == 0  # ON CONFLICT DO NOTHING で新規リンクは増えない


def test_scan_cache_backfills_newly_registered_stock_from_past_cache(tmp_path):
    """SPEC §2.4.2: 銘柄を新規登録したとき、API を呼ばず既存キャッシュの再走査で過去開示を埋められる。"""
    db = _db(tmp_path)
    _register_stock(db, "1111.T", "1111", "架空製作所", edinet_code="E90001")
    edinet.write_cache("2026-06-25", _load_fixture(), base_dir=tmp_path)

    disclosures.scan_cache(db, base_dir=tmp_path)  # この時点では 4444.T は未登録
    with db.connect() as conn:
        assert (
            conn.execute("SELECT COUNT(*) AS c FROM disclosure_links WHERE symbol = '4444.T'").fetchone()["c"] == 0
        )

    _register_stock(db, "4444.T", "4444", "架空電機", edinet_code="E90003")  # 後から登録
    result = disclosures.scan_cache(db, symbols=["4444.T"], base_dir=tmp_path)

    # 取下げは1回目の走査で反映済みなので、2回目は数えない（同じ値の行は更新しない）
    assert result == {"dates": 1, "documents": 1, "links": 1, "withdrawn": 0}
    with db.connect() as conn:
        still = conn.execute("SELECT withdrawal FROM disclosures WHERE doc_id = 'S9000006'").fetchone()
    assert still["withdrawal"] == 1  # 取下げ自体は消えていない
    with db.connect() as conn:
        row = conn.execute("SELECT doc_id, role FROM disclosure_links WHERE symbol = '4444.T'").fetchone()
    assert (row["doc_id"], row["role"]) == ("S9000005", "filer")


def test_scan_cache_skips_corrupted_cache_date_and_continues(tmp_path):
    db = _db(tmp_path)
    _register_stock(db, "1111.T", "1111", "架空製作所", edinet_code="E90001")
    edinet.write_cache("2026-06-25", _load_fixture(), base_dir=tmp_path)

    broken_path = edinet.cache_path("2026-06-26", base_dir=tmp_path)
    broken_path.parent.mkdir(parents=True, exist_ok=True)
    broken_path.write_bytes(b"not a gzip file")

    result = disclosures.scan_cache(db, base_dir=tmp_path)

    assert result["dates"] == 1  # 壊れた日は数に入れず、例外にもしない
    assert result["documents"] > 0


def test_scan_cache_noop_when_no_stocks_or_no_dates(tmp_path):
    db = _db(tmp_path)
    edinet.write_cache("2026-06-25", _load_fixture(), base_dir=tmp_path)

    # 登録銘柄が0件
    assert disclosures.scan_cache(db, base_dir=tmp_path) == {
        "dates": 0,
        "documents": 0,
        "links": 0,
        "withdrawn": 0,
    }

    _register_stock(db, "1111.T", "1111", "架空製作所", edinet_code="E90001")
    # 対象日が0件
    assert disclosures.scan_cache(db, dates=[], base_dir=tmp_path) == {
        "dates": 0,
        "documents": 0,
        "links": 0,
        "withdrawn": 0,
    }


# ---------- 取下げスタブ（SPEC §2.4.6） ----------


def _full_doc(doc_id: str, *, edinet_code: str = "E90001", sec_code: str = "11110") -> dict:
    """突合・保存できる完全な書類（有報）を1件作る。"""
    return {
        "docID": doc_id,
        "edinetCode": edinet_code,
        "secCode": sec_code,
        "filerName": "架空製作所株式会社",
        "docTypeCode": "120",
        "formCode": "030000",
        "ordinanceCode": "010",
        "submitDateTime": "2026-08-01 09:00",
        "withdrawalStatus": "0",
        "disclosureStatus": "0",
    }


def _withdrawal_stub(
    doc_id: str,
    *,
    withdrawal_status: str,
    parent_doc_id: str | None = None,
    submit_at: str | None = None,
) -> dict:
    """実物の取下げスタブを模す（docTypeCode 等は一切持たない。SPEC §2.4.6）。"""
    stub: dict = {
        "docID": doc_id,
        "withdrawalStatus": withdrawal_status,
        "docInfoEditStatus": "0",
        "disclosureStatus": "0",
    }
    if parent_doc_id is not None:
        stub["parentDocID"] = parent_doc_id
    if submit_at is not None:
        stub["submitDateTime"] = submit_at
    return stub


def test_scan_cache_withdrawal_stub_updates_parent_by_parent_doc_id(tmp_path):
    db = _db(tmp_path)
    _register_stock(db, "1111.T", "1111", "架空製作所", edinet_code="E90001")
    edinet.write_cache(
        "2026-08-01",
        {
            "metadata": {},
            "results": [
                _full_doc("S9100001"),
                _withdrawal_stub("S9100099", withdrawal_status="1", parent_doc_id="S9100001"),
            ],
        },
        base_dir=tmp_path,
    )

    result = disclosures.scan_cache(db, base_dir=tmp_path)

    assert result["withdrawn"] == 1
    with db.connect() as conn:
        row = conn.execute("SELECT withdrawal FROM disclosures WHERE doc_id = ?", ("S9100001",)).fetchone()
    assert row["withdrawal"] == 1


def test_scan_cache_withdrawal_stub_on_later_date_updates_earlier_saved_document(tmp_path):
    """実物と同じ形: 元の書類と取下げスタブが別々の日付のキャッシュに分かれて現れる。"""
    db = _db(tmp_path)
    _register_stock(db, "1111.T", "1111", "架空製作所", edinet_code="E90001")
    edinet.write_cache("2026-08-01", {"metadata": {}, "results": [_full_doc("S9100002")]}, base_dir=tmp_path)
    edinet.write_cache(
        "2026-08-05",
        {
            "metadata": {},
            "results": [_withdrawal_stub("S9100098", withdrawal_status="1", parent_doc_id="S9100002")],
        },
        base_dir=tmp_path,
    )

    result = disclosures.scan_cache(db, dates=["2026-08-01", "2026-08-05"], base_dir=tmp_path)

    assert result["withdrawn"] == 1
    with db.connect() as conn:
        row = conn.execute("SELECT withdrawal FROM disclosures WHERE doc_id = ?", ("S9100002",)).fetchone()
    assert row["withdrawal"] == 1


def test_scan_cache_sorts_dates_ascending_even_when_given_descending(tmp_path):
    """`disclosures_job` は新しい日付から順に `dates` を渡すので、降順で渡されても正しく処理できる必要がある。"""
    db = _db(tmp_path)
    _register_stock(db, "1111.T", "1111", "架空製作所", edinet_code="E90001")
    edinet.write_cache("2026-08-01", {"metadata": {}, "results": [_full_doc("S9100003")]}, base_dir=tmp_path)
    edinet.write_cache(
        "2026-08-05",
        {
            "metadata": {},
            "results": [_withdrawal_stub("S9100097", withdrawal_status="1", parent_doc_id="S9100003")],
        },
        base_dir=tmp_path,
    )

    # 降順で渡す
    result = disclosures.scan_cache(db, dates=["2026-08-05", "2026-08-01"], base_dir=tmp_path)

    assert result["withdrawn"] == 1
    with db.connect() as conn:
        row = conn.execute("SELECT withdrawal FROM disclosures WHERE doc_id = ?", ("S9100003",)).fetchone()
    assert row["withdrawal"] == 1


def test_scan_cache_withdrawal_status_2_matches_by_own_doc_id(tmp_path):
    db = _db(tmp_path)
    _register_stock(db, "1111.T", "1111", "架空製作所", edinet_code="E90001")
    edinet.write_cache("2026-08-01", {"metadata": {}, "results": [_full_doc("S9100004")]}, base_dir=tmp_path)
    edinet.write_cache(
        "2026-08-05",
        {"metadata": {}, "results": [_withdrawal_stub("S9100004", withdrawal_status="2")]},
        base_dir=tmp_path,
    )

    result = disclosures.scan_cache(db, dates=["2026-08-01", "2026-08-05"], base_dir=tmp_path)

    assert result["withdrawn"] == 1
    with db.connect() as conn:
        row = conn.execute("SELECT withdrawal FROM disclosures WHERE doc_id = ?", ("S9100004",)).fetchone()
    assert row["withdrawal"] == 2


def test_scan_cache_unmatched_withdrawal_stub_is_a_noop(tmp_path):
    db = _db(tmp_path)
    _register_stock(db, "1111.T", "1111", "架空製作所", edinet_code="E90001")
    edinet.write_cache(
        "2026-08-01",
        {
            "metadata": {},
            "results": [_withdrawal_stub("S9199999", withdrawal_status="1", parent_doc_id="S9199998")],
        },
        base_dir=tmp_path,
    )

    result = disclosures.scan_cache(db, base_dir=tmp_path)

    assert result == {"dates": 1, "documents": 0, "links": 0, "withdrawn": 0}


def test_scan_cache_withdrawal_stub_does_not_log_missing_field_warning(tmp_path, caplog):
    db = _db(tmp_path)
    _register_stock(db, "1111.T", "1111", "架空製作所", edinet_code="E90001")
    edinet.write_cache(
        "2026-08-01",
        {
            "metadata": {},
            "results": [_withdrawal_stub("S9199997", withdrawal_status="1", parent_doc_id="S9199996")],
        },
        base_dir=tmp_path,
    )

    with caplog.at_level(logging.WARNING, logger=disclosures.log.name):
        disclosures.scan_cache(db, base_dir=tmp_path)

    assert "必須項目が欠けている" not in caplog.text


def test_scan_cache_withdrawn_count_in_return_value(tmp_path):
    db = _db(tmp_path)
    _register_stock(db, "1111.T", "1111", "架空製作所", edinet_code="E90001")
    edinet.write_cache(
        "2026-08-01",
        {
            "metadata": {},
            "results": [
                _full_doc("S9100005"),
                _full_doc("S9100006"),
                _withdrawal_stub("S9100091", withdrawal_status="1", parent_doc_id="S9100005"),
                _withdrawal_stub("S9100092", withdrawal_status="1", parent_doc_id="S9100006"),
            ],
        },
        base_dir=tmp_path,
    )

    result = disclosures.scan_cache(db, base_dir=tmp_path)

    assert result["withdrawn"] == 2


# ---------- cleanup_orphans ----------


def test_cleanup_orphans_deletes_only_documents_with_no_remaining_links(tmp_path):
    db = _db(tmp_path)
    _register_stock(db, "1111.T", "1111", "架空製作所", edinet_code="E90001")
    _register_stock(db, "2222.T", "2222", "架空商事", edinet_code="E90002")
    edinet.write_cache("2026-06-25", _load_fixture(), base_dir=tmp_path)
    disclosures.scan_cache(db, base_dir=tmp_path)

    # S9000004 は 1111.T(subject) と 2222.T(filer) の両方にリンクしている。
    # 1111.T のリンクだけ手で消して、「他の銘柄がまだ参照している書類」を作る
    with db.write() as conn:
        conn.execute("DELETE FROM disclosure_links WHERE symbol = '1111.T'")

    deleted = disclosures.cleanup_orphans(db)

    with db.connect() as conn:
        remaining = {row["doc_id"] for row in conn.execute("SELECT doc_id FROM disclosures")}

    # 1111.T にしかリンクしていなかった書類（filer のみ）は消える
    assert "S9000001" not in remaining
    assert "S9000006" not in remaining
    # 2222.T のリンクがまだ残っている書類は消えない
    assert "S9000002" in remaining
    assert "S9000004" in remaining
    assert deleted == 2


# ---------- disclosures_job とのつなぎ込み ----------


def test_disclosures_job_scans_cache_after_fetch_and_returns_registered(monkeypatch, tmp_path):
    db = _db(tmp_path)
    _register_stock(db, "1111.T", "1111", "架空製作所", edinet_code="E90001")
    today = disclosures.today_jst()
    start = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    _insert_price(db, start)  # fetch_range 用（symbol は 1111.T のまま upsert される）
    settings = _FakeSettings()
    fixture = _load_fixture()
    cache_dir = tmp_path / "cache"

    def fake_fetch_day(db_, date, api_key, cancel=None, client=None, base_dir=None):
        if date == start:
            edinet.write_cache(date, fixture, base_dir=base_dir)
            return _ok(date, documents=len(fixture["results"]))
        edinet.write_cache(date, {"metadata": {}, "results": []}, base_dir=base_dir)
        return {"date": date, "result": "empty", "documents": 0, "bytes": 1}

    monkeypatch.setattr(edinet, "fetch_day", fake_fetch_day)
    job = disclosures.disclosures_job(db, settings, base_dir=cache_dir)
    result = job(FakeCtx(), {})

    # 1111.T が登録銘柄なので、fixture 中で 1111.T に紐づく書類だけが登録される
    # （filer: S9000001, S9000006 / subject: S9000004 の3件・3リンク）
    assert result["registered"] == {"documents": 3, "links": 3}
    assert "開示 3件を登録" in result["summary"]

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM disclosures").fetchone()["c"] == 3


def test_disclosures_job_scans_fetched_dates_before_reraising_cancelled(monkeypatch, tmp_path):
    db = _db(tmp_path)
    _register_stock(db, "1111.T", "1111", "架空製作所", edinet_code="E90001")
    today = disclosures.today_jst()
    start = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")
    _insert_price(db, start)
    settings = _FakeSettings()
    fixture = _load_fixture()
    cache_dir = tmp_path / "cache"

    calls = []

    def fake_fetch_day(db_, date, api_key, cancel=None, client=None, base_dir=None):
        calls.append(date)
        edinet.write_cache(date, fixture, base_dir=base_dir)
        if len(calls) == 2:
            cancel.set()
        return _ok(date, documents=len(fixture["results"]))

    monkeypatch.setattr(edinet, "fetch_day", fake_fetch_day)

    manager = JobManager()
    manager.register("disclosures", disclosures.disclosures_job(db, settings, base_dir=cache_dir))
    started = manager.start("disclosures", {})
    final = manager.join(started["id"], WAIT)

    assert final["state"] == "cancelled"
    assert len(calls) == 2
    # 中断されても、それまでに取得できた分はキャッシュと突合されて DB に登録済み
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM disclosures").fetchone()["c"] == 3
