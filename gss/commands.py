"""Command intake: from a row in the ``commands`` table to a decision.

v0.3 does NOT fly the drone. It learns about commands, claims them
atomically, validates them, and -- for accepted flight commands -- creates a
mission and hands it to an :class:`~gss.executor.Executor`. The only executor
in v0.3 is the dry runner, which transmits nothing (rule **R11** -- the
MAVLink send boundary in ``link.py`` does not widen).

Two independent discovery paths, both mandatory:

  * **Realtime** (fast path) -- a websocket subscription to INSERTs on
    ``commands``. Websockets drop and silently miss events across the gap.
  * **Poller** (correct path) -- queries for ``status='pending'`` every
    ``COMMAND_POLL_INTERVAL_S``. Guarantees eventual delivery.

Exactly-once is guaranteed by the atomic claim (``claim_command`` RPC):
whichever path calls it first wins, the other gets nothing and drops it.

**The clock**: expiry is evaluated against the database's ``now()`` (returned
by the claim RPC as ``server_now``), never ``time.time()``. A Raspberry Pi
has no battery-backed clock; after a power cut it believes it is some time in
the past until NTP corrects it. ``time.monotonic`` is used only for loop
pacing and mission-duration limits (a duration, not a wall-clock decision).

**Boundary**: these checks are intake sanity, NOT the safety system.
``safety.py`` (v0.4) will independently and continuously re-check geofence,
battery and altitude during flight, and its veto is final. Nobody may later
delete ``safety.py``'s checks on the grounds that ``commands.py`` already
validated.
"""

from __future__ import annotations

import collections
import json
import logging
import math
import queue
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from gss import config
from gss.executor import (
    PHASE_EVENTS,
    TERMINAL_PHASES,
    DryRunExecutor,
    Executor,
    MissionPhase,
    make_executor,
)
from gss import config, weather
from gss.safety import SafetyAction, SafetyContext, SafetyMonitor
from gss.store import TelemetryStore
from gss.telemetry import TelemetrySnapshot
from gss.weather import WeatherAction, WeatherTier
from gss.weather_feed import WeatherMonitor

log = logging.getLogger(__name__)

# In-flight verdicts the supervisor drives the executor on.
_SAFETY_ACTIONABLE = frozenset(
    {
        SafetyAction.HOLD,
        SafetyAction.DESCEND,
        SafetyAction.RTL_NOW,
        SafetyAction.DIVERT,
        SafetyAction.LAND_NOW,
    }
)

# The commands.type CHECK set (keep in sync with the schema migration).
_KNOWN_COMMAND_TYPES = frozenset({
    "summon", "goto", "perch", "return", "land", "abort", "hold", "takeoff",
    "set_altitude", "set_heading", "nudge", "orbit", "set_roi", "photo",
    "start_recording", "stop_recording", "panorama", "follow_me",
    "spotlight_on", "spotlight_off", "siren_on", "siren_off", "speaker_talk",
    "drop_release", "find_my_drone", "weather_continue", "weather_recall",
})
# What v0.3 actually acts on. Everything else known -> rejected "not handled yet".
_FLIGHT_COMMAND_TYPES = frozenset({"summon", "goto"})
_MISSION_TYPE_FOR = {"summon": "summon", "goto": "manual"}

_EARTH_RADIUS_M = 6_371_000.0
_RECENT_IDS_CAP = 4000
_SUPERVISOR_TICK_S = 0.25


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _parse_ts(value: str) -> datetime:
    """Parse a PostgREST timestamptz string to an aware UTC datetime."""
    s = value.strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if re.search(r"[+-]\d{2}$", s):  # postgres may emit a 2-digit offset
        s += ":00"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


class _RecentIds:
    """A bounded, thread-safe 'have I seen this id' set."""

    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._set: set[str] = set()
        self._order: collections.deque[str] = collections.deque()
        self._lock = threading.Lock()

    def contains(self, value: str) -> bool:
        with self._lock:
            return value in self._set

    def add(self, value: str) -> None:
        with self._lock:
            if value in self._set:
                return
            self._set.add(value)
            self._order.append(value)
            while len(self._order) > self._cap:
                self._set.discard(self._order.popleft())


class _Box:
    """A one-slot mutable flag passed into _process so the handler loop knows
    whether a failure happened before or after the claim landed."""

    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = False


@dataclass
class _ActiveMission:
    mission_id: str
    command_id: str
    command: dict
    executor: Executor
    started_mono: float
    last_phase: MissionPhase = MissionPhase.QUEUED
    duration_abort_sent: bool = False
    last_safety_action: SafetyAction | None = None
    # weather (v0.5)
    is_emergency: bool = False
    weather_warn_since_mono: float | None = None
    weather_last_action: WeatherAction | None = None
    weather_continue: bool = False
    weather_recalled: bool = False


@dataclass
class _AwaitingConfirmation:
    """A routine mission held pre-flight while a human decides whether to
    launch into marginal weather (v0.5)."""

    mission_id: str
    command_id: str
    command: dict
    reasons: tuple[str, ...]
    deadline_mono: float


