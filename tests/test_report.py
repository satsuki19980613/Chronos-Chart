"""P6-5: レポート生成（SPEC §2.7.6・§3 `ai_reports`）。

ネットワークには一切アクセスしない。合成の `PromptInput` / `AnalysisReport` と、
必要なら合成データを入れた一時 DB だけを使う。

最重要: **需給データ（空売り残高・貸借取引残高）をレポートに載せない**（CLAUDE.md 不変条件1）ことを
番兵値方式で固定する。`app/ai/report.py` のソースに需給を示す識別子が出現しないことも検査する。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

import numpy as np
import pytest
from conftest import make_prices

from app import disclosures
from app.ai import report
from app.ai.prompt import DisclosureItem, PromptInput, PromptSource
from app.ai.schema import AnalysisReport, SectionAnalysis
from app.database import Database
from app.errors import UserFacingError

SYMBOL = "7203.T"
CODE = "7203"

# 番兵値（現実にはあり得ない値）。需給3テーブルに入れ、レポート HTML のどこにも出ないことを確かめる。
SENTINEL_HOLDER = "SENTINEL_SHORT_XYZ"
SENTINEL_NOTE = "SENTINEL_SHORT_NOTE_999"
SENTINEL_QTY = 999999999
SENTINEL_MARGIN_KIND = "SENTINEL_MARGIN_KIND_ABC"
SENTINEL_MARGIN_BALANCE = 888888888

SENTINEL_STRINGS = [SENTINEL_HOLDER, SENTINEL_NOTE, SENTINEL_MARGIN_KIND]
SENTINEL_NUMBERS = [str(SENTINEL_QTY), str(SENTINEL_MARGIN_BALANCE)]


# ---------------------------------------------------------------------------
# 準備用ヘルパー
# ---------------------------------------------------------------------------
def _db(tmp_path) -> Database:
    db = Database(tmp_path / "report.db")
    db.init_schema()
    return db


def _register(db: Database, days: int = 150, symbol: str = SYMBOL, code: str = CODE) -> None:
    db.upsert_stock(symbol, code, "トヨタ自動車", "東証プライム", "JPY")
    prices = make_prices(100 + np.sin(np.arange(days) / 7) * 5 + np.arange(days) * 0.05)
    db.upsert_prices(symbol, prices)


def _insert_sentinel_supply_data(db: Database, symbol: str = SYMBOL) -> None:
    """需給3テーブル（空売り個別・空売り合計・貸借取引残高）に番兵値を入れる。"""
    with db.write() as conn:
        conn.execute(
            """
            INSERT INTO short_positions
                (symbol, calc_date, holder_id, holder, ratio, ratio_delta, quantity, qty_delta, note)
            VALUES (?, '2025-06-02', 'H1', ?, 12.34, 0.1, ?, 100, ?)
            """,
            (symbol, SENTINEL_HOLDER, SENTINEL_QTY, SENTINEL_NOTE),
        )
        conn.execute(
            """
            INSERT INTO short_totals (symbol, date, total_ratio, total_qty, holders)
            VALUES (?, '2025-06-02', 45.6, ?, 3)
            """,
            (symbol, SENTINEL_QTY),
        )
        conn.execute(
            """
            INSERT INTO margin_balances
                (symbol, date, settle_date, kind, yushi_new, yushi_repay, yushi_balance,
                 kashi_new, kashi_repay, kashi_balance, net_balance, fetched_at)
            VALUES (?, '2025-06-02', '2025-06-03', ?, 100, 50, ?, 10, 5, 20, 80, '2025-06-02 09:00:00')
            """,
            (symbol, SENTINEL_MARGIN_KIND, SENTINEL_MARGIN_BALANCE),
        )


def _insert_disclosure(
    db: Database,
    doc_id: str,
    submit_at: str,
    *,
    symbol: str = SYMBOL,
    doc_type_code: str = "120",
    role: str = "filer",
    description: str = "有価証券報告書の提出",
    reason: str | None = None,
    withdrawal: int | None = None,
) -> None:
    category = disclosures.classify(doc_type_code)
    with db.write() as conn:
        conn.execute(
            """
            INSERT INTO disclosures (doc_id, doc_type_code, description, reason, submit_at, withdrawal, category)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (doc_id, doc_type_code, description, reason, submit_at, withdrawal, category),
        )
        conn.execute(
            "INSERT INTO disclosure_links (doc_id, symbol, role) VALUES (?, ?, ?)",
            (doc_id, symbol, role),
        )


