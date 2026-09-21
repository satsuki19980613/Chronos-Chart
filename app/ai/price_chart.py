"""AI 分析レポート用の株価チャート（TradingView Lightweight Charts v5）の素材組み立て（P12-5）。

これまでの株価チャートは `app.ai.charts.line_chart` によるインライン SVG の折れ線だったが、
「ダッシュボードタブと同じローソク足 UI にしてほしい」という要望を受け、レポート HTML に
Lightweight Charts をインライン同梱してローソク足で描く。本モジュールは、その **データ（JSON）と
同梱素材（ライブラリ本体・初期化スクリプト）を組み立てる**役割だけを持つ。

- DB は一切触らない。入力は `app.ai.prompt.PromptInput`（AI に実際に送った内容そのもの）だけ
- `app.ai.report` の private 関数（`_parse_price_csv` など）は import しない（循環 import になるため）。
  CSV のパース規則（空文字・不正値は `None`）は本モジュール内に複製する
- 描画そのもの（`<script>` の中身）は `templates/report_chart.js` に書く。ここでは
  そのスクリプトが読む JSON と、フォールバック無しで動かせるだけの素材を渡す
- 配色は `app.ai.charts.PALETTE`（Okabe–Ito、色覚多様性に配慮）を使う。ただしローソク足だけは
  重なる SMA5 と紛れないよう専用の色にする（下の `CANDLE_UP_VAR` のコメント。SPEC §2.7.6）
"""

from __future__ import annotations

import csv
import functools
import io
import json
import re
from typing import Any

from .. import config
from ..errors import UserFacingError
from . import charts
from .prompt import PromptInput

# ---------------------------------------------------------------------------
# レポート HTML 側が参照する id（後続作業が依存するので変えない）
# ---------------------------------------------------------------------------
CHART_CONTAINER_ID = "cc-price-chart"
DATA_SCRIPT_ID = "cc-price-chart-data"

# 株価チャートに重ねる移動平均線（`app.ai.report._PRICE_LINE_COLUMNS` と同じ列・対応）
_PRICE_LINE_COLUMNS = ("sma_5", "sma_25", "sma_75")
_PRICE_LINE_LABELS = {"sma_5": "SMA5", "sma_25": "SMA25", "sma_75": "SMA75"}
_PRICE_LINE_COLORS = {"sma_5": "orange", "sma_25": "skyblue", "sma_75": "purple"}

# ローソク足の陽線/陰線色。日本式の「赤上げ・青下げ」。
# **`--chart-N` は使わない。** Okabe-Ito の vermilion はオレンジ寄りで、同じ図に重なる SMA5（orange）と
# 見分けがつかなくなるため（ダークモードの `--chart-6` はさらにオレンジ寄りになる）。
# 専用の CSS 変数を使い、テンプレート側でライト/ダーク/印刷それぞれの値を持つ
# （ライトと印刷は濃い赤、ダークはダッシュボード `web/js/chart.js` と同じ赤/青）。
CANDLE_UP_VAR = "--candle-up"
CANDLE_DOWN_VAR = "--candle-down"
# CSS 変数が解決できなかったときのフォールバック（テンプレートのライト時の値と同じ）
CANDLE_UP_COLOR = "#b3261e"
CANDLE_DOWN_COLOR = charts.PALETTE["blue"]

# この2つだけがゴールデン/デッドクロスの矢印表示になる（他は小さな丸。web/js/chart.js と同じ規則）
_MARKER_ARROW_SHORTS = {"GC", "DC"}

# `charts.PALETTE` のキー順（`templates/report.html.j2` の `--chart-1`〜`--chart-8` と対応させる）。
# `charts._PALETTE_ORDER` は private なので使わず、公開されている `PALETTE` の挿入順から複製する。
_PALETTE_ORDER = list(charts.PALETTE.keys())


def palette_var(name: str) -> str:
    """`charts.PALETTE` のキー名（例: "vermilion"）を CSS カスタムプロパティ名にする。

    レポート HTML（`templates/report.html.j2`）は `--chart-1`〜`--chart-8` を定義し、
    ダークモード（`prefers-color-scheme: dark`）・印刷（`@media print`）で明るさの違う値に
    上書きする。株価チャートの色もこの変数越しに解決させることで、レポートの配色変更に自動で
    追従する（`app.ai.price_chart` から見えるのは変数名だけで、実際の色は CSS 側が持つ）。
    """
    idx = _PALETTE_ORDER.index(name) + 1
    return f"--chart-{idx}"


# ---------------------------------------------------------------------------
# CSV パース（`app.ai.report` の規則を複製。private 関数の import は循環 import になるため不可）
# ---------------------------------------------------------------------------
def _to_float(text: Any) -> float | None:
    if text is None or text == "":
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _parse_price_rows(price_csv: str) -> list[dict[str, Any]]:
    """`date,open,high,low,close,volume` の CSV を行の辞書リストにする。値は不正なら `None`。"""
    reader = csv.DictReader(io.StringIO(price_csv))
    rows: list[dict[str, Any]] = []
    for row in reader:
        rows.append(
            {
                "date": row["date"],
                "open": _to_float(row.get("open")),
                "high": _to_float(row.get("high")),
                "low": _to_float(row.get("low")),
                "close": _to_float(row.get("close")),
                "volume": _to_float(row.get("volume")),
            }
        )
    return rows


