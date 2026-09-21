"""AI 分析レポートに送るプロンプトの組み立て（SPEC §2.7.3・§2.7.4・§2.7.5・§2.9.7・§2.9.8）。

**CLAUDE.md 不変条件1（最重要）**: 需給データ（空売り残高・貸借取引残高）を AI に送らない。

- プロンプト組み立ては `build_prompt` / `build_retry_prompt` の2関数だけに集約する。
  どちらも DB を直接触らない（`PromptSource.load` が作った凍結 `PromptInput` を受け取るだけ）
- `PromptSource` は許可された読み取りだけを行うファサード。需給に関するテーブル
  （空売り残高の個別・合計、貸借取引残高）を読むメソッドは持たない
- 画面のダッシュボード表示に使う集計データは一切参照しない（需給の表示データを含むため）
- 財務数値の読み取りは `app/financials.py` の `load_series`（financials テーブルのみを読む）と
  `app/financial_metrics.py` の `compute_metrics`（DB に触れない純関数）だけを使う
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType

from .. import ai_export
from .. import disclosures as disclosures_mod
from .. import events
from .. import financial_metrics
from .. import financials as financials_mod
from .. import indicators as ind
from ..database import Database
from ..errors import UserFacingError

# SPEC §2.7.1: 選択できる期間は直近20/60/120日のいずれか
PERIOD_CHOICES: tuple[int, ...] = (20, 60, 120)

# SPEC §2.9.8: 直近この日数だけ生の CSV（日々の値）を維持する。それより前は要約に置き換える。
# PERIOD_CHOICES の最小値と一致させているので、days=20 のときは圧縮する区間が発生しない
RAW_WINDOW_DAYS = 20

# SPEC §2.9.8 圧縮区間の指標ごとの方向判定（上昇/横ばい/下落）に使う閾値。
# `_direction` の docstring に判定規則そのものを記す
_OSCILLATOR_DIFF_THRESHOLD = 5.0  # 0〜100等に収まる振動指標は「差(pt)」で判定するときの閾値
_LEVEL_PCT_THRESHOLD = 3.0  # 価格スケールの指標は「変化率(%)」で判定するときの閾値(%)

# SPEC §2.7.3: 前提として必ず添える注記（開示の対象範囲についての注意）
_DISCLOSURE_NOTE = (
    "開示は EDINET の法定開示のみで、決算短信・業績修正は含まれない。一覧が空でも材料が無いことを意味しない。"
)

# SPEC §2.9.7: 財務指標に必ず添える注記（期ずれ・四半期報告書の廃止・会社予想対象外）
_FINANCIAL_NOTES: tuple[str, ...] = (
    "決算発表から有価証券報告書の提出まで1.5〜2.5か月のずれがあり、この財務数値は株価より古い時点のものである。",
    "四半期報告書は2024年に廃止されたため、EDINET からは年2回（有価証券報告書・半期報告書）しか取得できない。",
    "会社予想との比較は対象外である。",
)

_INSTRUCTIONS = (
    "あなたは日本株のテクニカル指標と法定開示を読み解くアシスタントです。以下の指示を厳守してください。\n"
    "- 出力は日本語で書くこと\n"
    "- 投資助言（売買の推奨）はしないこと。あくまで機械的なデータの読み解きにとどめること\n"
    "- 与えられたデータに書かれていない事実を作らないこと（数値・日付・出来事の創作禁止）\n"
    "- 根拠（evidence）は、与えられた株価・指標値・判定・シグナル・開示のいずれかの値を具体的に引用すること\n"
    "- 開示の一覧に無い出来事（決算短信・業績修正など）を根拠に使わないこと\n"
    "- 財務数値は株価より古い時点のものなので、株価の直近の動きの原因として断定しないこと\n"
    "- 要約区間については日々の値が与えられていないので、要約に書かれた値だけを根拠に使うこと\n"
    "- 指定されたスキーマに厳密に従い、フィールドの型・上限件数を守ること"
)


@dataclass(frozen=True)
class DisclosureItem:
    """プロンプトに送ってよい開示の項目だけを持つ（本文・原本 URL は持たない。SPEC §2.7.3）。"""

    submit_at: str
    label: str
    description: str
    reason: str
    role: str
    withdrawn: bool


@dataclass(frozen=True)
class IndicatorSummary:
    """圧縮区間（SPEC §2.9.8）における1指標分の要約。日々の値は持たない。"""

    key: str  # AI 向け列名（例: rsi_14）
    start: float  # 区間の期首値
    end: float  # 区間の期末値
    minimum: float
    maximum: float
    direction: str  # "上昇" / "横ばい" / "下落"（判定規則は `_direction` を参照）


@dataclass(frozen=True)
class CompressedPeriod:
    """直近 `RAW_WINDOW_DAYS` 日より前の区間を要約したもの（SPEC §2.9.8）。

    `days` が `RAW_WINDOW_DAYS` 以下のときは圧縮する区間が無いので `PromptInput.compressed` は None になる。
    """

    start: str  # 区間の開始日（date, ISO8601）
    end: str  # 区間の終了日（date, ISO8601）
    trading_days: int
    open: float
    high: float
    low: float
    close: float
    volume_avg: float
    volume_max: int
    signals: tuple[MappingProxyType, ...]  # この区間に発生したシグナルだけ（indicators.detect_signals 由来）
    indicators: tuple[IndicatorSummary, ...]


@dataclass(frozen=True)
class PromptInput:
    """プロンプトの材料一式。`PromptSource.load` だけが作る凍結 dataclass。

    `price_csv` / `indicator_csv` は **直近 `RAW_WINDOW_DAYS` 日（`days` がそれ未満なら `days` 日ぶん全部）
    の生データだけ**を持つ（SPEC §2.9.8 の圧縮により、以前の「`days` 日ぶん全部」という意味から変わった）。
    それより前の期間は `compressed`（None なら圧縮区間なし）が持つ。
    """

    symbol: str
    name: str
    exchange: str
    currency: str
    days: int
    price_csv: str  # date,open,high,low,close,volume の CSV ブロック（昇順、直近 RAW_WINDOW_DAYS 日まで）
    indicator_csv: str  # date,<指標列...> の CSV ブロック（昇順、直近 RAW_WINDOW_DAYS 日まで）
    latest: tuple[MappingProxyType, ...]  # indicators.evaluate_latest の結果（読み取り専用）
    signals: tuple[MappingProxyType, ...]  # indicators.detect_signals の結果（期間内のみ）
    disclosures: tuple[DisclosureItem, ...]
    generated_at: str
    # P11-5 で追加（末尾に追加。既存フィールドの順序は変えない）。
    # `financial_metrics.compute_metrics` の戻り値そのもの（frozen dataclass の外に置くのは、
    # compute_metrics の戻り値の形が SPEC §2.9.6 で凍結された辞書であり、dataclass 化しても
    # 読み取り専用性が増えるわけではないため。MappingProxyType で包むと逆に `financials["metrics"][0]["value"]`
    # のような既存コードの読み方と食い違うので、素の dict のまま持つ）。
    # 既定値は空の dict（`_format_financials` は "available" キーが無ければ「未取得」扱いにする）。
    # 他タスク（report.py 等）が `PromptInput` を直接組み立てる既存テストを壊さないようにするための既定値
    financials: dict = field(default_factory=dict)
    # SPEC §2.9.8 の圧縮区間。圧縮する区間が無い（days <= RAW_WINDOW_DAYS）ときは None。
    # 既定値 None も同じ理由（他タスクの既存テストが指定しなくても構築できるようにする）
    compressed: CompressedPeriod | None = None


# CSV ブロックの列（app/ai_export.py の table 列の並びと一致する）
_PRICE_COLUMNS = ["date", "open", "high", "low", "close", "volume"]

# DB 列名 -> AI 向け列名 の逆引き（value_kind の判定に使う。ai_export の table は AI 向け列名に
# 変換済みなので、圧縮区間の指標ごとの方向判定にはこの逆引きが必要）
_AI_NAME_TO_KEY = {v: k for k, v in ind.ai_column_names().items()}


def _indicator_kind(ai_column_name: str) -> str:
    """AI 向け列名から `indicators.value_kind` の分類（price/macd/pct）を引く。"""
    key = _AI_NAME_TO_KEY.get(ai_column_name)
    return ind.value_kind(key) if key is not None else "price"


class PromptSource:
    """プロンプトの材料を DB から読む唯一の窓口。

    許可された読み取り（銘柄・株価・指標・開示・財務指標）だけを行う。需給テーブルを読むメソッドは
    持たない（CLAUDE.md 不変条件1）。
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def load(self, symbol: str, days: int, *, now: datetime | None = None) -> PromptInput:
        """指定銘柄・期間のプロンプト材料を読み、`PromptInput` にまとめて返す。

        `days` が `PERIOD_CHOICES` 以外、または株価が無い銘柄なら `UserFacingError`。
        """
        if days not in PERIOD_CHOICES:
            raise UserFacingError(
                f"期間は{'/'.join(str(d) for d in PERIOD_CHOICES)}日のいずれかで指定してください: {days}"
            )
        try:
            data = ai_export.load(self.db, [symbol], days)[0]
        except ValueError as exc:
            raise UserFacingError(str(exc)) from exc

        table = data.table  # 直近 days 日ぶんの株価+指標（AI 向け列名・昇順）
        indicator_value_columns = [c for c in table.columns if c not in _PRICE_COLUMNS]

        range_start, range_end = str(table["date"].iloc[0]), str(table["date"].iloc[-1])
        all_signals = ind.detect_signals(data.indicators)
        signals = tuple(
            MappingProxyType(sig) for sig in all_signals if range_start <= sig["date"] <= range_end
        )

        # SPEC §2.9.8: 直近 RAW_WINDOW_DAYS 日は生のまま、それより前は要約に圧縮する
        raw_table = table.tail(RAW_WINDOW_DAYS)
        compressed_table = table.iloc[: len(table) - len(raw_table)]
        compressed = (
            _build_compressed_period(compressed_table, all_signals) if not compressed_table.empty else None
        )

        price_csv = raw_table[_PRICE_COLUMNS].to_csv(index=False, lineterminator="\n")
        indicator_csv = raw_table[["date"] + indicator_value_columns].to_csv(index=False, lineterminator="\n")

        latest = tuple(MappingProxyType(card) for card in ind.evaluate_latest(data.prices, data.indicators))

        disclosure_items = self._load_disclosures(symbol, range_start, range_end)

        # 財務指標（SPEC §2.9.7）。`load_series` は financials テーブルしか読まない。
        # 財務数値が1件も無い銘柄でも例外にならず {"available": False, ...} が返る
        series = financials_mod.load_series(self.db, symbol)
        financials = financial_metrics.compute_metrics(series)

        now_ = now if now is not None else datetime.now()
        stock = data.stock
        return PromptInput(
            symbol=stock["symbol"],
            name=stock["name"],
            exchange=stock["exchange"] or "",
            currency=stock["currency"] or "",
            days=days,
            price_csv=price_csv,
            indicator_csv=indicator_csv,
            latest=latest,
            signals=signals,
            disclosures=disclosure_items,
            generated_at=now_.strftime("%Y-%m-%d %H:%M:%S"),
            financials=financials,
            compressed=compressed,
        )

    def _load_disclosures(self, symbol: str, range_start: str, range_end: str) -> tuple[DisclosureItem, ...]:
        """株価 CSV の期間（`range_start` 〜 `range_end`）に提出日が入る開示だけを送ってよい形にする。"""
        listing = disclosures_mod.list_for_symbol(self.db, symbol)
        items = []
        for d in listing["items"]:
            submit_at = d.get("submit_at") or ""
            submit_date = submit_at[:10]
            if not (range_start <= submit_date <= range_end):
                continue
            withdrawal = d.get("withdrawal")
            items.append(
                DisclosureItem(
                    submit_at=submit_at,
                    label=events.label_for(d.get("doc_type_code")),
                    description=d.get("description") or "",
                    reason=d.get("reason") or "",
                    role="/".join(d.get("roles") or []),
                    withdrawn=withdrawal is not None and withdrawal != 0,
                )
            )
        # list_for_symbol は submit_at 降順で返るので、プロンプトでは株価・指標の CSV と
        # 同じ「古い→新しい」の昇順に揃える
        items.sort(key=lambda item: item.submit_at)
        return tuple(items)


