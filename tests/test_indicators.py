import numpy as np
import pandas as pd
import pytest

from app import indicators as ind

# J. Welles Wilder の RSI 計算例として広く使われているデータ（StockCharts の解説と同じ系列）
WILDER_CLOSES = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
    45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
]
WILDER_RSI = [70.53, 66.32, 66.55, 69.41, 66.36, 57.97]


def _rsi_by_hand(closes, period=14):
    deltas = np.diff(closes)
    gains, losses = np.clip(deltas, 0, None), np.clip(-deltas, 0, None)
    avg_gain, avg_loss = gains[:period].mean(), losses[:period].mean()
    values = [100 - 100 / (1 + avg_gain / avg_loss)]
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        values.append(100 - 100 / (1 + avg_gain / avg_loss))
    return np.array(values)


def test_rsi_matches_wilder_reference():
    result = ind.rsi(pd.Series(WILDER_CLOSES), 14)
    assert result.iloc[:14].isna().all()
    np.testing.assert_allclose(result.iloc[14:].to_numpy(), _rsi_by_hand(WILDER_CLOSES), atol=1e-9)
    # 公開されている値は途中の平均を丸めて計算しているため僅かにずれる
    np.testing.assert_allclose(result.iloc[14:].to_numpy(), WILDER_RSI, atol=0.1)


def test_rsi_all_gains_is_100():
    result = ind.rsi(pd.Series(np.arange(1, 31, dtype=float)), 14)
    assert result.dropna().eq(100).all()


def test_wilder_smooth_starts_with_simple_mean():
    s = pd.Series([np.nan, 1.0, 2.0, 3.0, 4.0])
    out = ind.wilder_smooth(s, 3)
    assert np.isnan(out.iloc[2])
    assert out.iloc[3] == pytest.approx(2.0)
    assert out.iloc[4] == pytest.approx((2.0 * 2 + 4.0) / 3)


def test_compute_indicators_columns_and_moving_averages(prices_factory):
    prices = prices_factory(np.arange(1, 101, dtype=float))
    out = ind.compute_indicators(prices)

    assert list(out.columns) == ["date", *ind.INDICATOR_KEYS]
    assert len(out) == 100
    assert np.isnan(out["sma_5"].iloc[3])
    assert out["sma_5"].iloc[4] == pytest.approx(3.0)
    assert out["sma_25"].iloc[-1] == pytest.approx(np.mean(np.arange(76, 101)))
    assert out["sma_75"].notna().sum() == 26


def test_constant_price_gives_flat_indicators(prices_factory):
    out = ind.compute_indicators(prices_factory([100.0] * 120))
    last = out.iloc[-1]
    assert last["bb_upper_2"] == pytest.approx(100.0)
    assert last["bb_lower_2"] == pytest.approx(100.0)
    assert last["macd"] == pytest.approx(0.0)
    assert last["deviation_25"] == pytest.approx(0.0)
    assert last["atr_14"] == pytest.approx(2.0)  # high-low = 2
    assert last["ichimoku_tenkan"] == pytest.approx(100.0)


def test_ichimoku_spans_are_shifted(prices_factory):
    prices = prices_factory(np.arange(1, 121, dtype=float))
    out = ind.compute_indicators(prices)
    high, low = prices["high"], prices["low"]
    tenkan = (high.rolling(9).max() + low.rolling(9).min()) / 2
    kijun = (high.rolling(26).max() + low.rolling(26).min()) / 2

    i = 80
    assert out["ichimoku_senkou_a"].iloc[i] == pytest.approx(((tenkan + kijun) / 2).iloc[i - ind.ICHIMOKU_SHIFT])
    assert out["ichimoku_chikou"].iloc[i] == pytest.approx(prices["close"].iloc[i + ind.ICHIMOKU_SHIFT])
    assert out["ichimoku_chikou"].iloc[-ind.ICHIMOKU_SHIFT:].isna().all()

    cloud = ind.ichimoku_future_cloud(prices)
    assert len(cloud) == ind.ICHIMOKU_SHIFT
    assert cloud[-1]["senkou_a"] == pytest.approx(((tenkan + kijun) / 2).iloc[-1])


def test_stochastics_bounds(prices_factory):
    rng = np.random.default_rng(0)
    out = ind.compute_indicators(prices_factory(100 + rng.normal(0, 2, 200).cumsum()))
    for key in ("stoch_k", "stoch_d", "stoch_slow_d", "rsi_14", "adx", "psychological_12"):
        values = out[key].dropna()
        assert ((values >= 0) & (values <= 100)).all(), key


def test_detect_golden_and_dead_cross(prices_factory):
    closes = [100.0] * 40 + [110.0] * 20 + [90.0] * 20
    out = ind.compute_indicators(prices_factory(closes))
    signals = ind.detect_signals(out)
    shorts = [s["short"] for s in signals]
    assert "GC" in shorts and "DC" in shorts
    assert shorts.index("GC") < shorts.index("DC")


def test_evaluate_latest_uptrend(prices_factory):
    prices = prices_factory(np.linspace(100, 200, 150))
    cards = {c["key"]: c for c in ind.evaluate_latest(prices, ind.compute_indicators(prices))}
    assert cards["sma"]["status"] == "bull"
    assert cards["ichimoku"]["status"] == "bull"
    assert cards["rsi_14"]["status"] == "bear"  # 上昇一辺倒で買われすぎ
