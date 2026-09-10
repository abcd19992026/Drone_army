"""weather.py -- conditions awareness and the stay-or-return decision (v0.5).

THE ONE THING THIS MODULE MUST GET RIGHT
----------------------------------------
There is a person this drone gets summoned by, and there are moments where he
is being attacked and cannot look at his phone. If bad weather turns the drone
around and sends it home in that moment, the system has failed at the only job
that justifies its existence.

So the default flips on mission type, and it must never be "simplified" into
one uniform behaviour:

  * ROUTINE mission (patrol, inspect, survey, test) + deteriorating weather
    -> GO HOME. Nobody is waiting.

  * EMERGENCY mission (summon, family_summon, search, accident) +
    deteriorating weather -> STAY. The drone holds station and keeps working.
    It leaves only when conditions cross a limit at which it can no longer fly
    safely AT ALL -- past that point staying does not help him either, it just
    adds a crashing aircraft to his situation.

AUTHORITY ORDER -- do not blur this
-----------------------------------
    safety.py  >  weather.py  >  commands  >  the human

weather.py can only make the system MORE conservative, never less. It may not
delay, override, or soften a safety.py verdict. Battery floor, point of no
return, geofence and link loss still win over everything, a human saying
"stay" included. If weather says STAY and safety says RTL_NOW, the drone goes
home.

The single place weather.py *extends* a mission is the emergency default
above, and that is bounded on both sides: by the SEVERE threshold, and by
safety.py, which is still evaluating and latching every tick.

TWO DATA SOURCES, AND WHY THE SPLIT MATTERS
------------------------------------------
  * FORECAST (from the internet, via gss.weather_feed) -- PRE-FLIGHT ONLY.
    Never the basis of an in-flight decision: R1 forbids depending on the
    internet while airborne.
  * OBSERVED (from the aircraft itself: WIND, VIBRATION, VFR_HUD throttle) --
    IN-FLIGHT. Better data anyway: a forecast is a guess about a 10 km square,
    the drone is measuring the air it is in.

This module is PURE: conditions in, verdict out. No I/O, no clock reads inside
the decisions (``now`` is passed in), no import of urllib / http / requests /
websockets / gss.store / gss.weather_feed. safety.py may import it, so the
import-boundary test covers it with the same strictness (tests/test_safety.py
and tests/test_weather.py).

Unknown or stale data classifies as MARGINAL -- never CLEAR (don't pretend),
never SEVERE (refusing to fly because the internet is down is its own
failure). Lightning is the one absolute: within LIGHTNING_RADIUS_KM is SEVERE
with no emergency exception, because unlike wind there is no partial version
of being struck.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from gss import config

# Sustained-throttle window: a brief throttle spike is a gust, a sustained one
# is the aircraft telling you it is near its limit. Starting point, tune on the
# real airframe.
_THROTTLE_SUSTAINED_S = 8.0


class WeatherTier(str, Enum):
    CLEAR = "CLEAR"        # everything within limits
    MARGINAL = "MARGINAL"  # flyable with elevated risk
    SEVERE = "SEVERE"      # not flyable


class WeatherAction(str, Enum):
    ALLOW = "ALLOW"      # proceed (pre-flight) / keep going (in flight)
    CONFIRM = "CONFIRM"  # routine + marginal pre-flight: wait for a human
    BLOCK = "BLOCK"      # do not launch
    WARN = "WARN"        # in flight: tell someone, keep flying for now
    STAY = "STAY"        # in flight: hold station and keep working
    RETURN = "RETURN"    # in flight: go home now


_TIER_RANK: dict[WeatherTier, int] = {
    WeatherTier.CLEAR: 0,
    WeatherTier.MARGINAL: 1,
    WeatherTier.SEVERE: 2,
}


@dataclass(frozen=True)
class WeatherConditions:
    """Normalised conditions from one source. Every field may be ``None``
    (not measured / not in the forecast); a missing field a check needs is
    treated as MARGINAL, never CLEAR."""

    source: str                       # "forecast" | "observed"
    age_s: float | None               # forecast: seconds since fetched
    wind_speed_ms: float | None = None
    wind_gust_ms: float | None = None
    precip_mmh: float | None = None
    visibility_m: float | None = None
    temperature_c: float | None = None
    lightning_nearby: bool | None = None    # within LIGHTNING_RADIUS_KM
    # observed-only
    throttle_pct: float | None = None
    vibration_max: float | None = None
    wind_direction_deg: float | None = None


@dataclass(frozen=True)
class WeatherVerdict:
    tier: WeatherTier
    reasons: tuple[str, ...]
    checked_at: datetime
    stale: bool = False
    source: str = "forecast"


@dataclass(frozen=True)
class WeatherDecision:
    action: WeatherAction
    tier: WeatherTier
    reasons: tuple[str, ...]
    is_emergency: bool
    checked_at: datetime


# ===========================================================================
# mission classification
# ===========================================================================


def is_emergency_mission(mission_type: str | None) -> bool:
    """True if a mission of this type must not be turned back by weather short
    of SEVERE (config.EMERGENCY_MISSION_TYPES)."""
    return (mission_type or "") in config.EMERGENCY_MISSION_TYPES


# ===========================================================================
# the checks -- each pure, each contributing a named reason
# ===========================================================================

_Finding = tuple[WeatherTier, str]


def check_wind(wind_ms: float | None) -> list[_Finding]:
    if wind_ms is None:
        return [(WeatherTier.MARGINAL, "sustained wind unknown")]
    if wind_ms >= config.WIND_SEVERE_MS:
        return [(
            WeatherTier.SEVERE,
            f"sustained wind {wind_ms:.1f} m/s at/above severe threshold "
            f"{config.WIND_SEVERE_MS:.1f} m/s",
        )]
    if wind_ms >= config.WIND_MARGINAL_MS:
        return [(
            WeatherTier.MARGINAL,
            f"sustained wind {wind_ms:.1f} m/s at/above marginal threshold "
            f"{config.WIND_MARGINAL_MS:.1f} m/s",
        )]
    return []


def check_gusts(gust_ms: float | None) -> list[_Finding]:
    if gust_ms is None:
        return [(WeatherTier.MARGINAL, "wind gusts unknown")]
    if gust_ms >= config.GUST_SEVERE_MS:
        return [(
            WeatherTier.SEVERE,
            f"wind gusts {gust_ms:.1f} m/s at/above severe threshold "
            f"{config.GUST_SEVERE_MS:.1f} m/s",
        )]
    if gust_ms >= config.GUST_MARGINAL_MS:
        return [(
            WeatherTier.MARGINAL,
            f"wind gusts {gust_ms:.1f} m/s at/above marginal threshold "
            f"{config.GUST_MARGINAL_MS:.1f} m/s",
        )]
    return []


def check_precip(precip_mmh: float | None) -> list[_Finding]:
    if precip_mmh is None:
        return [(WeatherTier.MARGINAL, "precipitation unknown")]
    if precip_mmh >= config.PRECIP_SEVERE_MMH:
        return [(
            WeatherTier.SEVERE,
            f"precipitation {precip_mmh:.1f} mm/h at/above severe threshold "
            f"{config.PRECIP_SEVERE_MMH:.1f} mm/h",
        )]
    if precip_mmh >= config.PRECIP_MARGINAL_MMH:
        return [(
            WeatherTier.MARGINAL,
            f"precipitation {precip_mmh:.2f} mm/h at/above marginal threshold "
            f"{config.PRECIP_MARGINAL_MMH:.2f} mm/h",
        )]
    return []


def check_visibility(visibility_m: float | None) -> list[_Finding]:
    if visibility_m is None:
        return [(WeatherTier.MARGINAL, "visibility unknown")]
    if visibility_m < config.VISIBILITY_MIN_M:
        return [(
            WeatherTier.SEVERE,
            f"visibility {visibility_m:.0f} m below minimum "
            f"{config.VISIBILITY_MIN_M:.0f} m",
        )]
    if visibility_m < config.VISIBILITY_MIN_M * 1.5:
        return [(
            WeatherTier.MARGINAL,
            f"visibility {visibility_m:.0f} m, close to the "
            f"{config.VISIBILITY_MIN_M:.0f} m minimum",
        )]
    return []


def check_temperature(temp_c: float | None) -> list[_Finding]:
    if temp_c is None:
        return [(WeatherTier.MARGINAL, "temperature unknown")]
    if temp_c < config.TEMP_MIN_C or temp_c > config.TEMP_MAX_C:
        return [(
            WeatherTier.SEVERE,
            f"temperature {temp_c:.0f} C outside the flyable range "
            f"[{config.TEMP_MIN_C:.0f}, {config.TEMP_MAX_C:.0f}] C "
            f"(battery performance falls off at both ends)",
        )]
    margin = 5.0
    if temp_c < config.TEMP_MIN_C + margin or temp_c > config.TEMP_MAX_C - margin:
        return [(
            WeatherTier.MARGINAL,
            f"temperature {temp_c:.0f} C near the edge of the flyable range "
            f"[{config.TEMP_MIN_C:.0f}, {config.TEMP_MAX_C:.0f}] C",
        )]
    return []


def check_lightning(lightning_nearby: bool | None) -> list[_Finding]:
    """Absolute. Lightning within LIGHTNING_RADIUS_KM is SEVERE, no exception,
    pre-flight or in-flight. A drone is a small metal object on the highest
    point around and there is no partial version of being struck."""
    if lightning_nearby is None:
        return [(WeatherTier.MARGINAL, "lightning status unknown")]
    if lightning_nearby:
        return [(
            WeatherTier.SEVERE,
            f"lightning within {config.LIGHTNING_RADIUS_KM:.0f} km -- absolute, "
            f"no emergency exception",
        )]
    return []


def check_throttle(throttle_pct: float | None, sustained_s: float) -> list[_Finding]:
    """Throttle is the most honest signal on the aircraft. A drone fighting
    wind holds a high throttle to stay in one place. Sustained low margin is a
    SEVERE condition in its own right, not a hint."""
    if throttle_pct is None:
        return [(WeatherTier.MARGINAL, "throttle unknown")]
    margin = 100.0 - throttle_pct
    if margin < config.THROTTLE_MARGIN_MIN_PCT:
        if sustained_s >= _THROTTLE_SUSTAINED_S:
            return [(
                WeatherTier.SEVERE,
                f"throttle margin {margin:.0f}% below minimum "
                f"{config.THROTTLE_MARGIN_MIN_PCT:.0f}% for {sustained_s:.0f}s -- "
                f"the aircraft is near its limit regardless of any forecast",
            )]
        return [(
            WeatherTier.MARGINAL,
            f"throttle margin {margin:.0f}% below minimum "
            f"{config.THROTTLE_MARGIN_MIN_PCT:.0f}%",
        )]
    return []


def check_vibration(vibration_max: float | None, rising_sharply: bool) -> list[_Finding]:
    if vibration_max is None:
        return []  # a bonus signal, not required data
    if vibration_max >= config.VIBRATION_SEVERE:
        return [(
            WeatherTier.SEVERE,
            f"vibration {vibration_max:.0f} at/above severe threshold "
            f"{config.VIBRATION_SEVERE:.0f} -- turbulence",
        )]
    if rising_sharply:
        return [(
            WeatherTier.MARGINAL,
            f"vibration rising sharply (now {vibration_max:.0f}) -- turbulence",
        )]
    return []


# ===========================================================================
# classification
# ===========================================================================


def _worst(findings: list[_Finding]) -> tuple[WeatherTier, list[str]]:
    tier = WeatherTier.CLEAR
    for t, _ in findings:
        if _TIER_RANK[t] > _TIER_RANK[tier]:
            tier = t
    reasons = [r for t, r in findings if t != WeatherTier.CLEAR]
    return tier, reasons


def classify_forecast(conditions: WeatherConditions, *, now: datetime) -> WeatherVerdict:
    """Pre-flight classification from the internet forecast."""
    stale = conditions.age_s is None or conditions.age_s > config.WEATHER_MAX_AGE_S
    if stale:
        age = "unknown" if conditions.age_s is None else f"{conditions.age_s / 60:.0f} min old"
        return WeatherVerdict(
            tier=WeatherTier.MARGINAL,
            reasons=(
                f"forecast is stale ({age}, max {config.WEATHER_MAX_AGE_S / 60:.0f} "
                f"min) -- treated as MARGINAL, not as good news",
            ),
            checked_at=now,
            stale=True,
            source="forecast",
        )
    findings: list[_Finding] = []
    findings += check_wind(conditions.wind_speed_ms)
    findings += check_gusts(conditions.wind_gust_ms)
    findings += check_precip(conditions.precip_mmh)
    findings += check_visibility(conditions.visibility_m)
    findings += check_temperature(conditions.temperature_c)
    findings += check_lightning(conditions.lightning_nearby)
    tier, reasons = _worst(findings)
    return WeatherVerdict(tier, tuple(reasons), now, stale=False, source="forecast")


def classify_observed(
    conditions: WeatherConditions,
    *,
    now: datetime,
    throttle_low_s: float = 0.0,
    vibration_rising: bool = False,
    forecast_lightning: bool | None = None,
) -> WeatherVerdict:
    """In-flight classification from what the aircraft is measuring. No precip
    / visibility / temperature checks -- the drone has no sensor for those.
    ``forecast_lightning`` carries the last pre-flight forecast's lightning
    status forward as cached LOCAL data (no network call): lightning is
    absolute in flight too."""
    findings: list[_Finding] = []
    findings += check_wind(conditions.wind_speed_ms)
    # No gust / precip / visibility / temperature checks here: the aircraft has
    # no sensor for any of them. Sustained wind, throttle and vibration are the
    # honest in-flight signals.
    findings += check_throttle(conditions.throttle_pct, throttle_low_s)
    findings += check_vibration(conditions.vibration_max, vibration_rising)
    if forecast_lightning:
        findings += check_lightning(True)
    tier, reasons = _worst(findings)
    return WeatherVerdict(tier, tuple(reasons), now, stale=False, source="observed")


# ===========================================================================
# the stay-or-return decision
# ===========================================================================


def preflight_decision(
    verdict: WeatherVerdict, *, is_emergency: bool, now: datetime
) -> WeatherDecision:
    tier = verdict.tier
    if tier == WeatherTier.CLEAR:
        return WeatherDecision(WeatherAction.ALLOW, tier, (), is_emergency, now)

    if tier == WeatherTier.MARGINAL:
        if is_emergency:
            # Launch immediately, no question asked. A person in danger is not
            # going to fill in a form. The caller logs that it launched into
            # marginal conditions and why.
            return WeatherDecision(
                WeatherAction.ALLOW,
                tier,
                verdict.reasons
                + ("emergency mission -- launching into marginal conditions, "
                   "no confirmation requested",),
                True,
                now,
            )
        return WeatherDecision(WeatherAction.CONFIRM, tier, verdict.reasons, False, now)

    # SEVERE -- refuse. For an emergency mission the caller must raise a loud,
    # fully explained alert: someone needs to know help is not on the way.
    return WeatherDecision(WeatherAction.BLOCK, tier, verdict.reasons, is_emergency, now)


def inflight_decision(
    verdict: WeatherVerdict,
    *,
    is_emergency: bool,
    warn_elapsed_s: float,
    now: datetime,
    human_recalled: bool = False,
    human_continue: bool = False,
) -> WeatherDecision:
    tier = verdict.tier

    if human_recalled:
        return WeatherDecision(
            WeatherAction.RETURN, tier, verdict.reasons + ("human recall",),
            is_emergency, now,
        )

    if tier == WeatherTier.CLEAR:
        return WeatherDecision(WeatherAction.ALLOW, tier, (), is_emergency, now)

    if is_emergency:
        if tier == WeatherTier.SEVERE:
            # Return, and it is NOT overridable. Staying does not help him now.
            return WeatherDecision(
                WeatherAction.RETURN, tier,
                verdict.reasons + ("SEVERE -- past the limit at which the aircraft "
                                   "can fly safely at all; not overridable",),
                True, now,
            )
        # MARGINAL: warn and STAY. No countdown, no answer required to keep
        # working. Only an explicit recall (or SEVERE) brings it home.
        return WeatherDecision(WeatherAction.STAY, tier, verdict.reasons, True, now)

    # routine mission
    if tier == WeatherTier.SEVERE:
        return WeatherDecision(WeatherAction.RETURN, tier, verdict.reasons, False, now)

    # routine + MARGINAL: warn, then return after the grace period unless a
    # human explicitly says continue. Default on no answer is RETURN.
    if human_continue:
        return WeatherDecision(
            WeatherAction.ALLOW, tier, verdict.reasons + ("human said continue",),
            False, now,
        )
    if warn_elapsed_s >= config.WEATHER_WARN_GRACE_S:
        return WeatherDecision(
            WeatherAction.RETURN, tier,
            verdict.reasons
            + (f"no 'continue' within the {config.WEATHER_WARN_GRACE_S:.0f}s grace",),
            False, now,
        )
    return WeatherDecision(WeatherAction.WARN, tier, verdict.reasons, False, now)


# ===========================================================================
# wind -> point of no return
# ===========================================================================


def headwind_component_ms(
    wind_from_deg: float | None,
    wind_speed_ms: float | None,
    course_to_home_deg: float | None,
) -> float:
    """The headwind component (m/s, + = headwind) the drone would fly into on
    the way home. A 10 m/s headwind against a 10 m/s cruise means the drone
    does not get home at all -- feeding this into safety.py's point-of-no-return
    is not a refinement, it is the difference between an honest estimate and an
    optimistic one in exactly the conditions where optimism is fatal.

    ``wind_from_deg`` is the compass direction the wind blows FROM. Returns 0.0
    if any input is missing (neutral -- safety.py clamps negatives anyway)."""
    if wind_from_deg is None or wind_speed_ms is None or course_to_home_deg is None:
        return 0.0
    # Wind blows from wind_from_deg. Flying a course of course_to_home_deg, the
    # component opposing you is speed * cos(angle between "where the wind is
    # from" and "where you are going").
    angle = math.radians((wind_from_deg - course_to_home_deg + 180.0) % 360.0 - 180.0)
    return wind_speed_ms * math.cos(angle)


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, degrees [0, 360)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0
