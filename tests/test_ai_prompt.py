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

from app import disclosures
from app.ai.prompt import (
    PERIOD_CHOICES,
    DisclosureItem,
    PromptInput,
    PromptSource,
    build_prompt,
    build_retry_prompt,
)
from app.ai.schema import AnalysisReport, SectionAnalysis
from app.database import Database
from app.errors import UserFacingError

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


def test_csv_row_count_scales_with_days(tmp_path):
    db = _db(tmp_path)
    _register(db, days=150)
    counts = {d: len(_csv_rows(PromptSource(db).load(SYMBOL, d).price_csv)) for d in PERIOD_CHOICES}
    assert counts[20] <= counts[60] <= counts[120]
    assert counts[20] == 20


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
    assert list(AnalysisReport.model_fields.keys()) == [
        "technical",
        "disclosure",
        "risks",
        "watch_points",
        "verdict",
        "confidence",
        "summary",
    ]


def test_analysis_report_list_limits():
    report = AnalysisReport(
        technical=SectionAnalysis(evidence=["a"], assessment="ok"),
        disclosure=SectionAnalysis(evidence=["b"], assessment="ok"),
        risks=["r"],
        watch_points=["w"],
        verdict="neutral",
        confidence="low",
        summary="summary",
    )
    assert report.verdict == "neutral"
    with pytest.raises(Exception):
        SectionAnalysis(evidence=["1", "2", "3", "4", "5", "6", "7"], assessment="too many")
