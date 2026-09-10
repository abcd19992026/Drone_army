"""safety.py -- the veto authority (v0.4).

This module answers two questions, continuously and locally:

  1. Should this command be allowed to start a flight?
  2. Given the drone's state right now, should it keep doing what it is doing,
     or must it do something else immediately?

It is the final authority on both. No other module may proceed against its
verdict. There is no override flag, no admin mode, and no command type that
bypasses it.

FAIL SAFE, NEVER FAIL OPEN (rule R12)
------------------------------------
If a check cannot be evaluated -- missing data, an unexpected value, or an
exception inside the check itself -- the verdict is DENY, not ALLOW. An unknown
or stale battery level is treated as empty. An unknown or stale position is
treated as lost. Pre-flight, DENY means REJECT; in flight, DENY means "get the
aircraft to safety" (RTL_NOW, latched).

CRITICAL VERDICTS LATCH (rule R13)
---------------------------------
Once safety.py has ordered a return or a landing, a momentarily better reading
does not cancel it. The latch clears only when the drone is on the ground and
disarmed.

LOCAL ONLY (rule R1)
-------------------
This module imports nothing from ``gss.store``, ``gss.commands``,
``gss.executor``, and no networking library (``urllib``, ``websockets``,
``requests``, an HTTP client, or even ``socket``) -- directly or transitively.
It reads the drone's state from a :class:`~gss.snapshot.TelemetrySnapshot` and
its thresholds from :mod:`gss.config`. Broadband going down while the drone is
airborne is exactly when this module matters most. ``tests/test_safety.py``
asserts this boundary so a future session cannot quietly break it.

Structure
---------
The core is pure functions: state in, verdict out. No I/O, no threads, no clock
reads inside the decision functions -- the current time is passed in. Only a
pure core can be tested exhaustively, and this module is tested harder than any
other. :class:`SafetyMonitor` is the thin shell around that core: it owns the
loop thread, the rolling windows, the latch, and the (best-effort, local-first)
logging.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum

from gss import config, weather
from gss.snapshot import SafeSpot, TelemetrySnapshot

log = logging.getLogger(__name__)


def _observed_wind(
    snapshot: TelemetrySnapshot,
) -> tuple[float | None, float | None]:
    """The aircraft's OWN measured wind as ``(speed_ms, blows_from_deg)``, or
    ``(None, None)`` when it is missing or stale. Never a forecast (R1)."""
    if (
        snapshot.wind_speed_ms is None
        or snapshot.wind_direction_deg is None
        or snapshot.wind_age_s is None
        or snapshot.wind_age_s > config.GPS_MAX_AGE_S
    ):
        return None, None
    return snapshot.wind_speed_ms, snapshot.wind_direction_deg


def _observed_headwind_home(
    snapshot: TelemetrySnapshot, context: SafetyContext
) -> float:
    """Headwind component (m/s) the drone would fly into on the way home, from
    the aircraft's OWN measured wind (never a forecast -- R1). 0.0 when wind
    data is missing, stale, or home/position is unknown. Fed into the
    point-of-no-return estimate: a 10 m/s headwind against a 10 m/s cruise
    means the drone does not get home at all.
    """
    speed, from_deg = _observed_wind(snapshot)
    if (
        speed is None
        or from_deg is None
        or snapshot.lat is None
        or snapshot.lon is None
        or context.home_lat is None
        or context.home_lon is None
    ):
        return 0.0
    course = weather.bearing_deg(
        snapshot.lat, snapshot.lon, context.home_lat, context.home_lon
    )
    return weather.headwind_component_ms(from_deg, speed, course)


# ===========================================================================
# Types
# ===========================================================================


class SafetyAction(str, Enum):
    """What safety.py orders. Ordered least to most drastic (see ``_ACTION_RANK``)."""

    ALLOW = "ALLOW"
    WARN = "WARN"
    HOLD = "HOLD"
    # Stop horizontal progress and reduce altitude to a safe value, then keep
    # evaluating. Between HOLD and RTL_NOW in severity: an altitude breach is
    # worse than needing to sit still (HOLD preserves the violation) but does
    # not by itself mean abandon the mission.
    DESCEND = "DESCEND"
    RTL_NOW = "RTL_NOW"
    # Home is NOT reachable with the battery available -- fly to the nearest
    # reachable known safe spot and land there. Between RTL_NOW and LAND_NOW:
    # starting a return the aircraft cannot finish is worse than landing
    # somewhere deliberate; landing on whatever is directly beneath it right
    # now (LAND_NOW) is the last resort when nothing else is reachable.
    DIVERT = "DIVERT"
    LAND_NOW = "LAND_NOW"
    REJECT = "REJECT"


class SafetySeverity(str, Enum):
    """How bad it is."""

    NOMINAL = "NOMINAL"
    CAUTION = "CAUTION"
    CRITICAL = "CRITICAL"
    EMERGENCY = "EMERGENCY"


_ACTION_RANK: dict[SafetyAction, int] = {
    SafetyAction.ALLOW: 0,
    SafetyAction.WARN: 1,
    SafetyAction.HOLD: 2,
    SafetyAction.DESCEND: 3,
    SafetyAction.RTL_NOW: 4,
    SafetyAction.DIVERT: 5,
    SafetyAction.LAND_NOW: 6,
    SafetyAction.REJECT: 7,
}
_SEVERITY_RANK: dict[SafetySeverity, int] = {
    SafetySeverity.NOMINAL: 0,
    SafetySeverity.CAUTION: 1,
    SafetySeverity.CRITICAL: 2,
    SafetySeverity.EMERGENCY: 3,
}

# Verdicts that latch until the drone is on the ground and disarmed (R13).
# DIVERT latches too: a momentarily better battery reading does not un-decide
# "home is not reachable".
_LATCHABLE: tuple[SafetyAction, ...] = (
    SafetyAction.RTL_NOW,
    SafetyAction.DIVERT,
    SafetyAction.LAND_NOW,
)


@dataclass(frozen=True)
class SafetyVerdict:
    """The output of an evaluation.

    ``reasons`` lists EVERY failing check, not just the first: at 2am
    "battery 19%, position stale 6s, link down 40s" tells the story and
    "battery low" does not.
    """

    action: SafetyAction
    severity: SafetySeverity
    reasons: tuple[str, ...]
    checked_at: datetime
    latched: bool = False
    # The action currently latched (RTL_NOW / DIVERT / LAND_NOW), or None. The
    # shell feeds this back into the next evaluation as ``prior_latch``. It is a
    # companion to ``latched`` above, not a separate concept.
    latch_action: SafetyAction | None = None
    # For DIVERT (and a LAND_NOW where nothing was reachable): the structured
    # facts the alert / mission_events row needs -- the chosen spot, its
    # coordinates, and why home was out of reach, with the numbers. ``None``
    # for every other verdict.
    divert_target: dict | None = None

    def is_veto(self) -> bool:
        """True when this verdict forbids or interrupts flight."""
        return self.action not in (SafetyAction.ALLOW, SafetyAction.WARN)


@dataclass(frozen=True)
class SafetyContext:
    """What safety.py needs to know about the flight it is judging."""

    preflight: bool = False
    phase: str | None = None
    mission_id: str | None = None
    is_summon: bool = False
    target_lat: float | None = None
    target_lon: float | None = None
    target_alt_m: float | None = None
    home_lat: float | None = None
    home_lon: float | None = None
    # The human's reported position, for a summon. R2: the on-station position
    # must be at least LATERAL_OFFSET_M from this.
    human_lat: float | None = None
    human_lon: float | None = None
    # Pre-flight only: is a mission already running for this drone?
    mission_active: bool = False
    # The known safe spots to divert to when home is not reachable. Plain data,
    # passed in by the shell (loaded from Supabase + a disk cache + a hardcoded
    # config fallback by gss.safe_spots) -- safety.py never reads the database
    # (R1). Empty is a valid state; the verdict is then LAND_NOW, not DIVERT.
    safe_spots: tuple[SafeSpot, ...] = ()


@dataclass(frozen=True)
class _Finding:
    """One check's contribution to a verdict."""

    action: SafetyAction
    severity: SafetySeverity
    reason: str
    # Structured facts for the verdict, when the string reason is not enough
    # (currently only the point-of-no-return DIVERT / LAND_NOW findings).
    data: dict | None = None


