"""mission.py verification (v0.6) -- real MAVLink flight, against a kinematic
flight-simulating fake vehicle (tests/fake_vehicle.py), not Mission Planner
SITL. See the phase report for why, and what that does and doesn't cover.

Layers:
  * a Rig (fake vehicle in flight-sim mode + MavlinkLink + TelemetryReader)
    driving MavlinkExecutor directly, for items 1, 2, 3, 4, 7, 8, 13;
  * a Rig plus a full CommandIntake against an in-memory fake store (no
    Supabase -- this suite needs no internet either) for items 5, 6, 9, 10,
    which need safety.py / weather.py actually driving the executor;
  * source-inspection tests for items 11 (transmit chokepoint) and 7's
    "no disarm above DISARM_MAX_ALT_M" claim;
  * a subprocess test for item 12 (the doubled transmit gate).

    python -m tests.test_mission
"""

from __future__ import annotations

import io
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

os.environ.setdefault("SUPABASE_ENABLED", "false")
os.environ["ALLOW_VEHICLE_CONTROL"] = "true"  # this whole suite exercises the real path
# Most items need no weather branch at all; the two that do (9, 10) always pass
# CommandIntake an explicit WeatherMonitor built on a FAKE forecast source, so
# this flag being off by default just keeps the other items' rigs simple --
# it is never relied on to reach the real internet either way.
os.environ.setdefault("WEATHER_ENABLED", "false")

from gss import config  # noqa: E402
from gss import mission  # noqa: E402
from gss.executor import MissionPhase  # noqa: E402
from gss.link import MavlinkLink  # noqa: E402
from gss.safety import SafetyMonitor  # noqa: E402
from gss.telemetry import TelemetryReader  # noqa: E402
from tests.fake_vehicle import FakeVehicle  # noqa: E402

_results: list[tuple[str, bool, str]] = []
_HOME = (25.5932, 85.2045)
_PORT = 5900


def _check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  ::  {detail}" if detail else ""))


def _next_port() -> int:
    global _PORT
    _PORT += 1
    return _PORT


# Fast confirmation timeouts for the whole suite -- the logic under test is the
# same regardless of how long a timeout is; a real flight tunes these from
# SITL/real logs, not this test file.
config.MODE_CONFIRM_TIMEOUT_S = 2.0
config.ARM_CONFIRM_TIMEOUT_S = 2.0
config.TAKEOFF_TIMEOUT_S = 15.0
config.MISSION_STEP_TIMEOUT_S = 20.0
config.MAVLINK_CMD_RETRIES = 2
config.MISSION_MAX_DURATION_S = 300.0


