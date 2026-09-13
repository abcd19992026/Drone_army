"""tests/test_scheduler.py -- Phase 9 verification suite.

Tests gss/scheduler.py (Patrol Scheduler) in isolation: a MagicMock stands in
for TelemetryStore, so none of this needs a real Supabase, ffmpeg, or a
MAVLink link. Mirrors tests/test_media.py's Phase 8 pytest style.

Verification items (per the Phase 9 spec):
  1  Due + active -> exactly one commands row is written this tick;
     next_run_at advances to the correct future occurrence (daily, weekly).
  2  next_run_at in the future -> nothing happens.
  3  active=false -> nothing happens even with next_run_at in the past.
  4  Daily next_occurrence: lands on the same time_of_day the following day,
     whether 'after' is before or after that time on the current day.
  5  Weekly next_occurrence: respects days_of_week across a week boundary.
  6  Drone mid-mission -> a due schedule is skipped, last_skipped_reason is
     set, and next_run_at still advances (no double-fire once it lands).
  7  Staleness: next_run_at more than SCHEDULER_MAX_STALENESS_S in the past
     is skipped with its own distinct reason, not launched late.
  8  Two schedules due in the same tick for the one drone: only the first
     produces a commands row; the second is skipped this tick.
  9  Supabase unreachable during the schedules read -> the tick does
     nothing; no crash, no partial writes (R10).
  10 SCHEDULER_ENABLED=false -> gss.scheduler is never imported at
     gss/main.py's module level; gss.main behaves identically to Phase 8.
  11 All earlier suites still pass (run separately, per the README: each
     test file individually via `pytest tests/test_<name>.py`).
"""

from __future__ import annotations

import inspect
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

os.environ.setdefault("MEDIA_ENABLED", "false")
os.environ.setdefault("SUPABASE_ENABLED", "false")

from gss import config
from gss import scheduler as sched

# 2026-09-13 is a Sunday. 03:00 UTC = 08:30 IST (Asia/Kolkata, UTC+5:30, no DST).
_NOW = datetime(2026, 9, 13, 3, 0, 0, tzinfo=timezone.utc)


def _sid() -> str:
    return str(uuid.uuid4())


def _schedule(
    *,
    schedule_id: str | None = None,
    name: str = "test patrol",
    target_lat: float = 25.6,
    target_lon: float = 85.2,
    cruise_alt_m: float | None = None,
    loiter_seconds: int = 120,
    cadence: str = "daily",
    time_of_day: str = "07:00:00",
    days_of_week: list[int] | None = None,
    active: bool = True,
    next_run_at: datetime,
) -> dict:
    return {
        "id": schedule_id or _sid(),
        "name": name,
        "target_lat": target_lat,
        "target_lon": target_lon,
        "cruise_alt_m": cruise_alt_m,
        "loiter_seconds": loiter_seconds,
        "cadence": cadence,
        "time_of_day": time_of_day,
        "days_of_week": days_of_week,
        "active": active,
        "next_run_at": next_run_at.isoformat(),
    }


def _stub_store(schedules: list[dict], *, active_missions: list[dict] | None = None):
    """A MagicMock that looks enough like TelemetryStore for scheduler.py."""
    store = MagicMock()
    store.get_active_patrol_schedules.return_value = list(schedules)
    store.list_active_missions.return_value = list(active_missions or [])
    store.create_command.return_value = _sid()
    store.update_patrol_schedule.return_value = True
    return store


# ── 1: due + active -> fires, next_run_at advances correctly ────────────────

def test_1_due_and_active_fires_and_advances_daily():
    schedule = _schedule(
        cadence="daily", time_of_day="07:00:00",
        next_run_at=_NOW - timedelta(minutes=5),
    )
    store = _stub_store([schedule])
    scheduler = sched.PatrolScheduler(store, drone_id="d1")

    scheduler.tick(now=_NOW)

    assert store.create_command.call_count == 1
    fire_kwargs = store.create_command.call_args.kwargs
    assert fire_kwargs["cmd_type"] == "goto"
    assert fire_kwargs["target_lat"] == 25.6
    assert fire_kwargs["target_lon"] == 85.2
    assert fire_kwargs["issued_by"] == f"scheduler:{schedule['id']}"

    assert store.update_patrol_schedule.call_count == 1
    update_kwargs = store.update_patrol_schedule.call_args.kwargs
    # independently verified: Sunday 08:30 IST, daily 07:00 IST already
    # passed today -> next occurrence is tomorrow 07:00 IST
    assert update_kwargs["next_run_at"] == "2026-09-14T01:30:00+00:00"
    assert update_kwargs["last_skipped_reason"] is None


