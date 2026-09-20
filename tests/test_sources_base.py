"""共通 HTTP クライアント（SPEC §4.1）。ネットワークは使わない。"""

import logging
import threading
import time

import pytest
import requests

from app import __version__
from app.errors import Cancelled
from app.sources import base
from app.sources.base import HttpClient, HttpError, mask_secrets, user_agent

KEY = "SECRETKEY0123456789"


class FakeResponse:
    def __init__(self, status_code=200, text="ok"):
        self.status_code = status_code
        self.text = text


class FakeSession:
    def __init__(self, responses, clock):
        self.responses = list(responses)
        self.clock = clock
        self.calls = []  # (時刻, url, params, headers)

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((self.clock.now, url, params, headers))
        assert timeout == base.TIMEOUT
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClock:
    """sleep した分だけ進む時計。"""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def fresh_source_state():
    base._states.clear()


def make(responses, interval=10, **kwargs):
    clock = FakeClock()
    session = FakeSession(responses, clock)
    client = HttpClient("karauri", interval, session=session, clock=clock, sleep=clock.sleep, **kwargs)
    return client, session, clock


def times(session):
    return [c[0] - session.calls[0][0] for c in session.calls]


# ---------- 間隔 ----------
def test_first_request_does_not_wait():
    client, _, clock = make([FakeResponse()])
    client.get("https://example.test/a")
    assert clock.slept == []


def test_min_interval_between_requests():
    client, session, _ = make([FakeResponse(), FakeResponse(), FakeResponse()], interval=10)
    for path in "abc":
        client.get(f"https://example.test/{path}")
    assert times(session) == [0, 10, 20]


def test_elapsed_time_counts_toward_interval():
    client, session, clock = make([FakeResponse(), FakeResponse()], interval=10)
    client.get("https://example.test/a")
    clock.now += 7  # パースなどで時間が経った
    client.get("https://example.test/b")
    assert clock.slept == [pytest.approx(3)]


def test_interval_is_shared_across_clients_of_same_source():
    clock = FakeClock()
    session = FakeSession([FakeResponse(), FakeResponse()], clock)
    a = HttpClient("edinet", 1, session=session, clock=clock, sleep=clock.sleep)
    b = HttpClient("edinet", 1, session=session, clock=clock, sleep=clock.sleep)
    a.get("https://example.test/a")
    b.get("https://example.test/b")
    assert times(session) == [0, 1]


def test_different_sources_do_not_block_each_other():
    clock = FakeClock()
    session = FakeSession([FakeResponse(), FakeResponse()], clock)
    HttpClient("karauri", 10, session=session, clock=clock, sleep=clock.sleep).get("https://example.test/a")
    HttpClient("edinet", 1, session=session, clock=clock, sleep=clock.sleep).get("https://example.test/b")
    assert clock.slept == []


def test_interval_can_follow_a_setting():
    current = {"v": 10}
    client, session, _ = make([FakeResponse()] * 3, interval=lambda: current["v"])
    client.get("https://example.test/a")
    client.get("https://example.test/b")
    current["v"] = 30
    client.get("https://example.test/c")
    client_times = times(session)
    assert client_times[1] == 10
    assert client_times[2] == 20  # 直前のリクエスト時点の設定（10秒）で次の時刻が決まっている


# ---------- リトライ ----------
def test_retries_on_5xx_with_backoff():
    client, session, _ = make([FakeResponse(503), FakeResponse(500), FakeResponse()], interval=1)
    assert client.get("https://example.test/a").status_code == 200
    assert times(session) == [0, 1, 3]  # 1s → 2s


def test_backoff_never_goes_below_min_interval():
    client, session, _ = make([FakeResponse(503), FakeResponse(503), FakeResponse()], interval=10)
    client.get("https://example.test/a")
    assert times(session) == [0, 10, 20]


def test_gives_up_after_max_retries():
    client, session, _ = make([FakeResponse(503)] * 4, interval=1)
    with pytest.raises(HttpError) as err:
        client.get("https://example.test/a")
    assert err.value.status == 503
    assert len(session.calls) == base.MAX_RETRIES + 1
    assert times(session) == [0, 1, 3, 7]


def test_4xx_fails_immediately():
    client, session, _ = make([FakeResponse(404)])
    with pytest.raises(HttpError) as err:
        client.get("https://example.test/a")
    assert err.value.status == 404
    assert len(session.calls) == 1


def test_429_is_retried_by_default():
    client, session, _ = make([FakeResponse(429), FakeResponse()], interval=1)
    client.get("https://example.test/a")
    assert len(session.calls) == 2


