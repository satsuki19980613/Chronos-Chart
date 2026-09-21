/*
 * AI 分析レポートの株価チャート初期化スクリプト（P12-5）。
 *
 * このファイルはテンプレートエンジン（Jinja2）を通さず、レポート HTML に <script> として
 * そのまま埋め込まれる。素の JavaScript のみで書き、Jinja の構文は書かない。
 *
 * 失敗しても既存の SVG フォールバック（app.ai.charts の折れ線）を壊さないよう、全体を
 * try/catch で包み、途中で失敗したらコンテナの中身を元に戻してから終わる。
 *
 * 色の扱い: ローソク足・線・マーカーの色は、まず templates/report.html.j2 が定義する
 * CSS カスタムプロパティ（--chart-1〜--chart-8。app.ai.charts.PALETTE の並び順と対応し、
 * ダークモードで明るさの違う値に上書きされる）から解決し、値が取れなければ payload に
 * 入っている 16進のフォールバック色を使う。prefers-color-scheme の変化で読み直す。
 *
 * 印刷だけは別扱いにする。beforeprint は「印刷用のスタイルが当たる前」に発火するので、
 * そこで getComputedStyle を読んでも画面用（ダーク）の値しか取れない。そのため印刷中は
 * CSS 変数を読まず、payload の 16進（= @media print の --chart-N と同じ Okabe-Ito の値）と
 * 白背景を直接使う。
 */