def _assert_no_sentinel(text: str) -> None:
    for s in SENTINEL_STRINGS + SENTINEL_NUMBERS:
        assert s not in text, f"番兵値 {s!r} がレポートに含まれている"


def _prompt_input(**overrides) -> PromptInput:
    base = dict(
        symbol="7203.T",
        name="トヨタ自動車",
        exchange="東証プライム",
        currency="JPY",
        days=20,
        price_csv="date,open,high,low,close,volume\n2025-01-06,100,101,99,100,1000\n",
        indicator_csv="date,sma_short\n2025-01-06,100\n",
        latest=(
            MappingProxyType(
                {"key": "sma", "label": "移動平均", "status": "bull", "value": 12.3, "note": "上昇中"}
            ),
            MappingProxyType(
                {"key": "rsi", "label": "RSI 中期(14)", "status": "neutral", "value": None, "note": "判定不能"}
            ),
        ),
        signals=(
            MappingProxyType(
                {"date": "2025-01-06", "direction": "buy", "label": "ゴールデンクロス(25/75)", "short": "GC"}
            ),
        ),
        disclosures=(),
        generated_at="2025-01-07 09:00:00",
    )
    base.update(overrides)
    return PromptInput(**base)


def _analysis_report(**overrides) -> AnalysisReport:
    base = dict(
        technical=SectionAnalysis(evidence=["SMAが上向き"], assessment="上昇基調にある"),
        disclosure=SectionAnalysis(evidence=["特段の開示なし"], assessment="材料は乏しい"),
        risks=["急な悪材料が出た場合の下振れリスク"],
        watch_points=["次回の有価証券報告書の提出時期"],
        verdict="bullish",
        confidence="medium",
        summary="総じて強気",
    )
    base.update(overrides)
    return AnalysisReport(**base)


# ---------------------------------------------------------------------------
# 番兵値テスト（最重要。SPEC §2.7.3・§10.2 に相当）
# ---------------------------------------------------------------------------
def test_sentinel_values_never_appear_in_report(tmp_path):
    db = _db(tmp_path)
    _register(db)
    _insert_sentinel_supply_data(db)

    data = PromptSource(db).load(SYMBOL, 60)
    html = report.render_report(data, _analysis_report(), model="gemini-2.5-flash")

    _assert_no_sentinel(html)


def test_ai_report_source_does_not_reference_supply_identifiers():
    """`app/ai/report.py` に需給を示す識別子が出現しないこと（CLAUDE.md 不変条件1）。"""
    path = Path(__file__).resolve().parent.parent / "app" / "ai" / "report.py"
    text = path.read_text(encoding="utf-8")
    for token in ["short_", "margin_", "taisyaku", "karauri"]:
        assert token not in text, f"report.py に禁止識別子 {token!r} が含まれている"


def test_report_template_does_not_reference_supply_identifiers():
    """テンプレートにも需給を示す識別子・語が出現しないこと。"""
    path = Path(__file__).resolve().parent.parent / "templates" / "report.html.j2"
    text = path.read_text(encoding="utf-8")
    for token in ["空売り", "貸借", "信用残", "需給", "short_", "margin_", "taisyaku", "karauri"]:
        assert token not in text, f"report.html.j2 に禁止語 {token!r} が含まれている"


# ---------------------------------------------------------------------------
# 免責事項
# ---------------------------------------------------------------------------
def test_disclaimer_contains_required_points():
    html = report.render_report(_prompt_input(), _analysis_report(), model="gemini-2.5-flash")
    assert "投資助言ではない" in html
    assert "検証していない" in html
    assert "EDINET" in html
    assert "決算短信" in html


