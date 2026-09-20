"""app/api.py (class Api) 用のテスト。

pywebview から JS へ返る形 {"ok": bool, "data"|"error": ...} を検証する。
ネットワークは使わず FakeFetcher で置き換える（tests/test_service.py の FakeFetcher と同じ方針）。
"""

from __future__ import annotations

import json
import os
import threading

import numpy as np
import pandas as pd
import pytest
from conftest import make_prices

from app.api import Api
from app.database import Database
from app.errors import UserFacingError
from app.fetcher import FetchError, SearchResult
from app.service import StockService
from app.settings import Settings
from app.sources import karauri, taisyaku


class FakeFetcher:
    """symbol ごとに「リモートに今ある全期間データ」を持つフェイク。

    fetch_history(period=...) は全期間、fetch_history(start=...) は start 以降を返す
    （tests/test_service.py の FakeFetcher と同じ挙動をシンボル別に拡張したもの）。
    """

    def __init__(self):
        self.data: dict[str, pd.DataFrame] = {}
        self.currency: dict[str, str | None] = {}
        self.calls: list[dict] = []
        self.fail_once: dict[str, Exception] = {}
        self.search_results = [SearchResult("7203.T", "Toyota", "東証", "EQUITY")]

    def set_prices(self, symbol: str, df: pd.DataFrame, currency: str | None = "JPY") -> None:
        self.data[symbol] = df
        self.currency[symbol] = currency

    def search(self, query):
        return self.search_results

    def fetch_currency(self, symbol):
        return self.currency.get(symbol, "JPY")

    def fetch_history(self, symbol, period=None, start=None):
        self.calls.append({"symbol": symbol, "period": period, "start": start})
        if symbol in self.fail_once:
            exc = self.fail_once.pop(symbol)
            raise exc
        df = self.data.get(symbol)
        if df is None:
            raise FetchError(f"{symbol} の株価データが見つかりませんでした")
        sliced = df if start is None else df[df["date"] >= start]
        return sliced.reset_index(drop=True)


def assert_json_ok(result):
    """pywebview は JSON にシリアライズして JS に渡すので、NaN を含まず必ずシリアライズできること。"""
    json.dumps(result, allow_nan=False)
    return result


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "test.db")
    db.init_schema()
    fetcher = FakeFetcher()
    full = make_prices(100 + np.sin(np.arange(220) / 8) * 10)
    fetcher.set_prices("7203.T", full.iloc[:150])
    fetcher.set_prices("6758.T", full.iloc[:150])
    service = StockService(db, fetcher, tmp_path / "csv", tmp_path / "output")
    api = Api(service)
    return api, service, fetcher, full, tmp_path


# ---------------------------------------------------------------------------
# 1. happy path: 公開メソッド一通り
# ---------------------------------------------------------------------------
def test_search_happy_path(env):
    api, *_ = env
    result = assert_json_ok(api.search("7203"))
    assert result["ok"] is True
    assert result["data"][0]["symbol"] == "7203.T"
    assert result["data"][0]["registered"] is False


def test_register_happy_path(env):
    api, service, fetcher, *_ = env
    result = assert_json_ok(api.register("7203.T", "Toyota", "東証"))
    assert result["ok"] is True
    assert result["data"]["added"] == 150
    assert result["data"]["stock"]["symbol"] == "7203.T"
    assert result["data"]["warnings"] == []
    assert service.db.get_stock("7203.T") is not None


def test_update_happy_path(env):
    api, service, fetcher, full, _ = env
    api.register("7203.T", "Toyota")
    fetcher.set_prices("7203.T", full.iloc[:180])
    result = assert_json_ok(api.update("7203.T"))
    assert result["ok"] is True
    assert result["data"]["added"] == 30


