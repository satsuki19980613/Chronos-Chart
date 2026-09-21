"""P12-3: レポートの株価チャート（Lightweight Charts）用データ組み立て（SPEC §2.7.6）。

`app.ai.price_chart` は DB を触らず `PromptInput` だけを入力にする純粋関数の集まりなので、
合成の `PromptInput` を組み立てるだけでテストできる（ネットワーク・DB 一切不要）。
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from app.ai import price_chart
from app.ai.prompt import DisclosureItem, PromptInput

SYMBOL = "7203.T"


def _prompt_input(**overrides) -> PromptInput:
    base = dict(
        symbol=SYMBOL,
        name="トヨタ自動車",
        exchange="東証プライム",
        currency="JPY",
        days=20,
        price_csv=(
            "date,open,high,low,close,volume\n"
            "2025-01-06,100,101,99,100,1000\n"
            "2025-01-07,100,102,98,101,1200\n"
            "2025-01-08,101,103,100,99,900\n"
        ),
        indicator_csv=(
            "date,sma_5,sma_25,sma_75\n"
            "2025-01-06,100,,\n"
            "2025-01-07,100.5,,\n"
            "2025-01-08,100.2,,\n"
        ),
        latest=(),
        signals=(
            MappingProxyType(
                {"date": "2025-01-06", "direction": "buy", "label": "ゴールデンクロス(25/75)", "short": "GC"}
            ),
        ),
        disclosures=(),
        generated_at="2025-01-09 09:00:00",
    )
    base.update(overrides)
    return PromptInput(**base)


def _disclosure(submit_at: str, label: str = "有価証券報告書") -> DisclosureItem:
    return DisclosureItem(
        submit_at=submit_at,
        label=label,
        description="",
        reason="",
        role="filer",
        withdrawn=False,
    )


# ---------------------------------------------------------------------------
# build_payload: candles / volumes / lines
# ---------------------------------------------------------------------------
def test_candles_drop_rows_with_missing_ohlc():
    data = _prompt_input(
        price_csv=(
            "date,open,high,low,close,volume\n"
            "2025-01-06,100,101,99,100,1000\n"
            "2025-01-07,,102,98,101,1200\n"  # open が欠測 -> 落ちる
            "2025-01-08,101,103,100,99,900\n"
        ),
    )
    payload = price_chart.build_payload(data)
    times = [c["time"] for c in payload["candles"]]
    assert times == ["2025-01-06", "2025-01-08"]


def test_volumes_only_rows_with_volume_and_up_flag():
    data = _prompt_input(
        price_csv=(
            "date,open,high,low,close,volume\n"
            "2025-01-06,100,101,99,105,1000\n"  # close >= open -> up
            "2025-01-07,100,102,98,90,\n"  # volume 欠測 -> volumes に入らない
            "2025-01-08,101,103,100,99,900\n"  # close < open -> down
        ),
    )
    payload = price_chart.build_payload(data)
    assert [v["time"] for v in payload["volumes"]] == ["2025-01-06", "2025-01-08"]
    by_time = {v["time"]: v for v in payload["volumes"]}
    assert by_time["2025-01-06"]["up"] is True
    assert by_time["2025-01-08"]["up"] is False


def test_volumes_up_defaults_true_when_open_or_close_missing():
    data = _prompt_input(
        price_csv=(
            "date,open,high,low,close,volume\n"
            "2025-01-06,,101,99,,1000\n"
        ),
    )
    payload = price_chart.build_payload(data)
    assert payload["volumes"][0]["up"] is True


def test_all_none_sma_series_excluded_from_lines():
    data = _prompt_input(
        indicator_csv=(
            "date,sma_5,sma_25,sma_75\n"
            "2025-01-06,100,,\n"
            "2025-01-07,100.5,,\n"
            "2025-01-08,100.2,,\n"
        ),
    )
    payload = price_chart.build_payload(data)
    labels = [line["label"] for line in payload["lines"]]
    assert labels == ["SMA5"]  # SMA25/SMA75 は全欠測なので出ない


def test_line_data_points_skip_missing_values():
    data = _prompt_input(
        indicator_csv=(
            "date,sma_5,sma_25,sma_75\n"
            "2025-01-06,100,,\n"
            "2025-01-07,,,\n"  # sma_5 が欠測 -> この日の点は無い
            "2025-01-08,100.2,,\n"
        ),
    )
    payload = price_chart.build_payload(data)
    sma5 = next(line for line in payload["lines"] if line["label"] == "SMA5")
    assert [p["time"] for p in sma5["data"]] == ["2025-01-06", "2025-01-08"]


def test_palette_var_matches_report_html_chart_variable_order():
    """`templates/report.html.j2` の --chart-1〜8 は charts.PALETTE の並び順に対応する。"""
    assert price_chart.palette_var("orange") == "--chart-1"
    assert price_chart.palette_var("skyblue") == "--chart-2"
    assert price_chart.palette_var("green") == "--chart-3"
    assert price_chart.palette_var("yellow") == "--chart-4"
    assert price_chart.palette_var("blue") == "--chart-5"
    assert price_chart.palette_var("vermilion") == "--chart-6"
    assert price_chart.palette_var("purple") == "--chart-7"
    assert price_chart.palette_var("black") == "--chart-8"


def test_lines_include_color_var_for_css_resolution():
    data = _prompt_input()
    payload = price_chart.build_payload(data)
    sma5 = next(line for line in payload["lines"] if line["label"] == "SMA5")
    assert sma5["colorVar"] == "--chart-1"  # orange
    assert sma5["color"] == "#E69F00"


def test_markers_include_color_var_for_css_resolution():
    data = _prompt_input(
        signals=(MappingProxyType({"date": "2025-01-06", "direction": "buy", "label": "GC", "short": "GC"}),),
        disclosures=(_disclosure("2025-01-07 09:00:00"),),
    )
    payload = price_chart.build_payload(data)
    by_time = {m["time"]: m for m in payload["markers"]}
    assert by_time["2025-01-06"]["colorVar"] == "--chart-3"  # green（買い）
    assert by_time["2025-01-07"]["colorVar"] == "--chart-2"  # skyblue（開示）


def test_build_payload_includes_candle_color_vars_and_fallback_colors():
    data = _prompt_input()
    payload = price_chart.build_payload(data)
    # ローソク足だけは専用の CSS 変数（ダークで SMA5 のオレンジと紛れないようにするため）
    assert payload["candleUpVar"] == "--candle-up"
    assert payload["candleDownVar"] == "--candle-down"
    assert payload["candleUpColor"] == "#b3261e"
    assert payload["candleDownColor"] == "#0072B2"


@pytest.mark.parametrize(
    "currency, expected_digits, expected_unit",
    [
        ("JPY", 0, "円"),
        ("jpy", 0, "円"),
        ("USD", 2, " USD"),
        ("", 2, ""),
    ],
)
def test_price_digits_and_unit_depend_on_currency(currency, expected_digits, expected_unit):
    data = _prompt_input(currency=currency)
    payload = price_chart.build_payload(data)
    assert payload["priceDigits"] == expected_digits
    assert payload["unit"] == expected_unit


# ---------------------------------------------------------------------------
# markers
# ---------------------------------------------------------------------------
def test_gc_dc_signals_become_arrows_with_text():
    data = _prompt_input(
        signals=(
            MappingProxyType({"date": "2025-01-06", "direction": "buy", "label": "GC", "short": "GC"}),
            MappingProxyType({"date": "2025-01-07", "direction": "sell", "label": "DC", "short": "DC"}),
        ),
    )
    payload = price_chart.build_payload(data)
    by_time = {m["time"]: m for m in payload["markers"]}
    buy = by_time["2025-01-06"]
    assert buy["shape"] == "arrowUp"
    assert buy["text"] == "GC"
    assert buy["size"] == 1
    assert buy["position"] == "belowBar"
    sell = by_time["2025-01-07"]
    assert sell["shape"] == "arrowDown"
    assert sell["text"] == "DC"
    assert sell["position"] == "aboveBar"


def test_non_cross_signals_become_small_circles():
    data = _prompt_input(
        signals=(
            MappingProxyType({"date": "2025-01-06", "direction": "buy", "label": "何か", "short": "X"}),
        ),
    )
    payload = price_chart.build_payload(data)
    marker = payload["markers"][0]
    assert marker["shape"] == "circle"
    assert marker["text"] == ""
    assert marker["size"] == 0.5


def test_disclosure_marker_is_above_bar_circle():
    data = _prompt_input(signals=(), disclosures=(_disclosure("2025-01-07 09:00:00"),))
    payload = price_chart.build_payload(data)
    marker = payload["markers"][0]
    assert marker["time"] == "2025-01-07"
    assert marker["position"] == "aboveBar"
    assert marker["shape"] == "circle"
    assert marker["size"] == 1


def test_markers_outside_price_csv_are_dropped():
    data = _prompt_input(
        signals=(
            MappingProxyType({"date": "2025-01-06", "direction": "buy", "label": "IN", "short": "GC"}),
            MappingProxyType({"date": "2099-12-31", "direction": "sell", "label": "OUT", "short": "DC"}),
        ),
        disclosures=(_disclosure("2099-12-31 09:00:00"),),
    )
    payload = price_chart.build_payload(data)
    assert [m["time"] for m in payload["markers"]] == ["2025-01-06"]


def test_markers_sorted_ascending_by_time():
    data = _prompt_input(
        signals=(
            MappingProxyType({"date": "2025-01-08", "direction": "sell", "label": "X", "short": "DC"}),
            MappingProxyType({"date": "2025-01-06", "direction": "buy", "label": "Y", "short": "GC"}),
        ),
        disclosures=(_disclosure("2025-01-07 09:00:00"),),
    )
    payload = price_chart.build_payload(data)
    times = [m["time"] for m in payload["markers"]]
    assert times == sorted(times)
    assert times == ["2025-01-06", "2025-01-07", "2025-01-08"]


# ---------------------------------------------------------------------------
# payload_json / escape_for_script
# ---------------------------------------------------------------------------
def test_escape_for_script_removes_raw_angle_brackets_and_amp():
    raw = 'テキスト</script><script>alert(1)</script>&amp;  '
    escaped = price_chart.escape_for_script(raw)
    assert "<" not in escaped
    assert ">" not in escaped
    assert "&" not in escaped
    assert " " not in escaped
    assert " " not in escaped
    assert "\\u003c" in escaped
    assert "\\u003e" in escaped
    assert "\\u0026" in escaped


def test_payload_json_has_no_raw_angle_brackets_or_amp():
    data = _prompt_input()
    text = price_chart.payload_json(data)
    assert "<" not in text
    assert ">" not in text
    # JSON の構造上どうしても出る `&` は無い想定（本文にも含めていないため）だが、
    # 万一混入しても escape_for_script を経由していればここには出ない
    assert "&" not in text


def test_payload_json_round_trips_to_same_payload():
    import json

    data = _prompt_input()
    payload = price_chart.build_payload(data)
    text = price_chart.payload_json(data)
    # \uXXXX エスケープは JSON パーサが正しく元の文字に戻す
    assert json.loads(text) == payload


# ---------------------------------------------------------------------------
# library_source / init_script
# ---------------------------------------------------------------------------
def test_library_source_contains_license_and_global():
    source = price_chart.library_source()
    assert "Apache License" in source
    assert "LightweightCharts" in source


def test_library_source_is_cached():
    assert price_chart.library_source() is price_chart.library_source()


def test_init_script_is_non_empty_and_does_not_contain_closing_script_tag():
    script = price_chart.init_script()
    assert script.strip() != ""
    assert "</script>" not in script


def test_init_script_is_cached():
    assert price_chart.init_script() is price_chart.init_script()


def test_init_script_resolves_colors_from_css_variables():
    """色は CSS カスタムプロパティ経由で解決し、ダーク/印刷時に読み直す仕組みがあること。"""
    script = price_chart.init_script()
    assert "getComputedStyle" in script
    assert "prefers-color-scheme" in script
    assert "beforeprint" in script
    assert "afterprint" in script
    assert "setMarkers" in script


# ---------------------------------------------------------------------------
# legend_items
# ---------------------------------------------------------------------------
def test_legend_items_includes_candle_and_present_sma_only():
    data = _prompt_input()
    items = price_chart.legend_items(data)
    kinds_labels = [(i["kind"], i["label"]) for i in items]
    assert ("candle", "ローソク足（陽線/陰線）") in kinds_labels
    assert ("line", "SMA5") in kinds_labels
    assert ("line", "SMA25") not in kinds_labels
    assert ("line", "SMA75") not in kinds_labels


def test_legend_items_omits_buy_signal_when_no_signals():
    data = _prompt_input(signals=())
    items = price_chart.legend_items(data)
    labels = [i["label"] for i in items]
    assert "買いシグナル" not in labels
    assert "売りシグナル" not in labels


def test_legend_items_includes_buy_signal_when_present():
    data = _prompt_input()  # デフォルトで buy シグナルを1つ持つ
    items = price_chart.legend_items(data)
    labels = [i["label"] for i in items]
    assert "買いシグナル" in labels


def test_legend_items_omits_disclosure_when_none():
    data = _prompt_input(disclosures=())
    items = price_chart.legend_items(data)
    assert "開示" not in [i["label"] for i in items]


def test_legend_items_includes_disclosure_when_present():
    data = _prompt_input(disclosures=(_disclosure("2025-01-07 09:00:00"),))
    items = price_chart.legend_items(data)
    assert "開示" in [i["label"] for i in items]


def test_legend_items_candle_uses_two_colors():
    data = _prompt_input()
    items = price_chart.legend_items(data)
    candle = next(i for i in items if i["kind"] == "candle")
    assert candle["color"] != candle["color2"]
    assert candle["color"] and candle["color2"]
