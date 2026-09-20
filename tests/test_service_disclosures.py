"""銘柄の登録・削除と開示（disclosures）の連動（P4-3）。

`app.disclosures.scan_cache` / `cleanup_orphans` はネットワークを使わずローカルキャッシュだけを
読む前提の関数なので、ここでは monkeypatch でフェイクに差し替えてテストする。
"""

import numpy as np
import pandas as pd
import pytest
from conftest import make_prices

from app import disclosures
from app.database import Database
from app.fetcher import FetchError, SearchResult
from app.service import StockService


class FakeFetcher:
    """ネットワークを使わずに yfinance の代わりをする（tests/test_service.py と同じ形）。"""

    def __init__(self, prices: pd.DataFrame):
        self.prices = prices
        self.calls = []

    def search(self, query):
        return [SearchResult("7203.T", "Toyota", "東証", "EQUITY")]

    def fetch_currency(self, symbol):
        return "JPY"

    def fetch_history(self, symbol, period=None, start=None):
        self.calls.append({"period": period, "start": start})
        df = self.prices if start is None else self.prices[self.prices["date"] >= start]
        if df.empty:
            raise FetchError("no data")
        return df.reset_index(drop=True)


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "test.db")
    db.init_schema()
    prices = make_prices(100 + np.sin(np.arange(260) / 8) * 10)
    fetcher = FakeFetcher(prices.iloc[:200])
    service = StockService(db, fetcher, tmp_path / "csv", tmp_path / "output")
    return service, fetcher, prices, tmp_path


def test_register_new_stock_calls_scan_cache_once(env, monkeypatch):
    service, *_ = env
    calls = []

    def fake_scan_cache(db, *, symbols=None, dates=None, base_dir=None):
        calls.append({"db": db, "symbols": symbols, "dates": dates, "base_dir": base_dir})
        return {"dates": 0, "documents": 0, "links": 0}

    monkeypatch.setattr(disclosures, "scan_cache", fake_scan_cache)

    service.register("7203.T", "Toyota")

    assert len(calls) == 1
    assert calls[0]["symbols"] == ["7203.T"]
    assert calls[0]["db"] is service.db


def test_register_existing_stock_delegates_to_update_and_skips_scan_cache(env, monkeypatch):
    service, fetcher, prices, _ = env
    calls = []
    monkeypatch.setattr(disclosures, "scan_cache", lambda *a, **k: calls.append(1))

    service.register("7203.T", "Toyota")
    assert len(calls) == 1  # 新規登録の分

    calls.clear()
    fetcher.prices = prices.iloc[:230]
    result = service.register("7203.T", "Toyota")  # 既に登録済みなので update() に委譲される

    assert calls == []
    assert result["added"] == 30  # update() の戻り値がそのまま返っている


def test_register_succeeds_even_if_scan_cache_raises(env, monkeypatch):
    service, *_ = env

    def boom(db, *, symbols=None, dates=None, base_dir=None):
        raise RuntimeError("cache broken")

    monkeypatch.setattr(disclosures, "scan_cache", boom)

    result = service.register("7203.T", "Toyota")

    assert result["added"] == 200
    assert result["stock"]["symbol"] == "7203.T"
    assert "7203.T" in {s["symbol"] for s in service.list_stocks()}
    assert len(service.db.get_indicators("7203.T")) == 200


def test_register_return_shape_unchanged(env, monkeypatch):
    service, *_ = env
    monkeypatch.setattr(disclosures, "scan_cache", lambda *a, **k: {"dates": 0, "documents": 0, "links": 0})

    result = service.register("7203.T", "Toyota")

    assert set(result.keys()) == {"stock", "added", "warnings"}


def test_delete_calls_cleanup_orphans(env, monkeypatch):
    service, *_ = env
    monkeypatch.setattr(disclosures, "scan_cache", lambda *a, **k: None)
    service.register("7203.T", "Toyota")

    calls = []
    monkeypatch.setattr(disclosures, "cleanup_orphans", lambda db: calls.append(db) or 0)

    service.delete("7203.T")

    assert len(calls) == 1
    assert calls[0] is service.db


def test_delete_succeeds_even_if_cleanup_orphans_raises(env, monkeypatch):
    service, *_ = env
    monkeypatch.setattr(disclosures, "scan_cache", lambda *a, **k: None)
    service.register("7203.T", "Toyota")

    def boom(db):
        raise RuntimeError("cleanup broken")

    monkeypatch.setattr(disclosures, "cleanup_orphans", boom)

    result = service.delete("7203.T")

    assert result is None
    assert service.db.get_stock("7203.T") is None


def test_delete_does_not_remove_fetch_log(env, monkeypatch):
    service, *_ = env
    monkeypatch.setattr(disclosures, "scan_cache", lambda *a, **k: None)
    monkeypatch.setattr(disclosures, "cleanup_orphans", lambda db: 0)
    service.register("7203.T", "Toyota")

    service.db.log_fetch("edinet", "2026-09-18")

    service.delete("7203.T")

    assert service.db.get_fetch("edinet", "2026-09-18") is not None
