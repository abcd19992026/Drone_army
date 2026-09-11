"""In-process fake ArduPilot vehicle for testing the GSS without SITL.

Serves MAVLink over TCP. It reproduces the ArduPilot behaviour that bit us in
v0.1: it sends **only HEARTBEAT** until the client explicitly asks for data
streams (via MAV_CMD_SET_MESSAGE_INTERVAL or REQUEST_DATA_STREAM). Once asked,
it streams the five telemetry messages the GSS maps.

Every value the GSS reads is driveable from a test, and both a clean
(FIN) and an abrupt (RST) disconnect can be forced. A ``heartbeat_only`` mode
keeps sending HEARTBEAT while ignoring stream requests, to exercise the GSS
stream watchdog.

Library use::

    fv = FakeVehicle(port=5799)
    fv.start()
    fv.wait_for_client(timeout=5)
    fv.set_battery(pct=42, voltage_v=22.2, current_a=8.1)
    fv.set_position(lat=25.5932, lon=85.2045, fix_type=3, satellites=12)
    fv.set_armed(True)
    ...
    fv.disconnect_abrupt()   # RST
    fv.stop()

Script use::

    python -m tests.fake_vehicle --port 5799
    python -m tests.fake_vehicle --port 5799 --heartbeat-only
"""

from __future__ import annotations

import argparse
import logging
import math
import socket
import struct
import threading
import time
from typing import Any

from pymavlink import mavutil

log = logging.getLogger("tests.fake_vehicle")

_DEFAULT_PORT = 5799  # deliberately not 5762, so it never collides with SITL

# MAVLink message ids the GSS asks for / we stream.
_MSG_GLOBAL_POSITION_INT = mavutil.mavlink.MAVLINK_MSG_ID_GLOBAL_POSITION_INT
_MSG_VFR_HUD = mavutil.mavlink.MAVLINK_MSG_ID_VFR_HUD
_MSG_SYS_STATUS = mavutil.mavlink.MAVLINK_MSG_ID_SYS_STATUS
_MSG_BATTERY_STATUS = mavutil.mavlink.MAVLINK_MSG_ID_BATTERY_STATUS
_MSG_GPS_RAW_INT = mavutil.mavlink.MAVLINK_MSG_ID_GPS_RAW_INT
_MSG_WIND = getattr(mavutil.mavlink, "MAVLINK_MSG_ID_WIND", 168)
_MSG_VIBRATION = getattr(mavutil.mavlink, "MAVLINK_MSG_ID_VIBRATION", 241)
_MSG_EKF_STATUS = getattr(mavutil.mavlink, "MAVLINK_MSG_ID_EKF_STATUS_REPORT", 193)
_STREAMABLE = (
    _MSG_GLOBAL_POSITION_INT,
    _MSG_VFR_HUD,
    _MSG_SYS_STATUS,
    _MSG_BATTERY_STATUS,
    _MSG_GPS_RAW_INT,
    _MSG_WIND,
    _MSG_VIBRATION,
    _MSG_EKF_STATUS,
)

# --- v0.6 flight simulation -------------------------------------------------
# A kinematic (not physics) simulator: mode changes, arming, takeoff, GUIDED
# position targets, RTL and LAND all move the vehicle and report back through
# ordinary telemetry, so gss/mission.py can be exercised end-to-end without
# real ArduPilot SITL. Off by default (``enable_flight_sim()``) -- every
# existing test that never calls it sees byte-for-byte the same fake vehicle
# as before.
_CMD_SET_MODE = mavutil.mavlink.MAV_CMD_DO_SET_MODE
_CMD_ARM = mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM
_CMD_TAKEOFF = mavutil.mavlink.MAV_CMD_NAV_TAKEOFF
_CMD_GET_HOME_POSITION = mavutil.mavlink.MAV_CMD_GET_HOME_POSITION
_MODE_GUIDED, _MODE_LOITER, _MODE_RTL, _MODE_LAND, _MODE_BRAKE = 4, 5, 6, 9, 17
_EARTH_RADIUS_M = 6_371_000.0
_SIM_CLIMB_MS = 3.0
_SIM_DESCEND_MS = 1.5
_SIM_GROUND_SPEED_MS = 9.0
_SIM_LAND_ALT_M = 0.15
_SIM_ARRIVE_M = 0.3
_PREARM_RESEND_S = 1.0


