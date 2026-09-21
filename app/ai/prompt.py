"""AI 分析レポートに送るプロンプトの組み立て（SPEC §2.7.3・§2.7.4・§2.7.5）。

**CLAUDE.md 不変条件1（最重要）**: 需給データ（空売り残高・貸借取引残高）を AI に送らない。

- プロンプト組み立ては `build_prompt` / `build_retry_prompt` の2関数だけに集約する。
  どちらも DB を直接触らない（`PromptSource.load` が作った凍結 `PromptInput` を受け取るだけ）
- `PromptSource` は許可された読み取りだけを行うファサード。需給に関するテーブル
  （空売り残高の個別・合計、貸借取引残高）を読むメソッドは持たない
- 画面のダッシュボード表示に使う集計データは一切参照しない（需給の表示データを含むため）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from .. import ai_export
from .. import disclosures as disclosures_mod
from .. import events
from .. import indicators as ind
from ..database import Database
from ..errors import UserFacingError

# SPEC §2.7.1: 選択できる期間は直近20/60/120日のいずれか
PERIOD_CHOICES: tuple[int, ...] = (20, 60, 120)

# SPEC §2.7.3: 前提として必ず添える注記（開示の対象範囲についての注意）
_DISCLOSURE_NOTE = (
    "開示は EDINET の法定開示のみで、決算短信・業績修正は含まれない。一覧が空でも材料が無いことを意味しない。"
)

_INSTRUCTIONS = (
    "あなたは日本株のテクニカル指標と法定開示を読み解くアシスタントです。以下の指示を厳守してください。\n"
    "- 出力は日本語で書くこと\n"
    "- 投資助言（売買の推奨）はしないこと。あくまで機械的なデータの読み解きにとどめること\n"
    "- 与えられたデータに書かれていない事実を作らないこと（数値・日付・出来事の創作禁止）\n"
    "- 根拠（evidence）は、与えられた株価・指標値・判定・シグナル・開示のいずれかの値を具体的に引用すること\n"
    "- 開示の一覧に無い出来事（決算短信・業績修正など）を根拠に使わないこと\n"
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
class PromptInput:
    """プロンプトの材料一式。`PromptSource.load` だけが作る凍結 dataclass。"""

    symbol: str
    name: str
    exchange: str
    currency: str
    days: int
    price_csv: str  # date,open,high,low,close,volume の CSV ブロック（昇順）
    indicator_csv: str  # date,<指標列...> の CSV ブロック（昇順）
    latest: tuple[MappingProxyType, ...]  # indicators.evaluate_latest の結果（読み取り専用）
    signals: tuple[MappingProxyType, ...]  # indicators.detect_signals の結果（期間内のみ）
    disclosures: tuple[DisclosureItem, ...]
    generated_at: str


# CSV ブロックの列（app/ai_export.py の table 列の並びと一致する）
_PRICE_COLUMNS = ["date", "open", "high", "low", "close", "volume"]


class PromptSource:
    """プロンプトの材料を DB から読む唯一の窓口。

    許可された読み取り（銘柄・株価・指標・開示）だけを行う。需給テーブルを読むメソッドは
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
        price_csv = table[_PRICE_COLUMNS].to_csv(index=False, lineterminator="\n")
        indicator_columns = ["date"] + [c for c in table.columns if c not in _PRICE_COLUMNS]
        indicator_csv = table[indicator_columns].to_csv(index=False, lineterminator="\n")

        latest = tuple(MappingProxyType(card) for card in ind.evaluate_latest(data.prices, data.indicators))

        range_start, range_end = str(table["date"].iloc[0]), str(table["date"].iloc[-1])
        all_signals = ind.detect_signals(data.indicators)
        signals = tuple(
            MappingProxyType(sig) for sig in all_signals if range_start <= sig["date"] <= range_end
        )

        disclosure_items = self._load_disclosures(symbol, range_start, range_end)

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


def build_prompt(data: PromptInput) -> str:
    """SPEC §2.7.3・§2.7.4 に沿ってプロンプト本文を組み立てる（送信データを厳密に統制する唯一の関数）。"""
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
        "## 株価（直近{}日、CSV、date昇順）".format(data.days),
        "```csv",
        data.price_csv.rstrip("\n"),
        "```",
        "",
        "## テクニカル指標（直近{}日、CSV、date昇順）".format(data.days),
        "```csv",
        data.indicator_csv.rstrip("\n"),
        "```",
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
        "## 期間内の開示（EDINET の法定開示のみ。本文は含まない）",
        _format_disclosures(data.disclosures),
    ]
    return "\n".join(sections)


def build_retry_prompt(data: PromptInput, error: str) -> str:
    """検証エラー後の再依頼プロンプト（SPEC §2.7.5 の3番）。"""
    return build_prompt(data) + f"\n\n前回の出力は次の検証エラーで失敗した: {error}。スキーマに厳密に従って再生成せよ"