# ---------------------------------------------------------------------------
# エスケープ（AI 出力に HTML タグが混ざっても escape されること）
# ---------------------------------------------------------------------------
def test_ai_output_is_escaped():
    malicious = _analysis_report(summary="<script>alert(1)</script>")
    html = report.render_report(_prompt_input(), malicious, model="gemini-2.5-flash")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_evidence_and_risks_are_escaped():
    malicious = _analysis_report(
        technical=SectionAnalysis(evidence=['<img src=x onerror="alert(1)">'], assessment="ok"),
        risks=["<b>太字リスク</b>"],
    )
    html = report.render_report(_prompt_input(), malicious, model="gemini-2.5-flash")
    assert "<img src=x" not in html
    assert "<b>太字リスク</b>" not in html


# ---------------------------------------------------------------------------
# verdict / confidence の日本語変換
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "verdict, label",
    [("bullish", "強気"), ("bearish", "弱気"), ("neutral", "中立")],
)
def test_verdict_is_translated_to_japanese(verdict, label):
    html = report.render_report(_prompt_input(), _analysis_report(verdict=verdict), model="m")
    assert label in html


@pytest.mark.parametrize(
    "confidence, label",
    [("low", "低"), ("medium", "中"), ("high", "高")],
)
def test_confidence_is_translated_to_japanese(confidence, label):
    html = report.render_report(_prompt_input(), _analysis_report(confidence=confidence), model="m")
    assert label in html


# ---------------------------------------------------------------------------
# ヘッダ・モデル名・期間
# ---------------------------------------------------------------------------
def test_header_includes_stock_info_and_model():
    html = report.render_report(_prompt_input(), _analysis_report(), model="gemini-2.5-flash")
    assert "トヨタ自動車" in html
    assert "7203.T" in html
    assert "東証プライム" in html
    assert "JPY" in html
    assert "gemini-2.5-flash" in html
    assert "直近20日" in html


def test_generated_at_override():
    html = report.render_report(
        _prompt_input(), _analysis_report(), model="m", generated_at="2030-01-01 00:00:00"
    )
    assert "2030-01-01 00:00:00" in html


# ---------------------------------------------------------------------------
# 指標表・シグナル
# ---------------------------------------------------------------------------
def test_indicator_table_has_required_columns_and_values():
    html = report.render_report(_prompt_input(), _analysis_report(), model="m")
    assert "指標" in html
    assert "判定" in html
    assert "値" in html
    assert "備考" in html
    assert "移動平均" in html
    assert "強気" in html  # status "bull" -> 強気
    assert "上昇中" in html
    assert "12.3" in html


def test_signal_is_listed():
    html = report.render_report(_prompt_input(), _analysis_report(), model="m")
    assert "ゴールデンクロス(25/75)" in html
    assert "買い" in html  # direction "buy" -> 買い


# ---------------------------------------------------------------------------
# 開示一覧
# ---------------------------------------------------------------------------
def test_disclosures_empty_shows_note():
    html = report.render_report(_prompt_input(disclosures=()), _analysis_report(), model="m")
    assert "期間内の開示はありません" in html


def test_disclosure_listed_with_details():
    items = (
        DisclosureItem(
            submit_at="2025-01-06T09:00",
            label="有報",
            description="有価証券報告書の提出",
            reason="定時提出",
            role="filer",
            withdrawn=False,
        ),
    )
    html = report.render_report(_prompt_input(disclosures=items), _analysis_report(), model="m")
    assert "有報" in html
    assert "有価証券報告書の提出" in html
    assert "定時提出" in html
    # role は画面と同じく日本語で見せる（生の "filer" は出さない）
    assert "提出者" in html
    assert "filer" not in html


def test_withdrawn_disclosure_is_marked():
    items = (
        DisclosureItem(
            submit_at="2025-01-06T09:00",
            label="有報",
            description="訂正",
            reason="",
            role="filer",
            withdrawn=True,
        ),
    )
    html = report.render_report(_prompt_input(disclosures=items), _analysis_report(), model="m")
    assert "取下げ済み" in html