def test_no_retry_statuses_fail_immediately():
    """karauri.net の 403 / 429 は拒否の意思表示なのでリトライしない（SPEC §2.2.4）。"""
    client, session, _ = make([FakeResponse(429)], no_retry_statuses=frozenset({403, 429}))
    with pytest.raises(HttpError) as err:
        client.get("https://example.test/a")
    assert err.value.status == 429
    assert len(session.calls) == 1


def test_interval_is_kept_after_a_failure():
    client, session, _ = make([FakeResponse(404), FakeResponse()], interval=10)
    with pytest.raises(HttpError):
        client.get("https://example.test/a")
    client.get("https://example.test/b")
    assert times(session) == [0, 10]


def test_connection_error_becomes_http_error_without_status():
    client, _, _ = make([requests.ConnectionError("boom")])
    with pytest.raises(HttpError) as err:
        client.get("https://example.test/a")
    assert err.value.status is None


# ---------- User-Agent ----------
def test_user_agent_identifies_the_tool():
    assert user_agent() == f"ChronosChart/{__version__}"
    assert user_agent(" me@example.test ") == f"ChronosChart/{__version__} (+me@example.test)"
    assert "Mozilla" not in user_agent("me@example.test")


def test_user_agent_header_is_sent_and_can_follow_a_setting():
    client, session, _ = make([FakeResponse()], agent=lambda: user_agent("me@example.test"))
    client.get("https://example.test/a")
    assert session.calls[0][3]["User-Agent"].endswith("(+me@example.test)")


# ---------- API キーの秘匿 ----------
def test_mask_secrets():
    url = f"https://api.example.test/documents.json?date=2026-09-18&type=2&Subscription-Key={KEY}"
    masked = mask_secrets(url)
    assert KEY not in masked
    assert "date=2026-09-18" in masked
    assert "Subscription-Key=***" in masked
    assert mask_secrets(f"x?key={KEY}&a=1") == "x?key=***&a=1"


def test_key_never_appears_in_logs(caplog):
    client, _, _ = make([FakeResponse(), FakeResponse(503), FakeResponse()], interval=1)
    with caplog.at_level(logging.DEBUG):
        client.get("https://api.example.test/documents.json", params={"date": "2026-09-18", "Subscription-Key": KEY})
        client.get(f"https://api.example.test/documents/S100?type=2&Subscription-Key={KEY}")
    assert "documents.json" in caplog.text
    assert KEY not in caplog.text


def test_key_never_appears_in_errors_or_logs_on_connection_failure(caplog):
    exc = requests.ConnectionError(f"Max retries exceeded with url: /x?Subscription-Key={KEY}")
    client, _, _ = make([exc])
    with caplog.at_level(logging.DEBUG), pytest.raises(HttpError) as err:
        client.get("https://api.example.test/x", params={"Subscription-Key": KEY})
    assert KEY not in caplog.text
    assert KEY not in str(err.value)
    assert err.value.__cause__ is None  # 元の例外（URL にキーを含む）を連鎖させない


def test_params_are_passed_to_the_session_unmasked():
    client, session, _ = make([FakeResponse()])
    client.get("https://api.example.test/x", params={"Subscription-Key": KEY})
    assert session.calls[0][2] == {"Subscription-Key": KEY}


# ---------- 中断 ----------
def test_cancel_before_request():
    client, session, _ = make([FakeResponse()])
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        client.get("https://example.test/a", cancel=cancel)
    assert session.calls == []


def test_cancel_interrupts_the_wait():
    session = FakeSession([FakeResponse(), FakeResponse()], FakeClock())
    client = HttpClient("karauri", 30, session=session)  # 実時間で 30 秒待つ設定
    cancel = threading.Event()
    client.get("https://example.test/a", cancel=cancel)
    threading.Timer(0.1, cancel.set).start()

    started = time.monotonic()
    with pytest.raises(Cancelled):
        client.get("https://example.test/b", cancel=cancel)
    assert time.monotonic() - started < 5
    assert len(session.calls) == 1


def test_cancel_while_waiting_for_another_request_of_the_same_source():
    client = HttpClient("karauri", 0, session=FakeSession([FakeResponse()], FakeClock()))
    cancel = threading.Event()
    client._state.lock.acquire()  # 別のジョブがこのソースを使用中
    try:
        threading.Timer(0.1, cancel.set).start()
        started = time.monotonic()
        with pytest.raises(Cancelled):
            client.get("https://example.test/a", cancel=cancel)
        assert time.monotonic() - started < 5
    finally:
        client._state.lock.release()


def test_lock_is_released_after_an_error():
    client, _, _ = make([FakeResponse(404), FakeResponse()], interval=0)
    with pytest.raises(HttpError):
        client.get("https://example.test/a")
    assert client.get("https://example.test/b").status_code == 200
