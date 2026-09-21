"""Gemini クライアント（SPEC §2.7.2, §2.7.4, §2.7.5）。

このモジュールの責務は「1回の Gemini 呼び出し」だけに限定する。

- クォータの加算・送信前ガード・分次リトライ・再依頼のループは呼び出し側（P6-2/P6-4）の仕事。
  ここでは 429 を `QuotaExceeded` に変換して投げ返すところまでしかやらない
- `HttpOptions.retry_options`（SDK 内蔵の自動再送）は**絶対に設定しない**。SDK の既定は
  「再送しない」で、アプリ側のカウンタ（送信するたびに `ai_usage.requests` を加算する）は
  その前提に立っている。有効にすると 429 のたびに SDK が黙って最大5回まで再送し、
  カウンタと実際の送信回数がずれる
- 需給データ（空売り・貸借）にはこのモジュールから一切関与しない。プロンプト組み立ては
  `app/ai/prompt.py` の担当で、このクライアントは文字列を渡されて文字列を返すだけ
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel

from ..errors import UserFacingError

log = logging.getLogger(__name__)

TEMPERATURE = 0.1  # SPEC §2.7.4
REQUEST_TIMEOUT_MS = 180_000  # 分析は長考するので3分（`HttpOptions.timeout` の単位はミリ秒）

_PING_PROMPT = "「OK」とだけ答えてください。"
_PING_MAX_OUTPUT_TOKENS = 16

_RETRY_DELAY_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*s\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class Usage:
    """1回の応答のトークン使用量。"""

    prompt_tokens: int
    output_tokens: int  # candidates + thoughts。無料枠の出力側を消費するのはこちら
    thoughts_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class Reply:
    """Gemini からの応答。"""

    text: str
    finish_reason: str  # enum の name を文字列にしたもの。取れなければ ""
    usage: Usage

    @property
    def truncated(self) -> bool:
        """出力トークン上限に達して打ち切られたか（SPEC §2.7.5 の1番）。"""
        return self.finish_reason == "MAX_TOKENS"


@dataclass(frozen=True)
class QuotaHit:
    """429 応答から読み取ったクォータ超過の内容（SPEC §2.7.2 の3分岐）。"""

    scope: str  # "per_day" | "per_minute" | "unknown"
    retry_delay: float | None  # RetryInfo.retryDelay を秒に直したもの
    quota_ids: tuple[str, ...]
    message: str  # 画面にそのまま出す日本語


class QuotaExceeded(UserFacingError):
    """Gemini からクォータ超過（429 / RESOURCE_EXHAUSTED）を受けた。"""

    def __init__(self, hit: QuotaHit) -> None:
        super().__init__(hit.message)
        self.hit = hit


def retry_delay_seconds(value: Any) -> float | None:
    """`RetryInfo.retryDelay` を秒に直す。

    公式に保証された書式ではないため、`"37s"` `"1.5s"` のような protobuf Duration の
    文字列表現も、素の数値も、想定外の値もすべて受け付け、読み取れなければ None を返す
    （例外を投げない）。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = _RETRY_DELAY_RE.match(value)
        if m:
            return float(m.group(1))
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _quota_ids_from_details(details: Any) -> tuple[str, ...]:
    """`google.rpc.QuotaFailure` の `violations[].quotaId` を集める。欠損には空タプルで応える。"""
    if not isinstance(details, dict):
        return ()
    error = details.get("error")
    if not isinstance(error, dict):
        return ()
    sub_details = error.get("details")
    if not isinstance(sub_details, list):
        return ()
    ids: list[str] = []
    for entry in sub_details:
        if not isinstance(entry, dict):
            continue
        if "QuotaFailure" not in str(entry.get("@type", "")):
            continue
        violations = entry.get("violations")
        if not isinstance(violations, list):
            continue
        for v in violations:
            if isinstance(v, dict) and v.get("quotaId"):
                ids.append(str(v["quotaId"]))
    return tuple(ids)


