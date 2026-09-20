"""app/events.py（P5-1: disclosures → チャートイベント変換）のテスト。

`app.events` は DB に触らない純粋な変換モジュールなので、大半のテストは
`disclosures.list_for_symbol()` が返す形の dict をそのまま組み立てて渡す（DB なし）。
最後に `StockService.dashboard()` の戻り値に `events` が入ることだけ、既存の流儀で DB を組み立てて確認する。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from conftest import make_prices

from app import disclosures, events
from app.database import Database
from app.fetcher import FetchError, SearchResult
from app.service import StockService


def _item(
    doc_id: str,
    submit_at: str,
    category: str = "report",
    doc_type_code: str | None = "120",
    withdrawal: int | None = None,
    roles: list[str] | None = None,
    description: str | None = None,
    filer_name: str | None = None,
    reason: str | None = None,
) -> dict:
    """`disclosures.list_for_symbol()` が返す1件分の item と同じ形を組み立てる（テスト用）。"""
    return {
        "doc_id": doc_id,
        "submit_at": submit_at,
        "category": category,
        "doc_type_code": doc_type_code,
        "description": description,
        "reason": reason,
        "filer_name": filer_name,
        "roles": roles if roles is not None else ["filer"],
        "withdrawal": withdrawal,
    }


def _list(items: list[dict], counts: dict | None = None, fetched_days: int = 1) -> dict:
    return {
        "items": items,
        "counts": counts or {"report": 0, "supply": 0, "other": 0, "withdrawn": 0, "total": len(items)},
        "fetched_days": fetched_days,
    }


# ---------------------------------------------------------------------------
# label_for
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "doc_type_code, expected",
    [
        ("120", "有報"),
        ("130", "有報"),
        ("140", "四半期"),
        ("150", "四半期"),
        ("160", "半期"),
        ("170", "半期"),
        ("350", "大量保有"),
        ("360", "大量保有"),
        ("220", "自己株"),
        ("230", "自己株"),
        ("240", "TOB"),  # 公開買付関連の下限
        ("280", "TOB"),
        ("320", "TOB"),  # 公開買付関連の上限
        ("239", "その他"),  # 240未満は範囲外
        ("321", "その他"),  # 320超は範囲外
        ("180", "臨報"),
        ("190", "臨報"),
        ("030", "届出"),
        ("040", "届出"),
        ("235", "内部統制"),
        ("236", "内部統制"),
        ("999", "その他"),  # 未知のコード
        (None, "その他"),
        ("", "その他"),
        ("abc", "その他"),
    ],
)
def test_label_for_table(doc_type_code, expected):
    assert events.label_for(doc_type_code) == expected


def test_label_for_235_is_internal_control_not_tender_offer():
    """完全一致の表を先に引くことの固定テスト（235は240〜320の範囲外だが、表引きの順序を明示する）。"""
    assert events.label_for("235") == "内部統制"
    assert events.label_for("236") == "内部統制"


# ---------------------------------------------------------------------------
# marker_date
# ---------------------------------------------------------------------------
# 2026-09-14(月)〜18(金)、週末を挟んで21(月) という並び（土日は株価が無い）
_DATES = ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21"]


def test_marker_date_snaps_to_exact_day_with_time():
    assert events.marker_date(_DATES, "2026-09-16 15:00") == "2026-09-16"


def test_marker_date_snaps_to_exact_day_without_time():
    assert events.marker_date(_DATES, "2026-09-16") == "2026-09-16"


def test_marker_date_snaps_to_next_business_day_when_no_candle_same_day():
    # 平日でも株価データがまだ無い日（データの穴）は次にある足へ寄る
    dates = ["2026-09-14", "2026-09-17"]  # 15, 16 が欠けている
    assert events.marker_date(dates, "2026-09-15 09:00") == "2026-09-17"


def test_marker_date_weekend_submission_snaps_to_monday():
    assert events.marker_date(_DATES, "2026-09-19 10:00") == "2026-09-21"  # 土曜提出 → 月曜
    assert events.marker_date(_DATES, "2026-09-20 10:00") == "2026-09-21"  # 日曜提出 → 月曜


def test_marker_date_after_last_candle_returns_none():
    assert events.marker_date(_DATES, "2026-09-22 09:00") is None


def test_marker_date_before_first_candle_returns_none():
    """先頭の足に寄せない（本タスクの決定。誤って最左のローソクに古い開示が付くのを防ぐ）。"""
    assert events.marker_date(_DATES, "2026-09-10 09:00") is None


@pytest.mark.parametrize("submit_at", ["", None, "not-a-date", "2026-13-40 10:00", "2026/09/16"])
def test_marker_date_empty_or_malformed_returns_none_without_raising(submit_at):
    assert events.marker_date(_DATES, submit_at) is None


def test_marker_date_empty_dates_returns_none():
    assert events.marker_date([], "2026-09-16 10:00") is None


# ---------------------------------------------------------------------------
# build: マーカーの同日まとめ
# ---------------------------------------------------------------------------
def test_build_single_item_marker_uses_label_as_text():
    dl = _list([_item("S1", "2026-09-16 09:00", category="report", doc_type_code="120")])
    result = events.build(dl, _DATES)

    assert result["markers"] == [
        {
            "id": "ev:2026-09-16",
            "date": "2026-09-16",
            "category": "report",
            "text": "有報",
            "count": 1,
            "doc_ids": ["S1"],
        }
    ]


def test_build_same_day_priority_supply_over_report_over_other():
    dl = _list(
        [
            _item("S1", "2026-09-16 09:00", category="report", doc_type_code="120"),
            _item("S2", "2026-09-16 10:00", category="other", doc_type_code="180"),
            _item("S3", "2026-09-16 11:00", category="supply", doc_type_code="350"),
        ]
    )
    result = events.build(dl, _DATES)

    assert len(result["markers"]) == 1
    marker = result["markers"][0]
    assert marker["category"] == "supply"  # supply > report > other
    assert marker["text"] == "開示3件"
    assert marker["count"] == 3
    assert marker["doc_ids"] == ["S1", "S2", "S3"]  # 昇順


def test_build_report_beats_other_when_no_supply():
    dl = _list(
        [
            _item("S2", "2026-09-16 09:00", category="other", doc_type_code="180"),
            _item("S1", "2026-09-16 10:00", category="report", doc_type_code="120"),
        ]
    )
    result = events.build(dl, _DATES)
    assert result["markers"][0]["category"] == "report"


def test_build_marker_id_format():
    dl = _list([_item("S1", "2026-09-14 09:00")])
    result = events.build(dl, _DATES)
    assert result["markers"][0]["id"] == "ev:2026-09-14"


# ---------------------------------------------------------------------------
# build: 取下げ
# ---------------------------------------------------------------------------
def test_build_withdrawal_excluded_from_marker_but_kept_in_items():
    dl = _list(
        [
            _item("S1", "2026-09-16 09:00", category="report", doc_type_code="120"),
            _item("S2", "2026-09-16 10:00", category="report", doc_type_code="120", withdrawal=1),
        ]
    )
    result = events.build(dl, _DATES)

    assert len(result["items"]) == 2
    assert {i["doc_id"] for i in result["items"]} == {"S1", "S2"}
    marker = result["markers"][0]
    assert marker["count"] == 1
    assert marker["doc_ids"] == ["S1"]  # S2（取下げ）は含まれない


def test_build_withdrawal_zero_is_not_treated_as_withdrawn():
    dl = _list([_item("S1", "2026-09-16 09:00", withdrawal=0)])
    result = events.build(dl, _DATES)
    assert result["markers"][0]["doc_ids"] == ["S1"]


def test_build_all_withdrawn_on_a_day_creates_no_marker():
    dl = _list(
        [
            _item("S1", "2026-09-16 09:00", withdrawal=1),
            _item("S2", "2026-09-16 10:00", withdrawal=2),
        ]
    )
    result = events.build(dl, _DATES)

    assert result["markers"] == []
    assert len(result["items"]) == 2  # items には残る


def test_build_marker_date_none_produces_no_marker_but_keeps_item():
    dl = _list([_item("S1", "2026-09-10 09:00")])  # 最初の足より前 → marker_date は None
    result = events.build(dl, _DATES)

    assert result["markers"] == []
    assert result["items"][0]["marker_date"] is None
    assert result["items"][0]["doc_id"] == "S1"


# ---------------------------------------------------------------------------
# build: 並び順・付随フィールド
# ---------------------------------------------------------------------------
def test_build_items_preserve_input_order():
    # list_for_symbol は submit_at 降順で返すので、build はその並びをそのまま保つ
    dl = _list(
        [
            _item("S3", "2026-09-18 09:00"),
            _item("S2", "2026-09-17 09:00"),
            _item("S1", "2026-09-16 09:00"),
        ]
    )
    result = events.build(dl, _DATES)
    assert [i["doc_id"] for i in result["items"]] == ["S3", "S2", "S1"]


def test_build_markers_sorted_by_date_ascending():
    dl = _list(
        [
            _item("S1", "2026-09-18 09:00"),
            _item("S2", "2026-09-14 09:00"),
            _item("S3", "2026-09-16 09:00"),
        ]
    )
    result = events.build(dl, _DATES)
    assert [m["date"] for m in result["markers"]] == ["2026-09-14", "2026-09-16", "2026-09-18"]


def test_build_item_includes_reason_field():
    """list_for_symbol が返す reason（提出事由）を items にそのまま通す（SPEC §2.6）。"""
    dl = _list([_item("S1", "2026-09-16 09:00")])
    result = events.build(dl, _DATES)
    assert result["items"][0]["reason"] is None


def test_build_item_passes_through_reason_value():
    """reason に値が入っている場合はそのまま通す（臨時報告書の提出事由）。"""
    dl = _list([_item("S1", "2026-09-16 09:00", reason="事業内容の変更")])
    result = events.build(dl, _DATES)
    assert result["items"][0]["reason"] == "事業内容の変更"


def test_build_item_shape_and_passthrough_fields():
    dl = _list(
        [
            _item(
                "S1",
                "2026-09-16 09:00",
                category="supply",
                doc_type_code="350",
                description="大量保有報告書",
                filer_name="テスト投資顧問",
                roles=["issuer"],
            )
        ]
    )
    result = events.build(dl, _DATES)
    item = result["items"][0]
    assert item == {
        "doc_id": "S1",
        "submit_at": "2026-09-16 09:00",
        "marker_date": "2026-09-16",
        "category": "supply",
        "label": "大量保有",
        "doc_type_code": "350",
        "description": "大量保有報告書",
        "reason": None,
        "filer_name": "テスト投資顧問",
        "roles": ["issuer"],
        "withdrawal": None,
    }


def test_build_passes_through_counts_and_fetched_days():
    counts = {"report": 1, "supply": 0, "other": 0, "withdrawn": 0, "total": 1}
    dl = _list([_item("S1", "2026-09-16 09:00")], counts=counts, fetched_days=42)
    result = events.build(dl, _DATES)
    assert result["counts"] == counts
    assert result["fetched_days"] == 42


# ---------------------------------------------------------------------------
# build: 空の入力
# ---------------------------------------------------------------------------
def test_build_empty_disclosure_list():
    dl = _list([])
    result = events.build(dl, _DATES)
    assert result["items"] == []
    assert result["markers"] == []


def test_build_empty_dates_produces_no_markers_but_keeps_items():
    dl = _list([_item("S1", "2026-09-16 09:00")])
    result = events.build(dl, dates=[])
    assert result["markers"] == []
    assert result["items"][0]["marker_date"] is None


def test_build_totally_empty_input():
    result = events.build({"items": []}, [])
    assert result == {
        "items": [],
        "markers": [],
        "counts": {"report": 0, "supply": 0, "other": 0, "withdrawn": 0, "total": 0},
        "fetched_days": 0,
    }


# ---------------------------------------------------------------------------
# StockService.dashboard() との統合
# ---------------------------------------------------------------------------
class FakeFetcher:
    """ネットワークを使わずに yfinance の代わりをする（tests/test_service_disclosures.py と同じ形）。"""

    def __init__(self, prices: pd.DataFrame):
        self.prices = prices

    def search(self, query):
        return [SearchResult("7203.T", "Toyota", "東証", "EQUITY")]

    def fetch_currency(self, symbol):
        return "JPY"

    def fetch_history(self, symbol, period=None, start=None):
        df = self.prices if start is None else self.prices[self.prices["date"] >= start]
        if df.empty:
            raise FetchError("no data")
        return df.reset_index(drop=True)


def _disclosure_doc(doc_id: str, submit_at: str, doc_type_code: str = "350") -> dict:
    """`disclosures.save_documents` に渡す1件分の dict（tests/test_api_disclosures.py の `_doc` を流用）。"""
    return {
        "doc_id": doc_id,
        "edinet_code": "E00001",
        "sec_code": "72030",
        "filer_name": "テスト株式会社",
        "issuer_edinet_code": None,
        "subject_edinet_code": None,
        "doc_type_code": doc_type_code,
        "form_code": None,
        "ordinance_code": None,
        "description": "大量保有報告書",
        "reason": None,
        "period_start": None,
        "period_end": None,
        "submit_at": submit_at,
        "parent_doc_id": None,
        "withdrawal": None,
        "disclosure": None,
        "category": disclosures.classify(doc_type_code),
    }


def test_dashboard_includes_events_with_markers_for_registered_symbol(tmp_path):
    db = Database(tmp_path / "test.db")
    db.init_schema()
    prices = make_prices(100 + np.sin(np.arange(30) / 4) * 5)
    fetcher = FakeFetcher(prices)
    service = StockService(db, fetcher, tmp_path / "csv", tmp_path / "output")

    service.register("7203.T", "Toyota")

    submit_date = prices["date"].iloc[5]  # ローソク足が確実にある日
    doc = _disclosure_doc("S1000001", f"{submit_date} 15:00")
    disclosures.save_documents(db, [(doc, [("7203.T", "filer")])])

    result = service.dashboard("7203.T")

    assert "events" in result
    assert "events" not in result["chart"]  # events は chart.short / chart.taisyaku と違うトップレベルのキー
    assert result["events"]["items"][0]["doc_id"] == "S1000001"
    assert result["events"]["markers"] == [
        {
            "id": f"ev:{submit_date}",
            "date": submit_date,
            "category": "supply",
            "text": "大量保有",
            "count": 1,
            "doc_ids": ["S1000001"],
        }
    ]


def test_dashboard_events_empty_when_no_disclosures(tmp_path):
    db = Database(tmp_path / "test.db")
    db.init_schema()
    prices = make_prices(100 + np.sin(np.arange(30) / 4) * 5)
    fetcher = FakeFetcher(prices)
    service = StockService(db, fetcher, tmp_path / "csv", tmp_path / "output")

    service.register("7203.T", "Toyota")

    result = service.dashboard("7203.T")

    assert result["events"]["items"] == []
    assert result["events"]["markers"] == []
