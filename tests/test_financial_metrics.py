import pytest

from app import financial_metrics as fm


def _period(period_end, **items):
    return {"period_end": period_end, "items": items}


def _series(periods, basis="consolidated", standard="jgaap"):
    return {
        "basis": basis,
        "standard": standard,
        "periods": periods,
        "source_docs": [{"doc_id": "S100TEST", "submit_at": "2025-06-20 09:00"}],
    }


def _metric(result, key):
    matches = [m for m in result["metrics"] if m["key"] == key]
    assert matches, f"{key} が metrics に無い"
    return matches[0]


# ---------------------------------------------------------------------------
# 1. 5期そろった日本基準の系列で、手計算どおりになること
# ---------------------------------------------------------------------------
JGAAP_PERIODS = [
    _period(
        "2021-03-31", revenue=1000.0, net_income=100.0, eps=10.0, total_assets=2000.0,
        equity=800.0, operating_cf=150.0, investing_cf=-50.0, roe=12.5, equity_ratio=40.0,
        per=15.0, payout_ratio=30.0,
    ),
    _period(
        "2022-03-31", revenue=1100.0, net_income=120.0, eps=12.0, total_assets=2200.0,
        equity=900.0, operating_cf=180.0, investing_cf=-60.0, roe=13.3, equity_ratio=41.0,
        per=14.0, payout_ratio=31.0,
    ),
    _period(
        "2023-03-31", revenue=1210.0, net_income=132.0, eps=13.2, total_assets=2400.0,
        equity=1000.0, operating_cf=200.0, investing_cf=-70.0, roe=13.2, equity_ratio=42.0,
        per=16.0, payout_ratio=29.0,
    ),
    _period(
        "2024-03-31", revenue=1331.0, net_income=145.2, eps=14.52, total_assets=2600.0,
        equity=1100.0, operating_cf=220.0, investing_cf=-80.0, roe=13.2, equity_ratio=42.3,
        per=17.0, payout_ratio=32.0,
    ),
    _period(
        "2025-03-31", revenue=1464.1, net_income=159.72, eps=15.972, total_assets=2800.0,
        equity=1200.0, operating_cf=240.0, investing_cf=-90.0, roe=13.3, equity_ratio=42.9,
        per=18.0, payout_ratio=33.0,
    ),
]


def test_full_5period_series_matches_hand_calculation():
    result = fm.compute_metrics(_series(JGAAP_PERIODS))

    assert result["available"] is True
    assert result["basis"] == "consolidated"
    assert result["standard"] == "jgaap"
    assert result["period_count"] == 5
    assert result["earliest_period_end"] == "2021-03-31"
    assert result["latest_period_end"] == "2025-03-31"

    # 売上高成長率: 前期比 (1464.1 - 1331.0) / 1331.0 * 100 = 10.0%
    rev_growth = _metric(result, "revenue_growth")
    assert rev_growth["value"] == pytest.approx(10.0, abs=1e-6)
    # CAGR: (1464.1 / 1000.0) ** (1/4) - 1 の百分率
    revenue = _metric(result, "revenue")
    assert revenue["cagr_pct"] == pytest.approx(((1464.1 / 1000.0) ** 0.25 - 1) * 100, abs=1e-6)
    assert revenue["change_pct"] == pytest.approx(10.0, abs=1e-6)

    # 純利益成長率
    ni_growth = _metric(result, "net_income_growth")
    assert ni_growth["value"] == pytest.approx((159.72 - 145.2) / 145.2 * 100, abs=1e-6)

    # EPS成長率
    eps_growth = _metric(result, "eps_growth")
    assert eps_growth["value"] == pytest.approx((15.972 - 14.52) / 14.52 * 100, abs=1e-6)

    # 営業CFマージン = 営業CF / 売上高 * 100（直近期）
    ocf_margin = _metric(result, "operating_cf_margin")
    assert ocf_margin["value"] == pytest.approx(240.0 / 1464.1 * 100, abs=1e-6)

    # フリーキャッシュフロー = 営業CF + 投資CF（符号のまま）
    fcf = _metric(result, "free_cash_flow")
    assert fcf["value"] == pytest.approx(240.0 + (-90.0), abs=1e-6)
    assert fcf["history"][0] == pytest.approx(150.0 + (-50.0), abs=1e-6)

    # アクルーアル = (純利益 - 営業CF) / 総資産 * 100
    accruals = _metric(result, "accruals")
    assert accruals["value"] == pytest.approx((159.72 - 240.0) / 2800.0 * 100, abs=1e-6)

    # 売上高（JPY）はこれまでどおり変化率(%)で change_kind == "pct"
    assert revenue["change_kind"] == "pct"


