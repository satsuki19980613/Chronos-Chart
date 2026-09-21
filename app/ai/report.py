"""AI 分析レポートの HTML 生成と保存（SPEC §2.7.6・§3 `ai_reports`）。

**CLAUDE.md 不変条件1（最重要）**: 需給データ（空売り残高・貸借取引残高）をレポートに載せない。
このモジュールが受け取るのは `app.ai.prompt.PromptInput`（= AI に実際に送った内容そのもの）と
`app.ai.schema.AnalysisReport`（= AI の出力）、そして任意で財務ハイライト用の `financials` 辞書
（P11 が渡す形。値そのものはこのモジュールが取得するわけではない）だけなので、需給が混入する経路が無い。
`service.dashboard()` の payload（需給の表示データを含む）はここでは一切参照しない。

HTML は Jinja2 の単一テンプレート（`templates/report.html.j2`）から生成する。外部 CSS/画像は
参照せず、生成された文字列だけで完結する1ファイルにする。AI の出力（`summary` 等）をそのまま
テンプレートに埋め込むため、**autoescape は必須**（`<script>` 混入対策）。図の大半は `app.ai.charts`
が自前でエスケープ済みの SVG 文字列を返すので、テンプレート側で `| safe` を使って埋め込む
（P12-2: レポートの視覚強化）。

**`<script>` を出力するのは株価チャートのためだけ**（P12-5: SVG 折れ線から Lightweight Charts の
ローソク足への差し替え）。`app.ai.price_chart` が組み立てる JSON・ライブラリ本体・初期化スクリプトの
3つをテンプレートの末尾に埋め込む。**AI の出力はこのスクリプト文脈には一切入らない**
（チャートに渡すのは株価・指標・シグナル・開示日だけで、`summary` 等の AI 生成文はいつもどおり
Jinja2 の autoescape 経由で HTML 本文にだけ出す）。詳細は `app.ai.price_chart` の docstring と
SPEC §2.7.6 を参照。
"""

from __future__ import annotations

import csv
import io
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from .. import config
from ..errors import UserFacingError
from . import charts, price_chart
from .prompt import PromptInput
from .schema import AnalysisReport

TEMPLATE_NAME = "report.html.j2"

# web/js/app.js の STATUS 表記（bull/bear/neutral/na → 強気/弱気/中立/—）に合わせる
_STATUS_LABELS = {"bull": "強気", "bear": "弱気", "neutral": "中立", "na": "—"}
# 判定に応じた CSS クラス名。未知の値は neutral 相当の見た目にする
_STATUS_CLASSES = {"bull": "bull", "bear": "bear", "neutral": "neutral", "na": "neutral"}
_DIRECTION_LABELS = {"buy": "買い", "sell": "売り"}
_VERDICT_LABELS = {"bullish": "強気", "bearish": "弱気", "neutral": "中立"}
_CONFIDENCE_LABELS = {"low": "低", "medium": "中", "high": "高"}
# 開示と登録銘柄の関係（SPEC §2.4.3 の role）。画面と同じく日本語で見せる
_ROLE_LABELS = {"filer": "提出者", "issuer": "発行者（保有された側）", "subject": "対象会社"}

# 指標カード（`app.indicators.evaluate_latest`）のカテゴリ分け（P12-2: 指標値の詳細を読みやすくする）。
# 実データの card には "group" が入っている（trend/oscillator/volatility）が、合成テストの
# 簡易カードには無いことがあるため、無ければ key の前方一致で分類する（フォールバック）。
_CATEGORY_LABELS = {
    "trend": "トレンド系",
    "oscillator": "オシレーター系",
    "volatility": "ボラティリティ系",
    "other": "その他",
}
_CATEGORY_ORDER = ["trend", "oscillator", "volatility", "other"]
_CATEGORY_BY_KEY: dict[str, str] = {
    "sma": "trend",
    "ema": "trend",
    "macd": "trend",
    "ichimoku": "trend",
    "adx": "trend",
    "gmma": "trend",
    "parabolic": "trend",
    "deviation_short": "trend",
    "deviation_long": "trend",
    "rsi": "oscillator",
    "rci": "oscillator",
    "stoch": "oscillator",
    "psychological": "oscillator",
    "momentum": "oscillator",
    "bb": "volatility",
    "stddev": "volatility",
}
# カテゴリ内で「主要」として初期表示する指標（残りは <details> に折りたたむ）
_PRIMARY_KEYS = {"sma", "macd", "ichimoku", "rsi", "bb"}