# ===========================================================================
# Tunable internals (starting points -- tune against real flight logs, not
# guesses; see the config.py safety block)
# ===========================================================================

_EARTH_RADIUS_M = 6_371_000.0
_GROUND_ALT_M = 1.0            # at/below this relative altitude = "on the ground"
_MIN_RETURN_SPEED_MS = 2.0     # floor on the assumed return ground speed
_CLIMB_DESCEND_LAND_S = 20.0   # fixed slice of an RTL: climb + descent + touchdown
_VERT_SPEED_MS = 2.0           # assumed climb/descent rate for the altitude slice
# Fallback pack discharge when the observed rate is not yet known. 0.08 %/s is
# ~4.8 %/min -- a full pack in ~21 min, deliberately faster (more pessimistic)
# than the ~40 min hover endurance so an unknown rate errs toward coming home.
_FALLBACK_DISCHARGE_PCT_PER_S = 0.08
# A pack cannot physically sustain much more than this. The OBSERVED rate can
# briefly read far higher just after a coarse (1 %) telemetry step or a current
# spike; clamp it so one noisy sample cannot turn a routine return into "land
# in a field". ~0.15 %/s is ~11 min from full to empty -- already aggressive.
_MAX_DISCHARGE_PCT_PER_S = 0.15
# At or below this, a spot's height above the dock is "ground level". Above it,
# a GPS-only spot is refused -- descending onto a raised surface the aircraft
# cannot see, on GPS alone, means descending into a wall.
_RAISED_SURFACE_EPS_M = 0.5
_ENROUTE_PHASES = frozenset({"enroute", "returning"})


# ===========================================================================
# Geometry (local copy -- safety.py imports no other gss module that has it)
# ===========================================================================


def great_circle_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two WGS-84 points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _home(context: SafetyContext) -> tuple[float, float] | None:
    if context.home_lat is None or context.home_lon is None:
        return None
    return (context.home_lat, context.home_lon)


def _airborne(snapshot: TelemetrySnapshot) -> bool:
    if snapshot.armed:
        return True
    alt = snapshot.alt_m_relative
    return alt is not None and alt > _GROUND_ALT_M


def _on_ground_and_disarmed(snapshot: TelemetrySnapshot) -> bool:
    """R13 latch-clear condition: BOTH on the ground AND disarmed.

    An unknown armed state or unknown altitude counts as "not safely on the
    ground" -- the latch stays (fail safe).
    """
    if snapshot.armed is None or snapshot.armed:
        return False
    alt = snapshot.alt_m_relative
    if alt is None:
        return False
    return alt <= _GROUND_ALT_M


# ===========================================================================
# The checks -- each one small, pure, and testable alone
# ===========================================================================


def check_battery(snapshot: TelemetrySnapshot) -> list[_Finding]:
    """Three independent floors: percentage warn, percentage floor, per-cell
    voltage. Whichever trips first wins. Unknown or stale => empty (R12)."""
    pct = snapshot.battery_pct
    age = snapshot.battery_age_s
    stale = age is not None and age > config.BATTERY_MAX_AGE_S
    if pct is None or stale:
        why = (
            "no battery reading received"
            if pct is None
            else f"battery reading {age:.1f}s stale (max {config.BATTERY_MAX_AGE_S:.0f}s)"
        )
        return [
            _Finding(
                SafetyAction.LAND_NOW,
                SafetySeverity.EMERGENCY,
                f"{why} -- battery treated as empty (R12)",
            )
        ]

    out: list[_Finding] = []

    voltage = snapshot.battery_voltage_v
    if voltage is None:
        out.append(
            _Finding(
                SafetyAction.WARN,
                SafetySeverity.CAUTION,
                "pack voltage unavailable -- the per-cell floor cannot be checked",
            )
        )
    else:
        per_cell = voltage / config.BATTERY_CELLS
        if per_cell <= config.BATTERY_CELL_FLOOR_V:
            out.append(
                _Finding(
                    SafetyAction.RTL_NOW,
                    SafetySeverity.CRITICAL,
                    f"per-cell voltage {per_cell:.2f}V at/below floor "
                    f"{config.BATTERY_CELL_FLOOR_V:.2f}V "
                    f"(pack {voltage:.1f}V / {config.BATTERY_CELLS} cells) -- "
                    f"overrides the {pct:.0f}% reading",
                )
            )

    if pct <= config.BATTERY_FLOOR_PCT:
        out.append(
            _Finding(
                SafetyAction.RTL_NOW,
                SafetySeverity.CRITICAL,
                f"battery {pct:.0f}% at/below hard floor {config.BATTERY_FLOOR_PCT:.0f}%",
            )
        )
    elif pct <= config.BATTERY_WARN_PCT:
        out.append(
            _Finding(
                SafetyAction.WARN,
                SafetySeverity.CAUTION,
                f"battery {pct:.0f}% at/below warn level {config.BATTERY_WARN_PCT:.0f}%",
            )
        )
    return out


