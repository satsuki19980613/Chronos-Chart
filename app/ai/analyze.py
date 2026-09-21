"""AI 分析の実行フロー（SPEC §2.7.1・§2.7.2・§2.7.5）。

`app/ai/client.py` は「1回の呼び出しにつき送信は1回だけ」という不変条件を持つ薄いラッパーに
留めてある。再送・再依頼のループをそこに同居させるとその保証が崩れるため、
**プロンプト組み立て → クォータ判定 → 送信 → 検証 → 再依頼という指揮だけ**をこのモジュールに
切り出す（メインの判断）。ここでは HTTP には一切触らず、`GeminiClient` / `Quota` / `PromptSource` を
呼び出すだけにとどめる。

このモジュールは需給データ（空売り残高・貸借取引残高）に一切関わらない。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from .. import config
from ..errors import Cancelled, UserFacingError
from .client import GeminiClient, QuotaExceeded, Reply
from .prompt import PromptInput, PromptSource, build_prompt, build_retry_prompt
from . import report
from .quota import Quota, apply_quota_hit
from .schema import AnalysisReport

log = logging.getLogger(__name__)

# SPEC §2.7.5 の4番。初回 + 再依頼2回 = 最大3回送信（スキーマ不一致による再依頼の上限）
MAX_RETRIES = 2

# 429（分次）の再送1回で待つ既定秒数（`retry_delay` が取れなかったとき）
_DEFAULT_MINUTE_RETRY_DELAY = 60.0

# 再依頼プロンプトに載せるエラー本文の上限文字数（入力を圧迫しないため）
_ERROR_BODY_LIMIT = 2000

_MAX_TOKENS_MESSAGE = (
    "出力が途中で打ち切られました（出力トークン上限）。"
    "設定タブで出力トークン上限を上げるか、thinking の予算を下げてください"
)

# SPEC §2.7.3: 実行前の確認ダイアログに出す「送信されるデータの種別」の一覧
_SEND_KINDS = ("銘柄情報", "株価とテクニカル指標", "EDINET の開示（本文は含まない）")

ProgressFn = Callable[[int, int, str], None]
SleepFn = Callable[[float], None]


@dataclass(frozen=True)
class AnalysisResult:
    """`run_analysis` の戻り値。レポート生成（P6-5）が指標値の表・開示一覧に使う。"""

    report: AnalysisReport
    data: PromptInput
    model: str
    in_tokens: int  # 実際に消費した入力トークンの合計（全試行の合計）
    out_tokens: int  # 同上（candidates + thoughts）
    attempts: int  # 実際に送信した回数


def _notify(progress: ProgressFn | None, current: int, total: int, label: str) -> None:
    if progress is not None:
        progress(current, total, label)


def _check_cancel(cancel: Any) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def _truncate_error(text: str) -> str:
    return text[:_ERROR_BODY_LIMIT]


def _sleep_with_cancel(delay: float, sleep_fn: SleepFn, cancel: Any) -> None:
    """`delay` 秒だけ待つ。待つ前後で cancel を確認する。"""
    _check_cancel(cancel)
    sleep_fn(delay)
    _check_cancel(cancel)


def _sanitize_for_filename(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", text)


def _save_raw_response(symbol: str, text: str, *, now: datetime | None = None) -> Path | None:
    """生のレスポンスを `config.LOG_DIR` に保存する（プロンプトは保存しない）。

    保存に失敗しても None を返すだけで例外は投げない（呼び出し側が元のエラーを優先するため）。
    """
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
        base = f"ai_response_{_sanitize_for_filename(symbol)}_{timestamp}"
        path = config.LOG_DIR / f"{base}.txt"
        suffix = 1
        while path.exists():
            path = config.LOG_DIR / f"{base}_{suffix}.txt"
            suffix += 1
        path.write_text(text, encoding="utf-8")
        return path
    except OSError:
        log.warning("AI 応答の生ログの保存に失敗しました", exc_info=True)
        return None


def estimate(
    db: Any,
    settings: Any,
    symbol: str,
    days: int,
    *,
    client: GeminiClient | None = None,
    now: datetime | None = None,
) -> dict:
    """実行前の確認ダイアログ用の見積り（SPEC §2.7.1）。

    数える順序は「プロンプト組み立て → count_tokens → Quota.check で可否判定」。
    `count_tokens` は Gemini への送信1回になるので、Quota.check を通す前に数えない
    （通しても弾かれてしまうと見積り自体ができなくなるため）。

    `can_run` が False のときは `reason` に理由を入れて**例外にしない**（ダイアログにそのまま出すため）。
    API キー・モデル未設定など `GeminiClient.from_settings` が失敗するケースも同様に扱う。
    """
    data = PromptSource(db).load(symbol, days, now=now)
    prompt = build_prompt(data)
    quota = Quota(db, settings)

    model = quota.model
    input_tokens = 0
    can_run = True
    reason: str | None = None
    try:
        if client is None:
            client = GeminiClient.from_settings(settings)
        model = client.model
        input_tokens = client.count_tokens(prompt)
        quota.check(input_tokens, now=now)
    except UserFacingError as exc:
        can_run = False
        reason = str(exc)

    return {
        "symbol": data.symbol,
        "name": data.name,
        "days": data.days,
        "model": model,
        "input_tokens": input_tokens,
        "quota": quota.snapshot(now=now),
        "can_run": can_run,
        "reason": reason,
        "sends": list(_SEND_KINDS),
    }


def _send_once(
    db: Any,
    client: GeminiClient,
    quota: Quota,
    prompt: str,
    *,
    sleep: SleepFn | None,
    cancel: Any,
    now: datetime | None,
    counters: dict,
) -> Reply:
    """1回分の「見積り→クォータ判定→送信→計上」を行う。

    429（分次）だけは、このレベルで `retry_delay` 待って1回だけ同じ内容を再送する
    （スキーマ不一致の再依頼とは別枠。§2.7.2 の判定表）。`counters["attempts"]` は
    実際に `generate` を呼んだ回数（この内部の再送も含む）を数える。
    """
    sleep_fn = sleep or time.sleep
    minute_retry_used = False
    while True:
        _check_cancel(cancel)
        need_tokens = client.count_tokens(prompt)
        quota.start_request(need_tokens, now=now, sleep=sleep, cancel=cancel)
        counters["attempts"] += 1
        try:
            reply = client.generate(prompt, schema=AnalysisReport)
        except QuotaExceeded as exc:
            hit = exc.hit
            apply_quota_hit(db, quota.model, hit.scope, now=now)
            if hit.scope == "per_minute" and not minute_retry_used:
                minute_retry_used = True
                delay = hit.retry_delay if hit.retry_delay else _DEFAULT_MINUTE_RETRY_DELAY
                _sleep_with_cancel(delay, sleep_fn, cancel)
                continue
            raise
        else:
            quota.finish_request(reply.usage.prompt_tokens, reply.usage.output_tokens, now=now)
            counters["in_tokens"] += reply.usage.prompt_tokens
            counters["out_tokens"] += reply.usage.output_tokens
            return reply


def run_analysis(
    db: Any,
    settings: Any,
    symbol: str,
    days: int,
    *,
    client: GeminiClient | None = None,
    quota: Quota | None = None,
    progress: ProgressFn | None = None,
    cancel: Any = None,
    sleep: SleepFn | None = None,
    now: datetime | None = None,
) -> AnalysisResult:
    """AI 分析を実行する（SPEC §2.7.5）。

    1. `PromptSource(db).load(symbol, days)` → `build_prompt`
    2. 最大 `1 + MAX_RETRIES` 回のループで送信・検証する。`finish_reason == MAX_TOKENS` は
       パースを試みずに中止する。スキーマ不一致は検証エラー本文を添えて再依頼する
    3. 3回目も失敗したら、生のレスポンスを `data/logs/` に保存して `UserFacingError` を投げる
    """
    if client is None:
        client = GeminiClient.from_settings(settings)
    if quota is None:
        quota = Quota(db, settings)

    data = PromptSource(db).load(symbol, days, now=now)
    prompt = build_prompt(data)

    total = 1 + MAX_RETRIES
    counters = {"in_tokens": 0, "out_tokens": 0, "attempts": 0}
    last_raw_text = ""
    last_error: str | None = None

    for attempt_no in range(1, total + 1):
        _check_cancel(cancel)
        _notify(progress, attempt_no, total, "送信中")

        try:
            reply = _send_once(db, client, quota, prompt, sleep=sleep, cancel=cancel, now=now, counters=counters)
        except UserFacingError:
            # 再依頼の直前ガード（§2.7.2）で止まった場合も「中止」の一種（SPEC §2.7.5 の4番）。
            # 元のメッセージ（クォータ超過などの案内）はそのまま活かしつつ、それまでに得ていた
            # 生レスポンスがあれば保存しておく（保存の成否に関わらず元のエラーを優先する）
            if last_raw_text:
                _save_raw_response(symbol, last_raw_text, now=now)
            raise

        _check_cancel(cancel)
        if reply.truncated:
            raise UserFacingError(_MAX_TOKENS_MESSAGE)

        _notify(progress, attempt_no, total, "検証中")
        last_raw_text = reply.text
        try:
            report = AnalysisReport.model_validate_json(reply.text)
        except ValidationError as exc:
            last_error = _truncate_error(str(exc))
            if attempt_no >= total:
                break
            _notify(progress, attempt_no, total, f"再依頼 {attempt_no}/{MAX_RETRIES}")
            prompt = build_retry_prompt(data, last_error)
            continue
        else:
            return AnalysisResult(
                report=report,
                data=data,
                model=client.model,
                in_tokens=counters["in_tokens"],
                out_tokens=counters["out_tokens"],
                attempts=counters["attempts"],
            )

    saved_path = _save_raw_response(symbol, last_raw_text, now=now)
    if saved_path is not None:
        raise UserFacingError(
            f"Gemini の応答がスキーマに合わず、{total}回試しても解決しませんでした。"
            f"生の応答を保存しました: {saved_path}"
        )
    raise UserFacingError(
        f"Gemini の応答がスキーマに合わず、{total}回試しても解決しませんでした。"
        "生の応答の保存にも失敗しました"
    )


def ai_analyze_job(db: Any, settings: Any) -> Callable[[Any, dict], dict]:
    """`jobs.register("ai_analyze", ai_analyze_job(db, settings))` に渡すジョブ関数を組み立てる。

    AI 分析は**手動実行のみ**（SPEC §2.7.1）。起動時の自動更新には含めない。
    レポートの生成と保存までをこのジョブで行い、画面には保存した1件の情報を返す。
    """

    def job(ctx: Any, params: dict) -> dict:
        symbol = (params or {}).get("symbol")
        days = (params or {}).get("days")
        if not symbol:
            raise UserFacingError("分析する銘柄が指定されていません")
        try:
            days = int(days)
        except (TypeError, ValueError):
            raise UserFacingError(f"期間の指定が不正です: {days}") from None

        result = run_analysis(
            db,
            settings,
            symbol,
            days,
            progress=ctx.progress,
            cancel=ctx.cancel,
            sleep=ctx.wait,  # 中断されたらすぐ Cancelled になる待機
        )

        ctx.progress(1, 1, "レポート生成中")
        # PromptInput.financials は P11-5（別作業者）が追加中のフィールド。この行を書いた時点では
        # まだ無いかもしれないため getattr で安全に読む（無ければ None → render_report は
        # 「財務数値は未取得です」の1行にフォールバックする）。P11-5 が入れば自動的に値が流れる
        financials = getattr(result.data, "financials", None)
        html = report.render_report(result.data, result.report, model=result.model, financials=financials)
        saved = report.save_report(
            db,
            config.REPORTS_DIR,
            symbol,
            html,
            model=result.model,
            in_tokens=result.in_tokens,
            out_tokens=result.out_tokens,
        )
        saved["attempts"] = result.attempts
        saved["summary"] = (
            f"{result.data.name}（{symbol}）の分析レポートを作成しました"
            f"（直近{result.data.days}日・{result.model}・入力 {result.in_tokens:,} / 出力 {result.out_tokens:,} トークン）"
        )
        return saved

    return job
