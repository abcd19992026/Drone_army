"""mission.py -- MavlinkExecutor: real MAVLink flight, in SITL (v0.6).

Through v0.5 every "flight" was a :class:`~gss.executor.DryRunExecutor` walking
a timeline and transmitting nothing. This module makes the drone actually fly.
It sits behind :mod:`gss.safety` (the veto) and :mod:`gss.weather` (conditions),
exactly as the earlier phases built it to.

THE TRANSMIT CHOKEPOINT
-----------------------
Every byte this project sends to move the aircraft goes through :func:`_transmit`
and nowhere else. ``gss.link`` exposes one gated passthrough (:meth:`MavlinkLink.transmit`)
and this is its only caller. ``tests/test_mission.py`` asserts that no other
module builds a flight command.

NEVER ASSUME A COMMAND TOOK EFFECT
---------------------------------
The single largest source of bugs in this kind of code. Every state change is
requested and then CONFIRMED from telemetry before the sequence proceeds:

    mode change  -> HEARTBEAT reports the new mode
    arm          -> the armed flag AND a COMMAND_ACK
    takeoff      -> relative altitude actually rising, and reaching the target
    waypoint     -> distance to the target actually decreasing

Each confirmation has a timeout. A timeout is a FAILURE -- the mission aborts
through the normal controlled-return path with a specific reason, never
"continue hopefully". Arming is never retried after a rejection: ArduPilot
rejects arming for a reason and repeating the request does not address it.

ABORT IS A CONTROLLED RETURN, NEVER A DISARM
-------------------------------------------
There is no code path in this module that disarms an armed vehicle above
``config.DISARM_MAX_ALT_M``. Cutting motors in flight turns a recoverable
situation into a falling object. :meth:`abort` and every safety verdict map to
a manoeuvre (HOLD / DESCEND / RTL / DIVERT / LAND), not to a kill.

ARDUPILOT'S FAILSAFES -- VERIFY, DO NOT REPLACE
----------------------------------------------
:func:`audit_failsafe_params` reads (never writes) the vehicle's battery / GCS
failsafe and fence parameters at startup and reports mismatches loudly. A human
changes them in Mission Planner. Our safety.py is the outer layer; ArduPilot's
failsafes are the inner one; both must exist.

GSS RESTART WHILE AIRBORNE
-------------------------
:func:`recover_airborne_vehicle` runs at startup: if the vehicle is armed and
above ``DISARM_MAX_ALT_M`` there is an aircraft in the sky with no software
flying it. It adopts the vehicle and orders RTL immediately -- recovering the
aircraft comes first, tidying the database second.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from gss import config
from gss.executor import Executor, MissionPhase
from gss.snapshot import TelemetrySnapshot

log = logging.getLogger(__name__)

SnapshotSource = Callable[[], TelemetrySnapshot]

# --- MAVLink constants (kept local; the dialect import lives behind link.py) --
try:
    from pymavlink import mavutil

    _M = mavutil.mavlink
    _CMD_SET_MODE = _M.MAV_CMD_DO_SET_MODE
    _CMD_ARM = _M.MAV_CMD_COMPONENT_ARM_DISARM
    _CMD_TAKEOFF = _M.MAV_CMD_NAV_TAKEOFF
    _MODE_FLAG_CUSTOM = _M.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
    _FRAME_GLOBAL_REL_ALT = _M.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT
    _RESULT_ACCEPTED = _M.MAV_RESULT_ACCEPTED
    _RESULT_IN_PROGRESS = getattr(_M, "MAV_RESULT_IN_PROGRESS", 5)
except Exception:  # pragma: no cover -- pymavlink is a hard dependency
    mavutil = None  # type: ignore
    _CMD_SET_MODE = 176
    _CMD_ARM = 400
    _CMD_TAKEOFF = 22
    _MODE_FLAG_CUSTOM = 1
    _FRAME_GLOBAL_REL_ALT = 6
    _RESULT_ACCEPTED = 0
    _RESULT_IN_PROGRESS = 5

# ArduCopter custom-mode numbers. Confirmation reads back the mode STRING from
# HEARTBEAT (mavutil.mode_string_v10), so these are only used when transmitting.
_COPTER_MODES: dict[str, int] = {
    "STABILIZE": 0, "GUIDED": 4, "LOITER": 5, "RTL": 6, "LAND": 9, "BRAKE": 17,
}

# SET_POSITION_TARGET_GLOBAL_INT type_mask: ignore everything except position
# (bits for vx,vy,vz,ax,ay,az,yaw,yaw_rate all set = ignored).
_POS_TARGET_IGNORE_ALL_BUT_POSITION = 0b0000_1111_1111_1000

_EARTH_RADIUS_M = 6_371_000.0

# The failsafe parameters we expect ArduPilot to have set, and what "sane"
# means for each. Starting points -- a human tunes the real values in Mission
# Planner; the audit only flags a mismatch, it never writes.
_EXPECTED_FAILSAFE = {
    "BATT_LOW_VOLT": ("> 0 (battery low-voltage failsafe voltage set)", lambda v: v > 0),
    "BATT_FS_LOW_ACT": ("2 = RTL on low battery", lambda v: int(v) in (2, 3, 4)),
    "FS_GCS_ENABLE": ("1 = GCS failsafe enabled", lambda v: int(v) >= 1),
    "FENCE_ENABLE": ("1 = geofence enabled", lambda v: int(v) == 1),
    "FENCE_RADIUS": (
        f"<= our MAX_RADIUS_M ({config.MAX_RADIUS_M:.0f} m)",
        lambda v: 0 < v <= config.MAX_RADIUS_M + 1.0,
    ),
    "FENCE_ALT_MAX": (
        f"<= our MAX_ALT_M ({config.MAX_ALT_M:.0f} m)",
        lambda v: 0 < v <= config.MAX_ALT_M + 1.0,
    ),
    "RTL_ALT": (
        "a sane RTL altitude (>= our MIN_ALT_M, <= our MAX_ALT_M), in cm",
        lambda v: config.MIN_ALT_M * 100 <= v <= config.MAX_ALT_M * 100 + 1,
    ),
}


class Directive(str, Enum):
    """A mid-mission order from the supervisor (a safety.py / weather.py verdict
    or an abort command). Checked before and DURING every manoeuvre."""

    NONE = "NONE"
    HOLD = "HOLD"          # hold position, keep evaluating
    DESCEND = "DESCEND"    # stop horizontal motion, descend to a safe altitude
    RTL = "RTL"            # return to the dock and land
    DIVERT = "DIVERT"      # fly to the chosen safe spot and land there
    LAND_NOW = "LAND_NOW"  # land where it is, now


# Verdict string (as safety.py / the supervisor emits it) -> Directive.
# ALLOW/WARN map to NONE -- the verdict has returned to nominal, which RELEASES
# a pending HOLD/DESCEND (see on_safety_action below). They are listed here,
# not left to the .get() default, so a typo'd or renamed verdict string never
# silently becomes a release.
_VERDICT_TO_DIRECTIVE: dict[str, Directive] = {
    "ALLOW": Directive.NONE,
    "WARN": Directive.NONE,
    "HOLD": Directive.HOLD,
    "DESCEND": Directive.DESCEND,
    "RTL_NOW": Directive.RTL,
    "DIVERT": Directive.DIVERT,
    "LAND_NOW": Directive.LAND_NOW,
}
_ENDING_DIRECTIVES = (Directive.RTL, Directive.DIVERT, Directive.LAND_NOW)


class _Abort(Exception):
    """Raised inside the flight sequence to break out to the return handler."""

    def __init__(self, reason: str, directive: Directive = Directive.RTL) -> None:
        super().__init__(reason)
        self.reason = reason
        self.directive = directive


class _Stopped(Exception):
    """Raised inside the flight sequence when close() has fired mid-step.

    Unlike _Abort, this unwinds ALL the way out of _fly() with NO further
    transmission and no _run_abort() dispatch -- close()'s whole contract is
    "stop watching, command nothing" (R5: a real flight in progress must not
    be aborted just because the GSS process is stopping). A step that simply
    `return`s on `self._stop.is_set()` would be worse than doing nothing: the
    caller (_do) has no way to tell "step genuinely confirmed" from "step gave
    up because we're shutting down", and would go on to run the *next* step
    anyway, silently racing the whole sequence to a false LANDED before
    recover_airborne_vehicle() ever gets a chance to run.
    """


# ===========================================================================
# geometry -- local copy (mission.py couples to nothing it does not need)
# ===========================================================================


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


def _destination(lat: float, lon: float, bearing_deg: float, dist_m: float) -> tuple[float, float]:
    """The point ``dist_m`` from (lat, lon) along ``bearing_deg``."""
    d = dist_m / _EARTH_RADIUS_M
    br = math.radians(bearing_deg)
    p1 = math.radians(lat)
    l1 = math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(br))
    l2 = l1 + math.atan2(
        math.sin(br) * math.sin(d) * math.cos(p1),
        math.cos(d) - math.sin(p1) * math.sin(p2),
    )
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


def standoff_point(
    person_lat: float,
    person_lon: float,
    *,
    dock_lat: float,
    dock_lon: float,
    offset_m: float,
    wind_from_deg: float | None,
) -> tuple[float, float, str]:
    """R2: the on-station point, ``offset_m`` horizontally from the person, on
    the DOWNWIND side.

    A drone that loses power drifts and falls downwind. Placed upwind, a failure
    carries it toward the person -- the exact outcome the offset exists to
    prevent. Downwind = the direction the wind is blowing TOWARD =
    ``wind_from_deg + 180``.

    If the wind is unknown, offset along the bearing from the person toward the
    dock, so the drone is already pointed down its return path.

    Returns ``(lat, lon, basis)`` where basis is "downwind" or "toward-dock".
    """
    if wind_from_deg is not None:
        bearing = (wind_from_deg + 180.0) % 360.0
        basis = "downwind"
    else:
        bearing = _bearing_deg(person_lat, person_lon, dock_lat, dock_lon)
        basis = "toward-dock"
    lat, lon = _destination(person_lat, person_lon, bearing, offset_m)
    return lat, lon, basis


# ===========================================================================
# THE transmit chokepoint
# ===========================================================================


def _transmit(link, build_send: Callable[[object], None], *, what: str,
              clear_ack_for: int | None = None) -> bool:
    """Every flight command in this project is built and sent HERE, and nowhere
    else. ``build_send(conn)`` performs exactly one ``conn.mav.*_send(...)``.
    Returns True if the bytes went onto the wire (not that the vehicle obeyed --
    that is a separate confirmation)."""
    return link.transmit(build_send, what=what, clear_ack_for=clear_ack_for)


def _tx_set_mode(link, mode: str) -> bool:
    mode_id = _COPTER_MODES[mode]
    return _transmit(
        link,
        lambda c: c.mav.command_long_send(
            c.target_system, c.target_component, _CMD_SET_MODE, 0,
            float(_MODE_FLAG_CUSTOM), float(mode_id), 0, 0, 0, 0, 0,
        ),
        what=f"set mode {mode}", clear_ack_for=_CMD_SET_MODE,
    )


def _tx_arm(link, arm: bool) -> bool:
    return _transmit(
        link,
        lambda c: c.mav.command_long_send(
            c.target_system, c.target_component, _CMD_ARM, 0,
            1.0 if arm else 0.0, 0, 0, 0, 0, 0, 0,
        ),
        what="arm" if arm else "disarm", clear_ack_for=_CMD_ARM,
    )


def _tx_takeoff(link, alt_m: float) -> bool:
    return _transmit(
        link,
        lambda c: c.mav.command_long_send(
            c.target_system, c.target_component, _CMD_TAKEOFF, 0,
            0, 0, 0, 0, 0, 0, float(alt_m),
        ),
        what=f"takeoff to {alt_m:.0f} m", clear_ack_for=_CMD_TAKEOFF,
    )


def _tx_position_target(link, lat: float, lon: float, alt_rel_m: float) -> bool:
    return _transmit(
        link,
        lambda c: c.mav.set_position_target_global_int_send(
            0, c.target_system, c.target_component, _FRAME_GLOBAL_REL_ALT,
            _POS_TARGET_IGNORE_ALL_BUT_POSITION,
            int(round(lat * 1e7)), int(round(lon * 1e7)), float(alt_rel_m),
            0, 0, 0, 0, 0, 0, 0, 0,
        ),
        what=f"goto {lat:.6f},{lon:.6f} @ {alt_rel_m:.0f} m",
    )


# ===========================================================================
# ArduPilot failsafe audit (read only -- never writes a parameter)
# ===========================================================================


@dataclass
class FailsafeAudit:
    ok: bool
    findings: list[str] = field(default_factory=list)
    values: dict[str, float | None] = field(default_factory=dict)


def audit_failsafe_params(link, timeout_s: float = 8.0) -> FailsafeAudit:
    """Read the vehicle's failsafe / fence parameters and compare them against
    what this project expects. Reports mismatches; writes NOTHING."""
    names = list(_EXPECTED_FAILSAFE)
    values = link.read_params(names, timeout_s=timeout_s)
    findings: list[str] = []
    for name, (expected, ok_fn) in _EXPECTED_FAILSAFE.items():
        v = values.get(name)
        if v is None:
            findings.append(f"{name}: not reported by the vehicle (expected {expected})")
            continue
        try:
            good = bool(ok_fn(v))
        except Exception:  # noqa: BLE001
            good = False
        if not good:
            findings.append(f"{name} = {v} -- expected {expected}")
    audit = FailsafeAudit(ok=not findings, findings=findings, values=values)
    if audit.ok:
        log.info("failsafe audit: ArduPilot failsafe/fence params look sane (%s)",
                 ", ".join(f"{k}={values[k]}" for k in names if values.get(k) is not None))
    else:
        log.error(
            "failsafe audit: %d mismatch(es) -- our safety.py is the OUTER layer, "
            "ArduPilot's failsafes are the inner one, and both must be right. "
            "Fix these in Mission Planner (this code will not write them):\n  - %s",
            len(findings), "\n  - ".join(findings),
        )
    return audit


# ===========================================================================
# GSS restart while airborne
# ===========================================================================


def recover_airborne_vehicle(link, snapshot_source: SnapshotSource) -> dict | None:
    """Startup check. If the vehicle is armed and above DISARM_MAX_ALT_M, there
    is an aircraft in the sky and no software flying it. Adopt it and order RTL
    NOW. Returns a detail dict for a ``vehicle_adopted`` mission_events row, or
    None if there is nothing airborne to recover.
    """
    snap = snapshot_source()
    if not snap.connected or snap.armed is not True:
        return None
    alt = snap.alt_m_relative
    if alt is None or alt <= config.DISARM_MAX_ALT_M:
        return None

    log.critical(
        "STARTUP: found an ARMED vehicle at %.1f m relative that this GSS did "
        "NOT launch. Adopting it and ordering RTL now. Recovering the aircraft "
        "comes first; the database comes second.", alt,
    )
    detail = {
        "found_armed": True,
        "alt_m_relative": round(alt, 1),
        "mode": snap.mode,
        "lat": snap.lat,
        "lon": snap.lon,
        "battery_pct": snap.battery_pct,
        "action": "RTL",
    }
    if not config.ALLOW_VEHICLE_CONTROL:
        detail["action"] = "RTL NOT SENT -- ALLOW_VEHICLE_CONTROL is false"
        log.critical(
            "STARTUP: cannot send RTL -- ALLOW_VEHICLE_CONTROL is false. A human "
            "must recover the vehicle. Its own GCS failsafe should bring it home."
        )
        return detail
    ok = False
    for _ in range(max(1, config.MAVLINK_CMD_RETRIES)):
        if _tx_set_mode(link, "RTL"):
            ok = True
            break
        time.sleep(0.5)
    detail["rtl_sent"] = ok
    if not ok:
        log.critical("STARTUP: RTL command could not be transmitted to the adopted vehicle")
    return detail


# ===========================================================================
# MavlinkExecutor
# ===========================================================================


class MavlinkExecutor(Executor):
    """Flies one mission over real MAVLink, in SITL. Confirms every step."""

    def __init__(
        self,
        mission_id: str,
        command: dict,
        snapshot_source: SnapshotSource,
        *,
        link,
        safety=None,
        weather=None,
        home_lat: float | None = None,
        home_lon: float | None = None,
        on_station_alt_m: float | None = None,
        loiter_seconds: float | None = None,
    ) -> None:
        self._mission_id = mission_id
        self._command = command
        self._snapshot = snapshot_source
        self._link = link
        self._safety = safety
        self._weather = weather
        self._home_lat = home_lat if home_lat is not None else config.HOME_LAT
        self._home_lon = home_lon if home_lon is not None else config.HOME_LON
        self._on_station_alt = (
            on_station_alt_m if on_station_alt_m is not None else config.ON_STATION_ALT
        )
        self._cruise_out = config.CRUISE_ALT_OUTBOUND
        self._cruise_in = config.CRUISE_ALT_INBOUND
        self._loiter_s = float(loiter_seconds) if loiter_seconds else 0.0
        self._is_summon = command.get("type") == "summon"

        self._target_lat = _f(command.get("target_lat"))
        self._target_lon = _f(command.get("target_lon"))
        params = command.get("params") if isinstance(command.get("params"), dict) else {}
        self._person_lat = _f(params.get("human_lat")) or self._target_lat
        self._person_lon = _f(params.get("human_lon")) or self._target_lon

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._phase = MissionPhase.QUEUED
        self._abort_reason: str | None = None
        self._directive = Directive.NONE
        self._directive_reason = ""
        # Which check ordered a HOLD directive ("lateral_offset" for R2,
        # None for every other HOLD cause). Only meaningful while
        # self._directive == Directive.HOLD -- see on_safety_action and
        # _resolve_r2_hold.
        self._directive_kind: str | None = None
        self._divert_spot: dict | None = None
        self._alt_ceiling = config.MAX_ALT_M   # DESCEND lowers this
        self._standoff: tuple[float, float, str] | None = None
        self._events: list[tuple[str, dict]] = []
        self._adopted = False

    # ---------------------------------------------------------------- API

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._fly, name=f"mission-{self._mission_id[:8]}", daemon=True
            )
        log.info(
            "mission %s: MavlinkExecutor starting -- REAL MAVLink flight "
            "(%s, target %s,%s, loiter %.0fs)",
            self._mission_id, self._command.get("type"),
            self._target_lat, self._target_lon, self._loiter_s,
        )
        self._thread.start()

    def poll(self) -> MissionPhase:
        with self._lock:
            return self._phase

    def abort(self, reason: str) -> None:
        """A controlled RETURN. NEVER a disarm above DISARM_MAX_ALT_M."""
        self.on_safety_action("RTL_NOW", reason)

    def on_safety_action(
        self,
        action: str,
        reason: str,
        *,
        divert_spot: dict | None = None,
        hold_kind: str | None = None,
    ) -> None:
        directive = _VERDICT_TO_DIRECTIVE.get(action, Directive.RTL)
        with self._lock:
            # Once ending (RTL/DIVERT/LAND_NOW), nothing softens or replaces
            # it -- not even another ending directive of lower rank, and
            # certainly not a release. The mission is already on its way to
            # ABORTED.
            if self._directive in _ENDING_DIRECTIVES:
                return
            if directive == Directive.NONE:
                # ALLOW/WARN: the verdict is back to nominal. RELEASE a
                # pending HOLD/DESCEND -- this is the only path back to NONE;
                # nothing else may ever lower the directive (see below). A
                # real-SITL finding (v0.6.1): without this, a HOLD ordered
                # once could never clear on its own even after the underlying
                # condition resolved, because every other directive value
                # only ranks upward from here.
                if self._directive != Directive.NONE:
                    log.info(
                        "mission %s: directive released (was %s) -- %s",
                        self._mission_id, self._directive.value, reason,
                    )
                self._directive = Directive.NONE
                self._directive_reason = ""
                self._directive_kind = None
                return
            # Never replace a live directive with another live one of lower
            # rank: e.g. an active DESCEND (rank 2) is not softened back to a
            # mere HOLD (rank 1) by a stale/racing verdict.
            if _rank(directive) < _rank(self._directive):
                return
            self._directive = directive
            self._directive_reason = reason
            self._directive_kind = hold_kind if directive == Directive.HOLD else None
            if divert_spot:
                self._divert_spot = divert_spot
        log.warning(
            "mission %s: directive %s -- %s", self._mission_id, directive.value, reason
        )

    @property
    def abort_reason(self) -> str | None:
        with self._lock:
            return self._abort_reason

    def drain_events(self) -> list[tuple[str, dict]]:
        """Extra mission_events the executor produced (adoption, GPS-only
        landing, standoff recompute). The supervisor writes them off the flight
        path. Phase events (armed/takeoff/enroute/...) stay the supervisor's."""
        with self._lock:
            out, self._events = self._events, []
        return out

    def close(self) -> None:
        """The GSS is shutting down. Do NOT abort a real mission -- the aircraft
        keeps flying its last GUIDED setpoint / onboard failsafes, and a
        restarted GSS reconciles (R5). Just stop our thread."""
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=3.0)

    # ---------------------------------------------------------- the flight

    def _fly(self) -> None:
        try:
            self._preflight_gate()
            self._do("set GUIDED", lambda: self._step_set_mode("GUIDED"))
            self._do("arm", self._step_arm)
            self._set_phase(MissionPhase.LAUNCHING)
            self._do("takeoff", lambda: self._step_takeoff(self._cruise_out))
            self._set_phase(MissionPhase.ENROUTE)
            lat, lon = self._compute_standoff()
            self._do("fly to station", lambda: self._step_goto(lat, lon, self._cruise_out))
            # ON_STATION means genuinely on station: at the on-station altitude,
            # about to loiter -- so the phase (and its 'arrived'/'loiter_start'
            # events) is entered AFTER the descent, not before it.
            self._do("descend to on-station alt", lambda: self._step_change_alt(self._on_station_alt))
            self._set_phase(MissionPhase.ON_STATION)
            self._do("loiter", self._step_loiter)
            # Likewise RETURNING (and its 'rtl' event): entered once actually
            # climbing away at the inbound cruise altitude, not one tick early.
            self._do("climb to inbound alt", lambda: self._step_change_alt(self._cruise_in))
            self._set_phase(MissionPhase.RETURNING)
            self._do("return + land", self._step_rtl_and_land)
            self._set_phase(MissionPhase.LANDED)
            log.info("mission %s: landed. Flight complete.", self._mission_id)
        except _Stopped:
            log.info("mission %s: flight thread stopping (GSS shutdown) -- "
                     "vehicle left exactly as it is, commanding nothing further",
                     self._mission_id)
        except _Abort as ab:
            self._run_abort(ab)
        except Exception as exc:  # noqa: BLE001 -- R8: never silent
            log.exception("mission %s: unexpected error in the flight sequence", self._mission_id)
            self._run_abort(_Abort(f"internal error: {type(exc).__name__}: {exc}", Directive.RTL))

    def _do(self, what: str, step: Callable[[], None]) -> None:
        """Run one sequence step, honouring a pending directive first."""
        self._check_directive()
        log.info("mission %s: step -- %s", self._mission_id, what)
        step()

    # -- directives -------------------------------------------------------

    def _check_directive(self) -> None:
        """Called before and repeatedly DURING every manoeuvre. HOLD/DESCEND are
        handled here without ending the mission; RTL/DIVERT/LAND_NOW raise."""
        with self._lock:
            d, reason, kind = self._directive, self._directive_reason, self._directive_kind
        if d in _ENDING_DIRECTIVES:
            raise _Abort(reason or d.value, d)
        if d == Directive.HOLD:
            if kind == "lateral_offset":
                self._resolve_r2_hold(reason)
            else:
                self._hold_position("safety HOLD", reason)
        elif d == Directive.DESCEND:
            self._descend_and_hold(reason)

    def _hold_position(self, label: str, reason: str) -> None:
        """Keep commanding the current position until the directive clears or
        escalates. safety.py keeps evaluating and will lift it or raise it."""
        snap = self._snapshot()
        hold_lat = snap.lat if snap.lat is not None else self._home_lat
        hold_lon = snap.lon if snap.lon is not None else self._home_lon
        hold_alt = min(snap.alt_m_relative or self._on_station_alt, self._alt_ceiling)
        log.warning("mission %s: %s -- %s", self._mission_id, label, reason)
        while not self._stop.is_set():
            with self._lock:
                d = self._directive
            if d in _ENDING_DIRECTIVES:
                raise _Abort(self._directive_reason or d.value, d)
            if d not in (Directive.HOLD, Directive.DESCEND):
                log.info("mission %s: %s released", self._mission_id, label)
                return
            _tx_position_target(self._link, hold_lat, hold_lon, hold_alt)
            self._sleep(1.0 / config.POSITION_TARGET_RATE_HZ)
        raise _Stopped()

    def _resolve_r2_hold(self, reason: str) -> None:
        """R2-specific HOLD (safety.py's lateral-offset check, tagged
        ``hold_kind == "lateral_offset"``): the real-SITL fix for a HOLD that
        could only ever re-send the current, already-too-close position and
        had no way to resolve on its own.

        Actively recomputes the standoff point against the CURRENT human
        position and wind -- :meth:`_compute_standoff`, the SAME computation
        used at mission start, not a bespoke escape manoeuvre -- and flies to
        it, opening the distance tick by tick. Releases exactly like
        :meth:`_hold_position` does: when the directive changes (safety.py's
        own next tick clears the HOLD once the distance is safe again, or
        escalates it).

        If this has not resolved within ``R2_ESCAPE_TIMEOUT_S``, ordinary GPS
        / arrival noise is not a plausible explanation any more -- it is a
        configuration or geometry problem (the recomputed point itself
        cannot be reached, or the human is being reported as moving faster
        than the aircraft can open distance). Holding any longer just repeats
        the defect this method exists to fix: escalate to RTL_NOW instead.
        """
        start_mono = time.monotonic()
        period = 1.0 / config.POSITION_TARGET_RATE_HZ
        log.warning(
            "mission %s: safety HOLD (R2) -- recomputing the standoff point "
            "and repositioning to open the distance: %s",
            self._mission_id, reason,
        )
        while not self._stop.is_set():
            with self._lock:
                d, kind = self._directive, self._directive_kind
            if d in _ENDING_DIRECTIVES:
                raise _Abort(self._directive_reason or d.value, d)
            if not (d == Directive.HOLD and kind == "lateral_offset"):
                log.info("mission %s: R2 HOLD released", self._mission_id)
                return
            lat, lon = self._compute_standoff(quiet=True)
            snap = self._snapshot()
            if (
                snap.lat is not None and snap.lon is not None
                and self._person_lat is not None and self._person_lon is not None
            ):
                dist = _haversine_m(snap.lat, snap.lon, self._person_lat, self._person_lon)
                log.info(
                    "mission %s: R2 HOLD repositioning -- %.1f m from the "
                    "human (target >= %.1f m)", self._mission_id, dist,
                    config.LATERAL_OFFSET_M + config.STANDOFF_MARGIN_M,
                )
            _tx_position_target(self._link, lat, lon, min(self._on_station_alt, self._alt_ceiling))
            if time.monotonic() - start_mono > config.R2_ESCAPE_TIMEOUT_S:
                raise _Abort(
                    f"R2 HOLD did not resolve within "
                    f"{config.R2_ESCAPE_TIMEOUT_S:.0f}s despite repositioning "
                    f"toward the recomputed standoff point -- treating as a "
                    f"configuration/geometry fault, not transient noise: {reason}",
                    Directive.RTL,
                )
            self._sleep(period)
        raise _Stopped()

    def _descend_and_hold(self, reason: str) -> None:
        """DESCEND: stop horizontal motion, come down to a safe altitude, hold,
        keep evaluating. When it clears the mission continues AT THE LOWER
        altitude -- it does not climb back through the ceiling it just breached.
        """
        with self._lock:
            self._alt_ceiling = min(self._alt_ceiling, self._on_station_alt)
        self._emit("standoff_recomputed", {"note": "DESCEND -- altitude ceiling lowered",
                                           "ceiling_m": self._alt_ceiling, "reason": reason})
        self._hold_position("safety DESCEND", reason)

    # -- confirmed steps ------------------------------------------------

    def _preflight_gate(self) -> None:
        """Everything that must be true before ANY arm. safety.py pre-flight
        ALLOW, weather ALLOW, EKF healthy, GPS adequate, home set, disarmed, on
        the ground -- and ArduPilot's own pre-arm status."""
        snap = self._snapshot()
        fails: list[str] = []
        if not snap.connected:
            fails.append("link to the vehicle is down")
        if snap.armed is not False:
            fails.append(f"vehicle is not disarmed (armed={snap.armed})")
        alt = snap.alt_m_relative
        if alt is None or alt > config.DISARM_MAX_ALT_M + 1.0:
            fails.append(f"vehicle is not on the ground (alt_m_relative={alt})")
        if snap.ekf_healthy is not True:
            fails.append(f"EKF not reported healthy (ekf_healthy={snap.ekf_healthy})")
        if snap.home_set is not True:
            fails.append("home position not set")
        if (snap.gps_fix_type or 0) < 3:
            fails.append(f"GPS fix type {snap.gps_fix_type} (need 3D)")
        if (snap.gps_satellites or 0) < config.GPS_MIN_SATELLITES:
            fails.append(f"{snap.gps_satellites} satellites (need {config.GPS_MIN_SATELLITES})")
        if snap.prearm_fail_text:
            fails.append(f"ArduPilot pre-arm check failing: {snap.prearm_fail_text!r}")
            self._emit("prearm_block", {"text": snap.prearm_fail_text})

        if self._safety is not None:
            try:
                verdict = self._safety.evaluate_command(self._command)
                if verdict.action.value != "ALLOW":
                    fails.append(f"safety.py pre-flight: {verdict.action.value} -- "
                                 + "; ".join(verdict.reasons))
            except Exception as exc:  # noqa: BLE001
                fails.append(f"safety.py pre-flight check errored: {exc}")
        if self._weather is not None:
            try:
                wx = self._weather.forecast_verdict()
                if wx.tier.value == "SEVERE":
                    fails.append("weather.py pre-flight: SEVERE -- " + "; ".join(wx.reasons))
            except Exception as exc:  # noqa: BLE001
                fails.append(f"weather.py pre-flight check errored: {exc}")

        if fails:
            raise _Abort("pre-arm gate refused: " + "; ".join(fails), Directive.NONE)
        log.info("mission %s: pre-arm gate clear", self._mission_id)

    def _step_set_mode(self, mode: str) -> None:
        for attempt in range(1, config.MAVLINK_CMD_RETRIES + 1):
            if self._stop.is_set():
                raise _Stopped()
            self._check_directive()
            if not _tx_set_mode(self._link, mode):
                raise _Abort(f"could not transmit '{mode}' mode change", Directive.RTL)
            deadline = time.monotonic() + config.MODE_CONFIRM_TIMEOUT_S
            while time.monotonic() < deadline and not self._stop.is_set():
                if (self._snapshot().mode or "").upper() == mode:
                    log.info("mission %s: mode confirmed %s", self._mission_id, mode)
                    return
                self._sleep(0.2)
            if self._stop.is_set():
                raise _Stopped()
            log.warning("mission %s: mode %s not confirmed (attempt %d/%d)",
                        self._mission_id, mode, attempt, config.MAVLINK_CMD_RETRIES)
        raise _Abort(
            f"mode change to {mode} not confirmed by HEARTBEAT within "
            f"{config.MODE_CONFIRM_TIMEOUT_S:.0f}s x{config.MAVLINK_CMD_RETRIES}",
            Directive.RTL,
        )

    def _step_arm(self) -> None:
        if not _tx_arm(self._link, True):
            raise _Abort("could not transmit the arm command", Directive.NONE)
        # Wait for the COMMAND_ACK first: a rejection is final, never retried.
        deadline = time.monotonic() + config.MAVLINK_CMD_TIMEOUT_S
        ack = None
        while time.monotonic() < deadline and not self._stop.is_set():
            ack = self._link.command_ack(_CMD_ARM)
            if ack is not None:
                break
            self._sleep(0.1)
        if self._stop.is_set():
            raise _Stopped()
        if ack is not None and ack[0] not in (_RESULT_ACCEPTED, _RESULT_IN_PROGRESS):
            name = _result_name(ack[0])
            raise _Abort(
                f"ArduPilot REJECTED arming ({name}). Not retrying -- it rejects "
                f"for a reason and repeating the request does not address it.",
                Directive.NONE,
            )
        # Then confirm the armed flag.
        deadline = time.monotonic() + config.ARM_CONFIRM_TIMEOUT_S
        while time.monotonic() < deadline and not self._stop.is_set():
            if self._snapshot().armed is True:
                log.info("mission %s: armed (ACK %s)", self._mission_id,
                         _result_name(ack[0]) if ack else "none")
                return
            self._sleep(0.2)
        if self._stop.is_set():
            raise _Stopped()
        raise _Abort(
            f"arm not confirmed by telemetry within {config.ARM_CONFIRM_TIMEOUT_S:.0f}s "
            f"(ACK was {_result_name(ack[0]) if ack else 'never received'})",
            Directive.NONE,
        )

    def _step_takeoff(self, alt_m: float) -> None:
        alt_m = min(alt_m, self._alt_ceiling)
        start_alt = self._snapshot().alt_m_relative or 0.0
        if not _tx_takeoff(self._link, alt_m):
            raise _Abort("could not transmit the takeoff command", Directive.RTL)
        deadline = time.monotonic() + config.TAKEOFF_TIMEOUT_S
        rising_seen = False
        last_alt = start_alt
        while time.monotonic() < deadline and not self._stop.is_set():
            self._check_directive()
            cur = self._snapshot().alt_m_relative
            if cur is not None:
                if cur > last_alt + 0.2:
                    rising_seen = True
                last_alt = cur
                if abs(cur - alt_m) <= config.ALT_TOLERANCE_M:
                    log.info("mission %s: takeoff confirmed at %.1f m", self._mission_id, cur)
                    return
            self._sleep(0.5)
        if self._stop.is_set():
            raise _Stopped()
        raise _Abort(
            f"takeoff not confirmed within {config.TAKEOFF_TIMEOUT_S:.0f}s "
            f"(alt {last_alt:.1f}/{alt_m:.1f} m, "
            f"{'never rose' if not rising_seen else 'stalled below target'})",
            Directive.RTL,
        )

    def _step_goto(self, lat: float, lon: float, alt_m: float) -> None:
        alt_m = min(alt_m, self._alt_ceiling)
        start = self._snapshot()
        start_dist = _dist_or(start, lat, lon, default=1e9)
        best = start_dist
        no_progress_since = time.monotonic()
        step_deadline = time.monotonic() + config.MISSION_STEP_TIMEOUT_S
        period = 1.0 / config.POSITION_TARGET_RATE_HZ
        while time.monotonic() < step_deadline and not self._stop.is_set():
            self._check_directive()
            _tx_position_target(self._link, lat, lon, min(alt_m, self._alt_ceiling))
            snap = self._snapshot()
            dist = _dist_or(snap, lat, lon, default=best)
            if dist <= config.WAYPOINT_ARRIVAL_M:
                log.info("mission %s: arrived (%.1f m from target)", self._mission_id, dist)
                return
            if dist < best - 0.5:
                best = dist
                no_progress_since = time.monotonic()
            elif time.monotonic() - no_progress_since > config.MISSION_STEP_TIMEOUT_S / 3:
                raise _Abort(
                    f"waypoint {lat:.6f},{lon:.6f}: distance not decreasing "
                    f"({dist:.0f} m, best {best:.0f} m) -- vehicle not responding to "
                    f"position targets", Directive.RTL,
                )
            self._sleep(period)
        if self._stop.is_set():
            raise _Stopped()
        raise _Abort(
            f"did not reach {lat:.6f},{lon:.6f} within {config.MISSION_STEP_TIMEOUT_S:.0f}s "
            f"(closest {best:.0f} m)", Directive.RTL,
        )

    def _step_change_alt(self, alt_m: float) -> None:
        alt_m = min(alt_m, self._alt_ceiling)
        snap = self._snapshot()
        lat = snap.lat if snap.lat is not None else self._home_lat
        lon = snap.lon if snap.lon is not None else self._home_lon
        deadline = time.monotonic() + config.MISSION_STEP_TIMEOUT_S
        period = 1.0 / config.POSITION_TARGET_RATE_HZ
        while time.monotonic() < deadline and not self._stop.is_set():
            self._check_directive()
            _tx_position_target(self._link, lat, lon, alt_m)
            cur = self._snapshot().alt_m_relative
            if cur is not None and abs(cur - alt_m) <= config.ALT_TOLERANCE_M:
                log.info("mission %s: altitude confirmed %.1f m", self._mission_id, cur)
                return
            self._sleep(period)
        if self._stop.is_set():
            raise _Stopped()
        raise _Abort(
            f"altitude change to {alt_m:.0f} m not confirmed within "
            f"{config.MISSION_STEP_TIMEOUT_S:.0f}s", Directive.RTL,
        )

    def _step_loiter(self) -> None:
        """Hold the standoff point for loiter_seconds. Recompute the standoff as
        the wind changes; reposition (without ending the mission) if the drift
        exceeds LATERAL_OFFSET_M by more than a small margin -- safety.py checks
        this every tick too and can order it."""
        end = time.monotonic() + max(self._loiter_s, 0.0)
        period = 1.0 / config.POSITION_TARGET_RATE_HZ
        margin = max(3.0, config.LATERAL_OFFSET_M * 0.25)
        while time.monotonic() < end and not self._stop.is_set():
            self._check_directive()
            lat, lon = self._compute_standoff(quiet=True)
            snap = self._snapshot()
            if snap.lat is not None:
                err = _haversine_m(snap.lat, snap.lon, lat, lon)
                if err > config.LATERAL_OFFSET_M + margin:
                    self._emit("standoff_recomputed", {
                        "reason": "wind shift -- repositioning to the downwind standoff",
                        "error_m": round(err, 1), "new_point": [round(lat, 7), round(lon, 7)],
                        "basis": self._standoff[2] if self._standoff else None,
                    })
                    log.info("mission %s: repositioning to standoff (%.0f m off)",
                             self._mission_id, err)
            _tx_position_target(self._link, lat, lon,
                                min(self._on_station_alt, self._alt_ceiling))
            self._sleep(period)
        if self._stop.is_set():
            raise _Stopped()

    def _step_rtl_and_land(self) -> None:
        self._fly_home_and_land(mode="RTL")

    # -- abort handling -------------------------------------------------

    def _run_abort(self, ab: _Abort) -> None:
        with self._lock:
            self._abort_reason = ab.reason
        snap = self._snapshot()
        airborne = snap.armed is True and (snap.alt_m_relative or 0.0) > config.DISARM_MAX_ALT_M

        if ab.directive == Directive.NONE or not airborne:
            # Pre-arm / on-the-ground failure: nothing is flying. End the mission.
            log.error("mission %s: aborted on the ground -- %s", self._mission_id, ab.reason)
            self._set_phase(MissionPhase.ABORTED)
            return

        log.error(
            "mission %s: ABORT in the air (%s) -- controlled return, never a disarm. %s",
            self._mission_id, ab.directive.value, ab.reason,
        )
        try:
            if ab.directive == Directive.LAND_NOW:
                self._land_here()
            elif ab.directive == Directive.DIVERT:
                self._fly_to_divert()
            else:  # RTL (also the abort-command mapping)
                self._fly_home_and_land(mode="RTL")
        except _Stopped:
            log.info("mission %s: flight thread stopping (GSS shutdown) mid-return -- "
                     "the controlled return was already commanded; ArduPilot keeps "
                     "flying it, we just stop watching", self._mission_id)
        except _Abort as inner:
            # A directive escalated mid-return (e.g. RTL -> LAND_NOW). Follow it.
            log.error("mission %s: return escalated -> %s", self._mission_id, inner.directive.value)
            try:
                if inner.directive == Directive.LAND_NOW:
                    self._land_here()
                else:
                    self._fly_home_and_land(mode="RTL")
            except _Stopped:
                log.info("mission %s: flight thread stopping (GSS shutdown) mid-escalated-"
                         "return -- ArduPilot keeps flying it", self._mission_id)
            except Exception:  # noqa: BLE001
                log.exception("mission %s: escalated return also failed", self._mission_id)
        except Exception:  # noqa: BLE001
            log.exception("mission %s: controlled return failed -- vehicle's own "
                          "failsafes are what is left", self._mission_id)
        self._set_phase(MissionPhase.ABORTED)

    def _land_here(self) -> None:
        log.warning("mission %s: LAND where it is, now", self._mission_id)
        self._emit("landing_gps_only", {"kind": "land_now", "lat": self._snapshot().lat,
                                        "lon": self._snapshot().lon,
                                        "note": "GPS-only landing (ArUco precision landing is a later phase)"})
        for _ in range(config.MAVLINK_CMD_RETRIES):
            if _tx_set_mode(self._link, "LAND"):
                break
            self._sleep(0.5)
        self._await_disarm_on_ground()

    def _fly_to_divert(self) -> None:
        spot = self._divert_spot or {}
        slat, slon = _f(spot.get("spot_lat")), _f(spot.get("spot_lon"))
        if slat is None or slon is None:
            log.error("mission %s: DIVERT with no spot coordinates -- RTL instead",
                      self._mission_id)
            self._fly_home_and_land(mode="RTL")
            return
        log.warning("mission %s: DIVERT to '%s' (%.6f,%.6f)", self._mission_id,
                    spot.get("spot_name"), slat, slon)
        try:
            self._step_set_mode("GUIDED")
            self._step_goto(slat, slon, min(self._cruise_in, self._alt_ceiling))
        except (_Abort, _Stopped):
            raise
        except Exception:  # noqa: BLE001
            log.exception("mission %s: could not fly to the divert spot", self._mission_id)
        self._emit("landing_gps_only", {
            "kind": "divert", "spot": spot.get("spot_name"),
            "lat": slat, "lon": slon, "has_marker": spot.get("spot_has_marker"),
            "note": "GPS-only landing at the safe spot (ArUco precision landing is a later phase)",
        })
        for _ in range(config.MAVLINK_CMD_RETRIES):
            if _tx_set_mode(self._link, "LAND"):
                break
            self._sleep(0.5)
        self._await_disarm_on_ground()

    def _fly_home_and_land(self, *, mode: str) -> None:
        log.info("mission %s: %s to the dock", self._mission_id, mode)
        sent = False
        for _ in range(config.MAVLINK_CMD_RETRIES):
            if _tx_set_mode(self._link, mode):
                sent = True
                break
            self._sleep(0.5)
        if not sent:
            raise _Abort(f"could not command {mode}", Directive.RTL)
        # ArduPilot's RTL flies home and lands itself. Watch it down, and honour
        # an escalation to LAND_NOW.
        deadline = time.monotonic() + config.MISSION_STEP_TIMEOUT_S * 3
        while time.monotonic() < deadline and not self._stop.is_set():
            with self._lock:
                d = self._directive
            if d == Directive.LAND_NOW:
                raise _Abort(self._directive_reason, Directive.LAND_NOW)
            snap = self._snapshot()
            if snap.armed is False and (snap.alt_m_relative or 0.0) <= config.DISARM_MAX_ALT_M:
                self._emit("landing_gps_only", {
                    "kind": mode.lower(), "lat": snap.lat, "lon": snap.lon,
                    "note": "GPS-only landing via ArduPilot RTL (ArUco precision "
                            "landing is a later phase)",
                })
                log.info("mission %s: down and disarmed after %s", self._mission_id, mode)
                return
            self._sleep(1.0)
        if self._stop.is_set():
            raise _Stopped()
        log.warning("mission %s: %s did not complete within the watch window; "
                    "ArduPilot is still flying it", self._mission_id, mode)

    def _await_disarm_on_ground(self) -> None:
        deadline = time.monotonic() + config.MISSION_STEP_TIMEOUT_S
        while time.monotonic() < deadline and not self._stop.is_set():
            snap = self._snapshot()
            if snap.armed is False and (snap.alt_m_relative or 0.0) <= config.DISARM_MAX_ALT_M:
                log.info("mission %s: on the ground and disarmed", self._mission_id)
                return
            self._sleep(1.0)
        log.warning("mission %s: not confirmed disarmed on the ground within the "
                    "window -- NOT sending a disarm from the air", self._mission_id)

    # -- helpers ------------------------------------------------------

    def _compute_standoff(self, *, quiet: bool = False) -> tuple[float, float]:
        if self._person_lat is None or self._person_lon is None:
            return self._target_lat or self._home_lat, self._target_lon or self._home_lon
        if not self._is_summon:
            # goto/other: fly to the target itself, no standoff.
            return self._target_lat, self._target_lon
        snap = self._snapshot()
        wind_from = (
            snap.wind_direction_deg
            if (snap.wind_direction_deg is not None
                and snap.wind_speed_ms is not None and snap.wind_speed_ms >= 0.5
                and snap.wind_age_s is not None and snap.wind_age_s <= config.GPS_MAX_AGE_S)
            else None
        )
        # LATERAL_OFFSET_M plus STANDOFF_MARGIN_M, not the bare legal minimum
        # -- targeting exactly the R2 floor left zero room for ordinary GPS /
        # arrival noise, which is exactly how the real-SITL pass landed 11 m
        # from the human (inside the 12 m boundary) and got stuck in a HOLD
        # that could never resolve. See config.STANDOFF_MARGIN_M.
        lat, lon, basis = standoff_point(
            self._person_lat, self._person_lon,
            dock_lat=self._home_lat, dock_lon=self._home_lon,
            offset_m=config.LATERAL_OFFSET_M + config.STANDOFF_MARGIN_M,
            wind_from_deg=wind_from,
        )
        prev = self._standoff
        self._standoff = (lat, lon, basis)
        if not quiet and (prev is None or prev[2] != basis
                          or _haversine_m(prev[0], prev[1], lat, lon) > 1.0):
            self._emit("standoff_recomputed", {
                "basis": basis, "wind_from_deg": wind_from,
                "offset_m": config.LATERAL_OFFSET_M,
                "point": [round(lat, 7), round(lon, 7)],
                "person": [self._person_lat, self._person_lon],
            })
            log.info("mission %s: standoff point %.6f,%.6f (%s, wind_from=%s)",
                     self._mission_id, lat, lon, basis, wind_from)
        return lat, lon

    def _set_phase(self, phase: MissionPhase) -> None:
        with self._lock:
            self._phase = phase

    def _emit(self, event: str, detail: dict) -> None:
        with self._lock:
            self._events.append((event, detail))

    def _sleep(self, seconds: float) -> None:
        self._stop.wait(seconds)

    @property
    def standoff(self) -> tuple[float, float, str] | None:
        """The last computed standoff point (lat, lon, basis). For tests."""
        with self._lock:
            return self._standoff


# ===========================================================================
# small helpers
# ===========================================================================


def _f(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _rank(d: Directive) -> int:
    return {
        Directive.NONE: 0, Directive.HOLD: 1, Directive.DESCEND: 2,
        Directive.RTL: 3, Directive.DIVERT: 4, Directive.LAND_NOW: 5,
    }[d]


def _dist_or(snap: TelemetrySnapshot, lat: float, lon: float, *, default: float) -> float:
    if snap.lat is None or snap.lon is None:
        return default
    return _haversine_m(snap.lat, snap.lon, lat, lon)


def _result_name(result: int) -> str:
    if mavutil is None:
        return str(result)
    return mavutil.mavlink.enums["MAV_RESULT"].get(result, type("x", (), {"name": str(result)})).name
