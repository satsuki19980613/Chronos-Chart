"""閲覧用 CSV の出力（Excel で文字化けしないよう UTF-8 BOM 付き）。"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pandas as pd

from .indicators import INDICATOR_COLUMNS

log = logging.getLogger(__name__)

PRICE_LABELS = {
    "date": "日付",
    "open": "始値",
    "high": "高値",
    "low": "安値",
    "close": "終値",
    "volume": "出来高",
}


def _safe_name(symbol: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]", "_", symbol)


def csv_paths(csv_dir: Path, symbol: str) -> dict[str, Path]:
    name = _safe_name(symbol)
    return {
        "prices": csv_dir / f"{name}_prices.csv",
        "indicators": csv_dir / f"{name}_indicators.csv",
    }


def export_csv(csv_dir: Path, symbol: str, prices: pd.DataFrame, indicators: pd.DataFrame) -> list[str]:
    """株価と指標の CSV を書き出す。書き込めなかったファイルの警告メッセージを返す。

    Excel で開いたままだと書き込みに失敗するため、DB 更新自体は止めずに警告に留める。
    """
    csv_dir.mkdir(parents=True, exist_ok=True)
    paths = csv_paths(csv_dir, symbol)

    price_out = prices[list(PRICE_LABELS)].rename(columns=PRICE_LABELS)
    indicator_out = indicators.round(4).rename(columns={"date": "日付", **dict(INDICATOR_COLUMNS)})

    warnings = []
    for frame, path in ((price_out, paths["prices"]), (indicator_out, paths["indicators"])):
        try:
            frame.to_csv(path, index=False, encoding="utf-8-sig")
        except PermissionError:
            msg = f"{path.name} を書き込めませんでした（Excel 等で開いていないか確認してください）"
            log.warning(msg)
            warnings.append(msg)
    return warnings


def remove_csv(csv_dir: Path, symbol: str) -> None:
    for path in csv_paths(csv_dir, symbol).values():
        try:
            path.unlink(missing_ok=True)
        except PermissionError:
            log.warning("could not remove %s", path)
