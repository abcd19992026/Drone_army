"""Read MAVLink messages into a single latest-known telemetry snapshot.

``TelemetryReader`` subscribes to :class:`~gss.link.MavlinkLink` and keeps the
most recent value seen for each field.

It owns two things:
  * the mapping from MAVLink messages to snapshot fields, and
  * the decision of WHICH messages to ask the vehicle for, at WHAT rate --
    sent through :class:`MavlinkLink` on every (re)connection.

Design rules:
  * A field that has not been received yet is ``None``, never ``0``. A missing
    value and a real zero are different things and a later safety module must
    tell them apart.
  * ``lat``/``lon`` of exactly 0/0 is ArduPilot's "no position estimate", not
    a real fix -- stored as ``None``.
  * ``position_valid`` is True only with real coordinates AND a 3D GPS fix.
    v0.4+ gates navigation decisions on this flag.
  * Battery *voltage* is captured alongside percentage on purpose -- percentage
    on a Li-ion pack reads optimistically under load, so voltage will serve as
    an independent floor.
"""

from __future__ import annotations

import logging
import math
import threading
from datetime import datetime, timezone
from typing import Any

from pymavlink import mavutil

from gss import config
from gss.link import MavlinkLink
from gss.snapshot import TelemetrySnapshot

__all__ = ["TelemetryReader", "TelemetrySnapshot", "format_console_line"]

log = logging.getLogger(__name__)

_UINT16_MAX = 65535
_UINT8_MAX = 255
_GPS_FIX_3D = mavutil.mavlink.GPS_FIX_TYPE_3D_FIX  # 3

# (human name, MAVLink message id, requested rate in Hz). TelemetryReader owns
# this list; link.py just transmits the requests.
_MSG_WIND = getattr(mavutil.mavlink, "MAVLINK_MSG_ID_WIND", 168)
_MSG_VIBRATION = getattr(mavutil.mavlink, "MAVLINK_MSG_ID_VIBRATION", 241)
_MSG_EKF_STATUS = getattr(mavutil.mavlink, "MAVLINK_MSG_ID_EKF_STATUS_REPORT", 193)

# EKF_STATUS_FLAGS bits (defaults match the ArduPilot dialect).
_EKF_ATTITUDE = getattr(mavutil.mavlink, "EKF_ATTITUDE", 1)
_EKF_VELOCITY_HORIZ = getattr(mavutil.mavlink, "EKF_VELOCITY_HORIZ", 2)
_EKF_POS_HORIZ_ABS = getattr(mavutil.mavlink, "EKF_POS_HORIZ_ABS", 8)
_EKF_CONST_POS_MODE = getattr(mavutil.mavlink, "EKF_CONST_POS_MODE", 16)
_PREARM_FRESH_S = 6.0  # a "PreArm:" complaint older than this is treated as cleared

_STREAM_PLAN: tuple[tuple[str, int, float], ...] = (
    ("GLOBAL_POSITION_INT", mavutil.mavlink.MAVLINK_MSG_ID_GLOBAL_POSITION_INT, config.STREAM_RATE_POSITION_HZ),
    ("VFR_HUD", mavutil.mavlink.MAVLINK_MSG_ID_VFR_HUD, config.STREAM_RATE_VFR_HZ),
    ("SYS_STATUS", mavutil.mavlink.MAVLINK_MSG_ID_SYS_STATUS, config.STREAM_RATE_STATUS_HZ),
    ("BATTERY_STATUS", mavutil.mavlink.MAVLINK_MSG_ID_BATTERY_STATUS, config.STREAM_RATE_STATUS_HZ),
    ("GPS_RAW_INT", mavutil.mavlink.MAVLINK_MSG_ID_GPS_RAW_INT, config.STREAM_RATE_GPS_HZ),
    # v0.5: the aircraft's own conditions sensing. WIND is ArduPilot's EKF wind
    # estimate; VIBRATION carries accel vibration levels + clipping counts.
    ("WIND", _MSG_WIND, config.STREAM_RATE_STATUS_HZ),
    ("VIBRATION", _MSG_VIBRATION, config.STREAM_RATE_STATUS_HZ),
    # v0.6: the arming-gate signals. EKF health, and (via STATUSTEXT, which is
    # unsolicited) ArduPilot's own pre-arm complaints.
    ("EKF_STATUS_REPORT", _MSG_EKF_STATUS, config.STREAM_RATE_STATUS_HZ),
)