class _RealtimeSubscriber:
    """Websocket subscription to INSERTs on the commands table (fast path)."""

    _TOPIC = "realtime:gss:commands"

    def __init__(self, supabase_url: str, access_token: str, on_command_id, stop: threading.Event) -> None:
        base = supabase_url.rstrip("/")
        self._ws_url = (
            base.replace("https://", "wss://").replace("http://", "ws://")
            + f"/realtime/v1/websocket?apikey={access_token}&vsn=1.0.0"
        )
        self._token = access_token
        self._on_command_id = on_command_id
        self._stop = stop
        self.status = "starting"

    def run(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            try:
                self._session()
                delay = 1.0
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                self.status = f"down ({type(exc).__name__})"
                log.warning(
                    "realtime: %s (%s); reconnecting in %.0fs",
                    type(exc).__name__, exc, delay,
                )
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, 30.0)
        self.status = "stopped"

    def _session(self) -> None:
        from websockets.exceptions import ConnectionClosed
        from websockets.sync.client import connect

        with connect(self._ws_url, open_timeout=10, close_timeout=5) as ws:
            ws.send(json.dumps({
                "topic": self._TOPIC,
                "event": "phx_join",
                "ref": "1",
                "join_ref": "1",
                "payload": {
                    "config": {
                        "broadcast": {"ack": False},
                        "presence": {"key": ""},
                        "postgres_changes": [
                            {"event": "INSERT", "schema": "public", "table": "commands"}
                        ],
                    },
                    "access_token": self._token,
                },
            }))
            last_hb = time.monotonic()
            hb_ref = 1
            while not self._stop.is_set():
                # Supabase Realtime drops a socket that misses ~2 heartbeats;
                # keep this comfortably under that.
                if time.monotonic() - last_hb >= 15:
                    hb_ref += 1
                    ws.send(json.dumps({
                        "topic": "phoenix", "event": "heartbeat",
                        "payload": {}, "ref": str(hb_ref),
                    }))
                    last_hb = time.monotonic()
                try:
                    raw = ws.recv(timeout=1.0)
                except TimeoutError:
                    continue
                except ConnectionClosed as exc:
                    log.info("realtime: server closed the socket (%s); reconnecting", exc)
                    return
                self._handle(raw)
            log.debug("realtime: session ending (stop requested)")

    def _handle(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        event = msg.get("event")
        if event == "phx_reply":
            # Replies come back for the join (ref "1") and for every heartbeat
            # (topic "phoenix"). Only the join tells us we are subscribed.
            payload = msg.get("payload", {})
            if msg.get("topic") == "phoenix":
                return  # heartbeat ack -- the connection is healthy, nothing to do
            if payload.get("status") == "ok":
                if self.status != "subscribed":
                    log.info("realtime: subscribed to commands INSERT")
                self.status = "subscribed"
            elif payload.get("status") == "error":
                log.warning("realtime: join rejected: %s", payload)
        elif event == "postgres_changes":
            data = msg.get("payload", {}).get("data", {})
            record = data.get("record") or data.get("new") or {}
            command_id = record.get("id")
            if command_id:
                log.debug("realtime: commands INSERT id=%s", command_id)
                self._on_command_id(str(command_id))
        elif event == "system":
            log.debug("realtime: system: %s", msg.get("payload"))


class CommandIntake:
    """Owns the two discovery paths, the claim/validate/accept flow, and the
    mission supervisor. Runs on its own threads -- never the MAVLink path."""

    def __init__(
        self,
        store: TelemetryStore,
        snapshot_source,
        *,
        drone_id: str | None = None,
        dock_id: str | None = None,
        home_lat: float | None = None,
        home_lon: float | None = None,
        max_radius_m: float | None = None,
        max_alt_m: float | None = None,
        on_station_alt_m: float | None = None,
        telemetry_max_age_s: float | None = None,
        poll_interval_s: float | None = None,
        default_ttl_s: float | None = None,
        mission_max_duration_s: float | None = None,
        allow_vehicle_control: bool | None = None,
        supabase_url: str | None = None,
        supabase_key: str | None = None,
        executor_factory=make_executor,
        realtime_enabled: bool = True,
        safety: SafetyMonitor | None = None,
        weather: WeatherMonitor | None = None,
    ) -> None:
        self._store = store
        self._snapshot = snapshot_source
        self._drone_id = drone_id or config.DRONE_ID
        self._dock_id = dock_id or config.DOCK_ID
        self._home_lat = home_lat if home_lat is not None else config.HOME_LAT
        self._home_lon = home_lon if home_lon is not None else config.HOME_LON
        self._max_radius_m = max_radius_m if max_radius_m is not None else config.MAX_RADIUS_M
        self._max_alt_m = max_alt_m if max_alt_m is not None else config.MAX_ALT_M
        self._on_station_alt_m = (
            on_station_alt_m if on_station_alt_m is not None else config.ON_STATION_ALT
        )
        self._telemetry_max_age_s = (
            telemetry_max_age_s if telemetry_max_age_s is not None
            else config.TELEMETRY_MAX_AGE_S
        )
        self._poll_interval_s = (
            poll_interval_s if poll_interval_s is not None else config.COMMAND_POLL_INTERVAL_S
        )
        self._default_ttl_s = (
            default_ttl_s if default_ttl_s is not None else config.COMMAND_DEFAULT_TTL_S
        )
        self._mission_max_duration_s = (
            mission_max_duration_s if mission_max_duration_s is not None
            else config.MISSION_MAX_DURATION_S
        )
        self._allow_vehicle_control = (
            allow_vehicle_control if allow_vehicle_control is not None
            else config.ALLOW_VEHICLE_CONTROL
        )
        self._executor_factory = executor_factory
        self._realtime_enabled = realtime_enabled

        # safety.py has the final say on every flight command and drives the
        # executor in flight. If the caller does not supply a monitor we own
        # one -- the veto is not optional (R1/R12). Intake never runs without it.
        self._owns_safety = safety is None
        self._safety = safety or SafetyMonitor(
            snapshot_source,
            event_sink=(store.log_event if store is not None else None),
            home_lat=self._home_lat,
            home_lon=self._home_lon,
        )

        # weather.py (v0.5): can make the system MORE conservative, never less,
        # and never overrides safety. Absent entirely when WEATHER_ENABLED is
        # false -- then every weather branch below is skipped and behaviour is
        # exactly v0.4.
        self._owns_weather = weather is None and config.WEATHER_ENABLED
        if weather is not None:
            self._weather: WeatherMonitor | None = weather
        elif config.WEATHER_ENABLED:
            self._weather = WeatherMonitor(
                snapshot_source,
                lat=self._home_lat,
                lon=self._home_lon,
                event_sink=(store.log_event if store is not None else None),
            )
        else:
            self._weather = None

        self._stop = threading.Event()
        self._inbox: queue.Queue[str] = queue.Queue()
        self._seen = _RecentIds(_RECENT_IDS_CAP)
        self._active: _ActiveMission | None = None
        self._awaiting: _AwaitingConfirmation | None = None
        self._active_lock = threading.Lock()
        self._retry_status: dict[str, tuple[str, str | None]] = {}
        self._retry_lock = threading.Lock()
        self._threads: list[threading.Thread] = []

        self._realtime: _RealtimeSubscriber | None = None
        if realtime_enabled:
            url = supabase_url or config.SUPABASE_URL
            key = supabase_key or config.SUPABASE_SERVICE_ROLE_KEY
            self._realtime = _RealtimeSubscriber(
                url, key, self.submit, self._stop
            )

    # ------------------------------------------------------------------ API

    def start(self) -> None:
        if self._owns_safety:
            self._safety.start()
        if self._owns_weather and self._weather is not None:
            self._weather.start()
        self._threads = [
            threading.Thread(target=self._handler_loop, name="cmd-handler", daemon=True),
            threading.Thread(target=self._poll_loop, name="cmd-poller", daemon=True),
            threading.Thread(target=self._supervisor_loop, name="mission-supervisor", daemon=True),
        ]
        if self._realtime is not None:
            self._threads.append(
                threading.Thread(target=self._realtime.run, name="cmd-realtime", daemon=True)
            )
        for thread in self._threads:
            thread.start()
        log.info(
            "command intake started (drone %s, poll every %.0fs, realtime %s, DRY RUN)",
            self._drone_id, self._poll_interval_s,
            "on" if self._realtime is not None else "OFF",
        )

    def submit(self, command_id: str) -> None:
        """Enqueue a command id for handling. Called by Realtime, the poller,
        and tests. Handled exactly once regardless of how many times called."""
        self._inbox.put(str(command_id))

    def close(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        with self._active_lock:
            active = self._active
        if active is not None:
            # DRY RUN only: nothing is flying, so ending the mission here is
            # safe and keeps the DB clean for the next run. A real executor
            # (v0.5) must NOT be aborted on GSS shutdown -- the drone keeps
            # flying its onboard mission and the GSS re-attaches (R5).
            active.executor.abort("GSS stopped")
        deadline = time.monotonic() + timeout_s
        for thread in self._threads:
            thread.join(timeout=max(0.05, deadline - time.monotonic()))
        with self._active_lock:
            active = self._active
            awaiting = self._awaiting
        if active is not None:
            self._store.update_mission(
                active.mission_id, status="aborted",
                abort_reason="GSS stopped during dry run",
            )
            self._store.update_command_status(active.command_id, "done")
            self._active = None
        if awaiting is not None:
            self._store.update_mission(
                awaiting.mission_id, status="aborted",
                abort_reason="GSS stopped while awaiting weather confirmation",
            )
            self._store.update_command_status(awaiting.command_id, "rejected",
                                              rejected_reason="GSS stopped before launch")
            self._awaiting = None
        with self._retry_lock:
            leftover = dict(self._retry_status)
        if leftover:
            log.error(
                "command intake: %d status write(s) never persisted: %s",
                len(leftover), leftover,
            )
        self._safety.set_context(None)
        if self._owns_safety:
            self._safety.close(timeout_s=max(0.1, deadline - time.monotonic()))
        if self._weather is not None:
            self._weather.end_mission()
            if self._owns_weather:
                self._weather.close(timeout_s=max(0.1, deadline - time.monotonic()))
        log.info("command intake stopped")

    @property
    def realtime_status(self) -> str:
        return self._realtime.status if self._realtime is not None else "disabled"

    @property
    def active_mission_id(self) -> str | None:
        with self._active_lock:
            return self._active.mission_id if self._active else None

    # ---------------------------------------------------------- handler

    def _handler_loop(self) -> None:
        while not self._stop.is_set():
            try:
                command_id = self._inbox.get(timeout=0.5)
            except queue.Empty:
                continue
            claimed = _Box()
            try:
                self._process(command_id, claimed)
            except Exception:
                log.exception("command intake: handling %s failed", command_id)
                # If we already claimed it, do not leave the row stuck in
                # 'accepted' -- reject it so an operator sees something.
                if claimed.value:
                    self._set_status(
                        command_id, "rejected",
                        reason="internal error while handling the command "
                        "(see the GSS log)",
                    )

    def _process(self, command_id: str, claimed: _Box) -> None:
        if self._seen.contains(command_id):
            return
        ok, claim = self._store.claim_command(command_id, self._drone_id)
        if not ok:
            # DB unreachable -- do NOT mark seen; the poller retries next cycle.
            log.warning("command %s: claim failed (DB unreachable); will retry", command_id)
            return
        self._seen.add(command_id)
        if claim is None:
            log.debug("command %s not available (already handled, not ours, or gone)", command_id)
            return
        claimed.value = True

        cmd = claim["command"]
        server_now = _parse_ts(claim["server_now"])
        ctype = cmd.get("type")
        log.info("claimed command %s (type=%s, issued_by=%s)", command_id, ctype, cmd.get("issued_by"))

        if ctype == "abort":
            self._handle_abort(cmd, server_now)
            return
        if ctype == "hold":
            self._handle_hold(cmd, server_now)
            return
        if ctype == "weather_continue":
            self._handle_weather_answer(cmd, server_now, recall=False)
            return
        if ctype == "weather_recall":
            self._handle_weather_answer(cmd, server_now, recall=True)
            return

        reason = self._validate_flight(cmd, server_now)
        if reason is not None:
            self._reject(command_id, reason)
            return

        # Weather pre-flight gate. safety.py (inside _validate_flight) has
        # already had the final say; weather can only make this MORE
        # conservative -- hold a routine launch for a human, or refuse.
        if self._weather is not None:
            if self._weather_preflight(cmd):
                return  # blocked or now awaiting confirmation

        self._accept_flight(cmd)

    # ---------------------------------------------------------- validation

    def _effective_expiry(self, cmd: dict) -> datetime:
        if cmd.get("expires_at"):
            return _parse_ts(cmd["expires_at"])
        return _parse_ts(cmd["issued_at"]) + timedelta(seconds=self._default_ttl_s)

    def _expiry_reason(self, cmd: dict, server_now: datetime) -> str | None:
        expiry = self._effective_expiry(cmd)
        if server_now > expiry:
            return (
                f"command expired: effective expiry {_fmt(expiry)}, "
                f"database time is now {_fmt(server_now)}"
            )
        return None

    def _validate_flight(self, cmd: dict, server_now: datetime) -> str | None:
        """Return a human-readable rejection reason, or None to accept.

        Order matches the spec. drone_id (check 1) is already enforced by the
        claim RPC -- a foreign command returns no row and we never get here.
        """
        ctype = cmd.get("type")

        # 2. known type
        if ctype not in _KNOWN_COMMAND_TYPES:
            return f"unknown command type {ctype!r}"
        if ctype not in _FLIGHT_COMMAND_TYPES:
            return (
                f"command type {ctype!r} is recognised but not handled until a "
                f"later version (v0.3 flies nothing)"
            )

        # 3. not expired -- server clock, never local
        expired = self._expiry_reason(cmd, server_now)
        if expired:
            return expired

        # 4. required parameters for this type
        lat, lon = cmd.get("target_lat"), cmd.get("target_lon")
        if lat is None or lon is None:
            return f"{ctype} requires target_lat and target_lon; got {lat}, {lon}"

        # 5. inside the geofence radius (great-circle)
        distance = _haversine_m(self._home_lat, self._home_lon, lat, lon)
        if distance > self._max_radius_m:
            return (
                f"target {distance / 1000:.1f} km from home, limit is "
                f"{self._max_radius_m / 1000:.1f} km"
            )

        # 6. requested altitude within the ceiling
        alt = cmd.get("target_alt_m")
        if alt is not None and alt > self._max_alt_m:
            return (
                f"requested altitude {alt:.0f} m exceeds the ceiling of "
                f"{self._max_alt_m:.0f} m"
            )

        # 7. link up and telemetry fresh -- do not accept a flight command blind
        snap: TelemetrySnapshot = self._snapshot()
        if not snap.connected:
            return "link to the vehicle is down -- refusing a flight command while blind"
        age = snap.telemetry_age_s
        if age is None or age > self._telemetry_max_age_s:
            return (
                f"telemetry is stale ("
                f"{'never received' if age is None else f'{age:.0f}s old'}"
                f"), max is {self._telemetry_max_age_s:.0f}s -- refusing a flight "
                f"command while blind"
            )

        # 8. no mission already active (or held for weather confirmation)
        with self._active_lock:
            active = self._active
            awaiting = self._awaiting
        if active is not None:
            return (
                f"a {active.command.get('type')} mission is already running "
                f"(mission {active.mission_id}); a second one is rejected, not queued"
            )
        if awaiting is not None:
            return (
                f"a mission (mission {awaiting.mission_id}) is awaiting a weather "
                f"decision; a second flight command is rejected, not queued"
            )

        # 9. safety.py has the FINAL say. Everything above is intake sanity --
        # a first filter, NOT a substitute for the safety system. safety.py
        # re-checks geofence / battery / position / lateral offset here and
        # again, continuously, in flight. Nobody may delete this call or
        # safety.py's checks on the grounds that commands.py already validated.
        # (server_now is the DB clock -- this path never reads the local clock.)
        verdict = self._safety.evaluate_command(cmd, now=server_now, mission_active=False)
        if verdict.action == SafetyAction.REJECT:
            return "safety veto -- " + "; ".join(verdict.reasons)
        return None

    # ---------------------------------------------------------- accept

    def _accept_flight(self, cmd: dict, *, weather_note: tuple[str, ...] | None = None) -> None:
        command_id = cmd["id"]
        mission_type = _MISSION_TYPE_FOR[cmd["type"]]
        cruise_alt = cmd.get("target_alt_m") or self._on_station_alt_m

        mission_id = self._store.create_mission(
            drone_id=self._drone_id,
            dock_id=self._dock_id,
            mission_type=mission_type,
            target_lat=cmd.get("target_lat"),
            target_lon=cmd.get("target_lon"),
            cruise_alt_m=cruise_alt,
            triggered_by=cmd.get("issued_by") or "command",
        )
        if mission_id is None:
            log.error("command %s: could not create mission row; rejecting", command_id)
            self._reject(command_id, "internal error: could not create the mission")
            return

        self._store.link_command_to_mission(command_id, mission_id)
        self._store.log_event(
            mission_id, "mission_created",
            detail={
                "dry_run": True,
                "command_id": command_id,
                "command_type": cmd["type"],
                "target": [cmd.get("target_lat"), cmd.get("target_lon")],
            },
            sync=True,
        )
        if weather_note is not None:
            # An emergency mission launching into MARGINAL conditions. No
            # confirmation was requested (a person in danger will not fill in a
            # form); record that it launched anyway and why.
            self._store.log_event(
                mission_id, "weather_warning",
                detail={"dry_run": True, "launched_into": "marginal",
                        "reasons": list(weather_note)},
                sync=True,
            )
            log.warning(
                "mission %s: EMERGENCY -- launching into MARGINAL weather, no "
                "confirmation asked: %s", mission_id, "; ".join(weather_note),
            )

        self._start_executor(cmd, mission_id, mission_type, command_id)
        log.info(
            "command %s ACCEPTED -> mission %s (%s). DRY RUN begins.",
            command_id, mission_id, mission_type,
        )

    def _start_executor(
        self, cmd: dict, mission_id: str, mission_type: str, command_id: str
    ) -> None:
        """Build the executor, register the mission as active, and start it.
        Shared by a fresh accept and a launch-after-weather-confirmation."""
        executor = self._executor_factory(
            mission_id, cmd, self._snapshot,
            allow_vehicle_control=self._allow_vehicle_control,
            on_station_alt_m=self._on_station_alt_m,
        )
        if not isinstance(executor, DryRunExecutor):
            raise RuntimeError(
                "refusing a non-dry-run executor in v0.5 "
                f"(got {type(executor).__name__})"
            )

        is_emergency = weather.is_emergency_mission(mission_type)
        self._store.update_command_status(command_id, "executing")
        with self._active_lock:
            self._active = _ActiveMission(
                mission_id=mission_id,
                command_id=command_id,
                command=cmd,
                executor=executor,
                started_mono=time.monotonic(),
                is_emergency=is_emergency,
            )
        self._safety.set_context(self._safety_context(cmd, mission_id, MissionPhase.QUEUED))
        if self._weather is not None:
            self._weather.begin_mission()
        executor.start()

    def _safety_context(
        self, cmd: dict, mission_id: str, phase: MissionPhase
    ) -> SafetyContext:
        """The in-flight context safety.py evaluates this mission against."""
        params = cmd.get("params") if isinstance(cmd.get("params"), dict) else {}
        return SafetyContext(
            preflight=False,
            phase=phase.value,
            mission_id=mission_id,
            is_summon=(cmd.get("type") == "summon"),
            target_lat=cmd.get("target_lat"),
            target_lon=cmd.get("target_lon"),
            target_alt_m=cmd.get("target_alt_m"),
            home_lat=self._home_lat,
            home_lon=self._home_lon,
            human_lat=params.get("human_lat"),
            human_lon=params.get("human_lon"),
        )

    # ---------------------------------------------------------- weather (v0.5)

    def _weather_preflight(self, cmd: dict) -> bool:
        """Consult weather.py before launch. Returns True if it took over the
        command (blocked it, or put the mission into awaiting_confirmation);
        False to let _accept_flight proceed (possibly with a note)."""
        now = datetime.now(timezone.utc)
        mission_type = _MISSION_TYPE_FOR[cmd["type"]]
        is_emergency = weather.is_emergency_mission(mission_type)
        verdict = self._weather.forecast_verdict(now=now)
        decision = weather.preflight_decision(
            verdict, is_emergency=is_emergency, now=now
        )
        log.info(
            "command %s: weather pre-flight %s / %s (emergency=%s) -- %s",
            cmd["id"], decision.action.value, decision.tier.value, is_emergency,
            "; ".join(decision.reasons) or "clear",
        )
        if decision.action == WeatherAction.ALLOW:
            if decision.tier == WeatherTier.CLEAR:
                return False
            # emergency into marginal -- launch now, _accept_flight logs it
            self._accept_flight(cmd, weather_note=decision.reasons)
            return True
        if decision.action == WeatherAction.CONFIRM:
            self._begin_awaiting_confirmation(cmd, decision)
            return True
        # BLOCK
        self._weather_block(cmd, decision)
        return True

    def _begin_awaiting_confirmation(self, cmd: dict, decision) -> None:
        command_id = cmd["id"]
        mission_type = _MISSION_TYPE_FOR[cmd["type"]]
        mission_id = self._store.create_mission(
            drone_id=self._drone_id, dock_id=self._dock_id, mission_type=mission_type,
            target_lat=cmd.get("target_lat"), target_lon=cmd.get("target_lon"),
            cruise_alt_m=cmd.get("target_alt_m") or self._on_station_alt_m,
            triggered_by=cmd.get("issued_by") or "command",
        )
        if mission_id is None:
            self._reject(command_id, "internal error: could not create the mission")
            return
        self._store.link_command_to_mission(command_id, mission_id)
        self._store.update_mission(mission_id, status="awaiting_confirmation")
        self._store.log_event(
            mission_id, "weather_warning",
            detail={"dry_run": True, "stage": "pre-flight", "tier": decision.tier.value,
                    "reasons": list(decision.reasons),
                    "confirm_timeout_s": config.WEATHER_CONFIRM_TIMEOUT_S},
            sync=True,
        )
        self._write_weather_alert(
            mission_id, kind="awaiting_confirmation", decision=decision,
        )
        self._store.update_command_status(command_id, "executing")
        with self._active_lock:
            self._awaiting = _AwaitingConfirmation(
                mission_id=mission_id, command_id=command_id, command=cmd,
                reasons=decision.reasons,
                deadline_mono=time.monotonic() + config.WEATHER_CONFIRM_TIMEOUT_S,
            )
        log.warning(
            "command %s: routine mission, weather MARGINAL -> mission %s "
            "awaiting_confirmation for up to %.0fs: %s",
            command_id, mission_id, config.WEATHER_CONFIRM_TIMEOUT_S,
            "; ".join(decision.reasons),
        )

    def _weather_block(self, cmd: dict, decision) -> None:
        command_id = cmd["id"]
        reason = "weather SEVERE -- not flyable: " + "; ".join(decision.reasons)
        if decision.is_emergency:
            # The worst possible outcome here is a SILENT refusal. Someone
            # needs to know help is not on the way.
            log.critical(
                "EMERGENCY MISSION REFUSED -- weather SEVERE. NO DRONE IS COMING. "
                "command %s. %s", command_id, "; ".join(decision.reasons),
            )
            self._write_weather_alert(None, kind="emergency_refused", decision=decision)
        else:
            log.error(
                "routine mission refused -- weather SEVERE. command %s. %s",
                command_id, "; ".join(decision.reasons),
            )
            self._write_weather_alert(None, kind="launch_blocked", decision=decision)
        self._reject(command_id, reason)

    def _handle_weather_answer(
        self, cmd: dict, server_now: datetime, *, recall: bool
    ) -> None:
        command_id = cmd["id"]
        expired = self._expiry_reason(cmd, server_now)
        if expired:
            self._reject(command_id, expired)
            return
        mid = cmd.get("mission_id") or (
            cmd.get("params", {}) or {}
        ).get("mission_id")
        verb = "weather_recall" if recall else "weather_continue"
        if not mid:
            self._reject(command_id, f"{verb} requires the mission_id it answers")
            return
        with self._active_lock:
            active = self._active
            awaiting = self._awaiting

        if awaiting is not None and awaiting.mission_id == mid:
            if recall:
                self._cancel_awaiting(awaiting, "recalled by the operator before launch")
                self._set_status(command_id, "done")
                return
            self._launch_awaiting(awaiting)
            self._set_status(command_id, "done")
            return

        if active is not None and active.mission_id == mid:
            with self._active_lock:
                if recall:
                    active.weather_recalled = True
                else:
                    active.weather_continue = True
            log.warning(
                "mission %s: operator %s (command %s)",
                mid, "RECALL" if recall else "CONTINUE", command_id,
            )
            self._set_status(command_id, "done")
            return

        self._reject(
            command_id,
            f"mission {mid} is not awaiting a decision and is not running -- it has "
            f"already ended; nothing to {'recall' if recall else 'continue'}",
        )

    def _launch_awaiting(self, awaiting: _AwaitingConfirmation) -> None:
        cmd = awaiting.command
        mission_type = _MISSION_TYPE_FOR[cmd["type"]]
        with self._active_lock:
            self._awaiting = None
        self._store.log_event(
            awaiting.mission_id, "weather_warning",
            detail={"dry_run": True, "stage": "pre-flight",
                    "note": "operator confirmed launch into marginal conditions"},
            sync=True,
        )
        self._start_executor(cmd, awaiting.mission_id, mission_type, awaiting.command_id)
        log.warning(
            "command %s CONFIRMED by operator -> mission %s launching into "
            "marginal weather. DRY RUN begins.",
            awaiting.command_id, awaiting.mission_id,
        )

    def _cancel_awaiting(self, awaiting: _AwaitingConfirmation, why: str) -> None:
        with self._active_lock:
            if self._awaiting is awaiting:
                self._awaiting = None
        self._store.log_event(
            awaiting.mission_id, "weather_launch_blocked",
            detail={"dry_run": True, "reason": why, "conditions": list(awaiting.reasons)},
            sync=True,
        )
        self._store.update_mission(
            awaiting.mission_id, status="aborted",
            abort_reason=f"weather: {why}",
        )
        self._set_status(awaiting.command_id, "rejected",
                         reason=f"weather: {why} ({'; '.join(awaiting.reasons)})")
        log.warning("mission %s cancelled before launch -- %s", awaiting.mission_id, why)

    def _write_weather_alert(self, mission_id: str | None, *, kind: str, decision) -> None:
        snap = self._snapshot()
        self._store.create_alert(
            mission_id=mission_id,
            detail={
                "kind": kind,
                "tier": decision.tier.value,
                "action": decision.action.value,
                "is_emergency": decision.is_emergency,
                "reasons": list(decision.reasons),
                "measured": {
                    "wind_speed_ms": snap.wind_speed_ms,
                    "throttle_pct": snap.throttle_pct,
                },
                "no_drone_is_coming": kind == "emergency_refused",
            },
        )

    def _consult_weather(self, active: _ActiveMission) -> None:
        """Every supervisor tick, AFTER safety (safety wins). Classify from the
        aircraft's own measurements and act: WARN -> log; STAY -> log; RETURN
        -> weather_return event + alert row + abort the dry run."""
        if self._weather is None:
            return
        now = datetime.now(timezone.utc)
        verdict = self._weather.observed_verdict(now=now)

        mono = time.monotonic()
        if verdict.tier == WeatherTier.CLEAR:
            active.weather_warn_since_mono = None
        elif active.weather_warn_since_mono is None:
            active.weather_warn_since_mono = mono
        warn_elapsed = (
            0.0 if active.weather_warn_since_mono is None
            else mono - active.weather_warn_since_mono
        )

        decision = weather.inflight_decision(
            verdict,
            is_emergency=active.is_emergency,
            warn_elapsed_s=warn_elapsed,
            now=now,
            human_recalled=active.weather_recalled,
            human_continue=active.weather_continue,
        )

        # RETURN always acts; other actions act once per distinct action.
        if decision.action != WeatherAction.RETURN and decision.action == active.weather_last_action:
            return
        prev = active.weather_last_action
        active.weather_last_action = decision.action
        reasons = "; ".join(decision.reasons) or decision.tier.value

        if decision.action == WeatherAction.ALLOW:
            if prev is not None:
                log.info("mission %s: weather back to acceptable (%s)",
                         active.mission_id, verdict.tier.value)
            return
        if decision.action == WeatherAction.WARN:
            log.warning("mission %s: WEATHER WARNING (%s) -- %s",
                        active.mission_id, verdict.tier.value, reasons)
            self._store.log_event(
                active.mission_id, "weather_warning",
                detail={"dry_run": True, "tier": verdict.tier.value,
                        "reasons": list(decision.reasons),
                        "grace_s": config.WEATHER_WARN_GRACE_S}, sync=True)
            return
        if decision.action == WeatherAction.STAY:
            log.warning(
                "mission %s: EMERGENCY -- weather %s but STAYING on station "
                "(no countdown; only an explicit recall or SEVERE brings it "
                "home): %s", active.mission_id, verdict.tier.value, reasons)
            self._store.log_event(
                active.mission_id, "weather_hold",
                detail={"dry_run": True, "tier": verdict.tier.value,
                        "reasons": list(decision.reasons),
                        "note": "emergency mission holds through marginal weather"},
                sync=True)
            return
        # RETURN
        if decision.action == active.weather_last_action and prev == WeatherAction.RETURN:
            return  # already returning
        measured = self._weather.observed_conditions(self._snapshot())
        log.error(
            "mission %s: WEATHER RETURN%s -- %s (measured: wind %s m/s, "
            "throttle %s%%)",
            active.mission_id,
            " (not overridable)" if verdict.tier == WeatherTier.SEVERE else "",
            reasons, measured.wind_speed_ms, measured.throttle_pct,
        )
        self._store.log_event(
            active.mission_id, "weather_return",
            detail={
                "dry_run": True, "tier": verdict.tier.value,
                "reasons": list(decision.reasons),
                "overridable": verdict.tier != WeatherTier.SEVERE and not active.weather_recalled,
                "measured": {
                    "wind_speed_ms": measured.wind_speed_ms,
                    "throttle_pct": measured.throttle_pct,
                    "vibration_max": measured.vibration_max,
                },
            },
            sync=True,
        )
        self._store.create_alert(
            mission_id=active.mission_id,
            detail={
                "kind": "weather_return",
                "tier": verdict.tier.value,
                "reasons": list(decision.reasons),
                "returning_to": "dock",
                "measured": {
                    "wind_speed_ms": measured.wind_speed_ms,
                    "throttle_pct": measured.throttle_pct,
                },
                "recording_until": "touchdown",
            },
        )
        active.executor.abort(f"weather return ({verdict.tier.value}): {reasons}")

    # ---------------------------------------------------------- abort / hold

    def _handle_abort(self, cmd: dict, server_now: datetime) -> None:
        command_id = cmd["id"]
        expired = self._expiry_reason(cmd, server_now)
        if expired:
            self._reject(command_id, expired)
            return
        with self._active_lock:
            active = self._active
        if active is None:
            self._reject(command_id, "no mission is running -- nothing to abort")
            return
        who = f" by {cmd['issued_by']}" if cmd.get("issued_by") else ""
        active.executor.abort(f"commanded abort (command {command_id}{who})")
        log.warning(
            "mission %s: ABORT requested by command %s -- supervisor will "
            "finalize within %.2fs", active.mission_id, command_id, _SUPERVISOR_TICK_S,
        )
        # The mission -> aborted transition + its mission_events are the
        # supervisor's job. This command is done once we have acted on it.
        self._set_status(command_id, "done")

    def _handle_hold(self, cmd: dict, server_now: datetime) -> None:
        command_id = cmd["id"]
        expired = self._expiry_reason(cmd, server_now)
        if expired:
            self._reject(command_id, expired)
            return
        with self._active_lock:
            active = self._active
        if active is None:
            self._reject(command_id, "no mission is running -- nothing to hold")
            return
        log.warning(
            "[DRY RUN] mission %s: would HOLD position now (command %s)",
            active.mission_id, command_id,
        )
        self._store.log_event(
            active.mission_id, "loiter_start",
            detail={"dry_run": True, "hold_command": command_id, "note": "commanded hold"},
            sync=True,
        )
        self._set_status(command_id, "done")

    # ---------------------------------------------------------- supervisor

    def _supervisor_loop(self) -> None:
        while not self._stop.is_set():
            with self._active_lock:
                active = self._active
                awaiting = self._awaiting
            if active is not None:
                try:
                    self._advance(active)
                except Exception:
                    log.exception("mission supervisor: advancing %s failed", active.mission_id)
            elif awaiting is not None:
                try:
                    self._service_awaiting(awaiting)
                except Exception:
                    log.exception("mission supervisor: awaiting %s failed", awaiting.mission_id)
            self._stop.wait(_SUPERVISOR_TICK_S)

    def _service_awaiting(self, awaiting: _AwaitingConfirmation) -> None:
        """A routine mission held for weather confirmation. No answer by the
        deadline -> cancelled, not launched."""
        if time.monotonic() >= awaiting.deadline_mono:
            self._cancel_awaiting(
                awaiting,
                f"no operator decision within {config.WEATHER_CONFIRM_TIMEOUT_S:.0f}s",
            )

    def _advance(self, active: _ActiveMission) -> None:
        phase = active.executor.poll()

        # safety.py is consulted every tick, independent of phase changes.
        if phase not in TERMINAL_PHASES:
            self._consult_safety(active)
            # weather.py -- AFTER safety, and only while safety has NOT ordered
            # anything: safety wins the authority order outright. If safety is
            # holding (untrustworthy position) or driving home, weather does
            # not get to countermand it -- e.g. "RETURN" is the wrong call when
            # safety has said the position cannot be trusted.
            if active.last_safety_action is None:
                self._consult_weather(active)

        if phase not in TERMINAL_PHASES and not active.duration_abort_sent:
            if time.monotonic() - active.started_mono > self._mission_max_duration_s:
                active.duration_abort_sent = True
                log.error(
                    "mission %s exceeded MISSION_MAX_DURATION_S (%.0fs); aborting",
                    active.mission_id, self._mission_max_duration_s,
                )
                active.executor.abort(
                    f"exceeded MISSION_MAX_DURATION_S ({self._mission_max_duration_s:.0f}s)"
                )
                phase = active.executor.poll()

        if phase == active.last_phase:
            return

        would = (
            active.executor.describe_phase(phase)
            if isinstance(active.executor, DryRunExecutor)
            else None
        )
        for event_name in PHASE_EVENTS.get(phase, ()):
            self._store.log_event(
                active.mission_id, event_name,
                detail={"dry_run": True, "phase": phase.value, "would": would},
                sync=True,
            )
        fields: dict[str, object] = {"status": phase.value}
        if phase == MissionPhase.ABORTED:
            fields["abort_reason"] = active.executor.abort_reason or "aborted"
        self._store.update_mission(active.mission_id, **fields)
        log.info(
            "mission %s: %s -> %s  [DRY RUN]%s",
            active.mission_id, active.last_phase.value, phase.value,
            f"  ({would})" if would else "",
        )
        active.last_phase = phase

        if phase in TERMINAL_PHASES:
            self._finalize(active, phase)

    def _consult_safety(self, active: _ActiveMission) -> None:
        """Every supervisor tick: ask safety.py, and drive the executor on its
        verdict. RTL_NOW / LAND_NOW -> abort the dry run; HOLD / DESCEND ->
        record what a real executor (v0.6) would do. A 'safety_veto'
        mission_event carries the full reason list.

        Every actionable verdict is handled EXPLICITLY -- an unrecognised
        action is a loud error that aborts (fail safe), never a silent
        fall-through."""
        self._safety.set_context(
            self._safety_context(active.command, active.mission_id, active.last_phase)
        )
        verdict = self._safety.current_verdict()
        if verdict.action not in _SAFETY_ACTIONABLE:
            # Safety is nominal (ALLOW/WARN). Clear the marker so a later
            # actionable verdict re-logs, and so weather.py may be consulted
            # again (it is skipped while safety has ordered anything).
            active.last_safety_action = None
            return
        if verdict.action == active.last_safety_action:
            return  # already acted on this verdict
        active.last_safety_action = verdict.action
        reasons = "; ".join(verdict.reasons) or verdict.severity.value
        log.error(
            "mission %s: SAFETY VETO %s (%s) -- %s",
            active.mission_id, verdict.action.value, verdict.severity.value, reasons,
        )
        self._store.log_event(
            active.mission_id, "safety_veto",
            detail={
                "action": verdict.action.value,
                "severity": verdict.severity.value,
                "latched": verdict.latched,
                "reasons": list(verdict.reasons),
            },
            sync=True,
        )
        if verdict.action == SafetyAction.DIVERT:
            # Home is not reachable with the battery available. A real executor
            # (v0.6) flies to the named safe spot and lands; the dry run ends
            # here like an RTL. Say WHERE and WHY, loudly, with the numbers --
            # if the drone lands away from its dock, whoever is looking for it
            # needs that immediately.
            tgt = verdict.divert_target or {}
            self._store.log_event(
                active.mission_id, "divert",
                detail={
                    "dry_run": True,
                    "reasons": list(verdict.reasons),
                    "safe_spot": {
                        "name": tgt.get("spot_name"),
                        "lat": tgt.get("spot_lat"),
                        "lon": tgt.get("spot_lon"),
                        "surface": tgt.get("spot_surface"),
                        "has_marker": tgt.get("spot_has_marker"),
                        "marker_id": tgt.get("spot_marker_id"),
                        "height_above_dock_m": tgt.get("spot_height_above_dock_m"),
                        "distance_m": tgt.get("spot_distance_m"),
                    },
                    "home_not_reachable": {
                        "battery_pct": tgt.get("battery_pct"),
                        "home_distance_m": tgt.get("home_distance_m"),
                        "home_needed_pct": tgt.get("home_needed_pct"),
                        "headwind_ms": tgt.get("headwind_ms"),
                    },
                },
                sync=True,
            )
            self._write_divert_alert(active, verdict, diverting=True)
            active.executor.abort(f"safety veto (DIVERT): {reasons}")
        elif verdict.action in (SafetyAction.RTL_NOW, SafetyAction.LAND_NOW):
            if verdict.action == SafetyAction.LAND_NOW and (
                verdict.divert_target or {}
            ).get("nowhere_reachable"):
                # Landing where it is because nothing was reachable -- the
                # worst case, and the one where the beacon (Phase 14) matters
                # most. The alert must say so, with the last position.
                self._write_divert_alert(active, verdict, diverting=False)
            active.executor.abort(f"safety veto ({verdict.action.value}): {reasons}")
        elif verdict.action == SafetyAction.HOLD:
            self._store.log_event(
                active.mission_id, "loiter_start",
                detail={"dry_run": True, "note": "safety HOLD", "reasons": list(verdict.reasons)},
                sync=True,
            )
        elif verdict.action == SafetyAction.DESCEND:
            # v0.4 is dry-run: log what v0.6's real executor will do. The
            # mission is not aborted -- DESCEND means correct the altitude and
            # keep going.
            log.warning(
                "[DRY RUN] mission %s: would stop horizontal progress and "
                "descend to a safe altitude -- %s", active.mission_id, reasons,
            )
            self._store.log_event(
                active.mission_id, "loiter_start",
                detail={"dry_run": True, "note": "safety DESCEND",
                        "would": "stop and descend below MAX_ALT_M",
                        "reasons": list(verdict.reasons)},
                sync=True,
            )
        else:
            # A new SafetyAction was added to _SAFETY_ACTIONABLE without a
            # branch here. Do not guess -- fail safe and make it visible.
            log.error(
                "mission %s: unhandled safety action %r -- aborting the dry run "
                "(a branch is missing in _consult_safety)",
                active.mission_id, verdict.action,
            )
            active.executor.abort(f"unhandled safety action {verdict.action.value}: {reasons}")

    def _write_divert_alert(
        self, active: _ActiveMission, verdict, *, diverting: bool
    ) -> None:
        """A landing away from the dock. Whoever is looking for the drone needs
        to know WHERE and WHY, immediately, with the numbers. If nothing was
        reachable (``diverting`` False) the alert says that too, with the last
        position -- and marks the beacon hook (spotlight + siren, Phase 14)."""
        tgt = verdict.divert_target or {}
        snap = self._snapshot()
        detail: dict = {
            "kind": "divert" if diverting else "land_now_nowhere",
            "reasons": list(verdict.reasons),
            "measured": {
                "battery_pct": snap.battery_pct,
                "wind_speed_ms": snap.wind_speed_ms,
                "headwind_ms": tgt.get("headwind_ms"),
                "home_distance_m": tgt.get("home_distance_m"),
                "home_needed_pct": tgt.get("home_needed_pct"),
            },
            "no_drone_is_coming": not diverting,
        }
        if diverting:
            detail["landing_at"] = {
                "name": tgt.get("spot_name"),
                "lat": tgt.get("spot_lat"),
                "lon": tgt.get("spot_lon"),
                "surface": tgt.get("spot_surface"),
                "has_marker": tgt.get("spot_has_marker"),
                "marker_id": tgt.get("spot_marker_id"),
                "height_above_dock_m": tgt.get("spot_height_above_dock_m"),
                "distance_m": tgt.get("spot_distance_m"),
                "accuracy": "ArUco pad" if tgt.get("spot_has_marker") else "GPS-only",
            }
        else:
            detail["landing_at"] = {
                "name": "current position -- no safe spot reachable",
                "lat": tgt.get("last_lat", snap.lat),
                "lon": tgt.get("last_lon", snap.lon),
            }
            # BEACON HOOK: Phase 14's spotlight + siren payload activates here.
            detail["beacon_hook"] = (
                "spotlight + siren (Phase 14) should activate on this landing"
            )
        self._store.create_alert(mission_id=active.mission_id, detail=detail)
        log.error(
            "mission %s: %s -- alert written (%s)",
            active.mission_id,
            "DIVERT to a safe spot" if diverting
            else "LAND_NOW where it is, nothing reachable",
            detail["landing_at"],
        )

    def _finalize(self, active: _ActiveMission, phase: MissionPhase) -> None:
        self._safety.set_context(None)
        if self._weather is not None:
            self._weather.end_mission()
        self._set_status(active.command_id, "done")
        with self._active_lock:
            if self._active is active:
                self._active = None
        log.info(
            "mission %s complete (%s); command %s -> done.  [DRY RUN]",
            active.mission_id, phase.value, active.command_id,
        )

    # ---------------------------------------------------------- poller

    def _poll_loop(self) -> None:
        self._reconcile_orphan_missions()
        while not self._stop.is_set():
            try:
                self._retry_status_writes()
                for command_id in self._store.list_pending_command_ids(self._drone_id):
                    self.submit(command_id)
            except Exception:
                log.exception("command poller: cycle failed")
            self._stop.wait(self._poll_interval_s)

    def _reconcile_orphan_missions(self) -> None:
        for mission in self._store.list_active_missions(self._drone_id):
            log.warning(
                "startup: mission %s is %r with no running executor -- marking "
                "aborted (orphaned by a GSS restart)",
                mission.get("id"), mission.get("status"),
            )
            self._store.update_mission(
                mission["id"], status="aborted",
                abort_reason="orphaned: GSS restarted while the mission was non-terminal",
            )

    # ---------------------------------------------------------- status writes

    def _reject(self, command_id: str, reason: str) -> None:
        log.info("command %s REJECTED: %s", command_id, reason)
        self._set_status(command_id, "rejected", reason=reason)

    def _set_status(self, command_id: str, status: str, reason: str | None = None) -> None:
        if self._store.update_command_status(command_id, status, rejected_reason=reason):
            with self._retry_lock:
                self._retry_status.pop(command_id, None)
            return
        # A dropped command-status write leaves the row stuck -- never discard.
        log.error(
            "command %s: could not write status %r; queued for retry each poll cycle",
            command_id, status,
        )
        with self._retry_lock:
            self._retry_status[command_id] = (status, reason)

    def _retry_status_writes(self) -> None:
        with self._retry_lock:
            pending = list(self._retry_status.items())
        for command_id, (status, reason) in pending:
            if self._store.update_command_status(command_id, status, rejected_reason=reason):
                with self._retry_lock:
                    self._retry_status.pop(command_id, None)
                log.info("command %s: status %r finally persisted on retry", command_id, status)
