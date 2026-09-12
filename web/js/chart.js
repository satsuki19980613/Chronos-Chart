// ローソク足チャートとテクニカル指標の描画（TradingView Lightweight Charts v5）
window.StockChart = (function () {
  const LWC = window.LightweightCharts;

  const COLORS = {
    up: "#ff5c6c",
    down: "#3ea6ff",
    text: "#8b97ad",
    grid: "#161e2e",
    border: "#243049",
    bg: "#121826",
  };

  // メインチャートに重ねる指標
  const OVERLAYS = [
    { id: "sma5", label: "SMA5", color: "#f5b83d", lines: [["sma_5", "SMA5"]] },
    { id: "sma25", label: "SMA25", color: "#4ade80", lines: [["sma_25", "SMA25"]] },
    { id: "sma75", label: "SMA75", color: "#c084fc", lines: [["sma_75", "SMA75"]] },
    { id: "ema12", label: "EMA12", color: "#fb923c", lines: [["ema_12", "EMA12"]] },
    { id: "ema26", label: "EMA26", color: "#22d3ee", lines: [["ema_26", "EMA26"]] },
    { id: "bb", label: "ボリンジャー", color: "#94a3b8" },
    { id: "ichimoku", label: "一目均衡表", color: "#f472b6" },
    { id: "signals", label: "シグナル", color: "#e6ebf3" },
  ];

  // 下段ペインに表示する指標
  const PANES = [
    { id: "volume", label: "出来高", color: "#8b97ad" },
    { id: "macd", label: "MACD (12,26,9)", color: "#f5b83d" },
    { id: "rsi", label: "RSI (14)", color: "#c084fc" },
    { id: "stoch", label: "ストキャスティクス (14,3,3)", color: "#4ade80" },
    { id: "dmi", label: "DMI / ADX (14)", color: "#fb923c" },
    { id: "atr", label: "ATR (14)", color: "#22d3ee" },
    { id: "deviation", label: "乖離率 (25)", color: "#f472b6" },
    { id: "psy", label: "サイコロジカル (12)", color: "#94a3b8" },
  ];

  const DEFAULTS = {
    overlays: ["sma5", "sma25", "sma75", "signals"],
    panes: ["volume", "macd", "rsi"],
  };

  // 2本の線の間を塗りつぶすシリーズプリミティブ（一目の雲・ボリンジャーバンド用）
  class BandFill {
    constructor(points, upColor, downColor) {
      this.points = points; // [{time, a, b}]
      this.upColor = upColor;
      this.downColor = downColor;
      const source = this;
      this._views = [{
        zOrder: () => "bottom",
        renderer: () => ({ draw: (target) => source._draw(target) }),
      }];
    }
    attached({ chart, series }) { this.chart = chart; this.series = series; }
    detached() { this.chart = null; this.series = null; }
    updateAllViews() {}
    paneViews() { return this._views; }

    _draw(target) {
      if (!this.chart) return;
      const ts = this.chart.timeScale();
      const pts = [];
      for (const p of this.points) {
        const x = ts.timeToCoordinate(p.time);
        const ya = this.series.priceToCoordinate(p.a);
        const yb = this.series.priceToCoordinate(p.b);
        if (x === null || ya === null || yb === null) continue;
        pts.push({ x, ya, yb, up: p.a >= p.b });
      }
      target.useMediaCoordinateSpace(({ context: ctx }) => {
        let run = [];
        let runUp = null;
        const flush = () => {
          if (run.length >= 2) {
            ctx.beginPath();
            run.forEach((p, i) => (i ? ctx.lineTo(p.x, p.ya) : ctx.moveTo(p.x, p.ya)));
            for (let i = run.length - 1; i >= 0; i--) ctx.lineTo(run[i].x, run[i].yb);
            ctx.closePath();
            ctx.fillStyle = runUp ? this.upColor : this.downColor;
            ctx.fill();
          }
        };
        for (const p of pts) {
          if (runUp !== null && p.up !== runUp) {
            const last = run[run.length - 1];
            flush();
            run = [last];
          }
          runUp = p.up;
          run.push(p);
        }
        flush();
      });
    }
  }

  function toSeries(dates, values) {
    const out = [];
    for (let i = 0; i < dates.length; i++) {
      if (values[i] !== null && values[i] !== undefined) out.push({ time: dates[i], value: values[i] });
    }
    return out;
  }

  function render(container, labelsEl, legendEl, data, currency, state) {
    const d = data.chart;
    const ind = d.indicators;
    const dates = d.dates;
    const digits = window.fmt.priceDigits(currency);
    const priceFormat = { type: "price", precision: digits, minMove: 1 / 10 ** digits };
    const overlays = new Set(state.overlays);
    const panes = PANES.filter((p) => state.panes.includes(p.id));

    container.style.height = `${400 + panes.length * 125}px`;

    const chart = LWC.createChart(container, {
      autoSize: true,
      layout: {
        background: { type: "solid", color: COLORS.bg },
        textColor: COLORS.text,
        fontFamily: "Segoe UI, Yu Gothic UI, Meiryo, sans-serif",
        fontSize: 11,
        panes: { separatorColor: COLORS.border, separatorHoverColor: "#33415f", enableResize: false },
      },
      grid: { vertLines: { color: COLORS.grid }, horzLines: { color: COLORS.grid } },
      crosshair: { mode: LWC.CrosshairMode.Normal },
      rightPriceScale: { borderColor: COLORS.border },
      timeScale: { borderColor: COLORS.border, rightOffset: 4, minBarSpacing: 1 },
      localization: { locale: "ja-JP", dateFormat: "yyyy/MM/dd" },
    });

    const legendItems = []; // [label, color, values[]]
    const line = (values, color, title, pane, opts = {}) => {
      const s = chart.addSeries(LWC.LineSeries, {
        color, lineWidth: 1.5, priceLineVisible: false, lastValueVisible: true,
        crosshairMarkerVisible: false, title: "", ...opts,
      }, pane);
      s.setData(toSeries(dates, values));
      if (title && pane === 0) legendItems.push([title, color, values]);
      return s;
    };
    const guide = (series, price, color = "#5d6880") =>
      series.createPriceLine({ price, color, lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: false });

    // ---------- メイン: ローソク足 ----------
    const candle = chart.addSeries(LWC.CandlestickSeries, {
      upColor: COLORS.up, downColor: COLORS.down,
      borderUpColor: COLORS.up, borderDownColor: COLORS.down,
      wickUpColor: COLORS.up, wickDownColor: COLORS.down,
      priceFormat,
    }, 0);
    const candles = dates.map((time, i) => ({ time, open: d.open[i], high: d.high[i], low: d.low[i], close: d.close[i] }));
    const future = overlays.has("ichimoku") ? d.future_cloud : [];
    candle.setData([...candles, ...future.map((c) => ({ time: c.date }))]);

    for (const o of OVERLAYS) {
      if (o.lines && overlays.has(o.id)) {
        for (const [key, title] of o.lines) line(ind[key], o.color, title, 0, { priceFormat });
      }
    }

    if (overlays.has("bb")) {
      const c = "#94a3b8";
      line(ind.bb_mid, c, "BB中心", 0, { lineStyle: LWC.LineStyle.Dotted, lastValueVisible: false, priceFormat });
      line(ind.bb_upper_2, c, "+2σ", 0, { lineWidth: 1, lastValueVisible: false, priceFormat });
      line(ind.bb_lower_2, c, "-2σ", 0, { lineWidth: 1, lastValueVisible: false, priceFormat });
      line(ind.bb_upper_1, "#5d6880", "", 0, { lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, lastValueVisible: false, priceFormat });
      line(ind.bb_lower_1, "#5d6880", "", 0, { lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, lastValueVisible: false, priceFormat });
      const band = dates
        .map((time, i) => ({ time, a: ind.bb_upper_2[i], b: ind.bb_lower_2[i] }))
        .filter((p) => p.a !== null && p.b !== null);
      candle.attachPrimitive(new BandFill(band, "rgba(148,163,184,0.07)", "rgba(148,163,184,0.07)"));
    }

    if (overlays.has("ichimoku")) {
      const allDates = [...dates, ...future.map((c) => c.date)];
      const spanA = [...ind.ichimoku_senkou_a, ...future.map((c) => c.senkou_a)];
      const spanB = [...ind.ichimoku_senkou_b, ...future.map((c) => c.senkou_b)];
      line(ind.ichimoku_tenkan, "#f472b6", "転換線", 0, { lineWidth: 1, priceFormat });
      line(ind.ichimoku_kijun, "#38bdf8", "基準線", 0, { lineWidth: 1, priceFormat });
      line(ind.ichimoku_chikou, "#a3e635", "遅行", 0, { lineWidth: 1, lastValueVisible: false, priceFormat });
      const a = chart.addSeries(LWC.LineSeries, { color: "rgba(255,92,108,0.55)", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, priceFormat }, 0);
      a.setData(toSeries(allDates, spanA));
      const b = chart.addSeries(LWC.LineSeries, { color: "rgba(62,166,255,0.55)", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, priceFormat }, 0);
      b.setData(toSeries(allDates, spanB));
      legendItems.push(["先行A", "#ff8a95", spanA], ["先行B", "#6cbcff", spanB]);
      const cloud = allDates
        .map((time, i) => ({ time, a: spanA[i], b: spanB[i] }))
        .filter((p) => p.a !== null && p.b !== null);
      candle.attachPrimitive(new BandFill(cloud, "rgba(255,92,108,0.13)", "rgba(62,166,255,0.13)"));
    }

    if (overlays.has("signals")) {
      // ゴールデン/デッドクロスは矢印+文字、その他は小さな丸にして混み合いを抑える
      const markers = d.signals.map((s) => {
        const buy = s.direction === "buy";
        const cross = s.short === "GC" || s.short === "DC";
        return {
          time: s.date,
          position: buy ? "belowBar" : "aboveBar",
          shape: cross ? (buy ? "arrowUp" : "arrowDown") : "circle",
          color: buy ? COLORS.up : COLORS.down,
          text: cross ? s.short : "",
          size: cross ? 1 : 0.5,
        };
      });
      LWC.createSeriesMarkers(candle, markers);
    }

    // ---------- 下段ペイン ----------
    const paneLabels = [];
    panes.forEach((p, idx) => {
      const pane = idx + 1;
      paneLabels.push(p.label);
      switch (p.id) {
        case "volume": {
          const vol = chart.addSeries(LWC.HistogramSeries, { priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false }, pane);
          vol.setData(dates.map((time, i) => ({
            time, value: d.volume[i],
            color: d.close[i] >= d.open[i] ? "rgba(255,92,108,0.55)" : "rgba(62,166,255,0.55)",
          })));
          line(ind.volume_ma_5, "#f5b83d", "", pane, { lineWidth: 1, lastValueVisible: false, priceFormat: { type: "volume" } });
          line(ind.volume_ma_25, "#4ade80", "", pane, { lineWidth: 1, lastValueVisible: false, priceFormat: { type: "volume" } });
          break;
        }
        case "macd": {
          const hist = chart.addSeries(LWC.HistogramSeries, { priceLineVisible: false, lastValueVisible: false, priceFormat: { type: "price", precision: 2, minMove: 0.01 } }, pane);
          hist.setData(dates.map((time, i) => (ind.macd_hist[i] === null ? { time } : {
            time, value: ind.macd_hist[i],
            color: ind.macd_hist[i] >= 0 ? "rgba(255,92,108,0.5)" : "rgba(62,166,255,0.5)",
          })));
          line(ind.macd, "#f5b83d", "", pane, { priceFormat: { type: "price", precision: 2, minMove: 0.01 } });
          line(ind.macd_signal, "#c084fc", "", pane, { lineWidth: 1, priceFormat: { type: "price", precision: 2, minMove: 0.01 } });
          break;
        }
        case "rsi": {
          const s = line(ind.rsi_14, "#c084fc", "", pane, { priceFormat: { type: "price", precision: 1, minMove: 0.1 } });
          guide(s, 70); guide(s, 30);
          break;
        }
        case "stoch": {
          const pf = { type: "price", precision: 1, minMove: 0.1 };
          const s = line(ind.stoch_k, "#4ade80", "", pane, { lineWidth: 1, priceFormat: pf });
          line(ind.stoch_d, "#f5b83d", "", pane, { lineWidth: 1, priceFormat: pf });
          line(ind.stoch_slow_d, "#c084fc", "", pane, { lineWidth: 1, priceFormat: pf });
          guide(s, 80); guide(s, 20);
          break;
        }
        case "dmi": {
          const pf = { type: "price", precision: 1, minMove: 0.1 };
          line(ind.plus_di, COLORS.up, "", pane, { lineWidth: 1, priceFormat: pf });
          line(ind.minus_di, COLORS.down, "", pane, { lineWidth: 1, priceFormat: pf });
          const s = line(ind.adx, "#f5b83d", "", pane, { priceFormat: pf });
          guide(s, 25);
          break;
        }
        case "atr":
          line(ind.atr_14, "#22d3ee", "", pane, { priceFormat });
          break;
        case "deviation": {
          const s = chart.addSeries(LWC.HistogramSeries, { priceLineVisible: false, priceFormat: { type: "price", precision: 2, minMove: 0.01 } }, pane);
          s.setData(dates.map((time, i) => (ind.deviation_25[i] === null ? { time } : {
            time, value: ind.deviation_25[i],
            color: ind.deviation_25[i] >= 0 ? "rgba(255,92,108,0.6)" : "rgba(62,166,255,0.6)",
          })));
          break;
        }
        case "psy": {
          const s = line(ind.psychological_12, "#94a3b8", "", pane, { priceFormat: { type: "price", precision: 1, minMove: 0.1 } });
          guide(s, 75); guide(s, 25);
          break;
        }
      }
    });

    const allPanes = chart.panes();
    allPanes[0].setStretchFactor(3.2);
    for (let i = 1; i < allPanes.length; i++) allPanes[i].setStretchFactor(1);

    // ---------- ペイン名 ----------
    function placePaneLabels() {
      const heights = chart.panes().map((p) => p.getHeight());
      let top = 0;
      labelsEl.innerHTML = heights.map((h, i) => {
        const html = i === 0 ? "" : `<span class="pane-label" style="top:${top + 4}px">${window.fmt.escape(paneLabels[i - 1])}</span>`;
        top += h + 1;
        return html;
      }).join("");
    }

    // ---------- 凡例（クロスヘア位置の値）----------
    const indexByDate = new Map(dates.map((t, i) => [t, i]));
    const futureIndex = new Map(future.map((c, i) => [c.date, dates.length + i]));
    function updateLegend(time) {
      const i = indexByDate.has(time) ? indexByDate.get(time) : (futureIndex.get(time) ?? dates.length - 1);
      const f = window.fmt;
      const parts = [];
      if (i < dates.length) {
        const prev = i > 0 ? d.close[i - 1] : null;
        const chg = prev ? d.close[i] - prev : null;
        const cls = chg > 0 ? "is-up" : chg < 0 ? "is-down" : "";
        parts.push(
          `<span class="lg-date">${f.date(dates[i])}</span>`,
          `<span>始 <b>${f.price(d.open[i], currency)}</b></span>`,
          `<span>高 <b>${f.price(d.high[i], currency)}</b></span>`,
          `<span>安 <b>${f.price(d.low[i], currency)}</b></span>`,
          `<span>終 <b class="${cls}">${f.price(d.close[i], currency)}</b></span>`,
          `<span class="${cls}">${chg === null ? "" : f.signed(chg, digits) + ` (${f.signed(chg / prev * 100, 2, "%")})`}</span>`,
        );
      } else {
        parts.push(`<span class="lg-date">${f.date(future[i - dates.length].date)}（先行）</span>`);
      }
      for (const [label, color, values] of legendItems) {
        if (!label) continue;
        parts.push(`<span style="color:${color}">${label} <b>${f.price(values[i] ?? null, currency)}</b></span>`);
      }
      legendEl.innerHTML = parts.join("");
    }

    chart.subscribeCrosshairMove((param) => {
      updateLegend(param.time ?? null);
    });
    updateLegend(null);

    const ro = new ResizeObserver(() => requestAnimationFrame(placePaneLabels));
    ro.observe(container);
    requestAnimationFrame(placePaneLabels);

    function setRange(range) {
      const total = dates.length;
      const extra = future.length;
      if (range === "all") {
        chart.timeScale().fitContent();
      } else {
        const bars = Number(range);
        chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, total - bars), to: total + extra + 2 });
      }
    }

    return {
      setRange,
      destroy() {
        ro.disconnect();
        chart.remove();
        labelsEl.innerHTML = "";
        legendEl.innerHTML = "";
      },
    };
  }

  return { OVERLAYS, PANES, DEFAULTS, render };
})();
