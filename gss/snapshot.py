"""The immutable telemetry snapshot type, kept in its own tiny module.

``TelemetrySnapshot`` lived in :mod:`gss.telemetry` through v0.3. It moved here
in v0.4 for one reason: :mod:`gss.safety` must be able to read the drone's
state without importing anything that touches a socket or the network (rule
**R1**). ``gss.telemetry`` imports ``gss.link`` which imports ``socket`` and
pymavlink; this module imports only the standard library's ``dataclasses`` and
``datetime``. ``gss.telemetry`` re-exports ``TelemetrySnapshot`` from here, so
every existing ``from gss.telemetry import TelemetrySnapshot`` keeps working.

Design rules (unchanged from v0.1.1):
  * A field that has not been received yet is ``None``, never ``0``.
  * ``position_valid`` is the single flag navigation decisions gate on. As of
    v0.4 it means: real lat/lon AND a 3D GPS fix AND enough satellites AND the
    position report is not stale (see :mod:`gss.safety` and
    ``config.POSITION_MAX_AGE_S``).
  * The per-field ``*_age_s`` values exist because one global "telemetry age"
    hides a partial stream failure: if VFR_HUD keeps arriving while
    GLOBAL_POSITION_INT stops, the link looks healthy while the position
    silently goes stale. safety.py checks each stream's age on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TelemetrySnapshot:
    """Immutable point-in-time view of the drone's state.

    Every optional field is ``None`` until the corresponding MAVLink message
    has been received at least once. The trailing ``*_age_s`` / ``gps_hdop``
    fields default to ``None`` so code that builds a snapshot by hand (older
    tests) keeps working; the live :class:`~gss.telemetry.TelemetryReader`
    always fills them in.
    """

    timestamp: datetime
    connected: bool
    lat: float | None
    lon: float | None
    position_valid: bool
    alt_m_amsl: float | None
    alt_m_relative: float | None
    heading_deg: float | None
    groundspeed_ms: float | None
    mode: str | None
    armed: bool | None
    battery_pct: float | None
    battery_voltage_v: float | None
    battery_current_a: float | None
    gps_fix_type: int | None
    gps_satellites: int | None
    link_age_s: float | None       # seconds since the last HEARTBEAT
    telemetry_age_s: float | None  # seconds since the last real telemetry message

    # --- per-field staleness (v0.4) -------------------------------------------
    # Seconds since the last message that carries this field. ``None`` means
    # "never received". safety.py treats a stale field exactly as it treats a
    # missing one (rule R12: fail safe).
    position_age_s: float | None = None   # since the last GLOBAL_POSITION_INT
    battery_age_s: float | None = None    # since the last SYS_STATUS/BATTERY_STATUS
    attitude_age_s: float | None = None   # since the last VFR_HUD
    gps_age_s: float | None = None        # since the last GPS_RAW_INT

    # Horizontal dilution of precision from GPS_RAW_INT (eph / 100). ``None``
    # when the vehicle reports it as unknown (UINT16_MAX).
    gps_hdop: float | None = None

    # --- observed conditions (v0.5) -----------------------------------------
    # Measured by the aircraft, not forecast. This is the ONLY weather data an
    # in-flight decision may use (rule R1: no internet while airborne), and it
    # is better data anyway -- a forecast is a guess about a 10 km square, the
    # drone is measuring the air it is actually in.
    wind_speed_ms: float | None = None          # ArduPilot EKF wind estimate
    wind_direction_deg: float | None = None     # direction the wind blows FROM
    throttle_pct: float | None = None           # VFR_HUD throttle, 0-100
    climb_rate_ms: float | None = None          # VFR_HUD climb (+ up)
    vibration_x: float | None = None
    vibration_y: float | None = None
    vibration_z: float | None = None
    vibration_clip_x: int | None = None
    vibration_clip_y: int | None = None
    vibration_clip_z: int | None = None
    wind_age_s: float | None = None             # since the last WIND / WIND_COV
    vibration_age_s: float | None = None        # since the last VIBRATION

    # --- arming / flight readiness (v0.6) ----------------------------------
    # mission.py's pre-arm gate reads these. ``None`` = not reported yet, and a
    # gate treats "not reported" as "not ready" (fail safe).
    ekf_healthy: bool | None = None              # EKF_STATUS_REPORT flags OK
    home_set: bool | None = None                 # HOME_POSITION received
    prearm_fail_text: str | None = None          # most recent "PreArm: ..." STATUSTEXT, if fresh


@dataclass(frozen=True)
class SafeSpot:
    """A place the drone can put itself down that is NOT its dock (v0.5.1).

    Loaded from the ``safe_spots`` table (or ``config.SAFE_SPOTS_FALLBACK``) by
    :mod:`gss.safe_spots` and handed to the pure safety core as plain data --
    :mod:`gss.safety` never reads the database (rule **R1**). ``surface`` is
    advisory: ``open_ground`` | ``terrace`` | ``field`` | ``rooftop``.

    Two tiers, and the divert selection respects the difference:

      * ``has_marker`` True  -- an ArUco pad the vision pipeline can land on
        accurately; a small ``radius_m`` is fine.
      * ``has_marker`` False -- GPS only. Selection ignores a small
        ``radius_m`` claim (needs ``config.SAFE_SPOT_MIN_GPS_RADIUS_M`` of
        genuinely clear ground) and refuses a non-zero ``height_above_dock_m``
        outright (GPS cannot put an aircraft on a raised surface it cannot see).

    ``height_above_dock_m`` is relative to the home point, because that is what
    the aircraft measures altitude against.
    """

    name: str
    lat: float
    lon: float
    radius_m: float = 10.0
    surface: str = "open_ground"
    has_marker: bool = False
    height_above_dock_m: float = 0.0
    marker_id: str | None = None
    approach_bearing_deg: float | None = None
    hazards: str | None = None
