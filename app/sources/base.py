"""間隔制御つきの共通 HTTP クライアント（SPEC §4.1）。

- ソースごとに最小リクエスト間隔を守り、同じソースへのリクエストは直列にする
- リトライの待機はソースの最小間隔を下回らない
- 待機は中断フラグ（threading.Event）で解ける
- API キーをログにも例外メッセージにも出さない
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable
from urllib.parse import urlencode

import requests

from .. import __version__
from ..errors import Cancelled, UserFacingError

log = logging.getLogger(__name__)

TIMEOUT = (10, 30)  # 接続 / 読み取り（秒）
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRIES = 3

_SENSITIVE_PARAMS = ("subscription-key", "key", "api_key", "apikey")
_SENSITIVE_RE = re.compile(rf"(?i)\b({'|'.join(re.escape(p) for p in _SENSITIVE_PARAMS)})=[^&\s'\"]+")


class HttpError(UserFacingError):
    """取得に失敗した。status は HTTP ステータス（通信自体の失敗は None）。"""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def mask_secrets(text: str) -> str:
    """URL や例外メッセージに含まれる API キーの値を *** にする。"""
    return _SENSITIVE_RE.sub(lambda m: f"{m.group(1)}=***", text)


def user_agent(contact: str = "") -> str:
    """ブラウザを偽装しない。連絡先があれば名乗る。"""
    contact = contact.strip()
    return f"ChronosChart/{__version__} (+{contact})" if contact else f"ChronosChart/{__version__}"


class _SourceState:
    def __init__(self):
        self.lock = threading.Lock()
        self.next_allowed = 0.0  # これより前にはリクエストしない（clock 基準）


_states: dict[str, _SourceState] = {}
_states_guard = threading.Lock()


def _state(source: str) -> _SourceState:
    with _states_guard:
        return _states.setdefault(source, _SourceState())


class HttpClient:
    def __init__(
        self,
        source: str,
        min_interval: float | Callable[[], float],
        agent: str | Callable[[], str] | None = None,
        no_retry_statuses: frozenset[int] = frozenset(),
        session=None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.source = source
        self._min_interval = min_interval
        self._agent = agent if agent is not None else user_agent()
        self.no_retry_statuses = no_retry_statuses
        self._session = session or requests.Session()
        self._clock = clock
        self._sleep = sleep
        self._state = _state(source)

    @property
    def min_interval(self) -> float:
        return float(self._min_interval() if callable(self._min_interval) else self._min_interval)

    def get(self, url: str, params: dict | None = None, cancel: threading.Event | None = None) -> requests.Response:
        """2xx のレスポンスを返す。それ以外は HttpError、中断されたら Cancelled。"""
        shown = mask_secrets(f"{url}?{urlencode(params)}" if params else url)
        headers = {"User-Agent": self._agent() if callable(self._agent) else self._agent}

        self._acquire(cancel)  # 同じソースへは1本ずつ
        try:
            for attempt in range(MAX_RETRIES + 1):
                self._wait_turn(cancel)
                try:
                    response = self._session.get(url, params=params, headers=headers, timeout=TIMEOUT)
                except requests.RequestException as exc:
                    self._state.next_allowed = self._clock() + self.min_interval
                    log.warning("%s GET %s failed: %s", self.source, shown, mask_secrets(str(exc)))
                    raise HttpError(f"{self.source} に接続できませんでした（{type(exc).__name__}）") from None

                status = response.status_code
                log.info("%s GET %s -> %d", self.source, shown, status)
                retryable = status in RETRY_STATUSES and status not in self.no_retry_statuses
                if retryable and attempt < MAX_RETRIES:
                    # 指数バックオフ。ただしソースの最小間隔は必ず守る
                    self._state.next_allowed = self._clock() + max(2.0**attempt, self.min_interval)
                    continue
                self._state.next_allowed = self._clock() + self.min_interval
                if 200 <= status < 300:
                    return response
                raise HttpError(f"{self.source} がエラーを返しました（HTTP {status}）", status)
        finally:
            self._state.lock.release()
        raise AssertionError("unreachable")

    def _acquire(self, cancel: threading.Event | None) -> None:
        """ソースのロックを取る。順番待ちの間も中断に応じる。"""
        while not self._state.lock.acquire(timeout=0.2):
            if cancel is not None and cancel.is_set():
                raise Cancelled()

    def _wait_turn(self, cancel: threading.Event | None) -> None:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        remaining = self._state.next_allowed - self._clock()
        if remaining <= 0:
            return
        if cancel is None:
            self._sleep(remaining)
        elif cancel.wait(remaining):  # 中断されたら待機をすぐ解く
            raise Cancelled()