# ---------------------------------------------------------------------------
# 1b. `%` 単位の指標の change_pct は「変化率」ではなく「ポイント差」で出す
#     （ROE 2%→8% を change_pct: 300.0 のような誤解を招く値にしないため）
# ---------------------------------------------------------------------------
def test_percent_unit_metric_uses_point_diff_for_change_pct():
    periods = [
        _period("2023-03-31", roe=2.0),
        _period("2024-03-31", roe=8.0),
    ]
    result = fm.compute_metrics(_series(periods))
    roe = _metric(result, "roe")
    assert roe["unit"] == "%"
    assert roe["change_kind"] == "pt"
    # 変化率(300%)ではなく、ポイント差(+6.0pt)であること
    assert roe["change_pct"] == pytest.approx(8.0 - 2.0)


def test_jpy_unit_metric_still_uses_pct_change():
    result = fm.compute_metrics(_series(JGAAP_PERIODS))
    revenue = _metric(result, "revenue")
    assert revenue["change_kind"] == "pct"
    assert revenue["change_pct"] == pytest.approx((1464.1 - 1331.0) / 1331.0 * 100)


def test_percent_unit_point_diff_is_computed_across_sign_crossing():
    # 赤字→黒字の転換でも、ポイント差は意味のある値になるので None にしない
    periods = [
        _period("2023-03-31", roe=-4.0),
        _period("2024-03-31", roe=3.0),
    ]
    result = fm.compute_metrics(_series(periods))
    roe = _metric(result, "roe")
    assert roe["change_pct"] == pytest.approx(3.0 - (-4.0))
    assert roe["change_kind"] == "pt"


# ---------------------------------------------------------------------------
# 1c. `%` と `times` の指標は CAGR に意味が無いので常に None（注記も出さない）
# ---------------------------------------------------------------------------
def test_percent_and_times_metrics_never_have_cagr():
    result = fm.compute_metrics(_series(JGAAP_PERIODS))
    for key in ("roe", "equity_ratio", "payout_ratio", "per", "revenue_growth", "accruals"):
        metric = _metric(result, key)
        assert metric["cagr_pct"] is None
    # 「算出していません」のような注記も出さない（欠損起因ではないため）
    assert not any(
        "CAGR" in note and ("ROE" in note or "PER" in note or "配当性向" in note or "自己資本比率" in note)
        for note in result["notes"]
    )


def test_percent_metric_cagr_is_none_even_without_sign_issues():
    periods = [
        _period("2021-03-31", roe=5.0),
        _period("2022-03-31", roe=6.0),
        _period("2023-03-31", roe=7.0),
    ]
    result = fm.compute_metrics(_series(periods))
    roe = _metric(result, "roe")
    assert roe["cagr_pct"] is None
    assert result["notes"] == []


# ---------------------------------------------------------------------------
# 2. 開示値（ROE・自己資本比率・PER・配当性向）はそのまま出て、再計算されないこと
# ---------------------------------------------------------------------------
def test_disclosed_ratios_are_passed_through_unchanged():
    result = fm.compute_metrics(_series(JGAAP_PERIODS))
    for key, expected_latest in (
        ("roe", 13.3), ("equity_ratio", 42.9), ("per", 18.0), ("payout_ratio", 33.0),
    ):
        metric = _metric(result, key)
        assert metric["source"] == "disclosed"
        assert metric["value"] == pytest.approx(expected_latest)
        assert metric["cagr_pct"] is None  # roe/equity_ratio/payout_ratio は「%」、per は「times」— CAGR は常に None
        # 発行体の開示値の並びがそのまま history に入っている（アプリでの再計算を挟まない）
        assert metric["history"] == [p["items"][key] for p in JGAAP_PERIODS]

    # 素の値も再計算されていない（開示値そのもの）
    for key in ("revenue", "net_income", "eps", "total_assets", "equity", "operating_cf"):
        metric = _metric(result, key)
        assert metric["source"] == "disclosed"
        assert metric["value"] == pytest.approx(JGAAP_PERIODS[-1]["items"][key])


