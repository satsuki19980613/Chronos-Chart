"""空売り残高合計の算出（SPEC §2.2.3）。compute_totals の単体テスト。"""

from app.sources.karauri import compute_totals


def _row(calc_date, holder_id, holder, ratio, quantity, note=""):
    return {
        "calc_date": calc_date,
        "holder_id": holder_id,
        "holder": holder,
        "ratio": ratio,
        "ratio_delta": None,
        "quantity": quantity,
        "qty_delta": None,
        "note": note,
    }


def test_no_rows_gives_no_totals():
    assert compute_totals([]) == []


def test_single_lost_holder_gives_zero_totals_for_that_date():
    """該当者がいなければ 0 / 0 / 0（日付自体は残る）。"""
    rows = [_row("2026-02-01", "z", "Z", 0.1, 1000)]
    totals = compute_totals(rows)
    assert totals == [{"date": "2026-02-01", "total_ratio": 0.0, "total_qty": 0, "holders": 0}]


def test_multi_holder_scenario():
    """holder_id ごとの最新採用・消失の二重条件（note 由来／ratio<0.5 由来）・名称ゆれの非二重計上。"""
    rows = [
        _row("2026-01-01", "h2", "H2 Corp", 2.0, 200000),
        _row("2026-01-05", "h1", "H1 Fund", 1.0, 100000),
        _row("2026-01-10", "h1", "H1 Fund", 1.5, 150000),
        # note に「消失」を含む＝ratio が 0.5 以上でも除外（note 由来の消失）
        _row("2026-01-05", "h3", "H3 Ltd", 0.9, 90000, note="報告義務消失"),
        # ratio が 0.5 未満＝note が無くても除外（ratio 由来の消失）
        _row("2026-01-05", "h4", "H4 LLC", 0.3, 30000),
        # 同じ holder_id で表示名だけが変わる（名称ゆれ）。二重計上されないこと
        _row("2026-01-01", "h5", "Foo", 0.8, 80000),
        _row("2026-01-10", "h5", "Foo Corp", 0.85, 85000),
    ]
    totals = compute_totals(rows)

    assert [t["date"] for t in totals] == ["2026-01-01", "2026-01-05", "2026-01-10"]  # 昇順

    by_date = {t["date"]: t for t in totals}

    # 2026-01-01: h2, h5(Foo) のみ報告済み（h1/h3/h4 はまだ報告なし）
    d1 = by_date["2026-01-01"]
    assert d1["holders"] == 2
    assert d1["total_ratio"] == 2.0 + 0.8
    assert d1["total_qty"] == 200000 + 80000

    # 2026-01-05: h1(当日報告) + h2(直近の01-01報告を継続) + h5(直近の01-01報告を継続)。
    # h3 は消失注記、h4 は ratio<0.5 で除外
    d2 = by_date["2026-01-05"]
    assert d2["holders"] == 3
    assert d2["total_ratio"] == 1.0 + 2.0 + 0.8
    assert d2["total_qty"] == 100000 + 200000 + 80000

    # 2026-01-10: h1(当日報告) + h2(直近の01-01報告を継続) + h5(当日報告、名称は Foo Corp に変わったが
    # holder_id が同じなので二重計上されない)。h3/h4 は直近の消失/低比率のまま除外
    d3 = by_date["2026-01-10"]
    assert d3["holders"] == 3
    assert d3["total_ratio"] == 1.5 + 2.0 + 0.85
    assert d3["total_qty"] == 150000 + 200000 + 85000


def test_holder_with_no_report_yet_is_not_counted():
    """報告日がまだ来ていない holder_id はその日の合計に含めない。"""
    rows = [
        _row("2026-03-01", "a", "A", 1.0, 1000),
        _row("2026-03-10", "b", "B", 2.0, 2000),
    ]
    totals = compute_totals(rows)
    first = next(t for t in totals if t["date"] == "2026-03-01")
    assert first["holders"] == 1
    assert first["total_ratio"] == 1.0


def test_ratio_none_is_not_treated_as_lost():
    """ratio が None の場合は 0.5 未満と比較できないので、note に基づく判定のみで消失を決める。"""
    rows = [_row("2026-04-01", "n", "N", None, None)]
    totals = compute_totals(rows)
    assert totals == [{"date": "2026-04-01", "total_ratio": 0.0, "total_qty": 0, "holders": 1}]
