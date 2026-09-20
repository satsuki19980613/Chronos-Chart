"""長時間処理をバックグラウンドのジョブとして実行する（SPEC §2.8.1）。

pywebview は API 呼び出しごとに別スレッドで動くので、画面側は start → status のポーリング → cancel で
進捗の表示と中断ができる。ジョブ関数は func(ctx, params) の形で、リクエストの合間に ctx.check() か
ctx.wait() を呼んで中断に応じる。
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import OrderedDict
from typing import Any, Callable

from .errors import Cancelled, UserFacingError

log = logging.getLogger(__name__)

KEEP_FINISHED = 20  # 終了したジョブの情報をメモリに残す数


class JobContext:
    """ジョブ関数に渡す。進捗の報告と中断の確認に使う。"""

    def __init__(self, job: "_Job"):
        self._job = job
        self.cancel = job.cancel  # HttpClient.get(cancel=...) にそのまま渡せる

    def progress(self, current: int, total: int, label: str = "") -> None:
        with self._job.lock:
            self._job.progress = {"current": current, "total": total, "label": label}

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    def wait(self, seconds: float) -> None:
        """time.sleep の代わり。中断されたらすぐ Cancelled を送出する。"""
        if self.cancel.wait(seconds):
            raise Cancelled()


class _Job:
    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.state = "running"  # running | done | error | cancelled
        self.progress = {"current": 0, "total": 0, "label": ""}
        self.result: Any = None
        self.error: str | None = None
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "state": self.state,
                "progress": dict(self.progress),
                "result": self.result,
                "error": self.error,
                "cancelling": self.cancel.is_set() and self.state == "running",
            }


class JobManager:
    def __init__(self):
        self._funcs: dict[str, Callable[[JobContext, dict], Any]] = {}
        self._jobs: OrderedDict[str, _Job] = OrderedDict()
        self._lock = threading.Lock()

    def register(self, kind: str, func: Callable[[JobContext, dict], Any]) -> None:
        self._funcs[kind] = func

    def start(self, kind: str, params: dict | None = None) -> dict:
        if kind not in self._funcs:
            raise UserFacingError(f"不明な処理です: {kind}")
        with self._lock:
            if any(j.kind == kind and j.state == "running" for j in self._jobs.values()):
                raise UserFacingError("同じ処理が実行中です。完了を待つか、中断してからやり直してください")
            job = _Job(kind)
            self._jobs[job.id] = job
            self._prune()
        job.thread = threading.Thread(target=self._run, args=(job, dict(params or {})), name=f"job-{kind}", daemon=True)
        job.thread.start()
        return job.snapshot()

    def status(self, job_id: str) -> dict:
        return self._get(job_id).snapshot()

    def cancel(self, job_id: str) -> dict:
        job = self._get(job_id)
        job.cancel.set()
        return job.snapshot()

    def active(self) -> list[dict]:
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.state == "running"]
        return [j.snapshot() for j in jobs]

    def join(self, job_id: str, timeout: float | None = None) -> dict:
        """ジョブの終了を待つ（テスト・終了処理用）。"""
        job = self._get(job_id)
        if job.thread is not None:
            job.thread.join(timeout)
        return job.snapshot()

    def _get(self, job_id: str) -> _Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise UserFacingError("処理の情報が見つかりません（アプリを再起動した可能性があります）")
        return job

    def _prune(self) -> None:
        finished = [jid for jid, j in self._jobs.items() if j.state != "running"]
        for jid in finished[: max(0, len(finished) - KEEP_FINISHED)]:
            del self._jobs[jid]

    def _run(self, job: _Job, params: dict) -> None:
        state, result, error = "done", None, None
        try:
            result = self._funcs[job.kind](JobContext(job), params)
        except Cancelled:
            state = "cancelled"
        except UserFacingError as exc:
            state, error = "error", str(exc)
        except Exception as exc:
            log.exception("job %s failed", job.kind)
            state, error = "error", f"予期しないエラーが発生しました: {exc}"
        with job.lock:
            job.state, job.result, job.error = state, result, error
        log.info("job %s (%s) finished: %s", job.kind, job.id, state)


def selftest_job(ctx: JobContext, params: dict) -> dict:
    """進捗表示と中断の動作確認用（--debug と開発サーバーでのみ登録する）。外部へはアクセスしない。"""
    steps = int(params.get("steps", 10))
    for i in range(steps):
        ctx.progress(i, steps, f"動作確認 {i + 1}/{steps}")
        ctx.wait(float(params.get("interval", 1.0)))
    ctx.progress(steps, steps, "完了")
    return {"steps": steps}
