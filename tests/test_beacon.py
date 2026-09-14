"""tests/test_beacon.py -- Phase 12 verification suite.

Tests gss/beacon.py (Find-My-Drone beacon) and gss/payload.py in isolation:
a MagicMock stands in for TelemetryStore and a plain MagicMock snapshot for
TelemetrySnapshot, so none of this needs a real Supabase or a MAVLink link.
Mirrors tests/test_scheduler.py's Phase 9 pytest style.

Verification items (per the Phase 12 spec):
  1  link_up=True at any elapsed time -> always OFF.
  2  seconds_since_link_lost below activate_delay_s -> PENDING, no actuator
     calls.
  3  Crossing activate_delay_s -> CONTINUOUS, spotlight_on + siren_on called
     exactly once (not re-called every tick while phase is unchanged).
  4  Past activate_delay_s + continuous_s -> PERIODIC, correctly alternates
     ON/OFF at the configured intervals over several simulated ticks.
  5  Link recovers mid-CONTINUOUS or mid-PERIODIC -> immediate OFF,
     spotlight_off + siren_off called, state resets (a later re-loss starts
     the timer from zero, not where it left off).
  6  get_system_config() unreachable/missing -> BeaconMonitor falls back to
     the hardcoded config defaults, doesn't crash, reads happen once at
     construction (never per-tick).
  7  An active mission exists during a phase transition -> exactly one
     mission_events row written with the right event name; no active
     mission -> no mission_events write attempted, no crash.
  8  BEACON_ENABLED=false -> gss.beacon never imported, gss.main behaves
     identically to Phase 11.
  9  PAYLOAD_HARDWARE_ENABLED=true with no real driver implemented ->
     RealPayloadActuator's NotImplementedError is caught by the monitor's
     tick try/except, logged, and does not crash the loop.

(Item 10 -- all earlier suites still pass -- is a full-repo `pytest tests/`
run, not a test in this file.)

Phase 13 (manual "find_my_drone" trigger) verification items:
  1  trigger_manual(60) with link currently up -> phase becomes MANUAL,
     spotlight_on + siren_on called exactly once.
  2  Calling trigger_manual again before the first expires -> end-time
     extends, no duplicate actuator calls (still MANUAL, no transition).
  3  Manual duration expires, link is up -> phase falls back to OFF,
     spotlight_off + siren_off called.
  4  Manual duration expires, link has been down long enough to qualify for
     CONTINUOUS -> phase becomes CONTINUOUS, no redundant off/on toggle
     (MANUAL and CONTINUOUS are both "on").
  5  gss/commands.py's get_system_config read for "beacon.manual_trigger_s"
     unreachable -> falls back to the hardcoded default, doesn't crash.
  6  commands.py: find_my_drone with no active mission -> command marked
     done, no mission_events write attempted.
  7  commands.py: find_my_drone with an active mission -> exactly one
     mission_events row with event="beacon_manual_triggered".
  8  commands.py: beacon_trigger callable raises -> command marked
     rejected with a reason, a later ordinary command still processes.
  9  commands.py: beacon_trigger is None (beacon disabled) -> find_my_drone
     rejected with "beacon not enabled", not a crash or a silent no-op.

Items 5-9 exercise gss.commands.CommandIntake._handle_find_my_drone
directly (same MagicMock-store, no-thread style as the Phase 11
independence tests in tests/test_alerts.py) rather than the live-Supabase
suite in tests/test_commands.py, which only gets one confirming assertion
that a real find_my_drone command reaches 'done'.
"""

from __future__ import annotations

import inspect
import os
import re
import time as _time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

os.environ.setdefault("SUPABASE_ENABLED", "false")
os.environ.setdefault("WEATHER_ENABLED", "false")
os.environ.setdefault("MEDIA_ENABLED", "false")

from gss import config
from gss.beacon import BeaconMonitor, BeaconPhase, beacon_phase
from gss.commands import CommandIntake
from gss.payload import FakePayloadActuator, RealPayloadActuator