def _parse_lines(indicator_csv: str, dates: list[str]) -> dict[str, list[float | None]]:
    """`indicator_csv` から SMA5/25/75 の列だけを `dates` の並びに合わせて取り出す。"""
    reader = csv.DictReader(io.StringIO(indicator_csv))
    by_date = {row["date"]: row for row in reader}
    result: dict[str, list[float | None]] = {col: [] for col in _PRICE_LINE_COLUMNS}
    for d in dates:
        row = by_date.get(d)
        for col in _PRICE_LINE_COLUMNS:
            result[col].append(_to_float(row.get(col)) if row is not None else None)
    return result


def _price_unit(currency: str) -> str:
    """`app.ai.report._price_unit` と同じロジック（JPY なら「円」、空なら空文字、他は " <通貨>"）。"""
    if not currency:
        return ""
    return "円" if currency.upper() == "JPY" else f" {currency}"


def _price_digits(currency: str) -> int:
    return 0 if currency.upper() == "JPY" else 2


# ---------------------------------------------------------------------------
# マーカー（売買シグナル・開示。`app.ai.report._price_markers` / web/js/chart.js と同じ規則）
# ---------------------------------------------------------------------------
def _signal_markers(data: PromptInput, valid_dates: set[str]) -> list[dict]:
    """売買シグナルをマーカー化する。GC/DC だけ矢印+文字、それ以外は小さな丸。"""
    markers: list[dict] = []
    for sig in data.signals:
        d = str(sig["date"])
        if d not in valid_dates:
            continue
        is_buy = sig["direction"] == "buy"
        short = sig["short"]
        is_cross = short in _MARKER_ARROW_SHORTS
        color_key = "green" if is_buy else "vermilion"
        markers.append(
            {
                "time": d,
                "position": "belowBar" if is_buy else "aboveBar",
                "shape": ("arrowUp" if is_buy else "arrowDown") if is_cross else "circle",
                "color": charts.PALETTE[color_key],
                "colorVar": palette_var(color_key),
                "text": short if is_cross else "",
                "size": 1 if is_cross else 0.5,
            }
        )
    return markers


def _disclosure_markers(data: PromptInput, valid_dates: set[str]) -> list[dict]:
    """開示をマーカー化する（株価 CSV に無い日付は落とす）。"""
    markers: list[dict] = []
    for disc in data.disclosures:
        d = str(disc.submit_at)[:10]
        if d not in valid_dates:
            continue
        markers.append(
            {
                "time": d,
                "position": "aboveBar",
                "shape": "circle",
                "color": charts.PALETTE["skyblue"],
                "colorVar": palette_var("skyblue"),
                "text": "",
                "size": 1,
            }
        )
    return markers


# ---------------------------------------------------------------------------
# ペイロード組み立て
# ---------------------------------------------------------------------------
def _build_parts(data: PromptInput) -> dict[str, Any]:
    """`build_payload` と `legend_items` の両方が使う中間データをまとめて作る。"""
    rows = _parse_price_rows(data.price_csv)
    dates = [r["date"] for r in rows]
    valid_dates = set(dates)

    candles = [
        {"time": r["date"], "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"]}
        for r in rows
        if r["open"] is not None and r["high"] is not None and r["low"] is not None and r["close"] is not None
    ]

    volumes = [
        {
            "time": r["date"],
            "value": r["volume"],
            "up": (r["close"] >= r["open"]) if (r["close"] is not None and r["open"] is not None) else True,
        }
        for r in rows
        if r["volume"] is not None
    ]

    line_values = _parse_lines(data.indicator_csv, dates)
    lines: list[dict[str, Any]] = []
    for col in _PRICE_LINE_COLUMNS:
        points = [
            {"time": d, "value": v} for d, v in zip(dates, line_values[col]) if v is not None
        ]
        if not points:
            # 値が1件も無い系列は入れない（凡例からも消すため。P12 のフィードバックで決まった規則）
            continue
        color_key = _PRICE_LINE_COLORS[col]
        lines.append(
            {
                "label": _PRICE_LINE_LABELS[col],
                "color": charts.PALETTE[color_key],
                "colorVar": palette_var(color_key),
                "data": points,
            }
        )

    currency = data.currency or ""
    return {
        "candles": candles,
        "volumes": volumes,
        "lines": lines,
        "signal_markers": _signal_markers(data, valid_dates),
        "disclosure_markers": _disclosure_markers(data, valid_dates),
        "price_digits": _price_digits(currency),
        "unit": _price_unit(currency),
    }


