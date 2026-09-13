"""Patrol Scheduler (v0.9): recurring flights on a timer.

This module's entire vocabulary is: read ``patrol_schedules``, write one row
to ``commands``. It never imports ``gss.link``, ``gss.mission``, or
``gss.safety``, and never transmits a MAVLink message (rule R11). A scheduled
flight is nothing more than an ordinary ``goto`` command -- it is picked up,
validated, safety-vetoed, and weather-gated by ``gss/commands.py`` exactly
like a phone-issued one. Zero special-casing.

Mirrors the pure/shell split used by ``weather.py`` and ``safety.py``:

  * :func:`next_occurrence` / :func:`due_schedules` -- pure. No I/O, no clock
    reads. Given data and a reference time, they return an answer.
  * :class:`PatrolScheduler` -- the networked shell. A background thread that
    ticks every ``config.SCHEDULER_TICK_S``, reads schedules through
    ``store.get_active_patrol_schedules()`` (R10: a dead Supabase logs a
    warning and the tick does nothing, never blocks, never crashes), and
    turns each due one into a single ``commands`` row.

No catch-up: if the GSS was down and a schedule's ``next_run_at`` is more
than ``config.SCHEDULER_MAX_STALENESS_S`` in the past, that occurrence is
skipped -- never queued, never launched late -- and ``next_run_at`` advances
to the next natural occurrence from now.

Timezone note: ``time_of_day`` / ``days_of_week`` are local Asia/Kolkata
wall-clock values (the schema's own comment says so). Rather than trust the
dock Pi's OS timezone setting -- wrong by exactly 5.5 hours if that is ever
UTC instead of IST -- :data:`SCHEDULE_TZ` does the local/UTC conversion
explicitly with ``zoneinfo``, independent of the host's configured timezone.
India Standard Time has no DST, so this conversion has no fold/gap
ambiguity to handle.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from datetime import time as time_cls
from datetime import timezone
from zoneinfo import ZoneInfo

from gss import config

log = logging.getLogger(__name__)

SCHEDULE_TZ = ZoneInfo("Asia/Kolkata")

# patrol_schedules.days_of_week: 0=Sunday .. 6=Saturday (per the migration).
# Python's date.weekday() is Monday=0..Sunday=6; convert between the two.
_MAX_WEEKLY_LOOKAHEAD_DAYS = 8  # a week + 1: guarantees a match is found


def _parse_time_of_day(value: object) -> time_cls:
    """Accept a ``datetime.time`` or a PostgREST ``"HH:MM:SS[.ffffff]"`` string."""
    if isinstance(value, time_cls):
        return value
    s = str(value).strip()
    for fmt in ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"scheduler: unparseable time_of_day {value!r}")


def _parse_ts(value: object) -> datetime:
    """Parse a PostgREST timestamptz string (or pass through a ``datetime``)
    to an aware UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ===========================================================================
# pure core
# ===========================================================================


def next_occurrence(schedule: dict, after: datetime) -> datetime:
    """Return the next fire time strictly after ``after``.

    Pure: no clock reads, no I/O. ``after`` must be timezone-aware (any zone;
    it is converted to :data:`SCHEDULE_TZ` internally). The result is
    timezone-aware, in UTC.

    ``schedule`` must supply:
      * ``"cadence"``: ``"daily"`` or ``"weekly"``
      * ``"time_of_day"``: a ``datetime.time`` or ``"HH:MM:SS"`` string,
        interpreted as local wall-clock time in :data:`SCHEDULE_TZ`
      * ``"days_of_week"`` (weekly only): a list of ints, 0=Sunday..6=Saturday

    Correct across a day boundary, a week boundary, and when ``after`` is
    exactly equal to a valid occurrence (returns the NEXT one, never the
    same one again).
    """
    if after.tzinfo is None:
        raise ValueError("next_occurrence: 'after' must be timezone-aware")

    cadence = schedule["cadence"]
    tod = _parse_time_of_day(schedule["time_of_day"])
    after_local = after.astimezone(SCHEDULE_TZ)

    if cadence == "daily":
        candidate = datetime.combine(after_local.date(), tod, tzinfo=SCHEDULE_TZ)
        if candidate <= after_local:
            candidate = datetime.combine(
                after_local.date() + timedelta(days=1), tod, tzinfo=SCHEDULE_TZ
            )
        return candidate.astimezone(timezone.utc)

    if cadence == "weekly":
        days = set(schedule.get("days_of_week") or ())
        if not days:
            raise ValueError("next_occurrence: weekly schedule with no days_of_week")
        for offset in range(_MAX_WEEKLY_LOOKAHEAD_DAYS):
            d = after_local.date() + timedelta(days=offset)
            dow = (d.weekday() + 1) % 7  # Mon=0..Sun=6 -> schema's Sun=0..Sat=6
            if dow not in days:
                continue
            candidate = datetime.combine(d, tod, tzinfo=SCHEDULE_TZ)
            if candidate > after_local:
                return candidate.astimezone(timezone.utc)
        raise AssertionError(
            "next_occurrence: no matching day within "
            f"{_MAX_WEEKLY_LOOKAHEAD_DAYS} days -- unreachable with a non-empty "
            "days_of_week"
        )

    raise ValueError(f"next_occurrence: unknown cadence {cadence!r}")