def check_link(snapshot: TelemetrySnapshot, link_down_s: float) -> list[_Finding]:
    """Link-loss ladder: past the grace period -> RTL_NOW (latched); below it,
    WARN. Brief dropouts are normal; turning around for each one is a hazard."""
    if link_down_s > config.LINK_LOSS_GRACE_S:
        return [
            _Finding(
                SafetyAction.RTL_NOW,
                SafetySeverity.CRITICAL,
                f"link down {link_down_s:.0f}s -- past the "
                f"{config.LINK_LOSS_GRACE_S:.0f}s grace, returning home",
            )
        ]
    if not snapshot.connected or link_down_s > 0:
        return [
            _Finding(
                SafetyAction.WARN,
                SafetySeverity.CAUTION,
                f"link down {link_down_s:.0f}s -- within the "
                f"{config.LINK_LOSS_GRACE_S:.0f}s grace, holding course",
            )
        ]
    age = snapshot.link_age_s
    if age is not None and age > config.HEARTBEAT_TIMEOUT_S * 0.6:
        return [
            _Finding(
                SafetyAction.WARN,
                SafetySeverity.CAUTION,
                f"link degraded -- last heartbeat {age:.1f}s ago",
            )
        ]
    return []


def gps_trustworthy(snapshot: TelemetrySnapshot) -> tuple[bool, str | None]:
    """Can the drone trust where it thinks it is? Returns (ok, why-not)."""
    fix = snapshot.gps_fix_type
    if fix is None or fix < 3:
        return False, f"GPS fix type {fix} (need 3D)"
    sats = snapshot.gps_satellites
    if sats is None or sats < config.GPS_MIN_SATELLITES:
        return False, f"{sats} GPS satellites (need {config.GPS_MIN_SATELLITES})"
    hdop = snapshot.gps_hdop
    if hdop is None or hdop > config.GPS_MAX_HDOP:
        return False, f"GPS HDOP {hdop} (max {config.GPS_MAX_HDOP})"
    gps_age = snapshot.gps_age_s
    if gps_age is None or gps_age > config.GPS_MAX_AGE_S:
        return False, f"GPS report {_fmt_age(gps_age)} stale (max {config.GPS_MAX_AGE_S:.0f}s)"
    pos_age = snapshot.position_age_s
    if pos_age is None or pos_age > config.POSITION_MAX_AGE_S:
        return (
            False,
            f"position report {_fmt_age(pos_age)} stale (max {config.POSITION_MAX_AGE_S:.0f}s)",
        )
    return True, None


def check_gps(
    snapshot: TelemetrySnapshot, gps_bad_s: float, *, preflight: bool
) -> list[_Finding]:
    """Untrustworthy position: pre-flight REJECT; in flight HOLD (a drone that
    cannot trust its position must not navigate), escalating to LAND_NOW past
    the grace period."""
    ok, why = gps_trustworthy(snapshot)
    if ok:
        return []
    if preflight:
        return [
            _Finding(
                SafetyAction.REJECT,
                SafetySeverity.CRITICAL,
                f"position not trustworthy for launch: {why}",
            )
        ]
    if gps_bad_s > config.GPS_LOSS_GRACE_S:
        return [
            _Finding(
                SafetyAction.LAND_NOW,
                SafetySeverity.EMERGENCY,
                f"position untrustworthy for {gps_bad_s:.0f}s ({why}) -- past the "
                f"{config.GPS_LOSS_GRACE_S:.0f}s grace, landing",
            )
        ]
    return [
        _Finding(
            SafetyAction.HOLD,
            SafetySeverity.CRITICAL,
            f"position not trustworthy ({why}) -- holding; will land if it persists",
        )
    ]


def check_geofence(
    snapshot: TelemetrySnapshot, context: SafetyContext, *, preflight: bool
) -> list[_Finding]:
    """Radius from home, altitude ceiling, minimum enroute altitude. Great-circle."""
    out: list[_Finding] = []
    home = _home(context)

    if preflight:
        if (
            context.target_lat is not None
            and context.target_lon is not None
            and home is not None
        ):
            dist = great_circle_m(
                home[0], home[1], context.target_lat, context.target_lon
            )
            if dist > config.MAX_RADIUS_M:
                out.append(
                    _Finding(
                        SafetyAction.REJECT,
                        SafetySeverity.CRITICAL,
                        f"target {dist / 1000:.1f} km from home exceeds the "
                        f"{config.MAX_RADIUS_M / 1000:.1f} km geofence",
                    )
                )
        if context.target_alt_m is not None and context.target_alt_m > config.MAX_ALT_M:
            out.append(
                _Finding(
                    SafetyAction.REJECT,
                    SafetySeverity.CRITICAL,
                    f"requested altitude {context.target_alt_m:.0f} m exceeds the "
                    f"{config.MAX_ALT_M:.0f} m ceiling",
                )
            )
        return out

    if snapshot.lat is not None and snapshot.lon is not None and home is not None:
        dist = great_circle_m(home[0], home[1], snapshot.lat, snapshot.lon)
        if dist > config.MAX_RADIUS_M:
            out.append(
                _Finding(
                    SafetyAction.RTL_NOW,
                    SafetySeverity.CRITICAL,
                    f"{dist / 1000:.1f} km from home -- outside the "
                    f"{config.MAX_RADIUS_M / 1000:.1f} km geofence, returning",
                )
            )

    alt = snapshot.alt_m_relative
    if alt is not None:
        if alt > config.MAX_ALT_M:
            out.append(
                _Finding(
                    SafetyAction.DESCEND,
                    SafetySeverity.CRITICAL,
                    f"altitude {alt:.0f} m above the {config.MAX_ALT_M:.0f} m "
                    f"ceiling -- stop and descend to a safe altitude",
                )
            )
        elif alt < config.MIN_ALT_M and (context.phase or "") in _ENROUTE_PHASES:
            out.append(
                _Finding(
                    SafetyAction.WARN,
                    SafetySeverity.CAUTION,
                    f"altitude {alt:.0f} m below the {config.MIN_ALT_M:.0f} m "
                    f"minimum enroute altitude",
                )
            )
    return out


