"""起動時の自動更新（SPEC §2.8.2）。ネットワークは使わない。"""

import threading
from datetime import datetime, timedelta

import pytest

from app import autoupdate
from app.autoupdate import AutoUpdater
from app.database import Database
from app.errors import Cancelled
from app.fetcher import FetchError
from app.jobs import JobManager
from app.settings import Settings
from app.sources.base import HttpError

WAIT = 5

TAISYAKU_DATE = "2026-09-18"  # フェイクの日証金取得が返す申込日


def fake_taisyaku_fetch_and_save(db, settings=None, symbols=None, cancel=None, client=None):
    """taisyaku.fetch_and_save の既定フェイク。渡された銘柄が全部見つかったことにする。"""
    symbols = list(symbols or [])
    return {"saved": len(symbols), "skipped": 0, "date": TAISYAKU_DATE, "missing": []}


@pytest.fixture(autouse=True)
def no_real_taisyaku(monkeypatch):
    """既存テストが日証金の実通信をしないよう、既定のフェイクを常に差し込む。

    需給ステップ自体を検証するテストは、必要に応じて `monkeypatch` で個別に上書きする。
    """
    monkeypatch.setattr(autoupdate.taisyaku, "fetch_and_save", fake_taisyaku_fetch_and_save)


class FakeService:
    """StockService.update の代わり。取得の記録だけ本物と同じように残す。"""

    def __init__(self, db, fail=()):
        self.db = db
        self.fail = set(fail)
        self.calls = []

    def update(self, symbol):
        self.calls.append(symbol)
        if symbol in self.fail:
            raise FetchError(f"{symbol} を取得できませんでした")
        self.db.log_fetch("yahoo", symbol)
        return {"added": 2, "warnings": []}


class FakeCtx:
    def __init__(self):
        self.cancel = threading.Event()
        self.labels = []
        self.waits = []

    def progress(self, current, total, label=""):
        self.labels.append(label)

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def wait(self, seconds):
        self.waits.append(seconds)
        self.check()


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "test.db")
    db.init_schema()
    for code in ("1301", "7203", "9984"):
        db.upsert_stock(f"{code}.T", code, f"Stock {code}", "東証", "JPY")
    settings = Settings(db, keyring_backend=object())
    return db, settings


def set_fetched(db, symbol, when, result="ok", source="yahoo"):
    with db.write() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO fetch_log (source, key, fetched_at, result) VALUES (?, ?, ?, ?)",
            (source, symbol, when.strftime("%Y-%m-%d %H:%M:%S"), result),
        )


def test_updates_every_stock_in_order_with_pause(env):
    db, settings = env
    service = FakeService(db)
    ctx = FakeCtx()
    result = AutoUpdater(db, service, settings).run(ctx, {})

    assert service.calls == ["1301.T", "7203.T", "9984.T"]
    assert ctx.waits == [autoupdate.PRICE_PAUSE_SEC] * 2  # 銘柄間だけ待つ（先頭の前は待たない）
    assert result["updated_symbols"] == ["1301.T", "7203.T", "9984.T"]
    assert result["summary"] == (
        f"株価 3件更新／日証金 {TAISYAKU_DATE} 分を取得（3銘柄）／EDINET スキップ（API キー未設定）／空売り スキップ（設定オフ）"
    )
    assert result["failed"] is False
    assert result["changed"] is True
    assert result["steps"]["prices"]["added"] == 6
    assert "株価を更新中 1/3" in ctx.labels


def test_disabled_by_setting(env):
    db, settings = env
    settings.update({"auto_update_on_start": False})
    service = FakeService(db)
    assert AutoUpdater(db, service, settings).run(FakeCtx(), {}) == {"skipped": "disabled"}
    assert service.calls == []


def test_force_ignores_the_setting(env):
    db, settings = env
    settings.update({"auto_update_on_start": False})
    service = FakeService(db)
    AutoUpdater(db, service, settings).run(FakeCtx(), {"force": True})
    assert len(service.calls) == 3