def _sim_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


def _sim_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _sim_step_toward(lat: float, lon: float, tlat: float, tlon: float, step_m: float):
    dist = _sim_distance_m(lat, lon, tlat, tlon)
    if step_m <= 0 or dist <= step_m or dist < 1e-9:
        return tlat, tlon
    brg = math.radians(_sim_bearing_deg(lat, lon, tlat, tlon))
    d = step_m / _EARTH_RADIUS_M
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(brg))
    l2 = l1 + math.atan2(
        math.sin(brg) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2)
    )
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


def _param_id_str(value: Any) -> str:
    """PARAM_REQUEST_READ's param_id arrives as either bytes or str, always
    null-padded to 16 chars. Normalise to a clean ASCII name."""
    if isinstance(value, bytes):
        return value.split(b"\x00", 1)[0].decode("ascii", "replace")
    return str(value).split("\x00", 1)[0]


class FakeVehicle:
    """A minimal MAVLink vehicle served over TCP, with test-driveable state."""

    def __init__(
        self,
        port: int = _DEFAULT_PORT,
        host: str = "127.0.0.1",
        system_id: int = 1,
        component_id: int = mavutil.mavlink.MAV_COMP_ID_AUTOPILOT1,
        heartbeat_hz: float = 2.0,
        stream_hz: float = 5.0,
    ) -> None:
        """Create (but do not start) a fake vehicle listening on ``host:port``."""
        self._addr = (host, int(port))
        self._system_id = system_id
        self._component_id = component_id
        self._heartbeat_period = 1.0 / heartbeat_hz
        self._stream_period = 1.0 / stream_hz

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn: mavutil.mavfile | None = None
        self._client_seen = threading.Event()
        self._force_disconnect: str | None = None

        # --- streaming control ---
        # Strict, like ArduPilot with SET_MESSAGE_INTERVAL: a message is only
        # streamed once it has been individually requested. REQUEST_DATA_STREAM
        # (the legacy fallback) flips _stream_all on instead.
        self._requested_ids: set[int] = set()
        self._stream_all = False
        self._heartbeat_only = False
        # Individually silenced message ids: HEARTBEAT and every other stream
        # keep flowing, these do not. Reproduces one MAVLink message type going
        # silent on an otherwise healthy link -- common on a real radio, and the
        # case where the system looks fine and is not.
        self._suppressed_ids: set[int] = set()

        # --- driveable vehicle state ---
        self._lat_deg = 0.0
        self._lon_deg = 0.0
        self._alt_amsl_m = 584.0
        self._rel_alt_m = 0.0
        self._heading_deg = 90.0
        self._groundspeed_ms = 0.0
        self._fix_type = mavutil.mavlink.GPS_FIX_TYPE_NO_FIX  # 0
        self._satellites = 0
        self._battery_pct = 100
        self._battery_mv = 12600
        self._battery_ca = 0
        self._armed = False
        self._custom_mode = 0  # copter: 0 == STABILIZE
        # v0.5 observed conditions
        self._wind_speed_ms = 0.0
        self._wind_dir_deg = 0.0
        self._throttle_pct = 0
        self._climb_ms = 0.0
        self._vib = (0.0, 0.0, 0.0)
        self._clip = (0, 0, 0)

        # --- v0.6 flight simulation state (all inert until enable_flight_sim) --
        self._flight_sim = False
        self._home: tuple[float, float] | None = None
        self._pos_target: tuple[float, float, float] | None = None
        self._arm_result = mavutil.mavlink.MAV_RESULT_ACCEPTED
        self._mode_change_accept = True
        self._mode_change_ignored = False
        self._ekf_healthy = True
        self._home_set_reported = False
        self._prearm_text: str | None = None
        self._last_prearm_send = 0.0
        self._last_sim_mono = 0.0
        self._params: dict[str, float] = {
            "BATT_LOW_VOLT": 19.5, "BATT_FS_LOW_ACT": 2, "FS_GCS_ENABLE": 1,
            "FENCE_ENABLE": 1, "FENCE_RADIUS": 6000.0, "FENCE_ALT_MAX": 60.0,
            "RTL_ALT": 4500.0,
        }
        self.arm_command_count = 0
        self.disarm_command_count = 0
        self.mode_change_requests: list[int] = []
        self._sim_ground_speed = _SIM_GROUND_SPEED_MS
        self._sim_climb = _SIM_CLIMB_MS
        self._sim_descend = _SIM_DESCEND_MS

    # ------------------------------------------------------------------ API

    def start(self) -> None:
        """Start the server thread."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="fake-vehicle", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """Stop the server and close the socket."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._close(getattr(self, "_conn", None))

    def wait_for_client(self, timeout: float = 5.0) -> bool:
        """Block until a client has connected (or ``timeout`` elapses)."""
        return self._client_seen.wait(timeout)

    def set_position(
        self,
        lat: float | None = None,
        lon: float | None = None,
        alt_m: float | None = None,
        rel_alt_m: float | None = None,
        fix_type: int | None = None,
        satellites: int | None = None,
        heading_deg: float | None = None,
        groundspeed_ms: float | None = None,
    ) -> None:
        """Update any subset of the position / GPS / motion state."""
        with self._lock:
            if lat is not None:
                self._lat_deg = lat
            if lon is not None:
                self._lon_deg = lon
            if alt_m is not None:
                self._alt_amsl_m = alt_m
            if rel_alt_m is not None:
                self._rel_alt_m = rel_alt_m
            if fix_type is not None:
                self._fix_type = int(fix_type)
            if satellites is not None:
                self._satellites = int(satellites)
            if heading_deg is not None:
                self._heading_deg = heading_deg
            if groundspeed_ms is not None:
                self._groundspeed_ms = groundspeed_ms

    def set_fix_type(self, fix_type: int) -> None:
        """Set the GPS fix type (0 = none, 2 = 2D, 3 = 3D, ...)."""
        with self._lock:
            self._fix_type = int(fix_type)

    def set_battery(
        self,
        pct: float | None = None,
        voltage_v: float | None = None,
        current_a: float | None = None,
    ) -> None:
        """Update battery percentage / voltage / current."""
        with self._lock:
            if pct is not None:
                self._battery_pct = int(pct)
            if voltage_v is not None:
                self._battery_mv = int(round(voltage_v * 1000))
            if current_a is not None:
                self._battery_ca = int(round(current_a * 100))

    def set_armed(self, armed: bool) -> None:
        """Set the armed state reported in HEARTBEAT."""
        with self._lock:
            self._armed = bool(armed)

    def set_mode(self, custom_mode: int) -> None:
        """Set the copter custom flight-mode number (0 = STABILIZE, 4 = GUIDED...)."""
        with self._lock:
            self._custom_mode = int(custom_mode)

    def set_heartbeat_only(self, value: bool = True) -> None:
        """When True, keep sending HEARTBEAT but never stream, even if asked."""
        with self._lock:
            self._heartbeat_only = bool(value)

    def set_wind(
        self, speed_ms: float | None = None, direction_deg: float | None = None
    ) -> None:
        """Set the EKF wind estimate reported in WIND (direction the wind is
        coming FROM)."""
        with self._lock:
            if speed_ms is not None:
                self._wind_speed_ms = float(speed_ms)
            if direction_deg is not None:
                self._wind_dir_deg = float(direction_deg) % 360.0

    def set_throttle(self, pct: float | None = None, climb_ms: float | None = None) -> None:
        """Set VFR_HUD throttle (0-100) and/or climb rate."""
        with self._lock:
            if pct is not None:
                self._throttle_pct = int(round(pct))
            if climb_ms is not None:
                self._climb_ms = float(climb_ms)

    def set_vibration(
        self,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        clip: tuple[int, int, int] | None = None,
    ) -> None:
        """Set the VIBRATION levels and, optionally, the clipping counts."""
        with self._lock:
            vx, vy, vz = self._vib
            self._vib = (
                float(x) if x is not None else vx,
                float(y) if y is not None else vy,
                float(z) if z is not None else vz,
            )
            if clip is not None:
                self._clip = (int(clip[0]), int(clip[1]), int(clip[2]))

    # ---------------------------------------------------- v0.6 flight sim API

    def enable_flight_sim(self) -> None:
        """Turn on the kinematic flight simulator: mode changes, arming,
        takeoff, GUIDED position targets, RTL and LAND all move the vehicle."""
        with self._lock:
            self._flight_sim = True
            if self._home is None:
                self._home = (self._lat_deg, self._lon_deg)
            self._ekf_healthy = True

    def set_home(self, lat: float, lon: float) -> None:
        """Where RTL flies back to, and what HOME_POSITION reports."""
        with self._lock:
            self._home = (lat, lon)

    def set_param(self, name: str, value: float) -> None:
        """Drive a failsafe/fence parameter the audit reads (item 13)."""
        with self._lock:
            self._params[name] = float(value)

    def get_param(self, name: str) -> float | None:
        with self._lock:
            return self._params.get(name)

    def reject_arm(self, result: int | None = None) -> None:
        """The next (and every subsequent) arm request is DENIED, like a real
        ArduPilot pre-arm-check failure. ``result`` defaults to MAV_RESULT_DENIED."""
        with self._lock:
            self._arm_result = (
                result if result is not None else mavutil.mavlink.MAV_RESULT_DENIED
            )

    def allow_arm(self) -> None:
        with self._lock:
            self._arm_result = mavutil.mavlink.MAV_RESULT_ACCEPTED

    def set_mode_change_response(self, accept: bool) -> None:
        """ACK-level refusal: COMMAND_ACK for DO_SET_MODE comes back DENIED."""
        with self._lock:
            self._mode_change_accept = bool(accept)

    def set_mode_change_ignored(self, ignored: bool) -> None:
        """The ACCEPTED-level lie: COMMAND_ACK says ACCEPTED but the mode in
        HEARTBEAT never actually changes -- exercises the CONFIRMATION path,
        not just the ACK path ("never assume a command took effect")."""
        with self._lock:
            self._mode_change_ignored = bool(ignored)

    def set_ekf_healthy(self, healthy: bool) -> None:
        with self._lock:
            self._ekf_healthy = bool(healthy)

    def set_prearm_fail(self, text: str) -> None:
        """Start sending an ArduPilot-style 'PreArm: ...' STATUSTEXT."""
        with self._lock:
            self._prearm_text = text
            self._last_prearm_send = 0.0

    def clear_prearm_fail(self) -> None:
        with self._lock:
            self._prearm_text = None

    def set_sim_rates(
        self, *, ground_speed_ms: float | None = None,
        climb_ms: float | None = None, descend_ms: float | None = None,
    ) -> None:
        """Speed up (or slow down) the kinematic simulator -- tests use this to
        avoid waiting real-world minutes for a multi-km real flight."""
        with self._lock:
            if ground_speed_ms is not None:
                self._sim_ground_speed = float(ground_speed_ms)
            if climb_ms is not None:
                self._sim_climb = float(climb_ms)
            if descend_ms is not None:
                self._sim_descend = float(descend_ms)

    def suppress_message(self, message_id: int) -> None:
        """Stop streaming just this MAVLink message id. HEARTBEAT and every
        other requested stream keep flowing normally."""
        with self._lock:
            self._suppressed_ids.add(int(message_id))

    def resume_message(self, message_id: int) -> None:
        """Undo :meth:`suppress_message` for this id."""
        with self._lock:
            self._suppressed_ids.discard(int(message_id))

    @property
    def suppressed_message_ids(self) -> set[int]:
        with self._lock:
            return set(self._suppressed_ids)

    def disconnect_clean(self) -> None:
        """Force a normal TCP close (FIN) of the current client connection."""
        with self._lock:
            self._force_disconnect = "clean"

    def disconnect_abrupt(self) -> None:
        """Force an abrupt RST close (SO_LINGER 0) of the current client."""
        with self._lock:
            self._force_disconnect = "abrupt"

    @property
    def requested_message_ids(self) -> set[int]:
        """Message ids the client has asked for via SET_MESSAGE_INTERVAL."""
        with self._lock:
            return set(self._requested_ids)

    @property
    def is_streaming(self) -> bool:
        """True once a stream request has been honoured."""
        with self._lock:
            if self._heartbeat_only:
                return False
            return self._stream_all or bool(self._requested_ids)

    def _wants(self, message_id: int) -> bool:
        with self._lock:
            if self._heartbeat_only:
                return False
            if message_id in self._suppressed_ids:
                return False
            return self._stream_all or message_id in self._requested_ids

    # -------------------------------------------------------------- server

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn = mavutil.mavlink_connection(
                    "tcpin:%s:%d" % self._addr,
                    dialect="ardupilotmega",
                    source_system=self._system_id,
                    source_component=self._component_id,
                )
            except OSError as exc:
                log.warning("fake vehicle: bind failed (%s); retrying", exc)
                if self._stop.wait(0.5):
                    return
                continue
            self._conn = conn
            log.info("fake vehicle listening on %s:%d", *self._addr)
            try:
                self._serve(conn)
            except Exception:
                log.exception("fake vehicle serve loop crashed; restarting")
            finally:
                self._close(conn)
                self._conn = None
        log.debug("fake vehicle thread exiting")

    def _serve(self, conn: mavutil.mavfile) -> None:
        last_hb = 0.0
        last_stream = 0.0
        last_sim = time.monotonic()
        had_client = False
        while not self._stop.is_set():
            # honour a forced-disconnect request
            with self._lock:
                fd, self._force_disconnect = self._force_disconnect, None
            if fd is not None and conn.port is not None:
                self._drop_client(conn, abrupt=(fd == "abrupt"))
                self._client_seen.clear()
                had_client = False
                continue

            # Drain ALL pending incoming messages this iteration, not just one:
            # the GSS sends its stream requests back-to-back and each waits ~1s
            # for a COMMAND_ACK, so a one-per-20ms drain rate can time a request
            # out under load.
            msgs = []
            for _ in range(32):
                try:
                    m = conn.recv_match(blocking=False)
                except OSError:
                    m = None
                if m is None:
                    break
                msgs.append(m)

            if conn.port is not None and not had_client:
                had_client = True
                self._client_seen.set()
                log.info("fake vehicle: client connected")
            elif conn.port is None and had_client:
                had_client = False
                self._client_seen.clear()
                log.info("fake vehicle: client disconnected")

            for msg in msgs:
                if msg.get_type() != "BAD_DATA":
                    self._handle_incoming(conn, msg)

            now = time.monotonic()
            dt = now - last_sim
            last_sim = now
            if self._flight_sim:
                self._sim_tick(dt)
            if conn.port is not None:
                if now - last_hb >= self._heartbeat_period:
                    self._send_heartbeat(conn)
                    last_hb = now
                if self.is_streaming and now - last_stream >= self._stream_period:
                    self._send_telemetry(conn)
                    last_stream = now
                self._send_prearm_statustext(conn)

            time.sleep(0.02)

    def _handle_incoming(self, conn: mavutil.mavfile, msg: Any) -> None:
        msg_type = msg.get_type()
        if msg_type == "COMMAND_LONG" and int(msg.command) == mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL:
            message_id = int(round(msg.param1))
            interval_us = msg.param2
            with self._lock:
                if interval_us <= 0:
                    self._requested_ids.discard(message_id)
                else:
                    self._requested_ids.add(message_id)
            # The vehicle acknowledges the command even in heartbeat-only mode:
            # it received it, it just will not act on it.
            self._safe_send(
                conn,
                lambda: conn.mav.command_ack_send(
                    mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
                    mavutil.mavlink.MAV_RESULT_ACCEPTED,
                ),
            )
        elif msg_type == "REQUEST_DATA_STREAM":
            with self._lock:
                self._stream_all = bool(msg.start_stop)
        elif msg_type == "COMMAND_LONG" and int(msg.command) == _CMD_SET_MODE:
            mode_id = int(round(msg.param2))
            with self._lock:
                self.mode_change_requests.append(mode_id)
                accept = self._mode_change_accept
                if accept and not self._mode_change_ignored:
                    self._custom_mode = mode_id
            self._safe_send(conn, lambda: conn.mav.command_ack_send(
                _CMD_SET_MODE,
                mavutil.mavlink.MAV_RESULT_ACCEPTED if accept
                else mavutil.mavlink.MAV_RESULT_DENIED,
            ))
        elif msg_type == "COMMAND_LONG" and int(msg.command) == _CMD_ARM:
            arm = msg.param1 > 0.5
            with self._lock:
                if arm:
                    self.arm_command_count += 1
                    result = self._arm_result
                    if result == mavutil.mavlink.MAV_RESULT_ACCEPTED:
                        self._armed = True
                else:
                    self.disarm_command_count += 1
                    result = mavutil.mavlink.MAV_RESULT_ACCEPTED
                    self._armed = False
            self._safe_send(conn, lambda: conn.mav.command_ack_send(_CMD_ARM, result))
        elif msg_type == "COMMAND_LONG" and int(msg.command) == _CMD_TAKEOFF:
            alt = float(msg.param7)
            with self._lock:
                self._pos_target = (self._lat_deg, self._lon_deg, alt)
            self._safe_send(conn, lambda: conn.mav.command_ack_send(
                _CMD_TAKEOFF, mavutil.mavlink.MAV_RESULT_ACCEPTED))
        elif msg_type == "SET_POSITION_TARGET_GLOBAL_INT":
            with self._lock:
                self._pos_target = (msg.lat_int / 1e7, msg.lon_int / 1e7, float(msg.alt))
        elif msg_type == "PARAM_REQUEST_READ":
            name = _param_id_str(msg.param_id)
            with self._lock:
                value = self._params.get(name)
            if value is not None:
                self._safe_send(conn, lambda: conn.mav.param_value_send(
                    name.encode("ascii")[:16], float(value),
                    mavutil.mavlink.MAV_PARAM_TYPE_REAL32, 0, 0))
        elif msg_type == "PARAM_REQUEST_LIST":
            with self._lock:
                items = list(self._params.items())
            for i, (name, value) in enumerate(items):
                self._safe_send(conn, lambda n=name, v=value, i=i: conn.mav.param_value_send(
                    n.encode("ascii")[:16], float(v),
                    mavutil.mavlink.MAV_PARAM_TYPE_REAL32, len(items), i))
        elif msg_type == "COMMAND_LONG" and int(msg.command) == _CMD_GET_HOME_POSITION:
            # Real ArduPilot answers this REGARDLESS of any "flight sim" mode
            # (found against real SITL, v0.6) -- it only ever broadcasts
            # HOME_POSITION unprompted when its home CHANGES, never to a GCS
            # that merely connects later, so telemetry.py must ask. Answering
            # this unconditionally (not gated on self._flight_sim, unlike the
            # periodic auto-send in _send_telemetry above) is what makes this
            # double as a regression test for that behaviour on every fake-
            # vehicle-backed suite, not just the flight-sim ones.
            with self._lock:
                if self._home is None:
                    self._home = (self._lat_deg, self._lon_deg)
                home = self._home
            self._safe_send(conn, lambda: conn.mav.home_position_send(
                int(round(home[0] * 1e7)), int(round(home[1] * 1e7)), 0,
                0.0, 0.0, 0.0, [1.0, 0.0, 0.0, 0.0], 0.0, 0.0, 0.0))
            self._safe_send(conn, lambda: conn.mav.command_ack_send(
                _CMD_GET_HOME_POSITION, mavutil.mavlink.MAV_RESULT_ACCEPTED))

    # -------------------------------------------------------- flight sim tick

    def _sim_tick(self, dt: float) -> None:
        """Kinematic step: mode changes, arming, takeoff, GUIDED position
        targets, RTL and LAND all move the vehicle. Not physics -- a straight
        line at a constant rate -- but enough to exercise mission.py's
        confirm-every-step state machine end to end."""
        if dt <= 0 or dt > 2.0:  # a stall (breakpoint, GC pause) -- skip this tick
            return
        with self._lock:
            if not self._armed:
                return
            mode = self._custom_mode
            lat, lon, alt = self._lat_deg, self._lon_deg, self._rel_alt_m
            home = self._home
            target = self._pos_target
            speed, climb, descend = self._sim_ground_speed, self._sim_climb, self._sim_descend

        if mode == _MODE_RTL:
            hlat, hlon = home if home is not None else (lat, lon)
            dist = _sim_distance_m(lat, lon, hlat, hlon)
            if dist > _SIM_ARRIVE_M:
                lat, lon = _sim_step_toward(lat, lon, hlat, hlon, speed * dt)
            else:
                alt = max(0.0, alt - descend * dt)
        elif mode == _MODE_LAND:
            alt = max(0.0, alt - descend * dt)
        elif target is not None:
            tlat, tlon, talt = target
            dist = _sim_distance_m(lat, lon, tlat, tlon)
            if dist > _SIM_ARRIVE_M:
                lat, lon = _sim_step_toward(lat, lon, tlat, tlon, speed * dt)
            if alt < talt - 0.05:
                alt = min(talt, alt + climb * dt)
            elif alt > talt + 0.05:
                alt = max(talt, alt - descend * dt)

        newly_landed = alt <= _SIM_LAND_ALT_M and mode in (_MODE_RTL, _MODE_LAND)
        with self._lock:
            self._lat_deg, self._lon_deg, self._rel_alt_m = lat, lon, (0.0 if newly_landed else alt)
            if newly_landed:
                self._armed = False
                self._custom_mode = mode  # ArduPilot leaves the mode as-is on disarm
                self._pos_target = None

    # ----------------------------------------------------------- send msgs

    def _send_heartbeat(self, conn: mavutil.mavfile) -> None:
        with self._lock:
            base_mode = mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
            if self._armed:
                base_mode |= mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
            custom_mode = self._custom_mode
            state = (
                mavutil.mavlink.MAV_STATE_ACTIVE
                if self._armed
                else mavutil.mavlink.MAV_STATE_STANDBY
            )
        self._safe_send(
            conn,
            lambda: conn.mav.heartbeat_send(
                mavutil.mavlink.MAV_TYPE_QUADROTOR,
                mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
                base_mode,
                custom_mode,
                state,
            ),
        )

    def _send_telemetry(self, conn: mavutil.mavfile) -> None:
        with self._lock:
            lat = int(round(self._lat_deg * 1e7))
            lon = int(round(self._lon_deg * 1e7))
            alt_mm = int(round(self._alt_amsl_m * 1000))
            rel_mm = int(round(self._rel_alt_m * 1000))
            hdg_cdeg = int(round(self._heading_deg * 100)) % 36000
            gs = float(self._groundspeed_ms)
            fix = int(self._fix_type)
            sats = int(self._satellites)
            pct = int(self._battery_pct)
            mv = int(self._battery_mv)
            ca = int(self._battery_ca)
            wind_speed = float(self._wind_speed_ms)
            wind_dir = float(self._wind_dir_deg)
            throttle = int(self._throttle_pct)
            climb = float(self._climb_ms)
            vib = self._vib
            clip = self._clip
        mav = conn.mav
        now_ms = int(time.time() * 1000) & 0xFFFFFFFF
        now_us = int(time.time() * 1e6) & 0xFFFFFFFFFFFFFFFF
        voltages = [mv] + [65535] * 9
        alt_amsl_m = alt_mm / 1000.0

        if self._wants(_MSG_GLOBAL_POSITION_INT):
            self._safe_send(conn, lambda: mav.global_position_int_send(
                now_ms, lat, lon, alt_mm, rel_mm, 0, 0, 0, hdg_cdeg))
        if self._wants(_MSG_VFR_HUD):
            self._safe_send(conn, lambda: mav.vfr_hud_send(
                gs, gs, int(hdg_cdeg / 100), throttle, alt_amsl_m, climb))
        if self._wants(_MSG_SYS_STATUS):
            self._safe_send(conn, lambda: mav.sys_status_send(
                0, 0, 0, 0, mv, ca, pct, 0, 0, 0, 0, 0, 0))
        if self._wants(_MSG_BATTERY_STATUS):
            self._safe_send(conn, lambda: mav.battery_status_send(
                0, 0, 0, 32767, voltages, ca, 0, 0, pct))
        if self._wants(_MSG_GPS_RAW_INT):
            self._safe_send(conn, lambda: mav.gps_raw_int_send(
                now_us, fix, lat, lon, alt_mm, 121, 200, 0, 0, sats))
        if self._wants(_MSG_WIND):
            # WIND: direction wind comes FROM (deg), horizontal speed, vertical.
            self._safe_send(conn, lambda: mav.wind_send(wind_dir, wind_speed, 0.0))
        if self._wants(_MSG_VIBRATION):
            self._safe_send(conn, lambda: mav.vibration_send(
                now_us, vib[0], vib[1], vib[2], clip[0], clip[1], clip[2]))

        with self._lock:
            flight_sim = self._flight_sim
            ekf_healthy = self._ekf_healthy
            home = self._home
            home_reported = self._home_set_reported
        if flight_sim and self._wants(_MSG_EKF_STATUS):
            flags = 0
            if ekf_healthy:
                flags = (
                    getattr(mavutil.mavlink, "EKF_ATTITUDE", 1)
                    | getattr(mavutil.mavlink, "EKF_VELOCITY_HORIZ", 2)
                    | getattr(mavutil.mavlink, "EKF_VELOCITY_VERT", 4)
                    | getattr(mavutil.mavlink, "EKF_POS_HORIZ_REL", 4)
                    | getattr(mavutil.mavlink, "EKF_POS_HORIZ_ABS", 8)
                    | getattr(mavutil.mavlink, "EKF_POS_VERT_ABS", 16)
                    | getattr(mavutil.mavlink, "EKF_PRED_POS_HORIZ_REL", 128)
                )
            self._safe_send(conn, lambda: mav.ekf_status_report_send(
                flags, 0.1, 0.1, 0.1, 0.1, 0.0))
        if flight_sim and home is not None:
            self._safe_send(conn, lambda: mav.home_position_send(
                int(round(home[0] * 1e7)), int(round(home[1] * 1e7)), 0,
                0.0, 0.0, 0.0, [1.0, 0.0, 0.0, 0.0], 0.0, 0.0, 0.0))
            if not home_reported:
                with self._lock:
                    self._home_set_reported = True

    def _send_prearm_statustext(self, conn: mavutil.mavfile) -> None:
        with self._lock:
            text = self._prearm_text
            due = self._flight_sim and text and (
                time.monotonic() - self._last_prearm_send >= _PREARM_RESEND_S
            )
            if due:
                self._last_prearm_send = time.monotonic()
        if due:
            self._safe_send(conn, lambda: conn.mav.statustext_send(
                mavutil.mavlink.MAV_SEVERITY_CRITICAL, text.encode("ascii")[:50]))

    def _safe_send(self, conn: mavutil.mavfile, send_fn: Any) -> None:
        try:
            send_fn()
        except OSError as exc:
            log.debug("fake vehicle: send failed (%s)", exc)

    # ------------------------------------------------------------- cleanup

    def _drop_client(self, conn: mavutil.mavfile, abrupt: bool) -> None:
        sock = conn.port
        if sock is None:
            return
        try:
            if abrupt:
                sock.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                )
                log.info("fake vehicle: forcing ABRUPT (RST) disconnect")
            else:
                log.info("fake vehicle: forcing clean (FIN) disconnect")
            sock.close()
        except OSError as exc:
            log.debug("fake vehicle: drop_client error (%s)", exc)
        finally:
            # Return pymavlink to the listening state for the next client.
            conn.port = None
            conn.fd = conn.listen.fileno()

    def _close(self, conn: mavutil.mavfile | None) -> None:
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            log.debug("fake vehicle: error closing connection", exc_info=True)


def _main() -> int:
    parser = argparse.ArgumentParser(description="Fake ArduPilot vehicle over TCP")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--heartbeat-only",
        action="store_true",
        help="send HEARTBEAT only, ignore stream requests (tests the watchdog)",
    )
    parser.add_argument("--lat", type=float, default=25.5932)
    parser.add_argument("--lon", type=float, default=85.2045)
    parser.add_argument("--fix-type", type=int, default=3)
    parser.add_argument("--satellites", type=int, default=12)
    parser.add_argument("--battery-pct", type=float, default=100.0)
    parser.add_argument("--battery-voltage", type=float, default=12.6)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    fv = FakeVehicle(port=args.port, host=args.host)
    fv.set_position(
        lat=args.lat, lon=args.lon,
        fix_type=args.fix_type, satellites=args.satellites,
    )
    fv.set_battery(pct=args.battery_pct, voltage_v=args.battery_voltage)
    if args.heartbeat_only:
        fv.set_heartbeat_only(True)
    fv.start()
    log.info(
        "fake vehicle running on %s:%d (%s). Ctrl+C to stop.",
        args.host, args.port,
        "HEARTBEAT-ONLY" if args.heartbeat_only else "normal",
    )
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        fv.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