def check_lateral_offset(
    snapshot: TelemetrySnapshot, context: SafetyContext, *, preflight: bool
) -> list[_Finding]:
    """R2: for a summon, the on-station position must be at least
    LATERAL_OFFSET_M horizontally from the reported human position. A 3 kg
    aircraft directly above a person is ~900 J waiting for one motor to fail.

    Only enforced when the command carries an explicit human position;
    mission planning (v0.6) computes the standoff target, and in flight the
    check below runs continuously against the drone's real position.
    """
    if not context.is_summon or context.human_lat is None or context.human_lon is None:
        return []

    if preflight:
        if context.target_lat is None or context.target_lon is None:
            return []
        dist = great_circle_m(
            context.target_lat, context.target_lon, context.human_lat, context.human_lon
        )
        if dist < config.LATERAL_OFFSET_M:
            return [
                _Finding(
                    SafetyAction.REJECT,
                    SafetySeverity.CRITICAL,
                    f"on-station target {dist:.0f} m from the reported human "
                    f"position; minimum lateral offset is "
                    f"{config.LATERAL_OFFSET_M:.0f} m (R2)",
                )
            ]
        return []

    if snapshot.lat is None or snapshot.lon is None:
        return []
    dist = great_circle_m(
        snapshot.lat, snapshot.lon, context.human_lat, context.human_lon
    )
    if dist < config.LATERAL_OFFSET_M:
        return [
            _Finding(
                SafetyAction.HOLD,
                SafetySeverity.CRITICAL,
                f"{dist:.0f} m from the reported human position -- inside the "
                f"{config.LATERAL_OFFSET_M:.0f} m lateral offset (R2); "
                f"hold and open the distance",
            )
        ]
    return []


def _return_budget_pct(
    dist_m: float,
    alt_m: float,
    *,
    discharge_rate_pct_per_s: float | None,
    wind_ms: float,
) -> float:
    """Battery percent to fly ``dist_m`` at altitude ``alt_m`` and land, with
    RTL_RESERVE_PCT to spare, into a ``wind_ms`` headwind (negative = tailwind,
    not credited)."""
    ground_speed = max(_MIN_RETURN_SPEED_MS, config.CRUISE_SPEED_MS - max(0.0, wind_ms))
    cruise_s = dist_m / ground_speed
    maneuver_s = _CLIMB_DESCEND_LAND_S + max(0.0, alt_m) / _VERT_SPEED_MS
    rate = (
        discharge_rate_pct_per_s
        if discharge_rate_pct_per_s and discharge_rate_pct_per_s > 0
        else _FALLBACK_DISCHARGE_PCT_PER_S
    )
    rate = min(rate, _MAX_DISCHARGE_PCT_PER_S)
    return (cruise_s + maneuver_s) * rate + config.RTL_RESERVE_PCT


def estimate_return_budget_pct(
    snapshot: TelemetrySnapshot,
    context: SafetyContext,
    *,
    discharge_rate_pct_per_s: float | None,
    wind_ms: float = 0.0,
) -> float | None:
    """Battery percentage needed, from the current state, to get home and land
    with RTL_RESERVE_PCT to spare. ``None`` if it cannot be computed.

    ``wind_ms`` is a headwind component in m/s that slows the return ground
    speed -- the shell feeds in a real measured-wind estimate (a 10 m/s
    headwind against a 10 m/s cruise means the drone does not get home at all).
    """
    home = _home(context)
    if home is None or not snapshot.position_valid or snapshot.lat is None or snapshot.lon is None:
        return None
    dist = great_circle_m(home[0], home[1], snapshot.lat, snapshot.lon)
    alt = snapshot.alt_m_relative if snapshot.alt_m_relative is not None else config.ON_STATION_ALT
    return _return_budget_pct(
        dist, alt, discharge_rate_pct_per_s=discharge_rate_pct_per_s, wind_ms=wind_ms
    )


def select_divert_spot(
    snapshot: TelemetrySnapshot,
    context: SafetyContext,
    *,
    discharge_rate_pct_per_s: float | None,
    wind_speed_ms: float | None = None,
    wind_from_deg: float | None = None,
) -> tuple[SafeSpot, float, float] | None:
    """The safe spot in ``context.safe_spots`` to divert to, as ``(spot,
    distance_m, needed_pct)``. ``None`` when nothing acceptable is reachable.

    Reachability uses the SAME headwind-aware budget as the point of no return
    -- the measured wind projected onto the course to each candidate -- not a
    straight-line guess.

    Two tiers, respected here and not merely stored on the row:

      * a GPS-only spot (``has_marker`` False) whose ``radius_m`` is below
        ``config.SAFE_SPOT_MIN_GPS_RADIUS_M`` is REJECTED -- the row's number is
        not trusted against 3-10 m of GPS error;
      * a GPS-only spot with a non-zero ``height_above_dock_m`` is REJECTED --
        GPS cannot put an aircraft onto a raised surface it cannot see;
      * a marker pad is preferred over a GPS-only spot even when it is further,
        by up to ``config.SAFE_SPOT_MARKER_PREFERENCE_M`` -- an accurate landing
        on a known pad is worth a kilometre of battery.
    """
    if (
        not context.safe_spots
        or snapshot.lat is None
        or snapshot.lon is None
        or snapshot.battery_pct is None
    ):
        return None
    pct = float(snapshot.battery_pct)
    alt = snapshot.alt_m_relative if snapshot.alt_m_relative is not None else config.ON_STATION_ALT
    reachable_marker: list[tuple[float, SafeSpot, float]] = []
    reachable_gps: list[tuple[float, SafeSpot, float]] = []
    for spot in context.safe_spots:
        if not spot.has_marker:
            if spot.radius_m < config.SAFE_SPOT_MIN_GPS_RADIUS_M:
                continue
            if abs(spot.height_above_dock_m) > _RAISED_SURFACE_EPS_M:
                continue
        dist = great_circle_m(snapshot.lat, snapshot.lon, spot.lat, spot.lon)
        if dist > config.SAFE_SPOT_MAX_DISTANCE_M:
            continue
        course = weather.bearing_deg(snapshot.lat, snapshot.lon, spot.lat, spot.lon)
        head = weather.headwind_component_ms(wind_from_deg, wind_speed_ms, course)
        needed = _return_budget_pct(
            dist, alt, discharge_rate_pct_per_s=discharge_rate_pct_per_s, wind_ms=head
        )
        if needed > pct:
            continue
        (reachable_marker if spot.has_marker else reachable_gps).append((dist, spot, needed))

    if not reachable_marker and not reachable_gps:
        return None
    reachable_marker.sort(key=lambda r: r[0])
    reachable_gps.sort(key=lambda r: r[0])

    if reachable_marker:
        m_dist, m_spot, m_needed = reachable_marker[0]
        if (
            not reachable_gps
            or m_dist <= reachable_gps[0][0] + config.SAFE_SPOT_MARKER_PREFERENCE_M
        ):
            return m_spot, m_dist, m_needed
    # no acceptable marker pad within the preference distance -> the nearest
    # reachable GPS-only spot.
    dist, spot, needed = reachable_gps[0]
    return spot, dist, needed