def test_disclosure_from_db_via_prompt_source_does_not_use_kessan_label(tmp_path):
    """開示ラベルに『決算』を単独で使わない（CLAUDE.md 不変条件6）ことを、実データ経路でも確認する。"""
    db = _db(tmp_path)
    _register(db, days=150)
    last_date = db.get_prices(SYMBOL)["date"].iloc[-1]
    _insert_disclosure(db, "D_REPORT", f"{last_date}T09:00", doc_type_code="120")

    data = PromptSource(db).load(SYMBOL, 20)
    html = report.render_report(data, _analysis_report(), model="m")
    assert data.disclosures[0].label != "決算"
    # 免責の「決算短信」は複合語なので許容されるが、開示一覧の種別セルには単独の「決算」は出ない
    assert "<td>決算</td>" not in html


# ---------------------------------------------------------------------------
# 単一ファイル完結（外部参照が無いこと）
# ---------------------------------------------------------------------------
def test_html_is_self_contained_single_file():
    html = report.render_report(_prompt_input(), _analysis_report(), model="m")
    assert "<link" not in html
    assert "src=" not in html
    assert "<script" not in html
    # href はページ内リンク（#...）のみ許可
    import re

    for m in re.finditer(r'href="([^"]*)"', html):
        assert m.group(1).startswith("#"), f"外部参照の href が見つかった: {m.group(1)}"


# ---------------------------------------------------------------------------
# P12-2: レポートの視覚強化（図・financials・折りたたみ）
# ---------------------------------------------------------------------------
def _sample_financials() -> dict:
    """`financials` の合成データ（P11 が渡す想定の形。SPEC・PLAN §4 の申し送り参照）。"""
    return {
        "standard": "ifrs",
        "basis": "consolidated",
        "periods": ["2022/3期", "2023/3期", "2024/3期", "2025/3期", "2026/3期"],
        "revenue": [3.0e13, 3.1e13, 3.7e13, 4.5e13, 4.8e13],
        "operating_income": [2.9e12, 3.0e12, 5.3e12, 5.35e12, 4.8e12],
        "net_income": [2.85e12, 2.45e12, 4.9e12, 4.77e12, 3.9e12],
        "operating_margin": [0.097, 0.097, 0.143, 0.119, 0.10],
        "cf": {
            "operating": [3.0e12, 3.5e12, 4.9e12, 5.1e12, 4.5e12],
            "investing": [-2.0e12, -2.2e12, -2.8e12, -3.0e12, -2.5e12],
            "financing": [-0.8e12, -1.0e12, -1.5e12, -1.8e12, -1.2e12],
        },
        "equity_ratio": [0.36, 0.37, 0.38, 0.39, 0.40],
        "roe": [0.085, 0.075, 0.132, 0.121, 0.095],
        "eps": [180.5, 155.2, 330.8, 320.1, 270.4],
        "per": {"low": 8.5, "high": 15.2, "current": 10.8},
    }


def test_price_chart_is_rendered():
    """優先度A: 株価チャート（終値＋SMA5/25/75）が入っていること。"""
    html = report.render_report(_prompt_input(), _analysis_report(), model="m")
    chart_section = html.split('id="price-chart"')[1].split("<h2")[0]
    assert "<svg" in chart_section
    assert "cc-chart--line" in chart_section


def test_multiple_svgs_present():
    html = report.render_report(_prompt_input(), _analysis_report(), model="m")
    assert html.count("<svg") >= 2


def test_price_chart_markers_exclude_dates_outside_price_csv():
    """マーカーの日付は株価 CSV に存在する日付だけに絞る（期間外の日付は渡さない）。"""
    data = _prompt_input(
        price_csv=(
            "date,open,high,low,close,volume\n"
            "2025-01-06,100,101,99,100,1000\n"
            "2025-01-07,100,102,98,101,1200\n"
        ),
        indicator_csv=(
            "date,sma_5,sma_25,sma_75\n2025-01-06,100,,\n2025-01-07,100.5,,\n"
        ),
        signals=(
            MappingProxyType(
                {"date": "2025-01-06", "direction": "buy", "label": "INRANGE_SIGNAL", "short": "GC"}
            ),
            MappingProxyType(
                {"date": "2099-12-31", "direction": "sell", "label": "OUTOFRANGE_SIGNAL", "short": "DC"}
            ),
        ),
        disclosures=(),
    )
    html = report.render_report(data, _analysis_report(), model="m")
    chart_section = html.split('id="price-chart"')[1].split("<h2")[0]
    assert "INRANGE_SIGNAL" in chart_section
    assert "OUTOFRANGE_SIGNAL" not in chart_section


