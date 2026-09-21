"""P12-1: レポート用インライン SVG 生成（`app/ai/charts.py`）。

ネットワークには一切アクセスしない。合成データだけを使う。

レポートは外部参照ゼロの単一 HTML で、サンドボックス付き iframe（`sandbox=""`）にも
埋め込まれるため、`<script` や `http://` / `https://` が出力に一切現れないことを
最重要項目として検査する。
"""

from __future__ import annotations

import re

import pytest

from app.ai import charts

# ---------------------------------------------------------------------------
# 共通ヘルパー
# ---------------------------------------------------------------------------

XSS_LABEL = "<script>alert(1)</script>"


def _assert_well_formed_svg(svg: str) -> None:
    """すべての図に共通する最低限の構造を検査する。"""
    assert svg.startswith("<svg")
    assert svg.endswith("</svg>")
    assert "viewBox=" in svg
    assert 'role="img"' in svg
    assert "<title>" in svg
    assert "<desc>" in svg


def _assert_no_external_reference(svg: str) -> None:
    """外部参照ゼロの担保: <script> と http(s):// が一切出ないこと。"""
    assert "<script" not in svg
    # 名前空間の宣言（何も取りに行かない）だけは許す。それ以外の URL は外部参照なので禁止
    stripped = svg.replace(f'xmlns="{charts.SVG_NS}"', "")
    assert "http://" not in stripped
    assert "https://" not in stripped


def _dates(n: int, start: str = "2025-06-01") -> list[str]:
    import datetime

    base = datetime.date.fromisoformat(start)
    return [(base + datetime.timedelta(days=i)).isoformat() for i in range(n)]


# ---------------------------------------------------------------------------
# scale_unit / format_number / format_percent / delta_mark
# ---------------------------------------------------------------------------


class TestScaleUnit:
    def test_trillion(self):
        assert charts.scale_unit([7_798_650_000_000]) == (1e12, "兆円")

    def test_hundred_million(self):
        divisor, label = charts.scale_unit([250_000_000_00])  # 2.5億
        assert divisor == 1e8
        assert label == "億円"

    def test_million(self):
        divisor, label = charts.scale_unit([12_000_000])
        assert divisor == 1e6
        assert label == "百万円"

    def test_yen(self):
        assert charts.scale_unit([1234]) == (1.0, "円")

    def test_empty_or_all_none(self):
        assert charts.scale_unit([]) == (1.0, "")
        assert charts.scale_unit([None, None]) == (1.0, "")

    def test_boundary_exact_trillion(self):
        divisor, _ = charts.scale_unit([1e12])
        assert divisor == 1e12

    def test_negative_values_use_absolute(self):
        divisor, label = charts.scale_unit([-2_000_000_000_000])
        assert (divisor, label) == (1e12, "兆円")


class TestFormatNumber:
    def test_none_is_dash(self):
        assert charts.format_number(None) == "—"

    def test_thousands_separator(self):
        assert charts.format_number(1234567) == "1,234,567"

    def test_digits(self):
        assert charts.format_number(1234.5678, digits=2) == "1,234.57"

    def test_nan_is_dash(self):
        assert charts.format_number(float("nan")) == "—"

    def test_zero(self):
        assert charts.format_number(0) == "0"


class TestFormatPercent:
    def test_basic(self):
        assert charts.format_percent(0.343) == "34.3%"

    def test_none_is_dash(self):
        assert charts.format_percent(None) == "—"

    def test_digits(self):
        assert charts.format_percent(0.05, digits=0) == "5%"

    def test_negative(self):
        assert charts.format_percent(-0.1) == "-10.0%"

    def test_zero(self):
        assert charts.format_percent(0.0) == "0.0%"


