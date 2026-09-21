"""財務指標の算出（P11-4）。

`app/financials.py`（別作業者が実装）の `load_series(db, symbol)` が返す dict を受け取り、
AI プロンプト・レポート表示向けに「1指標1行」の形へ要約する純関数群。

**このモジュールは DB・外部サイトのいずれにも触れない。** 入力の dict だけを見て計算する。
生の財務諸表を丸ごと AI に渡さないための圧縮であり（SPEC §2.9.6・§2.9.7）、
計算はここで行い LLM にはやらせない（`docs/research/llm-prompting.md` §1: LLM は表の数値同士の
計算を苦手とする）。

**このモジュールは需給データ（空売り残高・貸借取引残高）を一切扱わない。**
不変条件1（プロンプトへの需給データ混入禁止）を守るため、需給関連の識別子（禁止語一覧は
依頼元の指示を参照）をこのファイルに書かないこと（機械的に検査される）。

合成スコア（Piotroski F-Score 等）や会社予想比の進捗率は作らない（SPEC §2.9.6・
`docs/research/financial-metrics.md` §2: 係数が米国データで推定されたもので日本基準での
再検証を確認できていないため）。同業他社比較もしない（他社データを持っていない）。
"""

from __future__ import annotations

# 出力する期間の最大数（EDINET の「主要な経営指標等の推移」が5期分までしか取れないため。SPEC §2.9.3）
MAX_PERIODS = 5

# 変化率・ポイント差でトレンドを判定する閾値
TREND_PCT_THRESHOLD = 5.0  # % 単位でない指標（JPY・JPY/share）は変化率(%)で判定
TREND_PT_THRESHOLD = 0.5  # % 単位の指標（ROE 等）は差(ポイント)で判定

# ---------------------------------------------------------------------------
# 指標の定義（凍結された出力キー・ラベル・単位・トレンド判定方法）
# ---------------------------------------------------------------------------
# trend の値: "pct" = 変化率(%)で判定 / "pt" = ポイント差で判定 / None = 判定しない
# reversed: True の指標は「小さいほど良い」ため、判定前に符号を反転する（accruals のみ）
METRIC_META: dict[str, dict] = {
    # ---- そのまま採用する素の値（source="disclosed"。SPEC §2.9.6） ----
    "revenue": {"label": "売上高（収益）", "unit": "JPY", "trend": "pct"},
    "net_income": {"label": "当期純利益", "unit": "JPY", "trend": "pct"},
    "eps": {"label": "EPS（1株当たり利益）", "unit": "JPY/share", "trend": "pct"},
    "total_assets": {"label": "総資産", "unit": "JPY", "trend": None},  # 増減の良し悪しを一概に言えない
    # 純資産（日本基準の「主要な経営指標等の推移」に載る）。非支配株主持分を含むため自己資本とは別物で、
    # 足し合わせたり片方の代用にしたりしない。日本基準では equity が、IFRS では net_assets が
    # 欠測になるのが正常（どちらか一方しか開示されない）
    "net_assets": {"label": "純資産", "unit": "JPY", "trend": None},
    "equity": {"label": "自己資本", "unit": "JPY", "trend": None},  # 同上（IFRS の書類にのみ存在）
    "operating_cf": {"label": "営業キャッシュフロー", "unit": "JPY", "trend": "pct"},
    # ---- 発行体が算出済みの比率。そのまま採用し再計算しない ----
    "roe": {"label": "ROE（自己資本利益率）", "unit": "%", "trend": "pt"},
    "equity_ratio": {"label": "自己資本比率", "unit": "%", "trend": "pt"},
    "per": {"label": "PER", "unit": "times", "trend": None},  # 割安/割高は一概に言えない
    "payout_ratio": {"label": "配当性向", "unit": "%", "trend": None},  # 方針次第で高低の良し悪しが変わる
    # ---- アプリで計算する指標（source="computed"。SPEC §2.9.6） ----
    "revenue_growth": {"label": "売上高成長率", "unit": "%", "trend": "pt"},
    "net_income_growth": {"label": "純利益成長率", "unit": "%", "trend": "pt"},
    "eps_growth": {"label": "EPS成長率", "unit": "%", "trend": "pt"},
    "operating_cf_margin": {"label": "営業CFマージン", "unit": "%", "trend": "pt"},
    "free_cash_flow": {"label": "フリーキャッシュフロー", "unit": "JPY", "trend": "pct"},
    "accruals": {"label": "アクルーアル（利益の質）", "unit": "%", "trend": "pt", "reversed": True},
}