def test_no_stocks(tmp_path):
    db = Database(tmp_path / "empty.db")
    db.init_schema()
    updater = AutoUpdater(db, FakeService(db), Settings(db, keyring_backend=object()))
    assert updater.run(FakeCtx(), {}) == {"skipped": "no_stocks"}


def test_runs_only_once_per_process(env):
    """画面を再読込しても自動更新は走らない（起動時の1回だけ）。"""
    db, settings = env
    settings.update({"auto_update_min_interval_min": 0})
    service = FakeService(db)
    updater = AutoUpdater(db, service, settings)
    updater.run(FakeCtx(), {})
    assert updater.run(FakeCtx(), {}) == {"skipped": "already_ran"}
    assert len(service.calls) == 3


def test_recently_fetched_stocks_are_skipped(env):
    db, settings = env
    now = datetime(2026, 9, 21, 9, 0, 0)
    set_fetched(db, "1301.T", now - timedelta(minutes=10))  # 60分以内 → スキップ
    set_fetched(db, "7203.T", now - timedelta(minutes=61))  # 期限切れ → 取得
    set_fetched(db, "9984.T", now - timedelta(minutes=5), "error:timeout")  # 失敗の記録 → 取得
    service = FakeService(db)
    result = AutoUpdater(db, service, settings, now=lambda: now).run(FakeCtx(), {})

    assert service.calls == ["7203.T", "9984.T"]
    assert result["steps"]["prices"]["skipped"] == 1


def test_everything_fresh_means_no_requests(env):
    db, settings = env
    now = datetime(2026, 9, 21, 9, 0, 0)
    for symbol in ("1301.T", "7203.T", "9984.T"):
        set_fetched(db, symbol, now - timedelta(minutes=1))
    set_fetched(db, "zandaka", now - timedelta(minutes=1), source="taisyaku")
    service = FakeService(db)
    ctx = FakeCtx()
    result = AutoUpdater(db, service, settings, now=lambda: now).run(ctx, {})
    assert service.calls == []
    assert ctx.waits == []
    assert result["summary"] == "株価 取得済み／日証金 取得済み／EDINET スキップ（API キー未設定）／空売り スキップ（設定オフ）"
    assert result["updated_symbols"] == []
    assert result["changed"] is False  # 画面は何も知らせない


def test_zero_interval_disables_skipping(env):
    db, settings = env
    settings.update({"auto_update_min_interval_min": 0})
    now = datetime(2026, 9, 21, 9, 0, 0)
    set_fetched(db, "1301.T", now)
    service = FakeService(db)
    AutoUpdater(db, service, settings, now=lambda: now).run(FakeCtx(), {})
    assert len(service.calls) == 3


def test_one_failing_stock_does_not_stop_the_rest(env):
    """上場廃止など銘柄固有の失敗で、他の銘柄の更新を止めない。"""
    db, settings = env
    service = FakeService(db, fail={"1301.T"})
    result = AutoUpdater(db, service, settings).run(FakeCtx(), {})
    assert service.calls == ["1301.T", "7203.T", "9984.T"]
    assert result["steps"]["prices"]["updated_symbols"] == ["7203.T", "9984.T"]
    # 日証金は株価とは独立に3銘柄とも取得できたことにする（フェイク）ので、全体では3銘柄とも updated_symbols に入る
    assert result["updated_symbols"] == ["1301.T", "7203.T", "9984.T"]
    assert result["failed"] is True
    assert result["summary"] == (
        f"株価 2件更新・1件失敗／日証金 {TAISYAKU_DATE} 分を取得（3銘柄）／EDINET スキップ（API キー未設定）／空売り スキップ（設定オフ）"
    )
    assert "1301.T" in result["steps"]["prices"]["errors"][0]