def test_update_all_happy_path(env):
    api, service, fetcher, full, _ = env
    api.register("7203.T", "Toyota")
    api.register("6758.T", "Sony")
    fetcher.set_prices("7203.T", full.iloc[:160])
    fetcher.set_prices("6758.T", full.iloc[:160])
    result = assert_json_ok(api.update_all())
    assert result["ok"] is True
    assert result["data"]["updated"] == 2
    assert result["data"]["errors"] == []


def test_delete_happy_path(env):
    api, service, *_ = env
    api.register("7203.T", "Toyota")
    result = assert_json_ok(api.delete("7203.T"))
    assert result == {"ok": True, "data": True}
    assert service.db.get_stock("7203.T") is None


def test_list_stocks_happy_path(env):
    api, *_ = env
    api.register("7203.T", "Toyota")
    api.register("6758.T", "Sony")
    result = assert_json_ok(api.list_stocks())
    assert result["ok"] is True
    assert {s["symbol"] for s in result["data"]} == {"7203.T", "6758.T"}


def test_dashboard_happy_path(env):
    api, *_ = env
    api.register("7203.T", "Toyota")
    result = assert_json_ok(api.dashboard("7203.T"))
    assert result["ok"] is True
    assert result["data"]["stock"]["symbol"] == "7203.T"
    assert len(result["data"]["chart"]["dates"]) == 150


def test_export_happy_path(env):
    api, *_ = env
    api.register("7203.T", "Toyota")
    result = assert_json_ok(api.export(["7203.T"], "csv", 20))
    assert result["ok"] is True
    assert result["data"]["rows"] == 20
    assert result["data"]["format"] == "csv"


def test_list_exports_happy_path(env):
    api, *_ = env
    api.register("7203.T", "Toyota")
    api.export(["7203.T"], "csv", 10)
    result = assert_json_ok(api.list_exports())
    assert result["ok"] is True
    assert len(result["data"]) == 1


def test_open_csv_folder(env, monkeypatch):
    api, service, *_ = env
    calls = []
    monkeypatch.setattr(os, "startfile", lambda path: calls.append(path), raising=False)
    result = assert_json_ok(api.open_csv_folder())
    assert result["ok"] is True
    assert os.path.isdir(result["data"])
    assert len(calls) == 1


def test_open_output_folder(env, monkeypatch):
    api, service, *_ = env
    calls = []
    monkeypatch.setattr(os, "startfile", lambda path: calls.append(path), raising=False)
    result = assert_json_ok(api.open_output_folder())
    assert result["ok"] is True
    assert os.path.isdir(result["data"])
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 2. JSON シリアライズ可能性（全メソッドの成功結果）
# ---------------------------------------------------------------------------
def test_all_happy_results_are_json_serializable(env):
    api, service, fetcher, full, _ = env
    api.register("7203.T", "Toyota")
    api.register("6758.T", "Sony")
    for result in (
        api.search("toyota"),
        api.list_stocks(),
        api.dashboard("7203.T"),
        api.export(["7203.T", "6758.T"], "markdown", None),
        api.list_exports(),
    ):
        assert result["ok"] is True
        json.dumps(result, allow_nan=False)


# ---------------------------------------------------------------------------
# 3. エラーパス: ok=False, 空でない error 文字列, 例外を投げない
# ---------------------------------------------------------------------------
def assert_error(result):
    assert result["ok"] is False
    assert isinstance(result["error"], str) and result["error"] != ""
    json.dumps(result, allow_nan=False)
    return result


def test_update_unknown_symbol(env):
    api, *_ = env
    assert_error(api.update("NOPE"))


def test_dashboard_unknown_symbol(env):
    api, *_ = env
    assert_error(api.dashboard("NOPE"))


def test_export_unknown_symbol(env):
    api, *_ = env
    assert_error(api.export(["NOPE"], "csv", 20))


def test_delete_unknown_symbol_returns_error(env):
    """存在しない銘柄の削除は「削除できた」ように見せず、エラーを返す。"""
    api, *_ = env
    result = assert_error(api.delete("NOPE"))
    assert "NOPE" in result["error"]


