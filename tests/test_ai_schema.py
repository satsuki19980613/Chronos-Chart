"""P6-4: 検証と再依頼（SPEC §2.7.5・§2.7.2・§2.7.1）。

ネットワークには一切アクセスしない。`GeminiClient` の代わりに `count_tokens` / `generate` /
`model` だけを持つフェイクを渡す。`Quota` は本物（`app.ai.quota.Quota`）を使い、`Settings` 経由で
上限を調整することでガードの発動条件を作る。

ファイル名が `test_ai_schema.py` なのは SPEC §10.2 の一覧に合わせているため
（実体は `app/ai/analyze.py` の `run_analysis` / `estimate` のテスト）。
"""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import pytest
from conftest import make_prices
from pydantic import ValidationError

from app import config
from app.ai import analyze
from app.ai.analyze import MAX_RETRIES, AnalysisResult, estimate, run_analysis
from app.ai.client import QuotaExceeded, QuotaHit, Reply, Usage
from app.ai.quota import Quota, usage as quota_usage
from app.ai.schema import AnalysisReport, SectionAnalysis
from app.database import Database
from app.errors import Cancelled, UserFacingError
from app.settings import Settings

SYMBOL = "7203.T"
CODE = "7203"


# ---------------------------------------------------------------------------
# 準備用ヘルパー
# ---------------------------------------------------------------------------
def _db(tmp_path) -> Database:
    db = Database(tmp_path / "ai_analyze.db")
    db.init_schema()
    return db


def _register(db: Database, days: int = 150) -> None:
    db.upsert_stock(SYMBOL, CODE, "トヨタ自動車", "東証プライム", "JPY")
    prices = make_prices(100 + np.sin(np.arange(days) / 7) * 5 + np.arange(days) * 0.05)
    db.upsert_prices(SYMBOL, prices)


def _settings(db: Database, *, rpm: int = 60, tpm: int = 1_000_000, rpd: int = 100) -> Settings:
    settings = Settings(db)
    settings.update(
        {
            "gemini_model": "gemini-test-model",
            "gemini_rpm": rpm,
            "gemini_tpm": tpm,
            "gemini_rpd": rpd,
        }
    )
    return settings


def _valid_report_json() -> str:
    report = AnalysisReport(
        technical=SectionAnalysis(evidence=["株価が上昇"], assessment="堅調"),
        fundamental=SectionAnalysis(evidence=["財務数値は未取得"], assessment="未取得"),
        disclosure=SectionAnalysis(evidence=["開示なし"], assessment="材料なし"),
        risks=["市場全体の変動"],
        watch_points=["次回の開示"],
        verdict="neutral",
        confidence="low",
        summary="総括",
        data_scope_note="会社予想との比較は対象外。同業他社との比較は対象外。需給データは分析に含まない。",
    )
    return report.model_dump_json()


def _reply(text: str, *, finish_reason: str = "STOP", prompt_tokens: int = 100, output_tokens: int = 50) -> Reply:
    return Reply(
        text=text,
        finish_reason=finish_reason,
        usage=Usage(
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            thoughts_tokens=0,
            total_tokens=prompt_tokens + output_tokens,
        ),
    )


class FakeGeminiClient:
    """`count_tokens` / `generate` / `model` だけを持つフェイク。

    `script` は `generate()` を呼ぶたびに1つずつ消費する。要素が `Reply` ならそれを返し、
    `Exception` のインスタンスなら送出する。呼び出しごとのプロンプトは `prompts` に記録する。
    """

    def __init__(self, script: list, *, model: str = "gemini-test-model", count_tokens_value: int = 10) -> None:
        self._script = list(script)
        self._model = model
        self._count_tokens_value = count_tokens_value
        self.prompts: list[str] = []
        self.count_tokens_calls: list[str] = []

    @property
    def model(self) -> str:
        return self._model

    def count_tokens(self, prompt: str) -> int:
        self.count_tokens_calls.append(prompt)
        return self._count_tokens_value

    def generate(self, prompt: str, schema=None) -> Reply:
        self.prompts.append(prompt)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _counting_quota(db: Database, settings: Settings) -> tuple[Quota, list]:
    """`start_request` の呼び出し回数を数えられるようにした `Quota`。"""
    quota = Quota(db, settings)
    calls: list[int] = []
    original = quota.start_request

    def counting_start_request(need_tokens, **kwargs):
        calls.append(need_tokens)
        return original(need_tokens, **kwargs)

    quota.start_request = counting_start_request  # type: ignore[method-assign]
    return quota, calls


# ---------------------------------------------------------------------------
# 基本の成功パス
# ---------------------------------------------------------------------------
def test_success_on_first_try(tmp_path):
    db = _db(tmp_path)
    _register(db)
    settings = _settings(db)
    fake = FakeGeminiClient([_reply(_valid_report_json(), prompt_tokens=120, output_tokens=80)])

    result = run_analysis(db, settings, SYMBOL, 20, client=fake, sleep=lambda s: None)

    assert isinstance(result, AnalysisResult)
    assert result.attempts == 1
    assert result.model == fake.model
    assert result.report.verdict == "neutral"
    assert result.in_tokens == 120
    assert result.out_tokens == 80
    assert result.data.symbol == SYMBOL


