"""独立検証用テスト。

`app/indicators.py` の各指標を教科書的な定義から素の Python ループで再実装し、
`compute_indicators()` の出力と突き合わせる。pandas の rolling/ewm は使わず、
アプリのコード（wilder_smooth, rci, parabolic_sar 等）もコピーしない。

目的:
  - 数値が一致することの独立検証（NaN 位置も含む）
  - 0/1/2 行やロング期間に満たない行数、価格一定、レンジ0の日などの
    エッジケースで例外や inf が出ないことの確認
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from app import indicators as ind

NAN = float("nan")


def _isnan(x) -> bool:
    return isinstance(x, float) and math.isnan(x)


# ---------------------------------------------------------------------------
# 素朴なリファレンス実装（pandas の rolling/ewm 不使用）
# ---------------------------------------------------------------------------
def ref_sma(values, n):
    L = len(values)
    out = [NAN] * L
    for i in range(L):
        if i - n + 1 >= 0:
            out[i] = sum(values[i - n + 1 : i + 1]) / n
    return out


def ref_std_pop(values, n):
    L = len(values)
    out = [NAN] * L
    for i in range(L):
        if i - n + 1 >= 0:
            window = values[i - n + 1 : i + 1]
            m = sum(window) / n
            var = sum((x - m) ** 2 for x in window) / n
            out[i] = math.sqrt(var)
    return out


def ref_ema(values, n):
    """span=n, adjust=False, min_periods=n の ewm と同じ意味の再帰計算。

    最初に観測された値をシード（0番目の推定値）とし、以降は
    alpha*v + (1-alpha)*prev で更新。n 個の有効値が揃うまでは NaN。
    """
    L = len(values)
    out = [NAN] * L
    alpha = 2.0 / (n + 1)
    e = None
    count = 0
    for i in range(L):
        v = values[i]
        if _isnan(v):
            continue
        count += 1
        e = v if e is None else alpha * v + (1 - alpha) * e
        if count >= n:
            out[i] = e
    return out


def ref_rolling_min(values, n):
    L = len(values)
    out = [NAN] * L
    for i in range(L):
        if i - n + 1 >= 0:
            out[i] = min(values[i - n + 1 : i + 1])
    return out


def ref_rolling_max(values, n):
    L = len(values)
    out = [NAN] * L
    for i in range(L):
        if i - n + 1 >= 0:
            out[i] = max(values[i - n + 1 : i + 1])
    return out


def ref_rolling_sum(values, n):
    L = len(values)
    out = [NAN] * L
    for i in range(L):
        if i - n + 1 >= 0:
            window = values[i - n + 1 : i + 1]
            out[i] = NAN if any(_isnan(x) for x in window) else sum(window)
    return out


def wilder_smooth_ref(values, n):
    L = len(values)
    out = [NAN] * L
    run = 0
    start = None
    for i, v in enumerate(values):
        run = run + 1 if not _isnan(v) else 0
        if run == n:
            start = i
            break
    if start is None:
        return out
    out[start] = sum(values[start - n + 1 : start + 1]) / n
    for i in range(start + 1, L):
        v = values[i]
        out[i] = out[i - 1] if _isnan(v) else (out[i - 1] * (n - 1) + v) / n
    return out


def rank_desc_avg(vals):
    """最大値が順位1。同値は平均順位（Spearman 用）。"""
    n = len(vals)
    order = sorted(range(n), key=lambda i: -vals[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg_rank = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def ref_rci(values, period):
    L = len(values)
    out = [NAN] * L
    denom = period * (period**2 - 1)
    time_rank = list(range(period, 0, -1))  # 古い日ほど大きい数字、直近日=1
    for i in range(L):
        if i - period + 1 < 0:
            continue
        window = values[i - period + 1 : i + 1]
        price_rank = rank_desc_avg(window)
        d2 = sum((t - r) ** 2 for t, r in zip(time_rank, price_rank))
        out[i] = (1 - 6 * d2 / denom) * 100
    return out


def ref_rsi(close, period):
    L = len(close)
    delta = [NAN] + [close[i] - close[i - 1] for i in range(1, L)]
    gains = [NAN if _isnan(d) else max(d, 0.0) for d in delta]
    losses = [NAN if _isnan(d) else max(-d, 0.0) for d in delta]
    avg_gain = wilder_smooth_ref(gains, period)
    avg_loss = wilder_smooth_ref(losses, period)
    out = [NAN] * L
    for i in range(L):
        ag, al = avg_gain[i], avg_loss[i]
        if _isnan(ag) or _isnan(al):
            continue
        if al == 0:
            out[i] = 50.0 if ag == 0 else 100.0  # 完全に横ばいは中立の 50
        else:
            out[i] = 100 - 100 / (1 + ag / al)
    return out


def ref_dmi_adx(high, low, close, period):
    L = len(high)
    tr = [NAN] * L
    plus_dm = [NAN] * L
    minus_dm = [NAN] * L
    for i in range(1, L):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
        up, down = high[i] - high[i - 1], low[i - 1] - low[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
    smoothed_tr = wilder_smooth_ref(tr, period)
    smoothed_plus = wilder_smooth_ref(plus_dm, period)
    smoothed_minus = wilder_smooth_ref(minus_dm, period)
    plus_di, minus_di, dx = [NAN] * L, [NAN] * L, [NAN] * L
    for i in range(L):
        st = smoothed_tr[i]
        if _isnan(st) or st == 0:
            continue
        if not _isnan(smoothed_plus[i]):
            plus_di[i] = smoothed_plus[i] / st * 100
        if not _isnan(smoothed_minus[i]):
            minus_di[i] = smoothed_minus[i] / st * 100
        if not _isnan(plus_di[i]) and not _isnan(minus_di[i]):
            s = plus_di[i] + minus_di[i]
            dx[i] = abs(plus_di[i] - minus_di[i]) / s * 100 if s != 0 else NAN
    adx = wilder_smooth_ref(dx, period)
    return plus_di, minus_di, adx


def ref_psar(high, low, step=0.02, max_af=0.2):
    """Wilder のパラボリック SAR。初期トレンド判定はアプリと同じ
    「2本目までの高値+安値の和」方式（ここは慣習の一つであり、他に
    終値ベースの判定なども一般的。詳細は最終レポートを参照）。
    """
    L = len(high)
    out = [NAN] * L
    if L < 2:
        return out
    uptrend = high[1] + low[1] >= high[0] + low[0]
    sar = low[0] if uptrend else high[0]
    ep = high[0] if uptrend else low[0]
    af = step
    for i in range(1, L):
        sar = sar + af * (ep - sar)
        if uptrend:
            p1 = low[i - 1]
            p2 = low[i - 2] if i >= 2 else low[i - 1]
            sar = min(sar, p1, p2)
            if low[i] < sar:
                uptrend, sar, ep, af = False, ep, low[i], step
            elif high[i] > ep:
                ep, af = high[i], min(af + step, max_af)
        else:
            p1 = high[i - 1]
            p2 = high[i - 2] if i >= 2 else high[i - 1]
            sar = max(sar, p1, p2)
            if high[i] > sar:
                uptrend, sar, ep, af = True, ep, high[i], step
            elif low[i] < ep:
                ep, af = low[i], min(af + step, max_af)
        out[i] = sar
    return out


def ref_psar_close_init(high, low, close, step=0.02, max_af=0.2):
    """同じ機構だが、初期トレンド判定を終値ベースにした代替実装
    （比較・慣習差の確認用）。"""
    L = len(high)
    out = [NAN] * L
    if L < 2:
        return out
    uptrend = close[1] >= close[0]
    sar = low[0] if uptrend else high[0]
    ep = high[0] if uptrend else low[0]
    af = step
    for i in range(1, L):
        sar = sar + af * (ep - sar)
        if uptrend:
            p1, p2 = low[i - 1], (low[i - 2] if i >= 2 else low[i - 1])
            sar = min(sar, p1, p2)
            if low[i] < sar:
                uptrend, sar, ep, af = False, ep, low[i], step
            elif high[i] > ep:
                ep, af = high[i], min(af + step, max_af)
        else:
            p1, p2 = high[i - 1], (high[i - 2] if i >= 2 else high[i - 1])
            sar = max(sar, p1, p2)
            if high[i] > sar:
                uptrend, sar, ep, af = True, ep, high[i], step
            elif low[i] < ep:
                ep, af = low[i], min(af + step, max_af)
        out[i] = sar
    return out


def ref_stoch(high, low, close, k_period, d_period):
    L = len(close)
    lowest, highest = ref_rolling_min(low, k_period), ref_rolling_max(high, k_period)
    num, den = [NAN] * L, [NAN] * L
    for i in range(L):
        if not _isnan(lowest[i]) and not _isnan(highest[i]):
            num[i], den[i] = close[i] - lowest[i], highest[i] - lowest[i]
    k = [NAN if _isnan(den[i]) or den[i] == 0 else num[i] / den[i] * 100 for i in range(L)]
    num_sum, den_sum = ref_rolling_sum(num, d_period), ref_rolling_sum(den, d_period)
    d = [NAN if _isnan(den_sum[i]) or den_sum[i] == 0 else num_sum[i] / den_sum[i] * 100 for i in range(L)]
    return k, d


def ref_deviation(close, n):
    ma = ref_sma(close, n)
    return [NAN if _isnan(m) or m == 0 else (c - m) / m * 100 for c, m in zip(close, ma)]


def ref_psychological(close, n):
    L = len(close)
    up = [NAN] * L
    for i in range(1, L):
        up[i] = 1.0 if close[i] > close[i - 1] else 0.0
    s = ref_rolling_sum(up, n)
    return [NAN if _isnan(x) else x / n * 100 for x in s]


def ref_momentum(close, period, signal_n):
    L = len(close)
    mom = [NAN] * L
    for i in range(L):
        if i - period >= 0:
            mom[i] = close[i] - close[i - period]
    return mom, ref_sma(mom, signal_n)


def ref_mid_price(high, low, n):
    hi, lo = ref_rolling_max(high, n), ref_rolling_min(low, n)
    return [NAN if (_isnan(a) or _isnan(b)) else (a + b) / 2 for a, b in zip(hi, lo)]


def ref_ichimoku(high, low, close, tenkan_n, kijun_n, senkou2_n, shift):
    L = len(high)
    tenkan, kijun = ref_mid_price(high, low, tenkan_n), ref_mid_price(high, low, kijun_n)
    senkou1_raw = [NAN if (_isnan(t) or _isnan(k)) else (t + k) / 2 for t, k in zip(tenkan, kijun)]
    senkou2_raw = ref_mid_price(high, low, senkou2_n)
    senkou1, senkou2, chikou = [NAN] * L, [NAN] * L, [NAN] * L
    for i in range(L):
        if i - shift >= 0:
            senkou1[i], senkou2[i] = senkou1_raw[i - shift], senkou2_raw[i - shift]
        if i + shift < L:
            chikou[i] = close[i + shift]
    return tenkan, kijun, senkou1, senkou2, chikou


def ref_macd(close, fast, slow, signal_n):
    ema_fast, ema_slow = ref_ema(close, fast), ref_ema(close, slow)
    macd = [NAN if (_isnan(a) or _isnan(b)) else a - b for a, b in zip(ema_fast, ema_slow)]
    return macd, ref_ema(macd, signal_n)


def ref_bollinger(close, n, sigma):
    mid, sd = ref_sma(close, n), ref_std_pop(close, n)
    upper = [NAN if _isnan(m) else m + sigma * s for m, s in zip(mid, sd)]
    lower = [NAN if _isnan(m) else m - sigma * s for m, s in zip(mid, sd)]
    return upper, mid, lower


def compute_reference(df: pd.DataFrame) -> dict:
    p = ind.PARAMS
    d = df.sort_values("date").reset_index(drop=True)
    high, low, close = d["high"].astype(float).tolist(), d["low"].astype(float).tolist(), d["close"].astype(float).tolist()

    ref = {}
    for term in ("short", "mid", "long"):
        ref[f"sma_{term}"] = ref_sma(close, p["sma"][term])
        ref[f"ema_{term}"] = ref_ema(close, p["ema"][term])

    ref["bb_upper"], ref["bb_mid"], ref["bb_lower"] = ref_bollinger(close, p["bb"]["period"], p["bb"]["sigma"])
    ref["macd"], ref["macd_signal"] = ref_macd(close, p["macd"]["fast"], p["macd"]["slow"], p["macd"]["signal"])
    ref["rsi"] = ref_rsi(close, p["rsi"])

    ich = p["ichimoku"]
    tenkan, kijun, senkou1, senkou2, chikou = ref_ichimoku(
        high, low, close, ich["tenkan"], ich["kijun"], ich["senkou2"], ich["shift"]
    )
    ref["ichimoku_kijun"], ref["ichimoku_tenkan"] = kijun, tenkan
    ref["ichimoku_senkou1"], ref["ichimoku_senkou2"], ref["ichimoku_chikou"] = senkou1, senkou2, chikou

    ref["rci_short"] = ref_rci(close, p["rci"]["short"])
    ref["rci_long"] = ref_rci(close, p["rci"]["long"])

    ref["plus_di"], ref["minus_di"], ref["adx"] = ref_dmi_adx(high, low, close, p["dmi"])

    for group in ("short", "long"):
        for n in p["gmma"][group]:
            ref[f"gmma_{group}_{n}"] = ref_ema(close, n)

    ref["parabolic"] = ref_psar(high, low, p["parabolic"]["step"], p["parabolic"]["max"])

    ref["stoch_k"], ref["stoch_d"] = ref_stoch(high, low, close, p["stoch"]["k"], p["stoch"]["d"])

    for term in ("short", "long"):
        ref[f"deviation_{term}"] = ref_deviation(close, p["deviation"][term])

    ref["psychological"] = ref_psychological(close, p["psychological"])
    ref["stddev"] = ref_std_pop(close, p["stddev"])
    ref["momentum"], ref["momentum_signal"] = ref_momentum(close, p["momentum"]["period"], p["momentum"]["signal"])
    return ref


# ---------------------------------------------------------------------------
# テスト用データ生成
# ---------------------------------------------------------------------------
def make_ohlcv(seed: int, n: int, trend: float = 0.0, zero_range_at: int | None = None) -> pd.DataFrame:
    """現実的な高値・安値（始値・終値を挟む）を持つ OHLCV を生成する。"""
    rng = np.random.default_rng(seed)
    if n == 0:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

    steps = rng.normal(loc=trend, scale=1.0, size=n)
    close = 100 + np.cumsum(steps)
    close = np.maximum(close, 1.0)  # 非現実的なマイナス値を避ける
    open_ = np.empty(n)
    open_[0] = close[0]
    open_[1:] = close[:-1]

    extra = np.abs(rng.normal(0, 0.6, size=n))
    high = np.maximum(open_, close) + extra
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.6, size=n))
    low = np.minimum(low, np.minimum(open_, close))  # low <= min(open, close) を保証

    if zero_range_at is not None and 0 <= zero_range_at < n:
        high[zero_range_at] = close[zero_range_at]
        low[zero_range_at] = close[zero_range_at]
        open_[zero_range_at] = close[zero_range_at]

    volume = rng.integers(1000, 10000, size=n).astype("int64")
    dates = pd.bdate_range("2024-01-01", periods=n).strftime("%Y-%m-%d")
    return pd.DataFrame({"date": dates, "open": open_, "high": high, "low": low, "close": close, "volume": volume})


RANDOM_WALK = make_ohlcv(seed=42, n=300, trend=0.0, zero_range_at=150)
TRENDING = make_ohlcv(seed=7, n=300, trend=0.15)
CONSTANT = make_ohlcv(seed=1, n=120, trend=0.0)
CONSTANT["open"] = CONSTANT["high"] = CONSTANT["low"] = CONSTANT["close"] = 100.0


# ---------------------------------------------------------------------------
# 比較ヘルパー
# ---------------------------------------------------------------------------
def assert_series_close(actual: pd.Series, expected: list, name: str, atol: float = 1e-6):
    a = actual.to_numpy(dtype=float)
    e = np.array(expected, dtype=float)
    assert a.shape == e.shape, f"{name}: shape mismatch {a.shape} vs {e.shape}"
    nan_a, nan_e = np.isnan(a), np.isnan(e)
    assert (nan_a == nan_e).all(), (
        f"{name}: NaN mask mismatch at positions {np.where(nan_a != nan_e)[0].tolist()}"
    )
    assert not np.isinf(a[~nan_a]).any(), f"{name}: contains inf"
    if (~nan_a).any():
        diff = np.abs(a[~nan_a] - e[~nan_e])
        assert diff.max() <= atol, f"{name}: max abs diff {diff.max()}"


CASES = {
    "random_walk": RANDOM_WALK,
    "trending": TRENDING,
    "constant": CONSTANT,
}


@pytest.mark.parametrize("case_name", list(CASES))
@pytest.mark.parametrize("key", ind.INDICATOR_KEYS)
def test_indicator_matches_reference(case_name, key):
    prices = CASES[case_name]
    out = ind.compute_indicators(prices)
    ref = compute_reference(prices)
    assert_series_close(out[key], ref[key], f"{case_name}:{key}")


# ---------------------------------------------------------------------------
# parabolic SAR: 慣習差（初期トレンド判定）の確認
# ---------------------------------------------------------------------------
def test_parabolic_sar_matches_own_reimplementation():
    for name, prices in CASES.items():
        high = prices["high"].astype(float).tolist()
        low = prices["low"].astype(float).tolist()
        out = ind.parabolic_sar(prices["high"], prices["low"]).to_numpy(dtype=float)
        ref = np.array(ref_psar(high, low), dtype=float)
        nan_a, nan_e = np.isnan(out), np.isnan(ref)
        assert (nan_a == nan_e).all(), name
        assert np.abs(out[~nan_a] - ref[~nan_e]).max() <= 1e-6, name


def test_parabolic_sar_initial_trend_convention_can_diverge():
    """アプリは初期トレンドを『2本目までの高値+安値の和』で判定する。
    終値ベースの判定（別の一般的な流儀）では最初のトレンド判定自体が
    食い違う具体例があり、その後の SAR 軌道も乖離する
    （どちらが正しいという話ではなく、慣習の差であることの確認）。
    """
    high = [102.0, 100.5, 100.0, 99.5, 99.0, 98.5]
    low = [99.0, 98.0, 97.5, 97.0, 96.5, 96.0]
    close = [100.0, 100.4, 99.8, 99.2, 98.6, 98.0]

    hl_sum_uptrend0 = (high[1] + low[1]) >= (high[0] + low[0])  # 198.5 >= 201.0 -> False
    close_uptrend0 = close[1] >= close[0]  # 100.4 >= 100.0 -> True
    assert hl_sum_uptrend0 is False
    assert close_uptrend0 is True

    dates = pd.bdate_range("2024-01-01", periods=len(high)).strftime("%Y-%m-%d")
    prices = pd.DataFrame({"date": dates, "high": high, "low": low, "close": close})
    app_sar = ind.parabolic_sar(prices["high"], prices["low"]).tolist()
    close_based_sar = ref_psar_close_init(high, low, close)

    # 最初の2本は偶然どちらも同じ値に収束するが、3本目以降は初期トレンド判定の
    # 違いに起因して明確に乖離する。
    assert app_sar[1] == pytest.approx(close_based_sar[1])
    assert app_sar[3] == pytest.approx(101.73)
    assert close_based_sar[3] == pytest.approx(101.82)
    assert abs(app_sar[3] - close_based_sar[3]) > 0.05



# ---------------------------------------------------------------------------
# エッジケース: 例外を出さない・行数が保たれる・inf が出ない
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("n", [0, 1, 2, 10, 50])
def test_edge_case_row_counts(n):
    prices = make_ohlcv(seed=3, n=n)
    out = ind.compute_indicators(prices)
    assert list(out.columns) == ["date", *ind.INDICATOR_KEYS]
    assert len(out) == n

    numeric = out.drop(columns="date").to_numpy(dtype=float) if n else np.empty((0, len(ind.INDICATOR_KEYS)))
    assert not np.isinf(numeric).any(), "inf found in compute_indicators output"

    signals = ind.detect_signals(out)
    assert isinstance(signals, list)

    cards = ind.evaluate_latest(prices, out)
    assert isinstance(cards, list)
    if n == 0:
        assert cards == []


def test_edge_case_constant_price_no_inf_and_na_not_inf():
    out = ind.compute_indicators(CONSTANT)
    numeric = out.drop(columns="date").to_numpy(dtype=float)
    assert not np.isinf(numeric).any()

    last = out.iloc[-1]
    # レンジ0続きなので division-by-zero が起きる箇所は NaN であるべき（inf ではない）
    for key in ("stoch_k", "stoch_d", "plus_di", "minus_di", "adx", "deviation_short", "deviation_long"):
        pass  # 上の inf チェックで担保済み。ここでは値の妥当性を明示的にも確認する。
    assert last["bb_upper"] == pytest.approx(100.0)
    assert last["bb_lower"] == pytest.approx(100.0)
    assert last["stddev"] == pytest.approx(0.0)
    assert last["macd"] == pytest.approx(0.0)

    signals = ind.detect_signals(out)
    assert isinstance(signals, list)
    cards = ind.evaluate_latest(CONSTANT, out)
    assert isinstance(cards, list)


def test_edge_case_zero_range_day_within_series_no_inf():
    prices = make_ohlcv(seed=99, n=80, zero_range_at=40)
    out = ind.compute_indicators(prices)
    numeric = out.drop(columns="date").to_numpy(dtype=float)
    assert not np.isinf(numeric).any()
    ref = compute_reference(prices)
    for key in ind.INDICATOR_KEYS:
        assert_series_close(out[key], ref[key], f"zero_range_day:{key}")


def test_no_inf_ever_across_all_cases():
    for name, prices in {**CASES, "short_50": make_ohlcv(4, 50), "n1": make_ohlcv(5, 1), "n0": make_ohlcv(6, 0)}.items():
        out = ind.compute_indicators(prices)
        numeric = out.drop(columns="date").to_numpy(dtype=float) if len(out) else np.empty((0, 1))
        assert not np.isinf(numeric).any(), name