def test_export_empty_symbols(env):
    api, *_ = env
    assert_error(api.export([], "csv", 20))


def test_export_invalid_format(env):
    api, *_ = env
    api.register("7203.T", "Toyota")
    assert_error(api.export(["7203.T"], "xml", 20))


@pytest.mark.parametrize("days", [0, -5, -1, 10001, 1_000_000])
def test_export_days_out_of_range(env, days):
    api, *_ = env
    api.register("7203.T", "Toyota")
    assert_error(api.export(["7203.T"], "csv", days))


def test_export_days_non_numeric_string(env):
    api, *_ = env
    api.register("7203.T", "Toyota")
    assert_error(api.export(["7203.T"], "csv", "abc"))


@pytest.mark.parametrize("days", [20.5, True])
def test_export_days_non_integer_is_rejected(env, days):
    """days=20.5 や True は黙って切り捨て・変換せずエラーにする。"""
    api, *_ = env
    api.register("7203.T", "Toyota")
    assert_error(api.export(["7203.T"], "csv", days))


def test_export_days_integral_float_is_accepted(env):
    """JS から来る 20.0 のような整数値の float は受け付ける。"""
    api, *_ = env
    api.register("7203.T", "Toyota")
    assert api.export(["7203.T"], "csv", 20.0)["data"]["rows"] == 20


def test_export_days_numeric_string_is_accepted(env):
    """days="20" (数字の文字列) は int("20") が成功するため、エラーにはならず通常どおり処理される。"""
    api, *_ = env
    api.register("7203.T", "Toyota")
    result = assert_json_ok(api.export(["7203.T"], "csv", "20"))
    assert result["ok"] is True
    assert result["data"]["rows"] == 20


def test_update_fetcher_raises_fetch_error(env):
    api, service, fetcher, *_ = env
    api.register("7203.T", "Toyota")
    fetcher.fail_once["7203.T"] = FetchError("ネットワークエラー")
    result = assert_error(api.update("7203.T"))
    assert result["error"] == "ネットワークエラー"


def test_update_fetcher_raises_unexpected_error(env):
    api, service, fetcher, *_ = env
    api.register("7203.T", "Toyota")
    fetcher.fail_once["7203.T"] = RuntimeError("boom")
    result = assert_error(api.update("7203.T"))
    assert "boom" in result["error"]
    assert "予期しないエラー" in result["error"]


def test_register_fetcher_raises_fetch_error(env):
    api, service, fetcher, *_ = env
    fetcher.fail_once["9999.T"] = FetchError("見つかりません")
    result = assert_error(api.register("9999.T", "Unknown"))
    assert result["error"] == "見つかりません"
    # 途中で失敗したので銘柄自体も登録されないこと
    assert service.db.get_stock("9999.T") is None


def test_register_fetcher_raises_unexpected_error(env):
    api, service, fetcher, *_ = env
    fetcher.fail_once["9999.T"] = RuntimeError("kaboom")
    result = assert_error(api.register("9999.T", "Unknown"))
    assert "kaboom" in result["error"]


def test_register_with_empty_fetch_result(env):
    """フェッチャーが空(0行)の DataFrame を返した場合、登録はエラーになり DB に何も残らない。

    （以前は CSV 出力で TypeError になり、stocks 行だけが残る不整合があった）
    """
    api, service, fetcher, full, _ = env
    fetcher.set_prices("EMPTY.T", full.iloc[0:0])  # 正しいスキーマだが 0 行
    result = assert_error(api.register("EMPTY.T", "Empty Co"))
    assert "予期しない" not in result["error"]
    assert service.db.get_stock("EMPTY.T") is None


def test_register_rolls_back_when_rebuild_fails(env, monkeypatch):
    api, service, *_ = env

    def boom(symbol):
        raise RuntimeError("disk full")

    monkeypatch.setattr(service, "_rebuild", boom)
    assert_error(api.register("7203.T", "Toyota"))
    assert service.db.get_stock("7203.T") is None
    assert service.db.get_prices("7203.T").empty


