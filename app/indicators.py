"""テクニカル指標の計算。

入力は日付昇順の OHLCV DataFrame（列: date, open, high, low, close, volume）。
すべての指標は pandas / numpy だけで計算する（TA-Lib 等の外部依存なし）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# 一目均衡表の先行スパン/遅行スパンのずらし幅。
# 「当日を含めて26日」とする一般的な国内チャートの慣習に合わせ 25 本ずらす。
ICHIMOKU_SHIFT = 25

# (DB列名, CSV/画面表示名)。DBスキーマ・CSV出力はこの定義から生成する。
INDICATOR_COLUMNS: list[tuple[str, str]] = [
    ("sma_5", "SMA(5)"),
    ("sma_25", "SMA(25)"),
    ("sma_75", "SMA(75)"),
    ("ema_12", "EMA(12)"),
    ("ema_26", "EMA(26)"),
    ("deviation_25", "乖離率(25)%"),
    ("bb_mid", "BB中心線(20)"),
    ("bb_upper_1", "BB+1σ"),
    ("bb_lower_1", "BB-1σ"),
    ("bb_upper_2", "BB+2σ"),
    ("bb_lower_2", "BB-2σ"),
    ("bb_percent_b", "BB %B"),
    ("bb_bandwidth", "BBバンド幅%"),
    ("macd", "MACD(12,26)"),
    ("macd_signal", "MACDシグナル(9)"),
    ("macd_hist", "MACDヒストグラム"),
    ("rsi_14", "RSI(14)"),
    ("stoch_k", "ストキャス%K(14)"),
    ("stoch_d", "ストキャス%D(3)"),
    ("stoch_slow_d", "ストキャスSlow%D(3)"),
    ("atr_14", "ATR(14)"),
    ("plus_di", "+DI(14)"),
    ("minus_di", "-DI(14)"),
    ("adx", "ADX(14)"),
    ("ichimoku_tenkan", "一目 転換線(9)"),
    ("ichimoku_kijun", "一目 基準線(26)"),
    ("ichimoku_senkou_a", "一目 先行スパンA"),
    ("ichimoku_senkou_b", "一目 先行スパンB"),
    ("ichimoku_chikou", "一目 遅行スパン"),
    ("psychological_12", "サイコロジカル(12)%"),
    ("volume_ma_5", "出来高MA(5)"),
    ("volume_ma_25", "出来高MA(25)"),
]

INDICATOR_KEYS = [key for key, _ in INDICATOR_COLUMNS]


def wilder_smooth(values: pd.Series, period: int) -> pd.Series:
    """Wilder の平滑化（RSI/ATR/ADX 用）。

    最初の有効値は先頭 period 本の単純平均、以降は
    prev * (period - 1) / period + value / period で更新する。
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


def _mid_price(high: pd.Series, low: pd.Series, period: int) -> pd.Series:
    return (high.rolling(period).max() + low.rolling(period).min()) / 2


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    avg_gain = wilder_smooth(delta.clip(lower=0), period)
    avg_loss = wilder_smooth(-delta.clip(upper=0), period)
    result = 100 - 100 / (1 + _safe_div(avg_gain, avg_loss))
    # 下落がゼロの区間は RSI=100
    result[(avg_loss == 0) & avg_gain.notna()] = 100.0
    return result