def _retry_delay_from_details(details: Any) -> float | None:
    """`google.rpc.RetryInfo` の `retryDelay` を探す。欠損には None で応える。"""
    if not isinstance(details, dict):
        return None
    error = details.get("error")
    if not isinstance(error, dict):
        return None
    sub_details = error.get("details")
    if not isinstance(sub_details, list):
        return None
    for entry in sub_details:
        if not isinstance(entry, dict):
            continue
        if "RetryInfo" not in str(entry.get("@type", "")):
            continue
        return retry_delay_seconds(entry.get("retryDelay"))
    return None


def parse_quota_error(err: genai_errors.ClientError) -> QuotaHit:
    """429 の詳細から `QuotaHit` を組み立てる（SPEC §2.7.2 の3分岐）。

    `err.details` は本来レスポンス JSON（`{"error": {...}}`）が入るはずだが、
    実物の 429 は未採取のため、`details` が dict でない・`error` が無い・`details` リストが無い・
    `@type` が無い・`retryDelay` の書式が違う、といった欠損のすべてに耐える。
    """
    details = getattr(err, "details", None)
    quota_ids = _quota_ids_from_details(details)
    retry_delay = _retry_delay_from_details(details)

    lowered = [qid.lower() for qid in quota_ids]
    if any("perday" in qid for qid in lowered):
        scope = "per_day"
    elif any("perminute" in qid for qid in lowered):
        scope = "per_minute"
    else:
        scope = "unknown"  # 判別できない場合は安全側に倒して打ち切る

    if scope == "per_day":
        message = (
            "本日の無料枠を使い切りました。"
            "太平洋時間0時にリセットされます（日本時間の当日16時または17時）"
        )
    elif scope == "per_minute":
        message = "1分あたりの無料枠を使い切りました。しばらく待って再試行します"
    else:
        message = "Gemini の利用上限に達しました（種別を判別できないため送信を打ち切ります）"

    return QuotaHit(scope=scope, retry_delay=retry_delay, quota_ids=quota_ids, message=message)


def _usage_from_metadata(usage_metadata: Any) -> Usage:
    """`response.usage_metadata` から `Usage` を組み立てる。丸ごと None でも 0 で応える。"""
    if usage_metadata is None:
        return Usage(prompt_tokens=0, output_tokens=0, thoughts_tokens=0, total_tokens=0)
    prompt = getattr(usage_metadata, "prompt_token_count", None) or 0
    candidates = getattr(usage_metadata, "candidates_token_count", None) or 0
    thoughts = getattr(usage_metadata, "thoughts_token_count", None) or 0
    total = getattr(usage_metadata, "total_token_count", None) or 0
    return Usage(
        prompt_tokens=prompt,
        output_tokens=candidates + thoughts,
        thoughts_tokens=thoughts,
        total_tokens=total,
    )


def _finish_reason_name(response: Any) -> str:
    """`candidates[0].finish_reason` を文字列にする。取れなければ空文字。"""
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return ""
    fr = getattr(candidates[0], "finish_reason", None)
    if fr is None:
        return ""
    return getattr(fr, "name", str(fr))


def _reply_from_response(response: Any) -> Reply:
    text = getattr(response, "text", None) or ""
    return Reply(
        text=text,
        finish_reason=_finish_reason_name(response),
        usage=_usage_from_metadata(getattr(response, "usage_metadata", None)),
    )


def _connection_error_message(err: Exception) -> str:
    """SDK 例外以外（通信層）の失敗を画面向けの文言にする。"""
    if "timeout" in type(err).__name__.lower():
        return (
            f"Gemini への送信がタイムアウトしました（{REQUEST_TIMEOUT_MS // 1000}秒）。"
            "出力トークン上限や thinking の予算を下げるか、時間を置いて再試行してください"
        )
    return "Gemini に接続できませんでした。ネットワークの状態を確認してください"


