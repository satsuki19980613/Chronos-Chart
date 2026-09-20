"""ジョブ基盤（SPEC §2.8.1）。"""

import threading

import pytest

from app import jobs as jobs_module
from app.errors import UserFacingError
from app.jobs import JobManager, selftest_job

WAIT = 5  # スレッドの終了を待つ上限（秒）


def test_job_runs_to_completion_with_progress_and_result():
    manager = JobManager()
    seen = threading.Event()
    release = threading.Event()

    def work(ctx, params):
        ctx.progress(1, 3, "1件目")
        seen.set()
        release.wait(WAIT)
        ctx.progress(3, 3, "完了")
        return {"echo": params["value"]}

    manager.register("work", work)
    started = manager.start("work", {"value": 42})
    assert started["state"] == "running"

    assert seen.wait(WAIT)
    running = manager.status(started["id"])
    assert running["progress"] == {"current": 1, "total": 3, "label": "1件目"}
    assert [j["id"] for j in manager.active()] == [started["id"]]

    release.set()
    done = manager.join(started["id"], WAIT)
    assert done["state"] == "done"
    assert done["result"] == {"echo": 42}
    assert done["error"] is None
    assert manager.active() == []


def test_cancel_interrupts_wait_immediately():
    manager = JobManager()
    manager.register("slow", lambda ctx, params: ctx.wait(60))
    job = manager.start("slow")
    assert manager.cancel(job["id"])["cancelling"] in (True, False)  # 直後に終わっていてもよい
    final = manager.join(job["id"], WAIT)
    assert final["state"] == "cancelled"
    assert final["error"] is None


def test_check_raises_after_cancel():
    manager = JobManager()
    entered = threading.Event()
    proceed = threading.Event()
    steps = []

    def work(ctx, params):
        entered.set()
        proceed.wait(WAIT)
        ctx.check()
        steps.append("should not run")

    manager.register("w", work)
    job = manager.start("w")
    assert entered.wait(WAIT)
    manager.cancel(job["id"])
    proceed.set()
    assert manager.join(job["id"], WAIT)["state"] == "cancelled"
    assert steps == []


def test_user_facing_error_message_is_passed_through():
    manager = JobManager()

    def work(ctx, params):
        raise UserFacingError("連絡先が未設定です")

    manager.register("w", work)
    final = manager.join(manager.start("w")["id"], WAIT)
    assert final["state"] == "error"
    assert final["error"] == "連絡先が未設定です"


def test_unexpected_error_is_reported_and_logged(caplog):
    manager = JobManager()

    def work(ctx, params):
        raise RuntimeError("boom")

    manager.register("w", work)
    final = manager.join(manager.start("w")["id"], WAIT)
    assert final["state"] == "error"
    assert "予期しないエラー" in final["error"]
    assert "boom" in caplog.text


def test_same_kind_cannot_run_twice_but_other_kinds_can():
    manager = JobManager()
    release = threading.Event()
    manager.register("a", lambda ctx, params: release.wait(WAIT))
    manager.register("b", lambda ctx, params: "ok")

    first = manager.start("a")
    with pytest.raises(UserFacingError):
        manager.start("a")
    other = manager.start("b")
    assert manager.join(other["id"], WAIT)["state"] == "done"

    release.set()
    manager.join(first["id"], WAIT)
    again = manager.start("a")  # 終わった後はまた開始できる
    assert manager.join(again["id"], WAIT)["state"] == "done"


def test_unknown_kind_and_unknown_job():
    manager = JobManager()
    with pytest.raises(UserFacingError):
        manager.start("nope")
    with pytest.raises(UserFacingError):
        manager.status("missing")
    with pytest.raises(UserFacingError):
        manager.cancel("missing")


def test_finished_jobs_are_pruned(monkeypatch):
    monkeypatch.setattr(jobs_module, "KEEP_FINISHED", 3)
    manager = JobManager()
    manager.register("w", lambda ctx, params: None)
    ids = []
    for _ in range(6):
        job = manager.start("w")
        manager.join(job["id"], WAIT)
        ids.append(job["id"])
    manager.join(manager.start("w")["id"], WAIT)  # 次の start で古いものが落ちる

    with pytest.raises(UserFacingError):
        manager.status(ids[0])
    assert manager.status(ids[-1])["state"] == "done"


def test_params_are_copied():
    manager = JobManager()
    params = {"n": 1}
    manager.register("w", lambda ctx, p: p.update(n=2) or p["n"])
    final = manager.join(manager.start("w", params)["id"], WAIT)
    assert final["result"] == 2
    assert params == {"n": 1}


def test_selftest_job_reports_progress_and_can_be_cancelled():
    manager = JobManager()
    manager.register("selftest", selftest_job)
    final = manager.join(manager.start("selftest", {"steps": 3, "interval": 0})["id"], WAIT)
    assert final["state"] == "done"
    assert final["progress"]["current"] == 3
    assert final["result"] == {"steps": 3}

    slow = manager.start("selftest", {"steps": 100, "interval": 30})
    manager.cancel(slow["id"])
    assert manager.join(slow["id"], WAIT)["state"] == "cancelled"


# ---------- JS 公開 API 経由 ----------
def test_api_exposes_jobs_with_the_standard_envelope():
    from app.api import Api

    manager = JobManager()
    manager.register("selftest", selftest_job)
    api = Api(service=None, jobs=manager)

    started = api.start_job("selftest", {"steps": 2, "interval": 0})
    assert started["ok"] is True
    job_id = started["data"]["id"]
    manager.join(job_id, WAIT)

    status = api.job_status(job_id)
    assert status["ok"] is True
    assert status["data"]["state"] == "done"
    assert api.active_jobs() == {"ok": True, "data": []}
    assert api.cancel_job(job_id)["ok"] is True  # 終了後の中断要求は無害

    unknown = api.start_job("nope")
    assert unknown["ok"] is False
    assert "不明な処理" in unknown["error"]
    assert api.job_status("missing")["ok"] is False
