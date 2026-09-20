import json

import numpy as np
import pandas as pd
import pytest
from conftest import make_prices

from app.database import Database
from app.fetcher import FetchError, SearchResult, code_from_symbol, normalize_query
from app.indicators import INDICATOR_KEYS
from app.service import StockService


class FakeFetcher:
    """ネットワークを使わずに yfinance の代わりをする。"""

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


def test_register_saves_prices_indicators_and_csv(env):
    service, fetcher, _, tmp_path = env
    result = service.register("7203.T", "Toyota", "東証")

    assert result["added"] == 200
    assert fetcher.calls[0] == {"period": "1y", "start": None}
    stock = service.db.get_stock("7203.T")
    assert stock["code"] == "7203" and stock["currency"] == "JPY" and stock["row_count"] == 200
    assert len(service.db.get_indicators("7203.T")) == 200

    prices_csv = pd.read_csv(tmp_path / "csv" / "7203.T_株価.csv", encoding="utf-8-sig")
    assert list(prices_csv.columns) == ["日付", "始値", "高値", "安値", "終値", "出来高"]
    stored = service.db.get_prices("7203.T")
    assert prices_csv["日付"].iloc[0] == stored["date"].iloc[-1].replace("-", "/")  # 新しい日付が先頭
    indicators_csv = pd.read_csv(tmp_path / "csv" / "7203.T_テクニカル指標.csv", encoding="utf-8-sig")
    assert len(indicators_csv) == 200 and "RSI 中期(14)" in indicators_csv.columns
    assert list(indicators_csv.columns[:2]) == ["日付", "終値"]
    assert len(indicators_csv.columns) == len(INDICATOR_KEYS) + 2

    assert service.search("7203")[0]["registered"] is True


def test_update_fetches_from_last_date(env):
    service, fetcher, prices, _ = env
    service.register("7203.T", "Toyota")
    last = service.db.get_price_range("7203.T")[1]

    fetcher.prices = prices.iloc[:230]
    result = service.update("7203.T")

    assert fetcher.calls[-1] == {"period": None, "start": last}
    assert result["added"] == 30
    assert len(service.db.get_indicators("7203.T")) == 230


def test_update_refetches_everything_after_split(env):
    service, fetcher, prices, _ = env
    service.register("7203.T", "Toyota")
    first = service.db.get_price_range("7203.T")[0]

    adjusted = prices.iloc[:210].copy()
    adjusted[["open", "high", "low", "close"]] /= 2
    adjusted.loc[205, "splits"] = 2.0
    fetcher.prices = adjusted
    service.update("7203.T")

    assert fetcher.calls[-1]["start"] == first
    stored = service.db.get_prices("7203.T")
    assert stored["close"].iloc[0] == pytest.approx(adjusted["close"].iloc[0])


def test_dashboard_payload_is_json_serializable(env):
    service, *_ = env
    service.register("7203.T", "Toyota")
    data = service.dashboard("7203.T")

    json.dumps(data, allow_nan=False)
    assert len(data["chart"]["dates"]) == 200
    assert len(data["chart"]["future_cloud"]) == 25
    assert data["chart"]["future_cloud"][0]["date"] > data["chart"]["dates"][-1]
    assert data["table"]["rows"][0]["date"] == data["chart"]["dates"][-1]


def test_dashboard_chart_keys_unchanged(env):
    service, *_ = env
    service.register("7203.T", "Toyota")
    data = service.dashboard("7203.T")

    existing_keys = {
        "dates", "open", "high", "low", "close", "volume",
        "indicators", "params", "future_cloud", "signals",
    }
    assert set(data["chart"].keys()) == existing_keys | {"short", "taisyaku"}


def test_dashboard_short_unavailable_when_no_rows(env):
    service, *_ = env
    service.register("7203.T", "Toyota")
    data = service.dashboard("7203.T")

    assert data["chart"]["short"] == {
        "available": False,
        "reason": "0.5% 以上の報告なし、または未取得",
        "points": [],
    }


def test_dashboard_short_points_filter_and_carry(env):
    service, *_ = env
    service.register("7203.T", "Toyota")
    prices = service.db.get_prices("7203.T")
    d0, d1, d_last = prices["date"].iloc[0], prices["date"].iloc[5], prices["date"].iloc[-1]
    missing_date = "2099-01-01"  # 足の無い日付。捨てられるはず

    with service.db.write() as conn:
        # わざと日付の降順で入れて、出力が昇順に並び替わることを確認する
        conn.executemany(
            "INSERT INTO short_totals (symbol, date, total_ratio, total_qty, holders) VALUES (?, ?, ?, ?, ?)",
            [
                ("7203.T", missing_date, 9.9, 1, 1),
                ("7203.T", d1, 2.5, 100000, 3),
                ("7203.T", d0, 1.0, 50000, 1),
            ],
        )

    data = service.dashboard("7203.T")
    short = data["chart"]["short"]

    assert short["available"] is True and short["reason"] is None
    assert [p["date"] for p in short["points"]] == [d0, d1, d_last]
    assert short["points"][0] == {"date": d0, "ratio": 1.0, "qty": 50000, "holders": 1, "carried": False}
    assert short["points"][1]["carried"] is False
    # 最後の報告日 (d1) が最新の足 (d_last) より前なので、据え置きの点が1つ足される
    carried = short["points"][-1]
    assert carried == {"date": d_last, "ratio": 2.5, "qty": 100000, "holders": 3, "carried": True}