def test_1_due_and_active_fires_and_advances_weekly():
    schedule = _schedule(
        cadence="weekly", time_of_day="07:00:00", days_of_week=[0],  # Sunday
        next_run_at=_NOW - timedelta(minutes=5),
    )
    store = _stub_store([schedule])
    scheduler = sched.PatrolScheduler(store, drone_id="d1")

    scheduler.tick(now=_NOW)

    assert store.create_command.call_count == 1
    update_kwargs = store.update_patrol_schedule.call_args.kwargs
    # today (Sunday) 07:00 IST already passed -> next Sunday, not tomorrow
    assert update_kwargs["next_run_at"] == "2026-09-20T01:30:00+00:00"


# ── 2: future next_run_at -> nothing happens ────────────────────────────────

def test_2_future_next_run_at_does_nothing():
    schedule = _schedule(next_run_at=_NOW + timedelta(hours=1))
    store = _stub_store([schedule])
    scheduler = sched.PatrolScheduler(store, drone_id="d1")

    scheduler.tick(now=_NOW)

    store.create_command.assert_not_called()
    store.update_patrol_schedule.assert_not_called()


# ── 3: active=false -> nothing happens ──────────────────────────────────────

def test_3_inactive_does_nothing_even_if_past_due():
    schedule = _schedule(active=False, next_run_at=_NOW - timedelta(days=1))
    store = _stub_store([schedule])
    scheduler = sched.PatrolScheduler(store, drone_id="d1")

    scheduler.tick(now=_NOW)

    store.create_command.assert_not_called()
    store.update_patrol_schedule.assert_not_called()


# ── 4: daily next_occurrence, pure function ─────────────────────────────────

def test_4_daily_next_occurrence_before_and_after_today():
    schedule = {"cadence": "daily", "time_of_day": "07:00:00"}

    before = datetime(2026, 9, 13, 0, 0, 0, tzinfo=timezone.utc)  # 05:30 IST
    assert sched.next_occurrence(schedule, before) == datetime(
        2026, 9, 13, 1, 30, 0, tzinfo=timezone.utc
    )

    after = datetime(2026, 9, 13, 3, 0, 0, tzinfo=timezone.utc)  # 08:30 IST
    assert sched.next_occurrence(schedule, after) == datetime(
        2026, 9, 14, 1, 30, 0, tzinfo=timezone.utc
    )

    # exactly on a valid occurrence -> must return the NEXT one, not the same
    exact = datetime(2026, 9, 13, 1, 30, 0, tzinfo=timezone.utc)  # 07:00:00 IST
    assert sched.next_occurrence(schedule, exact) == datetime(
        2026, 9, 14, 1, 30, 0, tzinfo=timezone.utc
    )


# ── 5: weekly next_occurrence across a week boundary, pure function ────────

def test_5_weekly_next_occurrence_respects_days_of_week():
    # Sundays only. 2026-09-13 is a Sunday.
    schedule = {"cadence": "weekly", "time_of_day": "07:00:00", "days_of_week": [0]}

    before = datetime(2026, 9, 13, 0, 0, 0, tzinfo=timezone.utc)  # Sun 05:30 IST
    assert sched.next_occurrence(schedule, before) == datetime(
        2026, 9, 13, 1, 30, 0, tzinfo=timezone.utc
    )

    after = datetime(2026, 9, 13, 3, 0, 0, tzinfo=timezone.utc)  # Sun 08:30 IST, past
    assert sched.next_occurrence(schedule, after) == datetime(
        2026, 9, 20, 1, 30, 0, tzinfo=timezone.utc
    )

    # a mid-week reference with only Sundays configured must skip all the way
    # to the following Sunday, not fire "tomorrow"
    wednesday = datetime(2026, 9, 16, 3, 0, 0, tzinfo=timezone.utc)
    assert sched.next_occurrence(schedule, wednesday) == datetime(
        2026, 9, 20, 1, 30, 0, tzinfo=timezone.utc
    )