def build_payload(data: PromptInput) -> dict:
    """Lightweight Charts に渡す JSON（辞書）を組み立てる。"""
    parts = _build_parts(data)
    markers = parts["signal_markers"] + parts["disclosure_markers"]
    # Lightweight Charts は time 昇順を要求する。同日の中の順序（シグナル→開示）は安定ソートで保つ
    markers.sort(key=lambda m: m["time"])
    return {
        "candles": parts["candles"],
        "volumes": parts["volumes"],
        "lines": parts["lines"],
        "markers": markers,
        "priceDigits": parts["price_digits"],
        "unit": parts["unit"],
        # ローソク足の陽線/陰線色。CSS カスタムプロパティ（`--chart-N`）越しに解決させ、
        # ダークモード・印刷でレポートの配色に自動で追従させる（16進はその値が取れない場合のフォールバック）
        "candleUpVar": CANDLE_UP_VAR,
        "candleDownVar": CANDLE_DOWN_VAR,
        "candleUpColor": CANDLE_UP_COLOR,
        "candleDownColor": CANDLE_DOWN_COLOR,
    }


# `<script type="application/json">` に埋め込む際にエスケープが必要な文字
_SCRIPT_ESCAPES = {
    "<": "\\u003c",
    ">": "\\u003e",
    "&": "\\u0026",
    " ": "\\u2028",
    " ": "\\u2029",
}
_SCRIPT_ESCAPE_RE = re.compile("[" + "".join(_SCRIPT_ESCAPES) + "]")


def escape_for_script(text: str) -> str:
    """JSON 文字列を `<script>` の中に安全に埋め込めるようにする。

    `</script>` でタグが閉じられてしまうのを防ぐため `<` `>` `&` を、JS のリテラル改行として
    解釈される U+2028/U+2029 も `\\uXXXX` に置き換える。
    """
    return _SCRIPT_ESCAPE_RE.sub(lambda m: _SCRIPT_ESCAPES[m.group(0)], text)


def payload_json(data: PromptInput) -> str:
    """`build_payload` の結果を `<script>` に埋め込める JSON 文字列にする。"""
    raw = json.dumps(build_payload(data), ensure_ascii=False)
    return escape_for_script(raw)


# ---------------------------------------------------------------------------
# 同梱素材（ライブラリ本体・初期化スクリプト）
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=1)
def library_source() -> str:
    """Lightweight Charts 本体（`web/vendor/`）を読み込む。中身は一切加工しない（同梱物の著作権表示を保つため）。"""
    path = config.WEB_DIR / "vendor" / "lightweight-charts.standalone.production.js"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UserFacingError(f"Lightweight Charts の同梱ファイルを読み込めません: {path}") from exc


@functools.lru_cache(maxsize=1)
def init_script() -> str:
    """レポートに埋め込むチャート初期化スクリプト（`templates/report_chart.js`）を読み込む。"""
    path = config.BASE_DIR / "templates" / "report_chart.js"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UserFacingError(f"チャート初期化スクリプトを読み込めません: {path}") from exc


# ---------------------------------------------------------------------------
# 静的な凡例（JS 無しでも出せるように、テンプレート側で使う）
# ---------------------------------------------------------------------------
def legend_items(data: PromptInput) -> list[dict]:
    """実際にチャートへ入った系列・マーカー種別だけを載せる凡例を作る。

    データが無い SMA・シグナル・開示は載せない（P12 のフィードバックで決まった規則を踏襲）。
    テンプレート側は `color_var` を `var(--chart-N, <16進>)` の形で使い、チャート本体と同じ
    CSS カスタムプロパティから色を取る（ダークモード・印刷で一緒に切り替わるようにするため）。
    """
    parts = _build_parts(data)
    items: list[dict] = []

    def item(label: str, color_key: str, kind: str) -> dict:
        return {
            "label": label,
            "color": charts.PALETTE[color_key],
            "color_var": palette_var(color_key),
            "kind": kind,
        }

    if parts["candles"]:
        items.append(
            {
                "label": "ローソク足（陽線/陰線）",
                "color": CANDLE_UP_COLOR,
                "color_var": CANDLE_UP_VAR,
                "color2": CANDLE_DOWN_COLOR,
                "color_var2": CANDLE_DOWN_VAR,
                "kind": "candle",
            }
        )

    for col in _PRICE_LINE_COLUMNS:
        labels = [line["label"] for line in parts["lines"]]
        if _PRICE_LINE_LABELS[col] in labels:
            items.append(item(_PRICE_LINE_LABELS[col], _PRICE_LINE_COLORS[col], "line"))

    if any(m["position"] == "belowBar" for m in parts["signal_markers"]):
        items.append(item("買いシグナル", "green", "marker"))
    if any(m["position"] == "aboveBar" for m in parts["signal_markers"]):
        items.append(item("売りシグナル", "vermilion", "marker"))
    if parts["disclosure_markers"]:
        items.append(item("開示", "skyblue", "marker"))

    return items