# 株価チャートに重ねる移動平均線（SPEC で列名が固定されている: sma_5 / sma_25 / sma_75）
_PRICE_LINE_COLUMNS = ("sma_5", "sma_25", "sma_75")
_PRICE_LINE_LABELS = {"sma_5": "SMA5", "sma_25": "SMA25", "sma_75": "SMA75"}
# SMA25 と SMA75 は両方とも「青系」だと重なったときに見分けがつかないため、SMA75 は紫にする
_PRICE_LINE_COLORS = {"sma_5": "orange", "sma_25": "skyblue", "sma_75": "purple"}

# 財務ハイライト（`financials` = `app.financial_metrics.compute_metrics()` の戻り値そのもの。SPEC §2.9.6・§2.9.9）
_STANDARD_LABELS = {"jgaap": "日本基準", "ifrs": "IFRS", "usgaap": "米国基準"}
_BASIS_LABELS = {"consolidated": "連結", "nonconsolidated": "単体"}
# KPI カードに出す指標（financial_metrics.METRIC_ORDER のキー）。営業利益は EDINET の
# 「主要な経営指標等の推移」に項目が無く取得できないため、売上高・当期純利益・ROE・自己資本比率にする
_FIN_KPI_KEYS = [("revenue", "売上高"), ("net_income", "当期純利益"), ("roe", "ROE"), ("equity_ratio", "自己資本比率")]
_FIN_KPI_MISSING_NOTE_DEFAULT = "値を取得できていません（データ不足）"
_FIN_SOURCE_LABELS = {"disclosed": "開示値そのまま", "computed": "アプリで計算"}
_JPY_UNIT_DIVISOR = 1_000_000.0
_JPY_UNIT_LABEL = "百万円"


def _role_label(role: str) -> str:
    """`filer` や `subject/filer` のような role 文字列を日本語にする。"""
    if not role:
        return ""
    return "・".join(_ROLE_LABELS.get(part, part) for part in role.split("/"))