# ---------------------------------------------------------------------------
# 3. 期間が少ない・歯抜けの系列で落ちないこと
# ---------------------------------------------------------------------------
def test_single_period_does_not_crash():
    result = fm.compute_metrics(_series([JGAAP_PERIODS[0]]))
    assert result["available"] is True
    assert result["period_count"] == 1
    for metric in result["metrics"]:
        assert metric["change_pct"] is None
        assert metric["cagr_pct"] is None
        assert metric["trend"] is None
        assert metric["percentile"] is None


def test_two_period_series_does_not_crash():
    result = fm.compute_metrics(_series(JGAAP_PERIODS[:2]))
    assert result["available"] is True
    assert result["period_count"] == 2
    revenue = _metric(result, "revenue")
    assert revenue["change_pct"] == pytest.approx((1100.0 - 1000.0) / 1000.0 * 100)
    # 2期しかないので CAGR は「期数差1」の年率換算と一致する
    assert revenue["cagr_pct"] == pytest.approx(revenue["change_pct"])


def test_sparse_series_does_not_crash():
    sparse_periods = [
        _period("2021-03-31", revenue=1000.0),
        _period("2022-03-31", net_income=50.0),
        _period("2023-03-31", revenue=1200.0, operating_cf=100.0),
    ]
    result = fm.compute_metrics(_series(sparse_periods))
    assert result["available"] is True
    revenue = _metric(result, "revenue")
    assert revenue["history"] == [1000.0, None, 1200.0]
    assert revenue["value"] == pytest.approx(1200.0)
    # 直前期が欠損なので前期比は算出しない（例外にはしない）
    assert revenue["change_pct"] is None
    net_income = _metric(result, "net_income")
    assert net_income["value"] is None  # 最新期に無いので値なし
    accruals = _metric(result, "accruals")
    assert accruals["history"] == [None, None, None]  # total_assets が1度も無い


# ---------------------------------------------------------------------------
# 4. 赤字→黒字（符号がまたぐ）で change_pct / cagr_pct が None になり、notes に説明が入ること
# ---------------------------------------------------------------------------
def test_sign_crossing_blocks_growth_and_adds_note():
    periods = [
        _period("2023-03-31", net_income=-100.0, revenue=1000.0),
        _period("2024-03-31", net_income=-40.0, revenue=1100.0),
        _period("2025-03-31", net_income=50.0, revenue=1200.0),
    ]
    result = fm.compute_metrics(_series(periods))
    net_income = _metric(result, "net_income")
    assert net_income["change_pct"] is None
    assert net_income["cagr_pct"] is None
    assert any("当期純利益" in note and "赤字" in note for note in result["notes"])

    net_income_growth = _metric(result, "net_income_growth")
    # revenue_growth 用の history[i] は pct_change を使うので、赤字→黒字の期は None になる
    assert net_income_growth["history"][-1] is None


def test_zero_base_blocks_growth_and_adds_note():
    periods = [
        _period("2023-03-31", net_income=0.0),
        _period("2024-03-31", net_income=50.0),
    ]
    result = fm.compute_metrics(_series(periods))
    net_income = _metric(result, "net_income")
    assert net_income["change_pct"] is None
    assert any("当期純利益" in note and "ゼロ" in note for note in result["notes"])


# ---------------------------------------------------------------------------
# 5. series=None / periods=[] で available: False
# ---------------------------------------------------------------------------
def test_none_series_returns_unavailable():
    result = fm.compute_metrics(None)
    assert result == {
        "available": False, "basis": None, "standard": None, "period_count": 0,
        "latest_period_end": None, "earliest_period_end": None, "metrics": [], "interim": None,
        "notes": [],
    }


