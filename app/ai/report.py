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

# 財務ハイライト（`financials`。P11 が渡す想定の形。§SPEC 参照）
_STANDARD_LABELS = {"jgaap": "日本基準", "ifrs": "IFRS", "usgaap": "米国基準"}
_BASIS_LABELS = {"consolidated": "連結", "nonconsolidated": "単体"}


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
def _fin_series(financials: dict, key: str, n: int) -> list:
    values = financials.get(key)
    if not values:
        return [None] * n
    values = list(values)
    if len(values) < n:
        values = values + [None] * (n - len(values))
    return values[:n]


def _fin_amount_display(value: float | None, divisor: float, unit: str) -> str:
    if value is None:
        return "—"
    digits = 1 if unit and unit != "円" else 0
    return charts.format_number(value / divisor, digits=digits) + unit


def _is_missing_value(value: Any) -> bool:
    """`None` または NaN なら真。`app.ai.charts._is_missing` は非公開なのでここに複製する。"""
    if value is None:
        return True
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return False


def _delta_point(current: Any, previous: Any) -> dict:
    """比率指標（ROE・自己資本比率・営業利益率）の増減は「率の変化率」ではなく「ポイント差」で示す。

    `charts.delta_mark()` は比率もそのまま「相対変化率」として扱うため、0.150→0.343 のような
    比率の増減が「▲128.7%」という誤読を招く表示になってしまう（P12-2 フィードバック）。
    `charts.delta_mark` 自体は変更禁止なので、比率専用の表示をここで組み立てる。戻り値の形
    （`text`/`direction`/`class`）は `charts.delta_mark` と揃え、▲▼→ の記号は踏襲する。
    """
    if _is_missing_value(current) or _is_missing_value(previous):
        return {"text": "—", "direction": "na", "class": "is-na"}
    pt = (float(current) - float(previous)) * 100  # 比率(0.xx)の差をポイント(pt)に変換
    if abs(pt) < 0.05:
        return {"text": f"→{pt:+.1f}pt", "direction": "flat", "class": "is-flat"}
    if pt > 0:
        return {"text": f"▲{pt:.1f}pt", "direction": "up", "class": "is-up"}
    return {"text": f"▼{abs(pt):.1f}pt", "direction": "down", "class": "is-down"}


# 値が取得できない KPI カードに添える理由（P12-2 フィードバック: なぜ空欄か分かるようにする）
_FIN_KPI_MISSING_NOTES = {
    "営業利益": "この会計基準では区分表示されていないため取得できません",
}
_FIN_KPI_MISSING_NOTE_DEFAULT = "値を取得できていません（データ不足）"


def _fin_kpi(label: str, values: list, *, kind: str) -> dict:
    values = list(values or [])
    current = values[-1] if values else None
    previous = values[-2] if len(values) >= 2 else None
    if kind == "amount":
        divisor, unit = charts.scale_unit(values)
        value_display = _fin_amount_display(current, divisor, unit)
        delta = charts.delta_mark(current, previous)
    elif kind == "percent":
        value_display = charts.format_percent(current)
        delta = _delta_point(current, previous)
    elif kind == "eps":
        value_display = "—" if current is None else charts.format_number(current, digits=2) + "円"
        delta = charts.delta_mark(current, previous)
    else:  # pragma: no cover - 将来の拡張向けの保険
        value_display = charts.format_number(current)
        delta = charts.delta_mark(current, previous)
    note = ""
    if _is_missing_value(current):
        note = _FIN_KPI_MISSING_NOTES.get(label, _FIN_KPI_MISSING_NOTE_DEFAULT)
    return {
        "label": label,
        "value_display": value_display,
        "delta": delta,
        "spark_svg": charts.sparkline(values, aria=f"{label}の推移"),
        "note": note,
    }


def _non_empty_series(series: list[dict]) -> list[dict]:
    """全期間が `None`（欠測）の系列を除く。

    `charts.bars_with_line` / `grouped_bars` / `line_chart` は凡例を系列の有無に関わらず
    機械的に並べるため、値が1つも無い系列を渡すと「凡例だけ残って棒/線が無い」状態になる
    （P12-2 フィードバック）。呼び出し側であらかじめ除いておく。
    """
    return [s for s in series if any(not _is_missing_value(v) for v in (s.get("values") or []))]


def _fin_scaled_series(*value_lists: list) -> tuple[float, str, list[list]]:
    combined: list = [v for values in value_lists for v in values]
    divisor, unit = charts.scale_unit(combined)
    scaled = [[None if v is None else v / divisor for v in values] for values in value_lists]
    return divisor, unit, scaled