# ---------------------------------------------------------------------------
# 圧縮区間の組み立て（SPEC §2.9.8。DB には触れない純粋な計算）
# ---------------------------------------------------------------------------
def _direction(start: float, end: float, kind: str) -> str:
    """区間の期首値→期末値の変化から方向（上昇/横ばい/下落）を判定する（SPEC §2.9.8）。

    判定規則（このモジュールで定めたもの。SPEC への転記はメイン側で行う）:

    - `kind == "pct"`（`indicators.value_kind` が "pct" を返す列。RSI・RCI・ストキャス%K/%D・
      +DI/-DI/ADX・移動平均乖離率・サイコロジカルラインなど、0〜100や-100〜100等の範囲に収まる
      比率・オシレータ系の列）は **変化率ではなく差（pt）** で見る。値そのものが小さい期間から
      変化すると変化率が過大に出るため（例: RSI が 2→8 なら変化率は+300%だが、意味は「わずかな
      戻り」に過ぎない）。差が `+_OSCILLATOR_DIFF_THRESHOLD` 超なら「上昇」、
      `-_OSCILLATOR_DIFF_THRESHOLD` 未満なら「下落」、それ以外は「横ばい」
    - それ以外（`kind` が "price" または "macd"。SMA/EMA/ボリンジャー/一目/GMMA/パラボリック/
      標準偏差/モメンタム/MACD など、株価と同じ円スケールの列）は **変化率（%）** で見る。
      期首値がほぼ0（1e-9未満）のときはゼロ除算を避け、期首→期末の符号だけで判定する。
      変化率が `+_LEVEL_PCT_THRESHOLD`(%) 超なら「上昇」、`-_LEVEL_PCT_THRESHOLD`(%) 未満なら「下落」、
      それ以外は「横ばい」
    """
    if kind == "pct":
        diff = end - start
        if diff > _OSCILLATOR_DIFF_THRESHOLD:
            return "上昇"
        if diff < -_OSCILLATOR_DIFF_THRESHOLD:
            return "下落"
        return "横ばい"

    if abs(start) < 1e-9:
        diff = end - start
        if diff > 1e-9:
            return "上昇"
        if diff < -1e-9:
            return "下落"
        return "横ばい"

    pct = (end - start) / abs(start) * 100.0
    if pct > _LEVEL_PCT_THRESHOLD:
        return "上昇"
    if pct < -_LEVEL_PCT_THRESHOLD:
        return "下落"
    return "横ばい"


