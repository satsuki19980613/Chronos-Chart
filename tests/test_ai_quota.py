"""app.ai.quota のテスト（SPEC §2.7.2 クォータ管理）。

ネットワークには一切アクセスしない。DB は tmp_path に作り、Database.init_schema() で
移行 v5（ai_usage / ai_reports）まで適用してから使う。
"""

from __future__ import annotations

import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.ai import quota
from app.database import Database
from app.errors import Cancelled, UserFacingError
from app.settings import Settings

JST = ZoneInfo("Asia/Tokyo")


class FakeClock:
    """time.monotonic の代わり。テストから時刻を自由に進められる。"""

    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "test.db")
    db.init_schema()
    settings = Settings(db)  # gemini_model 等は secret ではないので keyring は不要
    return db, settings


# ---------- today_pt / next_reset_at ----------
def test_today_pt_is_pacific_time_not_jst(env):
    db, _ = env
    now = datetime(2026, 6, 15, 8, 0, tzinfo=JST)  # JST 6/15 朝 -> PT はまだ 6/14
    assert quota.today_pt(now) == "2026-06-14"


def test_today_pt_across_spring_forward(env):
    """3月の夏時間開始をまたぐと、同じ JST 時刻でも PT の日付の進み方が変わる。"""
    before = datetime(2026, 3, 8, 16, 0, tzinfo=JST)  # 夏時間開始前(PST, UTC-8)
    after = datetime(2026, 3, 9, 16, 0, tzinfo=JST)  # 夏時間開始後(PDT, UTC-7)
    assert quota.today_pt(before) == "2026-03-07"
    # 固定オフセットなら 3/8 になるはずだが、夏時間で1時間ずれて 3/9 に飛ぶ
    assert quota.today_pt(after) == "2026-03-09"


def test_today_pt_across_fall_back(env):
    """11月の夏時間終了をまたぐと、翌日の JST 同時刻でも PT の日付が変わらないことがある。"""
    before = datetime(2026, 11, 1, 16, 0, tzinfo=JST)  # 夏時間終了前(PDT, UTC-7)
    after = datetime(2026, 11, 2, 16, 0, tzinfo=JST)  # 夏時間終了後(PST, UTC-8)
    # 固定オフセットなら 11/1 と 11/2 に分かれるはずだが、夏時間の巻き戻しで両方とも 11/1 になる
    assert quota.today_pt(before) == "2026-11-01"
    assert quota.today_pt(after) == "2026-11-01"


def test_next_reset_at_is_next_pt_midnight(env):
    now = datetime(2026, 6, 15, 10, 30, tzinfo=quota.PT)
    result = quota.next_reset_at(now)
    back_in_pt = result.astimezone(quota.PT)
    assert back_in_pt.strftime("%H:%M:%S") == "00:00:00"
    assert back_in_pt.date().isoformat() == "2026-06-16"


def test_next_reset_at_at_exact_midnight_is_next_day(env):
    now = datetime(2026, 6, 15, 0, 0, 0, tzinfo=quota.PT)
    result = quota.next_reset_at(now)
    assert result.astimezone(quota.PT).date().isoformat() == "2026-06-16"


# ---------- usage / record_request / record_tokens ----------
def test_usage_defaults_to_zero_when_no_row(env):
    db, _ = env
    row = quota.usage(db, "gemini-x")
    assert row == {
        "date_pt": quota.today_pt(),
        "model": "gemini-x",
        "requests": 0,
        "in_tokens": 0,
        "out_tokens": 0,
        "exhausted": 0,
    }


def test_record_request_upserts_idempotently(env):
    db, _ = env
    quota.record_request(db, "gemini-x")
    quota.record_request(db, "gemini-x")
    quota.record_request(db, "gemini-x")
    assert quota.usage(db, "gemini-x")["requests"] == 3


def test_record_tokens_accumulates(env):
    db, _ = env
    quota.record_tokens(db, "gemini-x", 100, 20)
    quota.record_tokens(db, "gemini-x", 50, 5)
    row = quota.usage(db, "gemini-x")
    assert row["in_tokens"] == 150
    assert row["out_tokens"] == 25


