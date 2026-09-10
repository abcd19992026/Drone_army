"""Best-effort Supabase mirror of the live telemetry (v0.2).

This is the ONLY module under ``gss/`` that touches the network. Everything
here is strictly downstream of the MAVLink path and strictly best-effort:

  * **R10** -- a slow or failed Supabase call must never block, delay, or
    stall the MAVLink receive loop or the telemetry reader. If Supabase is
    unreachable for an hour, the GSS keeps flying the drone and keeps
    logging to the console exactly as before.
  * **R1**  -- nothing here is read back onto the MAVLink path. Values that
    ``safety.py`` will depend on live in ``config.py``, never in the
    database.
  * **R8**  -- failures are logged (WARNING) with context, never swallowed,
    never fatal.

Two threads, no I/O on the producer side:

  * *producer* -- wakes every ``SUPABASE_TELEMETRY_INTERVAL_S``, samples the
    current :class:`~gss.telemetry.TelemetrySnapshot`, decides whether
    anything meaningful changed, and enqueues an UPDATE job.
  * *consumer* -- drains the queue and performs the HTTP writes with capped
    retry/backoff. A stuck consumer only makes the bounded queue fill and
    shed its oldest entries; nothing upstream notices.

The queue is bounded (``SUPABASE_QUEUE_MAX``) and drops its OLDEST entry
when full: stale position data has no value, and unbounded growth would
eventually kill the process on a Raspberry Pi.

Telemetry is written as an UPDATE of the single ``drones`` row, never an
INSERT. At a 2 s cadence that is ~43,200 writes/day (86400 / 2); as updates
to one row that is nothing, as inserts it would exhaust the free tier and
build a table nobody can query.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from gss import config
from gss.telemetry import TelemetrySnapshot

log = logging.getLogger(__name__)

_MIN_MOVE_M = 1.0
_MAX_ATTEMPTS = 3         # telemetry writes (best-effort, coalescing)
_STATE_WRITE_ATTEMPTS = 6  # command / mission state writes (must not be lost)
_BACKOFF_CAP_S = 30.0
_REJECT_PAUSE_S = 5.0  # pause after a non-retryable (4xx) failure
_DRAIN_TIMEOUT_S = 2.0  # per-request timeout while flushing at shutdown
_DROP_LOG_INTERVAL_S = 30.0
_METERS_PER_DEG_LAT = 111_320.0


@dataclass(frozen=True)
class _Outcome:
    """Result of one HTTP attempt."""

    ok: bool
    status: int | None = None
    retryable: bool = False
    detail: str = ""
    body: str = ""  # response body on success (when return=representation)


class _DropOldestQueue:
    """A bounded FIFO that discards its oldest item when full, and counts drops."""

    def __init__(self, maxlen: int) -> None:
        self._dq: deque = deque()
        self._maxlen = maxlen
        self._cv = threading.Condition()
        self._dropped = 0

    def put(self, item: object) -> None:
        with self._cv:
            while len(self._dq) >= self._maxlen:
                self._dq.popleft()
                self._dropped += 1
            self._dq.append(item)
            self._cv.notify()

    def get(self, timeout: float) -> object | None:
        with self._cv:
            if not self._dq:
                self._cv.wait(timeout)
            return self._dq.popleft() if self._dq else None

    def empty(self) -> bool:
        with self._cv:
            return not self._dq

    def qsize(self) -> int:
        with self._cv:
            return len(self._dq)

    def take_dropped(self) -> int:
        with self._cv:
            dropped, self._dropped = self._dropped, 0
            return dropped

    def dropped_so_far(self) -> int:
        with self._cv:
            return self._dropped

    def has_kind(self, kind: str) -> bool:
        with self._cv:
            return any(isinstance(i, dict) and i.get("kind") == kind for i in self._dq)


SnapshotSource = Callable[[], TelemetrySnapshot]


class TelemetryStore:
    """Mirrors the current :class:`TelemetrySnapshot` into the ``drones`` row."""

    def __init__(
        self,
        snapshot_source: SnapshotSource,
        *,
        url: str | None = None,
        service_role_key: str | None = None,
        drone_id: str | None = None,
        telemetry_interval_s: float | None = None,
        heartbeat_write_s: float | None = None,
        queue_max: int | None = None,
        http_timeout_s: float = 8.0,
    ) -> None:
        """Build a store. Defaults come from :mod:`gss.config`."""
        self._snapshot_source = snapshot_source
        self._base = (url or config.SUPABASE_URL).rstrip("/")
        self._key = service_role_key or config.SUPABASE_SERVICE_ROLE_KEY
        self._drone_id = drone_id or config.DRONE_ID
        self._telemetry_interval_s = (
            telemetry_interval_s
            if telemetry_interval_s is not None
            else config.SUPABASE_TELEMETRY_INTERVAL_S
        )
        self._heartbeat_write_s = (
            heartbeat_write_s
            if heartbeat_write_s is not None
            else config.SUPABASE_HEARTBEAT_WRITE_S
        )
        self._http_timeout_s = http_timeout_s
        if not (self._base and self._key and self._drone_id):
            raise ValueError(
                "TelemetryStore needs a URL, a service-role key and a drone id"
            )

        self._queue = _DropOldestQueue(
            queue_max if queue_max is not None else config.SUPABASE_QUEUE_MAX
        )
        self._stop = threading.Event()
        self._started = False
        self._producer: threading.Thread | None = None
        self._consumer: threading.Thread | None = None

        # producer-side dedup state (only touched by the producer thread)
        self._last_enqueued: TelemetrySnapshot | None = None
        self._last_enqueue_mono: float | None = None

        # stats (atomic-ish ints, read for logging / tests)
        self._writes_ok = 0
        self._writes_failed = 0
        self._writes_superseded = 0
        self._total_dropped = 0
        self._last_drop_log_mono = time.monotonic()

    # ------------------------------------------------------------------ API

    def start(self) -> None:
        """Start the producer and consumer threads (idempotent)."""
        if self._started:
            return
        self._started = True
        log.info(
            "store: Supabase sync enabled -> %s (drone %s, telemetry every %.1fs, "
            "heartbeat write every %.1fs, queue max %d)",
            self._base, self._drone_id, self._telemetry_interval_s,
            self._heartbeat_write_s, self._queue._maxlen,  # noqa: SLF001 - own field
        )
        self._consumer = threading.Thread(
            target=self._consumer_loop, name="store-consumer", daemon=True
        )
        self._producer = threading.Thread(
            target=self._producer_loop, name="store-producer", daemon=True
        )
        self._consumer.start()
        self._producer.start()

    def close(self, timeout_s: float = 5.0) -> None:
        """Stop the producer, let the consumer flush the queue, then return.

        Never hangs longer than ``timeout_s`` waiting for the flush.
        """
        self._stop.set()
        if self._producer is not None:
            self._producer.join(timeout=2.0)
        if self._consumer is not None:
            deadline = time.monotonic() + timeout_s
            while self._consumer.is_alive() and time.monotonic() < deadline:
                self._consumer.join(timeout=0.2)
            if self._consumer.is_alive():
                log.warning(
                    "store: shutdown timeout, %d update(s) not flushed",
                    self._queue.qsize(),
                )
        self._log_drops(force=True)
        log.info(
            "store: stopped (%d ok, %d failed, %d superseded, %d dropped)",
            self._writes_ok, self._writes_failed, self._writes_superseded,
            self._total_dropped,
        )

    def log_event(
        self,
        mission_id: str | None,
        event: str,
        detail: dict | None = None,
        *,
        sync: bool = False,
    ) -> bool:
        """Write a ``mission_events`` row, auto-stamped from the live snapshot.

        lat/lon/alt/battery/voltage and link_up are filled from the current
        :class:`TelemetrySnapshot` so callers cannot forget to. Position is
        only stamped when it is valid.

        ``sync=False`` (default): enqueue on the best-effort queue -- fine for
        high-rate informational events. ``sync=True``: write it now, retried,
        and return whether it landed -- used for the mission's flight story so
        the events stay ordered and are not silently dropped. Returns True on
        an accepted enqueue or a successful sync write.
        """
        snap = self._snapshot_source()
        row = {
            "mission_id": mission_id,
            "event": event,
            "at": _iso(snap.timestamp),
            "detail": detail,
            "lat": snap.lat if snap.position_valid else None,
            "lon": snap.lon if snap.position_valid else None,
            "alt_m": snap.alt_m_relative,
            "battery_pct": snap.battery_pct,
            "battery_voltage_v": snap.battery_voltage_v,
            "link_up": snap.connected,
        }
        if not sync:
            self._enqueue({"kind": "event", "payload": row})
            return True
        out = self._sync_call(
            "POST", "/rest/v1/mission_events", None, row,
            attempts=4, what=f"mission_event {event}",
        )
        return out.ok

    @property
    def stats(self) -> dict[str, int]:
        """Counters for introspection / tests."""
        return {
            "writes_ok": self._writes_ok,
            "writes_failed": self._writes_failed,
            "superseded": self._writes_superseded,
            "dropped": self._total_dropped + self._queue.dropped_so_far(),
            "queued": self._queue.qsize(),
        }

    # ---------------------------------------------------------- producer

    def _producer_loop(self) -> None:
        while not self._stop.is_set():
            try:
                snap = self._snapshot_source()
                self._maybe_enqueue_telemetry(snap)
            except Exception:
                log.exception("store: producer cycle failed")
            self._stop.wait(self._telemetry_interval_s)

    def _maybe_enqueue_telemetry(self, snap: TelemetrySnapshot) -> None:
        now = time.monotonic()
        changed, reason = self._significant_change(snap)
        heartbeat_due = (
            self._last_enqueue_mono is None
            or now - self._last_enqueue_mono >= self._heartbeat_write_s
        )
        if not (changed or heartbeat_due):
            return
        self._enqueue({"kind": "telemetry", "payload": self._telemetry_payload(snap)})
        self._last_enqueued = snap
        self._last_enqueue_mono = now
        log.debug("store: queued telemetry (%s)", reason or "heartbeat write")

    def _significant_change(self, snap: TelemetrySnapshot) -> tuple[bool, str]:
        """Decide whether the drone's state moved enough to be worth a write."""
        prev = self._last_enqueued
        if prev is None:
            return True, "first write"
        if snap.connected != prev.connected:
            return True, "link state changed"
        if snap.armed != prev.armed:
            return True, "armed changed"
        if snap.mode != prev.mode:
            return True, "mode changed"
        if snap.battery_pct != prev.battery_pct:
            return True, "battery changed"
        if snap.position_valid != prev.position_valid:
            return True, "position validity changed"
        distance = _distance_m(prev.lat, prev.lon, snap.lat, snap.lon)
        if distance is not None and distance >= _MIN_MOVE_M:
            return True, f"moved {distance:.1f} m"
        return False, ""

    def _telemetry_payload(self, snap: TelemetrySnapshot) -> dict:
        """Build the PATCH body for the ``drones`` row.

        ``status`` and ``control_mode`` are deliberately NOT written here --
        those are mission/command state (v0.3+). This is the telemetry
        mirror only.
        """
        payload: dict = {
            "link_up": snap.connected,
            "armed": snap.armed,
            "mode": snap.mode,
            "battery_pct": snap.battery_pct,
            "battery_voltage_v": snap.battery_voltage_v,
            "battery_current_a": snap.battery_current_a,
            "lat": snap.lat,
            "lon": snap.lon,
            "alt_m_relative": snap.alt_m_relative,
            "alt_m_amsl": snap.alt_m_amsl,
            "heading_deg": snap.heading_deg,
            "groundspeed_ms": snap.groundspeed_ms,
            "gps_fix_type": snap.gps_fix_type,
            "gps_satellites": snap.gps_satellites,
            "position_valid": snap.position_valid,
            "last_telemetry_at": _iso(snap.timestamp),
        }
        if snap.connected and snap.link_age_s is not None:
            payload["last_heartbeat_at"] = _iso(
                snap.timestamp - timedelta(seconds=snap.link_age_s)
            )
        # last_known_* track the last trustworthy fix and are never nulled or
        # cleared on link loss -- they answer "where do I go looking for it".
        if snap.position_valid and snap.lat is not None and snap.lon is not None:
            payload["last_known_lat"] = snap.lat
            payload["last_known_lon"] = snap.lon
            payload["last_known_alt_m"] = snap.alt_m_relative
            payload["last_known_at"] = _iso(snap.timestamp)
        return payload

    def _enqueue(self, job: dict) -> None:
        if not self._started:
            return
        self._queue.put(job)

    # ---------------------------------------------------------- consumer

    def _consumer_loop(self) -> None:
        network_failures = 0
        while not (self._stop.is_set() and self._queue.empty()):
            job = self._queue.get(timeout=0.5)
            if job is None:
                continue
            draining = self._stop.is_set()
            ok, retryable_failure, superseded = self._write(job, allow_retry=not draining)
            if superseded:
                self._writes_superseded += 1
                network_failures = 0
            elif ok:
                self._writes_ok += 1
                network_failures = 0
            else:
                self._writes_failed += 1
                if draining:
                    pass
                elif retryable_failure:
                    # Network / 5xx: back the whole consumer off a failing
                    # endpoint with capped exponential delay so we do not
                    # hammer it (and so a full queue visibly sheds).
                    network_failures += 1
                    self._stop.wait(
                        min(_BACKOFF_CAP_S, float(2 ** min(network_failures, 5)))
                    )
                else:
                    # 4xx: our bug, not transient. Do not escalate; a short
                    # fixed pause keeps the log readable if it is persistent.
                    network_failures = 0
                    self._stop.wait(_REJECT_PAUSE_S)
            self._log_drops()
        self._log_drops(force=True)

    def _write(self, job: dict, allow_retry: bool) -> tuple[bool, bool, bool]:
        """Return (ok, was_a_retryable_failure, superseded)."""
        attempts = _MAX_ATTEMPTS if allow_retry else 1
        timeout = self._http_timeout_s if allow_retry else _DRAIN_TIMEOUT_S
        delay = 1.0
        for attempt in range(1, attempts + 1):
            outcome = self._http_write(job, timeout)
            if outcome.ok:
                return True, False, False
            if not outcome.retryable:
                # A 4xx is a bug in our request, not a transient fault --
                # never retried.
                log.warning(
                    "store: %s write rejected (HTTP %s), not retrying: %s",
                    job["kind"], outcome.status, outcome.detail,
                )
                return False, False, False
            if attempt < attempts:
                # A stale telemetry write has no value once a newer one is
                # already queued -- abandon it rather than burn a retry.
                if job["kind"] == "telemetry" and self._queue.has_kind("telemetry"):
                    log.debug("store: telemetry write superseded by a newer one")
                    return False, False, True
                log.warning(
                    "store: %s write failed (%s); retry %d/%d in %.0fs",
                    job["kind"], outcome.detail, attempt, attempts - 1, delay,
                )
                if self._stop.wait(delay):
                    return False, True, False
                delay = min(delay * 2, _BACKOFF_CAP_S)
        log.warning(
            "store: %s write gave up after %d attempt(s): endpoint unreachable "
            "or erroring", job["kind"], attempts,
        )
        return False, True, False

    def _http_write(self, job: dict, timeout: float) -> _Outcome:
        if job["kind"] == "telemetry":
            return self._request(
                "PATCH", "/rest/v1/drones",
                params={"id": f"eq.{self._drone_id}"},
                body=job["payload"], timeout=timeout,
            )
        return self._request(
            "POST", "/rest/v1/mission_events", params=None,
            body=job["payload"], timeout=timeout,
        )

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None,
        body: dict | None,
        timeout: float,
        prefer: str | None = "return=minimal",
    ) -> _Outcome:
        url = self._base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }
        if prefer is not None:
            headers["Prefer"] = prefer
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
                return _Outcome(ok=True, status=getattr(resp, "status", 200), body=text)
        except urllib.error.HTTPError as exc:
            detail = _read_error(exc)
            retryable = exc.code in (408, 429) or exc.code >= 500
            return _Outcome(
                ok=False, status=exc.code, retryable=retryable, detail=detail
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return _Outcome(
                ok=False, status=None, retryable=True,
                detail=f"{type(exc).__name__}: {exc}",
            )

    # ---------------------------------------------------- command / mission
    #
    # Synchronous DB operations for gss/commands.py. These run on the command
    # intake's own threads, never the MAVLink path (R10 holds by thread
    # isolation, same as the telemetry consumer). They retry HARDER than
    # telemetry: a dropped telemetry update is harmless, a dropped command
    # status write leaves a row stuck in 'accepted' forever. On final failure
    # they return False / None and log at ERROR -- the caller retries on its
    # next poll cycle rather than discarding.

    def _sync_call(
        self,
        method: str,
        path: str,
        params: dict | None,
        body: dict | None,
        *,
        prefer: str | None = "return=minimal",
        attempts: int = _STATE_WRITE_ATTEMPTS,
        timeout: float = 8.0,
        what: str = "db op",
    ) -> _Outcome:
        delay = 1.0
        last: _Outcome = _Outcome(ok=False, detail="not attempted")
        for attempt in range(1, attempts + 1):
            last = self._request(method, path, params, body, timeout, prefer)
            if last.ok:
                return last
            if not last.retryable:
                log.error(
                    "store: %s rejected (HTTP %s), not retrying: %s",
                    what, last.status, last.detail,
                )
                return last
            if attempt < attempts:
                log.warning(
                    "store: %s failed (%s); retry %d/%d in %.0fs",
                    what, last.detail, attempt, attempts - 1, delay,
                )
                if self._stop.wait(delay):
                    return last
                delay = min(delay * 2, _BACKOFF_CAP_S)
        log.error("store: %s could not be written after %d attempts: %s",
                  what, attempts, last.detail)
        return last

    def claim_command(
        self, command_id: str, drone_id: str
    ) -> tuple[bool, dict | None]:
        """Atomically claim a pending command for this drone.

        Calls the ``claim_command`` RPC (``UPDATE ... SET status='accepted',
        acked_at=now() WHERE id=$1 AND status='pending' AND drone_id=$2
        RETURNING *``).

        Returns ``(reached_db, payload)``:
          * ``(True,  {"command": {...}, "server_now": "<iso>"})`` -- claimed
          * ``(True,  None)`` -- nothing to claim (already handled, not ours,
            or gone); a normal, silent outcome
          * ``(False, None)`` -- could not reach the database; the caller must
            NOT mark the id seen so the poller retries it
        """
        out = self._sync_call(
            "POST", "/rest/v1/rpc/claim_command", None,
            {"p_command_id": command_id, "p_drone_id": drone_id},
            prefer=None, what=f"claim_command({command_id})",
        )
        if not out.ok:
            return False, None
        body = out.body.strip()
        if not body or body == "null":
            return True, None
        try:
            payload = json.loads(body)
            if isinstance(payload, str):  # some configs double-encode a jsonb return
                payload = json.loads(payload)
        except json.JSONDecodeError:
            log.error("store: claim_command returned unparseable body: %r", body[:200])
            return True, None
        if not isinstance(payload, dict) or "command" not in payload:
            return True, None
        return True, payload

    def update_command_status(
        self,
        command_id: str,
        status: str,
        *,
        rejected_reason: str | None = None,
        mission_id: str | None = None,
    ) -> bool:
        """PATCH a command's status (+ optional reason / mission link)."""
        body: dict = {"status": status}
        if rejected_reason is not None:
            body["rejected_reason"] = rejected_reason
        if mission_id is not None:
            body["mission_id"] = mission_id
        out = self._sync_call(
            "PATCH", "/rest/v1/commands", {"id": f"eq.{command_id}"}, body,
            what=f"command {command_id} -> {status}",
        )
        return out.ok

    def link_command_to_mission(self, command_id: str, mission_id: str) -> bool:
        """Set a command's mission_id."""
        out = self._sync_call(
            "PATCH", "/rest/v1/commands", {"id": f"eq.{command_id}"},
            {"mission_id": mission_id},
            what=f"link command {command_id} -> mission {mission_id}",
        )
        return out.ok

    def create_mission(
        self,
        *,
        drone_id: str,
        mission_type: str,
        dock_id: str | None = None,
        target_lat: float | None = None,
        target_lon: float | None = None,
        cruise_alt_m: float | None = None,
        triggered_by: str | None = None,
    ) -> str | None:
        """INSERT a missions row (status='queued'); return its id or None."""
        body = {
            "drone_id": drone_id,
            "dock_id": dock_id,
            "type": mission_type,
            "status": "queued",
            "target_lat": target_lat,
            "target_lon": target_lon,
            "cruise_alt_m": cruise_alt_m,
            "triggered_by": triggered_by,
        }
        out = self._sync_call(
            "POST", "/rest/v1/missions", None, body,
            prefer="return=representation", what="create_mission",
        )
        if not out.ok:
            return None
        try:
            rows = json.loads(out.body)
            return rows[0]["id"]
        except (json.JSONDecodeError, IndexError, KeyError):
            log.error("store: create_mission returned no id: %r", out.body[:200])
            return None

    def update_mission(self, mission_id: str, **fields: object) -> bool:
        """PATCH a missions row. Server triggers stamp started_at / ended_at."""
        out = self._sync_call(
            "PATCH", "/rest/v1/missions", {"id": f"eq.{mission_id}"}, dict(fields),
            what=f"mission {mission_id} update {list(fields)}",
        )
        return out.ok

    def list_pending_command_ids(self, drone_id: str) -> list[str]:
        """The poller's query: ids of pending commands for this drone."""
        out = self._sync_call(
            "GET", "/rest/v1/commands",
            {
                "drone_id": f"eq.{drone_id}",
                "status": "eq.pending",
                "select": "id",
                "order": "issued_at.asc",
            },
            None, attempts=3, timeout=6.0, what="poll pending commands",
        )
        if not out.ok:
            return []
        try:
            return [row["id"] for row in json.loads(out.body)]
        except (json.JSONDecodeError, KeyError, TypeError):
            log.error("store: pending-commands query returned junk: %r", out.body[:200])
            return []

    def list_active_missions(self, drone_id: str) -> list[dict]:
        """Missions for this drone that are not in a terminal state."""
        out = self._sync_call(
            "GET", "/rest/v1/missions",
            {
                "drone_id": f"eq.{drone_id}",
                "status": "not.in.(landed,aborted)",
                "select": "id,status,type,created_at",
            },
            None, attempts=3, timeout=6.0, what="list active missions",
        )
        if not out.ok:
            return []
        try:
            return list(json.loads(out.body))
        except json.JSONDecodeError:
            return []

    # ------------------------------------------------------------ drops

    def _log_drops(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_drop_log_mono < _DROP_LOG_INTERVAL_S:
            return
        window = now - self._last_drop_log_mono
        self._last_drop_log_mono = now
        dropped = self._queue.take_dropped()
        if dropped:
            self._total_dropped += dropped
            log.warning(
                "store: dropped %d queued update(s) in the last %.0fs "
                "(Supabase slow or unreachable); %d dropped total",
                dropped, window, self._total_dropped,
            )


def _iso(dt) -> str:
    """ISO-8601 UTC string PostgREST accepts for timestamptz."""
    return dt.isoformat()


def _distance_m(
    lat1: float | None, lon1: float | None, lat2: float | None, lon2: float | None
) -> float | None:
    """Rough planar distance in metres, or None if either point is missing."""
    if None in (lat1, lon1, lat2, lon2):
        return None
    dlat = (lat2 - lat1) * _METERS_PER_DEG_LAT
    dlon = (lon2 - lon1) * _METERS_PER_DEG_LAT * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dlat, dlon)


def _read_error(exc: urllib.error.HTTPError) -> str:
    """Best-effort body text from an HTTPError, truncated."""
    try:
        return exc.read().decode("utf-8", "replace")[:300]
    except Exception:
        return f"HTTP {exc.code}"