# Small, fast timings shared by most tests: activate at 10s, continuous for
# 20s (i.e. periodic starts at elapsed=30s), periodic on for 5s, off for 10s
# (cycle length 15s).
_ACTIVATE = 10.0
_CONTINUOUS = 20.0
_PERIODIC_ON = 5.0
_PERIODIC_INTERVAL = 10.0


def _snap(connected: bool):
    s = MagicMock()
    s.connected = connected
    return s


def _monitor(snapshot_source, *, actuator=None, store=None, event_sink=None, **timing):
    kwargs = dict(
        activate_delay_s=_ACTIVATE, continuous_s=_CONTINUOUS,
        periodic_interval_s=_PERIODIC_INTERVAL, periodic_on_s=_PERIODIC_ON,
    )
    kwargs.update(timing)
    return BeaconMonitor(
        snapshot_source, store=store, event_sink=event_sink,
        actuator=actuator or FakePayloadActuator(), **kwargs,
    )


def _on_calls(actuator):
    return [c for c in actuator.calls if c[0] in ("spotlight_on", "siren_on")]


def _off_calls(actuator):
    return [c for c in actuator.calls if c[0] in ("spotlight_off", "siren_off")]


# ── pure core ────────────────────────────────────────────────────────────

def test_pure_beacon_phase_boundaries():
    assert beacon_phase(None, 10, 20, 10, 5, 0) == BeaconPhase.OFF
    assert beacon_phase(0, 10, 20, 10, 5, 0) == BeaconPhase.PENDING
    assert beacon_phase(9.9, 10, 20, 10, 5, 0) == BeaconPhase.PENDING
    assert beacon_phase(10, 10, 20, 10, 5, 0) == BeaconPhase.CONTINUOUS
    assert beacon_phase(29.9, 10, 20, 10, 5, 19.9) == BeaconPhase.CONTINUOUS
    assert beacon_phase(30, 10, 20, 10, 5, 0) == BeaconPhase.PERIODIC_ON
    assert beacon_phase(34, 10, 20, 10, 5, 4) == BeaconPhase.PERIODIC_ON
    assert beacon_phase(36, 10, 20, 10, 5, 6) == BeaconPhase.PERIODIC_OFF
    # a full cycle (15s) later, back to ON
    assert beacon_phase(45, 10, 20, 10, 5, 15) == BeaconPhase.PERIODIC_ON


# ── 1: link up -> always OFF ────────────────────────────────────────────

def test_1_link_up_always_off():
    snap = _snap(True)
    monitor = _monitor(lambda: snap)

    monitor.tick(now_mono=0.0)
    assert monitor.phase == BeaconPhase.OFF
    monitor.tick(now_mono=10_000.0)
    assert monitor.phase == BeaconPhase.OFF


# ── 2: below activate_delay_s -> PENDING, no actuator calls ────────────

def test_2_pending_no_actuator_calls():
    snap = _snap(False)
    actuator = FakePayloadActuator()
    monitor = _monitor(lambda: snap, actuator=actuator)

    monitor.tick(now_mono=0.0)   # link loss starts here
    monitor.tick(now_mono=5.0)   # elapsed 5s < activate_delay_s (10s)

    assert monitor.phase == BeaconPhase.PENDING
    assert actuator.calls == []


# ── 3: crossing activate_delay_s -> CONTINUOUS, activates exactly once ──

def test_3_continuous_activates_exactly_once():
    snap = _snap(False)
    actuator = FakePayloadActuator()
    monitor = _monitor(lambda: snap, actuator=actuator)

    monitor.tick(now_mono=0.0)
    monitor.tick(now_mono=15.0)  # elapsed 15s -> CONTINUOUS
    assert monitor.phase == BeaconPhase.CONTINUOUS
    assert len(_on_calls(actuator)) == 2  # spotlight_on + siren_on, once each

    # several more ticks while still CONTINUOUS -- no re-activation
    monitor.tick(now_mono=16.0)
    monitor.tick(now_mono=20.0)
    monitor.tick(now_mono=29.0)
    assert len(_on_calls(actuator)) == 2


# ── 4: PERIODIC alternates ON/OFF at the configured intervals ──────────

