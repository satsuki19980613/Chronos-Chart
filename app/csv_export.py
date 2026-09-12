"""人間が閲覧するための CSV 出力。

- Excel で文字化けしないよう UTF-8 BOM 付き
- 新しい日付が上、日付は yyyy/mm/dd
- 列名は日本語、値は指標の種類に応じて小数点以下を丸める（株価単位の指標は株価と同じ桁）
- 指標ファイルにも終値を載せ、株価ファイルと見比べなくても読めるようにする
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pandas as pd

from .indicators import INDICATOR_COLUMNS, value_kind

log = logging.getLogger(__name__)

PRICE_LABELS = {
    "date": "日付",
    "open": "始値",
    "high": "高値",
    "low": "安値",
    "close": "終値",
    "volume": "出来高",
}

# 旧バージョンのファイル名（削除時に一緒に消す）
_LEGACY_SUFFIXES = ("_prices.csv", "_indicators.csv")


def _safe_name(symbol: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]", "_", symbol)


def csv_paths(csv_dir: Path, symbol: str) -> dict[str, Path]:
    name = _safe_name(symbol)
    return {
        "prices": csv_dir / f"{name}_株価.csv",
        "indicators": csv_dir / f"{name}_テクニカル指標.csv",
    }


def price_decimals(currency: str | None) -> int:
    return 1 if currency == "JPY" else 2


def _human_frame(frame: pd.DataFrame, decimals: dict[str, int]) -> pd.DataFrame:
    """新しい順に並べ、日付を yyyy/mm/dd に、数値を列ごとの桁で丸める。"""
    out = frame.sort_values("date", ascending=False).reset_index(drop=True)
    out["date"] = out["date"].str.replace("-", "/", regex=False)
    for col, digits in decimals.items():
        if col in out:
            out[col] = out[col].round(digits)
    return out


def build_human_tables(prices: pd.DataFrame, indicators: pd.DataFrame, currency: str | None) -> tuple[pd.DataFrame, pd.DataFrame]:
    pd_digits = price_decimals(currency)
    kind_digits = {"price": pd_digits, "macd": 2, "pct": 2}

    price_table = _human_frame(prices[list(PRICE_LABELS)].copy(), {c: pd_digits for c in ("open", "high", "low", "close")})
    price_table["volume"] = price_table["volume"].astype("int64")
    price_table = price_table.rename(columns=PRICE_LABELS)

    merged = indicators.merge(prices[["date", "close"]], on="date", how="left")
    merged = merged[["date", "close", *(k for k, _ in INDICATOR_COLUMNS)]]
    decimals = {"close": pd_digits, **{k: kind_digits[value_kind(k)] for k, _ in INDICATOR_COLUMNS}}
    indicator_table = _human_frame(merged, decimals).rename(
        columns={"date": "日付", "close": "終値", **dict(INDICATOR_COLUMNS)}
    )
    return price_table, indicator_table


def export_csv(
    csv_dir: Path, symbol: str, prices: pd.DataFrame, indicators: pd.DataFrame, currency: str | None = None
) -> list[str]:
    """株価と指標の CSV を書き出す。書き込めなかったファイルの警告メッセージを返す。

    Excel で開いたままだと書き込みに失敗するため、DB 更新自体は止めずに警告に留める。
    """
    csv_dir.mkdir(parents=True, exist_ok=True)
    paths = csv_paths(csv_dir, symbol)
    price_table, indicator_table = build_human_tables(prices, indicators, currency)

    warnings = []
    for frame, path in ((price_table, paths["prices"]), (indicator_table, paths["indicators"])):
        try:
            frame.to_csv(path, index=False, encoding="utf-8-sig")
        except PermissionError:
            msg = f"{path.name} を書き込めませんでした（Excel 等で開いていないか確認してください）"
            log.warning(msg)
            warnings.append(msg)
    _remove_legacy(csv_dir, symbol)
    return warnings


def remove_csv(csv_dir: Path, symbol: str) -> None:
    for path in csv_paths(csv_dir, symbol).values():
        _unlink(path)
    _remove_legacy(csv_dir, symbol)


def _remove_legacy(csv_dir: Path, symbol: str) -> None:
    for suffix in _LEGACY_SUFFIXES:
        _unlink(csv_dir / f"{_safe_name(symbol)}{suffix}")


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except PermissionError:
        log.warning("could not remove %s", path)