# Legacy fallback: (stream group, rate) covering the same messages. WIND and
# VIBRATION both ride the EXTRA1/EXTRA3 groups on ArduPilot; EXTENDED_STATUS is
# the safest single legacy bucket that ArduPilot maps them into.
_STREAM_GROUPS_FALLBACK: tuple[tuple[int, float], ...] = (
    (mavutil.mavlink.MAV_DATA_STREAM_POSITION, config.STREAM_RATE_POSITION_HZ),
    (mavutil.mavlink.MAV_DATA_STREAM_EXTRA2, config.STREAM_RATE_VFR_HZ),
    (mavutil.mavlink.MAV_DATA_STREAM_EXTRA1, config.STREAM_RATE_STATUS_HZ),
    (mavutil.mavlink.MAV_DATA_STREAM_EXTRA3, config.STREAM_RATE_STATUS_HZ),
    (
        mavutil.mavlink.MAV_DATA_STREAM_EXTENDED_STATUS,
        max(config.STREAM_RATE_STATUS_HZ, config.STREAM_RATE_GPS_HZ),
    ),
)


# TelemetrySnapshot now lives in gss/snapshot.py (imported above and re-exported)
# so safety.py can depend on the type without importing a socket. See that
# module's docstring.


class TelemetryReader:
    """Consumes messages from a :class:`MavlinkLink` and holds the latest values."""

    def __init__(self, link: MavlinkLink) -> None:
        """Register with ``link``; the reader starts collecting immediately."""
        self._link = link
        self._lock = threading.Lock()

        self._lat: float | None = None
        self._lon: float | None = None
        self._alt_m_amsl: float | None = None
        self._alt_m_relative: float | None = None
        self._heading_deg: float | None = None
        self._groundspeed_ms: float | None = None
        self._mode: str | None = None
        self._armed: bool | None = None
        self._battery_pct: float | None = None
        self._battery_voltage_v: float | None = None
        self._battery_current_a: float | None = None
        self._gps_fix_type: int | None = None
        self._gps_satellites: int | None = None
        self._gps_hdop: float | None = None

        # v0.6 arming-gate signals
        self._ekf_healthy: bool | None = None
        self._home_set: bool | None = None
        self._prearm_fail_text: str | None = None
        self._ekf_at: datetime | None = None
        self._prearm_at: datetime | None = None

        # v0.5 observed conditions
        self._wind_speed_ms: float | None = None
        self._wind_direction_deg: float | None = None
        self._throttle_pct: float | None = None
        self._climb_rate_ms: float | None = None
        self._vibration_x: float | None = None
        self._vibration_y: float | None = None
        self._vibration_z: float | None = None
        self._vibration_clip_x: int | None = None
        self._vibration_clip_y: int | None = None
        self._vibration_clip_z: int | None = None

        # Per-field arrival times (UTC). A partial stream failure -- one message
        # stops while the rest keep coming -- is invisible in a single global
        # age, so safety.py checks each of these. ``None`` until first seen.
        self._position_at: datetime | None = None
        self._battery_at: datetime | None = None
        self._attitude_at: datetime | None = None
        self._gps_at: datetime | None = None
        self._wind_at: datetime | None = None
        self._vibration_at: datetime | None = None

        # BATTERY_STATUS is richer than SYS_STATUS; once we have seen it, stop
        # letting SYS_STATUS overwrite the battery fields.
        self._battery_status_seen = False

        link.register_message_callback(self._on_message)
        link.register_on_connect(self._request_streams)
        # HOME_POSITION only ever arrives once, on request (real ArduPilot
        # never re-broadcasts it to a GCS that connects after the home was
        # already set -- found against real SITL, v0.6): register it with the
        # watchdog too, or a GSS that (re)connects to an already-running
        # vehicle stays stuck at home_set=None forever even though every
        # other stream is flowing perfectly healthily.
        link.register_watchdog_check("home position", lambda: self._home_set is not True)

    # --- stream requests (decides what to ask for; link.py transmits) ------

    def _request_streams(self, link: MavlinkLink) -> None:
        """on-connect callback: ask the vehicle to stream the messages we map,
        and (independently of the streams below) ask for HOME_POSITION once.

        Runs on link's connect-worker thread, never the RX thread, so it may
        block briefly waiting for command ACKs. Tries
        MAV_CMD_SET_MESSAGE_INTERVAL first; if the vehicle does not accept
        every request, also sends the legacy REQUEST_DATA_STREAM groups.

        HOME_POSITION is a REQUEST, not a flight command (same "ask the
        vehicle to report something" category as the stream requests below --
        R11's amended boundary) -- sent unconditionally here, ahead of the
        stream-interval branching, since it has nothing to do with which
        stream path the vehicle accepts. Found needed against real SITL
        (v0.6): ArduPilot never re-broadcasts its home position to a GCS that
        merely connects after the home was already set, so a GSS that
        (re)connects to an already-running vehicle -- the restart-recovery
        case -- would otherwise never see ``home_set`` become True.
        """
        link.request_home_position()
        accepted = 0
        for name, message_id, rate_hz in _STREAM_PLAN:
            # 2s per ACK: the requests are serialised (one MAV_CMD id, one ACK
            # at a time) and the vehicle is already streaming by the time the
            # later ones go out, so 1s was tight under load.
            if link.request_message_interval(message_id, rate_hz, ack_timeout_s=2.0):
                accepted += 1
            else:
                log.debug("%s not ACKed via SET_MESSAGE_INTERVAL", name)

        if accepted == len(_STREAM_PLAN):
            log.info(
                "Telemetry streams requested via SET_MESSAGE_INTERVAL (%d/%d messages)",
                accepted, len(_STREAM_PLAN),
            )
            return

        log.warning(
            "SET_MESSAGE_INTERVAL accepted %d/%d requests; "
            "falling back to REQUEST_DATA_STREAM",
            accepted, len(_STREAM_PLAN),
        )
        for stream_id, rate_hz in _STREAM_GROUPS_FALLBACK:
            link.request_data_stream(stream_id, rate_hz)
        log.info(
            "Telemetry streams requested via REQUEST_DATA_STREAM (%d groups)",
            len(_STREAM_GROUPS_FALLBACK),
        )

    # --- message mapping --------------------------------------------------

    def _on_message(self, message: Any) -> None:
        """Map one MAVLink message onto the stored fields. Runs on the RX thread."""
        message_type = message.get_type()
        now = datetime.now(timezone.utc)

        if message_type == "GLOBAL_POSITION_INT":
            raw_lat, raw_lon = message.lat, message.lon
            with self._lock:
                self._position_at = now
                if raw_lat == 0 and raw_lon == 0:
                    # ArduPilot sends 0/0 when it has no position estimate.
                    self._lat = None
                    self._lon = None
                else:
                    self._lat = raw_lat / 1e7
                    self._lon = raw_lon / 1e7
                # 0 is a legitimate altitude/heading -- do not zero-guard these.
                self._alt_m_amsl = message.alt / 1000.0
                self._alt_m_relative = message.relative_alt / 1000.0
                self._heading_deg = (
                    message.hdg / 100.0 if message.hdg != _UINT16_MAX else None
                )

        elif message_type == "VFR_HUD":
            with self._lock:
                self._attitude_at = now
                self._groundspeed_ms = float(message.groundspeed)
                # throttle 0-100; climb + up. 0 is legitimate for both.
                self._throttle_pct = float(message.throttle)
                self._climb_rate_ms = float(message.climb)

        elif message_type == "HEARTBEAT":
            # Ignore heartbeats from non-autopilot components (e.g. a GCS).
            if message.autopilot == mavutil.mavlink.MAV_AUTOPILOT_INVALID:
                return
            mode = mavutil.mode_string_v10(message)
            armed = bool(
                message.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
            )
            with self._lock:
                self._mode = mode
                self._armed = armed

        elif message_type == "SYS_STATUS":
            with self._lock:
                if not self._battery_status_seen:
                    self._battery_at = now
                    self._battery_voltage_v = _voltage_mv_to_v(message.voltage_battery)
                    self._battery_current_a = _current_ca_to_a(message.current_battery)
                    self._battery_pct = _percent(message.battery_remaining)

        elif message_type == "BATTERY_STATUS":
            first_cell = message.voltages[0] if message.voltages else _UINT16_MAX
            with self._lock:
                self._battery_status_seen = True
                self._battery_at = now
                self._battery_voltage_v = _voltage_mv_to_v(first_cell)
                self._battery_current_a = _current_ca_to_a(message.current_battery)
                self._battery_pct = _percent(message.battery_remaining)

        elif message_type == "GPS_RAW_INT":
            with self._lock:
                self._gps_at = now
                self._gps_fix_type = int(message.fix_type)
                self._gps_satellites = (
                    int(message.satellites_visible)
                    if message.satellites_visible != _UINT8_MAX
                    else None
                )
                eph = getattr(message, "eph", _UINT16_MAX)
                self._gps_hdop = None if eph in (0, _UINT16_MAX) else eph / 100.0

        elif message_type == "WIND":
            # Legacy ArduPilot WIND: direction the wind blows FROM (deg),
            # horizontal speed (m/s). speed < 0 means "no estimate".
            speed = float(message.speed)
            with self._lock:
                self._wind_at = now
                if speed < 0:
                    self._wind_speed_ms = None
                    self._wind_direction_deg = None
                else:
                    self._wind_speed_ms = speed
                    self._wind_direction_deg = float(message.direction) % 360.0

        elif message_type == "WIND_COV":
            wx, wy = float(message.wind_x), float(message.wind_y)
            with self._lock:
                self._wind_at = now
                self._wind_speed_ms = math.hypot(wx, wy)
                # wind_x/y is the vector the wind travels along (NED); the
                # "from" bearing is the opposite direction.
                self._wind_direction_deg = math.degrees(math.atan2(-wy, -wx)) % 360.0

        elif message_type == "EKF_STATUS_REPORT":
            flags = int(getattr(message, "flags", 0))
            healthy = (
                bool(flags & _EKF_ATTITUDE)
                and bool(flags & _EKF_VELOCITY_HORIZ)
                and bool(flags & _EKF_POS_HORIZ_ABS)
                and not (flags & _EKF_CONST_POS_MODE)
            )
            with self._lock:
                self._ekf_at = now
                self._ekf_healthy = healthy

        elif message_type == "HOME_POSITION":
            with self._lock:
                self._home_set = True

        elif message_type == "STATUSTEXT":
            text = str(getattr(message, "text", "") or "").strip()
            if text.lower().startswith("prearm"):
                with self._lock:
                    self._prearm_at = now
                    self._prearm_fail_text = text

        elif message_type == "VIBRATION":
            with self._lock:
                self._vibration_at = now
                self._vibration_x = float(message.vibration_x)
                self._vibration_y = float(message.vibration_y)
                self._vibration_z = float(message.vibration_z)
                self._vibration_clip_x = int(message.clipping_0)
                self._vibration_clip_y = int(message.clipping_1)
                self._vibration_clip_z = int(message.clipping_2)

    def get_snapshot(self) -> TelemetrySnapshot:
        """Return the current immutable :class:`TelemetrySnapshot`."""
        now = datetime.now(timezone.utc)
        last_heartbeat = self._link.last_heartbeat_at
        link_age_s = (
            (now - last_heartbeat).total_seconds()
            if last_heartbeat is not None
            else None
        )
        last_data = self._link.last_data_at
        telemetry_age_s = (
            (now - last_data).total_seconds() if last_data is not None else None
        )
        def _age(stamp: datetime | None) -> float | None:
            return (now - stamp).total_seconds() if stamp is not None else None

        with self._lock:
            fix = self._gps_fix_type
            sats = self._gps_satellites
            position_age_s = _age(self._position_at)
            # position_valid (v0.4): real coords AND a 3D fix AND enough
            # satellites AND a report that is not stale. A 30-second-old
            # position with position_valid still True is exactly the failure
            # safety.py must never see.
            position_valid = (
                self._lat is not None
                and self._lon is not None
                and fix is not None
                and fix >= _GPS_FIX_3D
                and sats is not None
                and sats >= config.GPS_MIN_SATELLITES
                and position_age_s is not None
                and position_age_s <= config.POSITION_MAX_AGE_S
            )
            return TelemetrySnapshot(
                timestamp=now,
                connected=self._link.is_connected,
                lat=self._lat,
                lon=self._lon,
                position_valid=position_valid,
                alt_m_amsl=self._alt_m_amsl,
                alt_m_relative=self._alt_m_relative,
                heading_deg=self._heading_deg,
                groundspeed_ms=self._groundspeed_ms,
                mode=self._mode,
                armed=self._armed,
                battery_pct=self._battery_pct,
                battery_voltage_v=self._battery_voltage_v,
                battery_current_a=self._battery_current_a,
                gps_fix_type=fix,
                gps_satellites=sats,
                link_age_s=link_age_s,
                telemetry_age_s=telemetry_age_s,
                position_age_s=position_age_s,
                battery_age_s=_age(self._battery_at),
                attitude_age_s=_age(self._attitude_at),
                gps_age_s=_age(self._gps_at),
                gps_hdop=self._gps_hdop,
                wind_speed_ms=self._wind_speed_ms,
                wind_direction_deg=self._wind_direction_deg,
                throttle_pct=self._throttle_pct,
                climb_rate_ms=self._climb_rate_ms,
                vibration_x=self._vibration_x,
                vibration_y=self._vibration_y,
                vibration_z=self._vibration_z,
                vibration_clip_x=self._vibration_clip_x,
                vibration_clip_y=self._vibration_clip_y,
                vibration_clip_z=self._vibration_clip_z,
                wind_age_s=_age(self._wind_at),
                vibration_age_s=_age(self._vibration_at),
                ekf_healthy=(
                    self._ekf_healthy
                    if (_age(self._ekf_at) is not None
                        and _age(self._ekf_at) <= config.GPS_MAX_AGE_S)
                    else None
                ),
                home_set=self._home_set,
                prearm_fail_text=(
                    self._prearm_fail_text
                    if (_age(self._prearm_at) is not None
                        and _age(self._prearm_at) <= _PREARM_FRESH_S)
                    else None
                ),
            )