# 開示値をそのまま出す指標（再計算しない）。他はアプリでの計算値
# 中間期の items（interim）もこの並びで出す
DISCLOSED_KEYS = (
    "revenue", "net_income", "eps", "total_assets", "net_assets", "equity", "operating_cf",
    "roe", "equity_ratio", "per", "payout_ratio",
)

# 出力順（画面・AI 向けともにこの順で並べる）
METRIC_ORDER = [
    "revenue", "revenue_growth",
    "net_income", "net_income_growth",
    "eps", "eps_growth",
    "total_assets", "net_assets", "equity",
    "roe", "equity_ratio",
    "operating_cf", "operating_cf_margin", "free_cash_flow",
    "accruals",
    "per", "payout_ratio",
]

# トレンドを判定する指標一覧（モジュール定数として明示。SPEC への反映用）
TREND_JUDGED_KEYS = [key for key in METRIC_ORDER if METRIC_META[key]["trend"] is not None]


# ---------------------------------------------------------------------------
# 個別の計算（他モジュールから直接呼べる小さい純関数。テストしやすくするため下線を付けない）
# ---------------------------------------------------------------------------
def pct_change(prev: float | None, curr: float | None) -> float | None:
    """(curr - prev) / abs(prev) * 100。

    分母を絶対値にするのは、前期が赤字（負）で当期が黒字転換したときに符号が反転してしまう
    標準的な (curr-prev)/prev の弱点を避けるため（前期・当期とも負＝赤字継続の場合に
    直感と合う符号で「悪化/改善」を出せる）。

    次の場合は None（無意味な数字を出さない）:
    - prev または curr が欠損
    - prev が 0（ゼロ除算）
    - prev と curr の符号が異なる（赤字→黒字 or 黒字→赤字の転換。「-3000%」のような無意味な値になる）
    """
    if prev is None or curr is None or prev == 0:
        return None
    if (prev > 0) != (curr > 0):
        return None
    return (curr - prev) / abs(prev) * 100.0


def growth_block_reason(prev: float | None, curr: float | None) -> str | None:
    """pct_change が None になった理由（注記用）。欠損が理由のときは None（注記しない）。"""
    if prev is None or curr is None:
        return None
    if prev == 0:
        return "前期の値がゼロ"
    if prev < 0 and curr > 0:
        return "前期が赤字（黒字転換）"
    if prev > 0 and curr < 0:
        return "当期が赤字（黒字から転換）"
    return None


def pt_diff(prev: float | None, curr: float | None) -> float | None:
    """curr - prev（ポイント差）。prev・curr のどちらかが欠損なら None。

    `%` 単位の指標（ROE・自己資本比率など）の前期比に使う。変化率(%)にすると
    「ROE 2%→8%」が `change_pct: 300.0` のような誤解を招く値になってしまうため
    （比率どうしの割合の変化ではなく、ポイントの差で見る。トレンド判定と同じ理由）。
    符号がまたいでも意味のある差になるので、`pct_change` のような符号チェックは行わない。
    """
    if prev is None or curr is None:
        return None
    return curr - prev


def valid_values(history: list[float | None]) -> list[tuple[int, float]]:
    """(元のインデックス, 値) を欠損以外だけ、昇順のまま返す。"""
    return [(i, v) for i, v in enumerate(history) if v is not None]


