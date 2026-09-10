"""Single source of truth for every GSS setting.

Values are read once, at import time, from environment variables (optionally
populated from a ``.env`` file) and exposed as module-level constants. Every
value has a sensible default, so the service runs with no ``.env`` present.

When the real drone flies, only ``MAVLINK_CONNECTION`` changes -- from a TCP
SITL endpoint to a serial port. No other module in this project should ever
contain a connection string, an IP, a port, or a bare magic number: import
the relevant constant from here instead.

Several constants below are declared now but unused in v0.1 (altitudes,
battery thresholds, geofence limits, recording retention). They live here so
that later versions never redefine them in another file.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

_VALID_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}


def _get_str(name: str, default: str) -> str:
    """Return env var ``name`` as a stripped string, or ``default`` if unset/blank."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


def _get_float(name: str, default: float) -> float:
    """Return env var ``name`` parsed as a float, or ``default`` if unset/blank."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"config: {name} must be a number, got {raw!r}") from exc


def _get_int(name: str, default: int) -> int:
    """Return env var ``name`` parsed as an int, or ``default`` if unset/blank."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"config: {name} must be an integer, got {raw!r}") from exc


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _get_bool(name: str, default: bool) -> bool:
    """Return env var ``name`` parsed as a boolean, or ``default`` if unset/blank."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(
        f"config: {name} must be one of {sorted(_TRUE | _FALSE)}, got {raw!r}"
    )


def _looks_like_uuid(value: str) -> bool:
    """True if ``value`` is a canonical 8-4-4-4-12 hex UUID string."""
    parts = value.split("-")
    if len(parts) != 5 or [len(p) for p in parts] != [8, 4, 4, 4, 12]:
        return False
    try:
        int(value.replace("-", ""), 16)
    except ValueError:
        return False
    return True


# --- Link -------------------------------------------------------------------
MAVLINK_CONNECTION: str = _get_str("MAVLINK_CONNECTION", "tcp:127.0.0.1:5762")

# --- Dock / home location (decimal degrees) ---------------------------------
HOME_LAT: float = _get_float("HOME_LAT", 25.5932)
HOME_LON: float = _get_float("HOME_LON", 85.2045)

# --- Altitudes (metres) ----------------------------------------------------
CRUISE_ALT_INBOUND: float = _get_float("CRUISE_ALT_INBOUND", 45.0)
CRUISE_ALT_OUTBOUND: float = _get_float("CRUISE_ALT_OUTBOUND", 55.0)
ON_STATION_ALT: float = _get_float("ON_STATION_ALT", 28.0)
LATERAL_OFFSET_M: float = _get_float("LATERAL_OFFSET_M", 12.0)

# --- Geofence ------------------------------------------------------------
MAX_RADIUS_M: float = _get_float("MAX_RADIUS_M", 6000.0)
MAX_ALT_M: float = _get_float("MAX_ALT_M", 60.0)

# --- Battery ladder (percent) --------------------------------------------
BATTERY_WARN_PCT: float = _get_float("BATTERY_WARN_PCT", 40.0)
BATTERY_FLOOR_PCT: float = _get_float("BATTERY_FLOOR_PCT", 20.0)

# --- Recording retention -------------------------------------------------
RECORDING_KEEP_DAYS: int = _get_int("RECORDING_KEEP_DAYS", 30)

# --- Timing ------------------------------------------------------------
TELEMETRY_INTERVAL_S: float = _get_float("TELEMETRY_INTERVAL_S", 2.0)
HEARTBEAT_TIMEOUT_S: float = _get_float("HEARTBEAT_TIMEOUT_S", 5.0)

# --- Reconnect backoff ---------------------------------------------------
# Kept short: this service will one day be reconnecting to an aircraft that
# is airborne, and 30s of blindness after the link is physically restored is
# not acceptable.
RECONNECT_BACKOFF_START_S: float = _get_float("RECONNECT_BACKOFF_START_S", 1.0)
RECONNECT_BACKOFF_CAP_S: float = _get_float("RECONNECT_BACKOFF_CAP_S", 5.0)

# --- Telemetry stream rates (Hz) ---------------------------------------
# link.py asks the vehicle to emit these messages at these rates on every
# (re)connection. telemetry.py owns this list; these are the knobs.
STREAM_RATE_POSITION_HZ: float = _get_float("STREAM_RATE_POSITION_HZ", 4.0)  # GLOBAL_POSITION_INT
STREAM_RATE_VFR_HZ: float = _get_float("STREAM_RATE_VFR_HZ", 2.0)            # VFR_HUD
STREAM_RATE_STATUS_HZ: float = _get_float("STREAM_RATE_STATUS_HZ", 1.0)     # SYS_STATUS, BATTERY_STATUS
STREAM_RATE_GPS_HZ: float = _get_float("STREAM_RATE_GPS_HZ", 1.0)           # GPS_RAW_INT

# Stream watchdog: if the link is up but no non-HEARTBEAT message has arrived
# for this long, re-send the stream requests (rate-limited).
STREAM_WATCHDOG_S: float = _get_float("STREAM_WATCHDOG_S", 5.0)
STREAM_REREQUEST_MIN_S: float = _get_float("STREAM_REREQUEST_MIN_S", 2.0)

# --- Supabase sync (v0.2) --------------------------------------------------
# The network side is strictly downstream and best-effort (rule R10). None of
# these values are read on the MAVLink path; safety-critical numbers stay in
# this file, never in the database (rule R1).
SUPABASE_ENABLED: bool = _get_bool("SUPABASE_ENABLED", True)
SUPABASE_URL: str = _get_str("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY: str = _get_str("SUPABASE_SERVICE_ROLE_KEY", "")
DRONE_ID: str = _get_str("DRONE_ID", "d5030000-0000-4000-8000-000000000001")
DOCK_ID: str = _get_str("DOCK_ID", "d0c00000-0000-4000-8000-000000000001")
SUPABASE_TELEMETRY_INTERVAL_S: float = _get_float("SUPABASE_TELEMETRY_INTERVAL_S", 2.0)
SUPABASE_HEARTBEAT_WRITE_S: float = _get_float("SUPABASE_HEARTBEAT_WRITE_S", 15.0)
SUPABASE_QUEUE_MAX: int = _get_int("SUPABASE_QUEUE_MAX", 200)

# --- Command intake (v0.3) ------------------------------------------------
# The path from a commands row to a decision. Realtime is the fast path, the
# poller is the correct one -- both run. This module never transmits a MAVLink
# message (rule R11); it hands accepted missions to an executor.
COMMAND_POLL_INTERVAL_S: float = _get_float("COMMAND_POLL_INTERVAL_S", 3.0)
COMMAND_DEFAULT_TTL_S: float = _get_float("COMMAND_DEFAULT_TTL_S", 120.0)
# A flight command is only accepted while telemetry is this fresh -- accepting
# one while blind is not acceptable. (Intake sanity only; safety.py re-checks.)
TELEMETRY_MAX_AGE_S: float = _get_float("TELEMETRY_MAX_AGE_S", 10.0)
MISSION_MAX_DURATION_S: float = _get_float("MISSION_MAX_DURATION_S", 900.0)

# Guard rail: MUST stay false until safety.py (v0.4) exists and a real
# executor (v0.5) sits behind it. Nothing reads this except the executor
# factory's assertion and the check below. Do not set it true.
ALLOW_VEHICLE_CONTROL: bool = _get_bool("ALLOW_VEHICLE_CONTROL", False)

# --- safety.py (v0.4) ----------------------------------------------------------
# These live HERE and NOT in the database's system_config table, on purpose:
# safety.py must reach a correct verdict with no network and no Supabase
# (rule R1). A value safety depends on cannot sit behind an HTTP call that
# fails exactly when the drone is airborne and the broadband is down.
#
# Every number below is a STARTING POINT to be tuned against real flight data,
# not a measured truth. They are deliberately conservative: erring toward an
# early return costs a cut-short mission; erring the other way costs the
# aircraft. Do not tighten them without flight logs to justify it.
SAFETY_TICK_HZ: float = _get_float("SAFETY_TICK_HZ", 4.0)
SAFETY_WATCHDOG_S: float = _get_float("SAFETY_WATCHDOG_S", 3.0)
POSITION_MAX_AGE_S: float = _get_float("POSITION_MAX_AGE_S", 2.0)
BATTERY_MAX_AGE_S: float = _get_float("BATTERY_MAX_AGE_S", 5.0)
GPS_MAX_AGE_S: float = _get_float("GPS_MAX_AGE_S", 5.0)
BATTERY_CELLS: int = _get_int("BATTERY_CELLS", 6)
BATTERY_CELL_FLOOR_V: float = _get_float("BATTERY_CELL_FLOOR_V", 3.2)
BATTERY_LAUNCH_MIN_PCT: float = _get_float("BATTERY_LAUNCH_MIN_PCT", 50.0)
RTL_RESERVE_PCT: float = _get_float("RTL_RESERVE_PCT", 20.0)
CRUISE_SPEED_MS: float = _get_float("CRUISE_SPEED_MS", 10.0)
MIN_ALT_M: float = _get_float("MIN_ALT_M", 5.0)
GPS_MIN_SATELLITES: int = _get_int("GPS_MIN_SATELLITES", 8)
GPS_MAX_HDOP: float = _get_float("GPS_MAX_HDOP", 2.0)
GPS_LOSS_GRACE_S: float = _get_float("GPS_LOSS_GRACE_S", 15.0)
LINK_LOSS_GRACE_S: float = _get_float("LINK_LOSS_GRACE_S", 30.0)
DISCHARGE_WINDOW_S: float = _get_float("DISCHARGE_WINDOW_S", 60.0)

# --- weather.py (v0.5) -------------------------------------------------------
# Same R1 reason as the safety block above: weather.py is imported on the same
# side of the network boundary as safety.py, so its thresholds live here, not
# in system_config.
#
# EVERY threshold below is a STARTING POINT, not a measured truth. These are
# estimates for a ~3 kg hexacopter with large, slow props -- an airframe that
# does not exist yet. Its real limits are discovered by flying it, carefully,
# on a day you are willing to lose it. Tune these against flight logs from the
# real machine and nothing else.
#
# WEATHER_ENABLED=false makes the GSS behave exactly as v0.4 did: no feed, no
# WeatherMonitor, no weather branch anywhere.
WEATHER_ENABLED: bool = _get_bool("WEATHER_ENABLED", True)
WEATHER_PROVIDER_URL: str = _get_str(
    "WEATHER_PROVIDER_URL", "https://api.open-meteo.com/v1/forecast"
)
WEATHER_FETCH_INTERVAL_S: float = _get_float("WEATHER_FETCH_INTERVAL_S", 600.0)
WEATHER_MAX_AGE_S: float = _get_float("WEATHER_MAX_AGE_S", 3600.0)
WEATHER_CONFIRM_TIMEOUT_S: float = _get_float("WEATHER_CONFIRM_TIMEOUT_S", 120.0)
WEATHER_WARN_GRACE_S: float = _get_float("WEATHER_WARN_GRACE_S", 60.0)
WEATHER_CACHE_PATH: str = _get_str("WEATHER_CACHE_PATH", ".weather_cache.json")

WIND_MARGINAL_MS: float = _get_float("WIND_MARGINAL_MS", 8.0)
WIND_SEVERE_MS: float = _get_float("WIND_SEVERE_MS", 12.0)
GUST_MARGINAL_MS: float = _get_float("GUST_MARGINAL_MS", 10.0)
GUST_SEVERE_MS: float = _get_float("GUST_SEVERE_MS", 14.0)
PRECIP_MARGINAL_MMH: float = _get_float("PRECIP_MARGINAL_MMH", 0.2)
PRECIP_SEVERE_MMH: float = _get_float("PRECIP_SEVERE_MMH", 2.0)
VISIBILITY_MIN_M: float = _get_float("VISIBILITY_MIN_M", 1000.0)
TEMP_MIN_C: float = _get_float("TEMP_MIN_C", 0.0)
TEMP_MAX_C: float = _get_float("TEMP_MAX_C", 45.0)
LIGHTNING_RADIUS_KM: float = _get_float("LIGHTNING_RADIUS_KM", 15.0)
THROTTLE_MARGIN_MIN_PCT: float = _get_float("THROTTLE_MARGIN_MIN_PCT", 15.0)
VIBRATION_SEVERE: float = _get_float("VIBRATION_SEVERE", 30.0)
EMERGENCY_MISSION_TYPES: frozenset[str] = frozenset(
    t.strip()
    for t in _get_str(
        "EMERGENCY_MISSION_TYPES", "summon,family_summon,search,accident"
    ).split(",")
    if t.strip()
)

# --- safe spots + the unreachable-home divert (v0.5.1) --------------------
# safety.py's point-of-no-return check answers "can it still get home?". When
# the headwind-aware return budget climbs above RETURN_BUDGET_IMPOSSIBLE_PCT --
# or simply above the battery left plus the reserve -- home is NOT reachable,
# and sending the aircraft home anyway means it runs out somewhere along the
# way, under power, over whatever is beneath it. The verdict then is DIVERT:
# fly to the nearest reachable known safe spot and land there. A controlled
# landing in the wrong place beats an uncontrolled arrival in a random one.
#
# These live HERE and not in system_config for the same R1 reason as the
# safety / weather blocks above: safety.py must decide with no network. Every
# number is a STARTING POINT for an airframe that does not exist yet.
RETURN_BUDGET_IMPOSSIBLE_PCT: float = _get_float("RETURN_BUDGET_IMPOSSIBLE_PCT", 95.0)
SAFE_SPOT_REFRESH_S: float = _get_float("SAFE_SPOT_REFRESH_S", 300.0)
SAFE_SPOT_MAX_DISTANCE_M: float = _get_float("SAFE_SPOT_MAX_DISTANCE_M", 6000.0)
SAFE_SPOT_CACHE_PATH: str = _get_str("SAFE_SPOT_CACHE_PATH", ".safe_spots_cache.json")

# Two tiers of safe spot. A spot WITH an ArUco marker can be landed on
# accurately by the vision pipeline, so a small pad is fine. A spot WITHOUT a
# marker is GPS-only -- 3-10 m of error in the open, worse between buildings --
# so it must be a genuinely clear circle of at least SAFE_SPOT_MIN_GPS_RADIUS_M,
# and the selection function REJECTS a non-marker spot whose row claims less
# rather than trusting the number. A marker pad is worth up to
# SAFE_SPOT_MARKER_PREFERENCE_M of extra battery distance over a GPS-only spot.
SAFE_SPOT_MIN_GPS_RADIUS_M: float = _get_float("SAFE_SPOT_MIN_GPS_RADIUS_M", 20.0)
SAFE_SPOT_MARKER_PREFERENCE_M: float = _get_float("SAFE_SPOT_MARKER_PREFERENCE_M", 1000.0)

# safety.py cannot read the database (R1). gss/safe_spots.py loads the spot
# list from Supabase at startup and every SAFE_SPOT_REFRESH_S, caches it to
# disk for an offline boot, and falls back to this list so it is NEVER empty --
# even on first run with no database and no cache. At minimum: the dock itself,
# an open, already-mapped place to put the aircraft down. Real spots are added
# later, from the ground, by a human who has looked at the place. Keep the
# coordinates in sync with HOME_LAT / HOME_LON.
SAFE_SPOTS_FALLBACK: tuple[dict[str, object], ...] = (
    {"name": "dock", "lat": HOME_LAT, "lon": HOME_LON, "radius_m": 10.0,
     "surface": "open_ground", "has_marker": True, "height_above_dock_m": 0.0},
)

# --- Logging ---------------------------------------------------------------
LOG_LEVEL: str = _get_str("LOG_LEVEL", "INFO").upper()


def _validate() -> None:
    """Reject nonsensical configuration at import time with a single clear error."""
    errors: list[str] = []

    if not -90.0 <= HOME_LAT <= 90.0:
        errors.append(f"HOME_LAT out of range [-90, 90]: {HOME_LAT}")
    if not -180.0 <= HOME_LON <= 180.0:
        errors.append(f"HOME_LON out of range [-180, 180]: {HOME_LON}")

    for name, value in (
        ("CRUISE_ALT_INBOUND", CRUISE_ALT_INBOUND),
        ("CRUISE_ALT_OUTBOUND", CRUISE_ALT_OUTBOUND),
        ("ON_STATION_ALT", ON_STATION_ALT),
        ("MAX_ALT_M", MAX_ALT_M),
    ):
        if value <= 0:
            errors.append(f"{name} must be positive, got {value}")

    if LATERAL_OFFSET_M < 0:
        errors.append(f"LATERAL_OFFSET_M must not be negative, got {LATERAL_OFFSET_M}")

    for name, value in (
        ("CRUISE_ALT_INBOUND", CRUISE_ALT_INBOUND),
        ("CRUISE_ALT_OUTBOUND", CRUISE_ALT_OUTBOUND),
        ("ON_STATION_ALT", ON_STATION_ALT),
    ):
        if value > MAX_ALT_M:
            errors.append(f"{name} ({value}) exceeds MAX_ALT_M ({MAX_ALT_M})")

    if MAX_RADIUS_M <= 0:
        errors.append(f"MAX_RADIUS_M must be positive, got {MAX_RADIUS_M}")

    for name, value in (
        ("BATTERY_WARN_PCT", BATTERY_WARN_PCT),
        ("BATTERY_FLOOR_PCT", BATTERY_FLOOR_PCT),
    ):
        if not 0.0 <= value <= 100.0:
            errors.append(f"{name} out of range [0, 100]: {value}")
    if BATTERY_FLOOR_PCT >= BATTERY_WARN_PCT:
        errors.append(
            f"BATTERY_FLOOR_PCT ({BATTERY_FLOOR_PCT}) must be below "
            f"BATTERY_WARN_PCT ({BATTERY_WARN_PCT})"
        )

    if RECORDING_KEEP_DAYS <= 0:
        errors.append(f"RECORDING_KEEP_DAYS must be positive, got {RECORDING_KEEP_DAYS}")

    if TELEMETRY_INTERVAL_S <= 0:
        errors.append(f"TELEMETRY_INTERVAL_S must be positive, got {TELEMETRY_INTERVAL_S}")
    if HEARTBEAT_TIMEOUT_S <= 0:
        errors.append(f"HEARTBEAT_TIMEOUT_S must be positive, got {HEARTBEAT_TIMEOUT_S}")

    if RECONNECT_BACKOFF_START_S <= 0:
        errors.append(
            f"RECONNECT_BACKOFF_START_S must be positive, got {RECONNECT_BACKOFF_START_S}"
        )
    if RECONNECT_BACKOFF_CAP_S < RECONNECT_BACKOFF_START_S:
        errors.append(
            f"RECONNECT_BACKOFF_CAP_S ({RECONNECT_BACKOFF_CAP_S}) must be >= "
            f"RECONNECT_BACKOFF_START_S ({RECONNECT_BACKOFF_START_S})"
        )

    for name, value in (
        ("STREAM_RATE_POSITION_HZ", STREAM_RATE_POSITION_HZ),
        ("STREAM_RATE_VFR_HZ", STREAM_RATE_VFR_HZ),
        ("STREAM_RATE_STATUS_HZ", STREAM_RATE_STATUS_HZ),
        ("STREAM_RATE_GPS_HZ", STREAM_RATE_GPS_HZ),
    ):
        if value <= 0:
            errors.append(f"{name} must be positive, got {value}")
        elif value > 50:
            errors.append(f"{name} unreasonably high (>50 Hz), got {value}")

    if STREAM_WATCHDOG_S <= 0:
        errors.append(f"STREAM_WATCHDOG_S must be positive, got {STREAM_WATCHDOG_S}")
    if STREAM_REREQUEST_MIN_S <= 0:
        errors.append(
            f"STREAM_REREQUEST_MIN_S must be positive, got {STREAM_REREQUEST_MIN_S}"
        )

    if LOG_LEVEL not in _VALID_LOG_LEVELS:
        errors.append(
            f"LOG_LEVEL must be one of {sorted(_VALID_LOG_LEVELS)}, got {LOG_LEVEL!r}"
        )

    if not MAVLINK_CONNECTION:
        errors.append("MAVLINK_CONNECTION must not be empty")

    # --- Supabase ---
    if SUPABASE_TELEMETRY_INTERVAL_S <= 0:
        errors.append(
            f"SUPABASE_TELEMETRY_INTERVAL_S must be positive, got {SUPABASE_TELEMETRY_INTERVAL_S}"
        )
    if SUPABASE_HEARTBEAT_WRITE_S < SUPABASE_TELEMETRY_INTERVAL_S:
        errors.append(
            f"SUPABASE_HEARTBEAT_WRITE_S ({SUPABASE_HEARTBEAT_WRITE_S}) must be >= "
            f"SUPABASE_TELEMETRY_INTERVAL_S ({SUPABASE_TELEMETRY_INTERVAL_S})"
        )
    if SUPABASE_QUEUE_MAX < 1:
        errors.append(f"SUPABASE_QUEUE_MAX must be >= 1, got {SUPABASE_QUEUE_MAX}")

    for name, value in (("DRONE_ID", DRONE_ID), ("DOCK_ID", DOCK_ID)):
        if value and not _looks_like_uuid(value):
            errors.append(f"{name} must be a UUID, got {value!r}")

    if SUPABASE_ENABLED:
        # No silent no-persistence mode: refuse to start without credentials.
        if not SUPABASE_URL:
            errors.append(
                "SUPABASE_URL is required when SUPABASE_ENABLED is true "
                "(set SUPABASE_ENABLED=false to run with no persistence)"
            )
        elif not (SUPABASE_URL.startswith("http://") or SUPABASE_URL.startswith("https://")):
            errors.append(f"SUPABASE_URL must be an http(s) URL, got {SUPABASE_URL!r}")
        if not SUPABASE_SERVICE_ROLE_KEY:
            errors.append(
                "SUPABASE_SERVICE_ROLE_KEY is required when SUPABASE_ENABLED is true"
            )
        if not DRONE_ID:
            errors.append("DRONE_ID is required when SUPABASE_ENABLED is true")

    # --- Command intake (v0.3) ---
    for name, value in (
        ("COMMAND_POLL_INTERVAL_S", COMMAND_POLL_INTERVAL_S),
        ("COMMAND_DEFAULT_TTL_S", COMMAND_DEFAULT_TTL_S),
        ("TELEMETRY_MAX_AGE_S", TELEMETRY_MAX_AGE_S),
        ("MISSION_MAX_DURATION_S", MISSION_MAX_DURATION_S),
    ):
        if value <= 0:
            errors.append(f"{name} must be positive, got {value}")

    # --- safety.py (v0.4) ---
    for name, value in (
        ("SAFETY_TICK_HZ", SAFETY_TICK_HZ),
        ("SAFETY_WATCHDOG_S", SAFETY_WATCHDOG_S),
        ("POSITION_MAX_AGE_S", POSITION_MAX_AGE_S),
        ("BATTERY_MAX_AGE_S", BATTERY_MAX_AGE_S),
        ("GPS_MAX_AGE_S", GPS_MAX_AGE_S),
        ("BATTERY_CELL_FLOOR_V", BATTERY_CELL_FLOOR_V),
        ("CRUISE_SPEED_MS", CRUISE_SPEED_MS),
        ("MIN_ALT_M", MIN_ALT_M),
        ("GPS_MAX_HDOP", GPS_MAX_HDOP),
        ("GPS_LOSS_GRACE_S", GPS_LOSS_GRACE_S),
        ("LINK_LOSS_GRACE_S", LINK_LOSS_GRACE_S),
        ("DISCHARGE_WINDOW_S", DISCHARGE_WINDOW_S),
    ):
        if value <= 0:
            errors.append(f"{name} must be positive, got {value}")

    if SAFETY_TICK_HZ > 50:
        errors.append(f"SAFETY_TICK_HZ unreasonably high (>50 Hz), got {SAFETY_TICK_HZ}")
    if SAFETY_WATCHDOG_S <= 1.0 / SAFETY_TICK_HZ:
        errors.append(
            f"SAFETY_WATCHDOG_S ({SAFETY_WATCHDOG_S}) must exceed one tick period "
            f"({1.0 / SAFETY_TICK_HZ:.3f}s at {SAFETY_TICK_HZ} Hz)"
        )
    if BATTERY_CELLS < 1:
        errors.append(f"BATTERY_CELLS must be >= 1, got {BATTERY_CELLS}")
    if not 2.5 <= BATTERY_CELL_FLOOR_V <= 4.2:
        errors.append(
            f"BATTERY_CELL_FLOOR_V ({BATTERY_CELL_FLOOR_V}) is outside a sane "
            f"Li-ion range [2.5, 4.2]"
        )
    for name, value in (
        ("BATTERY_LAUNCH_MIN_PCT", BATTERY_LAUNCH_MIN_PCT),
        ("RTL_RESERVE_PCT", RTL_RESERVE_PCT),
    ):
        if not 0.0 <= value <= 100.0:
            errors.append(f"{name} out of range [0, 100]: {value}")
    if BATTERY_LAUNCH_MIN_PCT <= BATTERY_FLOOR_PCT:
        errors.append(
            f"BATTERY_LAUNCH_MIN_PCT ({BATTERY_LAUNCH_MIN_PCT}) must be above "
            f"BATTERY_FLOOR_PCT ({BATTERY_FLOOR_PCT}): passing through a level "
            f"in flight is not the same as launching at it"
        )
    if GPS_MIN_SATELLITES < 4:
        errors.append(
            f"GPS_MIN_SATELLITES must be >= 4 (a 3D fix needs 4), got {GPS_MIN_SATELLITES}"
        )
    if MIN_ALT_M >= MAX_ALT_M:
        errors.append(
            f"MIN_ALT_M ({MIN_ALT_M}) must be below MAX_ALT_M ({MAX_ALT_M})"
        )

    # --- weather.py (v0.5) ---
    for name, value in (
        ("WEATHER_FETCH_INTERVAL_S", WEATHER_FETCH_INTERVAL_S),
        ("WEATHER_MAX_AGE_S", WEATHER_MAX_AGE_S),
        ("WEATHER_CONFIRM_TIMEOUT_S", WEATHER_CONFIRM_TIMEOUT_S),
        ("WEATHER_WARN_GRACE_S", WEATHER_WARN_GRACE_S),
        ("WIND_MARGINAL_MS", WIND_MARGINAL_MS),
        ("WIND_SEVERE_MS", WIND_SEVERE_MS),
        ("GUST_MARGINAL_MS", GUST_MARGINAL_MS),
        ("GUST_SEVERE_MS", GUST_SEVERE_MS),
        ("VISIBILITY_MIN_M", VISIBILITY_MIN_M),
        ("LIGHTNING_RADIUS_KM", LIGHTNING_RADIUS_KM),
        ("VIBRATION_SEVERE", VIBRATION_SEVERE),
    ):
        if value <= 0:
            errors.append(f"{name} must be positive, got {value}")
    for name, value in (
        ("PRECIP_MARGINAL_MMH", PRECIP_MARGINAL_MMH),
        ("PRECIP_SEVERE_MMH", PRECIP_SEVERE_MMH),
    ):
        if value < 0:
            errors.append(f"{name} must not be negative, got {value}")
    if WIND_MARGINAL_MS >= WIND_SEVERE_MS:
        errors.append(
            f"WIND_MARGINAL_MS ({WIND_MARGINAL_MS}) must be below WIND_SEVERE_MS "
            f"({WIND_SEVERE_MS})"
        )
    if GUST_MARGINAL_MS >= GUST_SEVERE_MS:
        errors.append(
            f"GUST_MARGINAL_MS ({GUST_MARGINAL_MS}) must be below GUST_SEVERE_MS "
            f"({GUST_SEVERE_MS})"
        )
    if PRECIP_MARGINAL_MMH >= PRECIP_SEVERE_MMH:
        errors.append(
            f"PRECIP_MARGINAL_MMH ({PRECIP_MARGINAL_MMH}) must be below "
            f"PRECIP_SEVERE_MMH ({PRECIP_SEVERE_MMH})"
        )
    if TEMP_MIN_C >= TEMP_MAX_C:
        errors.append(
            f"TEMP_MIN_C ({TEMP_MIN_C}) must be below TEMP_MAX_C ({TEMP_MAX_C})"
        )
    if not 0.0 < THROTTLE_MARGIN_MIN_PCT < 100.0:
        errors.append(
            f"THROTTLE_MARGIN_MIN_PCT out of range (0, 100): {THROTTLE_MARGIN_MIN_PCT}"
        )
    if WEATHER_MAX_AGE_S <= WEATHER_FETCH_INTERVAL_S:
        errors.append(
            f"WEATHER_MAX_AGE_S ({WEATHER_MAX_AGE_S}) must exceed "
            f"WEATHER_FETCH_INTERVAL_S ({WEATHER_FETCH_INTERVAL_S}) -- a forecast "
            f"would be stale before the next fetch"
        )
    if WEATHER_ENABLED and not WEATHER_PROVIDER_URL.startswith(("http://", "https://")):
        errors.append(
            f"WEATHER_PROVIDER_URL must be an http(s) URL, got {WEATHER_PROVIDER_URL!r}"
        )
    if not EMERGENCY_MISSION_TYPES:
        errors.append("EMERGENCY_MISSION_TYPES must not be empty")

    # --- safe spots + the unreachable-home divert (v0.5.1) ---
    if not 0.0 < RETURN_BUDGET_IMPOSSIBLE_PCT <= 100.0:
        errors.append(
            f"RETURN_BUDGET_IMPOSSIBLE_PCT out of range (0, 100]: "
            f"{RETURN_BUDGET_IMPOSSIBLE_PCT}"
        )
    for name, value in (
        ("SAFE_SPOT_REFRESH_S", SAFE_SPOT_REFRESH_S),
        ("SAFE_SPOT_MAX_DISTANCE_M", SAFE_SPOT_MAX_DISTANCE_M),
        ("SAFE_SPOT_MIN_GPS_RADIUS_M", SAFE_SPOT_MIN_GPS_RADIUS_M),
        ("SAFE_SPOT_MARKER_PREFERENCE_M", SAFE_SPOT_MARKER_PREFERENCE_M),
    ):
        if value <= 0:
            errors.append(f"{name} must be positive, got {value}")
    if not SAFE_SPOTS_FALLBACK:
        errors.append(
            "SAFE_SPOTS_FALLBACK must contain at least the dock -- the safe-spot "
            "list may never be empty (R1: safety.py must always have an answer)"
        )
    for _spot in SAFE_SPOTS_FALLBACK:
        if not {"name", "lat", "lon"} <= set(_spot):
            errors.append(f"SAFE_SPOTS_FALLBACK row missing name/lat/lon: {_spot}")

    if ALLOW_VEHICLE_CONTROL:
        # Hard stop. There is no vehicle-control executor before v0.5, and no
        # safety.py before v0.4. A future version relaxes this line; until
        # then the GSS must not start with this flag set.
        errors.append(
            "ALLOW_VEHICLE_CONTROL must be false: safety.py (v0.4) and a real "
            "executor (v0.5) do not exist yet. The GSS is dry-run only."
        )

    if errors:
        raise ValueError(
            "Invalid GSS configuration:\n  - " + "\n  - ".join(errors)
        )


_validate()