def test_4_periodic_alternates_on_off():
    snap = _snap(False)
    actuator = FakePayloadActuator()
    monitor = _monitor(lambda: snap, actuator=actuator)

    monitor.tick(now_mono=0.0)
    monitor.tick(now_mono=15.0)  # CONTINUOUS (2 "on" calls so far)
    assert len(_on_calls(actuator)) == 2

    monitor.tick(now_mono=30.0)  # periodic starts, now_in_cycle=0 -> ON (unchanged, already on)
    assert monitor.phase == BeaconPhase.PERIODIC_ON
    assert len(_on_calls(actuator)) == 2  # still on -- no new call, CONTINUOUS->PERIODIC_ON is on->on

    monitor.tick(now_mono=34.0)  # now_in_cycle=4 -- still ON
    assert monitor.phase == BeaconPhase.PERIODIC_ON
    assert len(_on_calls(actuator)) == 2

    monitor.tick(now_mono=36.0)  # now_in_cycle=6 -- OFF (real toggle)
    assert monitor.phase == BeaconPhase.PERIODIC_OFF
    assert len(_off_calls(actuator)) == 2

    monitor.tick(now_mono=44.0)  # now_in_cycle=14 -- still OFF
    assert monitor.phase == BeaconPhase.PERIODIC_OFF
    assert len(_off_calls(actuator)) == 2

    monitor.tick(now_mono=45.0)  # now_in_cycle=15 -> wraps to 0 -- ON again (real toggle)
    assert monitor.phase == BeaconPhase.PERIODIC_ON
    assert len(_on_calls(actuator)) == 4  # a second activation cycle


# ── 5: link recovery -> immediate OFF, resets, later loss starts at zero ──

def test_5_link_recovery_resets_state():
    snap = _snap(False)
    actuator = FakePayloadActuator()
    monitor = _monitor(lambda: snap, actuator=actuator)

    monitor.tick(now_mono=0.0)
    monitor.tick(now_mono=15.0)  # CONTINUOUS
    assert monitor.phase == BeaconPhase.CONTINUOUS

    snap.connected = True
    monitor.tick(now_mono=16.0)
    assert monitor.phase == BeaconPhase.OFF
    assert len(_off_calls(actuator)) == 2

    # a later, fresh loss must start the timer from zero, not resume
    snap.connected = False
    monitor.tick(now_mono=17.0)
    assert monitor.phase == BeaconPhase.PENDING
    monitor.tick(now_mono=20.0)  # only 3s since the NEW loss -- still PENDING
    assert monitor.phase == BeaconPhase.PENDING
    assert len(_on_calls(actuator)) == 2  # unchanged since the first activation


def test_5b_link_recovery_mid_periodic():
    snap = _snap(False)
    actuator = FakePayloadActuator()
    monitor = _monitor(lambda: snap, actuator=actuator)

    monitor.tick(now_mono=0.0)
    monitor.tick(now_mono=15.0)  # CONTINUOUS -- turns on
    assert monitor.phase == BeaconPhase.CONTINUOUS
    assert len(_on_calls(actuator)) == 2

    monitor.tick(now_mono=36.0)  # PERIODIC_OFF (CONTINUOUS->PERIODIC_OFF is on->off)
    assert monitor.phase == BeaconPhase.PERIODIC_OFF
    assert len(_off_calls(actuator)) == 2

    snap.connected = True
    monitor.tick(now_mono=37.0)
    assert monitor.phase == BeaconPhase.OFF
    # PERIODIC_OFF -> OFF is off->off: no new actuator call needed
    assert len(_off_calls(actuator)) == 2  # unchanged from the earlier ON->OFF toggle


# ── 6: system_config unreachable/missing -> falls back, reads once ────

def test_6a_system_config_fallback_used_and_read_once():
    store = MagicMock()
    # Mirrors store.py's own R10 contract: a bad read returns the default
    # passed in, never raises.
    store.get_system_config.side_effect = lambda key, default: default
    snap = _snap(False)

    monitor = BeaconMonitor(lambda: snap, store=store, actuator=FakePayloadActuator())

    assert monitor._activate_delay_s == config.BEACON_ACTIVATE_DELAY_S_DEFAULT
    assert monitor._continuous_s == config.BEACON_CONTINUOUS_S_DEFAULT
    assert monitor._periodic_interval_s == config.BEACON_PERIODIC_INTERVAL_S_DEFAULT
    assert store.get_system_config.call_count == 3

    monitor.tick(now_mono=0.0)
    monitor.tick(now_mono=1.0)
    monitor.tick(now_mono=2.0)
    assert store.get_system_config.call_count == 3  # never re-read per tick