class GeminiClient:
    """Gemini API への薄いラッパー。1回の呼び出しにつき1回だけ送信する。"""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        max_output_tokens: int,
        thinking_budget: int,
        sdk_client: Any = None,
        temperature: float = TEMPERATURE,
    ) -> None:
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._thinking_budget = thinking_budget
        self._temperature = temperature
        if sdk_client is not None:
            self._client = sdk_client
        else:
            # HttpOptions に retry_options を渡さない（上の docstring を参照）。
            # SDK の既定は「再送しない」で、自前のクォータカウンタはその前提に立っている
            self._client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
            )

    @classmethod
    def from_settings(cls, settings: Any, *, sdk_client: Any = None) -> "GeminiClient":
        """設定から組み立てる。キー・モデル未設定はここで `UserFacingError` にする。"""
        api_key = settings.get_secret("gemini_api_key")
        if not api_key:
            raise UserFacingError("Gemini の API キーが設定されていません。設定タブで入力してください")
        model = settings.get("gemini_model")
        if not model:
            raise UserFacingError("Gemini のモデル名が設定されていません。設定タブで入力してください")
        return cls(
            api_key=api_key,
            model=model,
            max_output_tokens=settings.get("gemini_max_output_tokens"),
            thinking_budget=settings.get("gemini_thinking_budget"),
            sdk_client=sdk_client,
        )

    @property
    def model(self) -> str:
        return self._model

    def count_tokens(self, prompt: str) -> int:
        """送信前の入力トークン見積り（SPEC §2.7.1）。"""
        try:
            result = self._client.models.count_tokens(model=self._model, contents=prompt)
        except genai_errors.ClientError as err:
            raise self._translate_client_error(err) from err
        except genai_errors.APIError as err:
            code = getattr(err, "code", None)
            raise UserFacingError(f"Gemini への送信に失敗しました（HTTP {code}）") from err
        except Exception as err:
            raise UserFacingError(_connection_error_message(err)) from err
        # 実測では `.total_tokens`（`total_token_count` ではない。SPEC §2.7.2a）
        return int(getattr(result, "total_tokens", None) or 0)

    def generate(self, prompt: str, schema: type[BaseModel] | None = None) -> Reply:
        """Gemini に1回だけ送信する。再送・再依頼のループは呼び出し側の責務。"""
        config_kwargs: dict[str, Any] = {
            "temperature": self._temperature,
            "max_output_tokens": self._max_output_tokens,
            "thinking_config": types.ThinkingConfig(thinking_budget=self._thinking_budget),
        }
        if schema is not None:
            config_kwargs["response_mime_type"] = "application/json"
            config_kwargs["response_schema"] = schema
        config = types.GenerateContentConfig(**config_kwargs)

        return self._send(prompt, config)

    def ping(self) -> Reply:
        """接続テスト（P6-6）用の最小プロンプト。スキーマなし・出力上限は小さく・thinking なし。"""
        config = types.GenerateContentConfig(
            temperature=self._temperature,
            max_output_tokens=_PING_MAX_OUTPUT_TOKENS,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        return self._send(_PING_PROMPT, config)

    def _send(self, prompt: str, config: types.GenerateContentConfig) -> Reply:
        """送信の共通部分。**1回の呼び出しにつき `generate_content` は1回だけ**。"""
        try:
            response = self._client.models.generate_content(
                model=self._model, contents=prompt, config=config
            )
        except genai_errors.ClientError as err:
            raise self._translate_client_error(err) from err
        except genai_errors.APIError as err:
            code = getattr(err, "code", None)
            raise UserFacingError(f"Gemini への送信に失敗しました（HTTP {code}）") from err
        except Exception as err:  # httpx のタイムアウトや名前解決の失敗など
            raise UserFacingError(_connection_error_message(err)) from err
        return _reply_from_response(response)

    def _translate_client_error(self, err: genai_errors.ClientError) -> UserFacingError:
        """`ClientError` を画面向けのメッセージに変換する。API キーは出さない。"""
        code = getattr(err, "code", None)
        if code == 429:
            return QuotaExceeded(parse_quota_error(err))
        if code in (400, 401, 403):
            detail = getattr(err, "message", None) or ""
            base = f"Gemini の API キーが正しくないか、モデル名（{self._model}）が使えません（HTTP {code}）"
            return UserFacingError(f"{base}: {detail}" if detail else base)
        if code == 404:
            return UserFacingError(
                f"Gemini のモデル名（{self._model}）が見つかりません（HTTP 404）。"
                "設定タブでモデル名を確認してください"
            )
        return UserFacingError(f"Gemini への送信に失敗しました（HTTP {code}）")