(function () {
  var container = null;
  var fallbackHtml = null;
  try {
    var dataEl = document.getElementById("cc-price-chart-data");
    if (!dataEl) return; // データが無ければ何もしない（SVG フォールバックのまま）

    var payload = JSON.parse(dataEl.textContent);

    if (!window.LightweightCharts) return; // ライブラリの読み込みに失敗していたら何もしない

    container = document.getElementById("cc-price-chart");
    if (!container) return;

    // 印刷中だけ true。CSS 変数を読まずに payload の 16進と白背景を使う
    var printing = false;

    // 印刷時の配色（templates/report.html.j2 の @media print と同じ値）
    var PRINT_THEME = { text: "#5b6472", border: "#b9c0cb", background: "#ffffff" };

    // CSS カスタムプロパティ（--chart-N 等）から色を読む。値が取れなければ fallback を使う
    function resolveColor(cssVarName, fallback) {
      if (printing || !cssVarName) return fallback;
      var value = getComputedStyle(document.documentElement).getPropertyValue(cssVarName);
      value = (value || "").trim();
      return value || fallback;
    }

    // レポートの --muted（軸の文字色）・--border（枠線・グリッド）、body の背景色を読む
    function readThemeColors() {
      if (printing) return PRINT_THEME;
      var rootStyle = getComputedStyle(document.documentElement);
      var muted = (rootStyle.getPropertyValue("--muted") || "").trim();
      var border = (rootStyle.getPropertyValue("--border") || "").trim();
      var bodyBg = (getComputedStyle(document.body).backgroundColor || "").trim();
      return {
        text: muted || PRINT_THEME.text,
        border: border || PRINT_THEME.border,
        background: bodyBg && bodyBg !== "rgba(0, 0, 0, 0)" ? bodyBg : PRINT_THEME.background,
      };
    }

    // 出来高の棒は陽線・陰線色を薄くして使う。--chart-N は3モードとも 16進なので rgba に直せる
    function withAlpha(color, alpha) {
      var hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(color || "");
      if (!hex) return color;
      var body = hex[1];
      if (body.length === 3) body = body[0] + body[0] + body[1] + body[1] + body[2] + body[2];
      var r = parseInt(body.slice(0, 2), 16);
      var g = parseInt(body.slice(2, 4), 16);
      var b = parseInt(body.slice(4, 6), 16);
      return "rgba(" + r + ", " + g + ", " + b + ", " + alpha + ")";
    }

    // フォールバック用の SVG を退避してから消し、チャートの高さを確保する
    fallbackHtml = container.innerHTML;
    container.innerHTML = "";
    container.style.height = "440px";

    var theme = readThemeColors();
    var chart = LightweightCharts.createChart(container, {
      autoSize: true,
      localization: { locale: "ja-JP", dateFormat: "yyyy/MM/dd" },
      layout: {
        background: { type: "solid", color: theme.background },
        textColor: theme.text,
        fontFamily: "Yu Gothic UI, Yu Gothic, Meiryo, Segoe UI, sans-serif",
        fontSize: 11,
        panes: { separatorColor: theme.border, enableResize: false },
      },
      grid: { vertLines: { color: theme.border }, horzLines: { color: theme.border } },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      rightPriceScale: { borderColor: theme.border },
      timeScale: { borderColor: theme.border, rightOffset: 2, minBarSpacing: 1 },
    });

    var digits = typeof payload.priceDigits === "number" ? payload.priceDigits : 0;
    var priceFormat = { type: "price", precision: digits, minMove: 1 / Math.pow(10, digits) };

    // ---------- ローソク足（ペイン0） ----------
    function candleColors() {
      return {
        up: resolveColor(payload.candleUpVar, payload.candleUpColor),
        down: resolveColor(payload.candleDownVar, payload.candleDownColor),
      };
    }

    var c0 = candleColors();
    var candleSeries = chart.addSeries(
      LightweightCharts.CandlestickSeries,
      {
        upColor: c0.up,
        downColor: c0.down,
        borderUpColor: c0.up,
        borderDownColor: c0.down,
        wickUpColor: c0.up,
        wickDownColor: c0.down,
        priceFormat: priceFormat,
      },
      0
    );
    candleSeries.setData(payload.candles || []);

    function applyCandleColors() {
      var c = candleColors();
      candleSeries.applyOptions({
        upColor: c.up,
        downColor: c.down,
        borderUpColor: c.up,
        borderDownColor: c.down,
        wickUpColor: c.up,
        wickDownColor: c.down,
      });
    }

    // ---------- 移動平均線（ペイン0に重ねる） ----------
    var lineRefs = []; // [{ series, colorVar, fallbackColor }] 色の再解決に使う
    (payload.lines || []).forEach(function (lineSeriesData) {
      var series = chart.addSeries(
        LightweightCharts.LineSeries,
        {
          color: resolveColor(lineSeriesData.colorVar, lineSeriesData.color),
          lineWidth: 1.5,
          priceLineVisible: false,
          crosshairMarkerVisible: false,
        },
        0
      );
      series.setData(lineSeriesData.data || []);
      lineRefs.push({
        series: series,
        colorVar: lineSeriesData.colorVar,
        fallbackColor: lineSeriesData.color,
      });
    });

    function applyLineColors() {
      lineRefs.forEach(function (ref) {
        ref.series.applyOptions({ color: resolveColor(ref.colorVar, ref.fallbackColor) });
      });
    }

    // ---------- 出来高（ペイン1。データが無ければペイン自体を作らない） ----------
    var volumeSeries = null;
    function volumeData() {
      var c = candleColors();
      var up = withAlpha(c.up, 0.5);
      var down = withAlpha(c.down, 0.5);
      return payload.volumes.map(function (v) {
        return { time: v.time, value: v.value, color: v.up ? up : down };
      });
    }
    if (payload.volumes && payload.volumes.length) {
      volumeSeries = chart.addSeries(
        LightweightCharts.HistogramSeries,
        { priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false },
        1
      );
      volumeSeries.setData(volumeData());
    }

    // ---------- マーカー（売買シグナル + 開示。createSeriesMarkers は1回だけ呼ぶ） ----------
    function resolvedMarkers() {
      return (payload.markers || []).map(function (m) {
        var resolved = {};
        for (var key in m) {
          if (Object.prototype.hasOwnProperty.call(m, key)) resolved[key] = m[key];
        }
        resolved.color = resolveColor(m.colorVar, m.color);
        return resolved;
      });
    }

    var markersApi = LightweightCharts.createSeriesMarkers(candleSeries, resolvedMarkers());

    // ---------- ペインの高さ配分（ダッシュボードと同じ 3.2 : 1） ----------
    var allPanes = chart.panes();
    if (allPanes.length > 1) {
      allPanes[0].setStretchFactor(3.2);
      for (var i = 1; i < allPanes.length; i++) allPanes[i].setStretchFactor(1);
    }

    chart.timeScale().fitContent();

    // ---------- テーマ追従（色を読み直して当て直す） ----------
    function applyAllColors() {
      var t = readThemeColors();
      chart.applyOptions({
        layout: {
          background: { type: "solid", color: t.background },
          textColor: t.text,
          panes: { separatorColor: t.border, enableResize: false },
        },
        grid: { vertLines: { color: t.border }, horzLines: { color: t.border } },
        rightPriceScale: { borderColor: t.border },
        timeScale: { borderColor: t.border },
      });
      applyCandleColors();
      applyLineColors();
      if (volumeSeries) volumeSeries.setData(volumeData());
      markersApi.setMarkers(resolvedMarkers());
    }

    if (window.matchMedia) {
      var media = window.matchMedia("(prefers-color-scheme: dark)");
      if (media.addEventListener) {
        media.addEventListener("change", applyAllColors);
      } else if (media.addListener) {
        // 古いブラウザ向け（addEventListener 未対応の環境の保険）
        media.addListener(applyAllColors);
      }
    }

    window.addEventListener("beforeprint", function () {
      printing = true;
      applyAllColors();
    });
    window.addEventListener("afterprint", function () {
      printing = false;
      applyAllColors();
    });
  } catch (e) {
    // 何が起きても他の表示は壊さない。途中まで進んでいたら SVG フォールバックを戻す
    if (container && fallbackHtml !== null && !container.firstChild) {
      container.innerHTML = fallbackHtml;
      container.style.height = "";
    }
  }
})();