def test_empty_periods_returns_unavailable():
    result = fm.compute_metrics(_series([]))
    assert result["available"] is False
    assert result["metrics"] == []


# ---------------------------------------------------------------------------
# 6. IFRS の系列（ordinary_income が無い）で落ちないこと
# ---------------------------------------------------------------------------
def test_ifrs_series_without_ordinary_income_does_not_crash():
    ifrs_periods = [
        _period(
            "2024-03-31", revenue=5000.0, net_income=400.0, eps=40.0, total_assets=9000.0,
            equity=3000.0, operating_cf=600.0, investing_cf=-200.0, roe=13.3, equity_ratio=33.3,
            per=20.0, payout_ratio=25.0, pretax_income=550.0,
        ),
        _period(
            "2025-03-31", revenue=5500.0, net_income=450.0, eps=45.0, total_assets=9500.0,
            equity=3300.0, operating_cf=650.0, investing_cf=-220.0, roe=13.6, equity_ratio=34.7,
            per=19.0, payout_ratio=26.0, pretax_income=600.0,
        ),
    ]
    result = fm.compute_metrics(_series(ifrs_periods, standard="ifrs"))
    assert result["available"] is True
    assert result["standard"] == "ifrs"
    # ordinary_income は日本基準専用の項目なので、出力の指標一覧に含まれない
    assert "ordinary_income" not in {m["key"] for m in result["metrics"]}
    revenue_growth = _metric(result, "revenue_growth")
    assert revenue_growth["value"] == pytest.approx((5500.0 - 5000.0) / 5000.0 * 100)


# ---------------------------------------------------------------------------
# 7. percentile が最小0・最大100になること
# ---------------------------------------------------------------------------
def test_percentile_hits_min_and_max():
    increasing = [
        _period("2021-03-31", revenue=100.0),
        _period("2022-03-31", revenue=200.0),
        _period("2023-03-31", revenue=300.0),
    ]
    result_up = fm.compute_metrics(_series(increasing))
    assert _metric(result_up, "revenue")["percentile"] == pytest.approx(100.0)

    decreasing = [
        _period("2021-03-31", revenue=300.0),
        _period("2022-03-31", revenue=200.0),
        _period("2023-03-31", revenue=100.0),
    ]
    result_down = fm.compute_metrics(_series(decreasing))
    assert _metric(result_down, "revenue")["percentile"] == pytest.approx(0.0)

    flat = [
        _period("2021-03-31", revenue=100.0),
        _period("2022-03-31", revenue=100.0),
    ]
    result_flat = fm.compute_metrics(_series(flat))
    assert _metric(result_flat, "revenue")["percentile"] == pytest.approx(50.0)

    single = [_period("2021-03-31", revenue=100.0)]
    result_single = fm.compute_metrics(_series(single))
    assert _metric(result_single, "revenue")["percentile"] is None


# ---------------------------------------------------------------------------
# 8. トレンド判定の境界値（ちょうど閾値を含む）
# ---------------------------------------------------------------------------
def test_compute_trend_pct_boundaries():
    # ちょうど +5% は「横ばい」（超えていないので改善ではない）
    assert fm.compute_trend([100.0, 105.0], "pct") == "横ばい"
    # +5%超で「改善」
    assert fm.compute_trend([100.0, 105.01], "pct") == "改善"
    # ちょうど -5% は「横ばい」
    assert fm.compute_trend([100.0, 95.0], "pct") == "横ばい"
    # -5%未満で「悪化」
    assert fm.compute_trend([100.0, 94.99], "pct") == "悪化"


def test_compute_trend_pt_boundaries():
    # % 単位の指標はポイント差で判定。ちょうど 0.5pt は「横ばい」
    assert fm.compute_trend([10.0, 10.5], "pt") == "横ばい"
    assert fm.compute_trend([10.0, 10.51], "pt") == "改善"
    assert fm.compute_trend([10.0, 9.5], "pt") == "横ばい"
    assert fm.compute_trend([10.0, 9.49], "pt") == "悪化"