# ---------------------------------------------------------------------------
# 4. 変な入力
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("query", ["", "   ", "７２０３", "x" * 5000])
def test_search_odd_inputs_never_raise(env, query):
    api, *_ = env
    result = assert_json_ok(api.search(query))
    assert result["ok"] is True


def test_register_same_symbol_twice_behaves_as_update(env):
    api, service, fetcher, full, _ = env
    first = api.register("7203.T", "Toyota")
    assert first["data"]["added"] == 150

    fetcher.set_prices("7203.T", full.iloc[:170])
    second = assert_json_ok(api.register("7203.T", "Toyota"))
    assert second["ok"] is True
    assert second["data"]["added"] == 20  # update() の差分取得と同じ挙動
    assert len(fetcher.calls) >= 2


def test_delete_then_dashboard_errors(env):
    api, *_ = env
    api.register("7203.T", "Toyota")
    api.delete("7203.T")
    assert_error(api.dashboard("7203.T"))


def test_export_duplicate_symbols_are_deduplicated(env):
    api, *_ = env
    api.register("7203.T", "Toyota")
    result = assert_json_ok(api.export(["7203.T", "7203.T", "7203.T"], "csv", 10))
    assert result["ok"] is True
    assert result["data"]["symbols"] == ["7203.T"]
    assert result["data"]["rows"] == 10  # 3重に数えられていないこと


def test_update_all_partial_failure_collects_errors_and_updates_others(env):
    api, service, fetcher, full, _ = env
    api.register("7203.T", "Toyota")
    api.register("6758.T", "Sony")

    fetcher.set_prices("6758.T", full.iloc[:170])
    fetcher.fail_once["7203.T"] = FetchError("7203 failed")

    result = assert_json_ok(api.update_all())
    assert result["ok"] is True
    assert result["data"]["updated"] == 1
    assert len(result["data"]["errors"]) == 1
    assert "7203.T" in result["data"]["errors"][0]
    # 失敗しなかった銘柄はちゃんと更新されている
    assert len(service.db.get_prices("6758.T")) == 170
    # 失敗した銘柄は元のまま
    assert len(service.db.get_prices("7203.T")) == 150


# ---------------------------------------------------------------------------
# 5. 同時実行スモークテスト
# ---------------------------------------------------------------------------
def test_concurrent_update_same_symbol_is_safe(env):
    api, service, fetcher, full, _ = env
    api.register("7203.T", "Toyota")
    fetcher.set_prices("7203.T", full.iloc[:200])  # 追加データを用意しておく

    errors = []
    results = []
    lock = threading.Lock()

    def worker():
        try:
            r = api.update("7203.T")
            with lock:
                results.append(r)
        except Exception as exc:  # pragma: no cover - 起きてはいけない
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert len(results) == 8
    assert all(r["ok"] is True for r in results)

    prices = service.db.get_prices("7203.T")
    assert len(prices) == 200
    assert prices["date"].is_unique  # 重複行がない (symbol,date) PK のおかげ

    indicators = service.db.get_indicators("7203.T")
    assert len(indicators) == 200


# ---------------------------------------------------------------------------
# 6. 需給データの取得（P2-6）: fetch_short / fetch_taisyaku / estimate_short_all
#    実際の取得ロジックは tests/test_karauri.py・tests/test_taisyaku.py で検証済みなので、
#    ここでは Api が db・settings を正しく渡し、戻り値・エラーを規約どおりに扱うことだけを見る。
# ---------------------------------------------------------------------------
@pytest.fixture
def supply_env(tmp_path):
    db = Database(tmp_path / "supply.db")
    db.init_schema()
    db.upsert_stock("1234.T", "1234", "テスト株式会社", "東証", "JPY")
    fetcher = FakeFetcher()
    service = StockService(db, fetcher, tmp_path / "csv", tmp_path / "output")
    settings = Settings(db, keyring_backend=object())
    api = Api(service, settings=settings)
    return api, db, settings