def test_consecutive_failures_abort_the_source(env):
    """オフライン時に全銘柄分の失敗を待たない。"""
    db, settings = env
    service = FakeService(db, fail={"1301.T", "7203.T", "9984.T"})
    result = AutoUpdater(db, service, settings).run(FakeCtx(), {})
    assert service.calls == ["1301.T", "7203.T"]
    assert result["failed"] is True
    assert "取得できず" in result["summary"]


def test_failure_does_not_raise(env):
    db, settings = env

    class Exploding(FakeService):
        def update(self, symbol):
            raise RuntimeError("unexpected")

    result = AutoUpdater(db, Exploding(db), settings).run(FakeCtx(), {})
    assert result["failed"] is True


def test_step_crash_is_contained_and_later_steps_still_run(env):
    db, settings = env
    updater = AutoUpdater(db, FakeService(db), settings)

    def broken(ctx, stocks):
        raise RuntimeError("boom")

    updater.steps.insert(0, ("broken", broken))
    result = updater.run(FakeCtx(), {})
    assert result["steps"]["broken"]["failed"] is True
    assert result["steps"]["prices"]["updated_symbols"] == ["1301.T", "7203.T", "9984.T"]


def test_cancel_stops_between_stocks(env):
    db, settings = env
    service = FakeService(db)
    ctx = FakeCtx()
    original = service.update

    def update_then_cancel(symbol):
        out = original(symbol)
        ctx.cancel.set()
        return out

    service.update = update_then_cancel
    with pytest.raises(Cancelled):
        AutoUpdater(db, service, settings).run(ctx, {})
    assert service.calls == ["1301.T"]
    assert db.get_fetch("yahoo", "1301.T")["result"] == "ok"  # 中断までの分は残る


def test_runs_as_a_job(env, monkeypatch):
    monkeypatch.setattr(autoupdate, "PRICE_PAUSE_SEC", 0)
    db, settings = env
    manager = JobManager()
    manager.register("auto_update", AutoUpdater(db, FakeService(db), settings).run)
    final = manager.join(manager.start("auto_update")["id"], WAIT)
    assert final["state"] == "done"
    assert final["result"]["summary"] == (
        f"株価 3件更新／日証金 {TAISYAKU_DATE} 分を取得（3銘柄）／EDINET スキップ（API キー未設定）／空売り スキップ（設定オフ）"
    )


def test_order_is_prices_then_taisyaku_then_short(env, monkeypatch):
    """実行順が SPEC §2.8.2 のとおり 株価 → 貸借取引残高 → 空売り であること。"""
    db, settings = env
    settings.update({"auto_update_short": True, "scrape_contact": "test@example.com"})
    order = []

    service = FakeService(db)
    original_update = service.update

    def tracking_update(symbol):
        order.append("prices")
        return original_update(symbol)

    service.update = tracking_update

    def fake_taisyaku(db_, settings_=None, symbols=None, cancel=None, client=None):
        order.append("taisyaku")
        return fake_taisyaku_fetch_and_save(db_, settings_, symbols=symbols, cancel=cancel, client=client)

    monkeypatch.setattr(autoupdate.taisyaku, "fetch_and_save", fake_taisyaku)
    monkeypatch.setattr(autoupdate.karauri, "select_targets", lambda *a, **k: (["1301.T"], 0))
    monkeypatch.setattr(autoupdate.karauri, "make_client", lambda settings_: object())

    def fake_fetch_one(db_, settings_, symbol, cancel=None, client=None):
        order.append("short")
        return {"symbol": symbol, "status": "ok"}

    monkeypatch.setattr(autoupdate.karauri, "fetch_one", fake_fetch_one)

    AutoUpdater(db, service, settings).run(FakeCtx(), {})
    assert order.index("prices") < order.index("taisyaku") < order.index("short")