def check_point_of_no_return(
    snapshot: TelemetrySnapshot,
    context: SafetyContext,
    *,
    discharge_rate_pct_per_s: float | None,
    wind_ms: float = 0.0,
    wind_speed_ms: float | None = None,
    wind_from_deg: float | None = None,
) -> list[_Finding]:
    """Every tick: is there still enough battery to get home?

      * comfortably yes -> nothing;
      * no, but home is still reachable (eating into the reserve) -> RTL_NOW;
      * home is NOT reachable (budget over RETURN_BUDGET_IMPOSSIBLE_PCT, or over
        the battery left plus the reserve) -> DIVERT to the nearest reachable
        safe spot, or LAND_NOW if nothing is reachable.

    All of these latch and outrank any command, a human saying "stay" included.
    """
    if not _airborne(snapshot):
        return []
    needed = estimate_return_budget_pct(
        snapshot, context, discharge_rate_pct_per_s=discharge_rate_pct_per_s, wind_ms=wind_ms
    )
    if needed is None:
        # Cannot compute -- position is not valid. The GPS check owns the
        # response (HOLD/LAND); flag it here so the reason list is complete,
        # but do not fail open.
        return [
            _Finding(
                SafetyAction.WARN,
                SafetySeverity.CAUTION,
                "cannot compute point of no return: position not valid",
            )
        ]
    pct = snapshot.battery_pct
    if pct is None:
        return []  # check_battery already ordered LAND_NOW
    if pct > needed:
        return []

    home = _home(context)
    dist_home = (
        great_circle_m(home[0], home[1], snapshot.lat, snapshot.lon)
        if home and snapshot.lat is not None and snapshot.lon is not None
        else float("nan")
    )
    headwind = max(0.0, wind_ms)
    home_unreachable = (
        needed > config.RETURN_BUDGET_IMPOSSIBLE_PCT
        or needed > pct + config.RTL_RESERVE_PCT
    )

    if not home_unreachable:
        return [
            _Finding(
                SafetyAction.RTL_NOW,
                SafetySeverity.CRITICAL,
                f"point of no return: {pct:.0f}% left, need ~{needed:.0f}% to reach "
                f"home ({dist_home:.0f} m"
                + (f" into a {headwind:.0f} m/s headwind" if headwind > 0.5 else "")
                + f", +{config.RTL_RESERVE_PCT:.0f}% reserve)",
            )
        ]

    pick = select_divert_spot(
        snapshot,
        context,
        discharge_rate_pct_per_s=discharge_rate_pct_per_s,
        wind_speed_ms=wind_speed_ms,
        wind_from_deg=wind_from_deg,
    )
    _dist_home = None if dist_home != dist_home else round(dist_home, 1)  # NaN check
    if pick is not None:
        spot, spot_dist, spot_needed = pick
        landing = (
            f"ArUco pad{f' {spot.marker_id}' if spot.marker_id else ''}"
            if spot.has_marker
            else f"GPS-only, {spot.radius_m:.0f} m clear ground"
        )
        data = {
            "spot_name": spot.name,
            "spot_lat": spot.lat,
            "spot_lon": spot.lon,
            "spot_surface": spot.surface,
            "spot_has_marker": spot.has_marker,
            "spot_marker_id": spot.marker_id,
            "spot_height_above_dock_m": spot.height_above_dock_m,
            "spot_radius_m": spot.radius_m,
            "spot_distance_m": round(spot_dist, 1),
            "spot_needed_pct": round(spot_needed, 1),
            "battery_pct": round(float(pct), 1),
            "home_distance_m": _dist_home,
            "home_needed_pct": round(needed, 1),
            "headwind_ms": round(headwind, 1),
        }
        return [
            _Finding(
                SafetyAction.DIVERT,
                SafetySeverity.CRITICAL,
                f"home not reachable: {pct:.0f}% left, need ~{needed:.0f}% to fly the "
                f"{dist_home:.0f} m home"
                + (f" into a {headwind:.0f} m/s headwind" if headwind > 0.5 else "")
                + f". Diverting to safe spot '{spot.name}' [{landing}] "
                f"({spot.lat:.5f}, {spot.lon:.5f}), {spot_dist:.0f} m, "
                f"need ~{spot_needed:.0f}%",
                data,
            )
        ]

    data = {
        "nowhere_reachable": True,
        "last_lat": snapshot.lat,
        "last_lon": snapshot.lon,
        "battery_pct": round(float(pct), 1),
        "home_distance_m": _dist_home,
        "home_needed_pct": round(needed, 1),
        "headwind_ms": round(headwind, 1),
        # BEACON HOOK (Phase 14 payload plan): the aircraft is putting itself
        # down away from the dock with nothing reachable -- the spotlight +
        # siren are how a person finds it. That payload does not exist yet;
        # this is where its activation attaches.
        "beacon": "attach spotlight+siren activation here (Phase 14)",
    }
    return [
        _Finding(
            SafetyAction.LAND_NOW,
            SafetySeverity.EMERGENCY,
            f"home not reachable ({pct:.0f}% left, need ~{needed:.0f}% for the "
            f"{dist_home:.0f} m) and no safe spot in range is reachable either -- "
            f"landing here at {snapshot.lat:.5f}, {snapshot.lon:.5f}",
            data,
        )
    ]


def check_preflight_readiness(
    snapshot: TelemetrySnapshot, context: SafetyContext
) -> list[_Finding]:
    """Everything that must be true before ANY flight is allowed."""
    out: list[_Finding] = []

    def reject(reason: str) -> None:
        out.append(_Finding(SafetyAction.REJECT, SafetySeverity.CRITICAL, reason))

    if not snapshot.connected:
        reject("link to the vehicle is down")
    age = snapshot.telemetry_age_s
    if age is None or age > config.TELEMETRY_MAX_AGE_S:
        reject(
            f"telemetry stale ({'never received' if age is None else f'{age:.0f}s old'}, "
            f"max {config.TELEMETRY_MAX_AGE_S:.0f}s)"
        )
    if not snapshot.position_valid:
        reject("position not valid (need real coords, a 3D fix, satellites, and a fresh report)")

    pct = snapshot.battery_pct
    batt_age = snapshot.battery_age_s
    if pct is None or (batt_age is not None and batt_age > config.BATTERY_MAX_AGE_S):
        reject("battery reading unavailable or stale")
    elif pct < config.BATTERY_LAUNCH_MIN_PCT:
        reject(
            f"battery {pct:.0f}% is below the launch minimum "
            f"{config.BATTERY_LAUNCH_MIN_PCT:.0f}% (higher than the in-flight "
            f"floor of {config.BATTERY_FLOOR_PCT:.0f}% on purpose -- taking off at "
            f"a level is not the same as passing through it)"
        )

    if context.mission_active:
        reject("a mission is already active for this drone")

    return out


# ===========================================================================
# Aggregation + the two entry points
# ===========================================================================

