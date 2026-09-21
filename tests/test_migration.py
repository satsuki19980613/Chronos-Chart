"""スキーマのマイグレーション（SPEC §3.1）。"""

import sqlite3

import numpy as np
from conftest import make_prices

from app import database
from app.database import MIGRATIONS, Database, add_column_if_missing

LATEST = MIGRATIONS[-1][0]


def _tables(db: Database) -> set[str]:
    with db.connect() as conn:
        return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_fresh_db_gets_latest_schema(tmp_path):
    db = Database(tmp_path / "new.db")
    assert db.init_schema() is False
    assert db.schema_version() == LATEST
    assert {"stocks", "prices", "indicators", "settings", "fetch_log"} <= _tables(db)
    assert {"short_positions", "short_totals", "margin_balances"} <= _tables(db)
    assert "edinet_codes" in _tables(db)
    assert {"disclosures", "disclosure_links"} <= _tables(db)
    assert {"ai_usage", "ai_reports"} <= _tables(db)


def test_ai_usage_has_the_columns_the_quota_manager_needs(tmp_path):
    """SPEC §3。requests は「送信を試みた回数」なので、成功・失敗を問わず加算される。"""
    db = Database(tmp_path / "ai.db")
    db.init_schema()
    with db.connect() as conn:
        usage = {r["name"] for r in conn.execute("PRAGMA table_info(ai_usage)")}
        reports = {r["name"] for r in conn.execute("PRAGMA table_info(ai_reports)")}
    assert usage == {"date_pt", "model", "requests", "in_tokens", "out_tokens", "exhausted"}
    assert reports == {"id", "symbol", "created_at", "model", "path", "in_tokens", "out_tokens"}


def test_margin_balances_survives_migration(tmp_path, monkeypatch):
    """貸借取引残高は取り直せないので、後続の移行で消えてはならない（SPEC §3.1）。"""
    path = tmp_path / "keep.db"
    db = Database(path)
    db.init_schema()
    db.upsert_stock("7203.T", "7203", "Toyota", "東証", "JPY")
    with db.write() as conn:
        conn.execute(
            "INSERT INTO margin_balances (symbol, date, settle_date, kind, yushi_balance, "
            "kashi_balance, net_balance, fetched_at) VALUES (?,?,?,?,?,?,?,?)",
            ("7203.T", "2026-09-17", "2026-09-24", "final", 820900, 0, 820900, "2026-09-20 10:00:00"),
        )

    def migrate_next(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS later (x INTEGER)")

    monkeypatch.setattr(database, "MIGRATIONS", [*MIGRATIONS, (LATEST + 1, migrate_next)])
    Database(path).init_schema()

    with Database(path).connect() as conn:
        rows = conn.execute("SELECT * FROM margin_balances").fetchall()
    assert len(rows) == 1
    assert rows[0]["yushi_balance"] == 820900
    assert rows[0]["kind"] == "final"


def test_migration_versions_are_strictly_increasing():
    versions = [v for v, _ in MIGRATIONS]
    assert versions == sorted(set(versions))
    assert versions[0] == 1


def test_legacy_db_is_migrated_without_losing_data(tmp_path):
    """土台（Autotechnical）が作った DB には settings も fetch_log も無い。"""
    path = tmp_path / "legacy.db"
    db = Database(path)
    db.init_schema()
    db.upsert_stock("7203.T", "7203", "Toyota", "東証", "JPY")
    db.upsert_prices("7203.T", make_prices(100 + np.arange(30.0)))
    with sqlite3.connect(path) as conn:  # 移行前の状態に戻す
        conn.execute("DROP TABLE settings")
        conn.execute("DROP TABLE fetch_log")

    db = Database(path)
    db.init_schema()

    assert db.schema_version() == LATEST
    assert db.get_stock("7203.T")["row_count"] == 30
    assert {"settings", "fetch_log"} <= _tables(db)


def test_init_schema_is_idempotent(tmp_path):
    db = Database(tmp_path / "twice.db")
    db.init_schema()
    db.set_setting("gemini_model", "some-model")
    db.log_fetch("yahoo", "7203.T")

    db.init_schema()
    db.init_schema()

    assert db.schema_version() == LATEST
    assert db.get_setting("gemini_model") == "some-model"
    assert db.get_fetch("yahoo", "7203.T")["result"] == "ok"


def test_new_migration_runs_once_and_keeps_existing_rows(tmp_path, monkeypatch):
    db = Database(tmp_path / "grow.db")
    db.init_schema()
    db.log_fetch("edinet", "2026-09-18", "empty")

    calls = []

    def migrate_next(conn):
        calls.append(1)
        add_column_if_missing(conn, "fetch_log", "note", "TEXT")

    monkeypatch.setattr(database, "MIGRATIONS", [*MIGRATIONS, (LATEST + 1, migrate_next)])
    db.init_schema()
    db.init_schema()

    assert calls == [1]
    assert db.schema_version() == LATEST + 1
    assert db.get_fetch("edinet", "2026-09-18")["result"] == "empty"
    with db.connect() as conn:
        assert "note" in [r["name"] for r in conn.execute("PRAGMA table_info(fetch_log)")]


def test_failed_migration_rolls_back_and_keeps_version(tmp_path, monkeypatch):
    db = Database(tmp_path / "fail.db")
    db.init_schema()

    def broken(conn):
        conn.execute("CREATE TABLE half_done (id INTEGER)")
        raise RuntimeError("boom")

    monkeypatch.setattr(database, "MIGRATIONS", [*MIGRATIONS, (LATEST + 1, broken)])
    try:
        db.init_schema()
    except RuntimeError:
        pass
    else:
        raise AssertionError("migration error must propagate")

    assert db.schema_version() == LATEST
    assert "half_done" not in _tables(db)


def test_add_column_if_missing_is_idempotent(tmp_path):
    db = Database(tmp_path / "col.db")
    db.init_schema()
    with db.write() as conn:
        add_column_if_missing(conn, "fetch_log", "extra", "INTEGER DEFAULT 0")
        add_column_if_missing(conn, "fetch_log", "extra", "INTEGER DEFAULT 0")
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(fetch_log)")]
    assert cols.count("extra") == 1


def test_settings_roundtrip(tmp_path):
    db = Database(tmp_path / "s.db")
    db.init_schema()
    assert db.get_setting("missing") is None
    db.set_setting("scrape_interval_sec", "10")
    db.set_setting("scrape_interval_sec", "15")
    assert db.get_setting("scrape_interval_sec") == "15"
    assert db.get_settings()["scrape_interval_sec"] == "15"


def test_fetch_log_upsert(tmp_path):
    db = Database(tmp_path / "f.db")
    db.init_schema()
    assert db.get_fetch("karauri", "7203.T") is None
    db.log_fetch("karauri", "7203.T", "error:timeout")
    db.log_fetch("karauri", "7203.T")
    row = db.get_fetch("karauri", "7203.T")
    assert row["result"] == "ok"
    assert row["fetched_at"]