def test_fetch_short_happy_path(supply_env, monkeypatch):
    api, db, settings = supply_env
    calls = []

    def fake_fetch_one(db_arg, settings_arg, symbol, cancel=None, client=None):
        calls.append((db_arg, settings_arg, symbol))
        return {"symbol": symbol, "status": "ok", "rows": 3, "dates": 2, "since": "2026-09-01"}

    monkeypatch.setattr(karauri, "fetch_one", fake_fetch_one)
    result = assert_json_ok(api.fetch_short("1234.T"))
    assert result["data"] == {"symbol": "1234.T", "status": "ok", "rows": 3, "dates": 2, "since": "2026-09-01"}
    assert calls == [(db, settings, "1234.T")]


def test_fetch_short_propagates_user_facing_error(supply_env, monkeypatch):
    api, *_ = supply_env

    def raise_error(db_arg, settings_arg, symbol, cancel=None, client=None):
        raise UserFacingError("scrape_contact（連絡先）が未設定です")

    monkeypatch.setattr(karauri, "fetch_one", raise_error)
    result = assert_error(api.fetch_short("1234.T"))
    assert "scrape_contact" in result["error"]


def test_fetch_short_unexpected_error_is_wrapped(supply_env, monkeypatch):
    api, *_ = supply_env

    def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(karauri, "fetch_one", boom)
    result = assert_error(api.fetch_short("1234.T"))
    assert "kaboom" in result["error"]
    assert "予期しないエラー" in result["error"]


def test_fetch_short_without_settings_configured(env):
    api, *_ = env  # env の Api は settings=None で作られている
    result = assert_error(api.fetch_short("1234.T"))
    assert "設定" in result["error"]


def test_fetch_taisyaku_happy_path(supply_env, monkeypatch):
    api, db, settings = supply_env
    calls = []

    def fake_fetch_and_save(db_arg, settings_arg=None, symbols=None, cancel=None, client=None):
        calls.append((db_arg, settings_arg))
        return {"saved": 1, "skipped": 0, "date": "2026-09-17", "missing": []}

    monkeypatch.setattr(taisyaku, "fetch_and_save", fake_fetch_and_save)
    result = assert_json_ok(api.fetch_taisyaku())
    assert result["data"] == {"saved": 1, "skipped": 0, "date": "2026-09-17", "missing": []}
    assert calls == [(db, settings)]


def test_fetch_taisyaku_without_settings_configured(env):
    api, *_ = env
    result = assert_error(api.fetch_taisyaku())
    assert "設定" in result["error"]


def test_estimate_short_all_happy_path(supply_env, monkeypatch):
    api, db, settings = supply_env

    def fake_estimate(db_arg, settings_arg, symbols=None):
        assert (db_arg, settings_arg) == (db, settings)
        return {"targets": 1, "skipped": 0, "interval_sec": 10, "eta_sec": 10, "contact_ok": True}

    monkeypatch.setattr(karauri, "estimate", fake_estimate)
    result = assert_json_ok(api.estimate_short_all())
    assert result["data"] == {"targets": 1, "skipped": 0, "interval_sec": 10, "eta_sec": 10, "contact_ok": True}


def test_estimate_short_all_passes_symbols_through(supply_env, monkeypatch):
    api, *_ = supply_env
    received = []

    def fake_estimate(db_arg, settings_arg, symbols=None):
        received.append(symbols)
        return {"targets": 0, "skipped": 0, "interval_sec": 10, "eta_sec": 0, "contact_ok": True}

    monkeypatch.setattr(karauri, "estimate", fake_estimate)
    api.estimate_short_all(["1234.T"])
    assert received == [["1234.T"]]


def test_estimate_short_all_without_settings_configured(env):
    api, *_ = env
    result = assert_error(api.estimate_short_all())
    assert "設定" in result["error"]