def _format_value(value: Any) -> str:
    """指標値の表示。浮動小数はそのままだと桁が長すぎるので丸める。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)

# report_path でファイル名に使えない文字（Windows のパス予約文字含む）をアンダースコアに置き換える
_UNSAFE_SYMBOL_CHARS = re.compile(r"[^0-9A-Za-z_-]")


def _environment(templates_dir: Path | None = None) -> Environment:
    """テンプレートの探索環境を作る。既定は `app.config.BASE_DIR / "templates"`。

    `templates_dir` はテストから差し替えるための引数（SPEC §2.7.6 の実装メモ）。
    AI の出力をそのまま埋め込むテンプレートなので autoescape は常に有効にする。
    """
    directory = templates_dir if templates_dir is not None else config.BASE_DIR / "templates"
    return Environment(loader=FileSystemLoader(str(directory)), autoescape=True)


def _category_for(card: Any) -> str:
    group = card.get("group") if hasattr(card, "get") else None
    if group in _CATEGORY_LABELS:
        return group
    key = card["key"]
    for prefix, cat in _CATEGORY_BY_KEY.items():
        if key == prefix or key.startswith(prefix + "_"):
            return cat
    return "other"


def _status_row(card: Any) -> dict:
    status = card["status"]
    value = card["value"]
    key = card["key"]
    return {
        "key": key,
        "label": card["label"],
        "status_label": _STATUS_LABELS.get(status, status),
        "status_class": _STATUS_CLASSES.get(status, "neutral"),
        "value_display": _format_value(value),
        "note": card["note"] or "",
        "category": _category_for(card),
    }


def _group_latest_rows(rows: list[dict]) -> list[dict]:
    """指標カードをカテゴリ別にまとめ、各カテゴリで「主要」と「そのほか」に分ける。

    「そのほか」は `<details>` で折りたたむための材料になる（P12-2: 指標値の詳細を読みやすくする）。
    """
    buckets: dict[str, list[dict]] = {}
    seen_order: list[str] = []
    for row in rows:
        cat = row["category"]
        if cat not in buckets:
            buckets[cat] = []
            seen_order.append(cat)
        buckets[cat].append(row)

    ordered_cats = [c for c in _CATEGORY_ORDER if c in buckets]
    ordered_cats += [c for c in seen_order if c not in ordered_cats]

    categories = []
    for cat in ordered_cats:
        items = buckets[cat]
        primary = [r for r in items if r["key"] in _PRIMARY_KEYS]
        rest = [r for r in items if r["key"] not in _PRIMARY_KEYS]
        if not primary:
            # 主要指標が1つも無いカテゴリでも、最初の1件だけは初期表示する
            primary, rest = items[:1], items[1:]
        categories.append({"label": _CATEGORY_LABELS.get(cat, cat), "primary": primary, "rest": rest})
    return categories


def _technical_badge(latest: tuple) -> dict:
    """「テクニカルの観点」冒頭のバッジ（SPEC: `verdict` ではなくこのセクション独自の要約）。

    AI の自己申告ではなく、`data.latest`（機械的な指標判定）の多数決から作る。色だけに
    頼らないよう、記号（▲▼→）を必ず添える。
    """
    bulls = sum(1 for c in latest if c["status"] == "bull")
    bears = sum(1 for c in latest if c["status"] == "bear")
    if bulls > bears:
        return {"label": "強気優勢", "symbol": "▲", "class": "bull"}
    if bears > bulls:
        return {"label": "弱気優勢", "symbol": "▼", "class": "bear"}
    return {"label": "拮抗・中立", "symbol": "→", "class": "neutral"}


def _signal_row(sig: Any) -> dict:
    direction = sig["direction"]
    return {
        "date": sig["date"],
        "direction_label": _DIRECTION_LABELS.get(direction, direction),
        "label": sig["label"],
        "short": sig["short"],
    }


# ---------------------------------------------------------------------------
# 株価チャート（CSV 文字列から折れ線 SVG を組み立てる）
# ---------------------------------------------------------------------------
def _to_float(text: Any) -> float | None:
    if text is None or text == "":
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _parse_price_csv(price_csv: str) -> tuple[list[str], list[float | None]]:
    reader = csv.DictReader(io.StringIO(price_csv))
    dates: list[str] = []
    closes: list[float | None] = []
    for row in reader:
        dates.append(row["date"])
        closes.append(_to_float(row.get("close")))
    return dates, closes


def _parse_price_lines(indicator_csv: str, dates: list[str]) -> dict[str, list[float | None]]:
    reader = csv.DictReader(io.StringIO(indicator_csv))
    by_date = {row["date"]: row for row in reader}
    result: dict[str, list[float | None]] = {col: [] for col in _PRICE_LINE_COLUMNS}
    for d in dates:
        row = by_date.get(d)
        for col in _PRICE_LINE_COLUMNS:
            result[col].append(_to_float(row.get(col)) if row is not None else None)
    return result


def _price_unit(currency: str) -> str:
    if not currency:
        return ""
    return "円" if currency.upper() == "JPY" else f" {currency}"


def _price_markers(data: PromptInput, valid_dates: set[str]) -> list[dict]:
    """買い/売りシグナルと開示提出日をマーカー化する。

    **株価 CSV に存在する日付だけ**を渡す（存在しない日付のマーカーは無視する）。
    """
    markers: list[dict] = []
    for sig in data.signals:
        d = str(sig["date"])
        if d in valid_dates:
            markers.append({"date": d, "kind": sig["direction"], "label": sig["label"]})
    for disc in data.disclosures:
        d = str(disc.submit_at)[:10]
        if d in valid_dates:
            markers.append({"date": d, "kind": "disclosure", "label": disc.label})
    return markers


def _price_chart_svg(data: PromptInput) -> str:
    """株価チャートの**フォールバック用** SVG 折れ線を組み立てる（P12-5）。

    レポート本体は Lightweight Charts のローソク足（`app.ai.price_chart`）を使うが、
    サンドボックス iframe がスクリプトを許可しない環境や初期化に失敗した場合に備え、
    `.price-chart-host`（`price_chart_container_id`）の中身として初期表示しておき、
    JS の初期化に成功したときだけ差し替える（SPEC §2.7.6）。
    """
    dates, closes = _parse_price_csv(data.price_csv)
    lines = _parse_price_lines(data.indicator_csv, dates)
    valid_dates = set(dates)

    series = [{"label": "終値", "values": closes, "color": "black"}]
    sma_series = [
        {"label": _PRICE_LINE_LABELS[col], "values": lines[col], "color": _PRICE_LINE_COLORS[col]}
        for col in _PRICE_LINE_COLUMNS
    ]
    # データが1つも無い移動平均線は凡例からも外す（P12-2 フィードバック: 財務の図と同じ理由）
    series += _non_empty_series(sma_series)
    markers = _price_markers(data, valid_dates)
    return charts.line_chart(
        dates, series, markers=markers, unit=_price_unit(data.currency), aria="株価と移動平均線・シグナル・開示日の推移"
    )


# ---------------------------------------------------------------------------
# 財務ハイライト（`financials`。P11 が実装するまでの受け皿。SPEC §2.9 / P12-2 の申し送り）
# ---------------------------------------------------------------------------
def _is_missing_value(value: Any) -> bool:
    """`None` または NaN なら真。`app.ai.charts._is_missing` は非公開なのでここに複製する。"""
    if value is None:
        return True
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return False


def _non_empty_series(series: list[dict]) -> list[dict]:
    """全期間が `None`（欠測）の系列を除く。

    `charts.bars_with_line` / `grouped_bars` / `line_chart` は凡例を系列の有無に関わらず
    機械的に並べるため、値が1つも無い系列を渡すと「凡例だけ残って棒/線が無い」状態になる
    （P12-2 フィードバック）。呼び出し側であらかじめ除いておく。
    """
    return [s for s in series if any(not _is_missing_value(v) for v in (s.get("values") or []))]


def _metric_value_display(item: dict) -> str:
    """指標1件（`metrics` の要素、または `interim.items` の要素）の値を表示用文字列にする。

    金額（`JPY`）は**百万円単位の3桁区切り**にする（円のままだと桁が読めないため。
    依頼元の指示）。単位ごとに付ける記号は SPEC §2.9.6 の単位一覧（'JPY'|'JPY/share'|'shares'|
    '%'|'times'|'persons'）に対応させる。
    """
    value = item.get("value")
    if _is_missing_value(value):
        return "—"
    unit = item.get("unit")
    value = float(value)
    if unit == "JPY":
        return charts.format_number(value / _JPY_UNIT_DIVISOR, digits=0) + _JPY_UNIT_LABEL
    if unit == "JPY/share":
        return charts.format_number(value, digits=2) + "円"
    if unit == "%":
        return charts.format_number(value, digits=1) + "%"
    if unit == "times":
        return charts.format_number(value, digits=2) + "倍"
    if unit == "shares":
        return charts.format_number(value, digits=0) + "株"
    if unit == "persons":
        return charts.format_number(value, digits=0) + "人"
    return charts.format_number(value)  # pragma: no cover - 未知の単位向けの保険


def _change_display(change: float | None, kind: str | None) -> dict:
    """前期比（`change_pct`/`change_kind`）を、記号（▲▼→）付きの表示にする。

    `change_kind` が `'pt'`（ROE・自己資本比率・各成長率など `%` 単位の指標）なら「pt」、
    `'pct'`（金額・1株当たり・PER）なら「%」を付ける（SPEC §2.9.6・依頼元の指示3番）。
    `financial_metrics` 側で符号反転・ゼロ除算のケースは既に `None` に落としてあるので、
    ここでは表示の記号付けだけを行う。
    """
    if _is_missing_value(change):
        return {"text": "—", "class": "is-na"}
    change = float(change)
    suffix = "pt" if kind == "pt" else "%"
    if change > 0.05:
        return {"text": f"▲{change:.1f}{suffix}", "class": "is-up"}
    if change < -0.05:
        return {"text": f"▼{abs(change):.1f}{suffix}", "class": "is-down"}
    return {"text": f"→{change:+.1f}{suffix}", "class": "is-flat"}


_TREND_DISPLAY = {
    "改善": {"text": "▲ 改善", "class": "is-up"},
    "悪化": {"text": "▼ 悪化", "class": "is-down"},
    "横ばい": {"text": "→ 横ばい", "class": "is-flat"},
}
_TREND_DISPLAY_NONE = {"text": "—", "class": "is-na"}


def _trend_display(trend: str | None) -> dict:
    return _TREND_DISPLAY.get(trend, _TREND_DISPLAY_NONE)


def _cagr_display(cagr_pct: float | None) -> str:
    if _is_missing_value(cagr_pct):
        return "—"
    return f"{float(cagr_pct):+.1f}%"


def _percentile_display(percentile: float | None) -> str:
    if _is_missing_value(percentile):
        return "—"
    return charts.format_number(percentile, digits=0) + "%"


def _period_labels(financials: dict, n: int) -> list[str]:
    """図の横軸ラベル。**期末日を逆算せず**、両端（最古・最新）だけに実際の日付を置く。

    `financials` には期末日の一覧が無く、`period_count` と両端の日付から中間の期末日を
    作ろうとすると実際の開示スケジュール（決算期変更・訂正報告書の期ずれ等）とずれる
    おそれがある。依頼元の指示により、中間は空欄の相対表記にする。
    """
    if n <= 0:
        return []
    labels = [""] * n
    earliest = financials.get("earliest_period_end")
    latest = financials.get("latest_period_end")
    if earliest:
        labels[0] = str(earliest)
    if latest:
        labels[-1] = str(latest)
    return labels


def _fin_kpi(metric: dict | None, label: str) -> dict:
    """KPI カード1件（値・前期比・トレンド・5期スパークライン）。"""
    if metric is None:
        return {
            "label": label,
            "value_display": "—",
            "delta": {"text": "—", "class": "is-na"},
            "trend_display": _TREND_DISPLAY_NONE,
            "spark_svg": "",
            "note": _FIN_KPI_MISSING_NOTE_DEFAULT,
        }
    value_display = _metric_value_display(metric)
    note = "" if not _is_missing_value(metric.get("value")) else _FIN_KPI_MISSING_NOTE_DEFAULT
    return {
        "label": label,
        "value_display": value_display,
        "delta": _change_display(metric.get("change_pct"), metric.get("change_kind")),
        "trend_display": _trend_display(metric.get("trend")),
        "spark_svg": charts.sparkline(metric.get("history") or [], aria=f"{label}の推移"),
        "note": note,
    }


def _metric_row(metric: dict) -> dict:
    """財務指標の表の1行。"""
    return {
        "label": metric["label"],
        "value_display": _metric_value_display(metric),
        "change_display": _change_display(metric.get("change_pct"), metric.get("change_kind")),
        "trend_display": _trend_display(metric.get("trend")),
        "cagr_display": _cagr_display(metric.get("cagr_pct")),
        "percentile_display": _percentile_display(metric.get("percentile")),
        "source_label": _FIN_SOURCE_LABELS.get(metric.get("source"), metric.get("source") or "—"),
    }


def _metric_is_all_missing(metric: dict) -> bool:
    """値も履歴も全部 `None` の指標なら真（表の行ごと省く。依頼元の指示4番）。"""
    if not _is_missing_value(metric.get("value")):
        return False
    return all(_is_missing_value(v) for v in (metric.get("history") or []))


def _series_in_millions(metric: dict | None) -> list:
    if metric is None:
        return []
    return [None if v is None else float(v) / _JPY_UNIT_DIVISOR for v in (metric.get("history") or [])]


def _fin_performance_svg(labels: list[str], revenue: dict | None, net_income: dict | None) -> str | None:
    """業績5期推移（売上高・当期純利益。営業利益は EDINET から取得できないため出さない）。"""
    bars = _non_empty_series(
        [
            {"label": f"売上高（{_JPY_UNIT_LABEL}）", "values": _series_in_millions(revenue), "color": "blue"},
            {"label": f"当期純利益（{_JPY_UNIT_LABEL}）", "values": _series_in_millions(net_income), "color": "green"},
        ]
    )
    if not bars:
        return None
    return charts.bars_with_line(labels, bars, None, aria="業績5期推移（売上高・当期純利益）")


def _fin_cf_svg(labels: list[str], operating_cf: dict | None, free_cash_flow: dict | None) -> str | None:
    """キャッシュフロー5期推移（営業キャッシュフロー・フリーキャッシュフロー）。

    営業利益と同じ理由で投資CF・財務CFの単独項目は「主要な経営指標等の推移」に無いため、
    取得できる営業CF・FCF（= 営業CF＋投資CF）だけを出す。
    """
    series = _non_empty_series(
        [
            {"label": f"営業CF（{_JPY_UNIT_LABEL}）", "values": _series_in_millions(operating_cf), "color": "blue"},
            {
                "label": f"フリーキャッシュフロー（{_JPY_UNIT_LABEL}）",
                "values": _series_in_millions(free_cash_flow),
                "color": "vermilion",
            },
        ]
    )
    if not series:
        return None
    return charts.grouped_bars(labels, series, aria="キャッシュフロー5期推移（営業CF・フリーキャッシュフロー）")


def _fin_ratio_svg(labels: list[str], equity_ratio: dict | None, roe: dict | None) -> str | None:
    """自己資本比率・ROEの推移。値は `financial_metrics` が既に % の数値（例: 12.5）で返す。"""
    series = _non_empty_series(
        [
            {"label": "自己資本比率", "values": (equity_ratio or {}).get("history") or [], "color": "skyblue"},
            {"label": "ROE", "values": (roe or {}).get("history") or [], "color": "purple"},
        ]
    )
    if not series:
        return None
    return charts.line_chart(labels, series, unit="%", aria="自己資本比率・ROEの推移")


def _fin_per_svg(per: dict | None) -> str | None:
    """PER の過去レンジ（取得できた期間の最小〜最大）と現在値。履歴が無ければ図を出さない。"""
    if per is None:
        return None
    valid = [v for v in (per.get("history") or []) if v is not None]
    if not valid:
        return None
    current = per.get("value")
    if current is None:
        current = valid[-1]
    return charts.bullet(min(valid), max(valid), current, label="PER（倍）", aria="PERの過去レンジと現在値")


def _build_financials(financials: dict | None) -> dict:
    """`app.financial_metrics.compute_metrics()` の戻り値から財務ハイライトの表示材料を作る。

    `financials` が無い・`available: False` のときは「財務数値は未取得です」の1行だけを
    出す材料を返す（SPEC §2.9.7: 空欄を並べず、セクションごと省く）。
    """
    financials = financials or {}
    if not financials.get("available"):
        return {"available": False}

    metrics = list(financials.get("metrics") or [])
    by_key = {m["key"]: m for m in metrics}
    n = financials.get("period_count") or 0
    labels = _period_labels(financials, n)

    kpis = [_fin_kpi(by_key.get(key), label) for key, label in _FIN_KPI_KEYS]

    figures = []
    if n:
        perf = _fin_performance_svg(labels, by_key.get("revenue"), by_key.get("net_income"))
        if perf is not None:
            figures.append({"title": "業績5期推移", "svg": perf})
        cf = _fin_cf_svg(labels, by_key.get("operating_cf"), by_key.get("free_cash_flow"))
        if cf is not None:
            figures.append({"title": "キャッシュフロー5期推移", "svg": cf})
        ratio = _fin_ratio_svg(labels, by_key.get("equity_ratio"), by_key.get("roe"))
        if ratio is not None:
            figures.append({"title": "自己資本比率・ROEの推移", "svg": ratio})
    per_fig = _fin_per_svg(by_key.get("per"))
    if per_fig is not None:
        figures.append({"title": "PERの過去レンジと現在値", "svg": per_fig})

    metric_rows = [_metric_row(m) for m in metrics if not _metric_is_all_missing(m)]

    period_range = None
    if financials.get("earliest_period_end") and financials.get("latest_period_end"):
        period_range = f"{financials['earliest_period_end']} 〜 {financials['latest_period_end']}"

    interim_raw = financials.get("interim")
    interim = None
    if interim_raw:
        interim = {
            "period_end": interim_raw.get("period_end"),
            "prior_period_end": interim_raw.get("prior_period_end"),
            "rows": [
                {
                    "label": item["label"],
                    "value_display": _metric_value_display(item),
                    "change_display": _change_display(item.get("change_pct"), item.get("change_kind")),
                }
                for item in (interim_raw.get("items") or [])
            ],
        }

    return {
        "available": True,
        "standard_label": _STANDARD_LABELS.get(financials.get("standard"), financials.get("standard") or "—"),
        "basis_label": _BASIS_LABELS.get(financials.get("basis"), financials.get("basis") or "—"),
        "period_range": period_range,
        "kpis": kpis,
        "figures": figures,
        "metric_rows": metric_rows,
        "interim": interim,
        "notes": list(financials.get("notes") or []),
    }


def render_report(
    data: PromptInput,
    report: AnalysisReport,
    *,
    model: str,
    generated_at: str | None = None,
    financials: dict | None = None,
) -> str:
    """`PromptInput`（送信データ）と `AnalysisReport`（AI の出力）からレポート HTML を組み立てる。

    表示順は SPEC §2.7.6・P12-2 のとおり「ヘッダ → 総括・判定 → 株価チャート → 財務ハイライト →
    各観点 → リスク → 注目点 → 指標表・シグナル → 開示一覧 → 免責」。`generated_at` を省略すると
    `data.generated_at`（プロンプト生成時刻）を使う。

    `financials` は任意で、`app.financial_metrics.compute_metrics()` の戻り値そのものを渡す
    （形式は同モジュールの docstring・SPEC §2.9 を参照。凍結済み）。省略、または
    `{"available": False}`（財務数値が1件も取得できていない銘柄）のときは、財務ハイライトの
    KPI・図・指標表を出さず「財務数値は未取得です」の1行だけを表示する。

    テンプレートは既定で `app.config.BASE_DIR / "templates"` から探す。テストなど別の場所から
    読ませたい場合は `_environment(templates_dir=...)` を直接使うこと（本関数のシグネチャは
    P6-6 が呼ぶ形のまま固定する。`financials` はキーワード専用の追加引数なので、既存の呼び出しは
    変更なしで動く）。
    """
    env = _environment()
    template = env.get_template(TEMPLATE_NAME)
    latest_rows = [_status_row(card) for card in data.latest]
    context = {
        "symbol": data.symbol,
        "name": data.name,
        "exchange": data.exchange,
        "currency": data.currency,
        "days": data.days,
        "generated_at": generated_at if generated_at is not None else data.generated_at,
        "model": model,
        "report": report,
        "verdict_label": _VERDICT_LABELS.get(report.verdict, report.verdict),
        "confidence_label": _CONFIDENCE_LABELS.get(report.confidence, report.confidence),
        "technical_badge": _technical_badge(data.latest),
        "price_chart_svg": _price_chart_svg(data),
        "price_chart_json": price_chart.payload_json(data),
        "price_chart_library": price_chart.library_source(),
        "price_chart_init": price_chart.init_script(),
        "price_chart_legend": price_chart.legend_items(data),
        "price_chart_container_id": price_chart.CHART_CONTAINER_ID,
        "price_chart_data_id": price_chart.DATA_SCRIPT_ID,
        "fin": _build_financials(financials),
        "latest_categories": _group_latest_rows(latest_rows),
        "signal_rows": [_signal_row(sig) for sig in data.signals],
        "disclosures": [
            {
                "submit_at": d.submit_at,
                "label": d.label,
                "role": _role_label(d.role),
                "description": d.description,
                "reason": d.reason,
                "withdrawn": d.withdrawn,
            }
            for d in data.disclosures
        ],
    }
    return template.render(**context)


def report_path(reports_dir: Path | str, symbol: str, now: datetime | None = None) -> Path:
    """保存先パスを決める（SPEC §2.7.6: `data/reports/report_<symbol>_<YYYYmmdd_HHMMSS>.html`）。

    symbol に含まれる `.`（例: `7203.T`）などファイル名に使いにくい文字はアンダースコアに置き換える。
    同じ秒に複数回呼ばれ、既にそのパスにファイルが存在する場合は `_2` `_3` ... を付けて衝突を避ける
    （`save_report` は返されたパスへ即座に書き込むので、実際の衝突判定はディスク上の存在確認でよい）。
    """
    now_ = now if now is not None else datetime.now()
    reports_dir = Path(reports_dir)
    safe_symbol = _UNSAFE_SYMBOL_CHARS.sub("_", symbol)
    stamp = now_.strftime("%Y%m%d_%H%M%S")
    candidate = reports_dir / f"report_{safe_symbol}_{stamp}.html"
    n = 2
    while candidate.exists():
        candidate = reports_dir / f"report_{safe_symbol}_{stamp}_{n}.html"
        n += 1
    return candidate


def save_report(
    db,
    reports_dir: Path | str,
    symbol: str,
    html: str,
    *,
    model: str,
    in_tokens: int | None = None,
    out_tokens: int | None = None,
    now: datetime | None = None,
) -> dict:
    """HTML を UTF-8 で書き出し、`ai_reports` に1行記録する。"""
    now_ = now if now is not None else datetime.now()
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = report_path(reports_dir, symbol, now_)
    path.write_text(html, encoding="utf-8")

    created_at = now_.strftime("%Y-%m-%d %H:%M:%S")
    with db.write() as conn:
        cur = conn.execute(
            "INSERT INTO ai_reports (symbol, created_at, model, path, in_tokens, out_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (symbol, created_at, model, str(path), in_tokens, out_tokens),
        )
        report_id = cur.lastrowid
    return {
        "id": report_id,
        "symbol": symbol,
        "created_at": created_at,
        "model": model,
        "path": str(path),
        "in_tokens": in_tokens,
        "out_tokens": out_tokens,
    }


def _with_exists(row: dict) -> dict:
    row = dict(row)
    row["exists"] = Path(row["path"]).exists()
    return row


def list_reports(db, *, limit: int | None = None) -> list[dict]:
    """`ai_reports` を新しい順（`created_at` 降順、同時刻は `id` 降順）で返す。

    ファイルが既に消えていても行自体は返し、`exists: False` を付ける（SPEC の一覧表示要件）。
    """
    query = "SELECT id, symbol, created_at, model, path, in_tokens, out_tokens FROM ai_reports " \
        "ORDER BY created_at DESC, id DESC"
    params: tuple = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (limit,)
    with db.connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_with_exists(dict(row)) for row in rows]


def get_report(db, report_id: int) -> dict:
    """1件だけ取得する。無ければ `UserFacingError`。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, symbol, created_at, model, path, in_tokens, out_tokens "
            "FROM ai_reports WHERE id = ?",
            (report_id,),
        ).fetchone()
    if row is None:
        raise UserFacingError(f"レポートが見つかりません（id={report_id}）")
    return _with_exists(dict(row))


def delete_missing(db) -> int:
    """ファイルが実在しなくなった `ai_reports` の行を削除する。戻り値は削除した件数。"""
    with db.write() as conn:
        rows = conn.execute("SELECT id, path FROM ai_reports").fetchall()
        missing_ids = [row["id"] for row in rows if not Path(row["path"]).exists()]
        if missing_ids:
            conn.executemany("DELETE FROM ai_reports WHERE id = ?", [(i,) for i in missing_ids])
    return len(missing_ids)
