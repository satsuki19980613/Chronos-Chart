"""画面から呼ばれる業務ロジック（検索・登録・更新・ダッシュボード用データ組み立て）。"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

from . import ai_export
from . import disclosures
from . import indicators as ind
from .config import INITIAL_PERIOD, OUTPUT_DIR
from .csv_export import csv_paths, export_csv, remove_csv
from .database import Database
from .fetcher import YahooFetcher, code_from_symbol

log = logging.getLogger(__name__)


class StockService:
    def __init__(self, db: Database, fetcher: YahooFetcher, csv_dir: Path, output_dir: Path = OUTPUT_DIR):
        self.db = db
        self.fetcher = fetcher
        self.csv_dir = csv_dir
        self.output_dir = output_dir
        # 同じ銘柄の登録/更新が同時に走らないようにする
        self._symbol_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)

    # ---------- 検索・登録 ----------
    def search(self, query: str) -> list[dict]:
        registered = {s["symbol"] for s in self.db.list_stocks()}
        return [{**r.to_dict(), "registered": r.symbol in registered} for r in self.fetcher.search(query)]

    def register(self, symbol: str, name: str, exchange: str | None = None) -> dict:
        """銘柄を登録する。初回は過去1年分を取得、登録済みなら差分更新。"""
        if self.db.get_stock(symbol) is None:
            prices = self.fetcher.fetch_history(symbol, period=INITIAL_PERIOD)
            if prices.empty:
                raise ValueError(f"{symbol} の株価データを取得できませんでした")
            currency = self.fetcher.fetch_currency(symbol)
            self.db.upsert_stock(symbol, code_from_symbol(symbol), name or symbol, exchange, currency)
            with self._symbol_locks[symbol]:
                try:
                    self.db.upsert_prices(symbol, prices, replace=True)
                    warnings = self._rebuild(symbol)
                except Exception:
                    # 途中で失敗したら「登録済みだがデータなし」の状態を残さない
                    self.db.delete_stock(symbol)
                    remove_csv(self.csv_dir, symbol)
                    raise
            self.db.log_fetch("yahoo", symbol)
            log.info("registered %s (%d rows)", symbol, len(prices))
            try:
                disclosures.scan_cache(self.db, symbols=[symbol])
            except Exception:
                log.exception("disclosures scan_cache failed for %s", symbol)
            return {"stock": self.db.get_stock(symbol), "added": len(prices), "warnings": warnings}
        return self.update(symbol)

    def update(self, symbol: str) -> dict:
        """最終取得日以降のデータを取得して指標・CSVを再計算する。"""
        if self.db.get_stock(symbol) is None:
            raise ValueError(f"{symbol} は登録されていません")

        with self._symbol_locks[symbol]:
            first_date, last_date = self.db.get_price_range(symbol)
            before = len(self.db.get_prices(symbol))
            if last_date is None:
                prices = self.fetcher.fetch_history(symbol, period=INITIAL_PERIOD)
                self.db.upsert_prices(symbol, prices, replace=True)
            else:
                # 最終日も再取得する（取得時点で当日の値が確定していなかった場合に備える）
                prices = self.fetcher.fetch_history(symbol, start=last_date)
                if (prices.loc[prices["date"] > last_date, "splits"].fillna(0) != 0).any():
                    # 株式分割があると過去の価格も調整されるため、保有期間全体を取り直す
                    log.info("split detected for %s, refetching from %s", symbol, first_date)
                    prices = self.fetcher.fetch_history(symbol, start=first_date)
                    self.db.upsert_prices(symbol, prices, replace=True)
                else:
                    self.db.upsert_prices(symbol, prices)
            warnings = self._rebuild(symbol)
            # 取得の記録。last_updated は起動時の再生成でも変わるので、自動更新のスキップ判定には使わない
            self.db.log_fetch("yahoo", symbol)

        after = len(self.db.get_prices(symbol))
        return {"stock": self.db.get_stock(symbol), "added": after - before, "warnings": warnings}

    def update_all(self) -> dict:
        results, errors = [], []
        for stock in self.db.list_stocks():
            try:
                results.append(self.update(stock["symbol"]))
            except Exception as exc:
                log.exception("update failed for %s", stock["symbol"])
                errors.append(f"{stock['symbol']}: {exc}")
        return {"updated": len(results), "errors": errors, "warnings": [w for r in results for w in r["warnings"]]}

    def rebuild_all(self, only_missing_csv: bool = False) -> int:
        """保存済みの株価から指標と CSV を作り直す。

        指標構成の変更時は全銘柄、only_missing_csv=True なら CSV が無い銘柄だけ
        （CSV の形式・ファイル名を変えたバージョンへの移行用）。作り直した銘柄数を返す。
        """
        count = 0
        for stock in self.db.list_stocks():
            symbol = stock["symbol"]
            if only_missing_csv and all(p.exists() for p in csv_paths(self.csv_dir, symbol).values()):
                continue
            with self._symbol_locks[symbol]:
                self._rebuild(symbol)
            count += 1
        return count

    def delete(self, symbol: str) -> None:
        if self.db.get_stock(symbol) is None:
            raise ValueError(f"{symbol} は登録されていません")
        with self._symbol_locks[symbol]:
            self.db.delete_stock(symbol)
            remove_csv(self.csv_dir, symbol)
        try:
            disclosures.cleanup_orphans(self.db)
        except Exception:
            log.exception("disclosures cleanup_orphans failed after deleting %s", symbol)

    def list_stocks(self) -> list[dict]:
        return self.db.list_stocks()

    def _rebuild(self, symbol: str) -> list[str]:
        """DB の全株価から指標を計算し直し、DB と CSV に保存する。"""
        prices = self.db.get_prices(symbol)
        indicators = ind.compute_indicators(prices)
        self.db.replace_indicators(symbol, indicators)
        stock = self.db.get_stock(symbol) or {}
        warnings = export_csv(self.csv_dir, symbol, prices, indicators, stock.get("currency"))
        self.db.touch_stock(symbol)
        return warnings

    # ---------- 出力（AI 向け） ----------
    def export(self, symbols: list[str], fmt: str, days: int | None) -> dict:
        return ai_export.export(self.db, self.output_dir, symbols, fmt, days)

    def list_exports(self) -> list[dict]:
        return ai_export.list_exports(self.output_dir)

    # ---------- ダッシュボード ----------
    def dashboard(self, symbol: str) -> dict:
        stock = self.db.get_stock(symbol)
        if stock is None:
            raise ValueError(f"{symbol} は登録されていません")

        prices = self.db.get_prices(symbol)
        indicators = self.db.get_indicators(symbol)
        if prices.empty:
            raise ValueError(f"{symbol} の株価データがありません。更新してください")
        if len(indicators) != len(prices):
            self._rebuild(symbol)
            indicators = self.db.get_indicators(symbol)

        signals = ind.detect_signals(indicators)
        paths = csv_paths(self.csv_dir, symbol)
        dates = prices["date"].tolist()
        with self.db.connect() as conn:
            short = _short_points(conn, symbol, dates)
            taisyaku = _taisyaku_points(conn, symbol, dates)

        return {
            "stock": stock,
            "quote": _quote(prices),
            "cards": ind.evaluate_latest(prices, indicators),
            "signals": list(reversed(signals[-12:])),
            "chart": {
                "dates": dates,
                "open": _clean(prices["open"]),
                "high": _clean(prices["high"]),
                "low": _clean(prices["low"]),
                "close": _clean(prices["close"]),
                "volume": _clean(prices["volume"]),
                "indicators": {key: _clean(indicators[key]) for key in ind.INDICATOR_KEYS},
                "params": ind.PARAMS,
                "future_cloud": _future_cloud(prices),
                "signals": signals,
                "short": short,
                "taisyaku": taisyaku,
            },
            "table": _table(prices, indicators, limit=60),
            "csv": {k: str(p) for k, p in paths.items()},
        }


def _clean(series: pd.Series) -> list:
    """JSON 化できるよう NaN を None に、numpy 型を Python 型にする。"""
    arr = series.to_numpy(dtype=float)
    return [None if np.isnan(v) else float(v) for v in arr]


def _quote(prices: pd.DataFrame) -> dict:
    last = prices.iloc[-1]
    prev_close = float(prices["close"].iloc[-2]) if len(prices) > 1 else None
    close = float(last["close"])
    change = close - prev_close if prev_close else None
    return {
        "date": last["date"],
        "open": float(last["open"]),
        "high": float(last["high"]),
        "low": float(last["low"]),
        "close": close,
        "volume": int(last["volume"]),
        "change": change,
        "change_pct": change / prev_close * 100 if prev_close else None,
    }


def _future_cloud(prices: pd.DataFrame) -> list[dict]:
    # 将来日付は土日のみ除いた営業日で近似する（祝日は考慮しない）
    last = pd.Timestamp(prices["date"].iloc[-1])
    future_dates = pd.bdate_range(last + pd.Timedelta(days=1), periods=ind.ICHIMOKU_SHIFT)
    cloud = ind.ichimoku_future_cloud(prices)
    return [{**c, "date": d.strftime("%Y-%m-%d")} for c, d in zip(cloud, future_dates)]


def _short_points(conn: sqlite3.Connection, symbol: str, dates: list[str]) -> dict:
    """空売り残高合計（short_totals）をチャート用の点列に整形する（SPEC §2.5.2）。

    ローソク足に存在する日付だけを残し、最後の報告が最新の足より前なら
    据え置きの点（carried=True）を1つ足して線を延ばす。需給データなので
    AI 向けの経路（ai_export.py / app/ai/）には一切渡さないこと。
    """
    rows = conn.execute(
        "SELECT date, total_ratio, total_qty, holders FROM short_totals WHERE symbol = ? ORDER BY date",
        (symbol,),
    ).fetchall()
    date_set = set(dates)
    points = [
        {
            "date": row["date"],
            "ratio": row["total_ratio"],
            "qty": row["total_qty"],
            "holders": row["holders"],
            "carried": False,
        }
        for row in rows
        if row["date"] in date_set
    ]
    if not points:
        return {"available": False, "reason": "0.5% 以上の報告なし、または未取得", "points": []}

    if dates and points[-1]["date"] < dates[-1]:
        last = points[-1]
        points.append(
            {
                "date": dates[-1],
                "ratio": last["ratio"],
                "qty": last["qty"],
                "holders": last["holders"],
                "carried": True,
            }
        )
    return {"available": True, "reason": None, "points": points}


def _taisyaku_points(conn: sqlite3.Connection, symbol: str, dates: list[str]) -> dict:
    """貸借取引残高（margin_balances）をチャート用の点列に整形する（SPEC §2.5.2）。

    ローソク足に存在する日付だけを残す。日次データなので据え置きは行わない。
    欠測日は欠測のまま渡し、線を切るのは画面側に任せる。
    """
    rows = conn.execute(
        "SELECT date, yushi_balance, kashi_balance, net_balance, kind "
        "FROM margin_balances WHERE symbol = ? ORDER BY date",
        (symbol,),
    ).fetchall()
    date_set = set(dates)
    points = [
        {
            "date": row["date"],
            "yushi": row["yushi_balance"],
            "kashi": row["kashi_balance"],
            "net": row["net_balance"],
            "kind": row["kind"],
        }
        for row in rows
        if row["date"] in date_set
    ]
    if not points:
        return {"available": False, "reason": "貸借銘柄ではない、または未取得", "points": []}
    return {"available": True, "reason": None, "points": points}


def _table(prices: pd.DataFrame, indicators: pd.DataFrame, limit: int) -> dict:
    merged = prices.merge(indicators, on="date", how="left").tail(limit).iloc[::-1]
    columns = [
        {"key": "date", "label": "日付", "kind": "date"},
        *({"key": k, "label": label, "kind": "price"} for k, label in (("open", "始値"), ("high", "高値"), ("low", "安値"), ("close", "終値"))),
        {"key": "volume", "label": "出来高", "kind": "volume"},
        *({"key": k, "label": label, "kind": ind.value_kind(k)} for k, label in ind.INDICATOR_COLUMNS),
    ]
    rows = []
    for record in merged.to_dict("records"):
        rows.append(
            {
                k: (None if isinstance(v, float) and np.isnan(v) else v.item() if hasattr(v, "item") else v)
                for k, v in record.items()
            }
        )
    return {"columns": columns, "rows": rows}