def _voltage_mv_to_v(millivolts: int) -> float | None:
    """Convert a MAVLink millivolt reading to volts; 0 / UINT16_MAX mean unknown."""
    if millivolts in (0, _UINT16_MAX):
        return None
    return millivolts / 1000.0


def _current_ca_to_a(centiamps: int) -> float | None:
    """Convert a MAVLink centiamp reading to amps; -1 means unknown."""
    if centiamps == -1:
        return None
    return centiamps / 100.0


def _percent(value: int) -> float | None:
    """Return a battery-remaining percentage; -1 means unknown."""
    if value == -1:
        return None
    return float(value)


def _fmt(value: float | int | None, spec: str, na: str = "--") -> str:
    """Format ``value`` with ``spec``, or return ``na`` right-padded to width."""
    if value is None:
        width = 0
        for ch in spec:
            if ch.isdigit():
                width = width * 10 + int(ch)
            elif ch == ".":
                break
        return na.rjust(width) if width else na
    return format(value, spec)


def format_console_line(snapshot: TelemetrySnapshot) -> str:
    """Render a snapshot as one readable console line."""
    stamp = snapshot.timestamp.astimezone().strftime("%H:%M:%S")

    if not snapshot.connected:
        age = _fmt(snapshot.link_age_s, ".0f", na="?")
        return f"[{stamp}] LINK DOWN, reconnecting (last heartbeat {age}s ago)"

    armed = "----" if snapshot.armed is None else ("ARMED" if snapshot.armed else "disarm")
    pos_label = "POS " if snapshot.position_valid else "pos?"
    return (
        f"[{stamp}] "
        f"{(snapshot.mode or '----'):>9} {armed:>6} "
        f"alt {_fmt(snapshot.alt_m_relative, '6.1f')}m rel "
        f"spd {_fmt(snapshot.groundspeed_ms, '5.1f')}m/s "
        f"hdg {_fmt(snapshot.heading_deg, '5.1f')} "
        f"{pos_label}{_fmt(snapshot.lat, '10.6f')},{_fmt(snapshot.lon, '10.6f')} "
        f"bat {_fmt(snapshot.battery_pct, '3.0f')}% "
        f"{_fmt(snapshot.battery_voltage_v, '5.2f')}V "
        f"{_fmt(snapshot.battery_current_a, '5.1f')}A "
        f"gps fix{_fmt(snapshot.gps_fix_type, '1d')}/{_fmt(snapshot.gps_satellites, '2d')}sat "
        f"link {_fmt(snapshot.link_age_s, '4.1f')}s"
    )