# The in-flight check set, as (name, callable-taking-kwargs). Kept as data so a
# test can prove that a check raising an exception yields DENY, not ALLOW (R12).
def _inflight_findings(
    snapshot: TelemetrySnapshot,
    context: SafetyContext,
    *,
    link_down_s: float,
    gps_bad_s: float,
    discharge_rate_pct_per_s: float | None,
    wind_ms: float,
    wind_speed_ms: float | None = None,
    wind_from_deg: float | None = None,
) -> tuple[list[_Finding], list[str]]:
    """Run every in-flight check. A check that raises does NOT abort the
    evaluation and does NOT count as passing: it contributes a DENY finding
    (RTL_NOW / CRITICAL) and is named in ``errors`` (rule R8: never silent)."""
    checks: list[tuple[str, Callable[[], list[_Finding]]]] = [
        ("battery", lambda: check_battery(snapshot)),
        ("link", lambda: check_link(snapshot, link_down_s)),
        ("gps", lambda: check_gps(snapshot, gps_bad_s, preflight=False)),
        ("geofence", lambda: check_geofence(snapshot, context, preflight=False)),
        ("lateral_offset", lambda: check_lateral_offset(snapshot, context, preflight=False)),
        (
            "point_of_no_return",
            lambda: check_point_of_no_return(
                snapshot,
                context,
                discharge_rate_pct_per_s=discharge_rate_pct_per_s,
                wind_ms=wind_ms,
                wind_speed_ms=wind_speed_ms,
                wind_from_deg=wind_from_deg,
            ),
        ),
    ]
    findings: list[_Finding] = []
    errors: list[str] = []
    for name, fn in checks:
        try:
            findings.extend(fn())
        except Exception as exc:  # noqa: BLE001 -- R12: a broken check must DENY
            log.exception("safety: check %r raised; failing safe to RTL_NOW", name)
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            findings.append(
                _Finding(
                    SafetyAction.RTL_NOW,
                    SafetySeverity.CRITICAL,
                    f"safety check {name!r} could not be evaluated "
                    f"({type(exc).__name__}) -- failing safe (R12)",
                )
            )
    return findings, errors


def _preflight_findings(
    snapshot: TelemetrySnapshot, context: SafetyContext
) -> tuple[list[_Finding], list[str]]:
    checks: list[tuple[str, Callable[[], list[_Finding]]]] = [
        ("readiness", lambda: check_preflight_readiness(snapshot, context)),
        ("battery", lambda: check_battery(snapshot)),
        ("gps", lambda: check_gps(snapshot, 0.0, preflight=True)),
        ("geofence", lambda: check_geofence(snapshot, context, preflight=True)),
        ("lateral_offset", lambda: check_lateral_offset(snapshot, context, preflight=True)),
    ]
    findings: list[_Finding] = []
    errors: list[str] = []
    for name, fn in checks:
        try:
            findings.extend(fn())
        except Exception as exc:  # noqa: BLE001 -- R12: a broken check must DENY
            log.exception("safety: pre-flight check %r raised; rejecting", name)
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            findings.append(
                _Finding(
                    SafetyAction.REJECT,
                    SafetySeverity.CRITICAL,
                    f"pre-flight check {name!r} could not be evaluated "
                    f"({type(exc).__name__}) -- rejecting (R12)",
                )
            )
    return findings, errors


def _worst(findings: Sequence[_Finding]) -> tuple[SafetyAction, SafetySeverity]:
    action = SafetyAction.ALLOW
    severity = SafetySeverity.NOMINAL
    for f in findings:
        if _ACTION_RANK[f.action] > _ACTION_RANK[action]:
            action = f.action
        if _SEVERITY_RANK[f.severity] > _SEVERITY_RANK[severity]:
            severity = f.severity
    return action, severity


def update_latch(
    prior_latch: SafetyAction | None,
    raw_action: SafetyAction,
    snapshot: TelemetrySnapshot,
) -> SafetyAction | None:
    """R13. The latched action is the most drastic of (prior latch, this tick's
    raw action) among the latchable set -- and it stays until the drone is on
    the ground and disarmed. LAND_NOW never de-escalates to RTL_NOW."""
    candidates = [a for a in (prior_latch, raw_action) if a in _LATCHABLE]
    if not candidates:
        return None
    latched = max(candidates, key=lambda a: _ACTION_RANK[a])
    if _on_ground_and_disarmed(snapshot):
        return None
    return latched


def watchdog_verdict(
    last_tick_age_s: float, now: datetime, *, watchdog_s: float | None = None
) -> SafetyVerdict | None:
    """A safety loop that has silently stopped is worse than none, because
    everything downstream still believes it is protected. Past the watchdog,
    that is an emergency."""
    limit = config.SAFETY_WATCHDOG_S if watchdog_s is None else watchdog_s
    if last_tick_age_s > limit:
        return SafetyVerdict(
            action=SafetyAction.LAND_NOW,
            severity=SafetySeverity.EMERGENCY,
            reasons=(
                f"safety loop stalled: last tick completed {last_tick_age_s:.1f}s "
                f"ago (watchdog {limit:.1f}s)",
            ),
            checked_at=now,
            latched=True,
            latch_action=SafetyAction.LAND_NOW,
        )
    return None


def evaluate(
    snapshot: TelemetrySnapshot,
    context: SafetyContext,
    *,
    now: datetime,
    link_down_s: float = 0.0,
    gps_bad_s: float = 0.0,
    discharge_rate_pct_per_s: float | None = None,
    wind_ms: float = 0.0,
    wind_speed_ms: float | None = None,
    wind_from_deg: float | None = None,
    prior_latch: SafetyAction | None = None,
) -> SafetyVerdict:
    """The one decision function. Pure: state in, verdict out.

    ``context.preflight`` selects the pre-flight gate (verdict ALLOW or REJECT)
    from the in-flight monitor (ALLOW / WARN / HOLD / RTL_NOW / LAND_NOW).
    Durations that the pure core cannot measure for itself -- ``link_down_s``,
    ``gps_bad_s`` -- are passed in by the shell.
    """
    if context.preflight:
        findings, _errors = _preflight_findings(snapshot, context)
        blocking = [f for f in findings if f.action not in (SafetyAction.ALLOW, SafetyAction.WARN)]
        if blocking:
            _, severity = _worst(blocking)
            return SafetyVerdict(
                action=SafetyAction.REJECT,
                severity=severity,
                reasons=tuple(f.reason for f in blocking),
                checked_at=now,
            )
        return SafetyVerdict(
            action=SafetyAction.ALLOW,
            severity=SafetySeverity.NOMINAL,
            reasons=(),
            checked_at=now,
        )

    findings, _errors = _inflight_findings(
        snapshot,
        context,
        link_down_s=link_down_s,
        gps_bad_s=gps_bad_s,
        discharge_rate_pct_per_s=discharge_rate_pct_per_s,
        wind_ms=wind_ms,
        wind_speed_ms=wind_speed_ms,
        wind_from_deg=wind_from_deg,
    )
    raw_action, severity = _worst(findings)
    reasons = [f.reason for f in findings if f.action != SafetyAction.ALLOW]
    divert_target = next(
        (
            f.data
            for f in findings
            if f.action in (SafetyAction.DIVERT, SafetyAction.LAND_NOW) and f.data
        ),
        None,
    )

    new_latch = update_latch(prior_latch, raw_action, snapshot)
    action = raw_action
    if new_latch is not None:
        if _ACTION_RANK[new_latch] > _ACTION_RANK[action]:
            action = new_latch
        if _SEVERITY_RANK[SafetySeverity.CRITICAL] > _SEVERITY_RANK[severity]:
            severity = SafetySeverity.CRITICAL
        if not any(
            f.action in _LATCHABLE for f in findings
        ):
            reasons.append(
                f"prior {new_latch.value} verdict latched -- clears only when "
                f"on the ground and disarmed (R13)"
            )

    return SafetyVerdict(
        action=action,
        severity=severity,
        reasons=tuple(reasons),
        checked_at=now,
        latched=new_latch is not None,
        latch_action=new_latch,
        divert_target=divert_target,
    )