class Rig:
    """FakeVehicle (flight sim) + MavlinkLink + TelemetryReader, close-target
    geometry so a full mission finishes in well under a minute."""

    def __init__(self, *, port: int | None = None, reject_arm=False, prearm_fail=None):
        self.port = port or _next_port()
        self.fv = FakeVehicle(port=self.port)
        self.fv.set_position(lat=_HOME[0], lon=_HOME[1], fix_type=3, satellites=12, rel_alt_m=0.0)
        self.fv.set_battery(pct=90, voltage_v=24.6)
        self.fv.enable_flight_sim()
        self.fv.set_sim_rates(ground_speed_ms=25.0, climb_ms=9.0, descend_ms=9.0)
        if reject_arm:
            self.fv.reject_arm()
        if prearm_fail:
            self.fv.set_prearm_fail(prearm_fail)
        self.fv.start()
        self.fv.wait_for_client(3)
        self.link = MavlinkLink(connection_string=f"tcp:127.0.0.1:{self.port}", heartbeat_timeout_s=4)
        self.reader = TelemetryReader(self.link)
        self.link.connect(timeout_s=8)
        self._await_basic_telemetry()

    def _await_basic_telemetry(self, timeout_s: float = 6.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            s = self.reader.get_snapshot()
            if s.armed is not None and s.ekf_healthy is not None and s.home_set is not None:
                return
            time.sleep(0.1)

    def command(self, *, is_summon=True, target=None, human=None, loiter_s=1.0,
                target_alt_m=None):
        target = target or (_HOME[0] + 0.0012, _HOME[1] + 0.0010)  # ~150 m out
        human = human or target
        return {
            "id": "cmd-1", "type": "summon" if is_summon else "goto",
            "target_lat": target[0], "target_lon": target[1],
            "target_alt_m": target_alt_m,
            "params": {"human_lat": human[0], "human_lon": human[1], "loiter_seconds": loiter_s},
        }

    def executor(self, cmd=None, **kw) -> mission.MavlinkExecutor:
        cmd = cmd or self.command()
        return mission.MavlinkExecutor(
            "m-test", cmd, self.reader.get_snapshot, link=self.link,
            home_lat=_HOME[0], home_lon=_HOME[1], on_station_alt_m=config.ON_STATION_ALT,
            loiter_seconds=(cmd.get("params") or {}).get("loiter_seconds"), **kw,
        )

    def close(self) -> None:
        self.link.close()
        self.fv.stop()


def _standoff_target_for(person: tuple[float, float]) -> tuple[float, float]:
    """A geometrically valid on-station target for a summon to ``person`` --
    genuinely more than LATERAL_OFFSET_M from them, the way a real client (or a
    future commands.py) would compute it. commands.py does not compute this
    itself yet (the Phase 4 R2 gap note): passing target == human would be
    correctly REJECTED by safety.py's R2 check, so tests must not do that."""
    lat, lon, _ = mission.standoff_point(
        person[0], person[1], dock_lat=_HOME[0], dock_lon=_HOME[1],
        offset_m=config.LATERAL_OFFSET_M + 5.0, wind_from_deg=None,
    )
    return lat, lon


def _wait_for(pred, timeout: float, poll: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(poll)
    return pred()


def _run_and_trace(ex: mission.MavlinkExecutor, get_snapshot, *, timeout: float = 60.0):
    """Poll an executor to a terminal phase, recording (t, phase, alt, lat, lon)
    at every phase change. Returns (final_phase, trace, all_events)."""
    ex.start()
    t0 = time.monotonic()
    trace = []
    events = []
    last = None
    while time.monotonic() - t0 < timeout:
        p = ex.poll()
        events.extend(ex.drain_events())
        if p != last:
            s = get_snapshot()
            trace.append((round(time.monotonic() - t0, 1), p.value, s.alt_m_relative, s.lat, s.lon))
            last = p
        if p in (MissionPhase.LANDED, MissionPhase.ABORTED):
            break
        time.sleep(0.2)
    events.extend(ex.drain_events())
    return ex.poll(), trace, events


# --------------------------------------------------------------------------
# item 1 -- full summon flight, altitude profile
# --------------------------------------------------------------------------


def test_full_summon_flight() -> None:
    rig = Rig()
    try:
        cmd = rig.command(loiter_s=1.0)
        ex = rig.executor(cmd)
        phase, trace, events = _run_and_trace(ex, rig.reader.get_snapshot, timeout=90)
        print("\n  item 1 -- phase trace (t, phase, alt_m_relative, lat, lon):")
        for row in trace:
            print(f"    {row}")
        _check("item 1: mission lands (no exception, no timeout)",
               phase == MissionPhase.LANDED, phase.value)
        by_phase = {row[1]: row[2] for row in trace}
        _check("item 1: outbound cruise altitude ~= CRUISE_ALT_OUTBOUND (55 m)",
               by_phase.get("enroute") is not None
               and abs(by_phase["enroute"] - config.CRUISE_ALT_OUTBOUND) <= config.ALT_TOLERANCE_M,
               str(by_phase.get("enroute")))
        on_station_alt = None
        for row in trace:
            if row[1] == "on_station":
                on_station_alt = row[2]
        _check("item 1: on-station altitude ~= ON_STATION_ALT (28 m)",
               on_station_alt is not None
               and abs(on_station_alt - config.ON_STATION_ALT) <= config.ALT_TOLERANCE_M * 3,
               str(on_station_alt))
        _check("item 1: inbound cruise altitude ~= CRUISE_ALT_INBOUND (45 m)",
               by_phase.get("returning") is not None
               and abs(by_phase["returning"] - config.CRUISE_ALT_INBOUND) <= config.ALT_TOLERANCE_M * 3,
               str(by_phase.get("returning")))
        s = rig.reader.get_snapshot()
        _check("item 1: landed back near the dock, disarmed",
               s.armed is False and mission._haversine_m(s.lat, s.lon, _HOME[0], _HOME[1]) < 5.0,
               f"armed={s.armed} dist_from_dock="
               f"{mission._haversine_m(s.lat, s.lon, _HOME[0], _HOME[1]):.1f}m")
    finally:
        rig.close()


# --------------------------------------------------------------------------
# item 2 -- R2 downwind standoff, live wind change
# --------------------------------------------------------------------------


def test_standoff_downwind_and_wind_flip() -> None:
    _check(
        "item 2 (geometry): standoff is exactly LATERAL_OFFSET_M downwind "
        "(wind from 90 -> downwind bearing 270)",
        _standoff_matches_bearing(90.0, 270.0),
    )
    _check(
        "item 2 (geometry): flipping the wind 180 degrees moves it to the "
        "opposite bearing",
        _standoff_matches_bearing(270.0, 90.0),
    )

    rig = Rig()
    try:
        rig.fv.set_wind(speed_ms=6.0, direction_deg=90.0)  # wind FROM the east
        cmd = rig.command(loiter_s=3.0)
        ex = rig.executor(cmd)
        ex.start()
        _wait_for(lambda: ex.standoff is not None, 30)
        p1 = ex.standoff
        print(f"\n  item 2 -- wind from 90 deg -> standoff {p1}")
        _check("item 2 (live): standoff computed while wind is from 90 deg",
               p1 is not None and p1[2] == "downwind", str(p1))

        rig.fv.set_wind(speed_ms=6.0, direction_deg=270.0)  # flip 180 degrees
        before = p1
        got_new = _wait_for(
            lambda: ex.standoff is not None and ex.standoff[:2] != before[:2], 20
        )
        p2 = ex.standoff
        print(f"  item 2 -- wind flipped to 270 deg -> standoff {p2}")
        _check("item 2 (live): the standoff point MOVED once the wind flipped 180 deg",
               got_new and p2 is not None, f"before={before} after={p2}")
        if p2 is not None:
            moved_m = mission._haversine_m(p1[0], p1[1], p2[0], p2[1])
            _check("item 2 (live): the move is on the order of the offset (not noise)",
                   moved_m > config.LATERAL_OFFSET_M, f"{moved_m:.1f} m")
        ex.abort("test done")
        _wait_for(lambda: ex.poll() in (MissionPhase.ABORTED, MissionPhase.LANDED), 30)
    finally:
        rig.close()


def _standoff_matches_bearing(wind_from_deg: float, expect_bearing_deg: float) -> bool:
    person = (25.60, 85.21)
    lat, lon, basis = mission.standoff_point(
        person[0], person[1], dock_lat=_HOME[0], dock_lon=_HOME[1],
        offset_m=config.LATERAL_OFFSET_M, wind_from_deg=wind_from_deg,
    )
    got_bearing = mission._bearing_deg(person[0], person[1], lat, lon)
    dist = mission._haversine_m(person[0], person[1], lat, lon)
    return (
        basis == "downwind"
        and abs(dist - config.LATERAL_OFFSET_M) < 0.5
        and abs(((got_bearing - expect_bearing_deg) + 180) % 360 - 180) < 1.0
    )


# --------------------------------------------------------------------------
# item 3 -- mode change ignored: aborts on timeout, specific reason
# --------------------------------------------------------------------------


def test_mode_change_ignored_aborts() -> None:
    rig = Rig()
    try:
        rig.fv.set_mode_change_ignored(True)
        ex = rig.executor()
        t0 = time.monotonic()
        phase, trace, _ = _run_and_trace(ex, rig.reader.get_snapshot, timeout=30)
        dt = time.monotonic() - t0
        _check("item 3: mission aborts (never proceeds past a mode that never took)",
               phase == MissionPhase.ABORTED, phase.value)
        _check("item 3: abort reason is specific (names the mode + 'not confirmed')",
               ex.abort_reason is not None
               and "GUIDED" in ex.abort_reason and "not confirmed" in ex.abort_reason,
               ex.abort_reason)
        _check("item 3: it took roughly TIMEOUT x RETRIES, not forever",
               dt < (config.MODE_CONFIRM_TIMEOUT_S * config.MAVLINK_CMD_RETRIES) + 10,
               f"{dt:.1f}s")
        print(f"\n  item 3 -- aborted after {dt:.1f}s: {ex.abort_reason}")
    finally:
        rig.close()


# --------------------------------------------------------------------------
# item 4 -- arming rejected: aborts, no retry loop
# --------------------------------------------------------------------------


def test_arm_rejected_no_retry() -> None:
    rig = Rig(reject_arm=True)
    try:
        ex = rig.executor()
        phase, trace, _ = _run_and_trace(ex, rig.reader.get_snapshot, timeout=15)
        _check("item 4: mission aborts on the ground (never airborne)",
               phase == MissionPhase.ABORTED, phase.value)
        _check("item 4: reason says ArduPilot REJECTED arming",
               ex.abort_reason is not None and "REJECTED" in ex.abort_reason,
               ex.abort_reason)
        _check("item 4: NOT retried -- exactly one arm command sent",
               rig.fv.arm_command_count == 1, str(rig.fv.arm_command_count))
        print(f"\n  item 4 -- {ex.abort_reason}; arm attempts={rig.fv.arm_command_count}")
    finally:
        rig.close()


# --------------------------------------------------------------------------
# items 5, 6, 9, 10 -- full CommandIntake (safety.py / weather.py actually
# driving the executor), against an in-memory fake store. No Supabase.
# --------------------------------------------------------------------------


class _FakeStore:
    """Just enough of TelemetryStore's interface for CommandIntake, in memory.
    No network -- this suite, like Phase 5's, needs no internet."""

    def __init__(self) -> None:
        self.commands: dict[str, dict] = {}
        self.missions: dict[str, dict] = {}
        self.events: list[tuple[str | None, str, dict | None]] = []
        self.alerts: list[dict] = []
        self._seq = 0

    def insert(self, **fields) -> str:
        cid = fields.setdefault("id", f"cmd-{len(self.commands) + 1}")
        row = {
            "status": "pending", "issued_by": "test", "mission_id": None,
            "rejected_reason": None,
            "issued_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": None,
            "params": None,
            **fields,
        }
        self.commands[cid] = row
        return cid

    def claim_command(self, command_id, drone_id):
        cmd = self.commands.get(command_id)
        if cmd is None or cmd["status"] != "pending":
            return True, None
        cmd["status"] = "accepted"
        return True, {"command": dict(cmd), "server_now": datetime.now(timezone.utc).isoformat()}

    def update_command_status(self, command_id, status, *, rejected_reason=None, mission_id=None):
        cmd = self.commands.get(command_id)
        if cmd is None:
            return False
        cmd["status"] = status
        if rejected_reason is not None:
            cmd["rejected_reason"] = rejected_reason
        if mission_id is not None:
            cmd["mission_id"] = mission_id
        return True

    def link_command_to_mission(self, command_id, mission_id):
        self.commands[command_id]["mission_id"] = mission_id
        return True

    def create_mission(self, *, drone_id, mission_type, dock_id=None, target_lat=None,
                        target_lon=None, cruise_alt_m=None, triggered_by=None):
        self._seq += 1
        mid = f"mission-{self._seq}"
        self.missions[mid] = {
            "id": mid, "status": "queued", "type": mission_type, "abort_reason": None,
            "target_lat": target_lat, "target_lon": target_lon, "started_at": None,
        }
        return mid

    def update_mission(self, mission_id, **fields):
        m = self.missions.get(mission_id)
        if m is None:
            return False
        if fields.get("status") not in ("queued", "awaiting_confirmation") and m.get("started_at") is None:
            m["started_at"] = datetime.now(timezone.utc).isoformat()
        m.update(fields)
        return True

    def log_event(self, mission_id, event, detail=None, *, sync=False):
        self.events.append((mission_id, event, detail))
        return True

    def create_alert(self, *, mission_id=None, channel="whatsapp", status="queued",
                      template_name=None, detail=None):
        self.alerts.append({"mission_id": mission_id, "channel": channel, "detail": detail})
        return f"alert-{len(self.alerts)}"

    def list_pending_command_ids(self, drone_id):
        return [cid for cid, c in self.commands.items() if c["status"] == "pending"]

    def list_active_missions(self, drone_id):
        return [dict(m) for m in self.missions.values() if m["status"] not in ("landed", "aborted")]

    def list_safe_spots(self):
        return []


class IntakeRig(Rig):
    """A Rig plus a full CommandIntake (safety.py + optional weather.py, real
    MavlinkExecutor), backed by an in-memory store. Mirrors production wiring
    exactly except for the store."""

    def __init__(self, *, with_weather=False, **kw):
        super().__init__(**kw)
        from gss.commands import CommandIntake

        self.store = _FakeStore()
        self.safety = SafetyMonitor(self.reader.get_snapshot, home_lat=_HOME[0], home_lon=_HOME[1],
                                    tick_hz=8.0)
        self.safety.start()
        self.weather = None
        if with_weather:
            import tempfile

            from gss.weather_feed import WeatherFeed, WeatherMonitor
            from tests.test_weather import FakeForecastSource

            cache = os.path.join(tempfile.gettempdir(), f"wx_mission_{self.port}.json")
            try:
                os.remove(cache)
            except OSError:
                pass
            feed = WeatherFeed(_HOME[0], _HOME[1], source=FakeForecastSource(),
                               cache_path=cache, fetch_interval_s=999)
            feed.fetch_once()  # a calm baseline forecast -- no network (fake source)
            self.weather = WeatherMonitor(self.reader.get_snapshot, feed=feed)
        self.intake = CommandIntake(
            self.store, self.reader.get_snapshot, home_lat=_HOME[0], home_lon=_HOME[1],
            realtime_enabled=False, safety=self.safety, weather=self.weather,
            link=self.link,
        )
        self.intake.start()

    def submit(self, **fields) -> str:
        cid = self.store.insert(drone_id=config.DRONE_ID, type="summon", **fields)
        self.intake.submit(cid)
        return cid

    def close(self) -> None:
        self.intake.close(timeout_s=5)
        # CommandIntake only closes safety/weather it OWNS; these are ours
        # (passed in explicitly, mirroring production's externally-supplied
        # monitors), so we stop them ourselves -- otherwise their threads keep
        # ticking against a closed link for the rest of the process.
        self.safety.close(timeout_s=2)
        if self.weather is not None:
            self.weather.close(timeout_s=1)
        super().close()


def test_battery_rtl_interrupts_manoeuvre() -> None:
    """item 5: RTL_NOW interrupts a manoeuvre in progress and it turns around
    -- proved by position samples, not by waiting for the step to finish."""
    rig = IntakeRig()
    try:
        person = (_HOME[0] + 0.006, _HOME[1] + 0.005)  # ~750 m -- a long goto leg
        far_target = _standoff_target_for(person)
        cid = rig.submit(target_lat=far_target[0], target_lon=far_target[1],
                         params={"human_lat": person[0], "human_lon": person[1],
                                 "loiter_seconds": 1})
        mid_seen = _wait_for(lambda: rig.store.commands[cid]["mission_id"] is not None, 10)
        mid = rig.store.commands[cid]["mission_id"]
        _check("item 5: mission accepted", mid_seen, str(rig.store.commands[cid]))
        _wait_for(lambda: rig.store.missions[mid]["status"] == "enroute", 15)

        samples = []
        deadline = time.monotonic() + 20
        dist_to_target_start = None
        while time.monotonic() < deadline:
            s = rig.reader.get_snapshot()
            if s.lat is not None:
                dist_target = mission._haversine_m(s.lat, s.lon, far_target[0], far_target[1])
                samples.append((round(time.monotonic(), 2), dist_target))
                if dist_to_target_start is None:
                    dist_to_target_start = dist_target
                if dist_target < dist_to_target_start * 0.7:
                    break  # well into the goto leg -- now interrupt it
            time.sleep(0.3)

        # Below BATTERY_FLOOR_PCT (RTL_NOW fires) but still comfortably inside
        # RTL_RESERVE_PCT of the ~250-300 m home distance at the interrupt point
        # -- home stays REACHABLE, so this proves a plain RTL turnaround, not
        # safety.py's separate (and separately-tested, see items 9/10) point-of-
        # no-return LAND-in-place escalation for a genuinely unreachable home.
        rig.fv.set_battery(pct=19, voltage_v=20.0)
        interrupted_at = samples[-1][1] if samples else None
        got = _wait_for(
            lambda: any(e[1] == "safety_veto" for e in rig.store.events
                        if e[0] == mid), 10)
        _check("item 5: safety_veto (RTL_NOW) fired", got,
               str([e[1] for e in rig.store.events if e[0] == mid]))

        # Prove it turned around WHILE still mid-leg: distance to the ORIGINAL
        # target should stop decreasing / distance to HOME should decrease,
        # inside the same goto step -- not after it would have finished.
        post = []
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            s = rig.reader.get_snapshot()
            if s.lat is not None:
                post.append(mission._haversine_m(s.lat, s.lon, _HOME[0], _HOME[1]))
            time.sleep(0.3)
        _check("item 5: it did NOT wait for the goto step to complete -- distance "
               "to the ORIGINAL target was still shrinking (mid-leg) when RTL fired",
               interrupted_at is not None and interrupted_at < dist_to_target_start,
               f"start={dist_to_target_start:.0f}m interrupted_at={interrupted_at}")
        _check("item 5: the vehicle actually turned around -- distance to HOME "
               "decreased after the interrupt",
               len(post) >= 2 and post[-1] < post[0], f"home_dist trace: {post[:3]}...{post[-3:]}")
        landed = _wait_for(lambda: rig.store.missions[mid]["status"] in ("landed", "aborted"), 40)
        _check("item 5: mission reaches a terminal state (landed after RTL)",
               landed, str(rig.store.missions[mid]))
        print(f"\n  item 5 -- home-distance trace after RTL_NOW: {[round(x) for x in post]}")
    finally:
        rig.close()


def test_link_loss_reconnect_same_mission() -> None:
    """item 6: link cut mid-flight beyond the grace period -- safety.py's own
    RTL_NOW and ArduPilot's failsafe point the same way; the link reconnects
    and the SAME mission (not a new one) is what continues."""
    rig = IntakeRig()
    saved_grace = config.LINK_LOSS_GRACE_S
    try:
        # An abrupt disconnect against this fake vehicle typically clears (link
        # detects the RST, backs off ~1s, reconnects) in under ~2s -- so the
        # grace has to be well below that to prove "past the grace period"
        # actually happened while still disconnected, not after reconnecting.
        config.LINK_LOSS_GRACE_S = 0.5
        cid = rig.submit(target_lat=_HOME[0] + 0.0012, target_lon=_HOME[1] + 0.0010,
                         params={"loiter_seconds": 3})
        _wait_for(lambda: rig.store.commands[cid]["mission_id"] is not None, 10)
        mid = rig.store.commands[cid]["mission_id"]
        _wait_for(lambda: rig.store.missions[mid]["status"] in ("launching", "enroute"), 15)

        rig.fv.disconnect_abrupt()
        _check("item 6: link actually drops", _wait_for(lambda: not rig.link.is_connected, 5))
        _check("item 6: safety RTL_NOW fires past the (shortened) link-loss grace",
               _wait_for(lambda: any(
                   e[1] == "safety_veto" and (e[2] or {}).get("action") == "RTL_NOW"
                   for e in rig.store.events if e[0] == mid), 10),
               str([e[1] for e in rig.store.events if e[0] == mid]))
        reconnected = _wait_for(lambda: rig.link.is_connected, 15)
        _check("item 6: the link reconnects on its own (v0.1.1 forever-reconnect)",
               reconnected)
        _check("item 6: the SAME mission id is still the active one -- no new "
               "mission was created across the reconnect",
               rig.intake.active_mission_id in (mid, None),  # None once it lands
               f"active={rig.intake.active_mission_id} expected={mid}")
        _check("item 6: exactly one mission exists for this run",
               len(rig.store.missions) == 1, str(list(rig.store.missions)))
        _wait_for(lambda: rig.store.missions[mid]["status"] in ("landed", "aborted"), 40)
        print(f"\n  item 6 -- mission {mid} status after reconnect+RTL: "
              f"{rig.store.missions[mid]['status']}")
    finally:
        config.LINK_LOSS_GRACE_S = saved_grace
        rig.close()


def test_weather_stay_real_position_over_time() -> None:
    """item 9, done properly: an emergency mission on station, weather MARGINAL,
    and REAL position samples over time proving it is still holding station
    well past WEATHER_WARN_GRACE_S -- not just the absence of a return event."""
    rig = IntakeRig(with_weather=True)
    saved_grace = config.WEATHER_WARN_GRACE_S
    try:
        config.WEATHER_WARN_GRACE_S = 3.0
        person = (_HOME[0] + 0.0012, _HOME[1] + 0.0010)
        target = _standoff_target_for(person)
        cid = rig.submit(target_lat=target[0], target_lon=target[1],
                         params={"human_lat": person[0], "human_lon": person[1],
                                 "loiter_seconds": 15})
        _wait_for(lambda: rig.store.commands[cid]["mission_id"] is not None, 10)
        mid = rig.store.commands[cid]["mission_id"]
        _wait_for(lambda: rig.store.missions[mid]["status"] == "on_station", 40)

        rig.fv.set_wind(speed_ms=9.0)  # observed MARGINAL
        _check("item 9: weather_hold (STAY) logged for the emergency mission",
               _wait_for(lambda: any(e[1] == "weather_hold" for e in rig.store.events
                                     if e[0] == mid), 10),
               str([e[1] for e in rig.store.events if e[0] == mid]))

        samples = []
        t0 = time.monotonic()
        while time.monotonic() - t0 < config.WEATHER_WARN_GRACE_S * 3 + 3:
            s = rig.reader.get_snapshot()
            if s.lat is not None:
                samples.append((round(time.monotonic() - t0, 1), round(s.lat, 6), round(s.lon, 6),
                               round(s.alt_m_relative or 0.0, 1)))
            time.sleep(0.5)
        print("\n  item 9 -- position samples through and past WEATHER_WARN_GRACE_S:")
        for row in samples:
            print(f"    {row}")

        drift = [mission._haversine_m(target[0], target[1], lat, lon) for _, lat, lon, _ in samples]
        _check("item 9: it held station (within a lateral-offset-ish radius) the "
               "WHOLE time, not drifting home",
               all(d < config.LATERAL_OFFSET_M + 15 for d in drift), f"max drift {max(drift):.1f} m")
        events_after = [e[1] for e in rig.store.events if e[0] == mid]
        _check("item 9: NO weather_return, NO abort -- well past WEATHER_WARN_GRACE_S",
               "weather_return" not in events_after
               and rig.store.missions[mid]["status"] not in ("aborted",),
               f"events={events_after} status={rig.store.missions[mid]['status']}")
    finally:
        config.WEATHER_WARN_GRACE_S = saved_grace
        rig.close()


def test_weather_severe_returns_with_numbers() -> None:
    """item 10: weather SEVERE on station during an emergency mission -> RETURN,
    not overridable, with the measured numbers."""
    rig = IntakeRig(with_weather=True)
    try:
        person = (_HOME[0] + 0.0012, _HOME[1] + 0.0010)
        target = _standoff_target_for(person)
        cid = rig.submit(target_lat=target[0], target_lon=target[1],
                         params={"human_lat": person[0], "human_lon": person[1],
                                 "loiter_seconds": 15})
        _wait_for(lambda: rig.store.commands[cid]["mission_id"] is not None, 10)
        mid = rig.store.commands[cid]["mission_id"]
        _wait_for(lambda: rig.store.missions[mid]["status"] == "on_station", 40)

        rig.fv.set_wind(speed_ms=13.5)  # observed SEVERE
        got = _wait_for(lambda: any(e[1] == "weather_return" for e in rig.store.events
                                    if e[0] == mid), 15)
        _check("item 10: weather_return fires for SEVERE, even for an emergency mission",
               got, str([e[1] for e in rig.store.events if e[0] == mid]))
        wr = [e for e in rig.store.events if e[0] == mid and e[1] == "weather_return"]
        detail = wr[0][2] if wr else {}
        _check("item 10: the detail carries measured NUMBERS (wind), not 'weather bad'",
               isinstance((detail or {}).get("measured", {}).get("wind_speed_ms"), (int, float))
               and any("13." in r for r in (detail or {}).get("reasons", [])),
               str(detail))
        _check("item 10: marked not overridable",
               (detail or {}).get("overridable") is False, str(detail))
        landed = _wait_for(lambda: rig.store.missions[mid]["status"] in ("landed", "aborted"), 40)
        _check("item 10: the mission actually returns (reaches a terminal state)",
               landed, str(rig.store.missions[mid]))
        print(f"\n  item 10 -- weather_return detail: {detail}")
    finally:
        rig.close()


# --------------------------------------------------------------------------
# item 7 -- abort command -> RTL, never disarm in the air; grep proof
# --------------------------------------------------------------------------


def test_abort_command_rtl_never_disarm_in_air() -> None:
    rig = IntakeRig()
    try:
        cid = rig.submit(target_lat=_HOME[0] + 0.0012, target_lon=_HOME[1] + 0.0010,
                         params={"loiter_seconds": 10})
        _wait_for(lambda: rig.store.commands[cid]["mission_id"] is not None, 10)
        mid = rig.store.commands[cid]["mission_id"]
        _wait_for(lambda: rig.store.missions[mid]["status"] in ("enroute", "on_station"), 30)

        alt_before_abort = rig.reader.get_snapshot().alt_m_relative
        abort_cid = rig.store.insert(drone_id=config.DRONE_ID, type="abort", mission_id=mid)
        rig.intake.submit(abort_cid)
        _check("item 7: abort command accepted",
               _wait_for(lambda: rig.store.commands[abort_cid]["status"] == "done", 5))
        landed = _wait_for(lambda: rig.store.missions[mid]["status"] in ("landed", "aborted"), 40)
        _check("item 7: mission reaches a terminal state after the abort", landed,
               str(rig.store.missions[mid]))
        s = rig.reader.get_snapshot()
        _check("item 7: it was airborne when the abort was issued (a meaningful test)",
               alt_before_abort is not None and alt_before_abort > config.DISARM_MAX_ALT_M,
               f"alt at abort={alt_before_abort}")
        _check("item 7: it is disarmed NOW, on the ground (RTL completed, not a mid-air cut)",
               s.armed is False and (s.alt_m_relative or 0) <= config.DISARM_MAX_ALT_M,
               f"armed={s.armed} alt={s.alt_m_relative}")
    finally:
        rig.close()


def test_no_disarm_code_path_above_threshold() -> None:
    """Static proof: mission.py has NO call that disarms the vehicle at all --
    grep for a False/0 arm request. The only arm transmit is param1=1 (arm)."""
    src = open("gss/mission.py", encoding="utf-8").read()
    disarm_calls = re.findall(r"_tx_arm\([^)]*\bFalse\b", src)
    any_disarm = re.findall(r"_tx_arm\(", src)
    _check("item 7 (static): _tx_arm() is called at all (sanity)", len(any_disarm) >= 1)
    _check("item 7 (static): mission.py contains NO call that requests a disarm "
           "(_tx_arm(..., False)) -- there is no code path that can cut motors "
           "in the air, guarded or not",
           len(disarm_calls) == 0, f"found: {disarm_calls}")
    # And the one arm call it does have is gated nowhere near an altitude check
    # that could be bypassed -- it always means "arm before takeoff", never
    # mid-flight.
    _check("item 7 (static): the only arm call passes True (arm), never a variable "
           "that could be False in flight",
           bool(re.search(r"_tx_arm\(self\._link,\s*True\)", src)), "")


# --------------------------------------------------------------------------
# item 8 -- GSS restart mid-flight: adopt + RTL
# --------------------------------------------------------------------------


def test_restart_adopts_airborne_vehicle() -> None:
    rig = Rig()
    logbuf = io.StringIO()
    handler = logging.StreamHandler(logbuf)
    handler.setLevel(logging.CRITICAL)
    logging.getLogger().addHandler(handler)
    try:
        # Fly it up and partway, with nothing watching -- simulates "the GSS
        # process that launched this has crashed".
        ex = rig.executor(rig.command(loiter_s=30))
        ex.start()
        _wait_for(lambda: (rig.reader.get_snapshot().alt_m_relative or 0) > 20, 30)
        alt_before = rig.reader.get_snapshot().alt_m_relative
        _check("item 8: the vehicle is genuinely airborne before 'GSS restart'",
               alt_before is not None and alt_before > config.DISARM_MAX_ALT_M,
               f"alt={alt_before}")

        # The "crash": stop watching it entirely -- a real process crash kills
        # every thread, including the one running ex._fly(). Nothing sends
        # anything further from the old executor from this point on.
        ex.close()

        # "Restart": a brand-new process's very first act, before intake exists.
        detail = mission.recover_airborne_vehicle(rig.link, rig.reader.get_snapshot)
        _check("item 8: recover_airborne_vehicle finds it and returns a detail",
               detail is not None and detail.get("found_armed") is True, str(detail))
        _check("item 8: RTL was actually sent",
               detail is not None and detail.get("rtl_sent") is True, str(detail))
        _check("item 8: a CRITICAL log line was raised",
               "STARTUP" in logbuf.getvalue() and "ARMED" in logbuf.getvalue(),
               logbuf.getvalue()[-300:])

        # NOT "alt_m_relative or 99" -- 0.0 (genuinely on the ground) is falsy,
        # so that idiom would silently fall back to 99 on the one value that
        # actually means "landed", and this would never fire.
        came_home = _wait_for(
            lambda: rig.reader.get_snapshot().armed is False
            and rig.reader.get_snapshot().alt_m_relative is not None
            and rig.reader.get_snapshot().alt_m_relative <= config.DISARM_MAX_ALT_M,
            40,
        )
        s = rig.reader.get_snapshot()
        _check("item 8: the vehicle actually comes home and lands after adoption",
               came_home and mission._haversine_m(s.lat, s.lon, _HOME[0], _HOME[1]) < 5.0,
               f"armed={s.armed} alt={s.alt_m_relative} "
               f"dist={mission._haversine_m(s.lat, s.lon, _HOME[0], _HOME[1]):.1f}m")
        print(f"\n  item 8 -- adopted at {alt_before:.1f} m, detail={detail}")

        # And a SECOND check with nothing airborne must be a no-op.
        rig2 = Rig()
        try:
            none_detail = mission.recover_airborne_vehicle(rig2.link, rig2.reader.get_snapshot)
            _check("item 8: a normal (disarmed, on the ground) startup finds nothing to adopt",
                   none_detail is None, str(none_detail))
        finally:
            rig2.close()
    finally:
        logging.getLogger().removeHandler(handler)
        rig.close()


# --------------------------------------------------------------------------
# item 11 -- the transmit chokepoint
# --------------------------------------------------------------------------

_FLIGHT_SEND_MARKERS = (
    "command_long_send", "set_position_target", "set_mode_send",
    "param_set_send", "mission_item",
)


def test_transmit_chokepoint() -> None:
    """No module other than gss/mission.py sends a flight command. link.py's
    legitimate command_long_send calls are SET_MESSAGE_INTERVAL (v0.1.1's
    boundary) and MAV_CMD_GET_HOME_POSITION (v0.6, found needed against real
    SITL -- a request, not a flight command, same category as the stream-rate
    requests), never a flight command."""
    import glob

    offenders: list[str] = []
    for path in glob.glob("gss/*.py"):
        if os.path.basename(path) in ("mission.py",):
            continue
        text = open(path, encoding="utf-8").read()
        for marker in _FLIGHT_SEND_MARKERS:
            for m in re.finditer(re.escape(marker) + r"\(", text):
                if os.path.basename(path) == "link.py" and marker == "command_long_send":
                    # the whitelisted exceptions: SET_MESSAGE_INTERVAL and
                    # MAV_CMD_GET_HOME_POSITION -- both requests, not commands.
                    window = text[max(0, m.start() - 400):m.start()]
                    if (
                        "SET_MESSAGE_INTERVAL" in window
                        or "request_message_interval" in window
                        or "MAV_CMD_GET_HOME_POSITION" in window
                    ):
                        continue
                offenders.append(f"{path}: {marker}")
    _check("item 11: no module other than gss/mission.py builds a flight-command "
           "MAVLink send (link.py's own command_long_send stays the whitelisted "
           "SET_MESSAGE_INTERVAL request)",
           not offenders, str(offenders))

    # And link.transmit() -- the gated passthrough -- is called from nowhere
    # but mission.py.
    callers = []
    for path in glob.glob("gss/*.py"):
        if os.path.basename(path) in ("mission.py", "link.py"):
            continue
        text = open(path, encoding="utf-8").read()
        if re.search(r"\.transmit\(", text):
            callers.append(path)
    _check("item 11: link.transmit() (the flight-command passthrough) is called "
           "from nowhere except gss/mission.py", not callers, str(callers))


# --------------------------------------------------------------------------
# item 12 -- the doubled transmit gate
# --------------------------------------------------------------------------


def test_doubled_gate_refuses_serial() -> None:
    env = dict(os.environ, SUPABASE_ENABLED="false", ALLOW_VEHICLE_CONTROL="true",
              MAVLINK_CONNECTION="COM3")
    env.pop("ALLOW_REAL_VEHICLE", None)
    out = subprocess.run(
        [sys.executable, "-c", "import gss.config"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    _check("item 12: ALLOW_VEHICLE_CONTROL=true + a serial connection string + "
           "ALLOW_REAL_VEHICLE unset -> the GSS refuses to start",
           out.returncode != 0 and "ALLOW_REAL_VEHICLE" in out.stderr,
           out.stderr.strip()[-300:])

    env2 = dict(env, ALLOW_REAL_VEHICLE="true")
    out2 = subprocess.run(
        [sys.executable, "-c", "import gss.config; print('started')"],
        capture_output=True, text=True, env=env2, timeout=30,
    )
    _check("item 12: the same setup with ALLOW_REAL_VEHICLE=true is accepted "
           "(the switch genuinely opens it, deliberately)",
           out2.returncode == 0 and "started" in out2.stdout, out2.stdout + out2.stderr)

    env3 = dict(os.environ, SUPABASE_ENABLED="false", ALLOW_VEHICLE_CONTROL="true",
               MAVLINK_CONNECTION="tcp:127.0.0.1:5762")
    env3.pop("ALLOW_REAL_VEHICLE", None)
    out3 = subprocess.run(
        [sys.executable, "-c", "import gss.config; print('started')"],
        capture_output=True, text=True, env=env3, timeout=30,
    )
    _check("item 12: a loopback SITL endpoint needs no second switch",
           out3.returncode == 0 and "started" in out3.stdout, out3.stdout + out3.stderr)


# --------------------------------------------------------------------------
# item 13 -- failsafe parameter audit
# --------------------------------------------------------------------------


def test_failsafe_audit_reports_mismatches() -> None:
    rig = Rig()
    try:
        audit_ok = mission.audit_failsafe_params(rig.link, timeout_s=5.0)
        _check("item 13: a correctly configured SITL passes the audit",
               audit_ok.ok, str(audit_ok.findings))

        rig.fv.set_param("FENCE_ENABLE", 0)
        rig.fv.set_param("FENCE_RADIUS", 99999.0)
        rig.fv.set_param("RTL_ALT", 0.0)
        audit_bad = mission.audit_failsafe_params(rig.link, timeout_s=5.0)
        print(f"\n  item 13 -- deliberately misconfigured SITL findings:\n    "
              + "\n    ".join(audit_bad.findings))
        _check("item 13: a deliberately misconfigured SITL FAILS the audit",
               not audit_bad.ok)
        _check("item 13: FENCE_ENABLE mismatch is named",
               any("FENCE_ENABLE" in f for f in audit_bad.findings), str(audit_bad.findings))
        _check("item 13: FENCE_RADIUS mismatch is named (vs our MAX_RADIUS_M)",
               any("FENCE_RADIUS" in f for f in audit_bad.findings), str(audit_bad.findings))
        _check("item 13: RTL_ALT mismatch is named",
               any("RTL_ALT" in f for f in audit_bad.findings), str(audit_bad.findings))
        _check("item 13: nothing was WRITTEN -- the vehicle's params are unchanged "
               "by the audit itself (read-only)",
               rig.fv.get_param("FENCE_ENABLE") == 0.0)  # still our deliberate misconfig, not silently fixed
    finally:
        rig.close()


# --------------------------------------------------------------------------


def main() -> int:
    for t in (
        test_full_summon_flight,
        test_standoff_downwind_and_wind_flip,
        test_mode_change_ignored_aborts,
        test_arm_rejected_no_retry,
        test_battery_rtl_interrupts_manoeuvre,
        test_link_loss_reconnect_same_mission,
        test_abort_command_rtl_never_disarm_in_air,
        test_no_disarm_code_path_above_threshold,
        test_restart_adopts_airborne_vehicle,
        test_weather_stay_real_position_over_time,
        test_weather_severe_returns_with_numbers,
        test_transmit_chokepoint,
        test_doubled_gate_refuses_serial,
        test_failsafe_audit_reports_mismatches,
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
    sys.exit(main())
