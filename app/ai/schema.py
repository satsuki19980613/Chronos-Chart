"""Gemini の構造化出力スキーマ（SPEC §2.7.4）。

`google-genai` の `response_schema` にそのまま渡す Pydantic v2 モデル。Gemini には HTML を
書かせず、この形の JSON だけを返させる。HTML は `app/ai/report.py`（別タスク）が Jinja2 で組み立てる。

**フィールドの定義順がそのまま Gemini の出力順になる。** `SectionAnalysis` は
`evidence`（根拠）→ `assessment`（評価）、`AnalysisReport` は各観点 → `risks` → `watch_points` →
`verdict` → `confidence` → `summary` の順で、常に「根拠 → 結論」を保つこと。先に結論（`verdict` 等）
を出力させると、あとに続く根拠がその結論に合わせた後付けになってしまうため（SPEC §2.7.4）。
レポート上の表示順（結論を先頭に見せるなど）はテンプレート側の自由でよく、ここでの定義順とは無関係。

各フィールドの `description` は日本語で書く。`response_schema` に渡すと Gemini 側にもこの
説明文が伝わる。ただし「日本語で答えよ」という指示自体はここには書かない（プロンプト側の役割）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SectionAnalysis(BaseModel):
    """観点（テクニカル／開示）ごとの分析。根拠を先に書かせてから評価させる。"""

    evidence: list[str] = Field(
        max_length=6,
        description="この観点についての根拠。与えられたデータ（株価・指標値・判定・シグナル・開示等）に"
        "実際に現れている事実だけを挙げる。データに無い事実を作らない。最大6件",
    )
    assessment: str = Field(description="上記の根拠を踏まえた、この観点についての評価")


class AnalysisReport(BaseModel):
    """Gemini に生成させる分析レポート全体の構造。"""

    technical: SectionAnalysis = Field(description="テクニカル指標の観点からの分析（根拠→評価）")
    disclosure: SectionAnalysis = Field(description="EDINET の法定開示の観点からの分析（根拠→評価）")
    risks: list[str] = Field(max_length=5, description="留意すべきリスク要因。最大5件")
    watch_points: list[str] = Field(max_length=5, description="今後注目すべき点。最大5件")
    verdict: Literal["bullish", "bearish", "neutral"] = Field(description="総合判定。強気/弱気/中立の3択")
    confidence: Literal["low", "medium", "high"] = Field(
        description="上記判定に対する自己申告の確信度。較正された確率ではないため数値ではなく3段階"
    )
    summary: str = Field(description="分析全体の総括。3行以内")