def test_dashboard_short_no_carry_when_last_report_is_latest_bar(env):
    service, *_ = env
    service.register("7203.T", "Toyota")
    d_last = service.db.get_prices("7203.T")["date"].iloc[-1]

    with service.db.write() as conn:
        conn.execute(
            "INSERT INTO short_totals (symbol, date, total_ratio, total_qty, holders) VALUES (?, ?, ?, ?, ?)",
            ("7203.T", d_last, 3.3, 200, 2),
        )

    short = service.dashboard("7203.T")["chart"]["short"]
    assert len(short["points"]) == 1
    assert short["points"][0]["carried"] is False


def test_dashboard_taisyaku_unavailable_when_no_rows(env):
    service, *_ = env
    service.register("7203.T", "Toyota")
    data = service.dashboard("7203.T")

    assert data["chart"]["taisyaku"] == {
        "available": False,
        "reason": "貸借銘柄ではない、または未取得",
        "points": [],
    }


def test_dashboard_taisyaku_points_no_carry_and_gaps_kept(env):
    service, *_ = env
    service.register("7203.T", "Toyota")
    prices = service.db.get_prices("7203.T")
    d0, d1, d2 = prices["date"].iloc[0], prices["date"].iloc[1], prices["date"].iloc[3]  # index2 は欠測にする
    missing_date = "2099-01-01"

    with service.db.write() as conn:
        conn.executemany(
            "INSERT INTO margin_balances "
            "(symbol, date, kind, yushi_balance, kashi_balance, net_balance, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("7203.T", d0, "final", 1000, 500, 500, "2026-01-01T00:00:00"),
                ("7203.T", d1, "prelim", 1100, 520, 580, "2026-01-02T00:00:00"),
                ("7203.T", d2, "final", 900, 600, 300, "2026-01-04T00:00:00"),
                ("7203.T", missing_date, "final", 1, 1, 0, "2026-01-05T00:00:00"),
            ],
        )

    taisyaku = service.dashboard("7203.T")["chart"]["taisyaku"]

    assert taisyaku["available"] is True and taisyaku["reason"] is None
    assert [p["date"] for p in taisyaku["points"]] == [d0, d1, d2]  # 昇順・欠測日は埋めない・missing_date は捨てる
    assert [p["kind"] for p in taisyaku["points"]] == ["final", "prelim", "final"]
    assert all("carried" not in p for p in taisyaku["points"])  # 貸借には据え置きを入れない
    assert taisyaku["points"][0] == {"date": d0, "yushi": 1000, "kashi": 500, "net": 500, "kind": "final"}
    # 最新の足の日付は取得していないので、据え置きの点は増えない
    assert taisyaku["points"][-1]["date"] != prices["date"].iloc[-1]


def test_delete_removes_db_rows_and_csv(env):
    service, _, _, tmp_path = env
    service.register("7203.T", "Toyota")
    service.delete("7203.T")

    assert service.db.get_stock("7203.T") is None
    assert service.db.get_prices("7203.T").empty
    assert service.db.get_indicators("7203.T").empty
    assert not (tmp_path / "csv" / "7203.T_株価.csv").exists()


def test_export_csv_and_markdown(env):
    service, fetcher, prices, tmp_path = env
    service.register("7203.T", "Toyota")
    service.register("6758.T", "Sony")

    res = service.export(["7203.T", "6758.T"], "csv", 20)
    df = pd.read_csv(res["path"])
    assert res["rows"] == 40 and len(df) == 40
    assert list(df.columns[:4]) == ["symbol", "name", "currency", "date"]
    assert {"sma_25", "rsi_14", "parabolic_sar", "gmma_long_ema_60"} <= set(df.columns)
    assert df["date"].is_monotonic_increasing is False  # 2銘柄連結
    assert df[df["symbol"] == "7203.T"]["date"].is_monotonic_increasing

    md = service.export(["7203.T"], "markdown", None)
    text = open(md["path"], encoding="utf-8").read()
    assert "## indicator_definitions" in text and "## 7203.T Toyota" in text
    assert "```csv\ndate,open,high,low,close,volume,sma_5" in text

    files = service.list_exports()
    assert {f["name"] for f in files} == {res["name"], md["name"]}


@pytest.mark.parametrize(
    "symbols, fmt, days",
    [([], "csv", 20), (["7203.T"], "xml", 20), (["NOPE"], "csv", 20), (["7203.T"], "csv", 0)],
)
def test_export_rejects_invalid_requests(env, symbols, fmt, days):
    service, *_ = env
    service.register("7203.T", "Toyota")
    with pytest.raises(ValueError):
        service.export(symbols, fmt, days)


def test_changed_indicator_columns_trigger_rebuild(env):
    service, *_ = env
    service.register("7203.T", "Toyota")
    with service.db.write() as conn:  # 旧バージョンの指標テーブルを再現
        conn.execute("DROP TABLE indicators")
        conn.execute("CREATE TABLE indicators (symbol TEXT, date TEXT, sma_5 REAL, PRIMARY KEY (symbol, date))")

    assert service.db.init_schema() is True
    service.rebuild_all()
    assert len(service.db.get_indicators("7203.T")) == 200
    assert service.db.init_schema() is False


def test_query_normalization():
    assert normalize_query(" ７２０３ ") == "7203"
    assert normalize_query("１３０ａ") == "130A"
    assert code_from_symbol("7203.T") == "7203"
    assert code_from_symbol("AAPL") == "AAPL"