def test_6b_system_config_raises_falls_back_no_crash():
    store = MagicMock()
    store.get_system_config.side_effect = RuntimeError("supabase is down")
    snap = _snap(False)

    monitor = BeaconMonitor(lambda: snap, store=store, actuator=FakePayloadActuator())  # must not raise

    assert monitor._activate_delay_s == config.BEACON_ACTIVATE_DELAY_S_DEFAULT
    assert monitor._continuous_s == config.BEACON_CONTINUOUS_S_DEFAULT
    assert monitor._periodic_interval_s == config.BEACON_PERIODIC_INTERVAL_S_DEFAULT


# ── 7: mission_events written only when a mission is active ───────────

def test_7a_active_mission_gets_exactly_one_mission_event():
    store = MagicMock()
    store.list_active_missions.return_value = [{"id": "mission-1", "status": "on_station"}]
    event_sink = MagicMock()
    snap = _snap(False)
    monitor = _monitor(lambda: snap, store=store, event_sink=event_sink)

    monitor.tick(now_mono=0.0)
    monitor.tick(now_mono=15.0)  # -> CONTINUOUS, activates

    event_sink.assert_called_once_with(
        "mission-1", "beacon_activated", {"phase": "CONTINUOUS"}, sync=True,
    )


def test_7b_no_active_mission_no_write_no_crash():
    store = MagicMock()
    store.list_active_missions.return_value = []
    event_sink = MagicMock()
    snap = _snap(False)
    monitor = _monitor(lambda: snap, store=store, event_sink=event_sink)

    monitor.tick(now_mono=0.0)
    monitor.tick(now_mono=15.0)  # -> CONTINUOUS, activates (no mission to log against)

    event_sink.assert_not_called()


def test_7c_store_list_active_missions_raises_no_crash():
    store = MagicMock()
    store.list_active_missions.side_effect = RuntimeError("unreachable")
    event_sink = MagicMock()
    snap = _snap(False)
    monitor = _monitor(lambda: snap, store=store, event_sink=event_sink)

    monitor.tick(now_mono=0.0)
    monitor.tick(now_mono=15.0)  # must not raise

    event_sink.assert_not_called()


# ── 8: BEACON_ENABLED=false -> gss.beacon never imported ──────────────

def test_8a_beacon_disabled_main_never_imports_at_module_level():
    from gss import main as gss_main

    source = inspect.getsource(gss_main)
    assert not re.search(
        r"^(from gss\.beacon|import gss\.beacon)", source, re.MULTILINE
    ), "gss.main must not import gss.beacon at module level"


def test_8b_make_beacon_returns_none_when_disabled(monkeypatch):
    from gss import main as gss_main

    monkeypatch.setattr(config, "BEACON_ENABLED", False)
    result = gss_main._make_beacon(MagicMock(), MagicMock())
    assert result is None


# ── Phase 14a: drones.beacon_active mirrors the phase's on/off-ness ────
#
# store.update_drone(beacon_active=...) must fire exactly on the same
# boundary crossings as the actuator calls (on->off / off->on), not on
# every phase change (e.g. CONTINUOUS->PERIODIC_ON is on->on: no call).

def _beacon_active_calls(store):
    return [
        c.kwargs["beacon_active"]
        for c in store.update_drone.call_args_list
        if "beacon_active" in c.kwargs
    ]


def test_p14a_entering_continuous_sets_beacon_active_true():
    store = MagicMock()
    store.list_active_missions.return_value = []
    snap = _snap(False)
    monitor = _monitor(lambda: snap, store=store)

    monitor.tick(now_mono=0.0)
    monitor.tick(now_mono=15.0)  # PENDING -> CONTINUOUS (off->on)

    assert monitor.phase == BeaconPhase.CONTINUOUS
    assert _beacon_active_calls(store) == [True]


