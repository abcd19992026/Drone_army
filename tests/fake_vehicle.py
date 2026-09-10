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
_STREAMABLE = (
    _MSG_GLOBAL_POSITION_INT,
    _MSG_VFR_HUD,
    _MSG_SYS_STATUS,
    _MSG_BATTERY_STATUS,
    _MSG_GPS_RAW_INT,
)


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

            try:
                msg = conn.recv_match(blocking=False)
            except OSError:
                msg = None

            if conn.port is not None and not had_client:
                had_client = True
                self._client_seen.set()
                log.info("fake vehicle: client connected")
            elif conn.port is None and had_client:
                had_client = False
                self._client_seen.clear()
                log.info("fake vehicle: client disconnected")

            if msg is not None and msg.get_type() != "BAD_DATA":
                self._handle_incoming(conn, msg)

            now = time.monotonic()
            if conn.port is not None:
                if now - last_hb >= self._heartbeat_period:
                    self._send_heartbeat(conn)
                    last_hb = now
                if self.is_streaming and now - last_stream >= self._stream_period:
                    self._send_telemetry(conn)
                    last_stream = now

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
                gs, gs, int(hdg_cdeg / 100), 0, alt_amsl_m, 0.0))
        if self._wants(_MSG_SYS_STATUS):
            self._safe_send(conn, lambda: mav.sys_status_send(
                0, 0, 0, 0, mv, ca, pct, 0, 0, 0, 0, 0, 0))
        if self._wants(_MSG_BATTERY_STATUS):
            self._safe_send(conn, lambda: mav.battery_status_send(
                0, 0, 0, 32767, voltages, ca, 0, 0, pct))
        if self._wants(_MSG_GPS_RAW_INT):
            self._safe_send(conn, lambda: mav.gps_raw_int_send(
                now_us, fix, lat, lon, alt_mm, 121, 200, 0, 0, sats))

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