def observed_discharge_rate(
    samples: Sequence[tuple[float, float]], window_s: float
) -> float | None:
    """Pack discharge in %/s over a rolling window of ``(monotonic_s, pct)``
    samples, oldest first. ``None`` when there is not enough data or the pack
    is not discharging -- the caller then uses a conservative fixed rate."""
    if len(samples) < 2:
        return None
    t0, p0 = samples[0]
    t1, p1 = samples[-1]
    span = t1 - t0
    if span < min(10.0, window_s / 2.0):
        return None
    drop = p0 - p1
    if drop <= 0:
        return None
    return drop / span


# ===========================================================================
# The shell: the safety loop
# ===========================================================================

EventSink = Callable[..., object]
SnapshotSource = Callable[[], TelemetrySnapshot]
SafeSpotSource = Callable[[], "Sequence[SafeSpot]"]


def _default_safe_spots() -> tuple[SafeSpot, ...]:
    """``config.SAFE_SPOTS_FALLBACK`` as :class:`SafeSpot` objects. Used when
    nothing wires a live safe-spot source (tests, and any moment before
    :class:`gss.safe_spots.SafeSpotBook` has loaded its first list)."""
    out: list[SafeSpot] = []
    for row in config.SAFE_SPOTS_FALLBACK:
        try:
            out.append(
                SafeSpot(
                    name=str(row["name"]),
                    lat=float(row["lat"]),  # type: ignore[arg-type]
                    lon=float(row["lon"]),  # type: ignore[arg-type]
                    radius_m=float(row.get("radius_m", 10.0)),  # type: ignore[arg-type]
                    surface=str(row.get("surface", "open_ground")),
                    has_marker=bool(row.get("has_marker", False)),
                    height_above_dock_m=float(row.get("height_above_dock_m", 0.0)),  # type: ignore[arg-type]
                    marker_id=(str(row["marker_id"]) if row.get("marker_id") else None),
                )
            )
        except (KeyError, TypeError, ValueError):
            log.warning("safety: ignoring malformed SAFE_SPOTS_FALLBACK row %r", row)
    return tuple(out)


@dataclass
class _LoopState:
    latch: SafetyAction | None = None
    gps_bad_since: float | None = None
    link_down_since: float | None = None
    samples: deque = field(default_factory=deque)