def test_p14a_periodic_on_off_toggles_beacon_active():
    store = MagicMock()
    store.list_active_missions.return_value = []
    snap = _snap(False)
    monitor = _monitor(lambda: snap, store=store)

    monitor.tick(now_mono=0.0)
    monitor.tick(now_mono=15.0)  # -> CONTINUOUS: beacon_active=True
    monitor.tick(now_mono=30.0)  # -> PERIODIC_ON (on->on): no new call
    assert _beacon_active_calls(store) == [True]

    monitor.tick(now_mono=36.0)  # -> PERIODIC_OFF (on->off)
    assert monitor.phase == BeaconPhase.PERIODIC_OFF
    assert _beacon_active_calls(store) == [True, False]

    monitor.tick(now_mono=45.0)  # -> PERIODIC_ON (off->on)
    assert monitor.phase == BeaconPhase.PERIODIC_ON
    assert _beacon_active_calls(store) == [True, False, True]


def test_p14a_link_recovery_sets_beacon_active_false():
    store = MagicMock()
    store.list_active_missions.return_value = []
    snap = _snap(False)
    monitor = _monitor(lambda: snap, store=store)

    monitor.tick(now_mono=0.0)
    monitor.tick(now_mono=15.0)  # -> CONTINUOUS: beacon_active=True
    assert _beacon_active_calls(store) == [True]

    snap.connected = True
    monitor.tick(now_mono=16.0)  # -> OFF (on->off)

    assert monitor.phase == BeaconPhase.OFF
    assert _beacon_active_calls(store) == [True, False]


def test_p14a_manual_trigger_sets_beacon_active_true_and_false():
    store = MagicMock()
    store.list_active_missions.return_value = []
    snap = _snap(True)
    monitor = _monitor(lambda: snap, store=store)

    with patch("gss.beacon.time.monotonic", return_value=100.0):
        monitor.trigger_manual(60.0)  # until 160.0
    monitor.tick(now_mono=101.0)  # OFF -> MANUAL (off->on)
    assert monitor.phase == BeaconPhase.MANUAL
    assert _beacon_active_calls(store) == [True]

    monitor.tick(now_mono=161.0)  # expired, link up -> OFF (on->off)
    assert monitor.phase == BeaconPhase.OFF
    assert _beacon_active_calls(store) == [True, False]


def test_p14a_no_store_no_crash():
    snap = _snap(False)
    monitor = _monitor(lambda: snap, store=None)

    monitor.tick(now_mono=0.0)
    monitor.tick(now_mono=15.0)  # must not raise despite no store

    assert monitor.phase == BeaconPhase.CONTINUOUS


def test_p14a_store_update_drone_raises_no_crash():
    store = MagicMock()
    store.list_active_missions.return_value = []
    store.update_drone.side_effect = RuntimeError("unreachable")
    snap = _snap(False)
    monitor = _monitor(lambda: snap, store=store)

    monitor.tick(now_mono=0.0)
    monitor.tick(now_mono=15.0)  # must not raise despite update_drone failing

    assert monitor.phase == BeaconPhase.CONTINUOUS


# ── 9: a prematurely-real actuator's NotImplementedError is caught ────

def test_9_real_actuator_raises_caught_never_crashes():
    snap = _snap(False)
    monitor = _monitor(lambda: snap, actuator=RealPayloadActuator())

    monitor.tick(now_mono=0.0)
    monitor.tick(now_mono=15.0)  # would call spotlight_on() -> NotImplementedError
    monitor.tick(now_mono=16.0)  # retried, still raises -- still must not crash

    # The transition never successfully completed (R8: don't lie about
    # state when the actuator call that defines it failed) -- phase stays
    # at the last successfully-reached one.
    assert monitor.phase == BeaconPhase.PENDING


# ===========================================================================
# Phase 13 -- manual "find_my_drone" trigger
# ===========================================================================
#
# trigger_manual() stamps its end-time from the REAL time.monotonic(), while
# these tests otherwise drive tick() with synthetic now_mono values. To keep
# both readings on one consistent imaginary timeline, time.monotonic() is
# patched (via gss.beacon.time.monotonic) only for the duration of each
# trigger_manual() call, to a value chosen on that same synthetic timeline.

