"""P6-3: スキーマとプロンプト（SPEC §2.7.3・§2.7.4・§10.2）。

ネットワークには一切アクセスしない。合成データのみを使う。

最重要: **需給データ（空売り残高・貸借取引残高）を AI に送らない**（CLAUDE.md 不変条件1）ことを
番兵値方式で固定する。加えて `app/ai/` 配下のソースに需給を示す識別子が出現しないことも検査する。
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import numpy as np
import pytest
from conftest import make_prices

from app import ai_export, disclosures
from app.ai import prompt as prompt_mod
from app.ai.prompt import (
    PERIOD_CHOICES,
    RAW_WINDOW_DAYS,
    DisclosureItem,
    PromptInput,
    PromptSource,
    build_prompt,
    build_retry_prompt,
)
from app.ai.schema import AnalysisReport, SectionAnalysis
from app.database import Database
from app.errors import UserFacingError
from app.financials import save_financials

SYMBOL = "7203.T"
CODE = "7203"

# 番兵値（現実にはあり得ない値）。需給3テーブルに入れ、プロンプトのどこにも出ないことを確かめる。
SENTINEL_HOLDER = "SENTINEL_SHORT_XYZ"
SENTINEL_NOTE = "SENTINEL_SHORT_NOTE_999"
SENTINEL_QTY = 999999999
SENTINEL_MARGIN_KIND = "SENTINEL_MARGIN_KIND_ABC"
SENTINEL_MARGIN_BALANCE = 888888888


# ---------------------------------------------------------------------------
# 準備用ヘルパー
# ---------------------------------------------------------------------------
def _db(tmp_path) -> Database:
    db = Database(tmp_path / "ai_prompt.db")
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


# ---------------------------------------------------------------------------
# 財務指標（P11-5）: 合成データの準備用ヘルパー
# ---------------------------------------------------------------------------
FIN_PERIODS = ["2022-03-31", "2023-03-31", "2024-03-31", "2025-03-31", "2026-03-31"]


def _fin_row(period_end: str, item: str, value: float, *, basis: str = "consolidated", period_type: str = "FY", unit: str = "JPY") -> dict:
    return {"period_end": period_end, "item": item, "value": value, "unit": unit, "basis": basis, "period_type": period_type}


def _save_full_financials(db: Database, symbol: str = SYMBOL) -> None:
    """5期分の通期データ（IFRS・連結）と、前年同期がある中間期を保存する（合成データ）。

    `per`（PER）は一度も保存しない。全期欠損の指標が行ごと省かれることを確認するため
    （SPEC §2.9.7）。値は実データではなく、桁区切り表記の確認用に SPEC §2.9.7 の例と同じ
    直近売上高（7,798,650百万円）を使っている。
    """
    revenue = [7_000_000_000_000, 7_200_000_000_000, 7_400_000_000_000, 7_243_650_000_000, 7_798_650_000_000]
    net_income = [500_000_000_000, 550_000_000_000, 600_000_000_000, 580_000_000_000, 650_000_000_000]
    eps = [300.0, 320.0, 340.0, 330.0, 360.0]
    total_assets = [18_000_000_000_000, 19_000_000_000_000, 20_000_000_000_000, 20_500_000_000_000, 21_000_000_000_000]
    equity = [6_000_000_000_000, 6_300_000_000_000, 6_600_000_000_000, 6_900_000_000_000, 7_200_000_000_000]
    operating_cf = [600_000_000_000, 650_000_000_000, 700_000_000_000, 680_000_000_000, 750_000_000_000]
    investing_cf = [-100_000_000_000, -120_000_000_000, -110_000_000_000, -130_000_000_000, -140_000_000_000]
    roe = [8.0, 9.0, 10.0, 11.0, 12.0]
    equity_ratio = [40.0, 41.0, 42.0, 43.0, 44.0]
    payout_ratio = [30.0, 30.0, 30.0, 30.0, 30.0]

    for i, period_end in enumerate(FIN_PERIODS):
        rows = [
            _fin_row(period_end, "revenue", revenue[i]),
            _fin_row(period_end, "net_income", net_income[i]),
            _fin_row(period_end, "eps", eps[i], unit="JPY/share"),
            _fin_row(period_end, "total_assets", total_assets[i]),
            _fin_row(period_end, "equity", equity[i]),
            _fin_row(period_end, "operating_cf", operating_cf[i]),
            _fin_row(period_end, "investing_cf", investing_cf[i]),
            _fin_row(period_end, "roe", roe[i], unit="%"),
            _fin_row(period_end, "equity_ratio", equity_ratio[i], unit="%"),
            _fin_row(period_end, "payout_ratio", payout_ratio[i], unit="%"),
        ]
        doc = {
            "doc_id": f"DOC_FY_{i}",
            "submit_at": f"{period_end[:4]}-06-25 09:00:00",
            "period_end": period_end,
            "standard": "ifrs",
        }
        save_financials(db, symbol, doc, rows)

    interim_rows = [
        _fin_row("2025-09-30", "revenue", 4_000_000_000_000, period_type="HY"),
        _fin_row("2025-09-30", "net_income", 300_000_000_000, period_type="HY"),
        _fin_row("2025-09-30", "eps", 180.0, period_type="HY", unit="JPY/share"),
    ]
    prior_rows = [
        _fin_row("2024-09-30", "revenue", 3_800_000_000_000, period_type="HY"),
        _fin_row("2024-09-30", "net_income", 280_000_000_000, period_type="HY"),
        _fin_row("2024-09-30", "eps", 170.0, period_type="HY", unit="JPY/share"),
    ]
    save_financials(
        db, symbol,
        {"doc_id": "DOC_HY_1", "submit_at": "2025-11-10 09:00:00", "period_end": "2025-09-30", "standard": "ifrs"},
        interim_rows,
    )
    save_financials(
        db, symbol,
        {"doc_id": "DOC_HY_0", "submit_at": "2024-11-08 09:00:00", "period_end": "2024-09-30", "standard": "ifrs"},
        prior_rows,
    )


SENTINEL_STRINGS = [SENTINEL_HOLDER, SENTINEL_NOTE, SENTINEL_MARGIN_KIND]
SENTINEL_NUMBERS = [str(SENTINEL_QTY), str(SENTINEL_MARGIN_BALANCE)]


def _assert_no_sentinel(text: str) -> None:
    for s in SENTINEL_STRINGS + SENTINEL_NUMBERS:
        assert s not in text, f"番兵値 {s!r} がプロンプトに含まれている"


# ---------------------------------------------------------------------------
# 番兵値テスト（最重要。SPEC §10.2）
# ---------------------------------------------------------------------------
def test_sentinel_values_never_appear_in_prompt(tmp_path):
    db = _db(tmp_path)
    _register(db)
    _insert_sentinel_supply_data(db)

    data = PromptSource(db).load(SYMBOL, 60)
    prompt = build_prompt(data)
    retry_prompt = build_retry_prompt(data, "検証エラーの本文")

    _assert_no_sentinel(prompt)
    _assert_no_sentinel(retry_prompt)
    # PromptInput 自体（dataclass の repr）にも紛れ込んでいないことを確かめる
    _assert_no_sentinel(repr(data))


def test_ai_sources_do_not_reference_supply_identifiers():
    """`app/ai/` 配下の全 .py ソースに需給を示す識別子が出現しないこと。

    `client.py` / `quota.py` は並行で作られる別タスクなので、無ければスキップせず
    「存在するファイルだけ」検査する。
    """
    ai_dir = Path(__file__).resolve().parent.parent / "app" / "ai"
    forbidden = ["short_", "margin_", "taisyaku", "karauri"]
    py_files = sorted(ai_dir.glob("*.py"))
    assert py_files, "app/ai/ に .py ファイルが見つからない"
    for path in py_files:
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{path.name} に禁止識別子 {token!r} が含まれている"


def test_prompt_source_does_not_reference_service():
    """`PromptSource`（`app/ai/prompt.py`）が `app.service` を import していないこと。"""
    text = (Path(__file__).resolve().parent.parent / "app" / "ai" / "prompt.py").read_text(encoding="utf-8")
    assert "service" not in text


# ---------------------------------------------------------------------------
# 入力検証
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("days", [0, 1, 30, 90, 121, -20])
def test_invalid_days_raises(tmp_path, days):
    db = _db(tmp_path)
    _register(db)
    with pytest.raises(UserFacingError):
        PromptSource(db).load(SYMBOL, days)


@pytest.mark.parametrize("days", list(PERIOD_CHOICES))
def test_valid_days_accepted(tmp_path, days):
    db = _db(tmp_path)
    _register(db)
    data = PromptSource(db).load(SYMBOL, days)
    assert data.days == days


def test_no_prices_raises(tmp_path):
    db = _db(tmp_path)
    db.upsert_stock(SYMBOL, CODE, "トヨタ自動車", "東証プライム", "JPY")
    with pytest.raises(UserFacingError):
        PromptSource(db).load(SYMBOL, 20)


# ---------------------------------------------------------------------------
# CSV ブロック
# ---------------------------------------------------------------------------
def _csv_rows(csv_text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(csv_text)))


def test_csv_blocks_are_ascending_and_within_days(tmp_path):
    db = _db(tmp_path)
    _register(db, days=150)
    data = PromptSource(db).load(SYMBOL, 20)

    price_rows = _csv_rows(data.price_csv)
    indicator_rows = _csv_rows(data.indicator_csv)
    assert 0 < len(price_rows) <= 20
    assert len(indicator_rows) == len(price_rows)

    price_dates = [r["date"] for r in price_rows]
    assert price_dates == sorted(price_dates)
    assert list(price_rows[0].keys())[:6] == ["date", "open", "high", "low", "close", "volume"]
    assert indicator_rows[0]["date"] == price_dates[0]


def test_csv_row_count_is_capped_at_raw_window(tmp_path):
    """P11-5 でセマンティクスが変わった: `price_csv` は圧縮（SPEC §2.9.8）により、`days` に関わらず
    直近 `RAW_WINDOW_DAYS` 日ぶん（生CSV）で一定になる。`days` に応じて増えるのはそれより前の
    `compressed`（要約）区間の営業日数の方なので、そちらで「日数が増えるほど対象が広がる」ことを確かめる。
    """
    db = _db(tmp_path)
    _register(db, days=150)
    counts = {d: len(_csv_rows(PromptSource(db).load(SYMBOL, d).price_csv)) for d in PERIOD_CHOICES}
    assert counts[20] == counts[60] == counts[120] == RAW_WINDOW_DAYS

    compressed_days = {}
    for d in PERIOD_CHOICES:
        data = PromptSource(db).load(SYMBOL, d)
        compressed_days[d] = data.compressed.trading_days if data.compressed else 0
    assert compressed_days[20] == 0
    assert compressed_days[20] < compressed_days[60] < compressed_days[120]


# ---------------------------------------------------------------------------
# 開示
# ---------------------------------------------------------------------------
def test_disclosures_are_filtered_to_price_range(tmp_path):
    db = _db(tmp_path)
    _register(db, days=150)
    all_dates = db.get_prices(SYMBOL)["date"].tolist()
    in_range_date = all_dates[-5]  # 直近20日の範囲内
    out_of_range_date = all_dates[0]  # 全期間の先頭（150日 >> 20日なので範囲外）

    _insert_disclosure(db, "D_IN_RANGE", f"{in_range_date}T09:00")
    _insert_disclosure(db, "D_OUT_OF_RANGE", f"{out_of_range_date}T09:00")

    data = PromptSource(db).load(SYMBOL, 20)
    submit_dates = {d.submit_at[:10] for d in data.disclosures}
    assert in_range_date in submit_dates
    assert out_of_range_date not in submit_dates
    assert len(data.disclosures) == 1


def test_disclosure_zero_items_still_shows_note(tmp_path):
    db = _db(tmp_path)
    _register(db)
    data = PromptSource(db).load(SYMBOL, 20)
    assert data.disclosures == ()
    prompt = build_prompt(data)
    assert "決算短信・業績修正は含まれない" in prompt


def test_withdrawn_disclosure_is_marked(tmp_path):
    db = _db(tmp_path)
    _register(db, days=150)
    last_date = _csv_rows(PromptSource(db).load(SYMBOL, 20).price_csv)[-1]["date"]
    _insert_disclosure(db, "D_WITHDRAWN", f"{last_date}T09:00", withdrawal=1)

    data = PromptSource(db).load(SYMBOL, 20)
    assert len(data.disclosures) == 1
    item = data.disclosures[0]
    assert item.withdrawn is True

    prompt = build_prompt(data)
    assert "取下げ済み" in prompt


def test_disclosure_label_never_uses_kessan_word(tmp_path):
    """開示のラベルに『決算』を使わない（CLAUDE.md 不変条件6）。EDINET にあるのは有報・半期報。"""
    db = _db(tmp_path)
    _register(db, days=150)
    last_date = _csv_rows(PromptSource(db).load(SYMBOL, 20).price_csv)[-1]["date"]
    _insert_disclosure(db, "D_REPORT", f"{last_date}T09:00", doc_type_code="120")

    data = PromptSource(db).load(SYMBOL, 20)
    assert data.disclosures[0].label != "決算"
    assert "決算" not in data.disclosures[0].label


# ---------------------------------------------------------------------------
# 再依頼プロンプト
# ---------------------------------------------------------------------------
def test_build_retry_prompt_includes_error_body(tmp_path):
    db = _db(tmp_path)
    _register(db)
    data = PromptSource(db).load(SYMBOL, 20)
    retry = build_retry_prompt(data, "confidence: field required")
    assert build_prompt(data) in retry
    assert "confidence: field required" in retry
    assert "スキーマに厳密に従って再生成せよ" in retry


# ---------------------------------------------------------------------------
# スキーマのフィールド順（根拠 → 結論。SPEC §2.7.4）
# ---------------------------------------------------------------------------
def test_section_analysis_field_order():
    assert list(SectionAnalysis.model_fields.keys()) == ["evidence", "assessment"]


def test_analysis_report_field_order():
    # 定義順がそのまま Gemini の出力順になる（SPEC §2.7.4）。「根拠 → 結論」を崩さないこと。
    # `fundamental` は技術と開示の間、`data_scope_note` は末尾（SPEC §2.9.9）
    assert list(AnalysisReport.model_fields.keys()) == [
        "technical",
        "fundamental",
        "disclosure",
        "risks",
        "watch_points",
        "verdict",
        "confidence",
        "summary",
        "data_scope_note",
    ]


def test_analysis_report_list_limits():
    report = AnalysisReport(
        technical=SectionAnalysis(evidence=["a"], assessment="ok"),
        fundamental=SectionAnalysis(evidence=["c"], assessment="ok"),
        disclosure=SectionAnalysis(evidence=["b"], assessment="ok"),
        risks=["r"],
        watch_points=["w"],
        verdict="neutral",
        confidence="low",
        summary="summary",
        data_scope_note="会社予想との比較・同業他社との比較は対象外。需給データは分析に含まない。",
    )
    assert report.verdict == "neutral"
    with pytest.raises(Exception):
        SectionAnalysis(evidence=["1", "2", "3", "4", "5", "6", "7"], assessment="too many")


# ---------------------------------------------------------------------------
# 財務指標（P11-5。SPEC §2.9.7）
# ---------------------------------------------------------------------------
def test_financials_section_uses_million_yen_formatting(tmp_path):
    """`## 財務指標` が出て、金額が百万円単位・3桁区切りになっていること。"""
    db = _db(tmp_path)
    _register(db, days=150)
    _save_full_financials(db)

    data = PromptSource(db).load(SYMBOL, 60)
    prompt = build_prompt(data)

    assert "## 財務指標" in prompt
    # SPEC §2.9.7 の例と同じ桁になる値（直近期の売上高 7,798,650,000,000円）で確認する
    assert "7,798,650百万円" in prompt
    # 生の円の桁（末尾の 6 個の 0 を含む文字列）をそのまま送っていないこと
    assert "7798650000000" not in prompt


def test_financials_unavailable_shows_single_line(tmp_path):
    """財務数値が1件も無い銘柄では、セクションごと出さず「未取得」の1行だけになる。"""
    db = _db(tmp_path)
    _register(db)  # 財務数値は保存しない

    data = PromptSource(db).load(SYMBOL, 20)
    assert data.financials["available"] is False

    prompt = build_prompt(data)
    assert "財務数値は未取得" in prompt
    # 表の体裁（指標の行）が出ていないこと
    assert "前期比" not in prompt.split("## 財務指標", 1)[1].split("## 期間内の開示", 1)[0]


def test_financials_all_missing_metric_is_omitted(tmp_path):
    """全期間で値が無い指標（PER）は行ごと省かれる。"""
    db = _db(tmp_path)
    _register(db, days=150)
    _save_full_financials(db)  # per は一度も保存しない

    data = PromptSource(db).load(SYMBOL, 60)
    financial_section = build_prompt(data).split("## 財務指標", 1)[1].split("## 期間内の開示", 1)[0]
    assert "PER" not in financial_section
    # 保存している指標（売上高）は出ていること
    assert "売上高（収益）" in financial_section


def test_financials_change_kind_formatting(tmp_path):
    """`change_kind` が pt の指標は「pt」、pct の指標は「%」で前期比が表記される。"""
    db = _db(tmp_path)
    _register(db, days=150)
    _save_full_financials(db)

    data = PromptSource(db).load(SYMBOL, 60)
    financial_section = build_prompt(data).split("## 財務指標", 1)[1].split("### 直近の中間期", 1)[0]

    import re

    assert re.search(r"前期比 [+-]\d+\.\d+pt", financial_section), financial_section
    assert re.search(r"前期比 [+-]\d+\.\d+%", financial_section), financial_section


def test_financials_interim_section_notes_six_months(tmp_path):
    """`interim` があるときだけ中間期の節が出て、6か月ぶんである旨が書かれていること。"""
    db = _db(tmp_path)
    _register(db, days=150)
    _save_full_financials(db)

    data = PromptSource(db).load(SYMBOL, 60)
    assert data.financials["interim"] is not None

    prompt = build_prompt(data)
    assert "### 直近の中間期" in prompt
    assert "6か月" in prompt
    assert "前年同期" in prompt


def test_financials_interim_section_absent_without_interim_data(tmp_path):
    db = _db(tmp_path)
    _register(db, days=150)
    # 通期のみ保存し、中間期は保存しない
    for i, period_end in enumerate(FIN_PERIODS):
        save_financials(
            db, SYMBOL,
            {"doc_id": f"DOC_FY_ONLY_{i}", "submit_at": f"{period_end[:4]}-06-25 09:00:00", "period_end": period_end, "standard": "ifrs"},
            [_fin_row(period_end, "revenue", 1_000_000_000_000 + i * 1_000_000_000)],
        )

    data = PromptSource(db).load(SYMBOL, 60)
    assert data.financials["interim"] is None
    assert "### 直近の中間期" not in build_prompt(data)


def test_financials_required_notes_always_present(tmp_path):
    """期ずれ・四半期報告書の廃止・会社予想対象外の3つの注記が必ず入っている。"""
    db = _db(tmp_path)
    _register(db, days=150)
    _save_full_financials(db)

    prompt = build_prompt(PromptSource(db).load(SYMBOL, 60))
    assert "1.5〜2.5か月" in prompt
    assert "四半期報告書は2024年に廃止" in prompt
    assert "会社予想との比較は対象外" in prompt


# ---------------------------------------------------------------------------
# テクニカル指標の圧縮（P11-5。SPEC §2.9.8）
# ---------------------------------------------------------------------------
def test_compression_keeps_only_recent_dates_raw(tmp_path):
    """120日で、直近20日ぶんの日付だけが生CSVに現れ、21日以上前の日付は現れない。"""
    db = _db(tmp_path)
    _register(db, days=150)

    all_dates = db.get_prices(SYMBOL)["date"].tolist()
    data = PromptSource(db).load(SYMBOL, 120)
    price_rows = _csv_rows(data.price_csv)

    assert len(price_rows) == RAW_WINDOW_DAYS
    raw_dates = {r["date"] for r in price_rows}
    assert raw_dates == set(all_dates[-RAW_WINDOW_DAYS:])
    # 21日以上前（末尾から21番目より前）の日付は生CSVに出ない
    assert all_dates[-(RAW_WINDOW_DAYS + 1)] not in raw_dates


def test_compressed_summary_has_high_low_and_signals(tmp_path):
    """圧縮区間の要約に、その区間の高値・安値・シグナルが出ている。"""
    db = _db(tmp_path)
    db.upsert_stock(SYMBOL, CODE, "トヨタ自動車", "東証プライム", "JPY")
    # 前半90日はフラット、後半60日で一直線に上昇させ、圧縮区間内（末尾21〜100日目あたり）に
    # 明確なゴールデンクロス（シグナル）を起こす
    closes = np.concatenate([np.full(90, 100.0), np.linspace(100.0, 200.0, 60)])
    db.upsert_prices(SYMBOL, make_prices(closes))

    data = PromptSource(db).load(SYMBOL, 120)
    assert data.compressed is not None
    assert len(data.compressed.signals) > 0
    assert data.compressed.high > data.compressed.low

    prompt = build_prompt(data)
    price_section = prompt.split("## 株価", 1)[1].split("## テクニカル指標", 1)[0]
    assert f"- 高値: {data.compressed.high}" in price_section
    assert f"- 安値: {data.compressed.low}" in price_section
    first_signal = data.compressed.signals[0]
    assert first_signal["label"] in price_section


def test_no_compression_section_when_days_equals_raw_window(tmp_path):
    """`days == 20` のときは圧縮する区間が無いので、要約の節が出ない。"""
    db = _db(tmp_path)
    _register(db, days=150)

    data = PromptSource(db).load(SYMBOL, RAW_WINDOW_DAYS)
    assert data.compressed is None
    assert "の要約" not in build_prompt(data)


# ---------------------------------------------------------------------------
# 圧縮前後の文字数比較（P11-5 やること3）
# ---------------------------------------------------------------------------
def _naive_full_csv_prompt(data: PromptInput, full_price_csv: str, full_indicator_csv: str) -> str:
    """P11-5 以前（圧縮なし・財務指標なし）の素朴な組み立てを模した比較用ヘルパー。テスト専用。

    `app.ai.prompt` の非公開ヘルパー（`_INSTRUCTIONS` 等）をそのまま使い、CSV ブロックだけを
    `days` 日ぶん全部の生データに差し替える。
    """
    sections = [
        prompt_mod._INSTRUCTIONS,
        "",
        "## 銘柄情報",
        f"- 証券コード: {data.symbol}",
        f"- 銘柄名: {data.name}",
        f"- 市場: {data.exchange}",
        f"- 通貨: {data.currency}",
        f"- 対象期間: 直近{data.days}日",
        f"- レポート生成日時: {data.generated_at}",
        "",
        "## 前提",
        f"- {prompt_mod._DISCLOSURE_NOTE}",
        "",
        f"## 株価（直近{data.days}日、CSV、date昇順）",
        "```csv",
        full_price_csv.rstrip("\n"),
        "```",
        "",
        f"## テクニカル指標（直近{data.days}日、CSV、date昇順）",
        "```csv",
        full_indicator_csv.rstrip("\n"),
        "```",
        "",
        "## 指標の定義（列名の読み方）",
        prompt_mod._format_definitions(),
        "",
        "## 最新日の指標判定",
        prompt_mod._format_latest(data.latest),
        "",
        "## 期間内のシグナル",
        prompt_mod._format_signals(data.signals),
        "",
        "## 期間内の開示（EDINET の法定開示のみ。本文は含まない）",
        prompt_mod._format_disclosures(data.disclosures),
    ]
    return "\n".join(sections)


def test_build_prompt_is_shorter_than_naive_full_csv_for_120_days(tmp_path):
    db = _db(tmp_path)
    _register(db, days=150)
    _save_full_financials(db)  # 財務指標を足したうえでも短くなることを確認する

    data = PromptSource(db).load(SYMBOL, 120)

    full_table = ai_export.load(db, [SYMBOL], 120)[0].table
    full_price_csv = full_table[["date", "open", "high", "low", "close", "volume"]].to_csv(
        index=False, lineterminator="\n"
    )
    indicator_cols = ["date"] + [
        c for c in full_table.columns if c not in ("date", "open", "high", "low", "close", "volume")
    ]
    full_indicator_csv = full_table[indicator_cols].to_csv(index=False, lineterminator="\n")

    naive = _naive_full_csv_prompt(data, full_price_csv, full_indicator_csv)
    actual = build_prompt(data)

    assert len(actual) < len(naive)


# ---------------------------------------------------------------------------
# 需給の番兵値（P11-5 での拡張。財務指標・圧縮を足しても現れないことを確かめる）
# ---------------------------------------------------------------------------
def test_sentinel_values_never_appear_with_financials_and_compression(tmp_path):
    db = _db(tmp_path)
    _register(db, days=150)
    _insert_sentinel_supply_data(db)
    _save_full_financials(db)

    data = PromptSource(db).load(SYMBOL, 120)
    prompt = build_prompt(data)

    _assert_no_sentinel(prompt)
    _assert_no_sentinel(repr(data))
    _assert_no_sentinel(repr(data.financials))
    _assert_no_sentinel(repr(data.compressed))