def compute_indicators(prices: pd.DataFrame) -> pd.DataFrame:
    """日ごとのテクニカル指標を計算し、date + INDICATOR_KEYS 列の DataFrame を返す。"""
    df = prices.sort_values("date").reset_index(drop=True)
    high, low, close = df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)
    volume = df["volume"].astype(float)

    out = pd.DataFrame({"date": df["date"]})

    # --- トレンド系: 移動平均 ---
    for n in (5, 25, 75):
        out[f"sma_{n}"] = close.rolling(n).mean()
    for n in (12, 26):
        out[f"ema_{n}"] = close.ewm(span=n, adjust=False, min_periods=n).mean()
    out["deviation_25"] = _safe_div(close - out["sma_25"], out["sma_25"]) * 100

    # --- ボリンジャーバンド (20, 母標準偏差) ---
    mid = close.rolling(20).mean()
    std = close.rolling(20).std(ddof=0)
    out["bb_mid"] = mid
    out["bb_upper_1"], out["bb_lower_1"] = mid + std, mid - std
    out["bb_upper_2"], out["bb_lower_2"] = mid + 2 * std, mid - 2 * std
    out["bb_percent_b"] = _safe_div(close - out["bb_lower_2"], out["bb_upper_2"] - out["bb_lower_2"])
    out["bb_bandwidth"] = _safe_div(out["bb_upper_2"] - out["bb_lower_2"], mid) * 100

    # --- MACD (12, 26, 9) ---
    out["macd"] = out["ema_12"] - out["ema_26"]
    out["macd_signal"] = out["macd"].ewm(span=9, adjust=False, min_periods=9).mean()
    out["macd_hist"] = out["macd"] - out["macd_signal"]

    # --- RSI (14, Wilder) ---
    out["rsi_14"] = rsi(close, 14)

    # --- ストキャスティクス (14, 3, 3) ---
    lowest = low.rolling(14).min()
    highest = high.rolling(14).max()
    out["stoch_k"] = _safe_div(close - lowest, highest - lowest) * 100
    out["stoch_d"] = _safe_div((close - lowest).rolling(3).sum(), (highest - lowest).rolling(3).sum()) * 100
    out["stoch_slow_d"] = out["stoch_d"].rolling(3).mean()

    # --- ATR / DMI・ADX (14, Wilder) ---
    tr = _true_range(high, low, close)
    out["atr_14"] = wilder_smooth(tr, 14)

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    plus_dm[up_move.isna()] = np.nan
    minus_dm[down_move.isna()] = np.nan
    tr_dmi = tr.where(close.shift(1).notna())  # DM と同じく2本目から
    smoothed_tr = wilder_smooth(tr_dmi, 14)
    out["plus_di"] = _safe_div(wilder_smooth(plus_dm, 14), smoothed_tr) * 100
    out["minus_di"] = _safe_div(wilder_smooth(minus_dm, 14), smoothed_tr) * 100
    dx = _safe_div((out["plus_di"] - out["minus_di"]).abs(), out["plus_di"] + out["minus_di"]) * 100
    out["adx"] = wilder_smooth(dx, 14)

    # --- 一目均衡表（その日のチャート上に描画される値として格納）---
    tenkan = _mid_price(high, low, 9)
    kijun = _mid_price(high, low, 26)
    out["ichimoku_tenkan"] = tenkan
    out["ichimoku_kijun"] = kijun
    out["ichimoku_senkou_a"] = ((tenkan + kijun) / 2).shift(ICHIMOKU_SHIFT)
    out["ichimoku_senkou_b"] = _mid_price(high, low, 52).shift(ICHIMOKU_SHIFT)
    out["ichimoku_chikou"] = close.shift(-ICHIMOKU_SHIFT)

    # --- サイコロジカルライン (12) ---
    up_day = (close.diff() > 0).astype(float)
    up_day[close.diff().isna()] = np.nan
    out["psychological_12"] = up_day.rolling(12).sum() / 12 * 100

    # --- 出来高移動平均 ---
    out["volume_ma_5"] = volume.rolling(5).mean()
    out["volume_ma_25"] = volume.rolling(25).mean()

    return out[["date", *INDICATOR_KEYS]]


def ichimoku_future_cloud(prices: pd.DataFrame) -> list[dict]:
    """最終日より先（1〜ICHIMOKU_SHIFT 本先）に描画される先行スパンA/Bを返す。"""
    df = prices.sort_values("date").reset_index(drop=True)
    high, low = df["high"].astype(float), df["low"].astype(float)
    tenkan = _mid_price(high, low, 9)
    kijun = _mid_price(high, low, 26)
    span_a = ((tenkan + kijun) / 2).tail(ICHIMOKU_SHIFT)
    span_b = _mid_price(high, low, 52).tail(ICHIMOKU_SHIFT)
    return [
        {"offset": i + 1, "senkou_a": _nan_to_none(a), "senkou_b": _nan_to_none(b)}
        for i, (a, b) in enumerate(zip(span_a, span_b))
    ]


def _nan_to_none(value):
    if value is None:
        return None
    value = float(value)
    return None if np.isnan(value) else value


def _crosses(a: pd.Series, b: pd.Series) -> tuple[pd.Series, pd.Series]:
    """a が b を上抜け / 下抜けした日を示すブール Series を返す。"""
    diff = a - b
    prev = diff.shift(1)
    valid = diff.notna() & prev.notna()
    return valid & (prev <= 0) & (diff > 0), valid & (prev >= 0) & (diff < 0)


def detect_signals(indicators: pd.DataFrame) -> list[dict]:
    """売買シグナル候補（クロス・買われすぎ/売られすぎ）を日付昇順で返す。"""
    ind = indicators.reset_index(drop=True)
    rules: list[tuple[pd.Series, str, str, str]] = []  # (該当日, 売買方向, チャート用略称, 名称)

    gc, dc = _crosses(ind["sma_5"], ind["sma_25"])
    rules += [(gc, "buy", "GC", "ゴールデンクロス(5/25)"), (dc, "sell", "DC", "デッドクロス(5/25)")]

    up, down = _crosses(ind["macd"], ind["macd_signal"])
    rules += [(up, "buy", "M+", "MACD買いクロス"), (down, "sell", "M-", "MACD売りクロス")]

    rsi_prev = ind["rsi_14"].shift(1)
    rules += [
        ((rsi_prev < 30) & (ind["rsi_14"] >= 30), "buy", "R30", "RSI 30回復"),
        ((rsi_prev > 70) & (ind["rsi_14"] <= 70), "sell", "R70", "RSI 70割れ"),
    ]

    signals = [
        {"date": ind.at[i, "date"], "direction": direction, "short": short, "label": label}
        for mask, direction, short, label in rules
        for i in ind.index[mask.fillna(False)]
    ]
    return sorted(signals, key=lambda s: s["date"])