def _build_compressed_period(compressed_table, all_signals) -> CompressedPeriod:
    """`compressed_table`（直近 RAW_WINDOW_DAYS 日より前の株価+指標）から `CompressedPeriod` を作る。

    `compressed_table` は空でないことを呼び出し側（`PromptSource.load`）が保証する。
    """
    start, end = str(compressed_table["date"].iloc[0]), str(compressed_table["date"].iloc[-1])
    signals = tuple(MappingProxyType(sig) for sig in all_signals if start <= sig["date"] <= end)

    summaries = []
    for col in [c for c in compressed_table.columns if c not in _PRICE_COLUMNS]:
        series = compressed_table[col].dropna()
        if series.empty:
            continue  # この区間ずっと欠測（長期移動平均の立ち上がり等）の列は出さない
        col_start, col_end = float(series.iloc[0]), float(series.iloc[-1])
        summaries.append(
            IndicatorSummary(
                key=col,
                start=col_start,
                end=col_end,
                minimum=float(series.min()),
                maximum=float(series.max()),
                direction=_direction(col_start, col_end, _indicator_kind(col)),
            )
        )

    return CompressedPeriod(
        start=start,
        end=end,
        trading_days=len(compressed_table),
        open=float(compressed_table["open"].iloc[0]),
        high=float(compressed_table["high"].max()),
        low=float(compressed_table["low"].min()),
        close=float(compressed_table["close"].iloc[-1]),
        volume_avg=float(compressed_table["volume"].mean()),
        volume_max=int(compressed_table["volume"].max()),
        signals=signals,
        indicators=tuple(summaries),
    )