def cagr(history: list[float | None]) -> tuple[float | None, str | None]:
    """取得できた期間の CAGR(%) と、None のときの理由（注記用。欠損起因なら None）を返す。

    期数差は period_end の年差ではなく、**history の有効値の個数 - 1** を使う（欠測があっても
    実際に埋まっている期間だけで平均成長率を出す。SPEC §2.9.6）。
    """
    valid = valid_values(history)
    if len(valid) < 2:
        return None, None
    _, earliest = valid[0]
    _, latest = valid[-1]
    n = len(valid) - 1
    if earliest == 0:
        return None, "取得できた最古期の値がゼロ"
    if (earliest < 0) != (latest < 0):
        return None, "取得期間中に符号が反転（赤字/黒字の転換）"
    ratio = latest / earliest
    if ratio < 0:
        return None, "取得期間中に符号が反転（赤字/黒字の転換）"
    return (ratio ** (1.0 / n) - 1.0) * 100.0, None


def percentile(history: list[float | None]) -> float | None:
    """直近の有効値が、取得できた値の範囲内でどこに位置するか（0〜100。最小=0・最大=100）。

    2期未満（有効値が1個以下）なら None。全期間が同値なら範囲がゼロになるため、
    位置なしの意味で中立の 50.0 を返す。
    """
    valid = [v for v in history if v is not None]
    if len(valid) < 2:
        return None
    latest = next(v for v in reversed(history) if v is not None)
    lo, hi = min(valid), max(valid)
    if hi == lo:
        return 50.0
    return (latest - lo) / (hi - lo) * 100.0


def classify_trend(rate: float | None, threshold: float) -> str | None:
    """変化率（またはポイント差）を3値に分類する。"""
    if rate is None:
        return None
    if rate > threshold:
        return "改善"
    if rate < -threshold:
        return "悪化"
    return "横ばい"


def compute_trend(history: list[float | None], mode: str | None, reversed_: bool = False) -> str | None:
    """指標のトレンド（改善/横ばい/悪化）を判定する。

    判定規則（このモジュールで定めたもの。SPEC への転記はメイン側で行う）:

    - 有効な値（欠損以外）が **1個以下なら None**。
    - **2個なら前期比だけ**を見て判定する。
    - **3個以上あれば直近3期（有効値ベース）** を見る。単発の期のブレに惑わされないよう、
      直近3期を2つの一期比（旧→中、中→新）に分け、両方が計算できればその平均を代表の変化率とする。
      片方だけ計算できる場合（符号反転等でもう片方が None）はその一期比だけを使う。両方 None なら None。
    - 変化率が **mode="pct" なら +5%超で改善・-5%未満で悪化・その間は横ばい**。
      **mode="pt"（%単位の指標）なら比率としての変化率ではなくポイント差**で同じ閾値（±0.5pt）を使う
      （ROE が 2%→8% のような比率の変化を「変化率300%」と読むのは直感に反するため）。
      mode=None の指標（PER・配当性向・総資産・自己資本など、増減の良し悪しを一概に言えない指標）は
      常に None。
    - **reversed_=True**（アクルーアルのみ）は符号を反転してから判定する（小さいほど利益の質が高いため）。
    """
    if mode is None:
        return None
    valid = valid_values(history)
    if len(valid) <= 1:
        return None

    def step_rate(prev_v: float, curr_v: float) -> float | None:
        return pct_change(prev_v, curr_v) if mode == "pct" else (curr_v - prev_v)

    if len(valid) == 2:
        (_, prev_v), (_, curr_v) = valid[-2], valid[-1]
        rate = step_rate(prev_v, curr_v)
    else:
        (_, v0), (_, v1), (_, v2) = valid[-3], valid[-2], valid[-1]
        candidates = [r for r in (step_rate(v0, v1), step_rate(v1, v2)) if r is not None]
        rate = sum(candidates) / len(candidates) if candidates else None

    if rate is None:
        return None
    if reversed_:
        rate = -rate
    threshold = TREND_PT_THRESHOLD if mode == "pt" else TREND_PCT_THRESHOLD
    return classify_trend(rate, threshold)


