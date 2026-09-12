"""SQLite へのデータ保存。

pywebview の API 呼び出しは別スレッドで実行されるため、
メソッドごとに接続を開き、書き込みはロックで直列化する。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

import numpy as np
import pandas as pd

from .indicators import INDICATOR_KEYS

PRICE_KEYS = ["open", "high", "low", "close", "volume"]


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._write_lock = threading.Lock()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock, self.connect() as conn:
            yield conn

    def init_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        indicator_cols = ",\n".join(f"    {key} REAL" for key in INDICATOR_KEYS)
        with self.write() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS stocks (
                    symbol        TEXT PRIMARY KEY,
                    code          TEXT NOT NULL,
                    name          TEXT NOT NULL,
                    exchange      TEXT,
                    currency      TEXT,
                    registered_at TEXT NOT NULL,
                    last_updated  TEXT
                );
                CREATE TABLE IF NOT EXISTS prices (
                    symbol TEXT NOT NULL REFERENCES stocks(symbol) ON DELETE CASCADE,
                    date   TEXT NOT NULL,
                    open   REAL,
                    high   REAL,
                    low    REAL,
                    close  REAL,
                    volume INTEGER,
                    PRIMARY KEY (symbol, date)
                );
                CREATE TABLE IF NOT EXISTS indicators (
                    symbol TEXT NOT NULL REFERENCES stocks(symbol) ON DELETE CASCADE,
                    date   TEXT NOT NULL,
                {indicator_cols},
                    PRIMARY KEY (symbol, date)
                );
                """
            )
            # 指標を追加したときは既存DBに列を足す
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(indicators)")}
            for key in INDICATOR_KEYS:
                if key not in existing:
                    conn.execute(f"ALTER TABLE indicators ADD COLUMN {key} REAL")

    # ---------- stocks ----------
    def upsert_stock(self, symbol: str, code: str, name: str, exchange: str | None, currency: str | None) -> None:
        now = _now()
        with self.write() as conn:
            conn.execute(
                """
                INSERT INTO stocks (symbol, code, name, exchange, currency, registered_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    name = excluded.name,
                    exchange = COALESCE(excluded.exchange, stocks.exchange),
                    currency = COALESCE(excluded.currency, stocks.currency)
                """,
                (symbol, code, name, exchange, currency, now),
            )

    def touch_stock(self, symbol: str) -> None:
        with self.write() as conn:
            conn.execute("UPDATE stocks SET last_updated = ? WHERE symbol = ?", (_now(), symbol))

    def get_stock(self, symbol: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(_STOCK_SELECT + " WHERE s.symbol = ?", (symbol,)).fetchone()
        return dict(row) if row else None

    def list_stocks(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(_STOCK_SELECT + " ORDER BY s.code").fetchall()
        return [dict(r) for r in rows]

    def delete_stock(self, symbol: str) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM stocks WHERE symbol = ?", (symbol,))

    # ---------- prices ----------
    def upsert_prices(self, symbol: str, prices: pd.DataFrame, replace: bool = False) -> int:
        rows = [
            (symbol, r.date, *(_to_db(getattr(r, k)) for k in PRICE_KEYS))
            for r in prices.itertuples(index=False)
        ]
        with self.write() as conn:
            if replace:
                conn.execute("DELETE FROM prices WHERE symbol = ?", (symbol,))
            conn.executemany(
                """
                INSERT INTO prices (symbol, date, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, date) DO UPDATE SET
                    open = excluded.open, high = excluded.high, low = excluded.low,
                    close = excluded.close, volume = excluded.volume
                """,
                rows,
            )
        return len(rows)

    def get_prices(self, symbol: str) -> pd.DataFrame:
        with self.connect() as conn:
            return pd.read_sql_query(
                "SELECT date, open, high, low, close, volume FROM prices WHERE symbol = ? ORDER BY date",
                conn,
                params=(symbol,),
            )

    def get_price_range(self, symbol: str) -> tuple[str | None, str | None]:
        with self.connect() as conn:
            row = conn.execute("SELECT MIN(date), MAX(date) FROM prices WHERE symbol = ?", (symbol,)).fetchone()
        return row[0], row[1]

    # ---------- indicators ----------
    def replace_indicators(self, symbol: str, indicators: pd.DataFrame) -> None:
        cols = ["date", *INDICATOR_KEYS]
        rows = [(symbol, *(_to_db(v) for v in r)) for r in indicators[cols].itertuples(index=False)]
        placeholders = ", ".join("?" for _ in range(len(cols) + 1))
        with self.write() as conn:
            conn.execute("DELETE FROM indicators WHERE symbol = ?", (symbol,))
            conn.executemany(
                f"INSERT INTO indicators (symbol, {', '.join(cols)}) VALUES ({placeholders})", rows
            )

    def get_indicators(self, symbol: str) -> pd.DataFrame:
        with self.connect() as conn:
            return pd.read_sql_query(
                f"SELECT date, {', '.join(INDICATOR_KEYS)} FROM indicators WHERE symbol = ? ORDER BY date",
                conn,
                params=(symbol,),
            )


_STOCK_SELECT = """
    SELECT s.symbol, s.code, s.name, s.exchange, s.currency, s.registered_at, s.last_updated,
           (SELECT COUNT(*) FROM prices p WHERE p.symbol = s.symbol) AS row_count,
           (SELECT MIN(date) FROM prices p WHERE p.symbol = s.symbol) AS first_date,
           (SELECT MAX(date) FROM prices p WHERE p.symbol = s.symbol) AS last_date
    FROM stocks s
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_db(value):
    """numpy 型や NaN を SQLite に渡せる値へ変換する。"""
    if value is None:
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return None if np.isnan(value) else float(value)
    return value
