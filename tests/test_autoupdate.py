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

WAIT = 5


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


def set_fetched(db, symbol, when, result="ok"):
    with db.write() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO fetch_log (source, key, fetched_at, result) VALUES ('yahoo', ?, ?, ?)",
            (symbol, when.strftime("%Y-%m-%d %H:%M:%S"), result),
        )


def test_updates_every_stock_in_order_with_pause(env):
    db, settings = env
    service = FakeService(db)
    ctx = FakeCtx()
    result = AutoUpdater(db, service, settings).run(ctx, {})

    assert service.calls == ["1301.T", "7203.T", "9984.T"]
    assert ctx.waits == [autoupdate.PRICE_PAUSE_SEC] * 2  # 銘柄間だけ待つ（先頭の前は待たない）
    assert result["updated_symbols"] == ["1301.T", "7203.T", "9984.T"]
    assert result["summary"] == "株価 3件更新"
    assert result["failed"] is False
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
    service = FakeService(db)
    ctx = FakeCtx()
    result = AutoUpdater(db, service, settings, now=lambda: now).run(ctx, {})
    assert service.calls == []
    assert ctx.waits == []
    assert result["summary"] == "株価 取得済み"
    assert result["updated_symbols"] == []


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
    assert result["updated_symbols"] == ["7203.T", "9984.T"]
    assert result["failed"] is True
    assert result["summary"] == "株価 2件更新・1件失敗"
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
    assert final["result"]["summary"] == "株価 3件更新"


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
