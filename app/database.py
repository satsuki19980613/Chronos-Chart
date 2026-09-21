"""SQLite へのデータ保存。

pywebview の API 呼び出しは別スレッドで実行されるため、
メソッドごとに接続を開き、書き込みはロックで直列化する。
"""

from __future__ import annotations

import logging
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

log = logging.getLogger(__name__)


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

    def init_schema(self) -> bool:
        """テーブルを作成する。

        指標の構成が変わっていた場合は indicators テーブルを作り直し True を返す
        （指標は株価から再計算できるため、呼び出し側で再計算する）。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        indicator_cols = ",\n".join(f"    {key} REAL" for key in INDICATOR_KEYS)
        with self.write() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            existing = [row["name"] for row in conn.execute("PRAGMA table_info(indicators)")]
            rebuilt = bool(existing) and existing != ["symbol", "date", *INDICATOR_KEYS]
            if rebuilt:
                conn.execute("DROP TABLE indicators")
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
            self._migrate(conn)
        return rebuilt

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """schema_version より新しい移行を順に適用する（再実行しても同じ結果になる）。

        indicators と違い、ここで足すテーブルは再取得コストが高い（再取得できないものもある）ので
        作り直さず、バージョンごとの移行関数で育てる。
        """
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        row = conn.execute("SELECT value FROM settings WHERE key = 'schema_version'").fetchone()
        current = int(row["value"]) if row else 0
        for version, migrate in MIGRATIONS:
            if version <= current:
                continue
            # sqlite3 は DDL の前に暗黙のトランザクションを張らないので、明示的に囲んで
            # 「移行の途中まで適用された DB」を残さない
            conn.commit()
            conn.execute("BEGIN")
            try:
                migrate(conn)
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES ('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(version),),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            log.info("migrated schema to version %d", version)

    def schema_version(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = 'schema_version'").fetchone()
        return int(row["value"]) if row else 0

    # ---------- settings ----------
    def get_setting(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def get_settings(self) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def set_setting(self, key: str, value: str | None) -> None:
        with self.write() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # ---------- fetch_log ----------
    def log_fetch(self, source: str, key: str, result: str = "ok") -> None:
        with self.write() as conn:
            conn.execute(
                "INSERT INTO fetch_log (source, key, fetched_at, result) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(source, key) DO UPDATE SET fetched_at = excluded.fetched_at, result = excluded.result",
                (source, key, _now(), result),
            )

    def get_fetch(self, source: str, key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT source, key, fetched_at, result FROM fetch_log WHERE source = ? AND key = ?",
                (source, key),
            ).fetchone()
        return dict(row) if row else None

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


def add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """既存テーブルに列が無ければ足す（移行関数から使う）。ddl は 'INTEGER DEFAULT 0' のような型定義。"""
    existing = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _migrate_v1(conn: sqlite3.Connection) -> None:
    """取得の記録（差分取得・スキップ判定用）。settings は _migrate が先に作る。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fetch_log (
            source     TEXT NOT NULL,
            key        TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            result     TEXT,
            PRIMARY KEY (source, key)
        )
        """
    )


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """需給データ（SPEC §3）。空売り残高・その合計・貸借取引残高。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS short_positions (
            symbol      TEXT NOT NULL REFERENCES stocks(symbol) ON DELETE CASCADE,
            calc_date   TEXT NOT NULL,
            holder_id   TEXT NOT NULL,
            holder      TEXT NOT NULL,
            ratio       REAL,
            ratio_delta REAL,
            quantity    INTEGER,
            qty_delta   INTEGER,
            note        TEXT,
            PRIMARY KEY (symbol, calc_date, holder_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_short_positions_symbol_date "
        "ON short_positions(symbol, calc_date)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS short_totals (
            symbol      TEXT NOT NULL REFERENCES stocks(symbol) ON DELETE CASCADE,
            date        TEXT NOT NULL,
            total_ratio REAL,
            total_qty   INTEGER,
            holders     INTEGER,
            PRIMARY KEY (symbol, date)
        )
        """
    )
    # 日証金の貸借取引残高。過去分を取り直す手段が無いので、以後の移行で DROP しないこと。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS margin_balances (
            symbol        TEXT NOT NULL REFERENCES stocks(symbol) ON DELETE CASCADE,
            date          TEXT NOT NULL,
            settle_date   TEXT,
            kind          TEXT NOT NULL,
            yushi_new     INTEGER,
            yushi_repay   INTEGER,
            yushi_balance INTEGER,
            kashi_new     INTEGER,
            kashi_repay   INTEGER,
            kashi_balance INTEGER,
            net_balance   INTEGER,
            fetched_at    TEXT NOT NULL,
            PRIMARY KEY (symbol, date)
        )
        """
    )


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """EDINET コードリスト（SPEC §2.4.1a・§3）。証券コード → EDINET コードの対応表。

    元ファイルを何度でも取り直せるので、取り込みは全置換でよい。
    sec_code は5桁のまま保存する（末尾は 0。'409A0' のように英字を含むことがある）。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS edinet_codes (
            edinet_code TEXT PRIMARY KEY,
            sec_code    TEXT,
            name        TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edinet_codes_sec ON edinet_codes(sec_code)")


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """開示メタデータと、銘柄との関係（SPEC §2.4.3・§2.4.6・§3）。

    書類そのもの（disclosures）と、登録銘柄との関係（disclosure_links）を分けて持つ。
    1つの書類が複数の登録銘柄に関係し得るため（買付者も対象会社も登録済み、など）。
    どちらも data/edinet_cache/ から作り直せるので、構成が変わったら再走査すればよい。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS disclosures (
            doc_id              TEXT PRIMARY KEY,
            edinet_code         TEXT,
            sec_code            TEXT,
            filer_name          TEXT,
            issuer_edinet_code  TEXT,
            subject_edinet_code TEXT,
            doc_type_code       TEXT NOT NULL,
            form_code           TEXT,
            ordinance_code      TEXT,
            description         TEXT,
            reason              TEXT,
            period_start        TEXT,
            period_end          TEXT,
            submit_at           TEXT NOT NULL,
            parent_doc_id       TEXT,
            withdrawal          INTEGER,
            disclosure          INTEGER,
            category            TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS disclosure_links (
            doc_id TEXT NOT NULL REFERENCES disclosures(doc_id) ON DELETE CASCADE,
            symbol TEXT NOT NULL REFERENCES stocks(symbol) ON DELETE CASCADE,
            role   TEXT NOT NULL,
            PRIMARY KEY (doc_id, symbol, role)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_disclosure_links_symbol ON disclosure_links(symbol)"
    )


def _migrate_v5(conn: sqlite3.Connection) -> None:
    """Gemini のクォータ管理と生成済みレポート（SPEC §2.7.2・§2.7.6・§3）。

    ai_usage は太平洋時間の日付ごと・モデルごとの使用量。requests は「送信を試みた回数」で、
    失敗した送信も枠を消費するため成功時ではなく送信直前に加算する（SPEC §2.7.2）。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_usage (
            date_pt    TEXT NOT NULL,
            model      TEXT NOT NULL,
            requests   INTEGER NOT NULL DEFAULT 0,
            in_tokens  INTEGER NOT NULL DEFAULT 0,
            out_tokens INTEGER NOT NULL DEFAULT 0,
            exhausted  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (date_pt, model)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_reports (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol     TEXT NOT NULL,
            created_at TEXT NOT NULL,
            model      TEXT NOT NULL,
            path       TEXT NOT NULL,
            in_tokens  INTEGER,
            out_tokens INTEGER
        )
        """
    )

# (バージョン, 移行関数)。追加するときは末尾に足し、既存の関数は書き換えない。
# 貸借取引残高（margin_balances）は再取得できないので、どの移行でも DROP しないこと。
MIGRATIONS = [
    (1, _migrate_v1),
    (2, _migrate_v2),
    (3, _migrate_v3),
    (4, _migrate_v4),
    (5, _migrate_v5),
]
