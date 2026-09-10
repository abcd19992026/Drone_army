"""v0.3 command-intake verification, against the real Supabase project in .env.

Needs SITL-less telemetry (tests/fake_vehicle.py) and a reachable Supabase
(the ``.env`` project). Each test wipes commands/missions/mission_events for
the drone first, so it is safe to re-run.

    python -m tests.test_commands
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

from dotenv import dotenv_values

_ENV = dotenv_values(".env")
for _k, _v in _ENV.items():
    if _v:
        os.environ.setdefault(_k, _v)

if not os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
    print("SKIP: no .env with SUPABASE_SERVICE_ROLE_KEY -- these tests need the real project")
    sys.exit(0)

BASE = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
DRONE = os.environ["DRONE_ID"]
FOREIGN_DRONE = "d5030000-0000-4000-8000-0000000000ff"

from gss.commands import CommandIntake  # noqa: E402
from gss.link import MavlinkLink  # noqa: E402
from gss.store import TelemetryStore  # noqa: E402
from gss.telemetry import TelemetryReader  # noqa: E402
from tests.fake_vehicle import FakeVehicle  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  ::  {detail}" if detail else ""))


def _rest(method, path, body=None, params=None, prefer="return=representation"):
    url = BASE + path
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={"apikey": KEY, "Authorization": "Bearer " + KEY,
                 "Content-Type": "application/json", "Prefer": prefer},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            txt = r.read().decode()
            return json.loads(txt) if txt.strip() else None
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode()[:300]}") from e


def _wipe():
    _rest("PATCH", "/rest/v1/missions",
          {"status": "aborted", "abort_reason": "test cleanup"},
          {"drone_id": f"in.({DRONE},{FOREIGN_DRONE})", "status": "not.in.(landed,aborted)"})
    _rest("DELETE", "/rest/v1/mission_events", params={"id": "not.is.null"})
    _rest("DELETE", "/rest/v1/commands", params={"drone_id": f"in.({DRONE},{FOREIGN_DRONE})"})
    _rest("DELETE", "/rest/v1/missions", params={"drone_id": f"in.({DRONE},{FOREIGN_DRONE})"})


def _insert_command(**kw):
    row = {"drone_id": DRONE, "type": "summon", "status": "pending",
           "issued_by": "test_commands", **kw}
    return _rest("POST", "/rest/v1/commands", row)[0]


def _command(cid, select="status,rejected_reason,mission_id,acked_at,completed_at"):
    rows = _rest("GET", "/rest/v1/commands", params={"id": f"eq.{cid}", "select": select})
    return rows[0] if rows else None


def _events(mission_id):
    return _rest("GET", "/rest/v1/mission_events",
                 params={"mission_id": f"eq.{mission_id}",
                         "select": "event,battery_pct,battery_voltage_v,link_up,at",
                         "order": "at.asc"}) or []


def _mission(mid):
    rows = _rest("GET", "/rest/v1/missions",
                 params={"id": f"eq.{mid}", "select": "status,type,abort_reason,started_at,ended_at"})
    return rows[0] if rows else None


def _wait(pred, timeout, poll=0.3):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(poll)
    return pred()


class _Rig:
    """A fake vehicle + link + store + intake, wired for one test."""

    def __init__(self, *, connected=True, fix_type=3, realtime_enabled=True, port=5799):
        self._port = port
        self.fv = FakeVehicle(port=port)
        self.fv.set_position(lat=25.90, lon=85.90, fix_type=fix_type, satellites=11)
        self.fv.set_battery(pct=73, voltage_v=23.4)
        self.fv.start()
        self.fv.wait_for_client(2)
        self.link = MavlinkLink(connection_string=f"tcp:127.0.0.1:{port}", heartbeat_timeout_s=4)
        self.reader = TelemetryReader(self.link)
        self.link.connect(timeout_s=8)
        time.sleep(3 if connected else 0)
        self.store = TelemetryStore(self.reader.get_snapshot)
        self.intake = CommandIntake(
            self.store, self.reader.get_snapshot,
            home_lat=25.5932, home_lon=85.2045,
            realtime_enabled=realtime_enabled,
        )
        self.intake.start()
        time.sleep(2)

    def close(self):
        self.intake.close(4)
        self.link.close()
        self.fv.stop()


# --------------------------------------------------------------------- tests

def test_1_valid_summon():
    _wipe()
    rig = _Rig()
    try:
        cmd = _insert_command(target_lat=25.60, target_lon=85.21, target_alt_m=30)
        _wait(lambda: _command(cmd["id"])["status"] == "done", 25)
        row = _command(cmd["id"])
        _check("1: command reaches 'done'", row["status"] == "done", row["status"])
        _check("1: acked_at + completed_at stamped",
               bool(row["acked_at"]) and bool(row["completed_at"]))
        mid = row["mission_id"]
        _check("1: mission linked", bool(mid))
        m = _mission(mid)
        _check("1: mission landed, type=summon, started+ended stamped",
               m["status"] == "landed" and m["type"] == "summon"
               and bool(m["started_at"]) and bool(m["ended_at"]), str(m))
        evs = _events(mid)
        names = [e["event"] for e in evs]
        _check("1: full event sequence written",
               names == ["mission_created", "armed", "takeoff", "enroute",
                         "arrived", "loiter_start", "rtl", "landed"], str(names))
        _check("1: every event has battery + voltage stamped",
               all(e["battery_pct"] == 73 and e["battery_voltage_v"] == 23.4
                   and e["link_up"] is True for e in evs))
    finally:
        rig.close()


def test_2_out_of_range():
    _wipe()
    rig = _Rig()
    try:
        cmd = _insert_command(target_lat=25.5932 + 0.18, target_lon=85.2045)  # ~20 km N
        _wait(lambda: _command(cmd["id"])["status"] == "rejected", 12)
        row = _command(cmd["id"])
        _check("2: rejected", row["status"] == "rejected", row["status"])
        r = (row["rejected_reason"] or "")
        _check("2: reason names the distance and the limit",
               "km from home" in r and "limit is" in r, r)
        _check("2: no mission created", row["mission_id"] is None)
    finally:
        rig.close()


def test_3_expired():
    _wipe()
    rig = _Rig()
    try:
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        cmd = _insert_command(target_lat=25.60, target_lon=85.21, expires_at=past)
        _wait(lambda: _command(cmd["id"])["status"] == "rejected", 12)
        row = _command(cmd["id"])
        _check("3: rejected", row["status"] == "rejected", row["status"])
        _check("3: reason says expired, with times",
               "expired" in (row["rejected_reason"] or "").lower()
               and "database time" in (row["rejected_reason"] or ""), row["rejected_reason"])
        _check("3: no mission", row["mission_id"] is None)
    finally:
        rig.close()


def test_4_foreign_drone():
    _wipe()
    _rest("POST", "/rest/v1/drones",
          {"id": FOREIGN_DRONE, "name": "Other-Drone", "home_dock_id": os.environ["DOCK_ID"]},
          prefer="resolution=merge-duplicates,return=minimal")
    rig = _Rig()
    try:
        cmd = _rest("POST", "/rest/v1/commands",
                    {"drone_id": FOREIGN_DRONE, "type": "summon", "status": "pending",
                     "issued_by": "test_commands", "target_lat": 25.60, "target_lon": 85.21})[0]
        # give the intake several poll cycles + realtime a chance
        time.sleep(10)
        row = _command(cmd["id"])
        _check("4: foreign command left 'pending' (not claimed)", row["status"] == "pending", row["status"])
        _check("4: foreign command NOT rejected", row["rejected_reason"] is None)
    finally:
        rig.close()
        _rest("DELETE", "/rest/v1/commands", params={"drone_id": f"eq.{FOREIGN_DRONE}"})
        _rest("DELETE", "/rest/v1/drones", params={"id": f"eq.{FOREIGN_DRONE}"})


def test_5_second_summon_while_running():
    _wipe()
    rig = _Rig()
    try:
        c1 = _insert_command(target_lat=25.60, target_lon=85.21)
        _wait(lambda: _command(c1["id"])["status"] in ("executing", "done"), 10)
        _check("5: first command executing", _command(c1["id"])["status"] == "executing",
               _command(c1["id"])["status"])
        c2 = _insert_command(target_lat=25.61, target_lon=85.22)
        _wait(lambda: _command(c2["id"])["status"] == "rejected", 10)
        r = _command(c2["id"])
        _check("5: second command rejected", r["status"] == "rejected", r["status"])
        _check("5: reason says a mission is already running",
               "already running" in (r["rejected_reason"] or ""), r["rejected_reason"])
        _check("5: first command unaffected (still executing or done)",
               _command(c1["id"])["status"] in ("executing", "done"))
        _wait(lambda: _command(c1["id"])["status"] == "done", 20)
        _check("5: first mission completes normally", _command(c1["id"])["status"] == "done")
    finally:
        rig.close()


def test_6_duplicate_exactly_once():
    _wipe()
    rig = _Rig(realtime_enabled=False)  # drive both deliveries by hand
    try:
        cmd = _insert_command(target_lat=25.60, target_lon=85.21)
        # deliver the same id via "realtime" and "poll" nearly simultaneously
        rig.intake.submit(cmd["id"])
        rig.intake.submit(cmd["id"])
        time.sleep(0.05)
        rig.intake.submit(cmd["id"])
        _wait(lambda: _command(cmd["id"])["status"] == "done", 25)
        mid = _command(cmd["id"])["mission_id"]
        missions = _rest("GET", "/rest/v1/missions",
                         params={"drone_id": f"eq.{DRONE}", "select": "id"})
        _check("6: exactly one mission created", len(missions) == 1, f"{len(missions)} missions")
        names = [e["event"] for e in _events(mid)]
        _check("6: each lifecycle event appears exactly once",
               names.count("takeoff") == 1 and names.count("landed") == 1
               and names.count("mission_created") == 1, str(names))
    finally:
        rig.close()


def test_7_stale_telemetry():
    _wipe()
    rig = _Rig()
    try:
        rig.fv.disconnect_abrupt()  # kill the link
        _wait(lambda: not rig.reader.get_snapshot().connected, 8)
        cmd = _insert_command(target_lat=25.60, target_lon=85.21)
        _wait(lambda: _command(cmd["id"])["status"] == "rejected", 12)
        r = _command(cmd["id"])
        _check("7: rejected while link is down", r["status"] == "rejected", r["status"])
        _check("7: reason cites the link / stale telemetry",
               "blind" in (r["rejected_reason"] or "").lower(), r["rejected_reason"])
        _check("7: no mission", r["mission_id"] is None)
    finally:
        rig.close()


def test_8_abort_during_run():
    _wipe()
    rig = _Rig()
    try:
        c1 = _insert_command(target_lat=25.60, target_lon=85.21)
        _wait(lambda: _command(c1["id"])["status"] == "executing", 10)
        mid = _command(c1["id"])["mission_id"]
        _wait(lambda: _mission(mid)["status"] in ("enroute", "on_station"), 8)
        t0 = time.time()
        ca = _rest("POST", "/rest/v1/commands",
                   {"drone_id": DRONE, "type": "abort", "status": "pending",
                    "issued_by": "test_commands"})[0]
        _wait(lambda: _mission(mid)["status"] == "aborted", 5)
        dt = time.time() - t0
        m = _mission(mid)
        _check("8: mission aborted", m["status"] == "aborted", m["status"])
        _check("8: abort_reason recorded", bool(m["abort_reason"]) and "abort" in m["abort_reason"].lower(),
               m["abort_reason"])
        _check("8: abort took effect promptly (< 4s incl. poll)", dt < 4.0, f"{dt:.1f}s")
        _wait(lambda: _command(ca["id"])["status"] == "done", 6)
        _check("8: abort command -> done", _command(ca["id"])["status"] == "done")
        _check("8: originating command -> done", _command(c1["id"])["status"] == "done")
        names = [e["event"] for e in _events(mid)]
        _check("8: 'aborted' event written once, no 'landed'",
               names.count("aborted") == 1 and "landed" not in names, str(names))
    finally:
        rig.close()


def test_9_clock_skew_simulated():
    """Prove expiry is DB-clock based: warp the local clock, results unchanged."""
    _wipe()
    import gss.commands as cmod
    real_parse = cmod._parse_ts

    # 1) structural: the expiry path must not touch the local clock
    import inspect
    src = inspect.getsource(cmod.CommandIntake._effective_expiry) + \
        inspect.getsource(cmod.CommandIntake._expiry_reason) + \
        inspect.getsource(cmod.CommandIntake._validate_flight)
    _check("9: expiry code contains no time.time()/datetime.now()",
           "time.time(" not in src and "datetime.now(" not in src
           and ".now()" not in src, "found a local-clock call in the expiry path")

    # 2) behavioural: monkeypatch time + datetime to a wildly wrong local clock,
    #    then run the expired-command case (should still reject) and a fresh one
    #    (should still accept). Only the DB's server_now matters.
    import datetime as _dt

    class _WarpDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _dt.datetime(2020, 1, 1, tzinfo=tz or timezone.utc)

    orig_dt = cmod.datetime
    orig_time = time.time
    cmod.datetime = _WarpDateTime
    time.time = lambda: 1_577_836_800.0  # 2020-01-01
    rig = None
    try:
        rig = _Rig()
        past = "2026-09-10T00:00:00+00:00"  # already in the past by server time
        c_exp = _insert_command(target_lat=25.60, target_lon=85.21, expires_at=past)
        c_ok = _insert_command(target_lat=25.61, target_lon=85.22)
        _wait(lambda: _command(c_exp["id"])["status"] == "rejected", 12)
        _wait(lambda: _command(c_ok["id"])["status"] in ("executing", "done"), 12)
        _check("9: expired command still rejected despite 2020 local clock",
               _command(c_exp["id"])["status"] == "rejected",
               _command(c_exp["id"])["status"])
        _check("9: fresh command still accepted despite 2020 local clock",
               _command(c_ok["id"])["status"] in ("executing", "done"),
               _command(c_ok["id"])["status"])
        _check("9: rejection reason quotes the DB's clock, not 2020",
               "2026" in (_command(c_exp["id"])["rejected_reason"] or ""),
               _command(c_exp["id"])["rejected_reason"])
    finally:
        cmod.datetime = orig_dt
        time.time = orig_time
        cmod._parse_ts = real_parse
        if rig:
            rig.close()


def test_10_poller_only():
    _wipe()
    rig = _Rig(realtime_enabled=False)
    try:
        _check("10: realtime is disabled for this rig",
               rig.intake.realtime_status == "disabled")
        cmd = _insert_command(target_lat=25.60, target_lon=85.21)
        # no submit() call -- only the poller can find it
        picked = _wait(lambda: _command(cmd["id"])["status"] != "pending", 8)
        _check("10: poller picked the command up within a few cycles", picked,
               _command(cmd["id"])["status"])
        _wait(lambda: _command(cmd["id"])["status"] == "done", 25)
        _check("10: poller-delivered command completes normally",
               _command(cmd["id"])["status"] == "done")
    finally:
        rig.close()


def main() -> int:
    tests = [
        test_1_valid_summon, test_2_out_of_range, test_3_expired,
        test_4_foreign_drone, test_5_second_summon_while_running,
        test_6_duplicate_exactly_once, test_7_stale_telemetry,
        test_8_abort_during_run, test_9_clock_skew_simulated, test_10_poller_only,
    ]
    for t in tests:
        print(f"\n--- {t.__name__} ---")
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            _check(t.__name__ + " (crashed)", False, repr(exc))
    _wipe()
    failed = [n for n, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