def test_usage_resets_after_date_changes_but_keeps_old_row(env):
    db, _ = env
    day1 = datetime(2026, 6, 15, 10, 0, tzinfo=quota.PT)
    day2 = datetime(2026, 6, 16, 10, 0, tzinfo=quota.PT)
    quota.record_request(db, "m", now=day1)
    quota.record_request(db, "m", now=day1)
    assert quota.usage(db, "m", now=day1)["requests"] == 2
    assert quota.usage(db, "m", now=day2)["requests"] == 0
    assert quota.usage(db, "m", now=day1)["requests"] == 2  # 前日の行は残る


# ---------- exhausted フラグ ----------
def test_mark_and_reset_exhausted_for_one_model(env):
    db, _ = env
    quota.mark_exhausted(db, "m")
    assert quota.usage(db, "m")["exhausted"] == 1
    changed = quota.reset_exhausted(db, "m")
    assert changed == 1
    assert quota.usage(db, "m")["exhausted"] == 0


def test_reset_exhausted_without_model_clears_all(env):
    db, _ = env
    quota.mark_exhausted(db, "a")
    quota.mark_exhausted(db, "b")
    quota.record_request(db, "c")  # exhausted ではないモデルは対象外
    changed = quota.reset_exhausted(db)
    assert changed == 2
    assert quota.usage(db, "a")["exhausted"] == 0
    assert quota.usage(db, "b")["exhausted"] == 0


def test_reset_exhausted_returns_zero_when_nothing_to_clear(env):
    db, _ = env
    assert quota.reset_exhausted(db) == 0


# ---------- apply_quota_hit ----------
def test_apply_quota_hit_per_day_marks_exhausted(env):
    db, _ = env
    quota.apply_quota_hit(db, "m", "per_day")
    assert quota.usage(db, "m")["exhausted"] == 1


def test_apply_quota_hit_unknown_marks_exhausted(env):
    db, _ = env
    quota.apply_quota_hit(db, "m", "unknown")
    assert quota.usage(db, "m")["exhausted"] == 1


def test_apply_quota_hit_per_minute_does_not_mark_exhausted(env):
    db, _ = env
    quota.apply_quota_hit(db, "m", "per_minute")
    assert quota.usage(db, "m")["exhausted"] == 0


# ---------- RateWindow ----------
def test_rate_window_counts_and_evicts_after_60_seconds():
    clock = FakeClock()
    window = quota.RateWindow(clock=clock)
    window.add(100)
    assert window.requests() == 1
    assert window.tokens() == 100
    clock.advance(59)
    assert window.requests() == 1
    clock.advance(1.5)
    assert window.requests() == 0
    assert window.tokens() == 0


def test_rate_window_wait_seconds_for_rpm():
    clock = FakeClock()
    window = quota.RateWindow(clock=clock)
    window.add(0)
    assert window.wait_seconds(rpm=1, tpm=0, need_tokens=0) == pytest.approx(60.0)
    clock.advance(60)
    assert window.wait_seconds(rpm=1, tpm=0, need_tokens=0) == 0.0


def test_rate_window_wait_seconds_for_tpm():
    clock = FakeClock()
    window = quota.RateWindow(clock=clock)
    window.add(900)
    assert window.wait_seconds(rpm=0, tpm=1000, need_tokens=200) == pytest.approx(60.0)
    assert window.wait_seconds(rpm=0, tpm=1000, need_tokens=50) == 0.0


def test_rate_window_zero_limit_means_unchecked():
    window = quota.RateWindow(clock=FakeClock())
    window.add(10**9)
    assert window.wait_seconds(rpm=0, tpm=0, need_tokens=10**9) == 0.0


# ---------- Quota.check ----------
def test_check_fails_when_model_not_configured(env):
    db, settings = env
    q = quota.Quota(db, settings)
    with pytest.raises(UserFacingError, match="未設定"):
        q.check(10)


def test_check_fails_when_any_limit_is_zero(env):
    db, settings = env
    settings.update({"gemini_model": "m", "gemini_rpm": 10, "gemini_tpm": 1000, "gemini_rpd": 0})
    q = quota.Quota(db, settings)
    with pytest.raises(UserFacingError, match="未設定"):
        q.check(10)


