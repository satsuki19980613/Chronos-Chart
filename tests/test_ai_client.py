"""Gemini クライアント（`app/ai/client.py`）のテスト（SPEC §2.7.2, §2.7.4, §2.7.5）。

ネットワークには一切アクセスしない。`google.genai.Client` の代わりに、
`models.count_tokens` / `models.generate_content` だけを持つフェイクを `sdk_client=` に渡す。
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.ai import client as ai_client
from app.ai.client import (
    GeminiClient,
    QuotaExceeded,
    QuotaHit,
    Reply,
    Usage,
    parse_quota_error,
    retry_delay_seconds,
)
from app.errors import UserFacingError
from google.genai import errors as genai_errors


# ---------- テスト用の道具 ----------


class _FakeUsageMetadata:
    def __init__(self, prompt=0, candidates=0, thoughts=None, total=0):
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates
        self.thoughts_token_count = thoughts
        self.total_token_count = total


class _FakeFinishReason:
    """enum のように `.name` を持つフェイク。"""

    def __init__(self, name: str):
        self.name = name

    def __str__(self) -> str:
        return f"FinishReason.{self.name}"


class _FakeCandidate:
    def __init__(self, finish_reason=None):
        self.finish_reason = finish_reason


class _FakeResponse:
    def __init__(self, text=None, finish_reason=None, usage_metadata=None):
        self.text = text
        self.candidates = [_FakeCandidate(finish_reason)] if finish_reason is not None else []
        self.usage_metadata = usage_metadata


class _FakeCountTokensResult:
    def __init__(self, total_tokens: int):
        self.total_tokens = total_tokens


class _FakeModels:
    """`client.models` のフェイク。呼び出し回数と渡された config を記録する。"""

    def __init__(self, *, response=None, error=None, total_tokens=0):
        self._response = response
        self._error = error
        self.total_tokens = total_tokens
        self.generate_content_calls: list[dict] = []
        self.count_tokens_calls: list[dict] = []

    def count_tokens(self, *, model, contents):
        self.count_tokens_calls.append({"model": model, "contents": contents})
        return _FakeCountTokensResult(self.total_tokens)

    def generate_content(self, *, model, contents, config):
        self.generate_content_calls.append({"model": model, "contents": contents, "config": config})
        if self._error is not None:
            raise self._error
        return self._response


class _FakeSdkClient:
    def __init__(self, models: _FakeModels):
        self.models = models


class _FakeSettings:
    def __init__(self, values: dict, secrets: dict):
        self._values = values
        self._secrets = secrets

    def get(self, key):
        return self._values.get(key)

    def get_secret(self, key):
        return self._secrets.get(key, "")


def _make_client(models: _FakeModels, **overrides) -> GeminiClient:
    kwargs = dict(
        api_key="dummy",
        model="gemini-2.5-flash",
        max_output_tokens=1024,
        thinking_budget=256,
        sdk_client=_FakeSdkClient(models),
    )
    kwargs.update(overrides)
    return GeminiClient(**kwargs)


def _quota_error(*, code=429, details=None):
    return genai_errors.ClientError(code, details if details is not None else {})


class _Schema(BaseModel):
    value: str


# ---------- count_tokens ----------


def test_count_tokens_reads_total_tokens_attribute():
    """戻り値は `.total_tokens`（`.total_token_count` ではない）。"""
    models = _FakeModels(total_tokens=123)
    client = _make_client(models)
    assert client.count_tokens("プロンプト") == 123
    assert models.count_tokens_calls == [{"model": "gemini-2.5-flash", "contents": "プロンプト"}]


# ---------- generate: 正常系 ----------


def test_generate_builds_reply_and_usage_from_normal_response():
    response = _FakeResponse(
        text='{"value": "ok"}',
        finish_reason=_FakeFinishReason("STOP"),
        usage_metadata=_FakeUsageMetadata(prompt=44, candidates=85, thoughts=389, total=518),
    )
    models = _FakeModels(response=response)
    client = _make_client(models)

    reply = client.generate("プロンプト", schema=_Schema)

    assert isinstance(reply, Reply)
    assert reply.text == '{"value": "ok"}'
    assert reply.finish_reason == "STOP"
    assert reply.truncated is False
    assert reply.usage == Usage(
        prompt_tokens=44, output_tokens=85 + 389, thoughts_tokens=389, total_tokens=518
    )


def test_generate_treats_missing_thoughts_token_count_as_zero():
    """thinking を使わなかった応答では `thoughts_token_count` が None。"""
    response = _FakeResponse(
        text="{}",
        finish_reason=_FakeFinishReason("STOP"),
        usage_metadata=_FakeUsageMetadata(prompt=10, candidates=20, thoughts=None, total=30),
    )
    models = _FakeModels(response=response)
    client = _make_client(models)

    reply = client.generate("プロンプト")

    assert reply.usage.thoughts_tokens == 0
    assert reply.usage.output_tokens == 20


def test_generate_survives_missing_usage_metadata_and_none_text():
    """`usage_metadata` が None、`text` が None でも落ちない。"""
    response = _FakeResponse(text=None, finish_reason=_FakeFinishReason("STOP"), usage_metadata=None)
    models = _FakeModels(response=response)
    client = _make_client(models)

    reply = client.generate("プロンプト")

    assert reply.text == ""
    assert reply.usage == Usage(prompt_tokens=0, output_tokens=0, thoughts_tokens=0, total_tokens=0)


def test_generate_keeps_text_when_truncated_by_max_tokens():
    """MAX_TOKENS でも text は捨てずに保持し、`truncated` が True になること。"""
    response = _FakeResponse(
        text="途中まで",
        finish_reason=_FakeFinishReason("MAX_TOKENS"),
        usage_metadata=_FakeUsageMetadata(prompt=1, candidates=2, thoughts=0, total=3),
    )
    models = _FakeModels(response=response)
    client = _make_client(models)

    reply = client.generate("プロンプト")

    assert reply.truncated is True
    assert reply.text == "途中まで"


def test_generate_passes_config_correctly_with_schema():
    response = _FakeResponse(text="{}", finish_reason=_FakeFinishReason("STOP"))
    models = _FakeModels(response=response)
    client = _make_client(models, max_output_tokens=999, thinking_budget=77)

    client.generate("プロンプト", schema=_Schema)

    assert len(models.generate_content_calls) == 1
    config = models.generate_content_calls[0]["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_schema is _Schema
    assert config.temperature == ai_client.TEMPERATURE
    assert config.max_output_tokens == 999
    assert config.thinking_config.thinking_budget == 77


def test_generate_without_schema_omits_response_mime_type_and_schema():
    response = _FakeResponse(text="plain", finish_reason=_FakeFinishReason("STOP"))
    models = _FakeModels(response=response)
    client = _make_client(models)

    client.generate("プロンプト")

    config = models.generate_content_calls[0]["config"]
    assert config.response_mime_type is None
    assert config.response_schema is None


def test_ping_uses_small_output_and_no_thinking_and_no_schema():
    response = _FakeResponse(text="OK", finish_reason=_FakeFinishReason("STOP"))
    models = _FakeModels(response=response)
    client = _make_client(models)

    reply = client.ping()

    assert reply.text == "OK"
    config = models.generate_content_calls[0]["config"]
    assert config.response_mime_type is None
    assert config.thinking_config.thinking_budget == 0
    assert config.max_output_tokens < 1024


# ---------- 429 とクォータ ----------


def test_generate_does_not_retry_on_429():
    """429 を受けても `generate_content` の呼び出し回数は1回のまま（再送しない）。"""
    error = _quota_error(
        details={
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "message": "quota exceeded",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": "GenerateContentPerDayPerProjectPerModel"}],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"},
                ],
            }
        }
    )
    models = _FakeModels(error=error)
    client = _make_client(models)

    with pytest.raises(QuotaExceeded) as excinfo:
        client.generate("プロンプト")

    assert len(models.generate_content_calls) == 1
    hit = excinfo.value.hit
    assert hit.scope == "per_day"
    assert hit.retry_delay == 37.0
    assert "GenerateContentPerDayPerProjectPerModel" in hit.quota_ids
    assert "本日の無料枠" in hit.message


def test_quota_exceeded_is_user_facing_error():
    hit = QuotaHit(scope="per_day", retry_delay=None, quota_ids=(), message="msg")
    exc = QuotaExceeded(hit)
    assert isinstance(exc, UserFacingError)
    assert exc.hit is hit
    assert str(exc) == "msg"


# ---------- parse_quota_error の3分岐 ----------


def _details_with_quota_id(quota_id: str):
    return {
        "error": {
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [{"quotaId": quota_id}],
                }
            ]
        }
    }


def test_parse_quota_error_per_day():
    err = _quota_error(details=_details_with_quota_id("GenerateContentPerDayPerProjectPerModel"))
    hit = parse_quota_error(err)
    assert hit.scope == "per_day"


def test_parse_quota_error_per_minute():
    err = _quota_error(details=_details_with_quota_id("GenerateContentPerMinutePerProjectPerModel"))
    hit = parse_quota_error(err)
    assert hit.scope == "per_minute"


def test_parse_quota_error_case_insensitive():
    err = _quota_error(details=_details_with_quota_id("generatecontentperdayperprojectpermodel"))
    hit = parse_quota_error(err)
    assert hit.scope == "per_day"


def test_parse_quota_error_unknown_when_undeterminable():
    err = _quota_error(details=_details_with_quota_id("SomeOtherLimit"))
    hit = parse_quota_error(err)
    assert hit.scope == "unknown"


@pytest.mark.parametrize(
    "details",
    [
        None,
        "not a dict",
        {},  # error キーが無い
        {"error": {}},  # details が無い
        {"error": {"details": []}},  # QuotaFailure が無い
        {"error": {"details": [{"violations": [{"quotaId": "PerDay"}]}]}},  # @type が無い
        {"error": {"details": [{"@type": "QuotaFailure", "violations": []}]}},  # violations が空
        {"error": {"details": [{"@type": "QuotaFailure"}]}},  # violations キーすら無い
        {"error": {"details": "not a list"}},
    ],
)
def test_parse_quota_error_tolerates_missing_fields(details):
    err = _quota_error(details=details)
    hit = parse_quota_error(err)
    assert hit.scope in ("per_day", "per_minute", "unknown")
    assert hit.scope == "unknown"
    assert hit.quota_ids == ()
    assert hit.retry_delay is None
    assert hit.message  # 何かしらのメッセージは必ずある


def test_parse_quota_error_reads_retry_delay_from_retry_info():
    err = _quota_error(
        details={
            "error": {
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": "PerMinute"}],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "1.5s"},
                ]
            }
        }
    )
    hit = parse_quota_error(err)
    assert hit.retry_delay == 1.5


# ---------- retry_delay_seconds の単体テスト ----------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("37s", 37.0),
        ("1.5s", 1.5),
        (37, 37.0),
        (37.5, 37.5),
        (None, None),
        ("abc", None),
        ("", None),
        (True, None),  # bool は int のサブクラスなので明示的に除外する
        ({}, None),
        ([], None),
    ],
)
def test_retry_delay_seconds(value, expected):
    assert retry_delay_seconds(value) == expected


# ---------- from_settings ----------


def test_from_settings_raises_when_api_key_missing():
    settings = _FakeSettings(
        values={"gemini_model": "gemini-2.5-flash"},
        secrets={},
    )
    with pytest.raises(UserFacingError, match="API キーが設定されていません"):
        GeminiClient.from_settings(settings)


def test_from_settings_raises_when_model_missing():
    settings = _FakeSettings(
        values={"gemini_model": ""},
        secrets={"gemini_api_key": "sk-dummy"},
    )
    with pytest.raises(UserFacingError, match="モデル名が設定されていません"):
        GeminiClient.from_settings(settings)


def test_from_settings_builds_client_with_settings_values():
    settings = _FakeSettings(
        values={
            "gemini_model": "gemini-2.5-flash",
            "gemini_max_output_tokens": 4096,
            "gemini_thinking_budget": 512,
        },
        secrets={"gemini_api_key": "sk-dummy"},
    )
    fake_sdk = _FakeSdkClient(_FakeModels(total_tokens=1))
    client = GeminiClient.from_settings(settings, sdk_client=fake_sdk)
    assert client.model == "gemini-2.5-flash"
    assert client._max_output_tokens == 4096
    assert client._thinking_budget == 512


# ---------- エラー変換（400/401/403/404/5xx） ----------


@pytest.mark.parametrize("code", [400, 401, 403])
def test_generate_translates_4xx_auth_errors_without_leaking_key(code):
    # err.message は実際の Gemini API では鍵の値そのものを含まない（"API key not valid" 等の
    # 定型文）。このクライアントは api_key を __init__ 後どこにも保持しないので、
    # 実際に設定したキーが例外メッセージに紛れ込まないことを確認する。
    error = genai_errors.ClientError(code, {"error": {"code": code, "message": "API key not valid"}})
    models = _FakeModels(error=error)
    client = _make_client(models, api_key="sk-SUPERSECRET")

    with pytest.raises(UserFacingError) as excinfo:
        client.generate("プロンプト")

    message = str(excinfo.value)
    assert "sk-SUPERSECRET" not in message
    assert str(code) in message


def test_generate_translates_404_as_model_not_found():
    error = genai_errors.ClientError(404, {"error": {"code": 404, "message": "model not found"}})
    models = _FakeModels(error=error)
    client = _make_client(models)

    with pytest.raises(UserFacingError, match="見つかりません"):
        client.generate("プロンプト")


def test_generate_translates_server_error_5xx():
    error = genai_errors.ServerError(500, {"error": {"code": 500, "message": "internal error"}})
    models = _FakeModels(error=error)
    client = _make_client(models)

    with pytest.raises(UserFacingError, match="HTTP 500"):
        client.generate("プロンプト")


def test_generate_translates_generic_communication_error_as_timeout():
    class _TimeoutLike(Exception):
        pass

    models = _FakeModels(error=_TimeoutLike("connection timed out"))
    client = _make_client(models)

    with pytest.raises(UserFacingError, match="タイムアウト"):
        client.generate("プロンプト")


def test_error_messages_never_contain_api_key_across_all_codes():
    api_key = "sk-VERY-SECRET-KEY"
    for code in (400, 401, 403, 404, 429, 500, 503):
        if code == 429:
            details = {"error": {"code": code, "details": []}}
        else:
            details = {"error": {"code": code, "message": "something went wrong"}}
        error_cls = genai_errors.ServerError if code >= 500 else genai_errors.ClientError
        error = error_cls(code, details)
        models = _FakeModels(error=error)
        client = _make_client(models, api_key=api_key)
        with pytest.raises(UserFacingError) as excinfo:
            client.generate("プロンプト")
        assert api_key not in str(excinfo.value)


# ---------- 不変条件: retry_options を設定しない ----------


def test_source_never_assigns_retry_options():
    """`google-genai` の `retry_options` を設定しない（CLAUDE.md の不変条件）。

    禁じているのは「設定すること」なので、理由を説明する散文まで縛らないよう代入だけを見る。
    有効にすると SDK が 429 のたびに黙って再送し、`ai_usage`（送信のたびに加算）と実数がずれる。
    """
    source = Path(ai_client.__file__).read_text(encoding="utf-8")
    assert re.search(r"retry_options\s*=", source) is None


def test_source_does_not_mention_short_selling_or_margin_identifiers():
    """このモジュールは需給データに一切関わらない（CLAUDE.md の不変条件 / SPEC §2.7.3）。"""
    source = Path(ai_client.__file__).read_text(encoding="utf-8")
    for forbidden in ("short_", "margin_", "taisyaku", "karauri"):
        assert forbidden not in source