def test_financials_none_renders_placeholder_without_error():
    """`financials` を省略しても例外にならず、プレースホルダが出ること（セクション自体は消さない）。"""
    html = report.render_report(_prompt_input(), _analysis_report(), model="m")
    assert "財務ハイライト" in html
    assert "財務数値はまだ取得していません" in html
    assert '<div class="kpi-grid">' not in html


def test_financials_empty_dict_also_renders_placeholder():
    html = report.render_report(_prompt_input(), _analysis_report(), model="m", financials={})
    assert "財務数値はまだ取得していません" in html


def test_financials_provided_renders_kpi_cards_and_charts():
    """優先度A: financials を渡すと KPI カードと各図が出ること。"""
    html = report.render_report(
        _prompt_input(), _analysis_report(), model="m", financials=_sample_financials()
    )
    assert "kpi-grid" in html
    for label in ("売上高", "営業利益", "EPS", "自己資本比率", "ROE"):
        assert label in html
    assert "IFRS" in html
    assert "連結" in html
    assert "2022/3期" in html and "2026/3期" in html
    assert "cc-chart--bars-line" in html  # 業績5期推移
    assert "cc-chart--grouped-bars" in html  # キャッシュフロー5期推移
    assert "cc-chart--bullet" in html  # PER レンジ
    # 良化・悪化を色だけに頼らない: ▲▼ の記号が併記されること
    assert "▲" in html or "▼" in html


def test_no_script_tag_anywhere():
    """サンドボックス iframe で表示するため <script> は一切出力しない。"""
    html = report.render_report(
        _prompt_input(), _analysis_report(), model="m", financials=_sample_financials()
    )
    assert "<script" not in html


def test_details_used_for_collapsing_extra_indicators():
    """指標値の詳細: 主要指標以外は <details> に折りたたむこと。"""
    latest = (
        MappingProxyType({"key": "sma", "label": "移動平均", "status": "bull", "value": 12.3, "note": "上昇中"}),
        MappingProxyType({"key": "macd", "label": "MACD", "status": "bull", "value": 1.1, "note": "上"}),
        MappingProxyType({"key": "adx", "label": "DMI/ADX", "status": "neutral", "value": 20.0, "note": "弱い"}),
        MappingProxyType({"key": "rsi", "label": "RSI 中期(14)", "status": "neutral", "value": 55.0, "note": "中立圏"}),
        MappingProxyType({"key": "rci", "label": "RCI 短期(9)", "status": "neutral", "value": 10.0, "note": "中立圏"}),
    )
    html = report.render_report(_prompt_input(latest=latest), _analysis_report(), model="m")
    assert "<details" in html
    assert "そのほかの" in html


def test_print_and_dark_mode_css_present():
    html = report.render_report(_prompt_input(), _analysis_report(), model="m")
    assert "@media print" in html
    assert "@media (prefers-color-scheme: dark)" in html


def test_generated_html_size_is_reasonable():
    html = report.render_report(_prompt_input(), _analysis_report(), model="m")
    assert len(html.encode("utf-8")) < 200_000
    html_fin = report.render_report(
        _prompt_input(), _analysis_report(), model="m", financials=_sample_financials()
    )
    assert len(html_fin.encode("utf-8")) < 200_000


# ---------------------------------------------------------------------------
# P12-2 フィードバック対応（実データ確認後の修正）
# ---------------------------------------------------------------------------
def test_ratio_kpi_delta_is_point_difference_not_percent_of_percent():
    """比率指標（ROE・自己資本比率）の増減は「率の変化率」ではなく「ポイント差」で示す。

    0.150 -> 0.343 を charts.delta_mark() にそのまま通すと「▲128.7%」という誤読を招く表示に
    なる（(0.343-0.150)/0.150 = 128.7%）。report.py 側でポイント差（19.3pt）に変換すること。
    """
    financials = _sample_financials()
    financials["roe"] = [0.150, 0.150, 0.150, 0.150, 0.343]
    html = report.render_report(
        _prompt_input(), _analysis_report(), model="m", financials=financials
    )
    assert "19.3pt" in html
    assert "128.7%" not in html
    # 自己資本比率も同様（サンプルデータは 0.39 -> 0.40 = 1.0pt）
    assert "1.0pt" in html