def test_progress_notifications(tmp_path):
    db = _db(tmp_path)
    _register(db)
    settings = _settings(db)
    fake = FakeGeminiClient([_reply(_valid_report_json())])
    events = []

    run_analysis(
        db, settings, SYMBOL, 20, client=fake, sleep=lambda s: None,
        progress=lambda current, total, label: events.append((current, total, label)),
    )

    labels = [e[2] for e in events]
    assert "送信中" in labels
    assert "検証中" in labels
    assert all(total == 1 + MAX_RETRIES for _, total, _ in events)


# ---------------------------------------------------------------------------
# スキーマ不一致 → 再依頼
# ---------------------------------------------------------------------------
def test_retry_after_schema_violation_then_success(tmp_path):
    db = _db(tmp_path)
    _register(db)
    settings = _settings(db)
    bad_text = "これはJSONではない"
    fake = FakeGeminiClient(
        [
            _reply(bad_text, prompt_tokens=80, output_tokens=40),
            _reply(_valid_report_json(), prompt_tokens=120, output_tokens=60),
        ]
    )
    progress_labels = []

    result = run_analysis(
        db, settings, SYMBOL, 20, client=fake, sleep=lambda s: None,
        progress=lambda current, total, label: progress_labels.append(label),
    )

    assert result.attempts == 2
    assert result.in_tokens == 80 + 120
    assert result.out_tokens == 40 + 60
    assert len(fake.prompts) == 2

    try:
        AnalysisReport.model_validate_json(bad_text)
        pytest.fail("bad_text はスキーマ違反であるはず")
    except ValidationError as exc:
        expected_error = str(exc)[:2000]
    assert expected_error in fake.prompts[1]
    assert fake.prompts[0] in fake.prompts[1]  # 再依頼プロンプトは元のプロンプトを含む
    assert any("再依頼 1/2" in label for label in progress_labels)