def test_compute_trend_uses_recent_three_periods_when_available():
    # 直近3期: 100 -> 110（+10%） -> 121（+10%）。平均+10% > 5% なので改善
    assert fm.compute_trend([100.0, 110.0, 121.0], "pct") == "改善"
    # 直近3期の1本目が欠損なら、算出できる方（2本目->3本目）だけで判定する
    assert fm.compute_trend([None, 100.0, 110.0], "pct") == "改善"


def test_compute_trend_none_cases():
    assert fm.compute_trend([], "pct") is None
    assert fm.compute_trend([100.0], "pct") is None
    assert fm.compute_trend([100.0, 110.0], None) is None  # mode=None の指標は常に None


def test_compute_trend_reversed_for_accruals_like_metric():
    # 通常なら値が増えれば「改善」だが、reversed_=True なら符号を反転して判定する
    assert fm.compute_trend([1.0, 2.0], "pt", reversed_=True) == "悪化"
    assert fm.compute_trend([2.0, 1.0], "pt", reversed_=True) == "改善"


def test_no_trend_metrics_are_always_none():
    result = fm.compute_metrics(_series(JGAAP_PERIODS))
    for key in ("per", "payout_ratio", "total_assets", "equity"):
        assert _metric(result, key)["trend"] is None
    assert "per" not in fm.TREND_JUDGED_KEYS
    assert "payout_ratio" not in fm.TREND_JUDGED_KEYS
    assert "revenue" in fm.TREND_JUDGED_KEYS


# ---------------------------------------------------------------------------
# 補足: 非連結フォールバックの注記
# ---------------------------------------------------------------------------
def test_nonconsolidated_basis_adds_note():
    result = fm.compute_metrics(_series(JGAAP_PERIODS[:2], basis="nonconsolidated"))
    assert result["basis"] == "nonconsolidated"
    assert any("単体" in note for note in result["notes"])


# ---------------------------------------------------------------------------
# 9. 純資産（net_assets）: 日本基準の会社には equity が無く、代わりに net_assets が載る
# ---------------------------------------------------------------------------
def test_net_assets_appears_between_total_assets_and_equity():
    result = fm.compute_metrics(_series(JGAAP_PERIODS))
    keys = [m["key"] for m in result["metrics"]]
    assert "net_assets" in keys
    assert keys.index("total_assets") < keys.index("net_assets") < keys.index("equity")
    assert fm.METRIC_ORDER.index("total_assets") < fm.METRIC_ORDER.index("net_assets") < fm.METRIC_ORDER.index("equity")


def test_net_assets_does_not_crash_when_equity_is_missing_everywhere():
    # 日本基準の「主要な経営指標等の推移」を模した系列: 自己資本(equity)は存在せず、純資産のみ載る
    jgaap_no_equity = [
        _period("2024-03-31", revenue=800.0, net_income=60.0, total_assets=1500.0, net_assets=700.0),
        _period("2025-03-31", revenue=850.0, net_income=65.0, total_assets=1600.0, net_assets=750.0),
    ]
    result = fm.compute_metrics(_series(jgaap_no_equity))
    assert result["available"] is True

    net_assets = _metric(result, "net_assets")
    assert net_assets["source"] == "disclosed"
    assert net_assets["value"] == pytest.approx(750.0)
    assert net_assets["history"] == [700.0, 750.0]
    # net_assets と equity を混同・合算していない
    assert net_assets["unit"] == "JPY"
    assert net_assets["trend"] is None

    equity = _metric(result, "equity")
    assert equity["value"] is None
    assert equity["history"] == [None, None]


# ---------------------------------------------------------------------------
# 10. 中間期（半期報告書）の実績
#
# `app/financials.py` の `load_series()` が返す入力側の形（"prior" キーは前年同期が無くても
# 必ず存在し、その場合は None）:
#   {"period_end": str, "items": {...}, "prior": {"period_end": str, "items": {...}} | None}
# ---------------------------------------------------------------------------
def _interim(period_end, items, prior=None):
    return {"period_end": period_end, "items": items, "prior": prior}


def _prior(period_end, items):
    return {"period_end": period_end, "items": items}


