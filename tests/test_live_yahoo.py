"""Yahoo! Finance への実ネットワークアクセスを伴う統合テスト。

通常の `pytest` 実行ではスキップされる。実行するには環境変数
AUTOTECHNICAL_LIVE=1 を設定すること:

    AUTOTECHNICAL_LIVE=1 python -m pytest tests/test_live_yahoo.py -v -s

app/ や web/ のコードは変更しない。DB・CSV・出力先はすべてこのファイル専用の
一時ディレクトリ（プロジェクトの data/ や output/ とは別）を使う。
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.database import Database
from app.fetcher import FetchError, YahooFetcher
from app.service import StockService

LIVE = os.environ.get("AUTOTECHNICAL_LIVE") == "1"
skip_unless_live = pytest.mark.skipif(not LIVE, reason="set AUTOTECHNICAL_LIVE=1 to run live Yahoo Finance tests")

SLOW_THRESHOLD_S = 15.0


def _timed(label, fn, *args, **kwargs):
    start = time.perf_counter()
    try:
        return fn(*args, **kwargs)
    finally:
        elapsed = time.perf_counter() - start
        flag = "  <<< SLOW" if elapsed > SLOW_THRESHOLD_S else ""
        print(f"[TIMING] {label}: {elapsed:.2f}s{flag}")


@pytest.fixture(scope="module")
def fetcher():
    return YahooFetcher()


@pytest.fixture
def service_env(tmp_path):
    """StockService backed by a throwaway temp dir (never the project's data/ or output/)."""
    root = tmp_path
    db = Database(root / "db" / "autotechnical.db")
    db.init_schema()
    fetcher = YahooFetcher()
    service = StockService(db, fetcher, root / "csv", root / "output")
    return service


# ---------------------------------------------------------------------------
# 1. search
# ---------------------------------------------------------------------------
@skip_unless_live
class TestSearch:
    def test_numeric_jp_code_puts_tosho_first(self, fetcher):
        results = _timed("search('7203')", fetcher.search, "7203")
        assert results, "expected at least one search result for 7203"
        print("[DATA] 7203 ->", [r.symbol for r in results[:5]])
        assert results[0].symbol == "7203.T"

    def test_fullwidth_jp_code(self, fetcher):
        results = _timed("search('７２０３')", fetcher.search, "７２０３")
        assert results, "expected at least one search result for full-width 7203"
        print("[DATA] full-width 7203 ->", [r.symbol for r in results[:5]])
        assert results[0].symbol == "7203.T"

    def test_new_alphanumeric_tosho_code(self, fetcher):
        # 285A is a newer-style Tokyo code (3 digits + letter). Search may or may not
        # surface it directly; the fetcher's _lookup_symbol fallback should catch it
        # if the search API misses it.
        results = _timed("search('285A')", fetcher.search, "285A")
        print("[DATA] 285A ->", [(r.symbol, r.name, r.exchange) for r in results[:5]])
        if results:
            assert results[0].symbol == "285A.T"
        else:
            print("[DATA] 285A: no results at all (search miss AND lookup fallback miss)")

    def test_us_ticker(self, fetcher):
        results = _timed("search('AAPL')", fetcher.search, "AAPL")
        assert results, "expected at least one search result for AAPL"
        print("[DATA] AAPL ->", [r.symbol for r in results[:5]])
        assert any(r.symbol == "AAPL" for r in results)

    def test_english_name(self, fetcher):
        results = _timed("search('sony')", fetcher.search, "sony")
        assert results, "expected at least one search result for 'sony'"
        print("[DATA] sony ->", [(r.symbol, r.name) for r in results[:5]])
        assert any("sony" in (r.name or "").lower() or "SONY" in r.symbol for r in results)

    def test_garbage_query_returns_empty_without_raising(self, fetcher):
        results = _timed("search('ZZZZZZ999')", fetcher.search, "ZZZZZZ999")
        print("[DATA] ZZZZZZ999 ->", results)
        assert results == []


# ---------------------------------------------------------------------------
# 2 & 3. fetch_history / fetch_currency
# ---------------------------------------------------------------------------
def _assert_valid_price_frame(df: pd.DataFrame, *, min_rows=1, max_rows=None):
    assert not df.empty
    if max_rows is not None:
        assert min_rows <= len(df) <= max_rows, f"row count {len(df)} not in [{min_rows}, {max_rows}]"

    dates = list(df["date"])
    assert dates == sorted(dates), "dates are not ascending"
    assert len(dates) == len(set(dates)), "duplicate dates found"
    for d in dates:
        assert len(d) == 10 and d[4] == "-" and d[7] == "-", f"not ISO date: {d}"

    for col in ("open", "high", "low", "close"):
        assert not df[col].isna().any(), f"NaN found in {col}"

    tol = 1e-6
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    opens = df["open"].to_numpy()
    closes = df["close"].to_numpy()
    assert (highs >= np.maximum(opens, closes) - tol).all(), "high < max(open, close) somewhere"
    assert (lows <= np.minimum(opens, closes) + tol).all(), "low > min(open, close) somewhere"

    assert (df["volume"] >= 0).all()
    for v in df["volume"]:
        assert float(v) == int(v), f"volume not integral: {v}"


@skip_unless_live
class TestFetchHistory:
    @pytest.mark.parametrize("symbol", ["7203.T", "AAPL"])
    def test_one_year_history(self, fetcher, symbol):
        df = _timed(f"fetch_history({symbol}, period=1y)", fetcher.fetch_history, symbol, period="1y")
        print(f"[DATA] {symbol} 1y rows={len(df)} first={df['date'].iloc[0]} last={df['date'].iloc[-1]}")
        zero_vol = int((df["volume"] == 0).sum())
        if zero_vol:
            print(f"[DATA] {symbol}: {zero_vol} rows with zero volume")
        _assert_valid_price_frame(df, min_rows=235, max_rows=265)

    @pytest.mark.parametrize("symbol", ["7203.T", "AAPL"])
    def test_start_param_filters_to_recent(self, fetcher, symbol):
        start = (pd.Timestamp.today() - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
        df = _timed(f"fetch_history({symbol}, start={start})", fetcher.fetch_history, symbol, start=start)
        print(f"[DATA] {symbol} since {start} rows={len(df)} dates={list(df['date'])}")
        _assert_valid_price_frame(df)
        assert all(d >= start for d in df["date"])

    def test_currency_jpy(self, fetcher):
        currency = _timed("fetch_currency(7203.T)", fetcher.fetch_currency, "7203.T")
        print("[DATA] 7203.T currency ->", currency)
        assert currency == "JPY"

    def test_currency_usd(self, fetcher):
        currency = _timed("fetch_currency(AAPL)", fetcher.fetch_currency, "AAPL")
        print("[DATA] AAPL currency ->", currency)
        assert currency == "USD"


# ---------------------------------------------------------------------------
# 4. full service flow
# ---------------------------------------------------------------------------
@skip_unless_live
class TestServiceFlow:
    def test_register_update_dashboard_export_delete(self, service_env):
        service = service_env

        reg_toyota = _timed("service.register(7203.T)", service.register, "7203.T", "Toyota", "東証")
        reg_apple = _timed("service.register(AAPL)", service.register, "AAPL", "Apple", "NASDAQ")
        print("[DATA] register 7203.T added=", reg_toyota["added"], "warnings=", reg_toyota["warnings"])
        print("[DATA] register AAPL added=", reg_apple["added"], "warnings=", reg_apple["warnings"])
        assert reg_toyota["added"] > 0
        assert reg_apple["added"] > 0

        rows_before = {s: len(service.db.get_prices(s)) for s in ("7203.T", "AAPL")}

        upd_toyota = _timed("service.update(7203.T)", service.update, "7203.T")
        upd_apple = _timed("service.update(AAPL)", service.update, "AAPL")
        print("[DATA] update 7203.T added=", upd_toyota["added"])
        print("[DATA] update AAPL added=", upd_apple["added"])
        # 直後の再更新なので追加は0件、あっても数件（前日分の確定など）で重複はしない
        assert upd_toyota["added"] in (0, 1)
        assert upd_apple["added"] in (0, 1)
        assert len(service.db.get_prices("7203.T")) == rows_before["7203.T"] + upd_toyota["added"]
        assert len(service.db.get_prices("AAPL")) == rows_before["AAPL"] + upd_apple["added"]
        for symbol in ("7203.T", "AAPL"):
            dates = list(service.db.get_prices(symbol)["date"])
            assert len(dates) == len(set(dates)), f"duplicate dates after update for {symbol}"

        for symbol in ("7203.T", "AAPL"):
            dash = _timed(f"service.dashboard({symbol})", service.dashboard, symbol)
            encoded = json.dumps(dash, allow_nan=False)
            assert encoded
            stored_close = float(service.db.get_prices(symbol)["close"].iloc[-1])
            assert math.isclose(dash["quote"]["close"], stored_close, rel_tol=1e-9)
            print(f"[DATA] {symbol} dashboard quote=", dash["quote"])

        for fmt, expected_ext in (("csv", ".csv"), ("markdown", ".md")):
            result = _timed(f"service.export(csv+aapl, {fmt})", service.export, ["7203.T", "AAPL"], fmt, 60)
            path = Path(result["path"])
            print(f"[DATA] export {fmt} ->", result)
            assert path.exists() and path.suffix == expected_ext
            assert path.stat().st_size > 0
            assert result["rows"] == 120  # 60 days x 2 symbols

            if fmt == "csv":
                df = pd.read_csv(path)
                assert len(df) == 120
                assert set(df["symbol"]) == {"7203.T", "AAPL"}
                assert df.groupby("symbol").size().to_dict() == {"7203.T": 60, "AAPL": 60}

        _timed("service.delete(AAPL)", service.delete, "AAPL")
        assert service.db.get_stock("AAPL") is None
        from app.csv_export import csv_paths

        for p in csv_paths(service.csv_dir, "AAPL").values():
            assert not p.exists()
        # 7203.T is left in place; nothing else should have been touched
        assert service.db.get_stock("7203.T") is not None


# ---------------------------------------------------------------------------
# 5. error behaviour
# ---------------------------------------------------------------------------
@skip_unless_live
class TestErrors:
    def test_fetch_history_invalid_symbol_raises_fetch_error(self, fetcher, capsys):
        start = time.perf_counter()
        with pytest.raises(FetchError):
            fetcher.fetch_history("ZZZZZZ999.T", period="1y")
        elapsed = time.perf_counter() - start
        print(f"[TIMING] fetch_history(ZZZZZZ999.T) raised in {elapsed:.2f}s")
        assert elapsed < SLOW_THRESHOLD_S, "invalid-symbol fetch took too long / may have hung"
        captured = capsys.readouterr()
        if captured.out or captured.err:
            print("[DATA] yfinance stdout for invalid symbol:", repr(captured.out[:500]))
            print("[DATA] yfinance stderr for invalid symbol:", repr(captured.err[:500]))

    def test_register_invalid_symbol_raises_cleanly(self, service_env):
        service = service_env
        with pytest.raises((FetchError, ValueError)):
            _timed("service.register(ZZZZZZ999.T)", service.register, "ZZZZZZ999.T", "Bogus")
        assert service.db.get_stock("ZZZZZZ999.T") is None