def due_schedules(schedules: list[dict], now: datetime) -> list[dict]:
    """Pure filter: active schedules whose next_run_at is at or before now."""
    return [
        s for s in schedules
        if s.get("active") and _parse_ts(s["next_run_at"]) <= now
    ]


# ===========================================================================
# the shell
# ===========================================================================


class PatrolScheduler:
    """Turns due ``patrol_schedules`` rows into ``commands`` rows, on a timer.

    Same shape as :class:`~gss.weather_feed.WeatherMonitor` /
    :class:`~gss.safety.SafetyMonitor`: a background thread with its own
    ``start()``/``close()``, ticking on its own interval, isolated from the
    MAVLink path by construction -- it never touches ``gss.link``.
    """

    def __init__(
        self,
        store,
        *,
        drone_id: str | None = None,
        dock_id: str | None = None,
        tick_s: float | None = None,
        max_staleness_s: float | None = None,
        default_alt_m: float | None = None,
    ) -> None:
        self._store = store
        self._drone_id = drone_id or config.DRONE_ID
        self._dock_id = dock_id or config.DOCK_ID
        self._tick_s = tick_s if tick_s is not None else config.SCHEDULER_TICK_S
        self._max_staleness_s = (
            max_staleness_s if max_staleness_s is not None
            else config.SCHEDULER_MAX_STALENESS_S
        )
        self._default_alt_m = (
            default_alt_m if default_alt_m is not None else config.ON_STATION_ALT
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle --

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="patrol-scheduler", daemon=True
        )
        self._thread.start()

    def close(self, timeout_s: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 -- R8: never let a tick crash the thread
                log.exception("scheduler: tick failed")
            self._stop.wait(self._tick_s)

    # -- the tick --

    def tick(self, now: datetime | None = None) -> None:
        """Run one scheduling pass. Safe to call directly (used by tests)."""
        now = now if now is not None else datetime.now(timezone.utc)

        schedules = self._store.get_active_patrol_schedules()
        if not schedules:
            # R10: an empty list also means "Supabase was unreachable" --
            # store.py already logged why. Either way there is nothing to do.
            return
        due = due_schedules(schedules, now)
        if not due:
            return

        drone_busy = bool(self._store.list_active_missions(self._drone_id))
        for schedule in due:
            if self._process_one(schedule, now, drone_busy=drone_busy):
                # Fired this tick -> the drone is busy for the REST of this
                # tick's due list too, even though the new mission row won't
                # exist until CommandIntake claims the command we just wrote.
                drone_busy = True

    def _process_one(self, schedule: dict, now: datetime, *, drone_busy: bool) -> bool:
        """Returns True if a command was fired for this schedule."""
        if drone_busy:
            self._skip(schedule, now, "drone already has a non-terminal mission")
            return False

        next_run_at = _parse_ts(schedule["next_run_at"])
        staleness_s = (now - next_run_at).total_seconds()
        if staleness_s > self._max_staleness_s:
            self._skip(
                schedule, now,
                f"missed while GSS was down ({staleness_s:.0f}s stale, "
                f"max {self._max_staleness_s:.0f}s)",
            )
            return False

        return self._fire(schedule, now)

    def _skip(self, schedule: dict, now: datetime, reason: str) -> None:
        schedule_id = schedule["id"]
        next_run = next_occurrence(schedule, now)
        log.info(
            "scheduler: schedule %s (%r) skipped -- %s; next run %s",
            schedule_id, schedule.get("name"), reason, next_run.isoformat(),
        )
        self._store.update_patrol_schedule(
            schedule_id,
            last_skipped_reason=reason,
            next_run_at=next_run.isoformat(),
        )

    def _fire(self, schedule: dict, now: datetime) -> bool:
        schedule_id = schedule["id"]
        target_alt = schedule.get("cruise_alt_m")
        if target_alt is None:
            target_alt = self._default_alt_m

        command_id = self._store.create_command(
            drone_id=self._drone_id,
            cmd_type="goto",
            target_lat=schedule["target_lat"],
            target_lon=schedule["target_lon"],
            target_alt_m=target_alt,
            params={"loiter_seconds": schedule.get("loiter_seconds")},
            issued_by=f"scheduler:{schedule_id}",
        )
        if command_id is None:
            log.error(
                "scheduler: schedule %s (%r) due but the command could not be "
                "written; next_run_at left unchanged, will retry next tick",
                schedule_id, schedule.get("name"),
            )
            return False

        next_run = next_occurrence(schedule, now)
        self._store.update_patrol_schedule(
            schedule_id,
            last_run_at=now.isoformat(),
            last_skipped_reason=None,
            next_run_at=next_run.isoformat(),
        )
        log.info(
            "scheduler: schedule %s (%r) fired -> command %s; next run %s",
            schedule_id, schedule.get("name"), command_id, next_run.isoformat(),
        )
        return True