def growth_series(history: list[float | None]) -> list[float | None]:
    """各期の前期比(%)を並べた系列（先頭は前期が無いので必ず None）。"""
    out: list[float | None] = [None]
    for i in range(1, len(history)):
        out.append(pct_change(history[i - 1], history[i]))
    return out


# ---------------------------------------------------------------------------
# 指標1件分の組み立て
# ---------------------------------------------------------------------------
def _build_metric(key: str, history: list[float | None], source: str, notes: list[str]) -> dict:
    meta = METRIC_META[key]
    label, unit, mode = meta["label"], meta["unit"], meta["trend"]

    value = history[-1] if history else None
    prev = history[-2] if len(history) >= 2 else None

    # `%` 単位の指標はポイント差、それ以外（JPY・JPY/share・times）は変化率(%)。
    # トレンド判定（compute_trend）で pt/pct を分けているのと同じ理由（誤読を避ける）
    if unit == "%":
        change_kind = "pt"
        change = pt_diff(prev, value)
    else:
        change_kind = "pct"
        change = pct_change(prev, value)
        reason = growth_block_reason(prev, value)
        if reason:
            notes.append(f"{label}: 前期比は{reason}のため算出していません。")

    # CAGR（複利成長率）は JPY・JPY/share の指標にのみ意味がある。`%` の指標（比率）や
    # `times`（PER）の複利成長率は解釈できないため常に None（欠損起因ではないので注記も出さない）
    if unit in ("%", "times"):
        cagr_value = None
    else:
        cagr_value, cagr_reason = cagr(history)
        if cagr_reason:
            notes.append(f"{label}: CAGR は{cagr_reason}のため算出していません。")

    return {
        "key": key,
        "label": label,
        "value": value,
        "unit": unit,
        "change_pct": change,
        "change_kind": change_kind,
        "cagr_pct": cagr_value,
        "trend": compute_trend(history, mode, meta.get("reversed", False)),
        "history": history,
        "percentile": percentile(history),
        "source": source,
    }


def _empty_result() -> dict:
    return {
        "available": False,
        "basis": None,
        "standard": None,
        "period_count": 0,
        "latest_period_end": None,
        "earliest_period_end": None,
        "metrics": [],
        "interim": None,
        "notes": [],
    }


INTERIM_NOTE = "中間期（6か月）の数値です。通期の系列とは合算していません。前年同期比で比較してください。"


def _build_interim(interim_raw: dict | None, notes: list[str]) -> dict | None:
    """中間期（半期報告書）の実績を「1指標1行」の items に要約する。

    半期報告書しか無い銘柄では通期の `periods` が1件も無いことがあり、それでも直近の実績自体は
    価値がある（実データのモロゾフで確認）。中間期は1〜2期分（当期・前年同期）しか無いため、
    成長率・CAGR・トレンド・percentile・FCF 等の複数期にまたがる計算値は出さない
    （`items` は開示値をそのまま出す `DISCLOSED_KEYS` のうち、値がある項目だけ）。

    `interim_raw` の形（`app/financials.py` の `load_series()` との契約）:
        {"period_end": str, "items": {key: value, ...},
         "prior": {"period_end": str, "items": {key: value, ...}} | None}
    `prior` キーは前年同期が無くても必ず存在し、その場合は None になる。
    """
    if not interim_raw:
        return None

    items_raw = interim_raw.get("items") or {}
    prior = interim_raw.get("prior") or {}
    prior_period_end = prior.get("period_end")
    prior_raw = prior.get("items") or {}

    items = []
    for key in DISCLOSED_KEYS:
        value = items_raw.get(key)
        if value is None:
            continue  # 値の無い項目は行ごと出さない
        meta = METRIC_META[key]
        prior_value = prior_raw.get(key)
        if meta["unit"] == "%":
            change_kind = "pt"
            change = pt_diff(prior_value, value)
        else:
            change_kind = "pct"
            change = pct_change(prior_value, value)
        items.append({
            "key": key,
            "label": meta["label"],
            "value": value,
            "unit": meta["unit"],
            "change_pct": change,
            "change_kind": change_kind,
        })

    notes.append(INTERIM_NOTE)

    return {
        "period_end": interim_raw.get("period_end"),
        "prior_period_end": prior_period_end,
        "items": items,
    }