def _fin_performance_svg(periods: list[str], revenue: list, operating_income: list, net_income: list, operating_margin: list) -> str:
    _, unit, (rev_s, op_s, net_s) = _fin_scaled_series(revenue, operating_income, net_income)
    suffix = f"（{unit}）" if unit else ""
    bars = _non_empty_series(
        [
            {"label": f"売上高{suffix}", "values": rev_s, "color": "blue"},
            {"label": f"営業利益{suffix}", "values": op_s, "color": "orange"},
            {"label": f"純利益{suffix}", "values": net_s, "color": "green"},
        ]
    )
    line = {
        "label": "営業利益率",
        "values": [None if v is None else v * 100 for v in operating_margin],
        "color": "vermilion",
        "unit": "%",
    }
    return charts.bars_with_line(periods, bars, line, aria="業績5期推移（売上高・営業利益・純利益・営業利益率）")


def _fin_cf_svg(periods: list[str], cf: dict) -> str:
    n = len(periods)
    operating = _fin_series(cf, "operating", n)
    investing = _fin_series(cf, "investing", n)
    financing = _fin_series(cf, "financing", n)
    _, unit, (op_s, inv_s, fin_s) = _fin_scaled_series(operating, investing, financing)
    suffix = f"（{unit}）" if unit else ""
    series = _non_empty_series(
        [
            {"label": f"営業CF{suffix}", "values": op_s, "color": "blue"},
            {"label": f"投資CF{suffix}", "values": inv_s, "color": "vermilion"},
            {"label": f"財務CF{suffix}", "values": fin_s, "color": "green"},
        ]
    )
    return charts.grouped_bars(periods, series, aria="キャッシュフロー5期推移（営業・投資・財務）")


def _fin_ratio_svg(periods: list[str], equity_ratio: list, roe: list) -> str:
    series = _non_empty_series(
        [
            {"label": "自己資本比率", "values": [None if v is None else v * 100 for v in equity_ratio], "color": "skyblue"},
            {"label": "ROE", "values": [None if v is None else v * 100 for v in roe], "color": "purple"},
        ]
    )
    return charts.line_chart(periods, series, unit="%", aria="自己資本比率・ROEの推移")


def _fin_per_svg(per: dict) -> str:
    return charts.bullet(
        per.get("low"), per.get("high"), per.get("current"), label="PER（倍）", aria="PERの過去レンジと現在値"
    )


def _build_financials(financials: dict | None) -> dict:
    """`financials`（P11 が渡す想定の形。省略可）から財務ハイライトの表示材料を作る。

    `financials` が無い・空のときはプレースホルダ用の情報だけを返す（セクション自体は消さない）。
    """
    financials = financials or {}
    periods = list(financials.get("periods") or [])
    if not periods:
        return {
            "available": False,
            "placeholder_svg": charts.placeholder(
                "財務数値はまだ取得していません（有価証券報告書からの取り込み機能の追加後、"
                "ここに業績・キャッシュフロー・自己資本比率などの推移が表示されます）",
                width=680,
                height=140,
            ),
        }

    n = len(periods)
    revenue = _fin_series(financials, "revenue", n)
    operating_income = _fin_series(financials, "operating_income", n)
    net_income = _fin_series(financials, "net_income", n)
    operating_margin = _fin_series(financials, "operating_margin", n)
    equity_ratio = _fin_series(financials, "equity_ratio", n)
    roe = _fin_series(financials, "roe", n)
    eps = _fin_series(financials, "eps", n)
    cf = financials.get("cf") or {}
    per = financials.get("per") or {}

    kpis = [
        _fin_kpi("売上高", revenue, kind="amount"),
        _fin_kpi("営業利益", operating_income, kind="amount"),
        _fin_kpi("EPS", eps, kind="eps"),
        _fin_kpi("自己資本比率", equity_ratio, kind="percent"),
        _fin_kpi("ROE", roe, kind="percent"),
    ]

    return {
        "available": True,
        "standard_label": _STANDARD_LABELS.get(financials.get("standard"), financials.get("standard") or "—"),
        "basis_label": _BASIS_LABELS.get(financials.get("basis"), financials.get("basis") or "—"),
        "period_range": f"{periods[0]} 〜 {periods[-1]}",
        "kpis": kpis,
        "performance_svg": _fin_performance_svg(periods, revenue, operating_income, net_income, operating_margin),
        "cf_svg": _fin_cf_svg(periods, cf),
        "ratio_svg": _fin_ratio_svg(periods, equity_ratio, roe),
        "per_svg": _fin_per_svg(per),
        "placeholder_svg": None,
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

    `financials` は任意（P11: 財務数値の取り込みが未実装のため）。渡さなければ財務ハイライトの
    セクションにプレースホルダを表示する。形式は本モジュールの docstring・PLAN §4 の申し送りを参照。

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
