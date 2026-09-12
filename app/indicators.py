"""テクニカル指標の計算。

入力は日付昇順の OHLCV DataFrame（列: date, open, high, low, close, volume）。
すべての指標は pandas / numpy だけで計算する（TA-Lib 等の外部依存なし）。
期間などのパラメータは PARAMS で一括管理する。変更した場合は登録済み銘柄の指標を再計算すること。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

PARAMS = {
    "sma": {"short": 5, "mid": 25, "long": 75},
    "ema": {"short": 5, "mid": 25, "long": 75},
    "bb": {"period": 20, "sigma": 2},
    "macd": {"fast": 12, "slow": 26, "signal": 9},
    "rsi": 14,
    # shift: 先行線・遅行線のずらし幅。「当日を含めて26日」とする国内チャートの慣習に合わせ 25 本
    "ichimoku": {"tenkan": 9, "kijun": 26, "senkou2": 52, "shift": 25},
    "rci": {"short": 9, "long": 26},
    "dmi": 14,
    # 多重移動平均（GMMA: 指数平滑移動平均の短期群・長期群）
    "gmma": {"short": [3, 5, 8, 10, 12, 15], "long": [30, 35, 40, 45, 50, 60]},
    "parabolic": {"step": 0.02, "max": 0.2},
    "stoch": {"k": 9, "d": 3},
    "deviation": {"short": 25, "long": 75},
    "psychological": 12,
    "stddev": 20,
    "momentum": {"period": 10, "signal": 9},
}

ICHIMOKU_SHIFT = PARAMS["ichimoku"]["shift"]

_P = PARAMS
# (DB列名, CSV/画面表示名)。DBスキーマ・CSV出力はこの定義から生成する。
INDICATOR_COLUMNS: list[tuple[str, str]] = [
    ("sma_short", f"移動平均 短期({_P['sma']['short']})"),
    ("sma_mid", f"移動平均 中期({_P['sma']['mid']})"),
    ("sma_long", f"移動平均 長期({_P['sma']['long']})"),
    ("bb_upper", f"ボリンジャー +{_P['bb']['sigma']}σ"),
    ("bb_mid", f"ボリンジャー 移動平均({_P['bb']['period']})"),
    ("bb_lower", f"ボリンジャー -{_P['bb']['sigma']}σ"),
    ("macd", f"MACD({_P['macd']['fast']},{_P['macd']['slow']})"),
    ("macd_signal", f"MACD シグナル({_P['macd']['signal']})"),
    ("rsi", f"RSI 中期({_P['rsi']})"),
    ("ichimoku_kijun", f"一目 基準線({_P['ichimoku']['kijun']})"),
    ("ichimoku_tenkan", f"一目 転換線({_P['ichimoku']['tenkan']})"),
    ("ichimoku_senkou1", "一目 先行線1"),
    ("ichimoku_senkou2", f"一目 先行線2({_P['ichimoku']['senkou2']})"),
    ("ichimoku_chikou", "一目 遅行線"),
    ("ema_short", f"指数平滑移動平均 短期({_P['ema']['short']})"),
    ("ema_mid", f"指数平滑移動平均 中期({_P['ema']['mid']})"),
    ("ema_long", f"指数平滑移動平均 長期({_P['ema']['long']})"),
    ("rci_short", f"RCI 短期({_P['rci']['short']})"),
    ("rci_long", f"RCI 長期({_P['rci']['long']})"),
    ("plus_di", f"+DI({_P['dmi']})"),
    ("minus_di", f"-DI({_P['dmi']})"),
    ("adx", f"ADX({_P['dmi']})"),
    *((f"gmma_short_{n}", f"多重移動平均 最短群({n})") for n in _P["gmma"]["short"]),
    *((f"gmma_long_{n}", f"多重移動平均 最長群({n})") for n in _P["gmma"]["long"]),
    ("parabolic", f"パラボリック({_P['parabolic']['step']},{_P['parabolic']['max']})"),
    ("stoch_k", f"ストキャス %K({_P['stoch']['k']})"),
    ("stoch_d", f"ストキャス %D({_P['stoch']['d']})"),
    ("deviation_short", f"移動平均乖離率 短期({_P['deviation']['short']})%"),
    ("deviation_long", f"移動平均乖離率 長期({_P['deviation']['long']})%"),
    ("psychological", f"サイコロジカルライン({_P['psychological']})%"),
    ("stddev", f"標準偏差({_P['stddev']})"),
    ("momentum", f"モメンタム({_P['momentum']['period']})"),
    ("momentum_signal", f"モメンタム シグナル({_P['momentum']['signal']})"),
]

INDICATOR_KEYS = [key for key, _ in INDICATOR_COLUMNS]


def value_kind(key: str) -> str:
    """表示時の丸め方の分類。price: 株価と同じ単位 / pct: パーセント等の指数 / macd: MACD 系。"""
    if key.startswith(("sma_", "ema_", "bb_", "ichimoku_", "gmma_")) or key in ("parabolic", "stddev", "momentum", "momentum_signal"):
        return "price"
    if key.startswith("macd"):
        return "macd"
    return "pct"


def ai_column_names() -> dict[str, str]:
    """AI 向け出力用の列名（英語 snake_case、期間を含めて自己説明的にする）。"""
    p = PARAMS
    ich = p["ichimoku"]
    names = {
        "sma_short": f"sma_{p['sma']['short']}",
        "sma_mid": f"sma_{p['sma']['mid']}",
        "sma_long": f"sma_{p['sma']['long']}",
        "bb_upper": f"bb_{p['bb']['period']}_upper_{p['bb']['sigma']}sd",
        "bb_mid": f"bb_{p['bb']['period']}_middle",
        "bb_lower": f"bb_{p['bb']['period']}_lower_{p['bb']['sigma']}sd",
        "macd": f"macd_{p['macd']['fast']}_{p['macd']['slow']}",
        "macd_signal": f"macd_signal_{p['macd']['signal']}",
        "rsi": f"rsi_{p['rsi']}",
        "ichimoku_kijun": f"ichimoku_kijun_{ich['kijun']}",
        "ichimoku_tenkan": f"ichimoku_tenkan_{ich['tenkan']}",
        "ichimoku_senkou1": "ichimoku_senkou_span_a",
        "ichimoku_senkou2": f"ichimoku_senkou_span_b_{ich['senkou2']}",
        "ichimoku_chikou": "ichimoku_chikou_span",
        "ema_short": f"ema_{p['ema']['short']}",
        "ema_mid": f"ema_{p['ema']['mid']}",
        "ema_long": f"ema_{p['ema']['long']}",
        "rci_short": f"rci_{p['rci']['short']}",
        "rci_long": f"rci_{p['rci']['long']}",
        "plus_di": f"plus_di_{p['dmi']}",
        "minus_di": f"minus_di_{p['dmi']}",
        "adx": f"adx_{p['dmi']}",
        "parabolic": "parabolic_sar",
        "stoch_k": f"stoch_k_{p['stoch']['k']}",
        "stoch_d": f"stoch_d_{p['stoch']['d']}",
        "deviation_short": f"ma_deviation_pct_{p['deviation']['short']}",
        "deviation_long": f"ma_deviation_pct_{p['deviation']['long']}",
        "psychological": f"psychological_line_pct_{p['psychological']}",
        "stddev": f"stddev_{p['stddev']}",
        "momentum": f"momentum_{p['momentum']['period']}",
        "momentum_signal": f"momentum_signal_sma_{p['momentum']['signal']}",
    }
    for group in ("short", "long"):
        for n in p["gmma"][group]:
            names[f"gmma_{group}_{n}"] = f"gmma_{group}_ema_{n}"
    return names


def indicator_definitions() -> list[tuple[str, str]]:
    """AI 向け出力に添える指標の定義と読み方（AI列名, 説明）。"""
    p = PARAMS
    n = ai_column_names()
    return [
        (f"{n['sma_short']}, {n['sma_mid']}, {n['sma_long']}", "終値の単純移動平均（短期/中期/長期）。短期>中期>長期なら上昇トレンド"),
        (f"{n['bb_upper']}, {n['bb_mid']}, {n['bb_lower']}", f"ボリンジャーバンド。{p['bb']['period']}日移動平均±{p['bb']['sigma']}×母標準偏差。バンド外は行き過ぎの目安"),
        (f"{n['macd']}, {n['macd_signal']}", "MACD=短期EMA−長期EMA、シグナル=MACDのEMA。MACDがシグナルを上抜けで買い、下抜けで売りの目安"),
        (n["rsi"], "RSI（Wilder平滑）。0〜100。70以上で買われすぎ、30以下で売られすぎ"),
        ("ichimoku_*", f"一目均衡表。転換線/基準線は期間中の(最高値+最安値)/2。先行スパンA/Bは{ich_shift_text()}先に描画される値をその日に格納。遅行スパンは{ich_shift_text()}先の終値（直近は空欄）。株価が雲(先行A/B)の上なら強気"),
        (f"{n['ema_short']}, {n['ema_mid']}, {n['ema_long']}", "終値の指数平滑移動平均（短期/中期/長期）"),
        (f"{n['rci_short']}, {n['rci_long']}", "RCI（日付と価格の順位相関×100）。-100〜+100。+80以上で高値圏、-80以下で底値圏"),
        (f"{n['plus_di']}, {n['minus_di']}, {n['adx']}", "DMI/ADX。+DI>-DIなら上昇優勢。ADX 25以上でトレンドが強い"),
        ("gmma_short_ema_*, gmma_long_ema_*", "多重移動平均(GMMA)。最短群がすべて最長群の上なら強い上昇トレンド"),
        (n["parabolic"], f"パラボリックSAR（加速因子{p['parabolic']['step']}、上限{p['parabolic']['max']}）。終値がSARより上なら上昇トレンド"),
        (f"{n['stoch_k']}, {n['stoch_d']}", "ストキャスティクス。0〜100。80以上で高値圏、20以下で安値圏"),
        (f"{n['deviation_short']}, {n['deviation_long']}", "移動平均乖離率(%)=(終値−移動平均)/移動平均×100"),
        (n["psychological"], "サイコロジカルライン(%)。期間中の上昇日の割合。75%以上で買われすぎ、25%以下で売られすぎ"),
        (n["stddev"], "終値の母標準偏差（価格の単位）。値動きの大きさ"),
        (f"{n['momentum']}, {n['momentum_signal']}", "モメンタム=終値−n日前終値、シグナル=その単純移動平均。0より上なら上昇の勢い"),
    ]


def ich_shift_text() -> str:
    return f"{PARAMS['ichimoku']['shift']}本"


# ---------------------------------------------------------------------------
# 個別の計算
# ---------------------------------------------------------------------------
def wilder_smooth(values: pd.Series, period: int) -> pd.Series:
    """Wilder の平滑化（RSI/DMI 用）。

    最初の有効値は先頭 period 本の単純平均、以降は
    (prev * (period - 1) + value) / period で更新する。
    """
    arr = values.to_numpy(dtype=float)
    out = np.full(arr.shape, np.nan)

    start = None
    run = 0
    for i, v in enumerate(arr):
        run = run + 1 if not np.isnan(v) else 0
        if run == period:
            start = i
            break
    if start is None:
        return pd.Series(out, index=values.index)

    out[start] = arr[start - period + 1 : start + 1].mean()
    for i in range(start + 1, len(arr)):
        v = arr[i]
        out[i] = out[i - 1] if np.isnan(v) else (out[i - 1] * (period - 1) + v) / period
    return pd.Series(out, index=values.index)


def _safe_div(num: pd.Series, den: pd.Series) -> pd.Series:
    return num / den.replace(0, np.nan)


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def _mid_price(high: pd.Series, low: pd.Series, period: int) -> pd.Series:
    return (high.rolling(period).max() + low.rolling(period).min()) / 2


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    avg_gain = wilder_smooth(delta.clip(lower=0), period)
    avg_loss = wilder_smooth(-delta.clip(upper=0), period)
    result = 100 - 100 / (1 + _safe_div(avg_gain, avg_loss))
    # 下落がゼロの区間は RSI=100
    result[(avg_loss == 0) & avg_gain.notna()] = 100.0
    return result


def _rank_desc(values: np.ndarray) -> np.ndarray:
    """大きい値ほど順位 1。同値は平均順位。"""
    order = np.argsort(-values, kind="mergesort")
    ranks = np.empty(len(values))
    ranks[order] = np.arange(1, len(values) + 1)
    for v in np.unique(values):
        mask = values == v
        if mask.sum() > 1:
            ranks[mask] = ranks[mask].mean()
    return ranks


def rci(close: pd.Series, period: int) -> pd.Series:
    """RCI（順位相関指数）。-100〜+100。

    日付順位（新しい日ほど 1）と価格順位（高い価格ほど 1）のスピアマン順位相関 ×100。
    """
    time_rank = np.arange(period, 0, -1, dtype=float)  # ウィンドウは古い→新しいの順
    denom = period * (period**2 - 1)

    def calc(window: np.ndarray) -> float:
        d = time_rank - _rank_desc(window)
        return (1 - 6 * np.sum(d**2) / denom) * 100

    return close.rolling(period).apply(calc, raw=True)


def parabolic_sar(high: pd.Series, low: pd.Series, step: float = 0.02, max_af: float = 0.2) -> pd.Series:
    """パラボリック SAR（Wilder）。"""
    h, lo = high.to_numpy(dtype=float), low.to_numpy(dtype=float)
    n = len(h)
    out = np.full(n, np.nan)
    if n < 2:
        return pd.Series(out, index=high.index)

    uptrend = h[1] + lo[1] >= h[0] + lo[0]
    sar = lo[0] if uptrend else h[0]
    ep = h[0] if uptrend else lo[0]
    af = step

    for i in range(1, n):
        sar = sar + af * (ep - sar)
        if uptrend:
            sar = min(sar, lo[i - 1], lo[i - 2] if i >= 2 else lo[i - 1])
            if lo[i] < sar:  # 反転して下降トレンドへ
                uptrend, sar, ep, af = False, ep, lo[i], step
            elif h[i] > ep:
                ep, af = h[i], min(af + step, max_af)
        else:
            sar = max(sar, h[i - 1], h[i - 2] if i >= 2 else h[i - 1])
            if h[i] > sar:  # 反転して上昇トレンドへ
                uptrend, sar, ep, af = True, ep, h[i], step
            elif lo[i] < ep:
                ep, af = lo[i], min(af + step, max_af)
        out[i] = sar
    return pd.Series(out, index=high.index)


# ---------------------------------------------------------------------------
# まとめて計算
# ---------------------------------------------------------------------------
def compute_indicators(prices: pd.DataFrame) -> pd.DataFrame:
    """日ごとのテクニカル指標を計算し、date + INDICATOR_KEYS 列の DataFrame を返す。"""
    p = PARAMS
    df = prices.sort_values("date").reset_index(drop=True)
    high, low, close = df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)

    out = pd.DataFrame({"date": df["date"]})

    # 移動平均 / 指数平滑移動平均
    for term in ("short", "mid", "long"):
        out[f"sma_{term}"] = close.rolling(p["sma"][term]).mean()
        out[f"ema_{term}"] = _ema(close, p["ema"][term])

    # ボリンジャーバンド（母標準偏差）
    mid = close.rolling(p["bb"]["period"]).mean()
    band = close.rolling(p["bb"]["period"]).std(ddof=0) * p["bb"]["sigma"]
    out["bb_upper"], out["bb_mid"], out["bb_lower"] = mid + band, mid, mid - band

    # MACD
    out["macd"] = _ema(close, p["macd"]["fast"]) - _ema(close, p["macd"]["slow"])
    out["macd_signal"] = out["macd"].ewm(span=p["macd"]["signal"], adjust=False, min_periods=p["macd"]["signal"]).mean()

    # RSI
    out["rsi"] = rsi(close, p["rsi"])

    # 一目均衡表（その日のチャート上に描画される値として格納）
    ich = p["ichimoku"]
    tenkan = _mid_price(high, low, ich["tenkan"])
    kijun = _mid_price(high, low, ich["kijun"])
    out["ichimoku_kijun"] = kijun
    out["ichimoku_tenkan"] = tenkan
    out["ichimoku_senkou1"] = ((tenkan + kijun) / 2).shift(ich["shift"])
    out["ichimoku_senkou2"] = _mid_price(high, low, ich["senkou2"]).shift(ich["shift"])
    out["ichimoku_chikou"] = close.shift(-ich["shift"])

    # RCI
    out["rci_short"] = rci(close, p["rci"]["short"])
    out["rci_long"] = rci(close, p["rci"]["long"])

    # DMI / ADX
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    tr = tr.where(prev_close.notna())  # DM と同じく2本目から
    up_move, down_move = high.diff(), -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    plus_dm[up_move.isna()] = np.nan
    minus_dm[down_move.isna()] = np.nan
    smoothed_tr = wilder_smooth(tr, p["dmi"])
    out["plus_di"] = _safe_div(wilder_smooth(plus_dm, p["dmi"]), smoothed_tr) * 100
    out["minus_di"] = _safe_div(wilder_smooth(minus_dm, p["dmi"]), smoothed_tr) * 100
    dx = _safe_div((out["plus_di"] - out["minus_di"]).abs(), out["plus_di"] + out["minus_di"]) * 100
    out["adx"] = wilder_smooth(dx, p["dmi"])

    # 多重移動平均（GMMA）
    for group in ("short", "long"):
        for n in p["gmma"][group]:
            out[f"gmma_{group}_{n}"] = _ema(close, n)

    # パラボリック
    out["parabolic"] = parabolic_sar(high, low, p["parabolic"]["step"], p["parabolic"]["max"])

    # ストキャスティクス（%D は (終値-最安値) と (最高値-最安値) の d 日合計の比）
    k = p["stoch"]["k"]
    lowest, highest = low.rolling(k).min(), high.rolling(k).max()
    out["stoch_k"] = _safe_div(close - lowest, highest - lowest) * 100
    d = p["stoch"]["d"]
    out["stoch_d"] = _safe_div((close - lowest).rolling(d).sum(), (highest - lowest).rolling(d).sum()) * 100

    # 移動平均乖離率
    for term in ("short", "long"):
        ma = close.rolling(p["deviation"][term]).mean()
        out[f"deviation_{term}"] = _safe_div(close - ma, ma) * 100

    # サイコロジカルライン
    diff = close.diff()
    up_day = (diff > 0).astype(float).where(diff.notna())
    out["psychological"] = up_day.rolling(p["psychological"]).sum() / p["psychological"] * 100

    # 標準偏差（母標準偏差）
    out["stddev"] = close.rolling(p["stddev"]).std(ddof=0)

    # モメンタム（当日終値 - n日前終値）とそのシグナル（単純移動平均）
    out["momentum"] = close - close.shift(p["momentum"]["period"])
    out["momentum_signal"] = out["momentum"].rolling(p["momentum"]["signal"]).mean()

    return out[["date", *INDICATOR_KEYS]]


def ichimoku_future_cloud(prices: pd.DataFrame) -> list[dict]:
    """最終日より先（1〜ICHIMOKU_SHIFT 本先）に描画される先行線1/2を返す。"""
    ich = PARAMS["ichimoku"]
    df = prices.sort_values("date").reset_index(drop=True)
    high, low = df["high"].astype(float), df["low"].astype(float)
    tenkan = _mid_price(high, low, ich["tenkan"])
    kijun = _mid_price(high, low, ich["kijun"])
    span1 = ((tenkan + kijun) / 2).tail(ICHIMOKU_SHIFT)
    span2 = _mid_price(high, low, ich["senkou2"]).tail(ICHIMOKU_SHIFT)
    return [
        {"offset": i + 1, "senkou1": _nan_to_none(a), "senkou2": _nan_to_none(b)}
        for i, (a, b) in enumerate(zip(span1, span2))
    ]


def _nan_to_none(value):
    if value is None:
        return None
    value = float(value)
    return None if np.isnan(value) else value


# ---------------------------------------------------------------------------
# シグナル・判定
# ---------------------------------------------------------------------------
def _crosses(a: pd.Series, b: pd.Series) -> tuple[pd.Series, pd.Series]:
    """a が b を上抜け / 下抜けした日を示すブール Series を返す。"""
    diff = a - b
    prev = diff.shift(1)
    valid = diff.notna() & prev.notna()
    return valid & (prev <= 0) & (diff > 0), valid & (prev >= 0) & (diff < 0)


def detect_signals(indicators: pd.DataFrame) -> list[dict]:
    """売買シグナル候補（クロス・買われすぎ/売られすぎからの反転）を日付昇順で返す。"""
    ind = indicators.reset_index(drop=True)
    rules: list[tuple[pd.Series, str, str, str]] = []  # (該当日, 売買方向, チャート用略称, 名称)
    s, m = PARAMS["sma"]["short"], PARAMS["sma"]["mid"]

    gc, dc = _crosses(ind["sma_short"], ind["sma_mid"])
    rules += [(gc, "buy", "GC", f"ゴールデンクロス({s}/{m})"), (dc, "sell", "DC", f"デッドクロス({s}/{m})")]

    up, down = _crosses(ind["macd"], ind["macd_signal"])
    rules += [(up, "buy", "M+", "MACD買いクロス"), (down, "sell", "M-", "MACD売りクロス")]

    rsi_prev = ind["rsi"].shift(1)
    rules += [
        ((rsi_prev < 30) & (ind["rsi"] >= 30), "buy", "R30", "RSI 30回復"),
        ((rsi_prev > 70) & (ind["rsi"] <= 70), "sell", "R70", "RSI 70割れ"),
    ]

    signals = [
        {"date": ind.at[i, "date"], "direction": direction, "short": short, "label": label}
        for mask, direction, short, label in rules
        for i in ind.index[mask.fillna(False)]
    ]
    return sorted(signals, key=lambda sig: sig["date"])


def evaluate_latest(prices: pd.DataFrame, indicators: pd.DataFrame) -> list[dict]:
    """最新日の主要指標を判定付きで返す（ダッシュボードのカード表示用）。"""
    if indicators.empty:
        return []
    p = PARAMS
    last = indicators.iloc[-1]
    close = float(prices.sort_values("date")["close"].iloc[-1])

    def v(key):
        return _nan_to_none(last[key])

    def has(*values):
        return all(x is not None for x in values)

    cards = []

    def card(key, label, value, status, note, group):
        cards.append({"key": key, "label": label, "value": value, "status": status, "note": note, "group": group})

    # ---- トレンド系 ----
    for prefix, name in (("sma", "移動平均"), ("ema", "指数平滑移動平均")):
        short, mid, long_ = v(f"{prefix}_short"), v(f"{prefix}_mid"), v(f"{prefix}_long")
        periods = "/".join(str(p[prefix][t]) for t in ("short", "mid", "long"))
        if not has(short, mid, long_):
            card(prefix, name, mid, "na", f"データ不足（{p[prefix]['long']}日分必要）", "trend")
        elif short > mid > long_:
            card(prefix, name, mid, "bull", f"短期 > 中期 > 長期（{periods}）上昇トレンド", "trend")
        elif short < mid < long_:
            card(prefix, name, mid, "bear", f"短期 < 中期 < 長期（{periods}）下降トレンド", "trend")
        else:
            card(prefix, name, mid, "neutral", f"短期・中期・長期が交錯（{periods}）", "trend")

    macd, sig = v("macd"), v("macd_signal")
    if has(macd, sig):
        card("macd", "MACD", macd, "bull" if macd > sig else "bear",
             f"MACD がシグナル（{sig:,.2f}）より{'上' if macd > sig else '下'}", "trend")

    tenkan, kijun, s1, s2 = v("ichimoku_tenkan"), v("ichimoku_kijun"), v("ichimoku_senkou1"), v("ichimoku_senkou2")
    if has(s1, s2):
        top, bottom = max(s1, s2), min(s1, s2)
        if close > top:
            status, note = "bull", "株価が雲の上"
        elif close < bottom:
            status, note = "bear", "株価が雲の下"
        else:
            status, note = "neutral", "株価が雲の中"
        if has(tenkan, kijun):
            note += "・転換線 > 基準線" if tenkan > kijun else "・転換線 ≦ 基準線"
        card("ichimoku", "一目均衡表（基準線）", kijun, status, note, "trend")

    adx, pdi, mdi = v("adx"), v("plus_di"), v("minus_di")
    if has(adx, pdi, mdi):
        direction = "上昇" if pdi > mdi else "下降"
        strong = adx >= 25
        status = ("bull" if pdi > mdi else "bear") if strong else "neutral"
        card("adx", "DMI / ADX", adx, status,
             f"{'強いトレンド' if strong else 'トレンド弱い'}・{direction}優勢（+DI {pdi:.1f} / -DI {mdi:.1f}）", "trend")

    shorts = [v(f"gmma_short_{n}") for n in p["gmma"]["short"]]
    longs = [v(f"gmma_long_{n}") for n in p["gmma"]["long"]]
    if has(*shorts, *longs):
        if min(shorts) > max(longs):
            status, note = "bull", "最短群がすべて最長群の上"
        elif max(shorts) < min(longs):
            status, note = "bear", "最短群がすべて最長群の下"
        else:
            status, note = "neutral", "最短群と最長群が交錯"
        card("gmma", "多重移動平均", None, status, note, "trend")

    sar = v("parabolic")
    if sar is not None:
        up = close > sar
        card("parabolic", "パラボリック", sar, "bull" if up else "bear",
             f"株価が SAR より{'上（上昇トレンド）' if up else '下（下降トレンド）'}", "trend")

    for term, label in (("short", "短期"), ("long", "長期")):
        dev = v(f"deviation_{term}")
        if dev is None:
            continue
        limit = 10 if term == "short" else 20
        status = "bear" if dev >= limit else "bull" if dev <= -limit else "neutral"
        note = f"上方乖離が大きい（+{limit}%以上）" if dev >= limit else f"下方乖離が大きい（-{limit}%以下）" if dev <= -limit else "通常範囲"
        card(f"deviation_{term}", f"移動平均乖離率 {label}({p['deviation'][term]})", dev, status, note, "trend")

    # ---- オシレーター系 ----
    r = v("rsi")
    if r is not None:
        status = "bear" if r >= 70 else "bull" if r <= 30 else "neutral"
        note = "買われすぎ（70以上）" if r >= 70 else "売られすぎ（30以下）" if r <= 30 else "中立圏"
        card("rsi", f"RSI 中期({p['rsi']})", r, status, note, "oscillator")

    rs, rl = v("rci_short"), v("rci_long")
    if has(rs, rl):
        status = "bear" if rs >= 80 else "bull" if rs <= -80 else "neutral"
        zone = "高値圏（+80以上）" if rs >= 80 else "底値圏（-80以下）" if rs <= -80 else "中立圏"
        card("rci", f"RCI 短期({p['rci']['short']})", rs, status, f"{zone}（長期 {rl:.1f}）", "oscillator")

    k, d = v("stoch_k"), v("stoch_d")
    if has(k, d):
        status = "bear" if k >= 80 else "bull" if k <= 20 else "neutral"
        zone = "高値圏（80以上）" if k >= 80 else "安値圏（20以下）" if k <= 20 else "中立圏"
        card("stoch", "ストキャス %K", k, status, f"{zone}（%D {d:.1f}）", "oscillator")

    psy = v("psychological")
    if psy is not None:
        status = "bear" if psy >= 75 else "bull" if psy <= 25 else "neutral"
        note = "買われすぎ（75%以上）" if psy >= 75 else "売られすぎ（25%以下）" if psy <= 25 else "中立圏"
        card("psychological", f"サイコロジカル({p['psychological']})", psy, status, note, "oscillator")

    mom, mom_sig = v("momentum"), v("momentum_signal")
    if has(mom, mom_sig):
        if mom > 0 and mom > mom_sig:
            status, note = "bull", "0より上でシグナルを上回る"
        elif mom < 0 and mom < mom_sig:
            status, note = "bear", "0より下でシグナルを下回る"
        else:
            status, note = "neutral", f"シグナル {mom_sig:,.2f}"
        card("momentum", f"モメンタム({p['momentum']['period']})", mom, status, note, "oscillator")

    # ---- ボラティリティ系 ----
    upper, lower = v("bb_upper"), v("bb_lower")
    if has(upper, lower):
        sigma = p["bb"]["sigma"]
        if close >= upper:
            status, note = "bear", f"+{sigma}σ 以上"
        elif close <= lower:
            status, note = "bull", f"-{sigma}σ 以下"
        else:
            status, note = "neutral", f"±{sigma}σ の範囲内（{lower:,.1f} 〜 {upper:,.1f}）"
        card("bb", "ボリンジャーバンド", v("bb_mid"), status, note, "volatility")

    sd = v("stddev")
    if sd is not None:
        card("stddev", f"標準偏差({p['stddev']})", sd, "neutral", f"終値比 {sd / close * 100:.2f}%", "volatility")

    return cards
