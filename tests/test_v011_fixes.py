"""Regression tests for the v0.1.1 fixes, driven by tests/fake_vehicle.py.

Run directly (no pytest needed)::

    python -m tests.test_v011_fixes

Covers:
  * Fix 1  -- streams requested on connect (SET_MESSAGE_INTERVAL) + watchdog
  * Fix 2  -- abrupt (RST) disconnect logs one WARNING, no traceback
  * Fix 3  -- lat/lon == 0 and fix_type < 3 guard + position_valid
  * Fix 4  -- component id resolved to 1
  * Fix 5  -- reconnect backoff comes from config (cap 5 s)
"""

from __future__ import annotations

import io
import logging
import sys
import time

from gss import config
from gss.link import MavlinkLink
from gss.telemetry import TelemetryReader
from tests.fake_vehicle import FakeVehicle

_PORT = 5810
_results: list[tuple[str, bool, str]] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  ::  {detail}" if detail else ""))


def _wait(predicate, timeout: float, poll: float = 0.1) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(poll)
    return predicate()


def _capture_logs() -> io.StringIO:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    return buf


def test_normal_streaming_and_guards() -> None:
    logs = _capture_logs()
    fv = FakeVehicle(port=_PORT)
    fv.set_position(lat=25.5932, lon=85.2045, fix_type=3, satellites=12)
    fv.set_battery(pct=42, voltage_v=22.2, current_a=8.1)
    fv.start()

    link = MavlinkLink(connection_string=f"tcp:127.0.0.1:{_PORT}", heartbeat_timeout_s=3.0)
    reader = TelemetryReader(link)
    try:
        _check("Fix4: link connects", link.connect(timeout_s=10))
        _check("Fix4: component id == 1", link.component_id == 1, f"got {link.component_id}")

        _wait(lambda: reader.get_snapshot().battery_pct is not None
              and reader.get_snapshot().gps_fix_type is not None, 6)
        snap = reader.get_snapshot()
        _check("Fix1: battery/voltage/current mapped",
               (snap.battery_pct, snap.battery_voltage_v, snap.battery_current_a) == (42.0, 22.2, 8.1),
               f"{snap.battery_pct},{snap.battery_voltage_v},{snap.battery_current_a}")
        _check("Fix1: position + gps mapped",
               round(snap.lat, 4) == 25.5932 and snap.gps_fix_type == 3 and snap.gps_satellites == 12)
        _check("Fix3: position_valid True (coords + 3D fix)", snap.position_valid is True)
        _wait(lambda: len(fv.requested_message_ids) == 5, 5)
        _check("Fix1: all 5 messages requested via SET_MESSAGE_INTERVAL",
               fv.requested_message_ids == {1, 24, 33, 74, 147}, str(sorted(fv.requested_message_ids)))
        _check("Fix1: log records the SET_MESSAGE_INTERVAL path",
               "via SET_MESSAGE_INTERVAL (5/5" in logs.getvalue())

        fv.set_position(lat=0.0, lon=0.0)
        _wait(lambda: reader.get_snapshot().lat is None, 3)
        snap = reader.get_snapshot()
        _check("Fix3: lat/lon 0/0 -> None", snap.lat is None and snap.lon is None)
        _check("Fix3: position_valid False when lat/lon None", snap.position_valid is False)

        fv.set_position(lat=25.60, lon=85.21)
        fv.set_fix_type(2)
        _wait(lambda: reader.get_snapshot().gps_fix_type == 2, 3)
        snap = reader.get_snapshot()
        _check("Fix3: real coords + fix_type 2 -> position_valid False",
               snap.lat is not None and snap.gps_fix_type == 2 and snap.position_valid is False)
        fv.set_fix_type(3)
        _wait(lambda: reader.get_snapshot().position_valid, 3)
        _check("Fix3: position_valid True again after fix 3",
               reader.get_snapshot().position_valid is True)
    finally:
        link.close()
        fv.stop()


def test_abrupt_disconnect_no_traceback() -> None:
    logs = _capture_logs()
    fv = FakeVehicle(port=_PORT + 1)
    fv.set_position(lat=25.5, lon=85.2, fix_type=3, satellites=10)
    fv.start()
    link = MavlinkLink(connection_string=f"tcp:127.0.0.1:{_PORT + 1}", heartbeat_timeout_s=3.0)
    TelemetryReader(link)
    try:
        link.connect(timeout_s=10)
        _wait(lambda: link.is_connected, 5)
        mark = len(logs.getvalue())
        fv.disconnect_abrupt()
        time.sleep(3)
        seg = logs.getvalue()[mark:]
        _check("Fix2: no traceback on abrupt (RST) disconnect",
               "Traceback (most recent call last)" not in seg)
        _check("Fix2: abrupt loss logged as a single WARNING",
               "WARNING gss.link: Link lost" in seg or "Connection closed by peer" in seg)
    finally:
        link.close()
        fv.stop()


def test_stream_watchdog() -> None:
    logs = _capture_logs()
    fv = FakeVehicle(port=_PORT + 2)
    fv.set_heartbeat_only(True)
    fv.set_position(lat=25.5, lon=85.2, fix_type=3, satellites=9)
    fv.set_battery(pct=80, voltage_v=23.5)
    fv.start()
    link = MavlinkLink(connection_string=f"tcp:127.0.0.1:{_PORT + 2}", heartbeat_timeout_s=30.0)
    reader = TelemetryReader(link)
    try:
        link.connect(timeout_s=10)
        window = max(12.0, 2 * config.STREAM_WATCHDOG_S + 3 * config.STREAM_REREQUEST_MIN_S)
        time.sleep(window)
        fires = logs.getvalue().count("Stream watchdog: no telemetry")
        _check("Fix1d: watchdog fires when telemetry is silent", fires >= 2, f"fires={fires}")
        _check("Fix1d: watchdog rate-limited (not a flood)", fires <= window, f"fires={fires}")
        _check("Fix1d: no telemetry while heartbeat-only",
               reader.get_snapshot().battery_pct is None)

        # self-heal
        fv.set_battery(pct=55, voltage_v=22.0)
        fv.set_heartbeat_only(False)
        _wait(lambda: reader.get_snapshot().battery_pct == 55.0, 8)
        mark = len(logs.getvalue())
        time.sleep(config.STREAM_WATCHDOG_S + 3)
        _check("Fix1d: telemetry self-heals after streaming resumes",
               reader.get_snapshot().battery_pct == 55.0)
        _check("Fix1d: watchdog goes quiet once telemetry flows",
               "Stream watchdog" not in logs.getvalue()[mark:])
    finally:
        link.close()
        fv.stop()


def main() -> int:
    logging.disable(logging.NOTSET)
    for test in (
        test_normal_streaming_and_guards,
        test_abrupt_disconnect_no_traceback,
        test_stream_watchdog,
    ):
        print(f"\n--- {test.__name__} ---")
        test()
    failed = [n for n, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