# ── P13-1: trigger_manual with link up -> MANUAL, activates once ──────

def test_p13_1_trigger_manual_with_link_up_activates():
    snap = _snap(True)
    actuator = FakePayloadActuator()
    monitor = _monitor(lambda: snap, actuator=actuator)

    monitor.tick(now_mono=0.0)
    assert monitor.phase == BeaconPhase.OFF

    with patch("gss.beacon.time.monotonic", return_value=100.0):
        monitor.trigger_manual(60.0)  # until 160.0
    monitor.tick(now_mono=101.0)

    assert monitor.phase == BeaconPhase.MANUAL
    assert len(_on_calls(actuator)) == 2


# ── P13-2: a second trigger before expiry extends, no duplicate calls ──

def test_p13_2_trigger_manual_again_extends_no_duplicate_calls():
    snap = _snap(True)
    actuator = FakePayloadActuator()
    monitor = _monitor(lambda: snap, actuator=actuator)

    with patch("gss.beacon.time.monotonic", return_value=100.0):
        monitor.trigger_manual(60.0)  # until 160.0
    monitor.tick(now_mono=110.0)
    assert monitor.phase == BeaconPhase.MANUAL
    assert len(_on_calls(actuator)) == 2

    with patch("gss.beacon.time.monotonic", return_value=150.0):
        monitor.trigger_manual(60.0)  # until 210.0 -- extended, not stacked (150+60, not 160+60)
    # 180 is past the OLD end-time (160) but well within the NEW one (210)
    monitor.tick(now_mono=180.0)

    assert monitor.phase == BeaconPhase.MANUAL
    assert len(_on_calls(actuator)) == 2  # unchanged -- no transition, no duplicate call


# ── P13-3: expiry with link up -> falls back to OFF ────────────────────

def test_p13_3_manual_expires_link_up_falls_back_to_off():
    snap = _snap(True)
    actuator = FakePayloadActuator()
    monitor = _monitor(lambda: snap, actuator=actuator)

    with patch("gss.beacon.time.monotonic", return_value=100.0):
        monitor.trigger_manual(60.0)  # until 160.0
    monitor.tick(now_mono=110.0)
    assert monitor.phase == BeaconPhase.MANUAL

    monitor.tick(now_mono=161.0)  # past 160 -- expired, link is up -> OFF

    assert monitor.phase == BeaconPhase.OFF
    assert len(_off_calls(actuator)) == 2


# ── P13-4: expiry with link down long enough -> CONTINUOUS, no redundant toggle ──

def test_p13_4_manual_expires_link_down_falls_through_to_continuous():
    snap = _snap(False)  # link down throughout
    actuator = FakePayloadActuator()
    monitor = _monitor(lambda: snap, actuator=actuator)

    monitor.tick(now_mono=0.0)  # link-loss timer starts at 0

    with patch("gss.beacon.time.monotonic", return_value=1.0):
        monitor.trigger_manual(5.0)  # until 6.0
    monitor.tick(now_mono=2.0)
    assert monitor.phase == BeaconPhase.MANUAL
    assert len(_on_calls(actuator)) == 2

    # manual expired (20 >= 6); elapsed since link loss is 20s -- within
    # [activate_delay=10, +continuous=30) -> CONTINUOUS
    monitor.tick(now_mono=20.0)

    assert monitor.phase == BeaconPhase.CONTINUOUS
    assert len(_on_calls(actuator)) == 2  # MANUAL->CONTINUOUS is on->on: no new call


# ── commands.py: _handle_find_my_drone (Phase 13 items 5-9) ────────────

def _find_my_drone_cmd(command_id: str = "cmd-fmd") -> dict:
    return {
        "id": command_id,
        "type": "find_my_drone",
        "issued_by": "phone",
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": None,
    }


def _intake(*, store=None, beacon_trigger=None):
    store = store if store is not None else MagicMock()
    snap = MagicMock()
    return CommandIntake(
        store, lambda: snap, realtime_enabled=False, beacon_trigger=beacon_trigger,
    )