def evaluate_latest(prices: pd.DataFrame, indicators: pd.DataFrame) -> list[dict]:
    """最新日の主要指標を判定付きで返す（ダッシュボードのカード表示用）。"""
    if indicators.empty:
        return []
    last = indicators.iloc[-1]
    close = float(prices.sort_values("date")["close"].iloc[-1])

    def v(key):
        return _nan_to_none(last[key])

    def card(key, label, value, status, note, group):
        return {"key": key, "label": label, "value": value, "status": status, "note": note, "group": group}

    cards = []

    sma5, sma25, sma75 = v("sma_5"), v("sma_25"), v("sma_75")
    if None not in (sma5, sma25, sma75):
        if sma5 > sma25 > sma75:
            cards.append(card("sma", "移動平均の並び", sma25, "bull", "5 > 25 > 75 上昇トレンド（パーフェクトオーダー）", "trend"))
        elif sma5 < sma25 < sma75:
            cards.append(card("sma", "移動平均の並び", sma25, "bear", "5 < 25 < 75 下降トレンド", "trend"))
        else:
            cards.append(card("sma", "移動平均の並び", sma25, "neutral", "移動平均が交錯（もみ合い）", "trend"))
    else:
        cards.append(card("sma", "移動平均の並び", sma25, "na", "データ不足（75日分必要）", "trend"))

    dev = v("deviation_25")
    if dev is not None:
        status = "bear" if dev >= 10 else "bull" if dev <= -10 else "neutral"
        note = "上方乖離が大きい（過熱）" if dev >= 10 else "下方乖離が大きい（売られすぎ）" if dev <= -10 else "乖離は通常範囲"
        cards.append(card("deviation_25", "乖離率(25)", dev, status, note, "trend"))

    macd, sig = v("macd"), v("macd_signal")
    if None not in (macd, sig):
        status = "bull" if macd > sig else "bear"
        note = "MACD がシグナルより上" if macd > sig else "MACD がシグナルより下"
        cards.append(card("macd", "MACD", macd, status, f"{note}（シグナル {sig:,.2f}）", "trend"))

    adx, pdi, mdi = v("adx"), v("plus_di"), v("minus_di")
    if None not in (adx, pdi, mdi):
        direction = "上昇" if pdi > mdi else "下降"
        strength = "強いトレンド" if adx >= 25 else "トレンド弱い"
        status = ("bull" if pdi > mdi else "bear") if adx >= 25 else "neutral"
        cards.append(card("adx", "ADX / DMI", adx, status, f"{strength}・{direction}優勢（+DI {pdi:.1f} / -DI {mdi:.1f}）", "trend"))

    tenkan, kijun, sa, sb = v("ichimoku_tenkan"), v("ichimoku_kijun"), v("ichimoku_senkou_a"), v("ichimoku_senkou_b")
    if None not in (sa, sb):
        top, bottom = max(sa, sb), min(sa, sb)
        if close > top:
            status, note = "bull", "株価が雲の上"
        elif close < bottom:
            status, note = "bear", "株価が雲の下"
        else:
            status, note = "neutral", "株価が雲の中"
        if None not in (tenkan, kijun):
            note += "・転換線 > 基準線" if tenkan > kijun else "・転換線 ≦ 基準線"
        cards.append(card("ichimoku", "一目均衡表", kijun, status, note, "trend"))

    r = v("rsi_14")
    if r is not None:
        status = "bear" if r >= 70 else "bull" if r <= 30 else "neutral"
        note = "買われすぎ（70以上）" if r >= 70 else "売られすぎ（30以下）" if r <= 30 else "中立圏"
        cards.append(card("rsi_14", "RSI(14)", r, status, note, "oscillator"))

    k, d = v("stoch_k"), v("stoch_d")
    if None not in (k, d):
        status = "bear" if k >= 80 else "bull" if k <= 20 else "neutral"
        zone = "高値圏（80以上）" if k >= 80 else "安値圏（20以下）" if k <= 20 else "中立圏"
        cards.append(card("stoch", "ストキャス %K", k, status, f"{zone}（%D {d:.1f}）", "oscillator"))

    psy = v("psychological_12")
    if psy is not None:
        status = "bear" if psy >= 75 else "bull" if psy <= 25 else "neutral"
        note = "買われすぎ（75%以上）" if psy >= 75 else "売られすぎ（25%以下）" if psy <= 25 else "中立圏"
        cards.append(card("psychological_12", "サイコロジカル(12)", psy, status, note, "oscillator"))

    pb, bw = v("bb_percent_b"), v("bb_bandwidth")
    if pb is not None:
        status = "bear" if pb >= 1 else "bull" if pb <= 0 else "neutral"
        note = "+2σ超え" if pb >= 1 else "-2σ割れ" if pb <= 0 else "±2σの範囲内"
        if bw is not None:
            note += f"（バンド幅 {bw:.1f}%）"
        cards.append(card("bb", "ボリンジャー %B", pb, status, note, "volatility"))

    atr = v("atr_14")
    if atr is not None:
        cards.append(card("atr_14", "ATR(14)", atr, "neutral", f"終値比 {atr / close * 100:.2f}%", "volatility"))

    return cards