class TestDeltaMark:
    def test_up(self):
        result = charts.delta_mark(112.3, 100.0)
        assert result["direction"] == "up"
        assert result["text"].startswith("↑")
        assert result["class"] == "is-up"

    def test_down(self):
        result = charts.delta_mark(90.0, 100.0)
        assert result["direction"] == "down"
        assert result["text"].startswith("↓")
        assert result["class"] == "is-down"

    def test_flat(self):
        result = charts.delta_mark(100.0, 100.0)
        assert result["direction"] == "flat"
        assert result["text"].startswith("→")

    def test_na_current_none(self):
        result = charts.delta_mark(None, 100.0)
        assert result["direction"] == "na"
        assert result["text"] == "—"

    def test_na_previous_none(self):
        result = charts.delta_mark(100.0, None)
        assert result["direction"] == "na"

    def test_previous_zero_positive_current(self):
        result = charts.delta_mark(5.0, 0.0)
        assert result["direction"] == "up"
        assert result["text"].startswith("↑")

    def test_previous_zero_negative_current(self):
        result = charts.delta_mark(-5.0, 0.0)
        assert result["direction"] == "down"

    def test_both_zero(self):
        result = charts.delta_mark(0.0, 0.0)
        assert result["direction"] == "flat"

    def test_nan_treated_as_missing(self):
        result = charts.delta_mark(float("nan"), 100.0)
        assert result["direction"] == "na"


# ---------------------------------------------------------------------------
# placeholder
# ---------------------------------------------------------------------------


class TestPlaceholder:
    def test_structure(self):
        svg = charts.placeholder("データなし")
        _assert_well_formed_svg(svg)
        _assert_no_external_reference(svg)
        assert charts.CHART_CLASS in svg

    def test_escapes_message(self):
        svg = charts.placeholder(XSS_LABEL)
        assert "<script>" not in svg
        assert "&lt;script&gt;" in svg


# ---------------------------------------------------------------------------
# sparkline
# ---------------------------------------------------------------------------


class TestSparkline:
    def test_empty_returns_empty_string(self):
        assert charts.sparkline([]) == ""

    def test_single_value_returns_empty_string(self):
        assert charts.sparkline([42]) == ""

    def test_normal(self):
        svg = charts.sparkline([1, 2, 3, 2, 4, None, 5])
        _assert_well_formed_svg(svg)
        _assert_no_external_reference(svg)

    def test_all_none_does_not_raise(self):
        svg = charts.sparkline([None, None, None])
        # 例外にならないことが最重要。<svg> である必要はあるが線が無くてもよい
        assert svg == "" or svg.startswith("<svg")


# ---------------------------------------------------------------------------
# line_chart
# ---------------------------------------------------------------------------


class TestLineChart:
    def test_normal_two_series(self):
        dates = _dates(60)
        series = [
            {"label": "終値", "values": [100 + i * 0.5 for i in range(60)], "color": "orange"},
            {"label": "SMA25", "values": [100 + i * 0.4 for i in range(60)], "color": "blue"},
        ]
        svg = charts.line_chart(dates, series, aria="株価とSMA")
        _assert_well_formed_svg(svg)
        _assert_no_external_reference(svg)
        # 出力サイズの目安: 60日の折れ線が 10KB 未満
        assert len(svg.encode("utf-8")) < 10 * 1024

    def test_none_breaks_the_line(self):
        dates = _dates(10)
        values = [1, 2, None, 4, 5, 6, 7, 8, 9, 10]
        series = [{"label": "値", "values": values, "color": "orange"}]
        svg = charts.line_chart(dates, series)
        paths = re.findall(r"<path\b[^>]*>", svg)
        # None の前後で経路が分断され、少なくとも2本の <path> になる
        assert len(paths) >= 2

    def test_markers(self):
        dates = _dates(10)
        series = [{"label": "終値", "values": list(range(10)), "color": "orange"}]
        markers = [
            {"date": dates[2], "kind": "buy", "label": "買い"},
            {"date": dates[5], "kind": "sell", "label": "売り"},
            {"date": dates[7], "kind": "disclosure", "label": "有報"},
        ]
        svg = charts.line_chart(dates, series, markers=markers)
        _assert_well_formed_svg(svg)
        assert "<polygon" in svg  # 買い・売りの三角
        assert "<circle" in svg  # 開示の丸

    def test_unknown_marker_kind_ignored(self):
        dates = _dates(5)
        series = [{"label": "値", "values": [1, 2, 3, 4, 5], "color": "orange"}]
        markers = [{"date": dates[1], "kind": "unknown", "label": "?"}]
        svg = charts.line_chart(dates, series, markers=markers)
        _assert_well_formed_svg(svg)

    def test_marker_date_not_in_range_ignored(self):
        dates = _dates(5)
        series = [{"label": "値", "values": [1, 2, 3, 4, 5], "color": "orange"}]
        markers = [{"date": "1999-01-01", "kind": "buy", "label": "買い"}]
        svg = charts.line_chart(dates, series, markers=markers)
        _assert_well_formed_svg(svg)

    def test_empty_dates_does_not_raise(self):
        svg = charts.line_chart([], [])
        assert svg.startswith("<svg")

    def test_all_none_values_does_not_raise(self):
        dates = _dates(5)
        series = [{"label": "値", "values": [None] * 5, "color": "orange"}]
        svg = charts.line_chart(dates, series)
        assert svg.startswith("<svg")

    def test_single_date_does_not_raise(self):
        svg = charts.line_chart(["2025-06-01"], [{"label": "値", "values": [1], "color": "orange"}])
        assert svg.startswith("<svg")

    def test_escapes_label_in_legend(self):
        dates = _dates(5)
        series = [
            {"label": XSS_LABEL, "values": [1, 2, 3, 4, 5], "color": "orange"},
            {"label": "SMA", "values": [1, 2, 3, 4, 5], "color": "blue"},
        ]
        svg = charts.line_chart(dates, series)
        assert "<script>" not in svg
        assert "&lt;script&gt;" in svg

    def test_escapes_marker_label(self):
        dates = _dates(5)
        series = [{"label": "値", "values": [1, 2, 3, 4, 5], "color": "orange"}]
        markers = [{"date": dates[1], "kind": "buy", "label": XSS_LABEL}]
        svg = charts.line_chart(dates, series, markers=markers)
        assert "<script>" not in svg