# ── P13-5: system_config unreachable for the manual-duration key ──────

def test_p13_5_system_config_unreachable_for_manual_duration_falls_back():
    store = MagicMock()
    store.get_system_config.side_effect = RuntimeError("unreachable")
    beacon_trigger = MagicMock()
    intake = _intake(store=store, beacon_trigger=beacon_trigger)
    cmd = _find_my_drone_cmd()

    intake._handle_find_my_drone(cmd, datetime.now(timezone.utc))  # must not raise

    beacon_trigger.assert_called_once_with(config.BEACON_MANUAL_TRIGGER_S_DEFAULT)
    store.update_command_status.assert_called_once_with(
        cmd["id"], "done", rejected_reason=None,
    )


# ── P13-6: no active mission -> done, no mission_events write ─────────

def test_p13_6_no_active_mission_no_mission_event():
    store = MagicMock()
    store.get_system_config.return_value = 45.0
    beacon_trigger = MagicMock()
    intake = _intake(store=store, beacon_trigger=beacon_trigger)
    cmd = _find_my_drone_cmd()

    intake._handle_find_my_drone(cmd, datetime.now(timezone.utc))

    beacon_trigger.assert_called_once_with(45.0)
    store.log_event.assert_not_called()
    store.update_command_status.assert_called_once_with(
        cmd["id"], "done", rejected_reason=None,
    )


# ── P13-7: active mission -> exactly one beacon_manual_triggered event ─

def test_p13_7_active_mission_gets_beacon_manual_triggered_event():
    from gss.commands import _ActiveMission
    from gss.executor import MissionPhase

    store = MagicMock()
    store.get_system_config.return_value = 30.0
    beacon_trigger = MagicMock()
    intake = _intake(store=store, beacon_trigger=beacon_trigger)
    cmd = _find_my_drone_cmd()

    intake._active = _ActiveMission(
        mission_id="mission-x", command_id="other-cmd", command={"type": "summon"},
        executor=MagicMock(), started_mono=_time.monotonic(),
        last_phase=MissionPhase.QUEUED,
    )

    intake._handle_find_my_drone(cmd, datetime.now(timezone.utc))

    store.log_event.assert_called_once_with(
        "mission-x", "beacon_manual_triggered",
        detail={"duration_s": 30.0, "command_id": cmd["id"]}, sync=True,
    )


# ── P13-8: beacon_trigger raises -> rejected, intake keeps working ─────

def test_p13_8_beacon_trigger_raises_rejects_and_intake_keeps_running():
    store = MagicMock()
    store.get_system_config.return_value = 30.0
    beacon_trigger = MagicMock(side_effect=RuntimeError("actuator fault"))
    intake = _intake(store=store, beacon_trigger=beacon_trigger)
    cmd = _find_my_drone_cmd()

    intake._handle_find_my_drone(cmd, datetime.now(timezone.utc))  # must not raise

    store.update_command_status.assert_called_once()
    args, kwargs = store.update_command_status.call_args
    assert args[1] == "rejected"
    assert "internal error" in (kwargs.get("rejected_reason") or "")

    # a later command still processes fine -- the fault didn't wedge intake
    store.reset_mock()
    beacon_trigger.side_effect = None
    cmd2 = _find_my_drone_cmd(command_id="cmd-fmd-2")
    intake._handle_find_my_drone(cmd2, datetime.now(timezone.utc))
    store.update_command_status.assert_called_once_with(
        cmd2["id"], "done", rejected_reason=None,
    )


# ── P13-9: beacon_trigger is None -> rejected "beacon not enabled" ─────

def test_p13_9_beacon_trigger_none_rejected_not_enabled():
    store = MagicMock()
    intake = _intake(store=store, beacon_trigger=None)
    cmd = _find_my_drone_cmd()

    intake._handle_find_my_drone(cmd, datetime.now(timezone.utc))

    store.update_command_status.assert_called_once_with(
        cmd["id"], "rejected", rejected_reason="beacon not enabled",
    )
    store.get_system_config.assert_not_called()  # short-circuited before reading config
