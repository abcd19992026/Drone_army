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