# ---------------------------------------------------------------------------
# bars_with_line
# ---------------------------------------------------------------------------


class TestBarsWithLine:
    CATEGORIES = ["2021/3期", "2022/3期", "2023/3期", "2024/3期", "2025/3期"]

    def test_normal_with_line(self):
        bars = [{"label": "売上高", "values": [100, 120, 90, 150, 200], "color": "orange"}]
        line = {"label": "ROE", "values": [8.0, 9.5, 7.0, 10.0, 11.0], "color": "blue", "unit": "%"}
        svg = charts.bars_with_line(self.CATEGORIES, bars, line, aria="業績推移")
        _assert_well_formed_svg(svg)
        _assert_no_external_reference(svg)
        assert len(svg.encode("utf-8")) < 5 * 1024

    def test_axis_starts_at_zero_for_all_positive_values(self):
        bars = [{"label": "売上高", "values": [500, 600, 700, 800, 900], "color": "orange"}]
        svg = charts.bars_with_line(self.CATEGORIES, bars)
        # 0 のラベルが軸に出ていること（切り詰めていないこと）
        assert ">0<" in svg or ">0.0<" in svg

    def test_handles_negative_values_with_zero_line(self):
        bars = [{"label": "当期純利益", "values": [-50, 30, -10, 80, 100], "color": "orange"}]
        svg = charts.bars_with_line(self.CATEGORIES, bars)
        _assert_well_formed_svg(svg)
        # 負の値があっても例外にならず、0 を跨いだ描画になる
        assert "<rect" in svg

    def test_no_line_omits_line_axis(self):
        bars = [{"label": "売上高", "values": [100, 120, 90, 150, 200], "color": "orange"}]
        svg = charts.bars_with_line(self.CATEGORIES, bars, None)
        _assert_well_formed_svg(svg)

    def test_single_category_does_not_raise(self):
        svg = charts.bars_with_line(["2025/3期"], [{"label": "売上高", "values": [100], "color": "orange"}])
        assert svg.startswith("<svg")

    def test_empty_categories_does_not_raise(self):
        svg = charts.bars_with_line([], [])
        assert svg.startswith("<svg")

    def test_all_none_values_does_not_raise(self):
        bars = [{"label": "売上高", "values": [None] * 5, "color": "orange"}]
        svg = charts.bars_with_line(self.CATEGORIES, bars)
        assert svg.startswith("<svg")

    def test_escapes_category_label(self):
        bars = [{"label": "売上高", "values": [1, 2, 3], "color": "orange"}]
        svg = charts.bars_with_line([XSS_LABEL, "b", "c"], bars)
        assert "<script>" not in svg
        assert "&lt;script&gt;" in svg


