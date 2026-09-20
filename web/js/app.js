// 画面制御（タブ切り替え・登録画面・ダッシュボード）
(function () {
  const $ = (id) => document.getElementById(id);
  const f = window.fmt;
  const STORAGE_KEY = "chronos.chart.v2";

  const state = {
    stocks: [],
    currentSymbol: null,
    dashboard: null,
    chart: null,
    range: "all",
    chartSettings: loadChartSettings(),
    exportSelected: new Set(),
    exportDays: "60",
    visibleRange: null, // チャートの表示範囲 {from, to}（"YYYY-MM-DD"）。P5-4 でイベント欄の絞り込みに使う
    pendingMarkerId: null, // 直近クリックされたマーカーの id（"ev:<日付>"）。P5-5 でイベント欄の強調に使う
  };

  // チャートの表示範囲が変わるたびに呼ばれる（render() 登録直後にも1回呼ばれる）。
  function onChartRangeChange(range) {
    state.visibleRange = range;
    renderEvents();
  }

  // マーカークリックのたびに呼ばれる。イベント欄の該当行へスクロール・強調するのに加え、
  // その日（marker_date）の開示を全件モーダルで開く（P9-3）
  function onMarkerClick(id) {
    state.pendingMarkerId = id;
    highlightEventRow(id);
    const date = id.slice(3);
    const items = (state.dashboard?.events?.items || []).filter((item) => item.marker_date === date);
    openDisclosureModal(items, date);
  }

  // ---------- 共通 ----------
  // 保存値には「保存時点で存在したチップ id の一覧」を known として持たせる。
  // 新しいチップ（例: disclosures）を既定 ON で追加しても、既存ユーザーの保存値には入っていないため
  // 「既定 ON なのに保存値に無いので OFF 扱い」になってしまう。DEFAULTS で ON かつ known に無い id だけを
  // 追加すれば、ユーザーが意図的に OFF にしたチップは復活させずに済む。この移行は今後チップを足すときにも効く
  function migrateChipList(saved, savedKnown, allIds, defaultIds, legacyKnownIds) {
    const known = savedKnown ? new Set(savedKnown) : legacyKnownIds; // known を持たない古い保存値の既定
    const list = saved.filter((id) => allIds.has(id));
    const listSet = new Set(list);
    for (const id of defaultIds) {
      if (allIds.has(id) && !known.has(id) && !listSet.has(id)) {
        list.push(id);
        listSet.add(id);
      }
    }
    return list;
  }

  function loadChartSettings() {
    const overlayIds = new Set(StockChart.OVERLAYS.map((i) => i.id));
    const paneIds = new Set(StockChart.PANES.map((i) => i.id));
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY));
      if (saved && Array.isArray(saved.overlays) && Array.isArray(saved.panes)) {
        // known を持たない古い保存値は「disclosures 以外はすべて既知」とみなす（このチップを今回追加したため）
        const legacyKnownOverlays = new Set([...overlayIds].filter((id) => id !== "disclosures"));
        return {
          overlays: migrateChipList(saved.overlays, saved.knownOverlays, overlayIds, StockChart.DEFAULTS.overlays, legacyKnownOverlays),
          panes: migrateChipList(saved.panes, saved.knownPanes, paneIds, StockChart.DEFAULTS.panes, paneIds),
        };
      }
    } catch (_) { /* 保存値がなければ既定値 */ }
    return { overlays: [...StockChart.DEFAULTS.overlays], panes: [...StockChart.DEFAULTS.panes] };
  }

  function saveChartSettings() {
    try {
      const payload = {
        overlays: state.chartSettings.overlays,
        panes: state.chartSettings.panes,
        // 次回の移行判定用に、保存時点で存在したチップ id を控えておく
        knownOverlays: StockChart.OVERLAYS.map((i) => i.id),
        knownPanes: StockChart.PANES.map((i) => i.id),
      };
      localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
    } catch (_) { /* noop */ }
  }

  function toast(message, type = "info", ms = 4000) {
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.textContent = message;
    $("toasts").appendChild(el);
    setTimeout(() => el.remove(), ms);
  }

  window.App = { toast }; // 他の画面スクリプト（settings.js など）から使う

  async function withBusy(text, fn) {
    if (!$("busy").hidden) return undefined; // 処理中の二重実行（Enter 連打など）を防ぐ
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
    if (name === "register") { refreshShortAllAvailability(); refreshDisclosuresAvailability(); }
    if (name === "dashboard") openDashboard(state.currentSymbol);
    if (name === "export") loadExportFiles();
    if (name === "settings") SettingsView.load();
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
      await offerDisclosuresAfterRegister();
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
    refreshShortAllAvailability();
    refreshDisclosuresAvailability();
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

  // ---------- 空売り残高の一括取得（SPEC §2.2.4） ----------

  // scrape_contact（karauri.net への連絡先。未設定の間はアクセスしない＝不変条件12）が
  // 入力済みかどうかを返す。登録タブ・ダッシュボードの両方から使う共通判定
  async function scrapeContactOk() {
    try {
      const settings = await api.call("get_settings");
      return Boolean((settings.values.scrape_contact || "").trim());
    } catch (_) {
      // 設定を読めなくても画面自体は使えるようにしておく（判断は次回の呼び出しに委ねる。
      // 実際のアクセス可否は Python 側が最終判断するので、ここで false 側に倒す必要はない）
      return true;
    }
  }

  async function refreshShortAllAvailability() {
    const btn = $("short-all");
    const hint = $("short-all-contact-hint");
    if (state.stocks.length === 0) {
      btn.disabled = true;
      hint.hidden = true;
      return;
    }
    const ok = await scrapeContactOk();
    btn.disabled = !ok;
    btn.title = ok ? "" : "設定タブで karauri.net への連絡先を入力してください";
    hint.hidden = ok;
  }

  async function runShortAll() {
    let est;
    try {
      est = await api.call("estimate_short_all");
    } catch (err) {
      toast(err.message, "error", 8000);
      return;
    }
    if (est.targets === 0) {
      toast(est.skipped ? "取得が必要な銘柄はありません（すべて取得済みです）" : "対象銘柄がありません");
      return;
    }
    const minutes = Math.max(1, Math.round(est.eta_sec / 60));
    const ok = confirm(
      `対象 ${est.targets} 銘柄 × 間隔 ${est.interval_sec} 秒 ＝ およそ ${minutes} 分かかります。実行しますか？\n\n` +
      "取得は深夜〜早朝の実行を推奨します（公表は取引時間外のため）。実行中はいつでも中断できます。"
    );
    if (!ok) return;

    let job;
    try {
      job = await Jobs.run("short_all", {});
    } catch (err) {
      toast(err.message, "error", 8000);
      return;
    }
    if (job.state === "cancelled") {
      toast("空売り残高の一括取得を中断しました");
    } else {
      toast(job.result?.summary || "空売り残高の一括取得が完了しました", job.result?.aborted ? "warn" : "info", 8000);
    }
    await refreshStocks();
    if (state.currentSymbol && $("view-dashboard").classList.contains("is-active")) await openDashboard(state.currentSymbol);
  }

  // ---------- 開示（EDINET）の取得（SPEC §2.4.4） ----------
  async function refreshDisclosuresAvailability() {
    const fetchBtn = $("disclosures-fetch");
    const redoBtn = $("disclosures-redo");
    try {
      const settings = await api.call("get_settings");
      const ok = Boolean(settings.secrets.edinet_api_key.source);
      const title = ok ? "" : "設定タブで EDINET の API キーを登録してください";
      fetchBtn.disabled = !ok;
      fetchBtn.title = title;
      redoBtn.disabled = !ok;
      redoBtn.title = title;
    } catch (_) {
      // 設定を読めなくても登録画面自体は使えるようにしておく（判断は次回の refreshStocks に委ねる）
    }
  }

  // 見積り結果から「取得する日数」「探す範囲」の行を組み立てる（確認ダイアログの共通部分）。
  // 「対象 N 日分」と期間を同じ行に並べると、N と期間の日数が一致しないときに食い違って見える
  //（当日を60秒以内に取得済みなら対象から外れる、など）。行を分け、期間は「探した範囲」だと分かるようにする
  function disclosuresRangeText(est, redoDays) {
    const etaText = est.eta_sec < 60 ? "1分未満で終わります" : `およそ ${Math.round(est.eta_sec / 60)} 分かかります`;
    const rangeText = est.start ? `${est.start} 〜 ${est.end}` : est.end;
    let text = `取得する日数: ${est.targets} 日分（間隔 ${est.interval_sec} 秒 ＝ ${etaText}）\n`;
    text += `探す範囲: ${rangeText}`;
    if (redoDays > 0) {
      text += "\n\nこれは取得済みの日付も含めてもう一度取りに行く操作です（過去分にも取下げ・書類情報の修正による更新が入ることがあります）。";
    }
    return text;
  }

  // 登録タブの「開示を取得」「直近90日を取り直す」と、登録直後の取得提案（P8-2）とで
  // ジョブの起動〜完了待ち〜トースト〜画面更新を共用する。
  // buildMessage(rangeText, est) は確認ダイアログの文面を返す（省略時は既定の文面）。
  // onEmpty(est) は対象日が0件のときに呼ばれる（省略時はトーストで知らせる。登録直後の提案では
  // 「何も言わない」を渡して登録の邪魔をしないようにする）
  async function runDisclosures(redoDays, { buildMessage, onEmpty } = {}) {
    let est;
    try {
      est = await api.call("estimate_disclosures", redoDays);
    } catch (err) {
      toast(err.message, "error", 8000);
      return;
    }
    if (est.targets === 0) {
      if (onEmpty) onEmpty(est);
      else toast("取得が必要な日付はありません（すべて取得済みです）");
      return;
    }
    const rangeText = disclosuresRangeText(est, redoDays);
    const message = buildMessage
      ? buildMessage(rangeText, est)
      : `${rangeText}\n\n実行しますか？（実行中はいつでも中断できます）`;
    const ok = confirm(message);
    if (!ok) return;

    let job;
    try {
      job = await Jobs.run("disclosures", redoDays > 0 ? { redo_days: redoDays } : {});
    } catch (err) {
      toast(err.message, "error", 8000);
      return;
    }
    if (job.state === "cancelled") {
      toast("開示の取得を中断しました");
    } else {
      toast(job.result?.summary || "開示の取得が完了しました", job.result?.aborted ? "warn" : "info", 8000);
    }
    if (state.currentSymbol && $("view-dashboard").classList.contains("is-active")) await openDashboard(state.currentSymbol);
  }

  // 登録直後に、その銘柄の期間ぶんの開示取得を提案する（P8-2）。起動時の自動更新は直近30日分しか
  // 取りに行かず、銘柄登録時はキャッシュの再走査だけで API を呼ばない（SPEC §2.4.2）。そのため
  // 過去1年分の株価を登録しても開示はキャッシュにある数日分しか埋まらない。EDINET キー未設定・
  // 対象0件のときは登録のじゃまをしないよう何も言わずに終わる
  async function offerDisclosuresAfterRegister() {
    let settings;
    try {
      settings = await api.call("get_settings");
    } catch (_) {
      return; // 登録自体は成功しているので、提案できないだけで諦める
    }
    if (!settings.secrets.edinet_api_key.source) return; // 未設定なら提案しない

    await runDisclosures(0, {
      onEmpty: () => {}, // 対象0件（＝新規銘柄の期間もすでに取得済み）なら何も言わない
      buildMessage: (rangeText) => {
        let message = "登録した銘柄の期間に合わせて、EDINET の開示を取得できます。\n\n";
        message += `${rangeText}\n\n`;
        message += "いまは起動時の自動更新では直近30日分しか取りに行きません。過去の開示を見るにはこの取得が必要です。\n";
        message += "実行しますか？（実行中はいつでも中断できます。あとで「開示を取得」からも実行できます）";
        return message;
      },
    });
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
    $("dash-supply-hint").hidden = !hasStocks;
    $("dash-disclosure-counts").hidden = !hasStocks;
    if (!hasStocks) {
      $("dash-fetch-short").disabled = true;
      $("dash-short-contact-hint").hidden = true;
      $("dash-fetch-taisyaku").disabled = true;
      $("dash-disclosure-counts").textContent = "";
      $("events-list").innerHTML = "";
      $("events-empty").hidden = true;
      $("events-summary").textContent = "";
      destroyChart();
      return;
    }
    const target = symbol && state.stocks.some((s) => s.symbol === symbol) ? symbol : state.stocks[0].symbol;
    const domestic = target.endsWith(".T");
    // 国内銘柄でも scrape_contact 未設定なら取得できない（不変条件12）。get_settings が失敗しても
    // ダッシュボードの表示自体は止めない（scrapeContactOk が true 側に倒してフォールバックする）
    const contactOk = await scrapeContactOk();
    const shortAvailable = domestic && contactOk;
    $("dash-fetch-short").disabled = !shortAvailable;
    $("dash-fetch-short").title = !domestic
      ? "空売り残高は国内銘柄（.T）のみ取得できます"
      : (contactOk ? "" : "設定タブで karauri.net への連絡先を入力してください");
    $("dash-short-contact-hint").hidden = !(domestic && !contactOk);
    $("dash-fetch-taisyaku").disabled = false;
    const data = await withBusy("読み込み中…", () => api.call("dashboard", target));
    if (!data) {
      // 読み込みに失敗したら、表示中の銘柄に選択を戻して画面と状態を一致させる
      const shown = state.dashboard?.stock.symbol;
      if (shown && state.stocks.some((s) => s.symbol === shown)) $("stock-select").value = shown;
      return;
    }
    state.currentSymbol = target;
    $("stock-select").value = target;
    state.dashboard = data;
    renderDashboard();
  }

  // 開示件数の表示（SPEC §2.4.6）。dashboard() の戻り値にすでに events.counts/fetched_days が
  // 入っているので、そこから同期的に作る（以前は get_disclosures を別途呼んでいた）
  function renderDisclosureCounts(events) {
    const el = $("dash-disclosure-counts");
    const { counts, fetched_days: fetchedDays } = events;
    if (!counts || counts.total === 0) {
      // 「0件」には2通りある。取得済みなのに「まだ取得していません」と出すと、
      // 済んだ取得をもう一度実行させてしまう
      el.textContent = fetchedDays
        ? `この銘柄の開示はまだありません（EDINET は ${fetchedDays} 日分取得済み）。`
        : "まだ取得していません。「登録」タブの「開示を取得」から取得できます。";
      return;
    }
    let text = `開示 ${counts.total}件（有報・半期報 ${counts.report} ／ 需給関連 ${counts.supply} ／ その他 ${counts.other}`;
    if (counts.withdrawn > 0) text += ` ／ 取下げ ${counts.withdrawn}`;
    text += "）";
    el.textContent = text;
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
    renderDisclosureCounts(data.events);
    renderTable(data.table, cur);
    updateSupplyChipAvailability();
    renderChart();
    renderEvents();
  }

  // ---------- 開示イベント欄（P5-4 / P5-5） ----------

  // submit_at は "2026-09-19 15:00" / "2026-09-19 15:00:00" のどちらでも来うる想定。
  // 秒の有無に関わらず "YYYY/MM/DD HH:MM" に整形する（秒は表示しない）
  function formatSubmitAt(raw) {
    if (!raw) return "";
    const [datePart, timePart = ""] = raw.split(" ");
    const dateFmt = datePart.replace(/-/g, "/");
    const hm = timePart.slice(0, 5);
    return hm ? `${dateFmt} ${hm}` : dateFmt;
  }

  // 表示範囲（チャートの visibleRange）でイベント一覧を絞り込む。
  // marker_date が無い（まだ足が無い）行は、提出日がチャート最終日より後なら「表示範囲の右端が
  // 最終足に達しているときだけ」末尾に出す。範囲が取れない（null）ときは全件そのまま返す
  function filterEvents(items, range, lastDate) {
    if (!range) return items.slice();
    const showTail = lastDate != null && range.to >= lastDate;
    return items.filter((item) => {
      const date = item.marker_date ?? item.submit_at.slice(0, 10);
      if (item.marker_date == null && lastDate != null && date > lastDate) {
        return showTail;
      }
      return date >= range.from && date <= range.to;
    });
  }

  function renderEventItem(item, lastDate) {
    const date = item.marker_date ?? item.submit_at.slice(0, 10);
    const withdrawn = item.withdrawal != null && item.withdrawal !== 0;
    const classes = ["event-item"];
    if (withdrawn) classes.push("is-withdrawn");
    const roles = item.roles || [];
    const showFiler = roles.includes("issuer") || roles.includes("subject");
    const descLine = item.description
      ? `<div class="event-desc">${f.escape(item.description)}</div>` : "";
    const metaParts = [];
    if (item.reason) metaParts.push(f.escape(item.reason));
    if (showFiler && item.filer_name) metaParts.push(`提出者: ${f.escape(item.filer_name)}`);
    const metaLine = metaParts.length ? `<div class="event-meta">${metaParts.join(" ／ ")}</div>` : "";
    let noMarkerLine = "";
    if (item.marker_date == null) {
      const submitDate = item.submit_at.slice(0, 10);
      const reasonText = lastDate != null && submitDate > lastDate
        ? "株価の足がまだありません" : "チャートの期間より前です";
      noMarkerLine = `<div class="event-nomarker">${reasonText}</div>`;
    }
    const withdrawnBadge = withdrawn ? `<span class="event-badge is-withdrawn">取下げ</span>` : "";
    return `
      <li class="${classes.join(" ")}" data-date="${f.escape(date)}"
          data-marker-date="${item.marker_date ? f.escape(item.marker_date) : ""}">
        <div class="event-row-1">
          <span class="event-time">${f.escape(formatSubmitAt(item.submit_at))}</span>
          <span class="event-badge category-${f.escape(item.category)}">${f.escape(item.label)}</span>
          ${withdrawnBadge}
          <button class="btn btn-sm event-open" data-doc-id="${f.escape(item.doc_id)}">開く</button>
        </div>
        ${descLine}
        ${metaLine}
        ${noMarkerLine}
      </li>`;
  }

  // state.dashboard.events と state.visibleRange だけから描画する（state.chart には依存しない。
  // renderChart() のたびに state.chart は作り直されるが、この関数はその影響を受けない）
  function renderEvents() {
    const events = state.dashboard?.events;
    const listEl = $("events-list");
    const emptyEl = $("events-empty");
    const summaryEl = $("events-summary");
    if (!events) {
      listEl.innerHTML = "";
      emptyEl.hidden = true;
      summaryEl.textContent = "";
      return;
    }
    const { items, counts, fetched_days: fetchedDays } = events;
    const dates = state.dashboard?.chart?.dates;
    const lastDate = dates && dates.length ? dates[dates.length - 1] : null;
    const filtered = filterEvents(items, state.visibleRange, lastDate);

    summaryEl.textContent = `表示範囲 ${filtered.length}件 ／ 全 ${counts.total}件`;

    if (counts.total === 0) {
      // 「0件」には2通りある（dash-disclosure-counts と同じ考え方）
      listEl.innerHTML = "";
      emptyEl.hidden = false;
      emptyEl.textContent = fetchedDays
        ? `この銘柄の開示はまだありません（EDINET は ${fetchedDays} 日分取得済み）。`
        : "まだ取得していません。「登録」タブの「開示を取得」から取得できます。";
      return;
    }
    if (filtered.length === 0) {
      listEl.innerHTML = "";
      emptyEl.hidden = false;
      emptyEl.textContent = "この表示範囲に開示はありません";
      return;
    }
    emptyEl.hidden = true;
    listEl.innerHTML = filtered.map((item) => renderEventItem(item, lastDate)).join("");
  }

  // イベント欄の行クリック（委譲。#events-list に一度だけ登録する）
  // 「開く」ボタンは EDINET を直接開くのではなく、その1件だけのモーダルを開く（P9-3）。
  // EDINET を開くのはモーダル内のボタンに移した
  function onEventsListClick(e) {
    const openBtn = e.target.closest(".event-open");
    if (openBtn) {
      e.stopPropagation();
      const item = (state.dashboard?.events?.items || []).find((i) => i.doc_id === openBtn.dataset.docId);
      if (item) openDisclosureModal([item]);
      return;
    }
    const row = e.target.closest(".event-item");
    if (!row) return;
    const markerDate = row.dataset.markerDate;
    if (markerDate) state.chart?.scrollToDate(markerDate);
  }

  // マーカークリック（P5-5）: 該当行（複数あり得る）を強調してスクロールする。
  // 表示範囲の絞り込みで出ていない場合は何もしない
  function highlightEventRow(id) {
    const date = id.slice(3);
    const rows = document.querySelectorAll(`#events-list .event-item[data-date="${CSS.escape(date)}"]`);
    if (!rows.length) return;
    rows[0].scrollIntoView({ block: "nearest" });
    rows.forEach((row) => {
      row.classList.add("is-marker-highlight");
      setTimeout(() => row.classList.remove("is-marker-highlight"), 2000);
    });
  }

  // ---------- 開示モーダル（P9-3） ----------
  // docId -> { loading: bool, result, error } のキャッシュ。「同じ書類の本文を2回読み込まない」（SPEC §2.4.8）ため、
  // モーダルを開くたびに作り直し、閉じたら破棄する（プロセス内キャッシュは Python 側 get_disclosure_text が別途持つ）
  let modalDocCache = new Map();

  function modalDocBlockHtml(item) {
    const withdrawn = item.withdrawal != null && item.withdrawal !== 0;
    const roles = item.roles || [];
    const showFiler = roles.includes("issuer") || roles.includes("subject");
    const descLine = item.description ? `<div class="event-desc">${f.escape(item.description)}</div>` : "";
    const metaParts = [];
    if (item.reason) metaParts.push(f.escape(item.reason));
    if (showFiler && item.filer_name) metaParts.push(`提出者: ${f.escape(item.filer_name)}`);
    const metaLine = metaParts.length ? `<div class="event-meta">${metaParts.join(" ／ ")}</div>` : "";
    const withdrawnBadge = withdrawn ? `<span class="event-badge is-withdrawn">取下げ</span>` : "";
    return `
      <div class="modal-doc" data-doc-id="${f.escape(item.doc_id)}">
        <div class="event-row-1">
          <span class="event-time">${f.escape(formatSubmitAt(item.submit_at))}</span>
          <span class="event-badge category-${f.escape(item.category)}">${f.escape(item.label)}</span>
          ${withdrawnBadge}
        </div>
        ${descLine}
        ${metaLine}
        <div class="modal-doc-actions">
          <button class="btn btn-sm modal-doc-text-btn" data-doc-id="${f.escape(item.doc_id)}">本文を表示</button>
          <button class="btn btn-sm modal-doc-open-btn" data-doc-id="${f.escape(item.doc_id)}">EDINET で開く</button>
        </div>
        <div class="modal-doc-result" data-doc-id="${f.escape(item.doc_id)}"></div>
      </div>`;
  }

  // 本文表示エリア（.modal-doc-result）の中身を、キャッシュの状態に応じて描画する
  function renderModalDocResult(docId) {
    const el = document.querySelector(`.modal-doc-result[data-doc-id="${CSS.escape(docId)}"]`);
    if (!el) return;
    const entry = modalDocCache.get(docId);
    if (!entry) { el.innerHTML = ""; return; }
    if (entry.loading) {
      el.innerHTML = `<div class="modal-doc-status">読み込み中…</div>`;
      return;
    }
    if (entry.error) {
      el.innerHTML = `<div class="modal-doc-status">${f.escape(entry.error)}</div>`;
      return;
    }
    const res = entry.result;
    if (!res.available) {
      el.innerHTML = `<div class="modal-doc-reason">${f.escape(res.reason || "本文を表示できません。")}（「EDINET で開く」からご確認ください）</div>`;
      return;
    }
    const truncatedNote = res.truncated
      ? `<div class="modal-doc-truncated">（長いため途中までを表示しています。全文は EDINET で確認してください）</div>` : "";
    el.innerHTML = `<div class="modal-doc-text">${f.escape(res.text)}</div>${truncatedNote}`;
  }

  // 本文の取得。すでにキャッシュにあれば何もしない（読み込み中も含めて二重取得を防ぐ）
  async function loadModalDocText(docId) {
    const existing = modalDocCache.get(docId);
    if (existing && (existing.loading || existing.result)) return;
    modalDocCache.set(docId, { loading: true });
    renderModalDocResult(docId);
    try {
      const res = await api.call("get_disclosure_text", docId);
      modalDocCache.set(docId, { result: res });
    } catch (err) {
      // キーの誤り・レート制限などは例外になる。本文欄には出さずトースト、欄自体は空に戻す
      modalDocCache.delete(docId);
      renderModalDocResult(docId);
      toast(err.message, "error");
      return;
    }
    renderModalDocResult(docId);
  }

  // マーカークリック（複数件）／イベント欄の「開く」（1件）の両方から呼ぶ。
  // 1件だけのときはボタンを押す手間を省くため自動で本文を読み込む（SPEC §2.4.8・ユーザー要望）
  function openDisclosureModal(items, headingDate) {
    if (!items.length) return;
    modalDocCache = new Map();
    const dialog = $("disclosure-modal");
    const date = headingDate ?? (items[0].marker_date ?? items[0].submit_at.slice(0, 10));
    $("modal-title").textContent = `${f.date(date)}の開示 ${items.length}件`;
    $("modal-body").innerHTML = items.map(modalDocBlockHtml).join("")
      + `<p class="hint modal-footnote">出典：EDINET（金融庁）。本ツールが加工して表示しています</p>`;
    if (!dialog.open) dialog.showModal();
    if (items.length === 1) loadModalDocText(items[0].doc_id);
  }

  // モーダル内のクリック（委譲）。本文表示・EDINET を開く・閉じるボタンをここでまとめて扱う
  function onModalBodyClick(e) {
    const textBtn = e.target.closest(".modal-doc-text-btn");
    if (textBtn) {
      loadModalDocText(textBtn.dataset.docId);
      return;
    }
    const openBtn = e.target.closest(".modal-doc-open-btn");
    if (openBtn) {
      api.call("open_disclosure", openBtn.dataset.docId).catch((err) => toast(err.message, "error"));
    }
  }

  // 背景（::backdrop）クリックで閉じる。<dialog> はクリック位置に関わらず自身の click イベントが飛んでくるので、
  // クリック座標が dialog 自身の矩形の外（＝backdrop）にあるかどうかで判定する
  function onModalBackdropClick(e) {
    if (e.target !== e.currentTarget) return; // 内部の要素のクリックは無視
    const rect = e.currentTarget.getBoundingClientRect();
    const inside = e.clientX >= rect.left && e.clientX <= rect.right && e.clientY >= rect.top && e.clientY <= rect.bottom;
    if (!inside) e.currentTarget.close();
  }

  // 需給ペインのチップは、表示中の銘柄にデータが1件も無ければ無効化し理由を表示する。
  // ON/OFF の状態（localStorage）自体は変えない。空のペインを作らないのは chart.js 側の仕事（P3-4）
  function updateSupplyChipAvailability() {
    const chart = state.dashboard?.chart;
    const apply = (id, info) => {
      const btn = document.querySelector(`#pane-chips .chip[data-id="${id}"]`);
      if (!btn) return;
      const available = Boolean(info?.available);
      btn.disabled = !available;
      btn.classList.toggle("is-unavailable", !available);
      btn.title = available ? "" : (info?.reason || "");
    };
    apply("short", chart?.short);
    apply("taisyaku", chart?.taisyaku);
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
    // kind はサーバー側で付与（price: 株価と同じ単位 / volume / date / その他は小数2桁）
    const format = (col, v) => {
      if (col.kind === "date") return f.date(v);
      if (v === null || v === undefined) return "—";
      if (col.kind === "volume") return f.num(v);
      if (col.kind === "price") return f.price(v, currency);
      return f.num(v, 2);
    };
    $("data-table").innerHTML = `
      <thead><tr>${table.columns.map((c) => `<th>${f.escape(c.label)}</th>`).join("")}</tr></thead>
      <tbody>${table.rows.map((r) => `<tr>${table.columns.map((c) => `<td>${format(c, r[c.key])}</td>`).join("")}</tr>`).join("")}</tbody>`;
  }

  function destroyChart() {
    if (state.chart) {
      state.chart.destroy();
      state.chart = null;
    }
  }

  // preserveRange: true のときは表示範囲（visibleLogicalRange）を引き継ぐ（チップ切替からの再生成用）。
  // false（既定）のときは銘柄切替・初回表示どおり state.range（期間ボタン）で設定し直す
  function renderChart({ preserveRange = false } = {}) {
    if (!state.dashboard) return;
    const prevRange = preserveRange ? state.chart?.visibleLogicalRange() : null;
    destroyChart();
    state.chart = StockChart.render(
      $("chart"), $("pane-labels"), $("chart-legend"),
      state.dashboard, state.dashboard.stock.currency, state.chartSettings,
    );
    // 購読はチャートの再生成のたびに閉じてしまうので、生成のたびに張り直す
    state.chart.onVisibleRangeChange(onChartRangeChange);
    state.chart.onMarkerClick(onMarkerClick);
    const restored = prevRange && typeof prevRange.from === "number" && typeof prevRange.to === "number";
    if (restored) state.chart.setVisibleLogicalRange(prevRange);
    else state.chart.setRange(state.range);
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
        renderChart({ preserveRange: true });
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
    $("events-list").addEventListener("click", onEventsListClick);
    $("modal-body").addEventListener("click", onModalBodyClick);
    $("modal-close").addEventListener("click", () => $("disclosure-modal").close());
    $("disclosure-modal").addEventListener("click", onModalBackdropClick);
    $("disclosure-modal").addEventListener("close", () => { modalDocCache = new Map(); });
    $("update-all").addEventListener("click", updateAll);
    $("short-all").addEventListener("click", runShortAll);
    $("disclosures-fetch").addEventListener("click", () => runDisclosures(0));
    $("disclosures-redo").addEventListener("click", () => runDisclosures(90));
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
    $("dash-fetch-short").addEventListener("click", async () => {
      const symbol = state.currentSymbol;
      if (!symbol) return;
      const res = await withBusy(`${symbol} の空売り残高を取得しています…`, () => api.call("fetch_short", symbol));
      if (!res) return;
      if (res.status === "skipped") {
        toast("空売り残高は国内銘柄（.T）のみ取得できます", "warn");
      } else {
        toast(`空売り残高を取得しました（${res.rows}行）`);
      }
      await openDashboard(symbol);
    });
    $("dash-fetch-taisyaku").addEventListener("click", async () => {
      const res = await withBusy("貸借取引残高（日証金）を取得しています…", () => api.call("fetch_taisyaku"));
      if (!res) return;
      if (res.date) {
        toast(`貸借取引残高を取得しました（申込日 ${f.date(res.date)}・${res.saved}銘柄）`);
      } else {
        toast("貸借取引残高を取得しました（対象銘柄の行はありませんでした）", "warn");
      }
      await refreshStocks();
      if (state.currentSymbol) await openDashboard(state.currentSymbol);
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

  // 起動時の自動更新（SPEC §2.8.2）。バックグラウンドで走り、失敗しても操作を妨げない。
  // 「起動時の1回だけ」は Python 側が保証するので、画面の再読込で呼んでも二重には走らない
  async function autoUpdate() {
    if (!state.stocks.length || Jobs.isRunning("auto_update")) return;
    let job;
    try {
      job = await Jobs.run("auto_update");
    } catch (err) {
      toast(`自動更新に失敗しました: ${err.message}`, "warn", 6000);
      return;
    }
    if (job.state === "cancelled") {
      toast("自動更新を中断しました");
    } else if (!job.result || job.result.skipped) {
      return;
    } else if (!job.result.changed && !job.result.failed) {
      return; // すべて取得済みで何もしなかったときは知らせない
    } else {
      toast(`自動更新: ${job.result.summary}`, job.result.failed ? "warn" : "info", 6000);
    }
    await refreshStocks();
    const updated = job.result?.updated_symbols || [];
    const onDashboard = $("view-dashboard").classList.contains("is-active");
    if (onDashboard && updated.includes(state.currentSymbol)) await openDashboard(state.currentSymbol);
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
    await Jobs.resume().catch(() => {});
    autoUpdate();
  }

  init();
})();