def test_kpi_delta_has_no_direction_based_color_class():
    """KPI カードの増減は色で良し悪しを言わない（▲▼の記号だけで方向を示す。本文色のまま）。

    `charts.delta_mark` 由来の "is-up"/"is-down" というクラス名自体は残ってよいが、
    それに色（--bull/--bear）を割り当てる CSS ルールを持たないことを検査する。
    """
    html = report.render_report(
        _prompt_input(), _analysis_report(), model="m", financials=_sample_financials()
    )
    assert ".kpi-delta.is-up" not in html
    assert ".kpi-delta.is-down" not in html
    assert ".kpi-delta.is-flat" not in html


def test_all_none_series_dropped_from_financial_chart_legends():
    """全期間が None の系列は、業績・CF・比率いずれの図でも凡例から外す。"""
    financials = _sample_financials()
    financials["operating_income"] = [None] * 5  # IFRS で営業利益を区分表示していない想定
    html = report.render_report(
        _prompt_input(), _analysis_report(), model="m", financials=financials
    )
    assert "売上高（兆円）" in html
    assert "営業利益（兆円）" not in html


def test_sma75_color_differs_from_sma25():
    """SMA25 と SMA75 が両方とも青系だと重なったとき見分けがつかないため、別系統の色にする。"""
    dates = [f"2025-01-{d:02d}" for d in range(1, 21)]
    price_rows = ["date,open,high,low,close,volume"]
    ind_rows = ["date,sma_5,sma_25,sma_75"]
    for i, d in enumerate(dates):
        c = 100 + i
        price_rows.append(f"{d},{c-1},{c+1},{c-2},{c},1000")
        ind_rows.append(f"{d},{c-0.5},{c-1.0},{c-1.5}")
    data = _prompt_input(
        price_csv="\n".join(price_rows) + "\n",
        indicator_csv="\n".join(ind_rows) + "\n",
        signals=(),
        disclosures=(),
    )
    html = report.render_report(data, _analysis_report(), model="m")
    chart_section = html.split('id="price-chart"')[1].split("<h2")[0]
    color_25 = re.search(r'stroke="([^"]+)"[^>]*/><text[^>]*>SMA25</text>', chart_section)
    color_75 = re.search(r'stroke="([^"]+)"[^>]*/><text[^>]*>SMA75</text>', chart_section)
    assert color_25 is not None and color_75 is not None
    assert color_25.group(1) != color_75.group(1)


def test_kpi_card_without_value_shows_a_reason():
    """値が取得できない KPI カードには、なぜ空なのか分かる理由を添える。"""
    financials = _sample_financials()
    financials["operating_income"] = [None] * 5
    html = report.render_report(
        _prompt_input(), _analysis_report(), model="m", financials=financials
    )
    assert "kpi-note" in html
    assert "取得できません" in html


def test_technical_badge_has_disambiguating_caveat():
    """テクニカルバッジは AI の総合判定とは別の集計であることを明示する。"""
    html = report.render_report(_prompt_input(), _analysis_report(), model="m")
    section = html.split('id="technical"')[1].split("<h2")[0]
    assert "総合判定" in section
    assert "とは別" in section


def test_body_is_centered():
    """広いウィンドウで開いても左に寄らないよう、本文は中央寄せにする。"""
    html = report.render_report(_prompt_input(), _analysis_report(), model="m")
    assert "margin: 0 auto;" in html


# ---------------------------------------------------------------------------
# report_path
# ---------------------------------------------------------------------------
def test_report_path_format(tmp_path):
    now = datetime(2025, 6, 1, 12, 34, 56)
    path = report.report_path(tmp_path, "7203.T", now=now)
    assert path.parent == tmp_path
    assert path.name == "report_7203_T_20250601_123456.html"


def test_report_path_avoids_collision_within_same_second(tmp_path):
    now = datetime(2025, 6, 1, 12, 34, 56)
    first = report.report_path(tmp_path, "7203.T", now=now)
    first.write_text("dummy", encoding="utf-8")  # save_report が直後に書き込む挙動を模す
    second = report.report_path(tmp_path, "7203.T", now=now)
    assert first != second
    assert second.name == "report_7203_T_20250601_123456_2.html"


