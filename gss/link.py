"""MAVLink connection ownership: connect, receive, reconnect forever.

``MavlinkLink`` owns exactly one MAVLink connection. It runs a receive loop on
a background daemon thread, dispatches every received message to registered
callbacks, fires on-connect callbacks after every successful (re)connection,
runs a stream watchdog, and reconnects with exponential backoff. Losing the
link never raises out of the thread and never calls ``sys.exit`` (R5, R8).

SEND BOUNDARY -- amended in v0.1.1, and again in v0.6
----------------------------------------------------
Through v0.5 this module sent exactly ONE category of message: telemetry
stream-rate requests (MAV_CMD_SET_MESSAGE_INTERVAL 511, REQUEST_DATA_STREAM).
Those ask the vehicle to *report* data.

v0.6 adds three things and NOTHING else:

  * :meth:`transmit` -- a single generic passthrough for a flight command,
    gated on ``config.ALLOW_VEHICLE_CONTROL``. Its ONLY caller is
    ``gss.mission.MavlinkExecutor``, which funnels every transmit through its
    own single chokepoint. link.py does not build flight commands, decide
    anything, or retry -- it just serialises the bytes onto the wire.
  * :meth:`read_params` -- PARAM_REQUEST_READ + collecting PARAM_VALUE. A
    READ only. This module still never writes a parameter (no PARAM_SET), and
    ``transmit`` must not be used for one either.
  * :meth:`request_home_position` -- MAV_CMD_GET_HOME_POSITION. Found needed
    against real SITL (2026-09-11): like the stream-rate requests above, this
    ASKS the vehicle to report something it already holds -- it does not
    control anything -- so it sits in the same "request, not command"
    category the stream-rate requests already established, not a widening of
    the boundary. ArduPilot only broadcasts HOME_POSITION when its home
    changes, never to a GCS that merely connects later, so a GSS joining an
    already-running vehicle (restart, or this test) would otherwise wait
    forever. See :meth:`register_watchdog_check`.

Still forbidden here: building an arm / mode / takeoff / goto / RTL message,
MISSION_* upload, PARAM_SET, manual control. Those live in ``gss.mission``.
A future session must not add other sends to this file.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from pymavlink import mavutil

from gss import config

log = logging.getLogger(__name__)

MessageCallback = Callable[[Any], None]
ConnectCallback = Callable[["MavlinkLink"], None]

# How long to sleep between receive polls when no message is waiting. Small
# enough for responsive telemetry, large enough to keep the loop cheap.
_RECV_IDLE_POLL_S = 0.1

_NON_VEHICLE_TYPES = frozenset(
    {
        mavutil.mavlink.MAV_TYPE_GCS,
        mavutil.mavlink.MAV_TYPE_GIMBAL,
        mavutil.mavlink.MAV_TYPE_ADSB,
        mavutil.mavlink.MAV_TYPE_ONBOARD_CONTROLLER,
    }
)


def _connection_closed(conn: Any) -> bool:
    """Best-effort check whether a socket-backed connection has hit EOF.

    pymavlink keeps a dead TCP socket open when ``autoreconnect=False`` and
    spins on it (printing "EOF on TCP socket"), so we detect the half-closed
    socket ourselves and bail promptly. Non-socket transports (serial) return
    False here and are covered by the heartbeat-timeout check instead.
    """
    port = getattr(conn, "port", None)
    if port is None:
        return True
    if not isinstance(port, socket.socket):
        return False
    try:
        was_blocking = port.getblocking()
        port.setblocking(False)
        try:
            peeked = port.recv(1, socket.MSG_PEEK)
        finally:
            port.setblocking(was_blocking)
    except (BlockingIOError, InterruptedError):
        return False
    except OSError:
        return True
    return peeked == b""


class MavlinkLink:
    """Owns one MAVLink connection and keeps it alive.

    Thread-safe. All shared state is guarded by an internal lock. Start the
    background receive thread with :meth:`connect` (which also waits for the
    first heartbeat) and stop it with :meth:`close`.
    """

    def __init__(
        self,
        connection_string: str | None = None,
        heartbeat_timeout_s: float | None = None,
    ) -> None:
        """Create a link. Defaults are taken from :mod:`gss.config`."""
        self._connection_string = connection_string or config.MAVLINK_CONNECTION
        self._heartbeat_timeout_s = (
            heartbeat_timeout_s
            if heartbeat_timeout_s is not None
            else config.HEARTBEAT_TIMEOUT_S
        )

        self._lock = threading.Lock()
        self._tx_lock = threading.Lock()
        self._conn: mavutil.mavfile | None = None
        self._connected = False
        self._last_heartbeat_at: datetime | None = None
        self._last_data_at: datetime | None = None
        self._last_stream_request_mono: float | None = None
        self._system_id: int | None = None
        self._component_id: int | None = None

        # command -> (result, monotonic_time); populated from COMMAND_ACK.
        self._command_acks: dict[int, tuple[int, float]] = {}
        # param name -> value; populated from PARAM_VALUE (v0.6 failsafe audit).
        self._params: dict[str, float] = {}

        self._callbacks: list[MessageCallback] = []
        self._callbacks_lock = threading.Lock()
        self._on_connect: list[ConnectCallback] = []
        self._on_connect_lock = threading.Lock()
        # Extra "is this still missing" predicates the stream watchdog also
        # considers, for a response that arrives once-per-request rather than
        # as a periodic stream (HOME_POSITION) -- see register_watchdog_check.
        self._watchdog_checks: list[tuple[str, Callable[[], bool]]] = []
        self._watchdog_checks_lock = threading.Lock()

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connect_worker: threading.Thread | None = None

    # --- public state --------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """True while the link is up and heartbeats are current."""
        with self._lock:
            return self._connected

    @property
    def last_heartbeat_at(self) -> datetime | None:
        """UTC timestamp of the most recent HEARTBEAT, or None if never seen."""
        with self._lock:
            return self._last_heartbeat_at

    @property
    def last_data_at(self) -> datetime | None:
        """UTC timestamp of the most recent non-HEARTBEAT, non-ACK message.

        i.e. when actual telemetry (position/battery/GPS/...) last arrived, as
        opposed to just a keep-alive heartbeat.
        """
        with self._lock:
            return self._last_data_at

    @property
    def system_id(self) -> int | None:
        """Autopilot MAVLink system ID once detected, else None."""
        with self._lock:
            return self._system_id

    @property
    def component_id(self) -> int | None:
        """Autopilot MAVLink component ID once detected, else None."""
        with self._lock:
            return self._component_id

    def register_message_callback(self, callback: MessageCallback) -> None:
        """Register a callback invoked for every received message.

        Callbacks run on the receive thread and must not block. Exceptions
        raised by a callback are logged and do not stop the loop.
        """
        with self._callbacks_lock:
            self._callbacks.append(callback)

    def register_on_connect(self, callback: ConnectCallback) -> None:
        """Register a callback fired after every successful (re)connection.

        Fires on the first connection AND after every reconnect, and again
        whenever the stream watchdog re-requests. Runs on a dedicated worker
        thread (never the receive thread), so it MAY block briefly -- e.g.
        waiting for a COMMAND_ACK. Exceptions are logged, not propagated.
        """
        with self._on_connect_lock:
            self._on_connect.append(callback)

    def register_watchdog_check(self, name: str, is_missing: Callable[[], bool]) -> None:
        """Register an extra staleness check for the stream watchdog.

        Plain telemetry silence (:meth:`_check_stream_watchdog`'s original
        check) misses a response that only ever arrives once per request --
        HOME_POSITION being the case that surfaced this: every OTHER stream
        keeps flowing fine, so overall telemetry never looks stale, and the
        one message that never came stays missing forever. ``is_missing`` is
        polled from the watchdog (never the receive thread) and, like the
        plain staleness check, is rate-limited by STREAM_REREQUEST_MIN_S and
        re-fires every on-connect callback (not just this one thing) when it
        returns True after STREAM_WATCHDOG_S.
        """
        with self._watchdog_checks_lock:
            self._watchdog_checks.append((name, is_missing))

    # --- lifecycle ---------------------------------------------------------

    def connect(self, timeout_s: float = 30.0) -> bool:
        """Start the receive thread and wait up to ``timeout_s`` for the link.

        Returns True if the first heartbeat arrived in time. Returns False
        otherwise; the background thread keeps retrying forever regardless.
        """
        self.start()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and not self._stop.is_set():
            if self.is_connected:
                return True
            time.sleep(0.1)
        return self.is_connected

    def start(self) -> None:
        """Start the background receive thread if it is not already running."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="mavlink-rx", daemon=True
            )
            self._thread.start()

    def close(self) -> None:
        """Stop the receive thread and close the connection. Safe to call twice."""
        self._stop.set()
        for thread in (self._thread, self._connect_worker):
            if thread is not None and thread.is_alive():
                thread.join(timeout=5.0)
                if thread.is_alive():
                    log.warning("Thread %s did not stop within 5s", thread.name)
        self._close_connection()
        self._mark_down()

    # --- stream requests (the only things this module transmits) -----------

    def request_message_interval(
        self, message_id: int, frequency_hz: float, ack_timeout_s: float = 1.0
    ) -> bool:
        """Ask the vehicle to emit ``message_id`` at ``frequency_hz`` (0 = stop).

        Sends MAV_CMD_SET_MESSAGE_INTERVAL and waits up to ``ack_timeout_s``
        for a COMMAND_ACK. Returns True only if the vehicle ACKed with
        MAV_RESULT_ACCEPTED; False on rejection, timeout, an unsupported
        command, or a dead link. Callers use a False return to fall back to
        REQUEST_DATA_STREAM.
        """
        interval_us = 0.0 if frequency_hz <= 0 else 1_000_000.0 / frequency_hz
        cmd = mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL
        with self._lock:
            conn = self._conn
            tgt_sys = self._system_id or 0
            tgt_comp = self._component_id or 0
            self._command_acks.pop(cmd, None)
        if conn is None:
            return False
        sent = self._send(
            lambda: conn.mav.command_long_send(
                tgt_sys, tgt_comp, cmd, 0,
                float(message_id), interval_us, 0.0, 0.0, 0.0, 0.0, 0.0,
            )
        )
        if not sent:
            return False
        deadline = time.monotonic() + ack_timeout_s
        while time.monotonic() < deadline:
            with self._lock:
                ack = self._command_acks.get(cmd)
            if ack is not None:
                return ack[0] == mavutil.mavlink.MAV_RESULT_ACCEPTED
            if self._stop.wait(0.05):
                return False
        return False

    def request_data_stream(self, stream_id: int, rate_hz: float) -> bool:
        """Send a legacy REQUEST_DATA_STREAM for ``stream_id`` at ``rate_hz``.

        Fire-and-forget (this message has no ACK). Returns False only if the
        link is down or the send failed.
        """
        with self._lock:
            conn = self._conn
            tgt_sys = self._system_id or 0
            tgt_comp = self._component_id or 0
        if conn is None:
            return False
        start = 0 if rate_hz <= 0 else 1
        req_rate = max(1, int(round(rate_hz)))
        return self._send(
            lambda: conn.mav.request_data_stream_send(
                tgt_sys, tgt_comp, stream_id, req_rate, start
            )
        )

    def request_home_position(self) -> bool:
        """Ask the vehicle to (re-)send HOME_POSITION now (MAV_CMD_GET_HOME_
        POSITION). A REQUEST for information the vehicle already holds, in the
        same category as :meth:`request_message_interval` above -- see the
        module docstring. Fire-and-forget like :meth:`request_data_stream`:
        the actual confirmation is the HOME_POSITION message itself arriving
        (handled by whichever callback is watching for it), not an ACK to
        this command.
        """
        cmd = mavutil.mavlink.MAV_CMD_GET_HOME_POSITION
        with self._lock:
            conn = self._conn
            tgt_sys = self._system_id or 0
            tgt_comp = self._component_id or 0
        if conn is None:
            return False
        return self._send(
            lambda: conn.mav.command_long_send(
                tgt_sys, tgt_comp, cmd, 0,
                0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
            )
        )

    # --- flight commands (v0.6) -- ONE generic passthrough, gated ----------

    def transmit(self, build_send: Callable[[Any], None], *, what: str,
                 clear_ack_for: int | None = None) -> bool:
        """Serialise ONE flight-command send onto the wire.

        The only caller is :class:`gss.mission.MavlinkExecutor`, through its own
        single chokepoint. ``build_send`` receives the live connection object
        (``.mav`` / ``.target_system`` / ``.target_component``) and must perform
        exactly one send. Gated on ``config.ALLOW_VEHICLE_CONTROL``: refuses and
        logs otherwise. link.py builds nothing and retries nothing.
        """
        if not config.ALLOW_VEHICLE_CONTROL:
            log.error(
                "link.transmit(%s) refused: ALLOW_VEHICLE_CONTROL is false", what
            )
            return False
        with self._lock:
            conn = self._conn
            if clear_ack_for is not None:
                self._command_acks.pop(clear_ack_for, None)
        if conn is None:
            log.warning("link.transmit(%s): link is down", what)
            return False
        return self._send(lambda: build_send(conn))

    def command_ack(self, command: int) -> tuple[int, float] | None:
        """The most recent COMMAND_ACK for ``command`` as (result, monotonic),
        or None. mission.py uses this to confirm arm / mode changes."""
        with self._lock:
            return self._command_acks.get(command)

    def read_params(
        self, names: "list[str]", timeout_s: float = 5.0
    ) -> dict[str, float | None]:
        """PARAM_REQUEST_READ each of ``names`` and collect the PARAM_VALUE
        replies. A READ only -- never PARAM_SET. Missing params come back None."""
        with self._lock:
            conn = self._conn
            tgt_sys = self._system_id or 0
            tgt_comp = self._component_id or 0
            for n in names:
                self._params.pop(n, None)
        if conn is None:
            return {n: None for n in names}
        for n in names:
            self._send(
                lambda n=n: conn.mav.param_request_read_send(
                    tgt_sys, tgt_comp, n.encode("ascii")[:16], -1
                )
            )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._lock:
                have = {n: self._params.get(n) for n in names}
            if all(v is not None for v in have.values()):
                return have
            if self._stop.wait(0.1):
                break
        with self._lock:
            return {n: self._params.get(n) for n in names}

    def _send(self, send_fn: Callable[[], None]) -> bool:
        """Serialise one transmit through ``_tx_lock``; swallow a dead socket."""
        try:
            with self._tx_lock:
                send_fn()
            return True
        except OSError as exc:
            log.warning("Stream request send failed (%s: %s)", type(exc).__name__, exc)
            return False

    # --- receive thread --------------------------------------------------------

    def _run(self) -> None:
        """Background thread: connect, receive until lost, back off, repeat forever."""
        delay = config.RECONNECT_BACKOFF_START_S
        while not self._stop.is_set():
            try:
                self._open_connection()
            except OSError as exc:
                log.warning(
                    "Link connect to %s failed (%s: %s); retrying in %.0fs",
                    self._connection_string, type(exc).__name__, exc, delay,
                )
                self._after_down()
                if self._stop.wait(delay):
                    break
                delay = min(delay * 2, config.RECONNECT_BACKOFF_CAP_S)
                continue
            except Exception as exc:  # genuinely unexpected -- keep the traceback
                log.error(
                    "Unexpected error opening link (%s: %s); retrying in %.0fs",
                    type(exc).__name__, exc, delay, exc_info=True,
                )
                self._after_down()
                if self._stop.wait(delay):
                    break
                delay = min(delay * 2, config.RECONNECT_BACKOFF_CAP_S)
                continue

            delay = config.RECONNECT_BACKOFF_START_S
            self._launch_connect_callbacks("link up")
            try:
                self._receive_until_lost()
            except OSError as exc:
                # Abrupt disconnect (peer RST / WinError 10054 / broken pipe)
                # is normal operation for a radio link -- one line, no stack.
                log.warning(
                    "Link lost (%s: %s); reconnecting", type(exc).__name__, exc
                )
            except Exception as exc:
                # Anything else is a real bug, not an expected disconnect.
                log.error(
                    "Unexpected receive-loop error (%s: %s); reconnecting",
                    type(exc).__name__, exc, exc_info=True,
                )

            self._after_down()
            if self._stop.is_set():
                break
            log.info("Link down. Reconnecting in %.0fs.", delay)
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, config.RECONNECT_BACKOFF_CAP_S)

        log.debug("Receive thread exiting")

    def _after_down(self) -> None:
        """Common teardown after a failed connect or a lost link."""
        self._mark_down()
        self._close_connection()

    def _open_connection(self) -> None:
        """Open the connection and block for the first vehicle heartbeat.

        Raises OSError if the transport cannot be opened, ConnectionError if
        no usable heartbeat arrives in time.
        """
        log.info("Opening MAVLink connection: %s", self._connection_string)
        # retries=0 -> pymavlink makes exactly one connection attempt and raises
        # on failure, leaving all backoff/retry timing to _run() below.
        conn = mavutil.mavlink_connection(
            self._connection_string,
            autoreconnect=False,
            dialect="ardupilotmega",
            retries=0,
        )
        try:
            heartbeat = self._await_vehicle_heartbeat(conn)
        except BaseException:
            self._safe_close(conn)
            raise

        system_id = heartbeat.get_srcSystem()
        component_id = heartbeat.get_srcComponent()
        # pymavlink locks target_system onto the first heartbeat but leaves
        # target_component at 0. Set both from the heartbeat we actually chose
        # so stream requests are addressed to the real autopilot.
        conn.target_system = system_id
        conn.target_component = component_id

        autopilot = mavutil.mavlink.enums["MAV_AUTOPILOT"].get(heartbeat.autopilot)
        autopilot_name = (
            autopilot.name if autopilot else f"UNKNOWN({heartbeat.autopilot})"
        )
        now = datetime.now(timezone.utc)
        with self._lock:
            self._conn = conn
            self._connected = True
            self._last_heartbeat_at = now
            self._last_data_at = now  # watchdog grace window starts now
            self._last_stream_request_mono = None
            self._system_id = system_id
            self._component_id = component_id
            self._command_acks.clear()
            self._params.clear()
        log.info(
            "Link up. system=%d component=%d autopilot=%s",
            system_id, component_id, autopilot_name,
        )

    def _await_vehicle_heartbeat(self, conn: Any) -> Any:
        """Return the first HEARTBEAT from an actual autopilot (not a GCS)."""
        deadline = time.monotonic() + self._heartbeat_timeout_s
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Cap each blocking call so close() during the handshake is prompt.
            hb = conn.recv_match(
                type="HEARTBEAT", blocking=True, timeout=min(0.5, remaining)
            )
            if hb is None:
                continue
            if hb.get_type() == "BAD_DATA":
                continue
            if hb.autopilot == mavutil.mavlink.MAV_AUTOPILOT_INVALID:
                continue  # a GCS or peripheral, not the vehicle
            if hb.type in _NON_VEHICLE_TYPES:
                continue
            return hb
        raise ConnectionError(
            f"no vehicle HEARTBEAT within {self._heartbeat_timeout_s:.1f}s"
        )

    def _receive_until_lost(self) -> None:
        """Pump messages to callbacks until the link goes stale or stop is set."""
        conn = self._conn
        if conn is None:  # pragma: no cover - _open_connection guarantees this
            raise RuntimeError("receive loop started with no connection")

        while not self._stop.is_set():
            # Non-blocking receive + explicit pacing: pymavlink's blocking mode
            # spins (and floods stdout with "EOF on TCP socket") on a
            # half-closed socket, so we own the poll cadence and the
            # dead-connection check instead.
            message = conn.recv_match(blocking=False)
            if message is None:
                if _connection_closed(conn):
                    log.warning("Connection closed by peer; marking link down")
                    return
                age = self._heartbeat_age_s()
                if age is None or age > self._heartbeat_timeout_s:
                    log.warning(
                        "No HEARTBEAT for %s; marking link down",
                        "ever" if age is None else f"{age:.1f}s",
                    )
                    return
                self._check_stream_watchdog()
                if self._stop.wait(_RECV_IDLE_POLL_S):
                    return
                continue

            message_type = message.get_type()
            if message_type == "BAD_DATA":
                continue

            now = datetime.now(timezone.utc)
            if message_type == "HEARTBEAT":
                with self._lock:
                    self._last_heartbeat_at = now
            elif message_type == "COMMAND_ACK":
                # A COMMAND_ACK is the vehicle answering one of our commands,
                # not telemetry -- it must not reset the stream watchdog.
                with self._lock:
                    self._command_acks[message.command] = (
                        message.result,
                        time.monotonic(),
                    )
            elif message_type == "PARAM_VALUE":
                # Answer to read_params(). A read only -- link.py never sends
                # PARAM_SET. Not telemetry; does not reset the watchdog.
                with self._lock:
                    self._params[str(message.param_id)] = float(message.param_value)
            else:
                with self._lock:
                    self._last_data_at = now

            self._dispatch(message)

    def _dispatch(self, message: Any) -> None:
        """Deliver one message to every registered callback, isolating failures."""
        with self._callbacks_lock:
            callbacks = list(self._callbacks)
        for callback in callbacks:
            try:
                callback(message)
            except Exception:
                name = getattr(callback, "__qualname__", repr(callback))
                log.exception(
                    "Message callback %s raised on %s", name, message.get_type()
                )

    # --- on-connect callbacks + stream watchdog ---------------------------

    def _launch_connect_callbacks(self, reason: str) -> None:
        """Run the on-connect callbacks on a fresh worker thread (once at a time)."""
        with self._on_connect_lock:
            if not self._on_connect:
                return
        with self._lock:
            if self._connect_worker is not None and self._connect_worker.is_alive():
                return
            self._last_stream_request_mono = time.monotonic()
            worker = threading.Thread(
                target=self._run_connect_callbacks,
                args=(reason,),
                name="mavlink-onconnect",
                daemon=True,
            )
            self._connect_worker = worker
        worker.start()

    def _run_connect_callbacks(self, reason: str) -> None:
        """Worker body: invoke every on-connect callback with this link."""
        with self._on_connect_lock:
            callbacks = list(self._on_connect)
        log.debug("Firing %d on-connect callback(s) [%s]", len(callbacks), reason)
        for callback in callbacks:
            if self._stop.is_set() or not self.is_connected:
                return
            try:
                callback(self)
            except Exception:
                name = getattr(callback, "__qualname__", repr(callback))
                log.exception("on-connect callback %s raised [%s]", name, reason)

    def _check_stream_watchdog(self) -> None:
        """Re-request streams if the link is up but telemetry has gone silent,
        or if a registered once-per-request check (see
        :meth:`register_watchdog_check`) is still missing after the same
        STREAM_WATCHDOG_S window.

        HEARTBEAT keeps arriving on a healthy link even when ArduPilot has
        stopped streaming position/battery, so plain silence is the only
        thing that catches THAT failure mode -- but a registered check is
        needed for the opposite shape of failure: every periodic stream flows
        fine (so telemetry never looks stale) while one once-only response
        never arrived. Both paths are rate-limited by STREAM_REREQUEST_MIN_S,
        sharing the same timer, and both re-fire every on-connect callback
        (cheap and idempotent -- re-requesting an already-flowing stream is a
        no-op) rather than something more targeted.
        """
        with self._lock:
            last_data = self._last_data_at
            last_request = self._last_stream_request_mono
        if last_data is None:
            return
        data_age = (datetime.now(timezone.utc) - last_data).total_seconds()
        stale = data_age >= config.STREAM_WATCHDOG_S

        with self._watchdog_checks_lock:
            checks = list(self._watchdog_checks)
        missing: list[str] = []
        for name, is_missing in checks:
            try:
                if is_missing():
                    missing.append(name)
            except Exception:
                log.exception("watchdog check %r raised", name)

        if not stale and not missing:
            return
        mono = time.monotonic()
        if (
            last_request is not None
            and mono - last_request < config.STREAM_REREQUEST_MIN_S
        ):
            return
        if stale:
            log.warning(
                "Stream watchdog: no telemetry for %.1fs on a live link; "
                "re-requesting streams",
                data_age,
            )
        if missing:
            log.warning(
                "Stream watchdog: still missing %s after %.0fs on a live "
                "link; re-requesting",
                ", ".join(missing), config.STREAM_WATCHDOG_S,
            )
        self._launch_connect_callbacks("stream watchdog")

    # --- helpers ---------------------------------------------------------

    def _heartbeat_age_s(self) -> float | None:
        """Seconds since the last heartbeat, or None if none has been received."""
        with self._lock:
            last = self._last_heartbeat_at
        if last is None:
            return None
        return (datetime.now(timezone.utc) - last).total_seconds()

    def _mark_down(self) -> None:
        """Mark the link as not connected."""
        with self._lock:
            self._connected = False

    def _close_connection(self) -> None:
        """Close and drop the underlying connection if present."""
        with self._lock:
            conn = self._conn
            self._conn = None
        self._safe_close(conn)

    def _safe_close(self, conn: Any) -> None:
        """Close a connection object, logging (not raising) any error."""
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            log.exception("Error while closing MAVLink connection")
