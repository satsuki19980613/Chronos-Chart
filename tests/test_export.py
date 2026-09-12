"""AI 向け出力 (ai_export.py) と 人間向け CSV (csv_export.py) のテスト。

方針:
- ネットワークを使わず、conftest.make_prices() などの合成データのみで完結させる。
- DB へのシード付けは Database を直接操作し（fetcher 経由の register は使わない）、
  ai_export / csv_export の入出力を厳密に検証する。
- 見つかった本物のバグは @pytest.mark.xfail(strict=True) として残し、再発を検知できるようにする。
"""

from __future__ import annotations

import csv
import io
import re

import numpy as np
import pandas as pd
import pytest
from conftest import make_prices

from app import ai_export, csv_export
from app import indicators as ind
from app.database import Database
from app.indicators import INDICATOR_COLUMNS, ai_column_names, indicator_definitions
from app.service import StockService

BASE_PRICE_COLS = ["symbol", "name", "currency", "date", "open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------
def _seed(db: Database, symbol: str, name: str, currency: str, prices: pd.DataFrame) -> pd.DataFrame:
    """fetcher を介さず DB に直接、株価と指標を投入する。"""
    db.upsert_stock(symbol, symbol.split(".")[0], name, "TEST", currency)
    db.upsert_prices(symbol, prices, replace=True)
    indicators = ind.compute_indicators(prices)
    db.replace_indicators(symbol, indicators)
    return indicators


def _wave(base: float, amplitude: float, n: int, **kwargs) -> pd.DataFrame:
    return make_prices(base + np.sin(np.arange(n) / 8) * amplitude, **kwargs)


def _csv_cell(line: str, index: int) -> str:
    """csv モジュールでクォートを考慮して 1 セルを取り出す。"""
    return next(csv.reader([line]))[index]


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    database.init_schema()
    return database


# ---------------------------------------------------------------------------
# AI CSV
# ---------------------------------------------------------------------------
def test_ai_csv_is_utf8_without_bom(db, tmp_path):
    _seed(db, "7203.T", "Toyota Motor Corp", "JPY", _wave(3000, 200, 200))
    res = ai_export.export(db, tmp_path / "output", ["7203.T"], "csv", 60)
    raw = open(res["path"], "rb").read()
    assert not raw.startswith(b"\xef\xbb\xbf")
    raw.decode("utf-8")  # デコードできること自体も確認


def test_ai_csv_header_matches_ai_column_names_with_no_duplicates(db, tmp_path):
    _seed(db, "7203.T", "Toyota", "JPY", _wave(3000, 200, 200))
    res = ai_export.export(db, tmp_path / "output", ["7203.T"], "csv", 60)
    header = open(res["path"], encoding="utf-8").readline().strip().split(",")

    assert header[:9] == BASE_PRICE_COLS
    # 列の並び順は INDICATOR_COLUMNS（計算順）に従うため、ここでは集合として一致することのみ確認する
    assert set(header[9:]) == set(ai_column_names().values())
    assert len(header) == len(set(header)), "重複した列名がある"


def test_ai_csv_dates_are_iso_and_ascending_per_symbol(db, tmp_path):
    _seed(db, "7203.T", "Toyota", "JPY", _wave(3000, 200, 200))
    _seed(db, "6758.T", "Sony", "JPY", _wave(3000, 150, 200))
    res = ai_export.export(db, tmp_path / "output", ["7203.T", "6758.T"], "csv", 60)
    df = pd.read_csv(res["path"], dtype={"date": str})

    assert df["date"].str.match(r"^\d{4}-\d{2}-\d{2}$").all()
    for symbol, group in df.groupby("symbol"):
        assert group["date"].is_monotonic_increasing


def test_ai_csv_no_thousands_separators(db, tmp_path):
    # 値そのものにカンマが入っていないかをセル単位で確認する（フィールド区切りのカンマと区別するため）
    _seed(db, "7203.T", "Toyota Motor Corporation", "JPY", _wave(12000, 800, 200))
    res = ai_export.export(db, tmp_path / "output", ["7203.T"], "csv", 60)
    lines = open(res["path"], encoding="utf-8").read().splitlines()

    for line in lines[1:]:
        for cell in next(csv.reader([line])):
            assert "," not in cell, f"値にカンマが含まれている: {cell!r}"


def test_ai_csv_empty_cells_for_missing_values(db, tmp_path):
    prices = _wave(3000, 200, 200)
    _seed(db, "7203.T", "Toyota", "JPY", prices)
    res = ai_export.export(db, tmp_path / "output", ["7203.T"], "csv", None)  # 全期間
    lines = open(res["path"], encoding="utf-8").read().splitlines()
    header = next(csv.reader([lines[0]]))
    sma75_idx = header.index("sma_75")  # 75日移動平均は先頭74行が計算不能

    first_data_row = lines[1]
    assert _csv_cell(first_data_row, sma75_idx) == ""

    df = pd.read_csv(res["path"])
    assert pd.isna(df.iloc[0]["sma_75"])


def test_ai_csv_days_larger_than_available_rows_returns_all_rows(db, tmp_path):
    prices = _wave(3000, 200, 50)  # 50 行しかない
    _seed(db, "7203.T", "Toyota", "JPY", prices)
    res = ai_export.export(db, tmp_path / "output", ["7203.T"], "csv", 9999)  # MAX_DAYS 以内で行数超過

    assert res["rows"] == 50
    df = pd.read_csv(res["path"])
    assert len(df) == 50


def test_ai_csv_name_with_comma_and_quote_roundtrips(db, tmp_path):
    tricky_name = 'Foo, "Bar" & Co.'
    _seed(db, "7203.T", tricky_name, "JPY", _wave(3000, 200, 60))
    res = ai_export.export(db, tmp_path / "output", ["7203.T"], "csv", 30)

    df = pd.read_csv(res["path"])
    assert (df["name"] == tricky_name).all()


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------
def _csv_blocks(markdown_text: str) -> list[str]:
    return re.findall(r"```csv\n(.*?)```", markdown_text, re.S)


def test_markdown_has_required_sections_per_symbol(db, tmp_path):
    _seed(db, "7203.T", "Toyota", "JPY", _wave(3000, 200, 200))
    _seed(db, "6758.T", "Sony", "JPY", _wave(3000, 150, 200))
    res = ai_export.export(db, tmp_path / "output", ["7203.T", "6758.T"], "markdown", 60)
    text = open(res["path"], encoding="utf-8").read()

    assert "## context" in text
    assert "## indicator_definitions" in text
    for symbol, name in (("7203.T", "Toyota"), ("6758.T", "Sony")):
        assert f"## {symbol} {name}" in text
        section = text.split(f"## {symbol} {name}")[1].split("\n## ")[0]
        assert "### latest_snapshot" in section
        assert "### signals_in_exported_range" in section
        assert "### daily_data" in section


def test_markdown_csv_blocks_parse_and_have_expected_row_count(db, tmp_path):
    _seed(db, "7203.T", "Toyota", "JPY", _wave(3000, 200, 200))
    _seed(db, "6758.T", "Sony", "JPY", _wave(3000, 150, 200))
    res = ai_export.export(db, tmp_path / "output", ["7203.T", "6758.T"], "markdown", 60)
    text = open(res["path"], encoding="utf-8").read()

    blocks = _csv_blocks(text)
    assert len(blocks) == 2
    for block in blocks:
        df = pd.read_csv(io.StringIO(block))
        assert len(df) == 60


def test_markdown_all_ai_columns_are_documented(db, tmp_path):
    """indicator_definitions() は列を "a, b, c" の並記か "prefix_*" のワイルドカードで説明する。
    どちらの形でも良いが、必ずどこかに現れていること。
    """
    patterns = [p.strip() for cols, _ in indicator_definitions() for p in cols.split(",")]

    def documented(name: str) -> bool:
        for p in patterns:
            if p.endswith("*"):
                if name.startswith(p[:-1]):
                    return True
            elif p == name:
                return True
        return False

    missing = [name for name in ai_column_names().values() if not documented(name)]
    assert missing == [], f"indicator_definitions() に載っていない AI 列名: {missing}"


def test_markdown_table_cells_escape_pipe_and_newline():
    assert ai_export._md_cell("a|b\nc|d") == "a\\|b c\\|d"


# ---------------------------------------------------------------------------
# ファイル名 / 一覧
# ---------------------------------------------------------------------------
def test_output_path_avoids_overwrite_within_same_second(tmp_path):
    from datetime import datetime

    output_dir = tmp_path / "output"
    now = datetime(2026, 1, 2, 3, 4, 5)

    first = ai_export.output_path(output_dir, ["7203.T"], "csv", now)
    output_dir.mkdir(parents=True, exist_ok=True)
    first.write_text("x", encoding="utf-8")

    second = ai_export.output_path(output_dir, ["7203.T"], "csv", now)

    assert first != second
    assert second.name.endswith("_2.csv")


def test_output_path_uses_and_n_more_suffix_over_3_symbols(tmp_path):
    from datetime import datetime

    output_dir = tmp_path / "output"
    now = datetime(2026, 1, 2, 3, 4, 5)
    symbols = ["AAA", "BBB", "CCC", "DDD", "EEE"]

    path = ai_export.output_path(output_dir, symbols, "csv", now)

    assert "_and2more" in path.name
    assert "DDD" not in path.name and "EEE" not in path.name  # 4件目以降は名前に出ない


def test_list_exports_ignores_unrelated_files(tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True)
    (output_dir / "technical_20260101_000000_7203.T.csv").write_text("a", encoding="utf-8")
    (output_dir / "technical_20260101_000000_7203.T.md").write_text("a", encoding="utf-8")
    (output_dir / "notes.txt").write_text("unrelated", encoding="utf-8")
    (output_dir / ".DS_Store").write_text("unrelated", encoding="utf-8")

    files = ai_export.list_exports(output_dir)

    assert {f["name"] for f in files} == {
        "technical_20260101_000000_7203.T.csv",
        "technical_20260101_000000_7203.T.md",
    }


# ---------------------------------------------------------------------------
# 人間向け CSV
# ---------------------------------------------------------------------------
def _decimal_places(text_value: str) -> int:
    return 0 if "." not in text_value else len(text_value.split(".")[1])


def test_human_csv_is_utf8_with_bom(tmp_path):
    prices = _wave(3000, 200, 120)
    indicators = ind.compute_indicators(prices)
    csv_export.export_csv(tmp_path / "csv", "7203.T", prices, indicators, "JPY")

    raw = (tmp_path / "csv" / "7203.T_株価.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")


def test_human_price_csv_newest_first_and_date_format(tmp_path):
    prices = _wave(3000, 200, 120)
    indicators = ind.compute_indicators(prices)
    price_table, _ = csv_export.build_human_tables(prices, indicators, "JPY")

    assert price_table["日付"].str.match(r"^\d{4}/\d{2}/\d{2}$").all()
    assert price_table["日付"].iloc[0] == prices["date"].max().replace("-", "/")
    assert price_table["日付"].is_monotonic_decreasing


def test_human_csv_jpy_prices_rounded_to_1_decimal(tmp_path):
    prices = _wave(3000, 300, 150)
    indicators = ind.compute_indicators(prices)
    price_table, _ = csv_export.build_human_tables(prices, indicators, "JPY")
    text = price_table.to_csv(index=False)

    for line in text.splitlines()[1:]:
        for cell in next(csv.reader([line]))[1:5]:  # 始値,高値,安値,終値
            assert _decimal_places(cell) <= 1, f"JPY のはずが小数第2位以下がある: {cell}"


def test_human_csv_usd_prices_rounded_to_2_decimals(tmp_path):
    prices = _wave(150, 15, 150)
    indicators = ind.compute_indicators(prices)
    price_table, _ = csv_export.build_human_tables(prices, indicators, "USD")
    text = price_table.to_csv(index=False)

    for line in text.splitlines()[1:]:
        for cell in next(csv.reader([line]))[1:5]:
            assert _decimal_places(cell) <= 2, f"USD のはずが小数第3位以下がある: {cell}"


def test_human_indicator_csv_column_order(tmp_path):
    prices = _wave(3000, 300, 150)
    indicators = ind.compute_indicators(prices)
    _, indicator_table = csv_export.build_human_tables(prices, indicators, "JPY")

    assert list(indicator_table.columns[:2]) == ["日付", "終値"]
    assert list(indicator_table.columns[2:]) == [label for _, label in INDICATOR_COLUMNS]


def test_human_price_csv_volume_is_integer(tmp_path):
    prices = _wave(3000, 300, 60)
    indicators = ind.compute_indicators(prices)
    price_table, _ = csv_export.build_human_tables(prices, indicators, "JPY")

    assert pd.api.types.is_integer_dtype(price_table["出来高"])
    text = price_table.to_csv(index=False)
    for line in text.splitlines()[1:]:
        volume_cell = next(csv.reader([line]))[5]
        assert "." not in volume_cell


def test_legacy_files_removed_on_export(tmp_path):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    legacy_prices = csv_dir / "7203.T_prices.csv"
    legacy_indicators = csv_dir / "7203.T_indicators.csv"
    legacy_prices.write_text("old", encoding="utf-8")
    legacy_indicators.write_text("old", encoding="utf-8")

    prices = _wave(3000, 200, 60)
    indicators = ind.compute_indicators(prices)
    csv_export.export_csv(csv_dir, "7203.T", prices, indicators, "JPY")

    assert not legacy_prices.exists()
    assert not legacy_indicators.exists()
    assert (csv_dir / "7203.T_株価.csv").exists()


def test_legacy_files_removed_on_delete(tmp_path):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "7203.T_prices.csv").write_text("old", encoding="utf-8")
    (csv_dir / "7203.T_indicators.csv").write_text("old", encoding="utf-8")

    csv_export.remove_csv(csv_dir, "7203.T")

    assert not (csv_dir / "7203.T_prices.csv").exists()
    assert not (csv_dir / "7203.T_indicators.csv").exists()