# ---------------------------------------------------------------------------
# save_report / list_reports / get_report / delete_missing
# ---------------------------------------------------------------------------
def test_save_report_writes_file_and_db_row(tmp_path):
    db = _db(tmp_path)
    reports_dir = tmp_path / "reports"
    now = datetime(2025, 6, 1, 9, 0, 0)

    result = report.save_report(
        db, reports_dir, SYMBOL, "<html><body>テスト</body></html>",
        model="gemini-2.5-flash", in_tokens=123, out_tokens=45, now=now,
    )

    path = Path(result["path"])
    assert path.exists()
    assert path.read_text(encoding="utf-8") == "<html><body>テスト</body></html>"
    assert result["symbol"] == SYMBOL
    assert result["model"] == "gemini-2.5-flash"
    assert result["in_tokens"] == 123
    assert result["out_tokens"] == 45
    assert result["id"] is not None

    with db.connect() as conn:
        row = conn.execute("SELECT * FROM ai_reports WHERE id = ?", (result["id"],)).fetchone()
    assert row is not None
    assert row["symbol"] == SYMBOL
    assert row["path"] == str(path)


def test_list_reports_returns_newest_first(tmp_path):
    db = _db(tmp_path)
    reports_dir = tmp_path / "reports"
    r1 = report.save_report(
        db, reports_dir, SYMBOL, "<html>1</html>", model="m",
        now=datetime(2025, 1, 1, 9, 0, 0),
    )
    r2 = report.save_report(
        db, reports_dir, SYMBOL, "<html>2</html>", model="m",
        now=datetime(2025, 1, 2, 9, 0, 0),
    )
    rows = report.list_reports(db)
    assert [r["id"] for r in rows] == [r2["id"], r1["id"]]
    assert all(r["exists"] for r in rows)


def test_list_reports_limit(tmp_path):
    db = _db(tmp_path)
    reports_dir = tmp_path / "reports"
    for i in range(3):
        report.save_report(
            db, reports_dir, SYMBOL, f"<html>{i}</html>", model="m",
            now=datetime(2025, 1, 1 + i, 9, 0, 0),
        )
    rows = report.list_reports(db, limit=2)
    assert len(rows) == 2


def test_list_reports_marks_missing_file_as_not_exists(tmp_path):
    db = _db(tmp_path)
    reports_dir = tmp_path / "reports"
    result = report.save_report(db, reports_dir, SYMBOL, "<html></html>", model="m")
    Path(result["path"]).unlink()

    rows = report.list_reports(db)
    assert len(rows) == 1
    assert rows[0]["exists"] is False


def test_get_report_returns_single_row(tmp_path):
    db = _db(tmp_path)
    reports_dir = tmp_path / "reports"
    result = report.save_report(db, reports_dir, SYMBOL, "<html></html>", model="m")

    row = report.get_report(db, result["id"])
    assert row["id"] == result["id"]
    assert row["exists"] is True


def test_get_report_missing_raises(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(UserFacingError):
        report.get_report(db, 9999)


def test_delete_missing_removes_only_rows_without_file(tmp_path):
    db = _db(tmp_path)
    reports_dir = tmp_path / "reports"
    kept = report.save_report(db, reports_dir, SYMBOL, "<html></html>", model="m")
    removed = report.save_report(
        db, reports_dir, SYMBOL, "<html></html>", model="m",
        now=datetime(2025, 1, 2, 9, 0, 0),
    )
    Path(removed["path"]).unlink()

    deleted_count = report.delete_missing(db)
    assert deleted_count == 1

    remaining_ids = [r["id"] for r in report.list_reports(db)]
    assert remaining_ids == [kept["id"]]


# ---------------------------------------------------------------------------
# 公開名の確認（P6-6 が参照する定数）
# ---------------------------------------------------------------------------
def test_template_name_constant():
    assert report.TEMPLATE_NAME == "report.html.j2"
    template_path = Path(__file__).resolve().parent.parent / "templates" / report.TEMPLATE_NAME
    assert template_path.exists()
