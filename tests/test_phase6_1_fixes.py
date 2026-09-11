"""Phase 6.1 verification -- the two defects found against real ArduPilot SITL.

    1. R2 HOLD used to idle at the too-close position forever (no margin on
       the standoff target, no way to release once ordered). Fixed by:
         a) config.STANDOFF_MARGIN_M added on top of LATERAL_OFFSET_M when
            mission.py computes the standoff point;
         b) MavlinkExecutor._resolve_r2_hold() recomputes the standoff point
            against the CURRENT human position/wind and flies to it, and
            escalates to RTL_NOW after config.R2_ESCAPE_TIMEOUT_S if that
            never resolves;
         c) a companion fix this depends on to mean anything: a HOLD/DESCEND
            directive can now be RELEASED (on_safety_action("ALLOW"/"WARN",
            ...)) once safety.py's verdict returns to nominal -- previously
            no directive could ever rank back down to NONE, so "resolving"
            the distance would still have left the mission stuck.

    2. BATTERY_CELLS defaulted to 6 against a real 3S pack, firing CRITICAL
       on a fully healthy aircraft. Fixed by safety.py's
       check_battery_cell_configuration(): a ONE-TIME, startup-only,
       disarmed-and-settled sanity check that hard-REJECTs every flight
       command (SafetyMonitor.evaluate_command) if the implied cell count
       disagrees with BATTERY_CELLS by more than
       BATTERY_CELL_MISMATCH_TOLERANCE cells' worth of voltage.

Uses the same fake-vehicle harness as tests/test_mission.py (Rig / IntakeRig)
-- real SafetyMonitor, real CommandIntake, real MavlinkExecutor, against a
kinematic simulator, not Mission Planner SITL. See that file's module
docstring for what that does and doesn't cover; the real-SITL scenario (a
standoff point 11 m from the human purely from ArduPilot's own arrival
precision) is simulated here by teleporting the fake vehicle's reported
position, not reproduced from real GPS noise.

    python -m tests.test_phase6_1_fixes
"""

from __future__ import annotations

import threading
import time