# ---------- 貸借取引残高（日証金）----------
def test_taisyaku_summary_when_no_registered_stock_has_a_row(env, monkeypatch):
    """登録銘柄がどれも貸借銘柄でないとき。save() は申込日を返さないので日付を出さない。"""
    db, settings = env

    def all_missing(db_, settings_=None, symbols=None, cancel=None, client=None):
        return {"saved": 0, "skipped": 0, "date": None, "missing": list(symbols or [])}

    monkeypatch.setattr(autoupdate.taisyaku, "fetch_and_save", all_missing)
    result = AutoUpdater(db, FakeService(db), settings).run(FakeCtx(), {})
    step = result["steps"]["taisyaku"]
    assert step["summary"] == "日証金 取得（登録銘柄の行なし）"
    assert "None" not in step["summary"]
    assert step["failed"] is False
    assert step["updated_symbols"] == []



def test_taisyaku_skipped_when_no_domestic_stocks(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(autoupdate.taisyaku, "fetch_and_save", lambda *a, **k: calls.append(1))
    db = Database(tmp_path / "foreign.db")
    db.init_schema()
    db.upsert_stock("AAPL", "AAPL", "Apple", "NASDAQ", "USD")
    settings = Settings(db, keyring_backend=object())
    result = AutoUpdater(db, FakeService(db), settings).run(FakeCtx(), {})
    assert result["steps"]["taisyaku"] == {
        "summary": "日証金 対象銘柄なし",
        "failed": False,
        "changed": False,
        "updated_symbols": [],
    }
    assert calls == []


def test_taisyaku_skipped_within_min_interval(env, monkeypatch):
    db, settings = env
    now = datetime(2026, 9, 21, 9, 0, 0)
    set_fetched(db, "zandaka", now - timedelta(minutes=1), source="taisyaku")
    calls = []
    monkeypatch.setattr(autoupdate.taisyaku, "fetch_and_save", lambda *a, **k: calls.append(1))

    result = AutoUpdater(db, FakeService(db), settings, now=lambda: now).run(FakeCtx(), {})
    assert result["steps"]["taisyaku"] == {
        "summary": "日証金 取得済み",
        "failed": False,
        "changed": False,
        "updated_symbols": [],
    }
    assert calls == []  # スキップ判定はフェイクを呼ぶ前に効く


def test_taisyaku_success_reports_the_application_date_and_count(env, monkeypatch):
    db, settings = env

    def fake(db_, settings_=None, symbols=None, cancel=None, client=None):
        assert sorted(symbols) == ["1301.T", "7203.T", "9984.T"]
        return {"saved": 2, "skipped": 0, "date": "2026-09-17", "missing": ["9984.T"]}

    monkeypatch.setattr(autoupdate.taisyaku, "fetch_and_save", fake)
    result = AutoUpdater(db, FakeService(db), settings).run(FakeCtx(), {})
    step = result["steps"]["taisyaku"]
    assert step["summary"] == "日証金 2026-09-17 分を取得（2銘柄）"
    assert step["updated_symbols"] == ["1301.T", "7203.T"]
    assert step["failed"] is False
    assert step["changed"] is True


def test_taisyaku_failure_does_not_stop_later_steps(env, monkeypatch):
    db, settings = env
    settings.update({"auto_update_short": True, "scrape_contact": "test@example.com"})

    def boom(*a, **k):
        raise RuntimeError("taisyaku.jp に接続できません")

    monkeypatch.setattr(autoupdate.taisyaku, "fetch_and_save", boom)
    monkeypatch.setattr(autoupdate.karauri, "select_targets", lambda *a, **k: ([], 0))

    result = AutoUpdater(db, FakeService(db), settings).run(FakeCtx(), {})
    assert result["steps"]["taisyaku"]["failed"] is True
    assert result["steps"]["taisyaku"]["summary"] == "日証金 失敗"
    assert result["failed"] is True
    assert result["steps"]["short"]["summary"] == "空売り 取得済み"  # 後続ステップは打ち切られない


# ---------- 空売り残高（karauri.net）----------
def test_short_skipped_when_setting_off_by_default(env):
    db, settings = env
    result = AutoUpdater(db, FakeService(db), settings).run(FakeCtx(), {})
    assert result["steps"]["short"] == {
        "summary": "空売り スキップ（設定オフ）",
        "failed": False,
        "changed": False,
        "updated_symbols": [],
    }


def test_short_skipped_when_scrape_contact_is_missing(env):
    """連絡先が未設定でもエラーにはしない（スキップ扱い）。"""
    db, settings = env
    settings.update({"auto_update_short": True})
    result = AutoUpdater(db, FakeService(db), settings).run(FakeCtx(), {})
    assert result["steps"]["short"] == {
        "summary": "空売り スキップ（連絡先が未設定）",
        "failed": False,
        "changed": False,
        "updated_symbols": [],
    }
    assert result["failed"] is False


def _enable_short(env, monkeypatch, targets):
    db, settings = env
    settings.update({"auto_update_short": True, "scrape_contact": "test@example.com"})
    monkeypatch.setattr(autoupdate.karauri, "select_targets", lambda *a, **k: (targets, 0))
    monkeypatch.setattr(autoupdate.karauri, "make_client", lambda settings_: object())
    return db, settings


def test_short_fetches_only_the_symbols_select_targets_returns(env, monkeypatch):
    db, settings = _enable_short(env, monkeypatch, ["7203.T", "9984.T"])
    calls = []

    def fake_fetch_one(db_, settings_, symbol, cancel=None, client=None):
        calls.append(symbol)
        return {"symbol": symbol, "status": "ok"}

    monkeypatch.setattr(autoupdate.karauri, "fetch_one", fake_fetch_one)
    result = AutoUpdater(db, FakeService(db), settings).run(FakeCtx(), {})
    assert calls == ["7203.T", "9984.T"]  # select_targets が選ばなかった 1301.T は取りに行かない
    step = result["steps"]["short"]
    assert step["summary"] == "空売り 2件更新"
    assert step["updated_symbols"] == ["7203.T", "9984.T"]
    assert step["failed"] is False


@pytest.mark.parametrize("status", [403, 429])
def test_short_aborts_immediately_on_forbidden(env, monkeypatch, status):
    db, settings = _enable_short(env, monkeypatch, ["1301.T", "7203.T", "9984.T"])
    calls = []

    def fake_fetch_one(db_, settings_, symbol, cancel=None, client=None):
        calls.append(symbol)
        raise HttpError("拒否されました", status=status)

    monkeypatch.setattr(autoupdate.karauri, "fetch_one", fake_fetch_one)
    result = AutoUpdater(db, FakeService(db), settings).run(FakeCtx(), {})
    assert calls == ["1301.T"]  # 最初の1件で即中止。以降の銘柄は取りに行かない。リトライもしない
    step = result["steps"]["short"]
    assert step["failed"] is True
    assert "拒否" in step["summary"]


def test_short_continues_after_a_single_non_forbidden_failure(env, monkeypatch):
    db, settings = _enable_short(env, monkeypatch, ["1301.T", "7203.T", "9984.T"])
    calls = []

    def fake_fetch_one(db_, settings_, symbol, cancel=None, client=None):
        calls.append(symbol)
        if symbol == "1301.T":
            raise RuntimeError("timeout")
        return {"symbol": symbol, "status": "ok"}

    monkeypatch.setattr(autoupdate.karauri, "fetch_one", fake_fetch_one)
    result = AutoUpdater(db, FakeService(db), settings).run(FakeCtx(), {})
    assert calls == ["1301.T", "7203.T", "9984.T"]  # 1回だけの失敗では次の銘柄へ進む
    step = result["steps"]["short"]
    assert step["updated_symbols"] == ["7203.T", "9984.T"]
    assert step["failed"] is True


def test_short_aborts_after_two_consecutive_failures(env, monkeypatch):
    db, settings = _enable_short(env, monkeypatch, ["1301.T", "7203.T", "9984.T"])
    calls = []

    def fake_fetch_one(db_, settings_, symbol, cancel=None, client=None):
        calls.append(symbol)
        raise RuntimeError("timeout")

    monkeypatch.setattr(autoupdate.karauri, "fetch_one", fake_fetch_one)
    result = AutoUpdater(db, FakeService(db), settings).run(FakeCtx(), {})
    assert calls == ["1301.T", "7203.T"]  # 連続2回の失敗で打ち切り。3件目には行かない（株価と同じ基準）
    step = result["steps"]["short"]
    assert step["failed"] is True
    assert "取得できず" in step["summary"]


def test_short_cancel_stops_between_symbols(env, monkeypatch):
    db, settings = _enable_short(env, monkeypatch, ["1301.T", "7203.T", "9984.T"])
    ctx = FakeCtx()
    calls = []

    def fake_fetch_one(db_, settings_, symbol, cancel=None, client=None):
        calls.append(symbol)
        ctx.cancel.set()
        return {"symbol": symbol, "status": "ok"}

    monkeypatch.setattr(autoupdate.karauri, "fetch_one", fake_fetch_one)
    with pytest.raises(Cancelled):
        AutoUpdater(db, FakeService(db), settings).run(ctx, {})
    assert calls == ["1301.T"]


# ---------- 取得の記録（StockService 側）----------
def test_service_update_and_register_record_the_fetch(tmp_path):
    import numpy as np
    from conftest import make_prices
    from test_service import FakeFetcher

    from app.service import StockService

    db = Database(tmp_path / "svc.db")
    db.init_schema()
    service = StockService(db, FakeFetcher(make_prices(100 + np.arange(60.0))), tmp_path / "csv", tmp_path / "out")

    service.register("7203.T", "Toyota", "東証")
    assert db.get_fetch("yahoo", "7203.T")["result"] == "ok"

    with db.write() as conn:
        conn.execute("DELETE FROM fetch_log")
    service.update("7203.T")
    assert db.get_fetch("yahoo", "7203.T")["result"] == "ok"


# ---------- 開示（EDINET。P4-5）----------
EDINET_API_KEY_ENV = "CHRONOS_EDINET_API_KEY"


def _job_result(**overrides):
    """disclosures.disclosures_job が返す辞書のフェイク（既定は「対象0日・登録0件」相当）。"""
    result = {
        "days": 0,
        "with_documents": [],
        "empty": [],
        "errors": [],
        "aborted": None,
        "registered": {"documents": 0, "links": 0},
        "summary": "",
    }
    result.update(overrides)
    return result


def _fake_disclosures_job(result, calls=None):
    """disclosures.disclosures_job の代わり。呼ばれた params を calls に積み、result を返す。"""

    def factory(db, settings):
        def job(ctx, params):
            if calls is not None:
                calls.append(params)
            if isinstance(result, Exception):
                raise result
            return result

        return job

    return factory


def _insert_disclosure_link(db, doc_id, symbol, role="filer", submit_at="2026-09-01T00:00:00"):
    """テストで disclosure_links の件数を変化させるための直接 INSERT（ジョブの中身はフェイクなので実際には呼ばれない）。"""
    with db.write() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO disclosures (doc_id, doc_type_code, submit_at, category) "
            "VALUES (?, ?, ?, ?)",
            (doc_id, "120", submit_at, "report"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO disclosure_links (doc_id, symbol, role) VALUES (?, ?, ?)",
            (doc_id, symbol, role),
        )


def test_edinet_skipped_silently_when_api_key_missing(env, monkeypatch):
    """API キー未設定はエラーにせず黙ってスキップする（SPEC §2.8.2 順3のスキップ条件）。"""
    db, settings = env
    pending_calls = []
    monkeypatch.setattr(
        autoupdate.disclosures, "pending_dates", lambda *a, **k: pending_calls.append(1) or ["2026-09-19"]
    )
    result = AutoUpdater(db, FakeService(db), settings).run(FakeCtx(), {})
    step = result["steps"]["edinet"]
    assert step == {
        "summary": "EDINET スキップ（API キー未設定）",
        "failed": False,
        "changed": False,
        "updated_symbols": [],
    }
    assert result["failed"] is False
    assert pending_calls == []  # API キーの有無を先に見るので、対象日は数えにいかない


def test_edinet_up_to_date_when_no_pending_dates(env, monkeypatch):
    db, settings = env
    monkeypatch.setenv(EDINET_API_KEY_ENV, "dummy-key")
    monkeypatch.setattr(autoupdate.disclosures, "pending_dates", lambda *a, **k: [])
    job_calls = []
    monkeypatch.setattr(autoupdate.disclosures, "disclosures_job", _fake_disclosures_job(_job_result(), job_calls))

    result = AutoUpdater(db, FakeService(db), settings).run(FakeCtx(), {})
    step = result["steps"]["edinet"]
    assert step == {"summary": "EDINET 取得済み", "failed": False, "changed": False, "updated_symbols": []}
    assert job_calls == []  # 対象日が0件ならジョブは作られるが呼ばれない


def test_edinet_calls_job_with_max_days_30(env, monkeypatch):
    db, settings = env
    monkeypatch.setenv(EDINET_API_KEY_ENV, "dummy-key")
    monkeypatch.setattr(
        autoupdate.disclosures, "pending_dates", lambda *a, **k: ["2026-09-19", "2026-09-18"]
    )
    job_calls = []
    monkeypatch.setattr(
        autoupdate.disclosures, "disclosures_job", _fake_disclosures_job(_job_result(days=2), job_calls)
    )

    AutoUpdater(db, FakeService(db), settings).run(FakeCtx(), {})
    assert autoupdate.AUTO_EDINET_MAX_DAYS == 30
    assert job_calls == [{"max_days": 30}]


def test_edinet_capped_message_when_more_than_max_days_pending(env, monkeypatch):
    db, settings = env
    monkeypatch.setenv(EDINET_API_KEY_ENV, "dummy-key")
    pending = [f"2026-08-{d:02d}" for d in range(1, 32)]  # 31日分（AUTO_EDINET_MAX_DAYS=30 を超える）
    monkeypatch.setattr(autoupdate.disclosures, "pending_dates", lambda *a, **k: pending)
    monkeypatch.setattr(
        autoupdate.disclosures, "disclosures_job", _fake_disclosures_job(_job_result(days=30))
    )

    result = AutoUpdater(db, FakeService(db), settings).run(FakeCtx(), {})
    step = result["steps"]["edinet"]
    assert "直近30日分" in step["summary"]
    assert "開示を取得" in step["summary"]
    assert step["failed"] is False


def test_edinet_no_capped_message_when_max_days_or_fewer_pending(env, monkeypatch):
    db, settings = env
    monkeypatch.setenv(EDINET_API_KEY_ENV, "dummy-key")
    pending = [f"2026-08-{d:02d}" for d in range(1, 31)]  # ちょうど30日分
    monkeypatch.setattr(autoupdate.disclosures, "pending_dates", lambda *a, **k: pending)
    monkeypatch.setattr(
        autoupdate.disclosures, "disclosures_job", _fake_disclosures_job(_job_result(days=30))
    )

    result = AutoUpdater(db, FakeService(db), settings).run(FakeCtx(), {})
    step = result["steps"]["edinet"]
    assert "直近" not in step["summary"]
    assert "開示を取得" not in step["summary"]
    assert step["summary"] == "EDINET 30日分"


def test_edinet_updated_symbols_only_for_symbols_with_new_links(env, monkeypatch):
    db, settings = env
    monkeypatch.setenv(EDINET_API_KEY_ENV, "dummy-key")
    monkeypatch.setattr(autoupdate.disclosures, "pending_dates", lambda *a, **k: ["2026-09-19"])
    _insert_disclosure_link(db, "DOC-EXISTING", "1301.T")  # 実行前から付いている分。件数は変化しない

    def factory(db_, settings_):
        def job(ctx, params):
            _insert_disclosure_link(db_, "DOC-NEW", "7203.T")  # 新しく付いた開示
            return _job_result(days=1, registered={"documents": 1, "links": 1})

        return job

    monkeypatch.setattr(autoupdate.disclosures, "disclosures_job", factory)
    result = AutoUpdater(db, FakeService(db), settings).run(FakeCtx(), {})
    step = result["steps"]["edinet"]
    assert step["updated_symbols"] == ["7203.T"]  # 1301.T（変化なし）・9984.T（付かず）は入らない
    assert step["changed"] is True


def test_edinet_changed_is_false_when_nothing_registered(env, monkeypatch):
    db, settings = env
    monkeypatch.setenv(EDINET_API_KEY_ENV, "dummy-key")
    monkeypatch.setattr(autoupdate.disclosures, "pending_dates", lambda *a, **k: ["2026-09-19"])
    monkeypatch.setattr(
        autoupdate.disclosures,
        "disclosures_job",
        _fake_disclosures_job(_job_result(days=1, registered={"documents": 0, "links": 0})),
    )

    result = AutoUpdater(db, FakeService(db), settings).run(FakeCtx(), {})
    step = result["steps"]["edinet"]
    assert step["changed"] is False
    assert step["updated_symbols"] == []


def test_edinet_aborted_marks_failed_and_summary_mentions_it(env, monkeypatch):
    db, settings = env
    monkeypatch.setenv(EDINET_API_KEY_ENV, "dummy-key")
    monkeypatch.setattr(
        autoupdate.disclosures, "pending_dates", lambda *a, **k: ["2026-09-19", "2026-09-18"]
    )
    monkeypatch.setattr(
        autoupdate.disclosures,
        "disclosures_job",
        _fake_disclosures_job(_job_result(days=1, aborted="forbidden", errors=["2026-09-18: 403"])),
    )

    result = AutoUpdater(db, FakeService(db), settings).run(FakeCtx(), {})
    step = result["steps"]["edinet"]
    assert step["failed"] is True
    assert "中止" in step["summary"]


def test_edinet_generic_exception_does_not_stop_later_steps(env, monkeypatch):
    db, settings = env
    monkeypatch.setenv(EDINET_API_KEY_ENV, "dummy-key")
    monkeypatch.setattr(autoupdate.disclosures, "pending_dates", lambda *a, **k: ["2026-09-19"])
    monkeypatch.setattr(
        autoupdate.disclosures,
        "disclosures_job",
        _fake_disclosures_job(RuntimeError("EDINET に接続できません")),
    )

    result = AutoUpdater(db, FakeService(db), settings).run(FakeCtx(), {})
    step = result["steps"]["edinet"]
    assert step["failed"] is True
    assert step["summary"] == "EDINET 失敗"
    assert "short" in result["steps"]  # 後続ステップは打ち切られない
    assert result["steps"]["short"]["summary"] == "空売り スキップ（設定オフ）"


def test_edinet_cancelled_propagates(env, monkeypatch):
    db, settings = env
    monkeypatch.setenv(EDINET_API_KEY_ENV, "dummy-key")
    monkeypatch.setattr(autoupdate.disclosures, "pending_dates", lambda *a, **k: ["2026-09-19"])
    monkeypatch.setattr(autoupdate.disclosures, "disclosures_job", _fake_disclosures_job(Cancelled()))

    with pytest.raises(Cancelled):
        AutoUpdater(db, FakeService(db), settings).run(FakeCtx(), {})


def test_step_order_is_prices_taisyaku_edinet_short(env):
    """SPEC §2.8.2 の順（株価→貸借取引残高→開示→空売り）どおりに self.steps が並ぶこと。"""
    db, settings = env
    names = [name for name, _ in AutoUpdater(db, FakeService(db), settings).steps]
    assert names == ["prices", "taisyaku", "edinet", "short"]
