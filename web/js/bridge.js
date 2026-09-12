// Python API の呼び出し口。
// 通常は pywebview のネイティブブリッジを使い、dev_server.py で ?dev 付き URL を開いたときだけ HTTP を使う。
(function () {
  const devMode = new URLSearchParams(location.search).has("dev");

  const ready = new Promise((resolve, reject) => {
    if (devMode || (window.pywebview && window.pywebview.api)) return resolve();
    const timer = setTimeout(() => reject(new Error("pywebview API に接続できませんでした")), 15000);
    window.addEventListener("pywebviewready", () => {
      clearTimeout(timer);
      resolve();
    }, { once: true });
  });

  async function call(method, ...args) {
    await ready;
    let res;
    if (devMode) {
      const r = await fetch(`/api/${method}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(args),
      });
      res = await r.json();
    } else {
      res = await window.pywebview.api[method](...args);
    }
    if (!res || !res.ok) throw new Error((res && res.error) || "通信に失敗しました");
    return res.data;
  }

  window.api = { call, ready };
})();
