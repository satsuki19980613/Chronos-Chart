"""app/api.py の AI 分析関連メソッド（P6-6）と `ai_analyze` ジョブのテスト。

ネットワークは使わない。Gemini の呼び出しはフェイクの SDK クライアントで差し替える。
実際にブラウザやフォルダを開くこともしない（`webbrowser.open` を monkeypatch する）。
"""

from __future__ import annotations

import json
import threading

import pytest

from app import config
from app.ai import analyze, quota, report
from app.ai.client import GeminiClient
from app.ai.prompt import DisclosureItem, PromptInput
from app.ai.schema import AnalysisReport, SectionAnalysis
from app.api import Api
from app.database import Database
from app.service import StockService
from app.settings import Settings


def assert_ok(result):
    assert result["ok"] is True, result
    json.dumps(result, allow_nan=False)
    return result["data"]


def assert_error(result):
    assert result["ok"] is False
    assert isinstance(result["error"], str) and result["error"] != ""
    return result["error"]


class FakeKeyring:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_password(self, service, key):
        return self.values.get(key)

    def set_password(self, service, key, value):
        self.values[key] = value

    def delete_password(self, service, key):
        self.values.pop(key, None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    db = Database(tmp_path / "test.db")
    db.init_schema()
    db.upsert_stock("1234.T", "1234", "テスト株式会社", "東証", "JPY")
    service = StockService(db, fetcher=None, csv_dir=tmp_path / "csv", output_dir=tmp_path / "output")
    settings = Settings(db, keyring_backend=FakeKeyring({"gemini_api_key": "KEY"}))
    api = Api(service, settings=settings)
    return api, db, settings


def _configure(settings, **overrides):
    values = {"gemini_model": "gemini-test", "gemini_rpm": 10, "gemini_tpm": 100000, "gemini_rpd": 50}
    values.update(overrides)
    settings.update(values)


# ---------------------------------------------------------------------------
# ai_quota / ai_reset_exhausted
# ---------------------------------------------------------------------------
def test_ai_quota_reports_not_configured_by_default(env):
    api, _db, _settings = env
    data = assert_ok(api.ai_quota())
    assert data["configured"] is False
    assert data["note"]


def test_ai_quota_reports_usage_after_requests(env):
    api, db, settings = env
    _configure(settings)
    quota.record_request(db, "gemini-test")
    quota.record_tokens(db, "gemini-test", 100, 20)
    data = assert_ok(api.ai_quota())
    assert data["configured"] is True
    assert data["used"]["requests"] == 1
    assert data["remaining"]["rpd"] == 49
    assert data["reset_at"]


def test_ai_reset_exhausted_clears_the_flag(env):
    api, db, settings = env
    _configure(settings)
    quota.mark_exhausted(db, "gemini-test")
    assert assert_ok(api.ai_quota())["exhausted"] is True
    assert assert_ok(api.ai_reset_exhausted())["cleared"] == 1
    assert assert_ok(api.ai_quota())["exhausted"] is False
    # 2回目は解除するものが無い
    assert assert_ok(api.ai_reset_exhausted())["cleared"] == 0


# ---------------------------------------------------------------------------
# ai_estimate
# ---------------------------------------------------------------------------
def _seed_prices(db, prices_factory, symbol="1234.T", n=140):
    prices = prices_factory([100 + i for i in range(n)])
    db.upsert_prices(symbol, prices)
    from app import indicators as ind

    db.replace_indicators(symbol, ind.compute_indicators(prices))


class _FakeClient:
    """`GeminiClient` の代わり。送信はせずトークン数と応答を返す。"""

    def __init__(self, text="", tokens=1234, truncated=False):
        self.model = "gemini-test"
        self._text = text
        self._tokens = tokens
        self._truncated = truncated
        self.generate_calls = 0

    def count_tokens(self, prompt):
        return self._tokens

    def generate(self, prompt, schema=None):
        from app.ai.client import Reply, Usage

        self.generate_calls += 1
        return Reply(
            text=self._text,
            finish_reason="MAX_TOKENS" if self._truncated else "STOP",
            usage=Usage(prompt_tokens=self._tokens, output_tokens=200, thoughts_tokens=50, total_tokens=self._tokens + 200),
        )


def test_ai_estimate_returns_reason_instead_of_raising_when_not_configured(env, prices_factory):
    api, db, _settings = env
    _seed_prices(db, prices_factory)
    data = assert_ok(api.ai_estimate("1234.T", 60))
    assert data["can_run"] is False
    assert data["reason"]
    assert data["quota"]["configured"] is False


def test_ai_estimate_reports_input_tokens(env, prices_factory, monkeypatch):
    api, db, settings = env
    _configure(settings)
    _seed_prices(db, prices_factory)
    monkeypatch.setattr(GeminiClient, "from_settings", classmethod(lambda cls, s, **kw: _FakeClient(tokens=4321)))
    data = assert_ok(api.ai_estimate("1234.T", 60))
    assert data["can_run"] is True
    assert data["input_tokens"] == 4321
    assert data["days"] == 60
    assert "銘柄情報" in data["sends"]


def test_ai_estimate_rejects_unknown_period(env, prices_factory):
    api, db, settings = env
    _configure(settings)
    _seed_prices(db, prices_factory)
    assert_error(api.ai_estimate("1234.T", 45))


# ---------------------------------------------------------------------------
# list_reports / open_report
# ---------------------------------------------------------------------------
def _save_dummy_report(db, tmp_dir, symbol="1234.T"):
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return report.save_report(db, tmp_dir, symbol, "<html></html>", model="gemini-test", in_tokens=10, out_tokens=5)


def test_list_and_open_report(env, tmp_path, monkeypatch):
    api, db, _settings = env
    saved = _save_dummy_report(db, tmp_path / "reports")
    rows = assert_ok(api.list_reports())
    assert len(rows) == 1 and rows[0]["exists"] is True

    opened = []
    monkeypatch.setattr("app.api.webbrowser.open", lambda url: opened.append(url))
    assert_ok(api.open_report(saved["id"]))
    assert opened and opened[0].startswith("file:")


def test_open_report_errors_when_the_file_is_gone(env, tmp_path, monkeypatch):
    api, db, _settings = env
    saved = _save_dummy_report(db, tmp_path / "reports")
    (tmp_path / "reports").joinpath(*[]).mkdir(parents=True, exist_ok=True)
    import os

    os.remove(saved["path"])
    monkeypatch.setattr("app.api.webbrowser.open", lambda url: pytest.fail("開いてはいけない"))
    assert "見つかりません" in assert_error(api.open_report(saved["id"]))
    assert assert_ok(api.list_reports())[0]["exists"] is False


# ---------------------------------------------------------------------------
# test_connection("gemini")
# ---------------------------------------------------------------------------
def test_gemini_connection_test_counts_the_request(env, monkeypatch):
    """接続テストも実際の送信1回。上限未設定でも動くが、必ず ai_usage に加算する。"""
    api, db, _settings = env

    class _PingClient(_FakeClient):
        def ping(self):
            return self.generate("ping")

    monkeypatch.setattr(GeminiClient, "from_settings", classmethod(lambda cls, s, **kw: _PingClient()))
    data = assert_ok(api.test_connection("gemini"))
    assert "接続できました" in data["message"]
    assert quota.usage(db, "gemini-test")["requests"] == 1
    assert quota.usage(db, "gemini-test")["in_tokens"] == 1234


def test_gemini_connection_test_without_a_key_is_an_error(tmp_path):
    db = Database(tmp_path / "nokey.db")
    db.init_schema()
    service = StockService(db, fetcher=None, csv_dir=tmp_path / "csv", output_dir=tmp_path / "output")
    settings = Settings(db, keyring_backend=FakeKeyring())
    api = Api(service, settings=settings)
    assert "API キー" in assert_error(api.test_connection("gemini"))


def test_unknown_connection_target_is_an_error(env):
    api, _db, _settings = env
    assert_error(api.test_connection("nowhere"))


# ---------------------------------------------------------------------------
# ai_analyze ジョブ
# ---------------------------------------------------------------------------
_VALID_JSON = AnalysisReport(
    technical=SectionAnalysis(evidence=["25日線の上"], assessment="強い"),
    disclosure=SectionAnalysis(evidence=["臨時報告書1件"], assessment="平常"),
    risks=["r"],
    watch_points=["w"],
    verdict="bullish",
    confidence="medium",
    summary="まとめ",
).model_dump_json()


class _Ctx:
    def __init__(self):
        self.cancel = threading.Event()
        self.labels = []

    def progress(self, current, total, label=""):
        self.labels.append(label)

    def check(self):
        pass

    def wait(self, seconds):
        pass


def test_ai_analyze_job_saves_a_report(env, prices_factory, monkeypatch):
    _api, db, settings = env
    _configure(settings)
    _seed_prices(db, prices_factory)
    monkeypatch.setattr(
        GeminiClient, "from_settings", classmethod(lambda cls, s, **kw: _FakeClient(text=_VALID_JSON))
    )
    job = analyze.ai_analyze_job(db, settings)
    ctx = _Ctx()
    result = job(ctx, {"symbol": "1234.T", "days": 60})

    assert result["symbol"] == "1234.T"
    assert result["model"] == "gemini-test"
    assert "レポートを作成しました" in result["summary"]
    from pathlib import Path

    html = Path(result["path"]).read_text(encoding="utf-8")
    assert "まとめ" in html and "投資助言" in html
    assert quota.usage(db, "gemini-test")["requests"] == 1
    assert "送信中" in ctx.labels and "レポート生成中" in ctx.labels
    assert report.list_reports(db)[0]["id"] == result["id"]


def test_ai_analyze_job_rejects_missing_parameters(env):
    _api, db, settings = env
    job = analyze.ai_analyze_job(db, settings)
    with pytest.raises(Exception):
        job(_Ctx(), {})
    with pytest.raises(Exception):
        job(_Ctx(), {"symbol": "1234.T", "days": "まいにち"})


def test_report_of_a_real_shaped_prompt_input_has_no_supply_demand_words(env):
    """レポートに需給が載らないこと（CLAUDE.md 不変条件1）を API 層でも押さえる。"""
    data = PromptInput(
        symbol="1234.T",
        name="テスト株式会社",
        exchange="東証",
        currency="JPY",
        days=60,
        price_csv="date,open,high,low,close,volume\n2026-01-05,1,1,1,1,1\n",
        indicator_csv="date,sma_5\n2026-01-05,1\n",
        latest=(),
        signals=(),
        disclosures=(
            DisclosureItem(
                submit_at="2026-01-05T09:00",
                label="臨報",
                description="臨時報告書",
                reason="第19条第2項第9号の2",
                role="filer",
                withdrawn=False,
            ),
        ),
        generated_at="2026-01-05 10:00:00",
    )
    html = report.render_report(data, AnalysisReport.model_validate_json(_VALID_JSON), model="gemini-test")
    for word in ("空売り", "貸借", "信用残", "需給"):
        assert word not in html