# ---------------------------------------------------------------------------
# 公開関数
# ---------------------------------------------------------------------------
def compute_metrics(series: dict | None) -> dict:
    """`app/financials.py` の `load_series()` が返す dict から、要約済みの財務指標を作る。

    `series` が None、かつ通期（`periods`）・中間期（`interim`）のどちらも無い（財務数値が
    1件も取れていない銘柄）なら `{"available": False, ...}` を返す（例外にしない。SPEC §2.9.7:
    空欄を並べず、財務のセクションごと省く判断は呼び出し側で行う）。

    半期報告書しか無い銘柄は `periods` が空でも `interim` だけで使えるデータがあるため、
    その場合は `available: True` とし `metrics` は空リストで返す（`period_count` は 0、
    `latest_period_end` / `earliest_period_end` は None のまま）。
    """
    if not series:
        return _empty_result()

    interim_raw = series.get("interim")
    periods = list(series.get("periods") or [])[-MAX_PERIODS:]
    if not periods and not interim_raw:
        return _empty_result()

    basis = series.get("basis")
    standard = series.get("standard")
    notes: list[str] = []

    if basis == "nonconsolidated":
        notes.append("連結の値が取得できないため、単体（非連結）の値を使用しています。")

    if not periods:
        # 通期の開示が無く、中間期（半期報告書）の実績だけがあるケース
        interim = _build_interim(interim_raw, notes)
        return {
            "available": True,
            "basis": basis,
            "standard": standard,
            "period_count": 0,
            "latest_period_end": None,
            "earliest_period_end": None,
            "metrics": [],
            "interim": interim,
            "notes": notes,
        }

    def raw(key: str) -> list[float | None]:
        return [p.get("items", {}).get(key) for p in periods]

    histories: dict[str, list[float | None]] = {
        "revenue": raw("revenue"),
        "net_income": raw("net_income"),
        "eps": raw("eps"),
        "total_assets": raw("total_assets"),
        "net_assets": raw("net_assets"),
        "equity": raw("equity"),
        "operating_cf": raw("operating_cf"),
        "roe": raw("roe"),
        "equity_ratio": raw("equity_ratio"),
        "per": raw("per"),
        "payout_ratio": raw("payout_ratio"),
    }
    investing_cf = raw("investing_cf")  # free_cash_flow の内部計算にのみ使う（単独の指標としては出さない）

    histories["revenue_growth"] = growth_series(histories["revenue"])
    histories["net_income_growth"] = growth_series(histories["net_income"])
    histories["eps_growth"] = growth_series(histories["eps"])

    histories["operating_cf_margin"] = [
        (ocf / rev * 100.0) if (ocf is not None and rev not in (None, 0)) else None
        for ocf, rev in zip(histories["operating_cf"], histories["revenue"])
    ]
    histories["free_cash_flow"] = [
        (ocf + icf) if (ocf is not None and icf is not None) else None
        for ocf, icf in zip(histories["operating_cf"], investing_cf)
    ]
    histories["accruals"] = [
        ((ni - ocf) / ta * 100.0) if (ni is not None and ocf is not None and ta not in (None, 0)) else None
        for ni, ocf, ta in zip(histories["net_income"], histories["operating_cf"], histories["total_assets"])
    ]

    metrics = []
    for key in METRIC_ORDER:
        source = "disclosed" if key in DISCLOSED_KEYS else "computed"
        metrics.append(_build_metric(key, histories[key], source, notes))

    interim = _build_interim(interim_raw, notes)

    return {
        "available": True,
        "basis": basis,
        "standard": standard,
        "period_count": len(periods),
        "latest_period_end": periods[-1].get("period_end"),
        "earliest_period_end": periods[0].get("period_end"),
        "metrics": metrics,
        "interim": interim,
        "notes": notes,
    }
