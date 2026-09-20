"""EDINET 開示の取得範囲・差分・ジョブ（SPEC §2.4.2・§2.4.4・§2.4.5・§2.8.1）。

ネットワークは使わない。`edinet.fetch_day` は monkeypatch でフェイクに差し替える。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

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
