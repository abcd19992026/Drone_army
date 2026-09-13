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
"""

from __future__ import annotations

import inspect
import os
import re
from unittest.mock import MagicMock

os.environ.setdefault("SUPABASE_ENABLED", "false")
os.environ.setdefault("WEATHER_ENABLED", "false")
os.environ.setdefault("MEDIA_ENABLED", "false")

from gss import config
from gss.beacon import BeaconMonitor, BeaconPhase, beacon_phase
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