def test_three_failures_saves_raw_response_and_raises(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _register(db)
    settings = _settings(db)
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")

    fake = FakeGeminiClient(
        [
            _reply("不正な応答1"),
            _reply("不正な応答2"),
            _reply("不正な応答3"),
        ]
    )

    with pytest.raises(UserFacingError):
        run_analysis(db, settings, SYMBOL, 20, client=fake, sleep=lambda s: None)

    assert len(fake.prompts) == 1 + MAX_RETRIES
    saved = list((tmp_path / "logs").glob(f"ai_response_*{CODE}*.txt")) + list(
        (tmp_path / "logs").glob("ai_response_*.txt")
    )
    assert saved, "生のレスポンスが data/logs/ に保存されていない"
    content = saved[0].read_text(encoding="utf-8")
    assert content == "不正な応答3"
    # プロンプトは保存しない
    for prompt in fake.prompts:
        assert prompt not in content


# ---------------------------------------------------------------------------
# MAX_TOKENS
# ---------------------------------------------------------------------------
def test_max_tokens_aborts_without_parsing(tmp_path):
    db = _db(tmp_path)
    _register(db)
    settings = _settings(db)
    fake = FakeGeminiClient([_reply("途中で切れたJSON", finish_reason="MAX_TOKENS")])

    with pytest.raises(UserFacingError) as excinfo:
        run_analysis(db, settings, SYMBOL, 20, client=fake, sleep=lambda s: None)

    message = str(excinfo.value)
    assert "出力トークン上限" in message
    assert "thinking" in message
    assert "期間" not in message
    assert len(fake.prompts) == 1  # 再依頼しない


# ---------------------------------------------------------------------------
# クォータのガード（再依頼のたびに通す）
# ---------------------------------------------------------------------------
def test_start_request_called_on_every_retry(tmp_path):
    db = _db(tmp_path)
    _register(db)
    settings = _settings(db)
    quota, calls = _counting_quota(db, settings)
    fake = FakeGeminiClient(
        [
            _reply("不正な応答"),
            _reply(_valid_report_json()),
        ]
    )

    result = run_analysis(db, settings, SYMBOL, 20, client=fake, quota=quota, sleep=lambda s: None)

    assert result.attempts == 2
    assert len(calls) == 2  # 初回・再依頼のたびに start_request を通している


def test_rpd_exhausted_blocks_second_attempt(tmp_path):
    db = _db(tmp_path)
    _register(db)
    settings = _settings(db, rpd=1)  # 1日1リクエストしか送れない設定
    quota, calls = _counting_quota(db, settings)
    fake = FakeGeminiClient(
        [
            _reply("不正な応答"),  # 1回目は送信できる（RPD を使い切る）
            _reply(_valid_report_json()),  # 2回目はガードで弾かれ、ここまで届かないはず
        ]
    )

    with pytest.raises(UserFacingError) as excinfo:
        run_analysis(db, settings, SYMBOL, 20, client=fake, quota=quota, sleep=lambda s: None)

    assert "無料枠" in str(excinfo.value)
    assert len(fake.prompts) == 1  # 2回目は generate まで到達しない
    assert len(calls) == 2  # start_request 自体は2回目も呼ばれ、その中で弾かれた


# ---------------------------------------------------------------------------
# 429（分次・日次）
# ---------------------------------------------------------------------------
def test_per_minute_quota_hit_retries_once_then_succeeds(tmp_path):
    db = _db(tmp_path)
    _register(db)
    settings = _settings(db)
    hit = QuotaHit(scope="per_minute", retry_delay=37.0, quota_ids=("PerMinute",), message="1分あたりの上限")
    fake = FakeGeminiClient(
        [
            QuotaExceeded(hit),
            _reply(_valid_report_json(), prompt_tokens=90, output_tokens=45),
        ]
    )
    sleep_calls: list[float] = []

    result = run_analysis(db, settings, SYMBOL, 20, client=fake, sleep=lambda s: sleep_calls.append(s))

    assert sleep_calls == [37.0]
    assert result.attempts == 2  # 429 の再送も実際の送信回数に数える
    assert result.report.verdict == "neutral"
    # per_minute は打ち切りフラグを立てない
    assert quota_usage(db, settings.get("gemini_model"))["exhausted"] == 0


def test_per_minute_quota_hit_without_retry_delay_uses_default_60(tmp_path):
    db = _db(tmp_path)
    _register(db)
    settings = _settings(db)
    hit = QuotaHit(scope="per_minute", retry_delay=None, quota_ids=(), message="1分あたりの上限")
    fake = FakeGeminiClient([QuotaExceeded(hit), _reply(_valid_report_json())])
    sleep_calls: list[float] = []

    run_analysis(db, settings, SYMBOL, 20, client=fake, sleep=lambda s: sleep_calls.append(s))

    assert sleep_calls == [60.0]


def test_per_day_quota_hit_aborts_and_marks_exhausted(tmp_path):
    db = _db(tmp_path)
    _register(db)
    settings = _settings(db)
    hit = QuotaHit(scope="per_day", retry_delay=None, quota_ids=("PerDay",), message="本日の上限")
    fake = FakeGeminiClient([QuotaExceeded(hit)])

    with pytest.raises(QuotaExceeded):
        run_analysis(db, settings, SYMBOL, 20, client=fake, sleep=lambda s: None)

    assert len(fake.prompts) == 1  # 再送しない
    assert quota_usage(db, settings.get("gemini_model"))["exhausted"] == 1


# ---------------------------------------------------------------------------
# 中断
# ---------------------------------------------------------------------------
def test_cancel_raises_cancelled(tmp_path):
    db = _db(tmp_path)
    _register(db)
    settings = _settings(db)
    fake = FakeGeminiClient([])
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(Cancelled):
        run_analysis(db, settings, SYMBOL, 20, client=fake, cancel=cancel, sleep=lambda s: None)

    assert fake.prompts == []  # 送信前に中断される


# ---------------------------------------------------------------------------
# estimate()
# ---------------------------------------------------------------------------
def test_estimate_can_run_true(tmp_path):
    db = _db(tmp_path)
    _register(db)
    settings = _settings(db)
    fake = FakeGeminiClient([], count_tokens_value=42)

    result = estimate(db, settings, SYMBOL, 20, client=fake)

    assert result["can_run"] is True
    assert result["reason"] is None
    assert result["input_tokens"] == 42
    assert result["symbol"] == SYMBOL
    assert result["days"] == 20
    assert result["model"] == fake.model
    assert result["sends"] == ["銘柄情報", "株価とテクニカル指標", "EDINET の開示（本文は含まない）"]
    assert "limits" in result["quota"]
    assert "remaining" in result["quota"]


def test_estimate_can_run_false_when_exhausted(tmp_path):
    db = _db(tmp_path)
    _register(db)
    settings = _settings(db)
    from app.ai.quota import mark_exhausted

    mark_exhausted(db, settings.get("gemini_model"))
    fake = FakeGeminiClient([], count_tokens_value=42)

    result = estimate(db, settings, SYMBOL, 20, client=fake)

    assert result["can_run"] is False
    assert result["reason"]
    assert "無料枠" in result["reason"]
    assert result["quota"]["exhausted"] is True
    # 例外にしない
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# 需給データを送らない不変条件（CLAUDE.md 不変条件1）
# ---------------------------------------------------------------------------
def test_analyze_source_does_not_reference_supply_identifiers():
    text = Path(analyze.__file__).read_text(encoding="utf-8")
    for token in ["short_", "margin_", "taisyaku", "karauri"]:
        assert token not in text, f"analyze.py に禁止識別子 {token!r} が含まれている"


# ---------------------------------------------------------------------------
# P11-6: AnalysisReport のフィールド定義順（SPEC §2.7.4・§2.9.9。「根拠→結論」を崩さない）
# ---------------------------------------------------------------------------
def test_analysis_report_field_order_is_evidence_before_conclusion():
    """`fundamental` は `technical` と `disclosure` の間、`data_scope_note` は最後（`summary` の後）。

    Pydantic のフィールド定義順がそのまま Gemini への出力順（response_schema）になるため、
    この順序は仕様そのもの（先に結論を出させると根拠が後付けになる）。
    """
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