# tests.test_mission MUST be imported before gss.config is ever imported by
# this process: its module-level code sets ALLOW_VEHICLE_CONTROL=true and
# WEATHER_ENABLED=false via os.environ, and config.py reads env vars ONCE, at
# its own import time -- import gss.config first here and both real-flight
# and no-live-weather-API behaviour silently fail to take effect.
from tests.test_mission import (  # noqa: E402 -- reuses the real-flight harness
    _HOME,
    IntakeRig,
    Rig,
    _standoff_target_for,
    _wait_for,
)
from gss import config, mission  # noqa: E402
from gss.safety import SafetyAction, SafetyMonitor  # noqa: E402
from tests.test_safety import _Box, snap  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  ::  {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# Fix 1a -- standoff point targets LATERAL_OFFSET_M + STANDOFF_MARGIN_M
# ---------------------------------------------------------------------------


def test_standoff_has_a_margin() -> None:
    """Verification #1: the standoff point is LATERAL_OFFSET_M +
    STANDOFF_MARGIN_M from the human, not the bare legal minimum."""
    _check("fix1a: STANDOFF_MARGIN_M is configured and positive",
           config.STANDOFF_MARGIN_M > 0, str(config.STANDOFF_MARGIN_M))

    human = (_HOME[0] + 0.001, _HOME[1] + 0.0008)
    expected = config.LATERAL_OFFSET_M + config.STANDOFF_MARGIN_M
    lat, lon, basis = mission.standoff_point(
        human[0], human[1], dock_lat=_HOME[0], dock_lon=_HOME[1],
        offset_m=expected, wind_from_deg=None,
    )
    dist = mission._haversine_m(human[0], human[1], lat, lon)
    _check("fix1a: standoff_point(offset_m=LATERAL_OFFSET_M+STANDOFF_MARGIN_M) "
           "is exactly that far from the human",
           abs(dist - expected) < 0.01, f"dist={dist:.3f}m expected={expected:.2f}m")
    _check("fix1a: that is strictly more than LATERAL_OFFSET_M alone",
           dist > config.LATERAL_OFFSET_M + 0.5,
           f"dist={dist:.3f}m LATERAL_OFFSET_M={config.LATERAL_OFFSET_M}")

    # Live: MavlinkExecutor._compute_standoff() actually uses the margin, not
    # just the bare offset -- this is the exact call site the real-SITL
    # defect came from.
    rig = Rig()
    try:
        cmd = rig.command(human=human, loiter_s=3.0)
        ex = rig.executor(cmd)
        ex.start()
        _wait_for(lambda: ex.standoff is not None, 30)
        p = ex.standoff
        _check("fix1a (live): MavlinkExecutor._compute_standoff() targets the "
               "margin, not the bare LATERAL_OFFSET_M floor", p is not None, str(p))
        if p is not None:
            live_dist = mission._haversine_m(human[0], human[1], p[0], p[1])
            _check("fix1a (live): the live standoff distance matches "
                   "LATERAL_OFFSET_M + STANDOFF_MARGIN_M",
                   abs(live_dist - expected) < 0.5,
                   f"live_dist={live_dist:.1f}m expected={expected:.1f}m")
        ex.abort("test done")
        _wait_for(lambda: ex.poll().value in ("aborted", "landed"), 30)
    finally:
        rig.close()


# ---------------------------------------------------------------------------
# Fix 1b -- R2 HOLD actively recomputes and repositions, and releases once
# resolved (verification #2)
# ---------------------------------------------------------------------------


def test_r2_hold_recomputes_and_repositions() -> None:
    """Verification #2: an R2 HOLD firing at 11 m (inside the boundary, the
    exact real-SITL arrival-noise scenario) makes the executor recompute the
    standoff point and fly toward it -- the distance to the human genuinely
    INCREASES over successive ticks, not merely re-sent from a fixed spot --
    and releases back to the ordinary mission once resolved."""
    rig = IntakeRig()
    # This test deliberately slows the sim to a crawl so the recompute-and-
    # reposition response is observable tick by tick -- give the R2 escape
    # timeout correspondingly more headroom, or the (much shorter, real-speed)
    # default would fire the ESCALATION path (test #3's scenario) before this
    # test's natural resolution ever gets a chance to happen.
    saved_timeout = config.R2_ESCAPE_TIMEOUT_S
    config.R2_ESCAPE_TIMEOUT_S = 60.0
    try:
        human = (_HOME[0] + 0.0012, _HOME[1] + 0.0010)
        target = _standoff_target_for(human)
        cid = rig.submit(target_lat=target[0], target_lon=target[1],
                          params={"human_lat": human[0], "human_lon": human[1],
                                  "loiter_seconds": 5})
        mid_seen = _wait_for(lambda: rig.store.commands[cid]["mission_id"] is not None, 10)
        mid = rig.store.commands[cid]["mission_id"]
        _check("r2-1: mission accepted", mid_seen, str(rig.store.commands[cid]))
        on_station = _wait_for(lambda: rig.store.missions[mid]["status"] == "on_station", 30)
        _check("r2-2: mission reached on_station before the R2 scenario is injected",
               on_station, str(rig.store.missions[mid]))

        # Slow the sim so the recompute-and-reposition response is observable
        # tick by tick, not resolved within a single sample.
        rig.fv.set_sim_rates(ground_speed_ms=1.0)
        # The real-SITL arrival-noise scenario: the vehicle ends up 11 m from
        # the human -- inside the 12 m R2 boundary -- through no fault of the
        # mission's own targeting (which now aims for offset+margin, but
        # arrival precision on real hardware still isn't exact; here it is
        # simulated directly).
        close_lat, close_lon = mission._destination(human[0], human[1], 0.0, 11.0)
        rig.fv.set_position(lat=close_lat, lon=close_lon)

        got_hold = _wait_for(
            lambda: any(
                e[1] == "safety_veto" and (e[2] or {}).get("action") == "HOLD"
                for e in rig.store.events if e[0] == mid
            ), 8,
        )
        _check("r2-3: safety.py fires a HOLD for the lateral offset", got_hold,
               str([e[1:] for e in rig.store.events if e[0] == mid]))

        # safety.py releases the HOLD as soon as the distance reaches
        # LATERAL_OFFSET_M (its own threshold) -- it does not wait for the
        # aircraft to coast all the way out to the full recomputed target
        # (offset + margin), the same "do not wait for the manoeuvre to
        # finish" philosophy already proven for RTL_NOW interrupting a goto
        # (item 5). So the resolution point to watch for is LATERAL_OFFSET_M,
        # not offset+margin.
        release_dist = config.LATERAL_OFFSET_M
        samples: list[float] = []
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            s = rig.reader.get_snapshot()
            if s.lat is not None:
                samples.append(mission._haversine_m(s.lat, s.lon, human[0], human[1]))
                if samples[-1] >= release_dist:
                    break
            time.sleep(0.4)
        # The trace typically dips first (the recomputed escape point isn't
        # collinear with wherever the vehicle happened to be teleported to,
        # so the straight-line path there can pass closer to the human
        # before curving away) then climbs to the release point -- idling at
        # a fixed distance (the original defect) would show a FLAT line, not
        # this shape. Check the sustained climb from the trough, not the raw
        # start-to-end delta, since a release right at LATERAL_OFFSET_M can
        # leave that delta small even though real repositioning happened.
        _check("r2-4: distance to the human actually INCREASED over successive "
               "ticks (recompute + reposition, not idling)",
               len(samples) >= 10 and samples[-1] > min(samples) + 2.0,
               str([round(x, 1) for x in samples]))
        _check("r2-5: it reached (at least) LATERAL_OFFSET_M -- the point "
               "safety.py itself releases the HOLD at",
               samples[-1] >= release_dist if samples else False,
               f"final={samples[-1] if samples else None:.1f}m target={release_dist:.1f}m"
               if samples else "no samples")

        # Restore normal flight speed -- the artificial crawl above was only
        # to make the reposition observable tick by tick; the rest of the
        # mission (finish loitering, climb, RTL, land) should complete at a
        # normal pace, same as every other test in this suite.
        rig.fv.set_sim_rates(ground_speed_ms=25.0, climb_ms=9.0, descend_ms=9.0)

        # And it RELEASES -- the mission is not permanently parked at the
        # escape point once the distance is safe again (the companion fix:
        # on_safety_action("ALLOW", ...) clears the directive back to NONE).
        landed = _wait_for(lambda: rig.store.missions[mid]["status"] in ("landed", "aborted"), 40)
        _check("r2-6: the mission RESUMES and reaches a terminal state -- it did "
               "not get stuck at the escape point once R2 resolved",
               landed, str(rig.store.missions[mid]))
        if landed:
            _check("r2-7: it landed normally (not an RTL_NOW/abort escalation -- "
                   "the HOLD genuinely resolved)",
                   rig.store.missions[mid]["status"] == "landed",
                   str(rig.store.missions[mid]))
    finally:
        rig.close()
        config.R2_ESCAPE_TIMEOUT_S = saved_timeout


def test_r2_hold_escalates_after_timeout() -> None:
    """Verification #3: an R2 HOLD that CANNOT resolve (the reported position
    never leaves the forbidden zone no matter what the executor commands --
    simulating a configuration/geometry fault, not transient noise) escalates
    to RTL_NOW after R2_ESCAPE_TIMEOUT_S. It does not loop forever."""
    saved_timeout = config.R2_ESCAPE_TIMEOUT_S
    config.R2_ESCAPE_TIMEOUT_S = 3.0
    rig = IntakeRig()
    try:
        human = (_HOME[0] + 0.0012, _HOME[1] + 0.0010)
        target = _standoff_target_for(human)
        cid = rig.submit(target_lat=target[0], target_lon=target[1],
                          params={"human_lat": human[0], "human_lon": human[1],
                                  # Long enough that the natural loiter timer
                                  # cannot race and beat the HOLD-detection +
                                  # position-freeze below to on_station's exit
                                  # -- this test cares about the escalation
                                  # timing, not the loiter duration.
                                  "loiter_seconds": 30})
        mid_seen = _wait_for(lambda: rig.store.commands[cid]["mission_id"] is not None, 10)
        mid = rig.store.commands[cid]["mission_id"]
        _check("r2t-1: mission accepted", mid_seen, str(rig.store.commands[cid]))
        _check("r2t-2: mission reached on_station",
               _wait_for(lambda: rig.store.missions[mid]["status"] == "on_station", 30),
               str(rig.store.missions[mid]))

        # Freeze horizontal movement: no matter what standoff point the
        # executor recomputes and commands, the reported position never
        # actually leaves the forbidden zone -- exactly the "recomputed point
        # cannot be reached" fault this timeout exists for. set_sim_rates
        # (ground_speed_ms=0.0) does NOT do this -- fake_vehicle.py's
        # _sim_step_toward treats a zero/negative step as "already arrived"
        # and snaps straight to the commanded target, the opposite of frozen.
        # A background thread that keeps re-asserting the same too-close
        # position, overriding whatever the flight sim computes each tick,
        # is what actually pins it.
        close_lat, close_lon = mission._destination(human[0], human[1], 0.0, 11.0)
        pin_stop = threading.Event()

        def _pin() -> None:
            while not pin_stop.is_set():
                rig.fv.set_position(lat=close_lat, lon=close_lon)
                pin_stop.wait(0.1)

        pin_thread = threading.Thread(target=_pin, daemon=True)
        pin_thread.start()

        _check("r2t-3: safety.py fires the HOLD",
               _wait_for(lambda: any(
                   e[1] == "safety_veto" and (e[2] or {}).get("action") == "HOLD"
                   for e in rig.store.events if e[0] == mid), 8),
               str([e[1:] for e in rig.store.events if e[0] == mid]))

        # It must NOT escalate before the timeout -- it genuinely tries to
        # resolve first (repositioning attempts, just unable to arrive).
        time.sleep(max(0.0, config.R2_ESCAPE_TIMEOUT_S - 1.0))
        _check("r2t-4: it has NOT escalated before R2_ESCAPE_TIMEOUT_S elapses",
               rig.store.missions[mid]["status"] != "aborted",
               str(rig.store.missions[mid]))

        # Cross the timeout, then stop pinning and let the sim move normally
        # again -- this test is about the ESCALATION trigger and its timing,
        # not about re-proving RTL-completion (already covered by items 5/7's
        # real-flight tests); releasing the pin lets the resulting RTL
        # actually land within our patience.
        time.sleep(2.5)
        pin_stop.set()
        pin_thread.join(timeout=2)

        escalated = _wait_for(
            lambda: "R2 HOLD did not resolve" in (rig.store.missions[mid]["abort_reason"] or ""),
            15,
        )
        _check("r2t-5: it escalated to RTL (via the abort path) naming the "
               "R2-timeout reason, instead of holding forever",
               escalated, str(rig.store.missions[mid]))
        landed = _wait_for(lambda: rig.store.missions[mid]["status"] in ("landed", "aborted"), 30)
        _check("r2t-6: the mission reaches a terminal state (RTL completes)",
               landed, str(rig.store.missions[mid]))
    finally:
        rig.close()
        config.R2_ESCAPE_TIMEOUT_S = saved_timeout


# ---------------------------------------------------------------------------
# Fix 2 -- the battery-cell-configuration startup gate (verification #4/#5)
# ---------------------------------------------------------------------------


def test_battery_cell_mismatch_blocks_flight() -> None:
    """Verification #4: a 3S-pack voltage against BATTERY_CELLS=6 makes the
    startup gate refuse every flight command, naming both numbers. The same
    voltage against the CORRECT cell count passes silently."""
    saved_cells = config.BATTERY_CELLS
    try:
        config.BATTERY_CELLS = 6
        # A settled, disarmed, fresh 3S pack at a plausible resting voltage.
        box = _Box(snap(armed=False, battery_voltage_v=12.6, battery_age_s=0.2))
        mon = SafetyMonitor(box, tick_hz=20.0)
        mon.start()
        try:
            _wait_for(lambda: mon.battery_cell_fault is not None, 3)
            fault = mon.battery_cell_fault
            _check("fix2-1: the mismatch is detected (BATTERY_CELLS=6 vs a 3S pack)",
                   fault is not None, str(fault))
            if fault:
                _check("fix2-2: the fault names the CONFIGURED cell count",
                       "6" in fault, fault)
                _check("fix2-3: the fault names the MEASURED voltage",
                       "12.6" in fault, fault)
            v = mon.evaluate_command({"type": "goto", "target_lat": _HOME[0] + 0.001,
                                       "target_lon": _HOME[1] + 0.001})
            _check("fix2-4: evaluate_command() REJECTS a flight command outright",
                   v.action == SafetyAction.REJECT, v.action.value)
            _check("fix2-5: the rejection reason IS the cell-mismatch fault",
                   bool(fault) and fault in v.reasons, str(v.reasons))
            v2 = mon.evaluate_command({"type": "summon", "target_lat": _HOME[0] + 0.001,
                                        "target_lon": _HOME[1] + 0.001,
                                        "params": {"human_lat": _HOME[0], "human_lon": _HOME[1]}})
            _check("fix2-6: it refuses EVERY flight command, not just one type",
                   v2.action == SafetyAction.REJECT, v2.action.value)
        finally:
            mon.close(timeout_s=2)

        # The correct cell count -- passes silently, no fault, evaluate_command
        # reaches the normal pre-flight checks instead of the hard gate.
        config.BATTERY_CELLS = 3
        box2 = _Box(snap(armed=False, battery_voltage_v=12.6, battery_age_s=0.2,
                         lat=_HOME[0], lon=_HOME[1]))
        mon2 = SafetyMonitor(box2, tick_hz=20.0, home_lat=_HOME[0], home_lon=_HOME[1])
        mon2.start()
        try:
            time.sleep(0.3)  # let a few ticks run
            _check("fix2-7: the CORRECT cell count -> no fault detected",
                   mon2.battery_cell_fault is None, str(mon2.battery_cell_fault))
            v3 = mon2.evaluate_command({"type": "goto", "target_lat": _HOME[0] + 0.001,
                                        "target_lon": _HOME[1] + 0.001})
            _check("fix2-8: evaluate_command() is NOT hard-gated -- reaches the "
                   "normal pre-flight checks (ALLOW, a healthy snapshot)",
                   v3.action == SafetyAction.ALLOW, str(v3.reasons))
        finally:
            mon2.close(timeout_s=2)
    finally:
        config.BATTERY_CELLS = saved_cells


def test_cell_check_runs_once_not_per_tick() -> None:
    """Verification #5: the check runs ONCE, at startup, disarmed. Ordinary
    in-flight voltage sag under load (after the vehicle arms) must not
    re-trigger it -- a healthy first reading stays healthy for the rest of
    the process, even if a later ARMED reading would look like a mismatch."""
    saved_cells = config.BATTERY_CELLS
    try:
        config.BATTERY_CELLS = 6
        # A settled, disarmed, correct 6S pack -- the check passes.
        # (6 * NOMINAL_CELL_RESTING_V = 23.1V; 22.8V is within tolerance.)
        box = _Box(snap(armed=False, battery_voltage_v=22.8, battery_age_s=0.2))
        mon = SafetyMonitor(box, tick_hz=20.0)
        mon.start()
        try:
            time.sleep(0.3)
            _check("fix2-9: the correct 6S reading passes at startup",
                   mon.battery_cell_fault is None, str(mon.battery_cell_fault))

            # Now arm and sag the voltage hard under load (a real, ordinary
            # flight condition) -- this must NOT retroactively flag a
            # mismatch; the check already ran, once, for good.
            box.s = snap(armed=True, battery_voltage_v=19.0, battery_age_s=0.1)
            time.sleep(0.5)
            _check("fix2-10: sagging under load AFTER arming does NOT retrigger "
                   "the mismatch gate (it only ever runs once, disarmed, at "
                   "startup)",
                   mon.battery_cell_fault is None, str(mon.battery_cell_fault))
        finally:
            mon.close(timeout_s=2)
    finally:
        config.BATTERY_CELLS = saved_cells


def main() -> int:
    for t in (
        test_standoff_has_a_margin,
        test_r2_hold_recomputes_and_repositions,
        test_r2_hold_escalates_after_timeout,
        test_battery_cell_mismatch_blocks_flight,
        test_cell_check_runs_once_not_per_tick,
    ):
        print(f"\n--- {t.__name__} ---")
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            _check(t.__name__ + " (crashed)", False, repr(exc))

    failed = [n for n, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        print("FAILED:")
        for n in failed:
            print(f"  - {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
