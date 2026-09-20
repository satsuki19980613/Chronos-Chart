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
    guide: "#5d6880",
  };

  // 開示マーカーの見た目（SPEC §2.5.3）。売買シグナルの色（COLORS.up/down）と紛れないよう別系統の色にする
  const EVENT_COLORS = { supply: "#fb923c", report: "#7dd3fc", other: "#8b97ad" };
  const EVENT_SHAPES = { supply: "arrowUp", report: "circle", other: "square" };
  const EVENT_POSITIONS = { supply: "belowBar", report: "aboveBar", other: "aboveBar" };

  // メインチャートに重ねる指標（チップの色は代表色）
  const OVERLAYS = [
    { id: "sma", label: "移動平均", color: "#f5b83d" },
    { id: "ema", label: "指数平滑移動平均", color: "#fb923c" },
    { id: "bb", label: "ボリンジャー", color: "#94a3b8" },
    { id: "ichimoku", label: "一目均衡表", color: "#f472b6" },
    { id: "gmma", label: "多重移動平均", color: "#34d399" },
    { id: "parabolic", label: "パラボリック", color: "#facc15" },
    { id: "signals", label: "シグナル", color: "#e6ebf3" },
    { id: "disclosures", label: "開示", color: "#7dd3fc" },
  ];

  // 下段ペインに表示する指標
  const PANES = [
    { id: "volume", label: "出来高", color: "#8b97ad" },
    { id: "macd", label: "MACD", color: "#f5b83d" },
    { id: "rsi", label: "RSI", color: "#c084fc" },
    { id: "rci", label: "RCI", color: "#38bdf8" },
    { id: "dmi", label: "DMI/ADX", color: "#fb923c" },
    { id: "stoch", label: "ストキャス", color: "#4ade80" },
    { id: "deviation", label: "乖離率", color: "#f472b6" },
    { id: "psy", label: "サイコロジカル", color: "#94a3b8" },
    { id: "stddev", label: "標準偏差", color: "#22d3ee" },
    { id: "momentum", label: "モメンタム", color: "#a3e635" },
    { id: "short", label: "空売り残高", color: "#fb7185" },
    { id: "taisyaku", label: "貸借取引残高（日証金）", color: "#2dd4bf" },
  ];

  const DEFAULTS = {
    overlays: ["sma", "signals", "disclosures"],
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
          if (run.length < 2) return;
          ctx.beginPath();
          run.forEach((p, i) => (i ? ctx.lineTo(p.x, p.ya) : ctx.moveTo(p.x, p.ya)));
          for (let i = run.length - 1; i >= 0; i--) ctx.lineTo(run[i].x, run[i].yb);
          ctx.closePath();
          ctx.fillStyle = runUp ? this.upColor : this.downColor;
          ctx.fill();
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

  // 値の正負で色分けしたヒストグラム用データ
  function toHistogram(dates, values, alpha = 0.55) {
    return dates.map((time, i) => (values[i] === null ? { time } : {
      time,
      value: values[i],
      color: values[i] >= 0 ? `rgba(255,92,108,${alpha})` : `rgba(62,166,255,${alpha})`,
    }));
  }

  function render(container, labelsEl, legendEl, data, currency, state) {
    const d = data.chart;
    const ind = d.indicators;
    const P = d.params;
    const dates = d.dates;
    const digits = window.fmt.priceDigits(currency);
    const priceFormat = { type: "price", precision: digits, minMove: 1 / 10 ** digits };
    const pf1 = { type: "price", precision: 1, minMove: 0.1 };
    const pf2 = { type: "price", precision: 2, minMove: 0.01 };
    const overlays = new Set(state.overlays);
    // 需給ペイン（short/taisyaku）は値が1件も無い銘柄では描画対象から外す。
    // チップが ON のまま銘柄を切り替えても空のペインを作らないため（SPEC §2.5.2・P3-4）
    const paneDataAvailable = { short: d.short?.available, taisyaku: d.taisyaku?.available };
    const panes = PANES.filter((p) => state.panes.includes(p.id) && paneDataAvailable[p.id] !== false);

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

    const legendItems = []; // [表示名, 色, 値配列, 値フォーマッタ(省略時は価格表示)] メインチャートの凡例
    const line = (values, color, pane, opts = {}, legend = null, legendFormat = null) => {
      const { dates: seriesDates = dates, ...options } = opts;
      const s = chart.addSeries(LWC.LineSeries, {
        color, lineWidth: 1.5, priceLineVisible: false, lastValueVisible: true,
        crosshairMarkerVisible: false, ...options,
      }, pane);
      s.setData(toSeries(seriesDates, values));
      if (legend) legendItems.push([legend, color, values, legendFormat]);
      return s;
    };
    // 描画用のシリーズを増やさずに凡例だけ登録する（貸借取引残高の区間分割シリーズ用。§P3-3）
    const legendOnly = (label, color, values, legendFormat = null) => {
      legendItems.push([label, color, values, legendFormat]);
    };
    const histogram = (values, pane, opts = {}) => {
      const s = chart.addSeries(LWC.HistogramSeries, { priceLineVisible: false, lastValueVisible: false, ...opts }, pane);
      s.setData(toHistogram(dates, values));
      return s;
    };
    const guide = (series, price) =>
      series.createPriceLine({ price, color: COLORS.guide, lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: false });

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

    const main = { priceFormat };

    if (overlays.has("sma")) {
      line(ind.sma_short, "#f5b83d", 0, main, `MA${P.sma.short}`);
      line(ind.sma_mid, "#4ade80", 0, main, `MA${P.sma.mid}`);
      line(ind.sma_long, "#c084fc", 0, main, `MA${P.sma.long}`);
    }

    if (overlays.has("ema")) {
      const dashed = { ...main, lineStyle: LWC.LineStyle.Dashed };
      line(ind.ema_short, "#fb923c", 0, dashed, `EMA${P.ema.short}`);
      line(ind.ema_mid, "#22d3ee", 0, dashed, `EMA${P.ema.mid}`);
      line(ind.ema_long, "#e879f9", 0, dashed, `EMA${P.ema.long}`);
    }

    if (overlays.has("bb")) {
      const c = "#94a3b8";
      const thin = { ...main, lineWidth: 1, lastValueVisible: false };
      line(ind.bb_upper, c, 0, thin, `+${P.bb.sigma}σ`);
      line(ind.bb_mid, c, 0, { ...thin, lineStyle: LWC.LineStyle.Dotted }, `BB中心(${P.bb.period})`);
      line(ind.bb_lower, c, 0, thin, `-${P.bb.sigma}σ`);
      const band = dates
        .map((time, i) => ({ time, a: ind.bb_upper[i], b: ind.bb_lower[i] }))
        .filter((p) => p.a !== null && p.b !== null);
      candle.attachPrimitive(new BandFill(band, "rgba(148,163,184,0.07)", "rgba(148,163,184,0.07)"));
    }

    if (overlays.has("ichimoku")) {
      const allDates = [...dates, ...future.map((c) => c.date)];
      const span1 = [...ind.ichimoku_senkou1, ...future.map((c) => c.senkou1)];
      const span2 = [...ind.ichimoku_senkou2, ...future.map((c) => c.senkou2)];
      const thin = { ...main, lineWidth: 1 };
      line(ind.ichimoku_kijun, "#38bdf8", 0, thin, "基準線");
      line(ind.ichimoku_tenkan, "#f472b6", 0, thin, "転換線");
      line(span1, "rgba(255,92,108,0.6)", 0, { ...thin, lastValueVisible: false, dates: allDates }, "先行線1");
      line(span2, "rgba(62,166,255,0.6)", 0, { ...thin, lastValueVisible: false, dates: allDates }, "先行線2");
      line(ind.ichimoku_chikou, "#a3e635", 0, { ...thin, lastValueVisible: false }, "遅行線");
      const cloud = allDates
        .map((time, i) => ({ time, a: span1[i], b: span2[i] }))
        .filter((p) => p.a !== null && p.b !== null);
      candle.attachPrimitive(new BandFill(cloud, "rgba(255,92,108,0.13)", "rgba(62,166,255,0.13)"));
    }

    if (overlays.has("gmma")) {
      const thin = { ...main, lineWidth: 1, lastValueVisible: false };
      P.gmma.short.forEach((n, i) => line(ind[`gmma_short_${n}`], `hsla(${40 - i * 5}, 95%, ${62 - i * 3}%, 0.85)`, 0, thin));
      P.gmma.long.forEach((n, i) => line(ind[`gmma_long_${n}`], `hsla(${160 + i * 6}, 70%, ${58 - i * 3}%, 0.85)`, 0, thin));
    }

    if (overlays.has("parabolic")) {
      line(ind.parabolic, "#facc15", 0, {
        ...main, lineVisible: false, pointMarkersVisible: true, pointMarkersRadius: 1.6, lastValueVisible: false,
      }, "SAR");
    }

    // ---------- マーカー（売買シグナル + 開示） ----------
    // 売買シグナルと開示のマーカーは1つの配列にマージして time 昇順に並べ、createSeriesMarkers を1回だけ呼ぶ。
    // 複数回呼んでも消えはしないが、別々に設定すると同じ足での重なり回避が効かない（SPEC §2.5.3）。
    // そのため配列の構築は overlays.has("signals") の分岐の外に出し、シグナルのチップが OFF でも
    // 開示マーカーだけは出せるようにする（逆も同様）
    {
      let markers = [];
      if (overlays.has("signals")) {
        // ゴールデン/デッドクロスは矢印+文字、その他は小さな丸にして混み合いを抑える
        markers = markers.concat(d.signals.map((s) => {
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
        }));
      }
      if (overlays.has("disclosures")) {
        // data.events はトップレベル（data.chart の外）。events が無い銘柄・古い payload でも落ちないように
        const eventMarkers = data.events?.markers ?? [];
        markers = markers.concat(eventMarkers.map((m) => ({
          id: m.id, // "ev:<日付>"。P5-5 のクリック検出で param.hoveredObjectId として使う
          time: m.date,
          position: EVENT_POSITIONS[m.category] ?? "aboveBar",
          shape: EVENT_SHAPES[m.category] ?? "square",
          color: EVENT_COLORS[m.category] ?? EVENT_COLORS.other,
          text: m.text,
          size: 1,
        })));
      }
      markers.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
      LWC.createSeriesMarkers(candle, markers);
    }

    // ---------- 下段ペイン ----------
    const paneLabels = [];
    panes.forEach((p, idx) => {
      const pane = idx + 1;
      switch (p.id) {
        case "volume": {
          paneLabels.push("出来高");
          const vol = chart.addSeries(LWC.HistogramSeries, { priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false }, pane);
          vol.setData(dates.map((time, i) => ({
            time, value: d.volume[i],
            color: d.close[i] >= d.open[i] ? "rgba(255,92,108,0.55)" : "rgba(62,166,255,0.55)",
          })));
          break;
        }
        case "macd": {
          paneLabels.push(`MACD (${P.macd.fast},${P.macd.slow})  シグナル (${P.macd.signal})`);
          const hist = ind.macd.map((m, i) => (m === null || ind.macd_signal[i] === null ? null : m - ind.macd_signal[i]));
          histogram(hist, pane, { priceFormat: pf2 });
          line(ind.macd, "#f5b83d", pane, { priceFormat: pf2 });
          line(ind.macd_signal, "#c084fc", pane, { lineWidth: 1, priceFormat: pf2 });
          break;
        }
        case "rsi": {
          paneLabels.push(`RSI 中期 (${P.rsi})`);
          const s = line(ind.rsi, "#c084fc", pane, { priceFormat: pf1 });
          guide(s, 70); guide(s, 30);
          break;
        }
        case "rci": {
          paneLabels.push(`RCI 短期 (${P.rci.short}) / 長期 (${P.rci.long})`);
          const s = line(ind.rci_short, "#38bdf8", pane, { lineWidth: 1, priceFormat: pf1 });
          line(ind.rci_long, "#f472b6", pane, { lineWidth: 1, priceFormat: pf1 });
          guide(s, 80); guide(s, 0); guide(s, -80);
          break;
        }
        case "dmi": {
          paneLabels.push(`+DI / -DI / ADX (${P.dmi})`);
          line(ind.plus_di, COLORS.up, pane, { lineWidth: 1, priceFormat: pf1 });
          line(ind.minus_di, COLORS.down, pane, { lineWidth: 1, priceFormat: pf1 });
          const s = line(ind.adx, "#f5b83d", pane, { priceFormat: pf1 });
          guide(s, 25);
          break;
        }
        case "stoch": {
          paneLabels.push(`ストキャスティクス %K (${P.stoch.k}) / %D (${P.stoch.d})`);
          const s = line(ind.stoch_k, "#4ade80", pane, { lineWidth: 1, priceFormat: pf1 });
          line(ind.stoch_d, "#f5b83d", pane, { lineWidth: 1, priceFormat: pf1 });
          guide(s, 80); guide(s, 20);
          break;
        }
        case "deviation": {
          paneLabels.push(`移動平均乖離率 短期 (${P.deviation.short}) / 長期 (${P.deviation.long}) %`);
          const s = histogram(ind.deviation_short, pane, { priceFormat: pf2 });
          line(ind.deviation_long, "#f472b6", pane, { lineWidth: 1, priceFormat: pf2 });
          guide(s, 0);
          break;
        }
        case "psy": {
          paneLabels.push(`サイコロジカルライン (${P.psychological}) %`);
          const s = line(ind.psychological, "#94a3b8", pane, { priceFormat: pf1 });
          guide(s, 75); guide(s, 25);
          break;
        }
        case "stddev":
          paneLabels.push(`標準偏差 (${P.stddev})`);
          line(ind.stddev, "#22d3ee", pane, { priceFormat });
          break;
        case "momentum": {
          paneLabels.push(`モメンタム (${P.momentum.period})  シグナル (${P.momentum.signal})`);
          const s = line(ind.momentum, "#a3e635", pane, { priceFormat });
          line(ind.momentum_signal, "#f472b6", pane, { lineWidth: 1, priceFormat });
          guide(s, 0);
          break;
        }
        case "short": {
          // 空売り残高合計（0.5%以上の報告義務者の合計・本ツール算出）。報告が出た日にだけ変わる量なので階段線。
          // 線種は必ず定数 LWC.LineType.WithSteps を使う（数値の 2 は曲線。SPEC §2.5.2）
          paneLabels.push("空売り残高（報告義務 0.5% 以上の合計・本ツール算出）%");
          const byDate = new Map(d.short.points.map((pt) => [pt.date, pt.ratio]));
          // シリーズに渡すのは payload の点だけ（報告があった日と、最後に足される据え置きの点）。
          // 階段線なので、点と点の間は自動で水平に延びる
          const sparse = dates.map((date) => (byDate.has(date) ? byDate.get(date) : null));
          // 凡例だけは直前の報告値を持ち越した配列を使う。階段線は見た目上ずっと値を保持しているので、
          // 報告のない日にクロスヘアを合わせたときに凡例が「—」になると画面と矛盾する
          let lastRatio = null;
          const held = dates.map((date) => {
            if (byDate.has(date)) lastRatio = byDate.get(date);
            return lastRatio;  // 最初の報告より前は null のまま
          });
          const fmtRatio = (v) => (v === null || v === undefined ? "—" : `${window.fmt.num(v, 2)}%`);
          line(sparse, "#fb7185", pane, {
            lineType: LWC.LineType.WithSteps,
            priceFormat: pf2,
          });
          legendOnly("空売り残高", "#fb7185", held, fmtRatio);
          break;
        }
        case "taisyaku": {
          // 貸借取引残高（日証金）。融資残高・貸株残高の通常線。日次データなので欠測は「不明」であり、
          // 据え置きにせず線を切る。案A: 連続区間（dates の並びで隣り合う日にデータがあるか）ごとに
          // 別シリーズへ分ける（SPEC §2.5.2・§9-2）
          paneLabels.push("貸借取引残高（日証金） 融資残高 / 貸株残高");
          const byDate = new Map(d.taisyaku.points.map((pt) => [pt.date, pt]));
          const segments = [];
          let cur = null;
          dates.forEach((date, i) => {
            if (byDate.has(date)) {
              if (cur === null) cur = { start: i, end: i };
              else cur.end = i;
            } else if (cur) {
              segments.push(cur);
              cur = null;
            }
          });
          if (cur) segments.push(cur);

          const denseFor = (field) => dates.map((date) => {
            const pt = byDate.get(date);
            return pt ? pt[field] : null;
          });
          const taisyakuFormat = { type: "price", precision: 0, minMove: 1 };
          const buildField = (field, color) => {
            segments.forEach((seg, idx) => {
              const isLast = idx === segments.length - 1;
              const values = dates.map((date, i) => {
                if (i < seg.start || i > seg.end) return null;
                const pt = byDate.get(date);
                return pt ? pt[field] : null;
              });
              // 区間シリーズには legend を渡さない。lastValueVisible は最後の区間だけ
              // （重複防止。区間ごとに出すと過去区間の末尾にも値ラベルが残ってしまう）
              line(values, color, pane, {
                lineWidth: 1.5,
                priceFormat: taisyakuFormat,
                lastValueVisible: isLast,
              });
            });
            legendOnly(field === "yushi" ? "融資残高" : "貸株残高", color, denseFor(field), (v) => (v === null || v === undefined ? "—" : window.fmt.volume(v)));
          };
          buildField("yushi", "#facc15");
          buildField("kashi", "#818cf8");
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
      for (const [label, color, values, formatter] of legendItems) {
        const raw = values[i] ?? null;
        const text = formatter ? formatter(raw) : f.price(raw, currency);
        parts.push(`<span style="color:${color}">${f.escape(label)} <b>${text}</b></span>`);
      }
      legendEl.innerHTML = parts.join("");
    }

    chart.subscribeCrosshairMove((param) => updateLegend(param.time ?? null));
    updateLegend(null);

    const ro = new ResizeObserver(() => requestAnimationFrame(placePaneLabels));
    ro.observe(container);
    requestAnimationFrame(placePaneLabels);

    function setRange(range) {
      if (range === "all") {
        chart.timeScale().fitContent();
      } else {
        const total = dates.length;
        chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, total - Number(range)), to: total + future.length + 2 });
      }
    }

    // ---------- 連動用 API（P5-3。イベント欄との連動に使う） ----------
    // 表示範囲の変化・マーカークリックは、render() ごとに閉じた購読を張る（destroy 時は chart.remove() で片付く）。
    // コールバックは配列で持ち、onXxx が複数回呼ばれても壊れないようにする（購読の張り直しのたびに増える想定）
    const rangeCallbacks = [];
    let rangeSubscribed = false;
    function currentVisibleDateRange() {
      const r = chart.timeScale().getVisibleRange();
      if (!r || r.from == null || r.to == null) return null;
      return { from: String(r.from), to: String(r.to) };
    }
    function onVisibleRangeChange(cb) {
      rangeCallbacks.push(cb);
      if (!rangeSubscribed) {
        rangeSubscribed = true;
        chart.timeScale().subscribeVisibleTimeRangeChange(() => {
          const range = currentVisibleDateRange();
          rangeCallbacks.forEach((fn) => fn(range));
        });
      }
      // 登録した直後に現在の表示範囲で1回呼ぶ（初期表示でイベント欄が空のままにならないように）
      cb(currentVisibleDateRange());
    }

    const markerClickCallbacks = [];
    let markerClickSubscribed = false;
    function onMarkerClick(cb) {
      markerClickCallbacks.push(cb);
      if (!markerClickSubscribed) {
        markerClickSubscribed = true;
        chart.subscribeClick((param) => {
          const id = param?.hoveredObjectId;
          if (typeof id === "string" && id.startsWith("ev:")) markerClickCallbacks.forEach((fn) => fn(id));
        });
      }
    }

    // その日付が画面中央付近に来るようスクロールする。現在の表示幅は保つ。
    // dates に無い日付（休場日など）は「その日以降で最初にある足」に寄せる。それも無ければ何もしない
    function scrollToDate(date) {
      const ts = chart.timeScale();
      const range = ts.getVisibleLogicalRange();
      if (!range) return;
      const width = range.to - range.from;
      let idx = indexByDate.has(date) ? indexByDate.get(date) : dates.findIndex((d0) => d0 >= date);
      if (idx === -1 || idx === undefined) return;
      ts.setVisibleLogicalRange({ from: idx - width / 2, to: idx + width / 2 });
    }

    function visibleLogicalRange() {
      return chart.timeScale().getVisibleLogicalRange();
    }
    function setVisibleLogicalRange(range) {
      if (!range || typeof range.from !== "number" || typeof range.to !== "number") return;
      chart.timeScale().setVisibleLogicalRange(range);
    }

    return {
      setRange,
      scrollToDate,
      onVisibleRangeChange,
      onMarkerClick,
      visibleLogicalRange,
      setVisibleLogicalRange,
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