def test_check_fails_when_exhausted_flag_is_set(env):
    db, settings = env
    settings.update({"gemini_model": "m", "gemini_rpm": 10, "gemini_tpm": 1000, "gemini_rpd": 10})
    quota.mark_exhausted(db, "m")
    q = quota.Quota(db, settings)
    with pytest.raises(UserFacingError, match="無料枠"):
        q.check(10)


def test_check_fails_when_rpd_used_up(env):
    db, settings = env
    settings.update({"gemini_model": "m", "gemini_rpm": 10, "gemini_tpm": 1000, "gemini_rpd": 1})
    quota.record_request(db, "m")
    q = quota.Quota(db, settings)
    with pytest.raises(UserFacingError, match="無料枠"):
        q.check(10)


def test_check_fails_when_need_tokens_exceeds_tpm(env):
    db, settings = env
    settings.update({"gemini_model": "m", "gemini_rpm": 10, "gemini_tpm": 500, "gemini_rpd": 10})
    q = quota.Quota(db, settings)
    with pytest.raises(UserFacingError, match="トークン"):
        q.check(1000)


def test_check_passes_when_within_limits(env):
    db, settings = env
    settings.update({"gemini_model": "m", "gemini_rpm": 10, "gemini_tpm": 1000, "gemini_rpd": 10})
    q = quota.Quota(db, settings)
    q.check(10)  # 例外が出なければ OK


def test_exhausted_can_be_manually_reset_then_check_passes_again(env):
    db, settings = env
    settings.update({"gemini_model": "m", "gemini_rpm": 10, "gemini_tpm": 1000, "gemini_rpd": 10})
    quota.mark_exhausted(db, "m")
    q = quota.Quota(db, settings)
    with pytest.raises(UserFacingError):
        q.check(10)
    quota.reset_exhausted(db, "m")
    q.check(10)


# ---------- Quota.wait ----------
def test_quota_wait_sleeps_until_rpm_window_clears(env):
    db, settings = env
    settings.update({"gemini_model": "m", "gemini_rpm": 1, "gemini_tpm": 1000, "gemini_rpd": 100})
    clock = FakeClock()
    window = quota.RateWindow(clock=clock)
    window.add(10)
    q = quota.Quota(db, settings, window=window)
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds)

    waited = q.wait(10, sleep=fake_sleep)
    assert waited == pytest.approx(60.0)
    assert sum(sleeps) == pytest.approx(60.0)


def test_quota_wait_returns_immediately_when_room_available(env):
    db, settings = env
    settings.update({"gemini_model": "m", "gemini_rpm": 10, "gemini_tpm": 1000, "gemini_rpd": 100})
    q = quota.Quota(db, settings)
    calls = []
    waited = q.wait(10, sleep=lambda s: calls.append(s))
    assert waited == 0.0
    assert calls == []


def test_quota_wait_raises_cancelled_when_cancel_event_is_set(env):
    db, settings = env
    settings.update({"gemini_model": "m", "gemini_rpm": 1, "gemini_tpm": 1000, "gemini_rpd": 100})
    clock = FakeClock()
    window = quota.RateWindow(clock=clock)
    window.add(10)
    q = quota.Quota(db, settings, window=window)
    cancel = threading.Event()
    calls = []

    def fake_sleep(seconds):
        calls.append(seconds)
        clock.advance(seconds)
        if len(calls) >= 3:
            cancel.set()

    with pytest.raises(Cancelled):
        q.wait(10, sleep=fake_sleep, cancel=cancel)
    assert len(calls) == 3  # 中断までは細切れに待ち続けていた


def test_quota_wait_raises_cancelled_immediately_if_already_set(env):
    db, settings = env
    settings.update({"gemini_model": "m", "gemini_rpm": 1, "gemini_tpm": 1000, "gemini_rpd": 100})
    window = quota.RateWindow(clock=FakeClock())
    window.add(10)
    q = quota.Quota(db, settings, window=window)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        q.wait(10, sleep=lambda s: None, cancel=cancel)


# ---------- Quota.start_request / finish_request ----------
def test_start_request_records_and_fills_window(env):
    db, settings = env
    settings.update({"gemini_model": "m", "gemini_rpm": 10, "gemini_tpm": 1000, "gemini_rpd": 10})
    window = quota.RateWindow(clock=FakeClock())
    q = quota.Quota(db, settings, window=window)
    q.start_request(200)
    assert quota.usage(db, "m")["requests"] == 1
    assert window.tokens() == 200