def test_interim_change_pct_is_year_over_year():
    interim = _interim(
        "2026-07-31",
        {"revenue": 16192722000.0, "roe": 8.0},
        prior=_prior("2025-07-31", {"revenue": 14421000000.0, "roe": 6.0}),
    )
    result = fm.compute_metrics(_series([], basis="consolidated", standard="jgaap") | {"interim": interim})
    assert result["available"] is True
    assert result["interim"]["period_end"] == "2026-07-31"
    assert result["interim"]["prior_period_end"] == "2025-07-31"

    items = {item["key"]: item for item in result["interim"]["items"]}
    revenue = items["revenue"]
    assert revenue["change_kind"] == "pct"
    assert revenue["change_pct"] == pytest.approx(
        (16192722000.0 - 14421000000.0) / 14421000000.0 * 100
    )
    roe = items["roe"]
    assert roe["change_kind"] == "pt"
    assert roe["change_pct"] == pytest.approx(8.0 - 6.0)  # ポイント差。変化率(33%)ではない

    assert any(fm.INTERIM_NOTE in note for note in result["notes"])


def test_interim_without_prior_has_none_change_and_none_prior_period_end():
    # prior が None（前年同期の開示がまだ無い、等）でも落ちない
    interim = _interim("2026-07-31", {"revenue": 1000.0}, prior=None)
    result = fm.compute_metrics(_series(JGAAP_PERIODS) | {"interim": interim})
    assert result["interim"]["prior_period_end"] is None
    revenue = next(item for item in result["interim"]["items"] if item["key"] == "revenue")
    assert revenue["change_pct"] is None


def test_interim_prior_present_but_items_empty_does_not_crash():
    # prior は dict として存在するが items が空（未取得等）でも落ちない
    interim = _interim("2026-07-31", {"revenue": 1000.0}, prior=_prior("2025-07-31", {}))
    result = fm.compute_metrics(_series(JGAAP_PERIODS) | {"interim": interim})
    assert result["interim"]["prior_period_end"] == "2025-07-31"
    revenue = next(item for item in result["interim"]["items"] if item["key"] == "revenue")
    assert revenue["change_pct"] is None  # prior の値自体が無いので算出しない


def test_interim_prior_with_only_some_items_present():
    # prior に一部の項目しか無い（例: revenue はあるが roe は無い）
    interim = _interim(
        "2026-07-31",
        {"revenue": 1000.0, "roe": 9.0},
        prior=_prior("2025-07-31", {"revenue": 900.0}),  # roe が無い
    )
    result = fm.compute_metrics(_series(JGAAP_PERIODS) | {"interim": interim})
    items = {item["key"]: item for item in result["interim"]["items"]}
    assert items["revenue"]["change_pct"] == pytest.approx((1000.0 - 900.0) / 900.0 * 100)
    assert items["roe"]["change_pct"] is None  # 前年同期に roe が無いので算出しない
    assert items["roe"]["change_kind"] == "pt"  # kind の決め方自体は unit で決まる（欠損とは独立）


def test_interim_omits_items_without_a_value():
    interim = _interim("2026-07-31", {"revenue": 1000.0, "net_income": None})
    result = fm.compute_metrics(_series(JGAAP_PERIODS) | {"interim": interim})
    keys = [item["key"] for item in result["interim"]["items"]]
    assert keys == ["revenue"]
    assert "net_income" not in keys


def test_interim_only_series_is_available_with_no_full_year_metrics():
    # 半期報告書しか無い銘柄（実データのモロゾフを想定）: periods が空でも interim があれば使える
    interim = _interim("2026-07-31", {"revenue": 1000.0, "net_income": 30.0})
    result = fm.compute_metrics({
        "basis": "consolidated", "standard": "jgaap", "periods": [], "interim": interim,
    })
    assert result["available"] is True
    assert result["metrics"] == []
    assert result["period_count"] == 0
    assert result["latest_period_end"] is None
    assert result["earliest_period_end"] is None
    assert result["interim"] is not None
    assert [item["key"] for item in result["interim"]["items"]] == ["revenue", "net_income"]


def test_no_interim_note_when_interim_is_absent():
    result = fm.compute_metrics(_series(JGAAP_PERIODS))
    assert result["interim"] is None
    assert not any(fm.INTERIM_NOTE in note for note in result["notes"])
