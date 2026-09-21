"""Gemini のクォータ管理（SPEC §2.7.2）。

`ai_usage` テーブル（移行 v5 で作成済み）に太平洋時間基準の日付ごと・モデルごとの使用量を積算する。
RPD のリセットが太平洋時間の深夜0時であり、夏時間の切替があるため固定オフセットは使わず
`zoneinfo.ZoneInfo("America/Los_Angeles")` を使う。

このモジュールは需給データ（空売り残高・貸借取引残高）に一切関わらない。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..errors import Cancelled, UserFacingError

PT = ZoneInfo("America/Los_Angeles")

# 直近60秒の RPM/TPM を数える窓の長さ
_WINDOW_SECONDS = 60.0
# 待機を細切れにする単位（秒）。短く刻むことで cancel にすぐ応答できる
_WAIT_STEP_SECONDS = 1.0

_QUOTA_EXHAUSTED_MESSAGE = (
    "本日の無料枠を使い切りました。太平洋時間0時にリセットされます（日本時間の当日16時または17時）。"
)
_NOT_CONFIGURED_MESSAGE = (
    "Gemini の1日あたりリクエスト上限が未設定です。設定タブで AI Studio の値を入力してください。"
)


def _to_pt(now: datetime | None) -> datetime:
    """now を太平洋時間の aware な datetime に変換する。

    now が None なら現在時刻を使う。now がナイーブ（タイムゾーン無し）なら、
    このモジュール以外の慣習（`datetime.now()` によるローカル素朴時刻）に合わせて
    実行環境のローカル時刻とみなす（`astimezone()` を引数無しで呼ぶとナイーブな
    datetime をローカル時刻として扱ってくれる仕様を利用する）。aware な datetime
    （例: JST を明示したもの）はそのまま変換する。
    """
    if now is None:
        now = datetime.now()
    if now.tzinfo is None:
        now = now.astimezone()
    return now.astimezone(PT)


def today_pt(now: datetime | None = None) -> str:
    """太平洋時間基準の日付を "YYYY-MM-DD" で返す。"""
    return _to_pt(now).strftime("%Y-%m-%d")


def next_reset_at(now: datetime | None = None) -> datetime:
    """次に RPD がリセットされる太平洋時間0時を、ローカル時刻に直した素朴 datetime で返す。"""
    pt_now = _to_pt(now)
    next_midnight_pt = datetime(pt_now.year, pt_now.month, pt_now.day, tzinfo=PT) + timedelta(days=1)
    return next_midnight_pt.astimezone().replace(tzinfo=None)


def _empty_usage(date_pt: str, model: str) -> dict:
    return {
        "date_pt": date_pt,
        "model": model,
        "requests": 0,
        "in_tokens": 0,
        "out_tokens": 0,
        "exhausted": 0,
    }


def usage(db, model: str, *, now: datetime | None = None) -> dict:
    """本日（太平洋時間）の使用量を返す。行が無ければ 0 埋めで返す（DB には作らない）。"""
    date = today_pt(now)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT date_pt, model, requests, in_tokens, out_tokens, exhausted "
            "FROM ai_usage WHERE date_pt = ? AND model = ?",
            (date, model),
        ).fetchone()
    return _empty_usage(date, model) if row is None else dict(row)


def record_request(db, model: str, *, now: datetime | None = None) -> None:
    """送信を試みた回数を1つ増やす。**送信直前に呼ぶ**（成功したかどうかは問わない）。"""
    date = today_pt(now)
    with db.write() as conn:
        conn.execute(
            "INSERT INTO ai_usage (date_pt, model, requests) VALUES (?, ?, 1) "
            "ON CONFLICT(date_pt, model) DO UPDATE SET requests = requests + 1",
            (date, model),
        )


def record_tokens(db, model: str, in_tokens: int, out_tokens: int, *, now: datetime | None = None) -> None:
    """応答後に入出力トークン数を加算する。"""
    date = today_pt(now)
    with db.write() as conn:
        conn.execute(
            "INSERT INTO ai_usage (date_pt, model, in_tokens, out_tokens) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(date_pt, model) DO UPDATE SET "
            "in_tokens = in_tokens + excluded.in_tokens, out_tokens = out_tokens + excluded.out_tokens",
            (date, model, in_tokens, out_tokens),
        )


def mark_exhausted(db, model: str, *, now: datetime | None = None) -> None:
    """本日そのモデルへの送信を打ち切る（日次上限の 429 を受けたとき）。"""
    date = today_pt(now)
    with db.write() as conn:
        conn.execute(
            "INSERT INTO ai_usage (date_pt, model, exhausted) VALUES (?, ?, 1) "
            "ON CONFLICT(date_pt, model) DO UPDATE SET exhausted = 1",
            (date, model),
        )


def reset_exhausted(db, model: str | None = None, *, now: datetime | None = None) -> int:
    """打ち切りフラグの手動解除（SPEC §2.1.3・§2.7.2）。本日ぶんのみが対象。

    model を省略すると本日の全モデルを解除する。戻り値は実際に解除した行数。
    """
    date = today_pt(now)
    with db.write() as conn:
        if model is None:
            cur = conn.execute(
                "UPDATE ai_usage SET exhausted = 0 WHERE date_pt = ? AND exhausted = 1", (date,)
            )
        else:
            cur = conn.execute(
                "UPDATE ai_usage SET exhausted = 0 WHERE date_pt = ? AND model = ? AND exhausted = 1",
                (date, model),
            )
        return cur.rowcount


def apply_quota_hit(db, model: str, scope: str, *, now: datetime | None = None) -> None:
    """429 応答を受けたときの後処理（SPEC §2.7.2 の判定表）。

    scope は quotaId から判別した種別:
    - "per_day": 日次上限 → 本日は打ち切る
    - "per_minute": 分次上限 → 打ち切らない（呼び出し側が retryDelay 待って1回だけ再送する）
    - "unknown": 判別できない → 安全側に倒して打ち切る
    "per_minute" 以外（想定外の値を含む）はすべて安全側で打ち切る扱いにする。
    """
    if scope == "per_minute":
        return
    mark_exhausted(db, model, now=now)


class RateWindow:
    """直近60秒の RPM/TPM をプロセス内メモリで数える窓（スレッドセーフ）。

    DB には残さない。プロセスを再起動すると窓の中身は失われるが、RPM/TPM は
    「直近60秒」の話でしかないので実害はない（RPD は ai_usage で永続化する）。
    """

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        # (計測時刻, トークン数, リクエストとして数えるか)
        self._events: deque[tuple[float, int, bool]] = deque()

    def add(self, tokens: int = 0) -> None:
        """1リクエスト分を記録する。"""
        with self._lock:
            self._events.append((self._clock(), tokens, True))

    def adjust_tokens(self, delta: int) -> None:
        """トークン数だけを補正する（見積りと実測の差分）。**リクエスト数には数えない。**

        `add` で足すと1回の送信が RPM 上2回に見えてしまう。
        """
        if not delta:
            return
        with self._lock:
            self._events.append((self._clock(), delta, False))

    def _purge(self, now: float) -> None:
        cutoff = now - _WINDOW_SECONDS
        while self._events and self._events[0][0] <= cutoff:
            self._events.popleft()

    def requests(self) -> int:
        with self._lock:
            self._purge(self._clock())
            return sum(1 for _, _, is_request in self._events if is_request)

    def tokens(self) -> int:
        with self._lock:
            self._purge(self._clock())
            return sum(tokens for _, tokens, _ in self._events)

    def wait_seconds(self, rpm: int, tpm: int, need_tokens: int) -> float:
        """上限に収まるまで待つべき秒数。

        rpm・tpm が 0 以下（未設定）の側はチェックしない。need_tokens 単体が tpm を
        超える場合は待っても解消しない（呼び出し側の check() で弾く前提）。
        """
        with self._lock:
            now = self._clock()
            self._purge(now)
            waits = []
            if rpm > 0:
                request_ts = [ts for ts, _, is_request in self._events if is_request]
                if len(request_ts) >= rpm:
                    # 窓から抜けるのを待つ必要がある最後の1件（rpm 件を超えていることもある）
                    waits.append(request_ts[len(request_ts) - rpm] + _WINDOW_SECONDS - now)
            if tpm > 0:
                total = sum(tokens for _, tokens, _ in self._events)
                if total + need_tokens > tpm:
                    remaining = total
                    wait_until_ts = now
                    for ts, tokens, _ in self._events:
                        remaining -= tokens
                        wait_until_ts = ts
                        if remaining + need_tokens <= tpm:
                            break
                    waits.append(wait_until_ts + _WINDOW_SECONDS - now)
            return max(waits) if waits else 0.0


class Quota:
    """設定（Settings）と ai_usage をまとめて見る窓口。DB とメモリ窓の両方を持つ。"""

    def __init__(self, db, settings, *, window: RateWindow | None = None, clock=None) -> None:
        self.db = db
        self.settings = settings
        if window is not None:
            self._window = window
        elif clock is not None:
            self._window = RateWindow(clock=clock)
        else:
            self._window = RateWindow()
        self._pending_estimate = 0  # start_request で見積もったトークン数（finish_request の補正用）

    @property
    def model(self) -> str:
        return self.settings.get("gemini_model")

    def _limits(self) -> dict:
        return {
            "rpm": self.settings.get("gemini_rpm"),
            "tpm": self.settings.get("gemini_tpm"),
            "rpd": self.settings.get("gemini_rpd"),
        }

    def snapshot(self, *, now: datetime | None = None) -> dict:
        """実行前の確認ダイアログ・`ai_quota()` 用の一覧（SPEC §2.7.1）。"""
        model = self.model
        limits = self._limits()
        configured = bool(model) and all(v > 0 for v in limits.values())
        used = usage(self.db, model, now=now) if model else _empty_usage(today_pt(now), model)
        remaining_rpd = None if limits["rpd"] <= 0 else max(limits["rpd"] - used["requests"], 0)
        remaining_tpm = None if limits["tpm"] <= 0 else max(limits["tpm"] - self._window.tokens(), 0)
        return {
            "model": model,
            "configured": configured,
            "limits": limits,
            "used": used,
            "remaining": {"rpd": remaining_rpd, "tpm": remaining_tpm},
            "exhausted": bool(used["exhausted"]),
            "reset_at": next_reset_at(now).strftime("%Y-%m-%d %H:%M"),
            "note": (
                "自前カウンタによる見積りです。上限はAPIキー単位ではなくプロジェクト単位のため、"
                "同じプロジェクトを他のツールでも使っている場合、実際の残量はこれより少ないことがあります。"
            ),
        }

    def check(self, need_tokens: int, *, now: datetime | None = None) -> None:
        """送信前ガード（SPEC §2.7.2）。送れないときは UserFacingError を投げる。

        判定順:
        1. 設定未完了（モデル名が空、または RPM/TPM/RPD のいずれかが0＝未設定）→ AI機能を無効として落とす
        2. 打ち切りフラグ（exhausted）が立っている
        3. RPD の残量が無い
        4. 今回送る分のトークン数自体が TPM 上限を超えている

        RPM/TPM の一時的な超過（今は上限内だが直近60秒の合計に上乗せすると超える）は
        待てば解消するため、ここでは落とさず wait() に任せる。
        """
        model = self.model
        limits = self._limits()
        if not model or any(v <= 0 for v in limits.values()):
            raise UserFacingError(_NOT_CONFIGURED_MESSAGE)
        used = usage(self.db, model, now=now)
        if used["exhausted"]:
            raise UserFacingError(_QUOTA_EXHAUSTED_MESSAGE)
        if used["requests"] >= limits["rpd"]:
            raise UserFacingError(_QUOTA_EXHAUSTED_MESSAGE)
        if need_tokens > limits["tpm"]:
            raise UserFacingError(
                f"送信予定のトークン数（約{need_tokens}）が1分あたりトークン上限（{limits['tpm']}）を"
                "超えています。期間を短くするか、設定タブで上限を見直してください。"
            )

    def wait(self, need_tokens: int, *, sleep=None, cancel=None) -> float:
        """RPM/TPM の直近60秒枠に収まるまで待つ。待った秒数を返す。

        待機は `_WAIT_STEP_SECONDS` 単位の細切れにして、cancel（threading.Event 相当）が
        立っていればすぐ `app.errors.Cancelled` を送出する（ジョブからの中断用）。
        """
        sleep = sleep or time.sleep
        limits = self._limits()
        waited = 0.0
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            remain = self._window.wait_seconds(limits["rpm"], limits["tpm"], need_tokens)
            if remain <= 0:
                return waited
            chunk = min(remain, _WAIT_STEP_SECONDS)
            sleep(chunk)
            waited += chunk
            if cancel is not None and cancel.is_set():
                raise Cancelled()

    def start_request(self, need_tokens: int, *, now: datetime | None = None, sleep=None, cancel=None) -> None:
        """送信直前に1回だけ呼ぶ。check → wait → record_request → window.add の順で行う。"""
        self.check(need_tokens, now=now)
        self.wait(need_tokens, sleep=sleep, cancel=cancel)
        record_request(self.db, self.model, now=now)
        self._window.add(need_tokens)
        self._pending_estimate = need_tokens

    def finish_request(self, reply_in_tokens: int, reply_out_tokens: int, *, now: datetime | None = None) -> None:
        """応答後に呼ぶ。DB に実トークン数を積み、window は見積りとの差分だけ足して補正する。"""
        record_tokens(self.db, self.model, reply_in_tokens, reply_out_tokens, now=now)
        actual = reply_in_tokens + reply_out_tokens
        correction = actual - self._pending_estimate
        self._pending_estimate = 0
        self._window.adjust_tokens(correction)
