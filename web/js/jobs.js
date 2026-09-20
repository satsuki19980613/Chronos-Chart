// バックグラウンドのジョブ（長時間処理）の開始・進捗表示・中断。
// Python 側の start_job / job_status / cancel_job / active_jobs をポーリングで使う。
window.Jobs = (function () {
  const POLL_MS = 1000;
  const $ = (id) => document.getElementById(id);
  const running = new Map(); // job_id -> { kind, last }
  let shownId = null;

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function render() {
    const box = $("job-status");
    // 後から始めたジョブを優先して表示する
    const ids = [...running.keys()];
    shownId = ids.length ? ids[ids.length - 1] : null;
    box.hidden = shownId === null;
    if (shownId === null) return;

    const job = running.get(shownId).last;
    const { current, total, label } = job.progress;
    $("job-label").textContent = job.cancelling ? "中断しています…" : (label || "処理中…");
    $("job-bar-fill").style.width = total > 0 ? `${Math.min(100, (current / total) * 100)}%` : "";
    $("job-bar-fill").parentElement.classList.toggle("is-indeterminate", !(total > 0));
    $("job-cancel").disabled = job.cancelling;
  }

  // ジョブの終了まで追いかけて、最終状態を返す
  async function follow(job, onProgress) {
    running.set(job.id, { kind: job.kind, last: job });
    render();
    try {
      while (job.state === "running") {
        await sleep(POLL_MS);
        job = await api.call("job_status", job.id);
        running.get(job.id).last = job;
        render();
        if (onProgress) onProgress(job);
      }
      return job;
    } finally {
      running.delete(job.id);
      render();
    }
  }

  // ジョブを開始して終了を待つ。state が "error" のときは例外にする（"cancelled" は例外にしない）
  async function run(kind, params = null, onProgress = null) {
    const job = await follow(await api.call("start_job", kind, params), onProgress);
    if (job.state === "error") throw new Error(job.error || "処理に失敗しました");
    return job;
  }

  // 画面を再読込したときに、実行中のジョブの表示を復帰する
  async function resume() {
    const jobs = await api.call("active_jobs");
    jobs.forEach((job) => { follow(job).catch(() => {}); });
  }

  function isRunning(kind) {
    return [...running.values()].some((j) => j.kind === kind);
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("job-cancel").addEventListener("click", async () => {
      if (shownId === null) return;
      try {
        const job = await api.call("cancel_job", shownId);
        if (running.has(job.id)) running.get(job.id).last = job;
        render();
      } catch (_) { /* 既に終わっている */ }
    });
  });

  return { run, resume, isRunning };
})();