def _format_definitions() -> str:
    """指標の列名は英語の略号なので、読み方を添えないと根拠の引用が当てにならない。

    `ai_export.render_markdown` が人向けの出力に添えているものと同じ定義表を使う（静的な説明文で、
    銘柄のデータは含まない）。
    """
    return "\n".join(f"- {cols}: {text}" for cols, text in ind.indicator_definitions())


def _format_latest(latest: tuple[MappingProxyType, ...]) -> str:
    if not latest:
        return "（判定なし。データ不足）"
    lines = []
    for card in latest:
        value = "" if card["value"] is None else card["value"]
        lines.append(f"- {card['label']}（{card['key']}）: 判定={card['status']} 値={value} 備考={card['note']}")
    return "\n".join(lines)


def _format_signals(signals: tuple[MappingProxyType, ...]) -> str:
    if not signals:
        return "（期間内のシグナルなし）"
    return "\n".join(f"- {sig['date']} {sig['direction']}: {sig['label']}（{sig['short']}）" for sig in signals)


def _format_disclosures(items: tuple[DisclosureItem, ...]) -> str:
    if not items:
        return "（期間内の開示なし。" + _DISCLOSURE_NOTE + "）"
    lines = []
    for d in items:
        withdrawn_note = "【取下げ済み】" if d.withdrawn else ""
        reason_part = f" 提出事由={d.reason}" if d.reason else ""
        lines.append(
            f"- {d.submit_at} {withdrawn_note}種別={d.label} 関係={d.role} 概要={d.description}{reason_part}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 株価・テクニカル指標セクション（SPEC §2.9.8: 直近は生CSV、それより前は要約）
# ---------------------------------------------------------------------------
def _csv_row_count(csv_text: str) -> int:
    lines = csv_text.strip("\n").splitlines()
    return max(len(lines) - 1, 0)  # ヘッダ行を除く


def _format_price_section(data: PromptInput) -> list[str]:
    raw_rows = _csv_row_count(data.price_csv)
    if data.compressed is None:
        # 圧縮する区間が無い（days <= RAW_WINDOW_DAYS）ので、全部が生の CSV
        return [
            f"## 株価（直近{data.days}日、CSV、date昇順）",
            "```csv",
            data.price_csv.rstrip("\n"),
            "```",
        ]
    c = data.compressed
    return [
        f"## 株価（直近{data.days}日ぶん。直近{raw_rows}日は生CSV、{c.start}〜{c.end}は要約に圧縮）",
        f"### 直近{raw_rows}日（生CSV、date昇順。ここより前の日々の値は与えられていない）",
        "```csv",
        data.price_csv.rstrip("\n"),
        "```",
        "",
        f"### {c.start}〜{c.end}の要約（{c.trading_days}営業日ぶん。日々の値は無く、この要約だけが根拠にできる）",
        f"- 始値: {c.open}",
        f"- 高値: {c.high}",
        f"- 安値: {c.low}",
        f"- 終値: {c.close}",
        f"- 出来高 平均: {c.volume_avg:.0f}",
        f"- 出来高 最大: {c.volume_max}",
        "- この区間のシグナル:",
        _format_signals(c.signals),
    ]


def _format_indicator_section(data: PromptInput) -> list[str]:
    raw_rows = _csv_row_count(data.indicator_csv)
    if data.compressed is None:
        return [
            f"## テクニカル指標（直近{data.days}日、CSV、date昇順）",
            "```csv",
            data.indicator_csv.rstrip("\n"),
            "```",
        ]
    c = data.compressed
    lines = [
        f"## テクニカル指標（直近{data.days}日ぶん。直近{raw_rows}日は生CSV、{c.start}〜{c.end}は指標ごとの要約に圧縮）",
        f"### 直近{raw_rows}日（生CSV、date昇順。ここより前の日々の値は与えられていない）",
        "```csv",
        data.indicator_csv.rstrip("\n"),
        "```",
        "",
        f"### {c.start}〜{c.end}の指標要約（{c.trading_days}営業日ぶん。日々の値は無いので、この要約の値だけが根拠にできる。方向は上昇/横ばい/下落）",
    ]
    for s in c.indicators:
        lines.append(
            f"- {s.key}: 期首={s.start:.4f} 期末={s.end:.4f} 最小={s.minimum:.4f} 最大={s.maximum:.4f} 方向={s.direction}"
        )
    return lines


# ---------------------------------------------------------------------------
# 財務指標セクション（SPEC §2.9.7）
# ---------------------------------------------------------------------------
_STANDARD_LABELS: dict[str | None, str] = {
    "jgaap": "日本基準",
    "ifrs": "IFRS",
    "usgaap": "米国基準",
    None: "不明",
}
_BASIS_LABELS: dict[str | None, str] = {
    "consolidated": "連結",
    "nonconsolidated": "単体",
    None: "不明",
}


def _format_financial_value(value: float, unit: str) -> str:
    """財務指標の値を、単位に応じた読みやすい表記にする（SPEC §2.9.7: 金額は百万円単位に丸める）。"""
    if unit == "JPY":
        return f"{value / 1_000_000:,.0f}百万円"
    if unit == "JPY/share":
        return f"{value:,.2f}円"
    if unit == "%":
        return f"{value:.1f}%"
    if unit == "times":
        return f"{value:.1f}倍"
    if unit == "shares":
        return f"{value:,.0f}株"
    if unit == "persons":
        return f"{value:,.0f}人"
    return str(value)


def _format_change(change: float | None, change_kind: str) -> str:
    """`change_kind` に応じて変化率(%)とポイント差(pt)を書き分ける。"""
    if change is None:
        return ""
    suffix = "pt" if change_kind == "pt" else "%"
    return f"{change:+.1f}{suffix}"


def _format_metric_line(metric: dict) -> str | None:
    """財務指標1件を1行にする。値もhistoryも全部欠損なら None（行ごと省く。SPEC §2.9.7）。"""
    history = metric.get("history") or []
    if not any(h is not None for h in history):
        return None

    value = metric["value"]
    value_text = (
        _format_financial_value(value, metric["unit"]) if value is not None else "データなし（直近期は未開示）"
    )
    extras = []
    change_text = _format_change(metric["change_pct"], metric["change_kind"])
    if change_text:
        extras.append(f"前期比 {change_text}")
    if metric["trend"]:
        extras.append(f"トレンド {metric['trend']}")
    if metric["cagr_pct"] is not None:
        extras.append(f"CAGR {metric['cagr_pct']:+.1f}%")
    suffix = f"（{'、'.join(extras)}）" if extras else ""
    return f"- {metric['label']}: {value_text}{suffix}"


def _format_financials(financials: dict) -> list[str]:
    """`## 財務指標` セクションを組み立てる（SPEC §2.9.7）。"""
    if not financials.get("available"):
        return ["## 財務指標", "財務数値は未取得（有価証券報告書の取り込みが行われていない）"]

    lines = ["## 財務指標"]

    standard_label = _STANDARD_LABELS.get(financials.get("standard"), str(financials.get("standard")))
    basis_label = _BASIS_LABELS.get(financials.get("basis"), str(financials.get("basis")))
    if financials.get("period_count"):
        period_text = (
            f"{financials['earliest_period_end']}〜{financials['latest_period_end']}"
            f"（{financials['period_count']}期）"
        )
    else:
        period_text = "通期（有価証券報告書）の開示なし"
    lines.append(f"- 会計基準: {standard_label} ／ {basis_label} ／ {period_text}")
    # 「前期比」と「トレンド」は見ている期間が違う。凡例を1行置かないと
    # 「前期比 +1.9pt、トレンド 悪化」のような行が矛盾して読める（実データで確認）
    lines.append(
        "- 読み方: 「前期比」は直前の1期との比較、「トレンド」は直近3期の平均的な変化方向。"
        "この2つは食い違うことがある（直近1期は上向いたが3期では下向き、など）"
    )

    metric_lines = [line for m in financials.get("metrics", []) if (line := _format_metric_line(m))]
    lines.extend(metric_lines)

    for note in financials.get("notes", []):
        lines.append(f"- 備考: {note}")

    interim = financials.get("interim")
    if interim:
        lines.append("")
        lines.append("### 直近の中間期（6か月。通期の系列とは合算していない）")
        prior_part = f"／前年同期: {interim['prior_period_end']}" if interim.get("prior_period_end") else "（前年同期なし）"
        lines.append(f"- 対象期: {interim['period_end']}{prior_part}")
        for item in interim.get("items", []):
            value_text = _format_financial_value(item["value"], item["unit"])
            change_text = _format_change(item["change_pct"], item["change_kind"])
            change_part = f"（前年同期比 {change_text}）" if change_text else ""
            lines.append(f"- {item['label']}: {value_text}{change_part}")

    lines.append("")
    for note in _FINANCIAL_NOTES:
        lines.append(f"- {note}")

    return lines


def build_prompt(data: PromptInput) -> str:
    """SPEC §2.7.3・§2.7.4・§2.9.7・§2.9.8 に沿ってプロンプト本文を組み立てる（送信データを厳密に統制する唯一の関数）。"""
    sections = [
        _INSTRUCTIONS,
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
        f"- {_DISCLOSURE_NOTE}",
        "",
        *_format_price_section(data),
        "",
        *_format_indicator_section(data),
        "",
        "## 指標の定義（列名の読み方）",
        _format_definitions(),
        "",
        "## 最新日の指標判定",
        _format_latest(data.latest),
        "",
        "## 期間内のシグナル",
        _format_signals(data.signals),
        "",
        *_format_financials(data.financials),
        "",
        "## 期間内の開示（EDINET の法定開示のみ。本文は含まない）",
        _format_disclosures(data.disclosures),
    ]
    return "\n".join(sections)


def build_retry_prompt(data: PromptInput, error: str) -> str:
    """検証エラー後の再依頼プロンプト（SPEC §2.7.5 の3番）。"""
    return build_prompt(data) + f"\n\n前回の出力は次の検証エラーで失敗した: {error}。スキーマに厳密に従って再生成せよ"
