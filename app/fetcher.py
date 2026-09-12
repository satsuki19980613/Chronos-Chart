"""Yahoo! Finance（yfinance）からの銘柄検索・株価取得。"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import asdict, dataclass

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

# 東証の銘柄コード（4桁数字、または 130A のような英字入り新コード）
JP_CODE_PATTERN = re.compile(r"^\d{3}[0-9A-Z]$")

EXCHANGE_NAMES = {
    "JPX": "東証",
    "TYO": "東証",
    "NMS": "NASDAQ",
    "NGM": "NASDAQ",
    "NCM": "NASDAQ",
    "NYQ": "NYSE",
    "PCX": "NYSE Arca",
    "ASE": "NYSE American",
}


class FetchError(Exception):
    """株価データを取得できなかった。"""


@dataclass
class SearchResult:
    symbol: str
    name: str
    exchange: str | None
    quote_type: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_query(query: str) -> str:
    """全角英数字を半角にし、前後の空白を除いて大文字化する。"""
    return unicodedata.normalize("NFKC", query).strip().upper()


def code_from_symbol(symbol: str) -> str:
    """'7203.T' -> '7203'。取引所サフィックスがなければそのまま。"""
    return symbol.split(".")[0] if "." in symbol else symbol


class YahooFetcher:
    def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        q = normalize_query(query)
        if not q:
            return []

        results: list[SearchResult] = []
        try:
            quotes = yf.Search(q, max_results=max_results, news_count=0).quotes
        except Exception as exc:  # ネットワークエラー等
            log.warning("search failed for %s: %s", q, exc)
            quotes = []

        for item in quotes:
            symbol = item.get("symbol")
            if not symbol:
                continue
            results.append(
                SearchResult(
                    symbol=symbol,
                    name=item.get("longname") or item.get("shortname") or symbol,
                    exchange=EXCHANGE_NAMES.get(item.get("exchange"), item.get("exchDisp") or item.get("exchange")),
                    quote_type=item.get("quoteType"),
                )
            )

        # 国内の銘柄コードは東証銘柄（.T）を最優先で表示する
        if JP_CODE_PATTERN.match(q):
            jp_symbol = f"{q}.T"
            hit = next((r for r in results if r.symbol == jp_symbol), None)
            if hit is None:
                hit = self._lookup_symbol(jp_symbol)
            if hit is not None:
                results = [hit, *(r for r in results if r.symbol != jp_symbol)]

        return results

    def _lookup_symbol(self, symbol: str) -> SearchResult | None:
        """検索APIに出てこないシンボルを直接確認する。"""
        try:
            ticker = yf.Ticker(symbol)
            if ticker.history(period="5d").empty:
                return None
            info = ticker.get_info() or {}
        except Exception as exc:
            log.warning("lookup failed for %s: %s", symbol, exc)
            return None
        return SearchResult(
            symbol=symbol,
            name=info.get("longName") or info.get("shortName") or symbol,
            exchange=EXCHANGE_NAMES.get(info.get("exchange"), info.get("exchange")),
            quote_type=info.get("quoteType"),
        )

    def fetch_currency(self, symbol: str) -> str | None:
        try:
            return yf.Ticker(symbol).fast_info.get("currency")
        except Exception:
            return None

    def fetch_history(self, symbol: str, period: str | None = None, start: str | None = None) -> pd.DataFrame:
        """日足を取得して列 date/open/high/low/close/volume/splits の DataFrame で返す。

        yfinance の OHLC は株式分割調整済み（配当調整なし）の値。
        """
        kwargs = {"start": start} if start else {"period": period or "1y"}
        try:
            raw = yf.Ticker(symbol).history(interval="1d", auto_adjust=False, **kwargs)
        except Exception as exc:
            raise FetchError(f"{symbol} の株価取得に失敗しました: {exc}") from exc

        if raw is None or raw.empty:
            raise FetchError(f"{symbol} の株価データが見つかりませんでした")

        index = raw.index
        if getattr(index, "tz", None) is not None:
            index = index.tz_localize(None)
        df = pd.DataFrame(
            {
                "date": index.strftime("%Y-%m-%d"),
                "open": raw["Open"].to_numpy(),
                "high": raw["High"].to_numpy(),
                "low": raw["Low"].to_numpy(),
                "close": raw["Close"].to_numpy(),
                "volume": raw["Volume"].fillna(0).astype("int64").to_numpy(),
                "splits": raw["Stock Splits"].to_numpy() if "Stock Splits" in raw else 0.0,
            }
        )
        df = df.dropna(subset=["open", "high", "low", "close"])
        return df.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