def test_start_request_reevaluates_guard_each_call(env):
    """再依頼のたびに直前でガードを再評価する（事前にまとめて確保しない）。"""
    db, settings = env
    settings.update({"gemini_model": "m", "gemini_rpm": 10, "gemini_tpm": 1000, "gemini_rpd": 1})
    window = quota.RateWindow(clock=FakeClock())
    q = quota.Quota(db, settings, window=window)
    q.start_request(10)  # 1回目で RPD を使い切る
    with pytest.raises(UserFacingError, match="無料枠"):
        q.start_request(10)  # 2回目（再依頼）は直前の再評価で弾かれる
    assert quota.usage(db, "m")["requests"] == 1  # 弾かれた分は加算されない


def test_finish_request_corrects_window_to_actual_tokens(env):
    db, settings = env
    settings.update({"gemini_model": "m", "gemini_rpm": 10, "gemini_tpm": 1000, "gemini_rpd": 10})
    window = quota.RateWindow(clock=FakeClock())
    q = quota.Quota(db, settings, window=window)
    q.start_request(200)  # 見積り200トークン分を先に積む
    assert window.tokens() == 200
    q.finish_request(reply_in_tokens=150, reply_out_tokens=30)  # 実際は180トークンだった
    assert window.tokens() == 180
    row = quota.usage(db, "m")
    assert row["in_tokens"] == 150
    assert row["out_tokens"] == 30


def test_finish_request_does_not_count_a_second_request(env):
    """トークン数の補正で RPM を二重計上しない（1回の送信は窓の上でも1リクエスト）。"""
    db, settings = env
    settings.update({"gemini_model": "m", "gemini_rpm": 10, "gemini_tpm": 1000, "gemini_rpd": 10})
    window = quota.RateWindow(clock=FakeClock())
    q = quota.Quota(db, settings, window=window)
    q.start_request(200)
    q.finish_request(reply_in_tokens=150, reply_out_tokens=30)
    assert window.requests() == 1
    assert window.tokens() == 180
    # 補正が 0 のとき（見積りが的中）も同じ
    q.start_request(100)
    q.finish_request(reply_in_tokens=60, reply_out_tokens=40)
    assert window.requests() == 2


# ---------- Quota.snapshot ----------
def test_snapshot_when_not_configured(env):
    db, settings = env
    q = quota.Quota(db, settings)
    snap = q.snapshot()
    assert snap["configured"] is False
    assert snap["remaining"]["rpd"] is None
    assert snap["remaining"]["tpm"] is None
    assert snap["exhausted"] is False
    assert "プロジェクト単位" in snap["note"]


def test_snapshot_reports_remaining_and_reset_time(env):
    db, settings = env
    settings.update({"gemini_model": "m", "gemini_rpm": 10, "gemini_tpm": 1000, "gemini_rpd": 5})
    now = datetime(2026, 6, 15, 10, 0, tzinfo=quota.PT)
    quota.record_request(db, "m", now=now)
    quota.record_request(db, "m", now=now)
    q = quota.Quota(db, settings)
    snap = q.snapshot(now=now)
    assert snap["configured"] is True
    assert snap["used"]["requests"] == 2
    assert snap["remaining"]["rpd"] == 3
    assert snap["remaining"]["tpm"] == 1000  # ウィンドウが空ならまだ満額
    assert snap["reset_at"] == quota.next_reset_at(now).strftime("%Y-%m-%d %H:%M")


def test_snapshot_reflects_exhausted_flag(env):
    db, settings = env
    settings.update({"gemini_model": "m", "gemini_rpm": 10, "gemini_tpm": 1000, "gemini_rpd": 5})
    quota.mark_exhausted(db, "m")
    q = quota.Quota(db, settings)
    assert q.snapshot()["exhausted"] is True


# ---------- 需給データに関わらないことの確認（CLAUDE.md の不変条件） ----------
def test_module_source_has_no_forbidden_identifiers():
    import inspect

    source = inspect.getsource(quota)
    for banned in ("short_", "margin_", "taisyaku", "karauri"):
        assert banned not in source
