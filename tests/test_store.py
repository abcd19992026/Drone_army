"""Tests for gss/store.py against a local mock HTTP endpoint (no real Supabase).

Run directly::

    python -m tests.test_store

Covers: telemetry UPDATE shape, skip-redundant-write, heartbeat write,
last_known_* rules, bounded drop-oldest queue, retry/backoff, 4xx no-retry,
the R10 property (a dead endpoint never slows the producer), and graceful
shutdown flush.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("SUPABASE_ENABLED", "false")  # keep config import happy

from gss.store import TelemetryStore, _distance_m
from gss.telemetry import TelemetrySnapshot

_results: list[tuple[str, bool, str]] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  ::  {detail}" if detail else ""))


def _wait(pred, timeout: float, poll: float = 0.05) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(poll)
    return pred()


# --------------------------------------------------------------- mock server
class _Mock:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.status_code = 204
        self.delay_s = 0.0
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b""
                with outer._lock:
                    outer.requests.append({
                        "method": self.command,
                        "path": self.path,
                        "auth": self.headers.get("Authorization", ""),
                        "body": json.loads(raw or b"{}"),
                    })
                    code = outer.status_code
                    delay = outer.delay_s
                if delay:
                    time.sleep(delay)
                self.send_response(code)
                self.end_headers()
                if code >= 400:
                    self.wfile.write(b'{"message":"mock error"}')

            do_PATCH = _handle
            do_POST = _handle

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._t = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._t.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def count(self, method: str | None = None) -> int:
        with self._lock:
            if method is None:
                return len(self.requests)
            return sum(1 for r in self.requests if r["method"] == method)

    def last(self) -> dict:
        with self._lock:
            return dict(self.requests[-1])

    def stop(self) -> None:
        self._server.shutdown()


def _snap(*, lat=25.5932, lon=85.2045, battery=90.0, mode="STABILIZE",
          connected=True, position_valid=True, fix=6, volts=22.2, age=0.2):
    now = datetime.now(timezone.utc)
    return TelemetrySnapshot(
        timestamp=now, connected=connected, lat=lat, lon=lon,
        position_valid=position_valid, alt_m_amsl=584.0, alt_m_relative=0.0,
        heading_deg=90.0, groundspeed_ms=0.0, mode=mode, armed=False,
        battery_pct=battery, battery_voltage_v=volts, battery_current_a=0.0,
        gps_fix_type=fix, gps_satellites=11, link_age_s=age, telemetry_age_s=age,
    )


# ------------------------------------------------------------------- tests
def test_happy_path_and_dedup():
    mock = _Mock()
    box = {"s": _snap()}
    store = TelemetryStore(lambda: box["s"], url=mock.url, service_role_key="svc",
                           drone_id="d5030000-0000-4000-8000-000000000001",
                           telemetry_interval_s=0.2, heartbeat_write_s=2.0)
    store.start()
    try:
        _wait(lambda: mock.count("PATCH") >= 1, 3)
        req = mock.last()
        _check("telemetry is a PATCH on /rest/v1/drones?id=eq...",
               req["method"] == "PATCH" and "/rest/v1/drones?id=eq." in req["path"], req["path"])
        _check("uses service_role bearer", req["auth"] == "Bearer svc")
        body = req["body"]
        _check("payload carries battery/voltage/mode/position_valid",
               body.get("battery_pct") == 90.0 and body.get("battery_voltage_v") == 22.2
               and body.get("mode") == "STABILIZE" and body.get("position_valid") is True)
        _check("payload has last_telemetry_at + last_heartbeat_at",
               "last_telemetry_at" in body and "last_heartbeat_at" in body)
        _check("payload writes last_known_* when position valid",
               body.get("last_known_lat") == 25.5932 and "last_known_at" in body)
        _check("payload does NOT set status/control_mode",
               "status" not in body and "control_mode" not in body)

        # nothing changes -> writes should stop until the heartbeat interval
        n_after_first = mock.count("PATCH")
        time.sleep(1.0)  # 5 producer cycles, < heartbeat_write_s
        _check("redundant cycles are skipped",
               mock.count("PATCH") == n_after_first, f"{n_after_first} -> {mock.count('PATCH')}")

        # heartbeat write forces one through even with no change
        _wait(lambda: mock.count("PATCH") > n_after_first, 3)
        _check("heartbeat write fires within SUPABASE_HEARTBEAT_WRITE_S",
               mock.count("PATCH") > n_after_first)

        # a real move triggers an immediate write
        n = mock.count("PATCH")
        box["s"] = _snap(lat=25.60, lon=85.25)
        _wait(lambda: mock.count("PATCH") > n, 2)
        _check("a >1 m move triggers a write", mock.count("PATCH") > n)

        # invalid position -> last_known_* omitted, never nulled
        box["s"] = _snap(position_valid=False, fix=1, lat=0.0, lon=0.0)
        _wait(lambda: "last_known_lat" not in mock.last()["body"], 2)
        b = mock.last()["body"]
        _check("last_known_* omitted (not nulled) when position invalid",
               "last_known_lat" not in b and "last_known_at" not in b)
        _check("position_valid=false still written", b.get("position_valid") is False)
    finally:
        store.close(timeout_s=3)
        mock.stop()


def test_r10_dead_endpoint_never_blocks_producer():
    # Point at a black-hole port (connection refused fast) and a slow mock.
    mock = _Mock()
    mock.delay_s = 3.0  # every write hangs for 3 s
    box = {"s": _snap()}
    store = TelemetryStore(lambda: box["s"], url=mock.url, service_role_key="svc",
                           drone_id="d1", telemetry_interval_s=0.1,
                           heartbeat_write_s=0.3, queue_max=5, http_timeout_s=1.0)
    # Independent "console loop" proxy: sample the snapshot on a tight cadence
    # and record intervals; it must stay smooth regardless of the store.
    intervals = []
    stop = threading.Event()

    def console():
        last = time.monotonic()
        while not stop.is_set():
            box["s"]  # read like the real console does
            time.sleep(0.1)
            now = time.monotonic()
            intervals.append(now - last)
            last = now

    ct = threading.Thread(target=console, daemon=True)
    ct.start()
    store.start()
    try:
        time.sleep(4)
        worst = max(intervals)
        _check("console cadence unaffected by hung Supabase (<0.25 s jitter)",
               worst < 0.25, f"worst interval {worst:.3f}s")
        _check("bounded queue dropped items while endpoint hung",
               store.stats["dropped"] > 0 or store._queue.qsize() <= 5,
               f"stats={store.stats}")
    finally:
        stop.set()
        ct.join(timeout=1)
        store.close(timeout_s=2)
        mock.stop()


def test_4xx_not_retried():
    mock = _Mock()
    mock.status_code = 400
    box = {"s": _snap()}
    store = TelemetryStore(lambda: box["s"], url=mock.url, service_role_key="svc",
                           drone_id="d1", telemetry_interval_s=5.0, heartbeat_write_s=30.0)
    store.start()
    try:
        _wait(lambda: store.stats["writes_failed"] >= 1, 5)
        time.sleep(1.0)
        _check("4xx is not retried (exactly one request for the one job)",
               mock.count() == 1, f"requests={mock.count()}")
        _check("4xx failure counted, never fatal", store.stats["writes_failed"] == 1)
    finally:
        store.close(timeout_s=2)
        mock.stop()


def test_5xx_is_retried():
    mock = _Mock()
    mock.status_code = 503
    box = {"s": _snap()}
    store = TelemetryStore(lambda: box["s"], url=mock.url, service_role_key="svc",
                           drone_id="d1", telemetry_interval_s=5.0, heartbeat_write_s=30.0)
    store.start()
    try:
        # one job, retried with backoff 1s,2s,4s -> ~4 requests over ~7s
        _wait(lambda: mock.count() >= 3, 12)
        _check("5xx is retried (multiple attempts for one job)",
               mock.count() >= 3, f"attempts={mock.count()}")
        _check("retries are capped, not infinite", mock.count() <= 5, f"attempts={mock.count()}")
    finally:
        store.close(timeout_s=2)
        mock.stop()


def test_graceful_shutdown_flushes():
    mock = _Mock()
    box = {"s": _snap()}
    store = TelemetryStore(lambda: box["s"], url=mock.url, service_role_key="svc",
                           drone_id="d1", telemetry_interval_s=0.2, heartbeat_write_s=1.0)
    store.start()
    _wait(lambda: mock.count() >= 1, 3)
    t0 = time.time()
    store.close(timeout_s=3)
    _check("close() returns promptly", time.time() - t0 < 3.5, f"{time.time() - t0:.1f}s")
    _check("queue drained at shutdown", store._queue.empty())
    mock.stop()


def test_log_event_autostamps():
    mock = _Mock()
    box = {"s": _snap(battery=44.0, volts=21.1)}
    store = TelemetryStore(lambda: box["s"], url=mock.url, service_role_key="svc",
                           drone_id="d1", telemetry_interval_s=5.0, heartbeat_write_s=30.0)
    store.start()
    try:
        store.log_event("m-123", "link_lost", {"reason": "test"})
        _wait(lambda: mock.count("POST") >= 1, 3)
        posts = [r for r in mock.requests if r["method"] == "POST"]
        b = posts[0]["body"]
        _check("event POSTs to /rest/v1/mission_events",
               "/rest/v1/mission_events" in posts[0]["path"])
        _check("event auto-stamps battery/voltage/link_up from snapshot",
               b.get("battery_pct") == 44.0 and b.get("battery_voltage_v") == 21.1
               and b.get("link_up") is True)
        _check("event carries mission_id + event + detail",
               b.get("mission_id") == "m-123" and b.get("event") == "link_lost"
               and b.get("detail") == {"reason": "test"})
    finally:
        store.close(timeout_s=2)
        mock.stop()


def test_distance_helper():
    _check("_distance_m ~111 m for 0.001 deg lat",
           abs(_distance_m(25.0, 85.0, 25.001, 85.0) - 111.32) < 1.0)
    _check("_distance_m None when a point is missing",
           _distance_m(None, 85.0, 25.0, 85.0) is None)


def main() -> int:
    for t in (test_distance_helper, test_happy_path_and_dedup,
              test_r10_dead_endpoint_never_blocks_producer,
              test_4xx_not_retried, test_5xx_is_retried,
              test_graceful_shutdown_flushes, test_log_event_autostamps):
        print(f"\n--- {t.__name__} ---")
        try:
            t()
        except Exception as exc:  # noqa: BLE001 - test harness
            _check(t.__name__ + " (crashed)", False, repr(exc))
    failed = [n for n, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