# ---------------------------------------------------------------------------
# grouped_bars
# ---------------------------------------------------------------------------


class TestGroupedBars:
    CATEGORIES = ["2021/3期", "2022/3期", "2023/3期", "2024/3期", "2025/3期"]

    def test_normal(self):
        series = [
            {"label": "営業CF", "values": [120, -30, 90, 60, 150], "color": "green"},
            {"label": "投資CF", "values": [-40, -20, -60, -10, -80], "color": "vermilion"},
        ]
        svg = charts.grouped_bars(self.CATEGORIES, series, aria="キャッシュフロー")
        _assert_well_formed_svg(svg)
        _assert_no_external_reference(svg)
        assert len(svg.encode("utf-8")) < 5 * 1024

    def test_negative_values_draw_zero_line(self):
        series = [{"label": "投資CF", "values": [-40, -20, -60, -10, -80], "color": "vermilion"}]
        svg = charts.grouped_bars(self.CATEGORIES, series)
        _assert_well_formed_svg(svg)
        assert "<rect" in svg

    def test_empty_series_does_not_raise(self):
        svg = charts.grouped_bars(self.CATEGORIES, [])
        assert svg.startswith("<svg")

    def test_single_category_does_not_raise(self):
        svg = charts.grouped_bars(["2025/3期"], [{"label": "営業CF", "values": [100], "color": "green"}])
        assert svg.startswith("<svg")


# ---------------------------------------------------------------------------
# bullet
# ---------------------------------------------------------------------------


class TestBullet:
    def test_normal(self):
        svg = charts.bullet(8.0, 15.0, 12.5, label="PER（過去5期レンジ）")
        _assert_well_formed_svg(svg)
        _assert_no_external_reference(svg)

    def test_current_outside_range(self):
        svg = charts.bullet(8.0, 15.0, 20.0, label="PER")
        assert svg.startswith("<svg")

    def test_missing_values_returns_placeholder_like_svg(self):
        svg = charts.bullet(None, 15.0, 12.5, label="PER")
        assert svg.startswith("<svg")
        assert svg.endswith("</svg>")

    def test_low_equals_high(self):
        svg = charts.bullet(10.0, 10.0, 10.0, label="ROE")
        assert svg.startswith("<svg")

    def test_low_greater_than_high_is_swapped(self):
        # 呼び出し側が high/low を取り違えても例外にならない
        svg = charts.bullet(15.0, 8.0, 12.0, label="PER")
        assert svg.startswith("<svg")

    def test_escapes_label(self):
        svg = charts.bullet(8.0, 15.0, 12.5, label=XSS_LABEL)
        assert "<script>" not in svg
        assert "&lt;script&gt;" in svg


# ---------------------------------------------------------------------------
# 全図共通: class 定数
# ---------------------------------------------------------------------------


def test_chart_class_constant():
    assert charts.CHART_CLASS == "cc-chart"


def test_palette_is_okabe_ito():
    expected = {
        "orange": "#E69F00",
        "skyblue": "#56B4E9",
        "green": "#009E73",
        "yellow": "#F0E442",
        "blue": "#0072B2",
        "vermilion": "#D55E00",
        "purple": "#CC79A7",
        "black": "#000000",
    }
    assert charts.PALETTE == expected


@pytest.mark.parametrize(
    "func_name, args",
    [
        ("placeholder", ("メッセージ",)),
        ("line_chart", (_dates(5), [{"label": "値", "values": [1, 2, 3, 4, 5], "color": "orange"}])),
        (
            "bars_with_line",
            (
                ["a", "b", "c"],
                [{"label": "値", "values": [1, 2, 3], "color": "orange"}],
            ),
        ),
        (
            "grouped_bars",
            (["a", "b", "c"], [{"label": "値", "values": [1, -2, 3], "color": "orange"}]),
        ),
        ("bullet", (1.0, 2.0, 1.5)),
    ],
)
def test_no_external_reference_across_charts(func_name, args):
    """全図に共通する外部参照ゼロの担保。"""
    func = getattr(charts, func_name)
    svg = func(*args)
    _assert_no_external_reference(svg)
    _assert_well_formed_svg(svg)