# ---------------------------------------------------------------------------
# service 経由の end-to-end（test_service.py と同じ流儀）
# ---------------------------------------------------------------------------
class _FakeFetcher:
    def __init__(self, prices: pd.DataFrame, currency: str = "JPY"):
        self.prices = prices
        self.currency = currency

    def search(self, query):
        return []

    def fetch_currency(self, symbol):
        return self.currency

    def fetch_history(self, symbol, period=None, start=None):
        return self.prices.reset_index(drop=True)


def test_service_export_and_rebuild_all_round_trip(tmp_path):
    db = Database(tmp_path / "test.db")
    db.init_schema()
    fetcher = _FakeFetcher(_wave(3000, 200, 120))
    service = StockService(db, fetcher, tmp_path / "csv", tmp_path / "output")

    service.register("7203.T", "Toyota")
    service.rebuild_all()
    res = service.export(["7203.T"], "csv", 30)

    assert (tmp_path / "csv" / "7203.T_株価.csv").exists()
    assert (tmp_path / "csv" / "7203.T_テクニカル指標.csv").exists()
    df = pd.read_csv(res["path"])
    assert len(df) == 30


# ---------------------------------------------------------------------------
# テスト追加時に見つかり修正したバグの回帰テスト
# ---------------------------------------------------------------------------
def test_ai_markdown_fmt_does_not_lose_precision_or_use_scientific_notation():
    """（修正済み）ai_export._fmt() は以前 round(value, 4) の後に ':g' で文字列化しており、
    ':g' のデフォルト有効桁数(6桁)で丸められてしまう。

    実データでも確認済み: stddev_20 は daily_data ブロックでは 121.0533 だが、
    latest_snapshot / indicator status テーブルでは "121.053" と最後の桁が失われる。
    さらに値が大きいと "1e+06" のような指数表記になり、AI が誤読しかねない。

    正しい仕様（ai_export.py 冒頭のコメント）は「日付から新しい日付へ昇順、
    数値は桁区切りなし」であり、有効桁数で丸めて良いとはどこにも書かれていない。
    同じ日付の値が daily_data の CSV ブロックと latest_snapshot で異なって見えるのは
    「unambiguous で LLM が読みやすい」という要件に反する。
    """
    assert ai_export._fmt(12345.6789) == "12345.6789"
    assert ai_export._fmt(600000.1234) == "600000.1234"



def test_evaluate_latest_notes_have_no_thousands_separator():
    """（修正済み）app/indicators.py の evaluate_latest() は以前、一部の note を ':,.1f' / ':,.2f' で
    フォーマットしており、値が1000以上だとカンマ区切り（例: "3,567.9"）が入る。

    このカンマ付き文字列は ai_export.render_markdown() の
    "indicator | status | value | note" テーブルにそのまま出力されるため、
    ai_export.py の方針コメント「数値に桁区切りや単位を付けない」に反する
    （実データでも "±2σ の範囲内（3,567.9 〜 4,052.1）" のように再現する）。
    """
    prices = _wave(12000, 800, 120)  # bb_upper/lower が1000を超えるようにする
    indicators = ind.compute_indicators(prices)
    cards = ind.evaluate_latest(prices, indicators)
    bb_card = next(c for c in cards if c["key"] == "bb")

    assert re.search(r"\d,\d{3}", bb_card["note"]) is None, bb_card["note"]

