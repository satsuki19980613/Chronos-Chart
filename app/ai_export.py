"""AI（LLM）に読ませるための出力。閲覧用 CSV（csv_export.py）とは目的が異なる。

共通方針:
- UTF-8（BOM なし）、日付は ISO 8601（YYYY-MM-DD）、古い日付から新しい日付へ昇順
- 列名は英語 snake_case で期間を含める（例: sma_25, rsi_14）
- 数値に桁区切りや単位を付けない。計算できない値は空欄

形式:
- csv: 全銘柄を 1 つの縦長テーブルにまとめる（symbol 列で区別）
- markdown: 前提条件・指標の定義・銘柄ごとの最新判定・シグナル・日次データ（CSV ブロック）
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from pathlib import Path

import numpy as np
import pandas as pd

from . import indicators as ind
from .database import Database

FORMATS = {"csv": ".csv", "markdown": ".md"}
STATUS_EN = {"bull": "bullish", "bear": "bearish", "neutral": "neutral", "na": "insufficient_data"}
MAX_DAYS = 10000


@dataclass
class StockData:
    stock: dict
    prices: pd.DataFrame  # 全期間
    indicators: pd.DataFrame  # 全期間
    days: int | None

    @cached_property
    def table(self) -> pd.DataFrame:
        """出力対象期間の株価+指標（AI 向け列名）。"""
        merged = self.prices.merge(self.indicators, on="date", how="left")
        if self.days:
            merged = merged.tail(self.days)
        merged = merged.rename(columns=ind.ai_column_names())
        value_cols = [c for c in merged.columns if c not in ("date", "volume")]
        merged[value_cols] = merged[value_cols].astype(float).round(4)
        merged["volume"] = merged["volume"].astype("int64")
        return merged.reset_index(drop=True)


def validate_request(symbols: list[str], fmt: str, days: int | None) -> tuple[list[str], str, int | None]:
    if fmt not in FORMATS:
        raise ValueError(f"出力形式が不正です: {fmt}")
    if not symbols:
        raise ValueError("出力する銘柄を選択してください")
    if days is not None:
        # 整数と整数表記の文字列（"20"）のみ受け付ける。20.5 や True は拒否する
        if isinstance(days, bool) or (isinstance(days, float) and not days.is_integer()):
            raise ValueError(f"期間が不正です: {days}")
        try:
            days = int(days)
        except (TypeError, ValueError):
            raise ValueError(f"期間が不正です: {days}") from None
        if not 1 <= days <= MAX_DAYS:
            raise ValueError(f"期間は 1〜{MAX_DAYS} 日で指定してください")
    unique = list(dict.fromkeys(symbols))
    return unique, fmt, days


def load(db: Database, symbols: list[str], days: int | None) -> list[StockData]:
    result = []
    for symbol in symbols:
        stock = db.get_stock(symbol)
        if stock is None:
            raise ValueError(f"{symbol} は登録されていません")
        prices = db.get_prices(symbol)
        if prices.empty:
            raise ValueError(f"{symbol} の株価データがありません")
        indicators = db.get_indicators(symbol)
        if len(indicators) != len(prices):
            indicators = ind.compute_indicators(prices)
        result.append(StockData(stock, prices, indicators, days))
    return result


def output_path(output_dir: Path, symbols: list[str], fmt: str, now: datetime | None = None) -> Path:
    now = now or datetime.now()
    head = "_".join(re.sub(r"[^0-9A-Za-z.-]", "_", s) for s in symbols[:3])
    more = f"_and{len(symbols) - 3}more" if len(symbols) > 3 else ""
    base = f"technical_{now:%Y%m%d_%H%M%S}_{head}{more}"
    path = output_dir / f"{base}{FORMATS[fmt]}"
    counter = 2
    while path.exists():  # 同じ秒に複数回出力した場合
        path = output_dir / f"{base}_{counter}{FORMATS[fmt]}"
        counter += 1
    return path


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def render_csv(data: list[StockData]) -> str:
    frames = []
    for d in data:
        table = d.table.copy()
        table.insert(0, "currency", d.stock["currency"] or "")
        table.insert(0, "name", d.stock["name"])
        table.insert(0, "symbol", d.stock["symbol"])
        frames.append(table)
    combined = pd.concat(frames, ignore_index=True)
    return combined.to_csv(index=False, lineterminator="\n")


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------
def _fmt(value) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return ""
    if isinstance(value, float):
        # 指数表記や有効桁の切り捨てをせず、日次データの CSV ブロックと同じ小数4桁までで表す
        text = f"{round(value, 4):.4f}".rstrip("0").rstrip(".")
        return "0" if text == "-0" else text
    return str(value)


def _md_cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_markdown(data: list[StockData], generated_at: datetime | None = None) -> str:
    generated_at = generated_at or datetime.now()
    days = data[0].days if data else None
    out = io.StringIO()
    w = out.write

    w("# Autotechnical technical indicator export\n\n")
    w("## context\n\n")
    w(f"- generated_at: {generated_at:%Y-%m-%dT%H:%M:%S}\n")
    w(f"- symbols: {', '.join(d.stock['symbol'] for d in data)}\n")
    w(f"- period: {'last ' + str(days) + ' trading days per symbol' if days else 'all stored trading days'}\n")
    w("- source: Yahoo Finance daily bars via yfinance\n")
    w("- price_basis: split-adjusted, not dividend-adjusted\n")
    w("- row_order: oldest to newest\n")
    w("- missing_values: empty = not enough history to compute (e.g. long moving averages near the start, chikou span for the latest days)\n")
    w("- status_values: bullish / bearish / neutral / insufficient_data (mechanical rules, not investment advice)\n")
    w("- signal_codes: GC/DC = short SMA crosses mid SMA up/down, M+/M- = MACD crosses signal up/down, R30 = RSI recovers above 30, R70 = RSI falls below 70\n")
    w("- language: column names are English; indicator labels and notes are Japanese\n\n")

    w("## indicator_definitions\n\n")
    w("| columns | definition |\n|---|---|\n")
    for cols, desc in ind.indicator_definitions():
        w(f"| {_md_cell(cols)} | {_md_cell(desc)} |\n")
    w("\n")

    for d in data:
        s = d.stock
        table = d.table
        prices = d.prices
        w(f"## {s['symbol']} {s['name']}\n\n")
        w(f"- code: {s['code']}\n")
        w(f"- exchange: {s['exchange'] or ''}\n")
        w(f"- currency: {s['currency'] or ''}\n")
        w(f"- stored_range: {prices['date'].iloc[0]} to {prices['date'].iloc[-1]} ({len(prices)} rows)\n")
        w(f"- exported_range: {table['date'].iloc[0]} to {table['date'].iloc[-1]} ({len(table)} rows)\n\n")

        last = prices.iloc[-1]
        prev_close = float(prices["close"].iloc[-2]) if len(prices) > 1 else None
        close = float(last["close"])
        w(f"### latest_snapshot ({last['date']})\n\n")
        w(f"- open: {_fmt(float(last['open']))}\n- high: {_fmt(float(last['high']))}\n")
        w(f"- low: {_fmt(float(last['low']))}\n- close: {_fmt(close)}\n- volume: {int(last['volume'])}\n")
        if prev_close:
            w(f"- change: {_fmt(close - prev_close)}\n- change_pct: {_fmt((close - prev_close) / prev_close * 100)}\n")
        w("\n")

        w("| key | indicator | status | value | note |\n|---|---|---|---|---|\n")
        for card in ind.evaluate_latest(prices, d.indicators):
            w(
                f"| {card['key']} | {_md_cell(card['label'])} | {STATUS_EN[card['status']]} "
                f"| {_fmt(card['value'])} | {_md_cell(card['note'])} |\n"
            )
        w("\n")

        start = table["date"].iloc[0]
        signals = [sig for sig in ind.detect_signals(d.indicators) if sig["date"] >= start]
        w("### signals_in_exported_range\n\n")
        if signals:
            w("| date | direction | code | signal |\n|---|---|---|---|\n")
            for sig in signals:
                w(f"| {sig['date']} | {sig['direction']} | {sig['short']} | {_md_cell(sig['label'])} |\n")
        else:
            w("none\n")
        w("\n")

        w("### daily_data\n\n```csv\n")
        w(table.to_csv(index=False, lineterminator="\n"))
        w("```\n\n")

    return out.getvalue()


def export(db: Database, output_dir: Path, symbols: list[str], fmt: str, days: int | None) -> dict:
    symbols, fmt, days = validate_request(symbols, fmt, days)
    data = load(db, symbols, days)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_path(output_dir, symbols, fmt)
    content = render_csv(data) if fmt == "csv" else render_markdown(data)
    path.write_text(content, encoding="utf-8", newline="\n")
    return {
        "path": str(path),
        "name": path.name,
        "format": fmt,
        "symbols": symbols,
        "rows": int(sum(len(d.table) for d in data)),
        "size": path.stat().st_size,
    }


def list_exports(output_dir: Path) -> list[dict]:
    if not output_dir.exists():
        return []
    files = [p for p in output_dir.iterdir() if p.is_file() and p.suffix in FORMATS.values()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {
            "name": p.name,
            "path": str(p),
            "format": "csv" if p.suffix == ".csv" else "markdown",
            "size": p.stat().st_size,
            "modified": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        }
        for p in files
    ]
