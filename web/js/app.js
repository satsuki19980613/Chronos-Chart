// 画面制御（タブ切り替え・登録画面・ダッシュボード）
(function () {
  const $ = (id) => document.getElementById(id);
  const f = window.fmt;
  const STORAGE_KEY = "autotechnical.chart.v2";

  const state = {
    stocks: [],
    currentSymbol: null,
    dashboard: null,
    chart: null,
    range: "all",
    chartSettings: loadChartSettings(),
    exportSelected: new Set(),
    exportDays: "60",
  };

  // ---------- 共通 ----------
  function loadChartSettings() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY));
      if (saved && Array.isArray(saved.overlays) && Array.isArray(saved.panes)) {
        const ids = (items) => new Set(items.map((i) => i.id));
        const overlayIds = ids(StockChart.OVERLAYS);
        const paneIds = ids(StockChart.PANES);
        return {
          overlays: saved.overlays.filter((id) => overlayIds.has(id)),
          panes: saved.panes.filter((id) => paneIds.has(id)),
        };
      }
    } catch (_) { /* 保存値がなければ既定値 */ }
    return { overlays: [...StockChart.DEFAULTS.overlays], panes: [...StockChart.DEFAULTS.panes] };
  }

  function saveChartSettings() {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state.chartSettings)); } catch (_) { /* noop */ }
  }

  function toast(message, type = "info", ms = 4000) {
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.textContent = message;
    $("toasts").appendChild(el);
    setTimeout(() => el.remove(), ms);
  }

  async function withBusy(text, fn) {
    $("busy-text").textContent = text;
    $("busy").hidden = false;
    try {
      return await fn();
    } catch (err) {
      toast(err.message, "error", 6000);
      return undefined;
    } finally {
      $("busy").hidden = true;
    }
  }

  function showWarnings(result) {
    (result?.warnings || []).forEach((w) => toast(w, "warn", 7000));
  }

  function switchTab(name) {
    document.querySelectorAll(".tab").forEach((t) => {
      const active = t.dataset.tab === name;
      t.classList.toggle("is-active", active);
      t.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll(".view").forEach((v) => v.classList.toggle("is-active", v.id === `view-${name}`));
    if (name === "dashboard") openDashboard(state.currentSymbol);
    if (name === "export") loadExportFiles();
  }

  // ---------- 登録画面 ----------
  async function search(event) {
    event.preventDefault();
    const query = $("search-input").value.trim();
    if (!query) return;
    const results = await withBusy("検索中…", () => api.call("search", query));
    if (!results) return;
    renderSearchResults(results);
  }

  function renderSearchResults(results) {
    $("search-results-wrap").hidden = results.length === 0;
    $("search-empty").hidden = results.length !== 0;
    $("search-results").innerHTML = results.map((r, i) => `
      <tr>
        <td class="symbol">${f.escape(r.symbol)}</td>
        <td>${f.escape(r.name)}</td>
        <td>${f.escape(r.exchange || "—")}</td>
        <td class="muted">${f.escape(r.quote_type || "—")}</td>
        <td class="actions">
          ${r.registered
            ? `<span class="tag tag-ok">登録済み</span> <button class="btn btn-sm" data-action="view" data-symbol="${f.escape(r.symbol)}">表示</button>`
            : `<button class="btn btn-sm btn-primary" data-action="register" data-index="${i}">登録</button>`}
        </td>
      </tr>`).join("");
    $("search-results").onclick = async (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      if (btn.dataset.action === "view") return viewStock(btn.dataset.symbol);
      const r = results[Number(btn.dataset.index)];
      const res = await withBusy(`${r.symbol} の過去1年分の株価を取得しています…`, () => api.call("register", r.symbol, r.name, r.exchange));
      if (!res) return;
      toast(`${r.name}（${r.symbol}）を登録しました（${res.added}件）`);
      showWarnings(res);
      r.registered = true;
      renderSearchResults(results);
      await refreshStocks();
    };
  }

  async function refreshStocks() {
    try {
      state.stocks = await api.call("list_stocks");
    } catch (err) {
      toast(err.message, "error");
      return;
    }
    renderStockList();
    renderStockSelect();
    renderExportStocks();
  }

  function renderStockList() {
    const list = state.stocks;
    $("stock-count").textContent = list.length;
    $("stock-empty").hidden = list.length > 0;
    $("update-all").disabled = list.length === 0;
    $("stock-list").innerHTML = list.map((s) => `
      <tr>
        <td class="symbol">${f.escape(s.code)}</td>
        <td>${f.escape(s.name)} <span class="muted">${f.escape(s.symbol)}</span></td>
        <td>${f.escape(s.exchange || "—")}</td>
        <td>${f.date(s.first_date)} 〜 ${f.date(s.last_date)}</td>
        <td class="num">${f.num(s.row_count)}</td>
        <td class="muted">${f.escape(s.last_updated || "—")}</td>
        <td class="actions">
          <button class="btn btn-sm" data-action="view" data-symbol="${f.escape(s.symbol)}">ダッシュボード</button>
          <button class="btn btn-sm" data-action="update" data-symbol="${f.escape(s.symbol)}">更新</button>
          <button class="btn btn-sm btn-danger" data-action="delete" data-symbol="${f.escape(s.symbol)}">削除</button>
        </td>
      </tr>`).join("");
  }

  async function onStockListClick(e) {
    const btn = e.target.closest("button");
    if (!btn) return;
    const symbol = btn.dataset.symbol;
    const stock = state.stocks.find((s) => s.symbol === symbol);
    if (btn.dataset.action === "view") return viewStock(symbol);
    if (btn.dataset.action === "update") {
      const res = await withBusy(`${symbol} を更新しています…`, () => api.call("update", symbol));
      if (!res) return;
      toast(`${stock.name} を更新しました（新規 ${res.added}件）`);
      showWarnings(res);
      await refreshStocks();
    }
    if (btn.dataset.action === "delete") {
      if (!confirm(`${stock.name}（${symbol}）を削除しますか？\nデータベースの株価・指標と CSV ファイルが削除されます。`)) return;
      const ok = await withBusy("削除しています…", () => api.call("delete", symbol));
      if (!ok) return;
      toast(`${stock.name} を削除しました`);
      if (state.currentSymbol === symbol) state.currentSymbol = null;
      await refreshStocks();
    }
  }

  async function updateAll() {
    const res = await withBusy("全銘柄を更新しています…", () => api.call("update_all"));
    if (!res) return;
    toast(`${res.updated}銘柄を更新しました`);
    res.errors.forEach((err) => toast(err, "error", 8000));
    showWarnings(res);
    await refreshStocks();
  }

  function viewStock(symbol) {
    state.currentSymbol = symbol;
    switchTab("dashboard");
  }

  // ---------- ダッシュボード ----------
  function renderStockSelect() {
    $("stock-select").innerHTML = state.stocks
      .map((s) => `<option value="${f.escape(s.symbol)}">${f.escape(s.code)}　${f.escape(s.name)}</option>`)
      .join("");
    if (state.currentSymbol) $("stock-select").value = state.currentSymbol;
  }

  async function openDashboard(symbol) {
    const hasStocks = state.stocks.length > 0;
    $("dash-empty").hidden = hasStocks;
    $("dash-content").hidden = !hasStocks;
    $("dash-update").disabled = !hasStocks;
    $("stock-select").disabled = !hasStocks;
    if (!hasStocks) {
      destroyChart();
      return;
    }
    const target = symbol && state.stocks.some((s) => s.symbol === symbol) ? symbol : state.stocks[0].symbol;
    state.currentSymbol = target;
    $("stock-select").value = target;
    const data = await withBusy("読み込み中…", () => api.call("dashboard", target));
    if (!data) return;
    state.dashboard = data;
    renderDashboard();
  }

  function renderDashboard() {
    const data = state.dashboard;
    const { stock, quote } = data;
    const cur = stock.currency;

    $("q-name").textContent = stock.name;
    $("q-sub").textContent = `${stock.symbol}・${stock.exchange || "—"}・${f.date(quote.date)} 時点`;
    $("q-close").textContent = f.price(quote.close, cur);
    const cls = quote.change > 0 ? "is-up" : quote.change < 0 ? "is-down" : "";
    $("q-change").className = `change ${cls}`;
    $("q-change").textContent = quote.change === null ? "" : `${f.signed(quote.change, f.priceDigits(cur))}（${f.signed(quote.change_pct, 2, "%")}）`;
    $("q-open").textContent = f.price(quote.open, cur);
    $("q-high").textContent = f.price(quote.high, cur);
    $("q-low").textContent = f.price(quote.low, cur);
    $("q-volume").textContent = f.volume(quote.volume);
    $("dash-updated").textContent = stock.last_updated ? `最終更新 ${stock.last_updated}` : "";
    $("cards-date").textContent = f.date(quote.date);

    renderCards(data.cards, cur);
    renderSignals(data.signals);
    renderTable(data.table, cur);
    renderChart();
  }

  const GROUPS = { trend: "トレンド系", oscillator: "オシレーター系", volatility: "ボラティリティ" };
  const STATUS = { bull: "強気", bear: "弱気", neutral: "中立", na: "—" };

  function cardValue(c, currency) {
    if (c.value === null) return "";
    if (["sma", "ema", "ichimoku", "parabolic", "bb", "stddev", "momentum"].includes(c.key)) return f.price(c.value, currency);
    if (c.key.startsWith("deviation_")) return f.signed(c.value, 2, "%");
    if (c.key === "macd") return f.num(c.value, 2);
    return f.num(c.value, 1);
  }

  function renderCards(cards, currency) {
    $("cards").innerHTML = Object.entries(GROUPS).map(([group, title]) => {
      const items = cards.filter((c) => c.group === group);
      if (!items.length) return "";
      return `<div class="card-group"><div class="card-group-title">${title}</div>${items.map((c) => `
        <div class="ind-card ${c.status}">
          <div class="name">${f.escape(c.label)}<span class="badge ${c.status}">${STATUS[c.status]}</span></div>
          <div class="value">${cardValue(c, currency)}</div>
          <div class="note">${f.escape(c.note)}</div>
        </div>`).join("")}</div>`;
    }).join("");
  }

  function renderSignals(signals) {
    $("signals").innerHTML = signals.length
      ? signals.map((s) => `
        <li>
          <span class="date">${f.date(s.date)}</span>
          <span class="signal-dot ${s.direction}"></span>
          <span>${f.escape(s.label)}</span>
        </li>`).join("")
      : `<li class="muted">シグナルはありません</li>`;
  }

  function renderTable(table, currency) {
    const priceKeys = new Set(["open", "high", "low", "close"]);
    const format = (key, v) => {
      if (key === "date") return f.date(v);
      if (v === null || v === undefined) return "—";
      if (key === "volume" || key.startsWith("volume_ma")) return f.num(v);
      if (priceKeys.has(key)) return f.price(v, currency);
      return f.num(v, 2);
    };
    $("data-table").innerHTML = `
      <thead><tr>${table.columns.map((c) => `<th>${f.escape(c.label)}</th>`).join("")}</tr></thead>
      <tbody>${table.rows.map((r) => `<tr>${table.columns.map((c) => `<td>${format(c.key, r[c.key])}</td>`).join("")}</tr>`).join("")}</tbody>`;
  }

  function destroyChart() {
    if (state.chart) {
      state.chart.destroy();
      state.chart = null;
    }
  }

  function renderChart() {
    if (!state.dashboard) return;
    destroyChart();
    state.chart = StockChart.render(
      $("chart"), $("pane-labels"), $("chart-legend"),
      state.dashboard, state.dashboard.stock.currency, state.chartSettings,
    );
    state.chart.setRange(state.range);
  }

  function renderChips() {
    const build = (items, key, el) => {
      el.innerHTML = items.map((item) => `
        <button class="chip ${state.chartSettings[key].includes(item.id) ? "is-on" : ""}"
                data-id="${item.id}" style="--chip-color:${item.color}">${f.escape(item.label)}</button>`).join("");
      el.onclick = (e) => {
        const btn = e.target.closest(".chip");
        if (!btn) return;
        const list = state.chartSettings[key];
        const idx = list.indexOf(btn.dataset.id);
        if (idx >= 0) list.splice(idx, 1); else list.push(btn.dataset.id);
        btn.classList.toggle("is-on", idx < 0);
        saveChartSettings();
        renderChart();
      };
    };
    build(StockChart.OVERLAYS, "overlays", $("overlay-chips"));
    build(StockChart.PANES, "panes", $("pane-chips"));
  }

  // ---------- 出力画面 ----------
  function renderExportStocks() {
    const symbols = new Set(state.stocks.map((s) => s.symbol));
    state.exportSelected = new Set([...state.exportSelected].filter((s) => symbols.has(s)));

    $("export-empty").hidden = state.stocks.length > 0;
    $("export-stocks").innerHTML = state.stocks.map((s) => {
      const checked = state.exportSelected.has(s.symbol);
      return `
        <tr data-symbol="${f.escape(s.symbol)}" class="${checked ? "is-selected" : ""}">
          <td><input type="checkbox" ${checked ? "checked" : ""} aria-label="${f.escape(s.name)}を選択"></td>
          <td class="symbol">${f.escape(s.code)}</td>
          <td>${f.escape(s.name)} <span class="muted">${f.escape(s.symbol)}</span></td>
          <td>${f.escape(s.exchange || "—")}</td>
          <td>${f.date(s.first_date)} 〜 ${f.date(s.last_date)}</td>
          <td class="num">${f.num(s.row_count)}</td>
        </tr>`;
    }).join("");
    updateExportControls();
  }

  function updateExportControls() {
    const n = state.exportSelected.size;
    $("export-selected").textContent = n;
    $("export-run").disabled = n === 0;
    $("export-run").textContent = n ? `${n}銘柄を出力する` : "出力する";
    const all = $("export-check-all");
    all.checked = n > 0 && n === state.stocks.length;
    all.indeterminate = n > 0 && n < state.stocks.length;
    all.disabled = state.stocks.length === 0;
  }

  function toggleExportRow(e) {
    const row = e.target.closest("tr[data-symbol]");
    if (!row) return;
    const symbol = row.dataset.symbol;
    const selected = !state.exportSelected.has(symbol);
    if (selected) state.exportSelected.add(symbol); else state.exportSelected.delete(symbol);
    row.classList.toggle("is-selected", selected);
    row.querySelector("input").checked = selected;
    updateExportControls();
  }

  async function loadExportFiles() {
    let files;
    try {
      files = await api.call("list_exports");
    } catch (err) {
      toast(err.message, "error");
      return;
    }
    $("export-files-empty").hidden = files.length > 0;
    $("export-files").innerHTML = files.map((file) => `
      <tr>
        <td class="file-name">${f.escape(file.name)}</td>
        <td><span class="tag">${file.format === "csv" ? "CSV" : "Markdown"}</span></td>
        <td class="num">${f.num(file.size / 1024, 1)} KB</td>
        <td class="muted">${f.escape(file.modified)}</td>
      </tr>`).join("");
  }

  async function runExport() {
    // 表示順（コード順）で出力する
    const symbols = state.stocks.map((s) => s.symbol).filter((s) => state.exportSelected.has(s));
    if (!symbols.length) return;
    const fmt = document.querySelector('input[name="export-format"]:checked').value;
    const days = state.exportDays ? Number(state.exportDays) : null;
    const res = await withBusy("出力しています…", () => api.call("export", symbols, fmt, days));
    if (!res) return;
    toast(`${res.name} を出力しました（${res.symbols.length}銘柄・${f.num(res.rows)}行）`, "info", 6000);
    $("export-dir").textContent = `出力先: ${res.path}`;
    await loadExportFiles();
  }

  // ---------- 初期化 ----------
  function bind() {
    document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));
    $("search-form").addEventListener("submit", search);
    $("stock-list").addEventListener("click", onStockListClick);
    $("update-all").addEventListener("click", updateAll);
    $("stock-select").addEventListener("change", (e) => openDashboard(e.target.value));
    $("dash-update").addEventListener("click", async () => {
      const symbol = state.currentSymbol;
      const res = await withBusy(`${symbol} を更新しています…`, () => api.call("update", symbol));
      if (!res) return;
      toast(`更新しました（新規 ${res.added}件）`);
      showWarnings(res);
      await refreshStocks();
      await openDashboard(symbol);
    });
    $("range-buttons").addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      state.range = btn.dataset.range;
      document.querySelectorAll("#range-buttons button").forEach((b) => b.classList.toggle("is-active", b === btn));
      state.chart?.setRange(state.range);
    });
    const openFolder = (method) => async () => {
      try {
        await api.call(method);
      } catch (err) {
        toast(err.message, "error");
      }
    };
    $("open-csv").addEventListener("click", openFolder("open_csv_folder"));
    $("open-output").addEventListener("click", openFolder("open_output_folder"));

    $("export-stocks").addEventListener("click", toggleExportRow);
    $("export-check-all").addEventListener("change", (e) => {
      state.exportSelected = e.target.checked ? new Set(state.stocks.map((s) => s.symbol)) : new Set();
      renderExportStocks();
    });
    $("export-days").addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      state.exportDays = btn.dataset.days;
      document.querySelectorAll("#export-days button").forEach((b) => b.classList.toggle("is-active", b === btn));
    });
    $("export-run").addEventListener("click", runExport);
  }

  async function init() {
    bind();
    renderChips();
    try {
      await api.ready;
    } catch (err) {
      toast(err.message, "error", 10000);
      return;
    }
    await refreshStocks();
  }

  init();
})();
