// 設定タブ（SPEC §2.1）。API キーは画面にはマスクした値しか持たず、「表示」を押したときだけ平文を取りに行く。
window.SettingsView = (function () {
  const $ = (id) => document.getElementById(id);
  const REVEAL_MS = 10000;
  const fields = () => [...document.querySelectorAll("#view-settings [data-setting]")];
  const secretRows = () => [...document.querySelectorAll("#view-settings .secret-row")];

  function render(view) {
    for (const el of fields()) {
      const value = view.values[el.dataset.setting];
      if (el.type === "checkbox") el.checked = Boolean(value);
      else el.value = value ?? "";
    }
    for (const row of secretRows()) {
      const info = view.secrets[row.dataset.secret];
      const current = row.querySelector(".secret-current");
      const fromEnv = info.source === "env";
      current.textContent = info.masked ? `${info.masked}${fromEnv ? "（環境変数）" : ""}` : "未設定";
      current.classList.toggle("is-set", Boolean(info.masked));
      row.querySelector("input").value = "";
      // 環境変数で指定されている間は、そちらが優先されるので画面からは変更できない
      row.querySelector("input").disabled = fromEnv;
      row.querySelector("input").placeholder = fromEnv
        ? `環境変数 ${info.env} が優先されています`
        : "新しいキーを入力（変更しない場合は空のまま）";
      row.querySelector('[data-act="reveal"]').disabled = !info.masked;
      row.querySelector('[data-act="clear"]').disabled = !info.masked || fromEnv;
    }
    $("settings-warnings").innerHTML = view.warnings
      .map((w) => `<div class="notice">${window.fmt.escape(w)}</div>`).join("");
  }

  async function load() {
    try {
      render(await api.call("get_settings"));
    } catch (err) {
      App.toast(err.message, "error");
    }
  }

  function collect() {
    const values = {};
    for (const el of fields()) values[el.dataset.setting] = el.type === "checkbox" ? el.checked : el.value;
    for (const row of secretRows()) {
      const typed = row.querySelector("input").value.trim();
      if (typed) values[row.dataset.secret] = typed; // 空欄は「変更しない」
    }
    return values;
  }

  async function save() {
    try {
      render(await api.call("save_settings", collect()));
      $("settings-saved").textContent = `保存しました（${new Date().toLocaleTimeString("ja-JP")}）`;
      App.toast("設定を保存しました");
    } catch (err) {
      App.toast(err.message, "error", 8000);
    }
  }

  async function onSecretAction(row, act) {
    const key = row.dataset.secret;
    try {
      if (act === "reveal") {
        const current = row.querySelector(".secret-current");
        const masked = current.textContent;
        current.textContent = await api.call("reveal_secret", key);
        setTimeout(() => { if (current.isConnected) current.textContent = masked; }, REVEAL_MS);
      } else if (act === "clear") {
        if (!confirm("保存されている API キーを削除しますか？")) return;
        render(await api.call("save_settings", { [key]: "" }));
        App.toast("API キーを削除しました");
      }
    } catch (err) {
      App.toast(err.message, "error", 8000);
    }
  }

  // 接続テスト（SPEC §2.1.3）。API キーの値そのものは画面に出さない
  async function testConnection(target, btn, resultEl) {
    btn.disabled = true;
    resultEl.classList.remove("is-ok", "is-error");
    resultEl.textContent = "確認中…";
    try {
      const data = await api.call("test_connection", target);
      resultEl.textContent = data.message;
      resultEl.classList.add("is-ok");
    } catch (err) {
      resultEl.textContent = err.message;
      resultEl.classList.add("is-error");
    } finally {
      btn.disabled = false;
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("settings-save").addEventListener("click", save);
    $("view-settings").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-act]");
      if (btn) onSecretAction(btn.closest(".secret-row"), btn.dataset.act);
    });
    $("test-edinet").addEventListener("click", () => testConnection("edinet", $("test-edinet"), $("test-edinet-result")));
  });

  return { load };
})();