class SafetyMonitor:
    """Runs the safety core on a dedicated thread at SAFETY_TICK_HZ, independent
    of the command handler and the mission supervisor, and keeps evaluating even
    when no mission is active. Also watches ITSELF (SAFETY_WATCHDOG_S).

    Verdict changes are logged locally FIRST, then handed to ``event_sink``
    best-effort. The local log is the record of truth; Supabase is a
    convenience. ``event_sink`` is injected (never imported) so this module
    keeps its no-network boundary (R1).
    """

    def __init__(
        self,
        snapshot_source: SnapshotSource,
        *,
        event_sink: EventSink | None = None,
        home_lat: float | None = None,
        home_lon: float | None = None,
        tick_hz: float | None = None,
        watchdog_s: float | None = None,
        safe_spots_source: SafeSpotSource | None = None,
    ) -> None:
        self._snapshot_source = snapshot_source
        self._event_sink = event_sink
        self._home_lat = home_lat if home_lat is not None else config.HOME_LAT
        self._home_lon = home_lon if home_lon is not None else config.HOME_LON
        # Injected, never imported -- gss.safe_spots lives outside this module's
        # no-network boundary (R1). Defaults to the config fallback.
        self._safe_spots_source = safe_spots_source or _default_safe_spots
        self._period_s = 1.0 / (tick_hz or config.SAFETY_TICK_HZ)
        self._watchdog_s = watchdog_s if watchdog_s is not None else config.SAFETY_WATCHDOG_S

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._state = _LoopState()
        self._context: SafetyContext | None = None
        self._last_verdict: SafetyVerdict | None = None
        self._last_tick_mono = time.monotonic()
        self._watchdog_tripped = False

    # ---------------------------------------------------------------- API

    def start(self) -> None:
        if self._threads:
            return
        self._last_tick_mono = time.monotonic()
        self._threads = [
            threading.Thread(target=self._loop, name="safety-loop", daemon=True),
            threading.Thread(target=self._watchdog, name="safety-watchdog", daemon=True),
        ]
        for t in self._threads:
            t.start()
        log.info(
            "safety monitor started (%.1f Hz, watchdog %.1fs) -- local-only, no network",
            1.0 / self._period_s, self._watchdog_s,
        )

    def close(self, timeout_s: float = 3.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=timeout_s)
        self._threads = []
        log.info("safety monitor stopped")

    def set_context(self, context: SafetyContext | None) -> None:
        """The mission supervisor sets this when a mission starts / changes
        phase, and clears it (``None``) when the mission ends."""
        with self._lock:
            self._context = context
            if context is None:
                # A finished mission: reset the per-mission timers. The latch is
                # NOT reset here -- only being on the ground and disarmed clears
                # it (R13); the loop keeps evaluating and will clear it itself.
                self._state.gps_bad_since = None
                self._state.link_down_since = None

    def current_verdict(self) -> SafetyVerdict:
        """The latest verdict, for the supervisor to consult each tick. If the
        loop's watchdog has tripped, this is an EMERGENCY regardless of the last
        computed verdict."""
        now = datetime.now(timezone.utc)
        if self._watchdog_tripped:
            wd = watchdog_verdict(
                time.monotonic() - self._last_tick_mono, now, watchdog_s=self._watchdog_s
            )
            if wd is not None:
                return wd
        with self._lock:
            verdict = self._last_verdict
        if verdict is not None:
            return verdict
        return SafetyVerdict(SafetyAction.ALLOW, SafetySeverity.NOMINAL, (), now)

    def evaluate_command(
        self, command: dict, *, now: datetime | None = None, mission_active: bool = False
    ) -> SafetyVerdict:
        """Pre-flight gate for :mod:`gss.commands`. Returns ALLOW or REJECT.

        This is the FINAL authority. The intake validation in commands.py is a
        first filter, not a substitute -- nobody may delete either.
        """
        snapshot = self._snapshot_source()
        context = SafetyContext(
            preflight=True,
            is_summon=(command.get("type") == "summon"),
            target_lat=_num(command.get("target_lat")),
            target_lon=_num(command.get("target_lon")),
            target_alt_m=_num(command.get("target_alt_m")),
            home_lat=self._home_lat,
            home_lon=self._home_lon,
            human_lat=_num(_param(command, "human_lat")),
            human_lon=_num(_param(command, "human_lon")),
            mission_active=mission_active,
        )
        return evaluate(snapshot, context, now=now or datetime.now(timezone.utc))

    # ---------------------------------------------------------------- loop

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001 -- R8: never silent, never fatal
                log.exception("safety: tick failed")
            finally:
                self._last_tick_mono = time.monotonic()
            self._stop.wait(self._period_s)

    def _tick(self) -> None:
        snapshot = self._snapshot_source()
        now = datetime.now(timezone.utc)
        mono = time.monotonic()

        with self._lock:
            state = self._state
            context = self._context

            if snapshot.battery_pct is not None:
                state.samples.append((mono, float(snapshot.battery_pct)))
            cutoff = mono - config.DISCHARGE_WINDOW_S
            while state.samples and state.samples[0][0] < cutoff:
                state.samples.popleft()
            rate = observed_discharge_rate(list(state.samples), config.DISCHARGE_WINDOW_S)

            gps_ok, _ = gps_trustworthy(snapshot)
            if gps_ok:
                state.gps_bad_since = None
            elif state.gps_bad_since is None:
                state.gps_bad_since = mono
            gps_bad_s = 0.0 if state.gps_bad_since is None else mono - state.gps_bad_since

            if snapshot.connected:
                state.link_down_since = None
            elif state.link_down_since is None:
                state.link_down_since = mono
            link_down_s = (
                0.0 if state.link_down_since is None else mono - state.link_down_since
            )

            prior_latch = state.latch

        try:
            safe_spots = tuple(self._safe_spots_source() or ())
        except Exception:  # noqa: BLE001 -- the spot source must not break a tick
            log.exception("safety: safe-spot source failed; using none this tick")
            safe_spots = ()

        base_context = context or SafetyContext(
            preflight=False, home_lat=self._home_lat, home_lon=self._home_lon
        )
        eval_context = replace(base_context, safe_spots=safe_spots)
        wind_speed, wind_from = _observed_wind(snapshot)
        verdict = evaluate(
            snapshot,
            eval_context,
            now=now,
            link_down_s=link_down_s,
            gps_bad_s=gps_bad_s,
            discharge_rate_pct_per_s=rate,
            wind_ms=_observed_headwind_home(snapshot, eval_context),
            wind_speed_ms=wind_speed,
            wind_from_deg=wind_from,
            prior_latch=prior_latch,
        )

        with self._lock:
            self._state.latch = verdict.latch_action
            previous = self._last_verdict
            self._last_verdict = verdict

        if _verdict_changed(previous, verdict):
            self._emit(verdict, context)

    def _watchdog(self) -> None:
        # A second, deliberately trivial thread: if _loop stalls it cannot
        # notice its own failure.
        period = min(1.0, self._watchdog_s / 2.0)
        while not self._stop.is_set():
            age = time.monotonic() - self._last_tick_mono
            wd = watchdog_verdict(
                age, datetime.now(timezone.utc), watchdog_s=self._watchdog_s
            )
            if wd is not None:
                if not self._watchdog_tripped:
                    log.error(
                        "safety: loop watchdog tripped -- last tick %.1fs ago "
                        "(watchdog %.1fs). Treating as EMERGENCY.",
                        age, self._watchdog_s,
                    )
                    self._watchdog_tripped = True
                    self._emit(wd, self._context)
            elif self._watchdog_tripped:
                log.warning("safety: loop watchdog recovered")
                self._watchdog_tripped = False
            self._stop.wait(period)

    # ------------------------------------------------------------- logging

    def _emit(self, verdict: SafetyVerdict, context: SafetyContext | None) -> None:
        level = {
            SafetySeverity.NOMINAL: logging.INFO,
            SafetySeverity.CAUTION: logging.WARNING,
            SafetySeverity.CRITICAL: logging.ERROR,
            SafetySeverity.EMERGENCY: logging.CRITICAL,
        }[verdict.severity]
        log.log(
            level,
            "SAFETY %s / %s%s: %s",
            verdict.action.value,
            verdict.severity.value,
            " [latched]" if verdict.latched else "",
            "; ".join(verdict.reasons) or "nominal",
        )
        sink = self._event_sink
        if sink is None or not verdict.is_veto():
            return
        mission_id = context.mission_id if context is not None else None
        if mission_id is not None:
            # A mission is active: the mission supervisor owns the mission_events
            # row for this veto (it writes it sync, with the full reason list and
            # the executor action it took). Writing a second row here would just
            # duplicate it. The local log above is still the record of truth.
            return
        try:
            sink(
                mission_id,
                "safety_veto",
                {
                    "action": verdict.action.value,
                    "severity": verdict.severity.value,
                    "latched": verdict.latched,
                    "reasons": list(verdict.reasons),
                    "divert_target": verdict.divert_target,
                },
                sync=False,
            )
        except Exception:  # noqa: BLE001 -- best-effort; local log already has it
            log.exception("safety: event sink failed (non-fatal; local log stands)")


# ===========================================================================
# small helpers
# ===========================================================================


def _fmt_age(value: float | None) -> str:
    return "never" if value is None else f"{value:.1f}s"


def _num(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _param(command: dict, key: str):
    params = command.get("params")
    if isinstance(params, dict) and key in params:
        return params[key]
    return command.get(key)


def _verdict_changed(a: SafetyVerdict | None, b: SafetyVerdict) -> bool:
    if a is None:
        return True
    return (
        a.action is not b.action
        or a.severity is not b.severity
        or a.reasons != b.reasons
        or a.latched != b.latched
    )