# ── 6: drone mid-mission -> skipped, reason set, next_run_at advances ───────

def test_6_drone_busy_skips_and_advances():
    schedule = _schedule(next_run_at=_NOW - timedelta(minutes=5))
    store = _stub_store(
        [schedule], active_missions=[{"id": _sid(), "status": "enroute"}]
    )
    scheduler = sched.PatrolScheduler(store, drone_id="d1")

    scheduler.tick(now=_NOW)

    store.create_command.assert_not_called()
    assert store.update_patrol_schedule.call_count == 1
    kwargs = store.update_patrol_schedule.call_args.kwargs
    assert "non-terminal mission" in kwargs["last_skipped_reason"]
    assert kwargs["next_run_at"] == "2026-09-14T01:30:00+00:00"


# ── 7: staleness -> skipped with a distinct reason, not launched late ──────

def test_7_stale_next_run_at_skipped_not_launched_late():
    stale_by = timedelta(seconds=config.SCHEDULER_MAX_STALENESS_S + 60)
    schedule = _schedule(next_run_at=_NOW - stale_by)
    store = _stub_store([schedule])
    scheduler = sched.PatrolScheduler(store, drone_id="d1")

    scheduler.tick(now=_NOW)

    store.create_command.assert_not_called()
    kwargs = store.update_patrol_schedule.call_args.kwargs
    assert "missed while GSS was down" in kwargs["last_skipped_reason"]
    assert kwargs["next_run_at"] == "2026-09-14T01:30:00+00:00"


# ── 8: two due in one tick, one drone -> only the first fires ──────────────

def test_8_two_due_same_tick_only_first_fires():
    s1 = _schedule(name="first", next_run_at=_NOW - timedelta(minutes=10))
    s2 = _schedule(name="second", next_run_at=_NOW - timedelta(minutes=5))
    store = _stub_store([s1, s2])
    scheduler = sched.PatrolScheduler(store, drone_id="d1")

    scheduler.tick(now=_NOW)

    assert store.create_command.call_count == 1
    assert store.create_command.call_args.kwargs["issued_by"] == f"scheduler:{s1['id']}"

    assert store.update_patrol_schedule.call_count == 2
    first_call, second_call = store.update_patrol_schedule.call_args_list
    assert first_call.kwargs["last_skipped_reason"] is None  # s1 fired
    assert "non-terminal mission" in second_call.kwargs["last_skipped_reason"]  # s2 skipped


# ── 9: Supabase unreachable -> tick does nothing, no crash, no writes ──────

def test_9_store_unreachable_does_nothing_no_crash():
    store = MagicMock()
    # R10: store.py's own retry logic already logged the failure and gave up;
    # an empty list is how the caller finds out. Indistinguishable here from
    # "no schedules exist", and both should be a no-op.
    store.get_active_patrol_schedules.return_value = []
    scheduler = sched.PatrolScheduler(store, drone_id="d1")

    scheduler.tick(now=_NOW)  # must not raise

    store.create_command.assert_not_called()
    store.update_patrol_schedule.assert_not_called()
    store.list_active_missions.assert_not_called()  # never even asked


# ── 10: SCHEDULER_ENABLED=false -> gss.scheduler never imported ────────────

def test_10_scheduler_disabled_main_behaves_like_phase_8(monkeypatch):
    from gss import main as gss_main

    source = inspect.getsource(gss_main)
    # the import must be deferred inside _make_scheduler (mirroring
    # _make_weather / _make_store / _make_intake), never at module level
    assert not re.search(
        r"^(from gss\.scheduler|import gss\.scheduler)", source, re.MULTILINE
    ), "gss.main must not import gss.scheduler at module level"

    monkeypatch.setattr(config, "SCHEDULER_ENABLED", False)
    fake_store = MagicMock()

    result = gss_main._make_scheduler(fake_store)

    assert result is None
