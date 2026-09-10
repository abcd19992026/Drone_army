"""safety.py verification -- the module tested harder than any other.

Three layers:

  * a SCENARIO TABLE driven through the pure core (no I/O, no threads) -- one
    row per named situation, the state going in, the verdict expected out;
  * SHELL tests for :class:`gss.safety.SafetyMonitor` (timers, latch, the
    self-watchdog thread) against a hand-driven snapshot source, still no
    network;
  * the IMPORT-BOUNDARY test (rule R1): a fresh interpreter imports
    ``gss.safety`` and nothing networked comes with it;
  * LIVE tests (fake vehicle + the real Supabase project in .env) mirroring
    the phase's verification list -- skipped automatically without .env.

    python -m tests.test_safety
"""

from __future__ import annotations

import io
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone

os.environ.setdefault("SUPABASE_ENABLED", "false")  # pure/shell layers: no network

from gss import config  # noqa: E402
from gss import safety  # noqa: E402
from gss.safety import (  # noqa: E402
    SafetyAction,
    SafetyContext,
    SafetyMonitor,
    SafetySeverity,
)
from gss.snapshot import TelemetrySnapshot  # noqa: E402

_results: list[tuple[str, bool, str]] = []
_NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)


def _check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  ::  {detail}" if detail else ""))


# --------------------------------------------------------------------------
# snapshot builder -- a healthy cruising drone; override any field per row
# --------------------------------------------------------------------------

_HOME = (config.HOME_LAT, config.HOME_LON)
# ~950 m from home: close enough that the default point-of-no-return budget
# (~30%) is comfortably below a healthy pack.
_POS = (config.HOME_LAT + 0.007, config.HOME_LON + 0.0055)


def snap(**over) -> TelemetrySnapshot:
    base = dict(
        timestamp=_NOW,
        connected=True,
        lat=_POS[0],
        lon=_POS[1],
        position_valid=True,
        alt_m_amsl=600.0,
        alt_m_relative=28.0,
        heading_deg=90.0,
        groundspeed_ms=9.0,
        mode="GUIDED",
        armed=True,
        battery_pct=85.0,
        battery_voltage_v=24.0,
        battery_current_a=12.0,
        gps_fix_type=3,
        gps_satellites=13,
        link_age_s=0.4,
        telemetry_age_s=0.3,
        position_age_s=0.3,
        battery_age_s=0.4,
        attitude_age_s=0.3,
        gps_age_s=0.6,
        gps_hdop=0.9,
    )
    base.update(over)
    return TelemetrySnapshot(**base)


_CRUISE_CTX = SafetyContext(
    preflight=False,
    phase="on_station",
    mission_id="m-test",
    is_summon=True,
    target_lat=_POS[0],
    target_lon=_POS[1],
    home_lat=_HOME[0],
    home_lon=_HOME[1],
)
_PREFLIGHT_CTX = SafetyContext(
    preflight=True,
    is_summon=True,
    target_lat=_POS[0],
    target_lon=_POS[1],
    home_lat=_HOME[0],
    home_lon=_HOME[1],
)

# A human position ~7 m from _POS -- inside LATERAL_OFFSET_M (12 m).
_HUMAN_CLOSE = (_POS[0] + 0.00005, _POS[1] + 0.00004)
# and one ~40 m away -- clear.
_HUMAN_FAR = (_POS[0] + 0.0003, _POS[1] + 0.0002)


# --------------------------------------------------------------------------
# the scenario table
# --------------------------------------------------------------------------


@dataclass
class Scenario:
    name: str
    snapshot: TelemetrySnapshot
    context: SafetyContext
    expect_action: SafetyAction
    expect_severity: SafetySeverity
    expect_latched: bool = False
    link_down_s: float = 0.0
    gps_bad_s: float = 0.0
    discharge_rate: float | None = None
    prior_latch: SafetyAction | None = None
    reason_contains: str | None = None
    reason_contains_all: tuple[str, ...] = ()


def _ponr_exact_battery() -> float:
    # The exact budget for the default snapshot at a known discharge rate. The
    # check is ``pct <= needed`` -- setting pct to exactly this value must trip
    # it. (Don't round: rounding up would put us a hair above the line.)
    return safety.estimate_return_budget_pct(
        snap(), _CRUISE_CTX, discharge_rate_pct_per_s=0.05
    )


_SCENARIOS: list[Scenario] = [
    Scenario("nominal cruise", snap(), _CRUISE_CTX, SafetyAction.ALLOW, SafetySeverity.NOMINAL),
    Scenario(
        "battery at warn level",
        snap(battery_pct=float(config.BATTERY_WARN_PCT)),
        _CRUISE_CTX,
        SafetyAction.WARN,
        SafetySeverity.CAUTION,
        reason_contains="warn level",
    ),
    Scenario(
        "battery at hard floor",
        snap(battery_pct=float(config.BATTERY_FLOOR_PCT)),
        _CRUISE_CTX,
        SafetyAction.RTL_NOW,
        SafetySeverity.CRITICAL,
        expect_latched=True,
        reason_contains="hard floor",
    ),
    Scenario(
        "voltage floor tripped, percentage still healthy",
        snap(battery_pct=80.0, battery_voltage_v=config.BATTERY_CELL_FLOOR_V * config.BATTERY_CELLS - 0.5),
        _CRUISE_CTX,
        SafetyAction.RTL_NOW,
        SafetySeverity.CRITICAL,
        expect_latched=True,
        reason_contains="per-cell voltage",
    ),
    Scenario(
        "battery reading absent",
        snap(battery_pct=None, battery_voltage_v=None),
        _CRUISE_CTX,
        SafetyAction.LAND_NOW,
        SafetySeverity.EMERGENCY,
        expect_latched=True,
        reason_contains="treated as empty",
    ),
    Scenario(
        "battery reading stale",
        snap(battery_age_s=config.BATTERY_MAX_AGE_S + 3.0),
        _CRUISE_CTX,
        SafetyAction.LAND_NOW,
        SafetySeverity.EMERGENCY,
        expect_latched=True,
        reason_contains="stale",
    ),
    Scenario(
        "position stale while heartbeat healthy",
        snap(position_valid=False, position_age_s=8.0),
        _CRUISE_CTX,
        SafetyAction.HOLD,
        SafetySeverity.CRITICAL,
        gps_bad_s=8.0,
        reason_contains="position report",
    ),
    Scenario(
        "GPS fix lost mid flight",
        snap(position_valid=False, gps_fix_type=1),
        _CRUISE_CTX,
        SafetyAction.HOLD,
        SafetySeverity.CRITICAL,
        gps_bad_s=0.0,
    ),
    Scenario(
        "GPS lost then recovered within grace",
        snap(),
        _CRUISE_CTX,
        SafetyAction.ALLOW,
        SafetySeverity.NOMINAL,
        gps_bad_s=0.0,  # shell resets the timer the moment GPS is trustworthy again
    ),
    Scenario(
        "GPS lost beyond grace",
        snap(position_valid=False, gps_fix_type=1),
        _CRUISE_CTX,
        SafetyAction.LAND_NOW,
        SafetySeverity.EMERGENCY,
        expect_latched=True,
        gps_bad_s=config.GPS_LOSS_GRACE_S + 5.0,
    ),
    Scenario(
        "link down within grace",
        snap(connected=False),
        _CRUISE_CTX,
        SafetyAction.WARN,
        SafetySeverity.CAUTION,
        link_down_s=config.LINK_LOSS_GRACE_S - 5.0,
    ),
    Scenario(
        "link down beyond grace",
        snap(connected=False),
        _CRUISE_CTX,
        SafetyAction.RTL_NOW,
        SafetySeverity.CRITICAL,
        expect_latched=True,
        link_down_s=config.LINK_LOSS_GRACE_S + 10.0,
        reason_contains="link down",
    ),
    Scenario(
        "outside geofence radius",
        snap(lat=config.HOME_LAT + 1.0, lon=config.HOME_LON + 1.0),
        _CRUISE_CTX,
        SafetyAction.RTL_NOW,
        SafetySeverity.CRITICAL,
        expect_latched=True,
        reason_contains="geofence",
    ),
    Scenario(
        "above max altitude",
        snap(alt_m_relative=config.MAX_ALT_M + 10.0),
        _CRUISE_CTX,
        SafetyAction.DESCEND,
        SafetySeverity.CRITICAL,
        reason_contains="ceiling",
    ),
    Scenario(
        "above ceiling AND below battery floor -- more severe verdict wins",
        snap(alt_m_relative=config.MAX_ALT_M + 10.0,
             battery_pct=float(config.BATTERY_FLOOR_PCT) - 1.0),
        _CRUISE_CTX,
        SafetyAction.RTL_NOW,   # RTL_NOW outranks DESCEND
        SafetySeverity.CRITICAL,
        expect_latched=True,
        reason_contains_all=("ceiling", "hard floor"),
    ),
    Scenario(
        "point of no return exactly reached",
        snap(battery_pct=_ponr_exact_battery()),
        _CRUISE_CTX,
        SafetyAction.RTL_NOW,
        SafetySeverity.CRITICAL,
        expect_latched=True,
        discharge_rate=0.05,
        reason_contains="point of no return",
    ),
    Scenario(
        "point of no return passed, then battery recovers (stays latched, R13)",
        snap(battery_pct=90.0),
        _CRUISE_CTX,
        SafetyAction.RTL_NOW,
        SafetySeverity.CRITICAL,
        expect_latched=True,
        prior_latch=SafetyAction.RTL_NOW,
        reason_contains="latched",
    ),
    Scenario(
        "summon on-station too close to the human (R2), in flight",
        snap(),
        replace(_CRUISE_CTX, human_lat=_HUMAN_CLOSE[0], human_lon=_HUMAN_CLOSE[1]),
        SafetyAction.HOLD,
        SafetySeverity.CRITICAL,
        reason_contains="R2",
    ),
    Scenario(
        "pre-flight: summon on-station too close to the human (R2)",
        snap(),
        replace(_PREFLIGHT_CTX, human_lat=_HUMAN_CLOSE[0], human_lon=_HUMAN_CLOSE[1]),
        SafetyAction.REJECT,
        SafetySeverity.CRITICAL,
        reason_contains="lateral offset",
    ),
    Scenario(
        "pre-flight: battery below launch minimum",
        snap(battery_pct=30.0),
        _PREFLIGHT_CTX,
        SafetyAction.REJECT,
        SafetySeverity.CRITICAL,
        reason_contains="launch minimum",
    ),
    Scenario(
        "pre-flight: healthy summon is allowed",
        snap(),
        replace(_PREFLIGHT_CTX, human_lat=_HUMAN_FAR[0], human_lon=_HUMAN_FAR[1]),
        SafetyAction.ALLOW,
        SafetySeverity.NOMINAL,
    ),
    Scenario(
        "pre-flight: target outside geofence",
        snap(),
        replace(_PREFLIGHT_CTX, target_lat=config.HOME_LAT + 1.0, target_lon=config.HOME_LON),
        SafetyAction.REJECT,
        SafetySeverity.CRITICAL,
        reason_contains="geofence",
    ),
    Scenario(
        "pre-flight: link down",
        snap(connected=False),
        _PREFLIGHT_CTX,
        SafetyAction.REJECT,
        SafetySeverity.CRITICAL,
        reason_contains="link",
    ),
]


def test_scenario_table() -> None:
    print("\n  {:<52} {:>9} {:>10} {:>8}   {}".format(
        "scenario", "action", "severity", "latched", "result"))
    print("  " + "-" * 100)
    for sc in _SCENARIOS:
        v = safety.evaluate(
            sc.snapshot,
            sc.context,
            now=_NOW,
            link_down_s=sc.link_down_s,
            gps_bad_s=sc.gps_bad_s,
            discharge_rate_pct_per_s=sc.discharge_rate,
            prior_latch=sc.prior_latch,
        )
        ok = (
            v.action == sc.expect_action
            and v.severity == sc.expect_severity
            and v.latched == sc.expect_latched
        )
        if sc.reason_contains is not None:
            ok = ok and any(sc.reason_contains in r for r in v.reasons)
        for needle in sc.reason_contains_all:
            ok = ok and any(needle in r for r in v.reasons)
        flag = "PASS" if ok else "FAIL"
        print("  {:<52} {:>9} {:>10} {:>8}   {} {}".format(
            sc.name[:52], v.action.value, v.severity.value, str(v.latched), flag,
            "" if ok else f"(got {v.action.value}/{v.severity.value}/{v.latched}; "
                          f"reasons={list(v.reasons)})"))
        _results.append((f"scenario: {sc.name}", ok, ""))

    # a machine-checkable roll-up too
    n = len(_SCENARIOS)
    passed = sum(1 for name, ok, _ in _results if name.startswith("scenario: ") and ok)
    _check(f"scenario table: {passed}/{n} rows", passed == n)


# --------------------------------------------------------------------------
# R12: a check that raises must DENY, not ALLOW
# --------------------------------------------------------------------------


def test_broken_check_denies() -> None:
    real = safety.check_battery

    def boom(_snapshot):
        raise RuntimeError("simulated sensor-parse bug")

    safety.check_battery = boom  # type: ignore[assignment]
    try:
        v = safety.evaluate(snap(), _CRUISE_CTX, now=_NOW)
        _check("R12 in-flight: broken check does not ALLOW", v.action != SafetyAction.ALLOW,
               v.action.value)
        _check("R12 in-flight: broken check escalates to a veto",
               v.action in (SafetyAction.HOLD, SafetyAction.RTL_NOW, SafetyAction.LAND_NOW)
               and v.severity in (SafetySeverity.CRITICAL, SafetySeverity.EMERGENCY),
               f"{v.action.value}/{v.severity.value}")
        _check("R12 in-flight: the failure is named, not swallowed (R8)",
               any("could not be evaluated" in r for r in v.reasons), str(v.reasons))

        pv = safety.evaluate(snap(), _PREFLIGHT_CTX, now=_NOW)
        _check("R12 pre-flight: broken check REJECTs", pv.action == SafetyAction.REJECT,
               pv.action.value)
    finally:
        safety.check_battery = real  # type: ignore[assignment]


# --------------------------------------------------------------------------
# watchdog + latch + discharge-rate units
# --------------------------------------------------------------------------


def test_watchdog_verdict() -> None:
    fresh = safety.watchdog_verdict(config.SAFETY_WATCHDOG_S - 0.1, _NOW)
    _check("watchdog: fresh tick -> no verdict", fresh is None)
    stalled = safety.watchdog_verdict(config.SAFETY_WATCHDOG_S + 1.0, _NOW)
    _check("watchdog: stalled loop -> LAND_NOW / EMERGENCY / latched",
           stalled is not None and stalled.action == SafetyAction.LAND_NOW
           and stalled.severity == SafetySeverity.EMERGENCY and stalled.latched,
           "" if stalled is None else f"{stalled.action.value}/{stalled.severity.value}")


def test_latch_clear_rule() -> None:
    airborne = snap(armed=True, alt_m_relative=20.0)
    grounded = snap(armed=False, alt_m_relative=0.0)
    unknown_alt = snap(armed=False, alt_m_relative=None)
    _check("latch holds while airborne",
           safety.update_latch(SafetyAction.RTL_NOW, SafetyAction.ALLOW, airborne)
           == SafetyAction.RTL_NOW)
    _check("latch holds while armed on the ground",
           safety.update_latch(SafetyAction.RTL_NOW, SafetyAction.ALLOW,
                               snap(armed=True, alt_m_relative=0.0)) == SafetyAction.RTL_NOW)
    _check("latch holds when altitude is unknown (fail safe)",
           safety.update_latch(SafetyAction.RTL_NOW, SafetyAction.ALLOW, unknown_alt)
           == SafetyAction.RTL_NOW)
    _check("latch clears only on the ground AND disarmed",
           safety.update_latch(SafetyAction.RTL_NOW, SafetyAction.ALLOW, grounded) is None)
    _check("LAND_NOW latch never de-escalates to RTL_NOW",
           safety.update_latch(SafetyAction.LAND_NOW, SafetyAction.RTL_NOW, airborne)
           == SafetyAction.LAND_NOW)


def test_discharge_rate() -> None:
    _check("discharge rate: too few samples -> None",
           safety.observed_discharge_rate([(0.0, 90.0)], 60.0) is None)
    _check("discharge rate: not discharging -> None (caller uses fallback)",
           safety.observed_discharge_rate([(0.0, 80.0), (40.0, 82.0)], 60.0) is None)
    rate = safety.observed_discharge_rate([(0.0, 90.0), (60.0, 84.0)], 60.0)
    _check("discharge rate: 6% over 60s -> 0.1 %/s",
           rate is not None and abs(rate - 0.1) < 1e-6, str(rate))


# --------------------------------------------------------------------------
# SHELL: SafetyMonitor timers + self-watchdog, no network
# --------------------------------------------------------------------------


class _Box:
    def __init__(self, s: TelemetrySnapshot) -> None:
        self.s = s
        self.block = 0.0  # seconds each snapshot read should hang (stalls the loop)

    def __call__(self) -> TelemetrySnapshot:
        if self.block:
            time.sleep(self.block)
        return self.s


def test_monitor_gps_and_link_timers() -> None:
    box = _Box(snap())
    mon = SafetyMonitor(box, tick_hz=20.0, watchdog_s=3.0)
    mon.set_context(replace(_CRUISE_CTX))
    mon.start()
    try:
        time.sleep(0.3)
        _check("monitor: nominal -> ALLOW", mon.current_verdict().action == SafetyAction.ALLOW,
               mon.current_verdict().action.value)

        box.s = snap(position_valid=False, gps_fix_type=1)
        time.sleep(0.3)
        _check("monitor: GPS lost -> HOLD (within grace)",
               mon.current_verdict().action == SafetyAction.HOLD,
               mon.current_verdict().action.value)

        box.s = snap()  # recovers well within GPS_LOSS_GRACE_S
        time.sleep(0.3)
        _check("monitor: GPS recovered within grace -> back to ALLOW",
               mon.current_verdict().action == SafetyAction.ALLOW,
               mon.current_verdict().action.value)

        box.s = snap(connected=False)
        time.sleep(0.4)
        _check("monitor: link just dropped -> WARN (within grace)",
               mon.current_verdict().action == SafetyAction.WARN,
               mon.current_verdict().action.value)
    finally:
        mon.close()


def test_monitor_self_watchdog() -> None:
    box = _Box(snap())
    mon = SafetyMonitor(box, tick_hz=20.0, watchdog_s=1.0)
    mon.start()
    try:
        time.sleep(0.3)
        _check("monitor: healthy loop -> watchdog quiet",
               mon.current_verdict().action == SafetyAction.ALLOW)
        # Genuinely stall the loop: every snapshot read now hangs for 4 s, well
        # past the 1 s watchdog.
        box.block = 4.0
        time.sleep(2.0)
        v = mon.current_verdict()
        _check("monitor: stalled loop -> current_verdict is LAND_NOW / EMERGENCY",
               v.action == SafetyAction.LAND_NOW and v.severity == SafetySeverity.EMERGENCY,
               f"{v.action.value}/{v.severity.value}")
        box.block = 0.0
    finally:
        mon.close()


def test_monitor_latch_survives_flapping_battery() -> None:
    """R13 through the shell: a flapping pack reading does not cancel an RTL."""
    box = _Box(snap(armed=True, alt_m_relative=25.0, battery_pct=19.0))
    mon = SafetyMonitor(box, tick_hz=20.0)
    mon.set_context(replace(_CRUISE_CTX))
    mon.start()
    try:
        time.sleep(0.3)
        _check("monitor: 19% -> RTL_NOW latched",
               mon.current_verdict().action == SafetyAction.RTL_NOW
               and mon.current_verdict().latched)
        for pct in (60.0, 18.0, 70.0, 21.0, 80.0):  # bounce around
            box.s = snap(armed=True, alt_m_relative=25.0, battery_pct=pct)
            time.sleep(0.1)
        v = mon.current_verdict()
        _check("monitor: RTL stays latched despite the pack bouncing back up (R13)",
               v.action == SafetyAction.RTL_NOW and v.latched, f"{v.action.value} latched={v.latched}")
        # now genuinely land + disarm
        box.s = snap(armed=False, alt_m_relative=0.0, battery_pct=80.0)
        time.sleep(0.3)
        _check("monitor: latch clears once on the ground and disarmed",
               mon.current_verdict().action == SafetyAction.ALLOW,
               mon.current_verdict().action.value)
    finally:
        mon.close()


# --------------------------------------------------------------------------
# R1: the import boundary
# --------------------------------------------------------------------------


def test_import_boundary() -> None:
    probe = (
        "import sys\n"
        "import gss.safety\n"
        "roots = {'urllib','http','websockets','requests','socket','ssl','_socket',"
        "'_ssl','aiohttp','httpx','asyncio','ftplib','smtplib','poplib','imaplib',"
        "'telnetlib','xmlrpc','selectors','ssl'}\n"
        "exact = {'gss.store','gss.commands','gss.executor','gss.link','gss.telemetry'}\n"
        "bad = sorted(m for m in sys.modules if m in exact or m.split('.')[0] in roots)\n"
        "print(repr(bad))\n"
    )
    env = dict(os.environ, SUPABASE_ENABLED="false")
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, env=env, timeout=60
    )
    printed = (out.stdout or "").strip()
    _check("import boundary: probe ran", out.returncode == 0, out.stderr.strip()[:300])
    _check(
        "import boundary: gss.safety pulls in NO networking module and none of "
        "gss.store / gss.commands / gss.executor / gss.link / gss.telemetry (R1)",
        printed == "[]",
        f"leaked: {printed}",
    )


# --------------------------------------------------------------------------
# Fix 1: safety is a hard start-up requirement
# --------------------------------------------------------------------------


def test_safety_is_a_hard_startup_requirement() -> None:
    import contextlib

    import gss.main as gmain
    import gss.safety as gsafety

    real = gsafety.SafetyMonitor

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("simulated: the safety monitor cannot be constructed")

    gsafety.SafetyMonitor = _Boom  # type: ignore[assignment]
    logbuf = io.StringIO()
    handler = logging.StreamHandler(logbuf)
    logging.getLogger().addHandler(handler)
    outbuf = io.StringIO()
    code: object = "did-not-exit"
    try:
        with contextlib.redirect_stdout(outbuf):
            try:
                code = gmain.run()
            except SystemExit as exc:
                code = exc.code
    finally:
        gsafety.SafetyMonitor = real  # type: ignore[assignment]
        logging.getLogger().removeHandler(handler)

    _check("Fix1: GSS exits with code 3 when the safety monitor cannot be built",
           code == 3, f"exit code was {code!r}")
    _check("Fix1: the reason is logged",
           "will not run without it" in logbuf.getvalue(),
           logbuf.getvalue().strip()[-200:])
    _check("Fix1: the telemetry loop is never reached (no console line printed)",
           outbuf.getvalue().strip() == "", repr(outbuf.getvalue()[:200]))


# --------------------------------------------------------------------------
# LIVE: fake vehicle + real Supabase (mirrors the phase verification list)
# --------------------------------------------------------------------------

_LIVE = bool(os.environ.get("SUPABASE_SERVICE_ROLE_KEY"))


def _live_layer() -> int:
    from dotenv import dotenv_values

    for k, v in dotenv_values(".env").items():
        if v:
            os.environ.setdefault(k, v)
    if not os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
        print("\nSKIP live layer: no .env with SUPABASE_SERVICE_ROLE_KEY")
        return 0

    import json
    import urllib.request

    from gss.commands import CommandIntake
    from gss.link import MavlinkLink
    from gss.store import TelemetryStore
    from gss.telemetry import TelemetryReader
    from tests.fake_vehicle import FakeVehicle

    BASE = os.environ["SUPABASE_URL"].rstrip("/")
    KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    DRONE = os.environ["DRONE_ID"]

    def rest(method, path, body=None, params=None, prefer="return=representation"):
        url = BASE + path
        if params:
            url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        req = urllib.request.Request(
            url, data=json.dumps(body).encode() if body is not None else None, method=method,
            headers={"apikey": KEY, "Authorization": "Bearer " + KEY,
                     "Content-Type": "application/json", "Prefer": prefer})
        with urllib.request.urlopen(req, timeout=20) as r:
            txt = r.read().decode()
            return json.loads(txt) if txt.strip() else None

    def wipe():
        rest("PATCH", "/rest/v1/missions",
             {"status": "aborted", "abort_reason": "test cleanup"},
             {"drone_id": f"eq.{DRONE}", "status": "not.in.(landed,aborted)"})
        rest("DELETE", "/rest/v1/mission_events", params={"id": "not.is.null"})
        rest("DELETE", "/rest/v1/commands", params={"drone_id": f"eq.{DRONE}"})
        rest("DELETE", "/rest/v1/missions", params={"drone_id": f"eq.{DRONE}"})

    def command(cid, select="status,rejected_reason,mission_id"):
        rows = rest("GET", "/rest/v1/commands", params={"id": f"eq.{cid}", "select": select})
        return rows[0] if rows else None

    def events(mid):
        return rest("GET", "/rest/v1/mission_events",
                    params={"mission_id": f"eq.{mid}", "select": "event,detail,at",
                            "order": "at.asc"}) or []

    def mission(mid):
        rows = rest("GET", "/rest/v1/missions",
                    params={"id": f"eq.{mid}", "select": "status,abort_reason"})
        return rows[0] if rows else None

    def insert(**kw):
        row = {"drone_id": DRONE, "type": "summon", "status": "pending",
               "issued_by": "test_safety", **kw}
        return rest("POST", "/rest/v1/commands", row)[0]

    def waitfor(pred, timeout, poll=0.3):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if pred():
                    return True
            except Exception:
                pass
            time.sleep(poll)
        return False

    class Rig:
        def __init__(self, *, port=5801, airborne=True):
            self.fv = FakeVehicle(port=port)
            # ~700 m from home -- inside the geofence so the only veto is the
            # one each test deliberately induces.
            self.fv.set_position(lat=25.5975, lon=85.2085, fix_type=3, satellites=12,
                                 heading_deg=90.0, groundspeed_ms=9.0)
            self.fv.set_battery(pct=90, voltage_v=24.6)
            if airborne:
                self.fv.set_armed(True)
                self.fv.set_position(rel_alt_m=25.0)
            self.fv.start()
            self.fv.wait_for_client(2)
            self.link = MavlinkLink(connection_string=f"tcp:127.0.0.1:{port}", heartbeat_timeout_s=4)
            self.reader = TelemetryReader(self.link)
            self.link.connect(timeout_s=8)
            time.sleep(3)
            self.store = TelemetryStore(self.reader.get_snapshot)
            self.intake = CommandIntake(
                self.store, self.reader.get_snapshot,
                home_lat=25.5932, home_lon=85.2045, realtime_enabled=True)
            self.intake.start()
            time.sleep(2)

        def close(self):
            self.intake.close(4)
            self.link.close()
            self.fv.stop()

    # -- capture GSS logs so we can prove "local log is the record of truth" --
    logbuf = io.StringIO()
    handler = logging.StreamHandler(logbuf)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)

    # 2/3 -- drain battery below floor mid-dry-run; RTL; latch survives recovery
    try:
        wipe()
        rig = Rig(port=5801, airborne=True)
        try:
            c = insert(target_lat=25.60, target_lon=85.21, target_alt_m=30)
            waitfor(lambda: command(c["id"])["status"] in ("executing", "done"), 15)
            mid = command(c["id"])["mission_id"]
            _check("live-2: mission accepted and executing", bool(mid),
                   str(command(c["id"])["status"]))
            waitfor(lambda: mission(mid) and mission(mid)["status"] == "enroute", 10)
            mark = len(logbuf.getvalue())
            rig.fv.set_battery(pct=15, voltage_v=21.0)  # below BATTERY_FLOOR_PCT
            got = waitfor(lambda: mission(mid) and mission(mid)["status"] == "aborted", 12)
            _check("live-2: safety drove the mission to aborted", got,
                   str(mission(mid)))
            evs = events(mid)
            veto = [e for e in evs if e["event"] == "safety_veto"]
            _check("live-2: a safety_veto mission_event was written", bool(veto),
                   str([e["event"] for e in evs]))
            _check("live-2: veto detail carries the full reason list + RTL_NOW",
                   bool(veto) and veto[0]["detail"].get("action") == "RTL_NOW"
                   and isinstance(veto[0]["detail"].get("reasons"), list)
                   and len(veto[0]["detail"]["reasons"]) >= 1,
                   str(veto[0]["detail"]) if veto else "")
            seg = logbuf.getvalue()[mark:]
            _check("live-2: verdict is in the LOCAL log first (SAFETY ... RTL_NOW)",
                   "SAFETY" in seg and "RTL_NOW" in seg)

            # 3 -- raise the battery back up; the RTL must NOT be cancelled
            mark2 = len(logbuf.getvalue())
            rig.fv.set_battery(pct=92, voltage_v=24.8)
            time.sleep(4)
            seg2 = logbuf.getvalue()[mark2:]
            _check("live-3 (R13): battery back to 92% does NOT clear the veto "
                   "(no ALLOW/nominal logged, mission still aborted)",
                   mission(mid)["status"] == "aborted"
                   and "SAFETY ALLOW" not in seg2,
                   seg2.strip()[-200:])
        finally:
            rig.close()
    except Exception as exc:  # noqa: BLE001
        _check("live-2/3 (crashed)", False, repr(exc))

    # 4 -- fix_type -> 1 mid-mission: HOLD, then LAND_NOW after GPS_LOSS_GRACE_S.
    #      The dry-run timeline is only ~15 s, so shorten the grace for this
    #      test (safety.py reads config.GPS_LOSS_GRACE_S live on every check).
    try:
        wipe()
        _saved_grace = config.GPS_LOSS_GRACE_S
        config.GPS_LOSS_GRACE_S = 3.0
        rig = Rig(port=5802, airborne=True)
        try:
            c = insert(target_lat=25.60, target_lon=85.21)
            waitfor(lambda: command(c["id"])["status"] in ("executing", "done"), 15)
            mid = command(c["id"])["mission_id"]
            waitfor(lambda: mission(mid) and mission(mid)["status"] in
                    ("launching", "enroute"), 8)
            rig.fv.set_fix_type(1)  # early -- the dry-run timeline is short
            hold = waitfor(
                lambda: any(
                    e["event"] == "safety_veto" and e["detail"].get("action") == "HOLD"
                    for e in events(mid)),
                8)
            _check("live-4: GPS fix lost -> HOLD veto recorded", hold,
                   str([(e["event"], e["detail"].get("action")) for e in events(mid)]))
            land = waitfor(
                lambda: mission(mid) and mission(mid)["status"] == "aborted"
                and any(e["detail"].get("action") == "LAND_NOW" for e in events(mid)
                        if e["event"] == "safety_veto"),
                12)
            _check("live-4: GPS still lost past the grace -> LAND_NOW, mission aborted",
                   land, str([(e["event"], e["detail"].get("action")) for e in events(mid)]))
        finally:
            rig.close()
            config.GPS_LOSS_GRACE_S = _saved_grace
    except Exception as exc:  # noqa: BLE001
        _check("live-4 (crashed)", False, repr(exc))

    # 5 -- ONLY GLOBAL_POSITION_INT stops; HEARTBEAT, VFR_HUD, SYS_STATUS and
    #      GPS_RAW_INT keep flowing. position_age_s climbs while battery_age_s
    #      and link_age_s stay fresh, position_valid flips to False, and safety
    #      reacts -- with the battery reading still perfectly healthy. This is
    #      the partial-stream failure the old single global "telemetry age"
    #      would have missed.
    from pymavlink import mavutil as _mavutil

    _GPI = _mavutil.mavlink.MAVLINK_MSG_ID_GLOBAL_POSITION_INT
    try:
        fv = FakeVehicle(port=5806)
        fv.set_position(lat=25.5975, lon=85.2085, fix_type=3, satellites=12, rel_alt_m=25.0)
        fv.set_battery(pct=90, voltage_v=24.6)
        fv.set_armed(True)
        fv.start()
        fv.wait_for_client(2)
        link = MavlinkLink(connection_string="tcp:127.0.0.1:5806", heartbeat_timeout_s=8)
        reader = TelemetryReader(link)
        link.connect(timeout_s=8)
        time.sleep(3)
        _check("live-5: position_valid True while streaming",
               reader.get_snapshot().position_valid is True)
        mon = SafetyMonitor(reader.get_snapshot, home_lat=25.5932, home_lon=85.2045,
                            tick_hz=8.0)
        mon.set_context(SafetyContext(preflight=False, phase="on_station",
                                      home_lat=25.5932, home_lon=85.2045))
        mon.start()
        try:
            time.sleep(1.0)
            _check("live-5: safety ALLOW while telemetry flows",
                   mon.current_verdict().action == SafetyAction.ALLOW)

            fv.suppress_message(_GPI)  # ONLY GLOBAL_POSITION_INT goes silent
            print("\n  live-5: after stopping ONLY GLOBAL_POSITION_INT --")
            print("    {:>6}  {:>12}  {:>12}  {:>12}  {:>10}  {}".format(
                "t (s)", "position_age", "battery_age", "link_age", "pos_valid", "verdict"))
            t0 = time.time()
            rows = []
            for _ in range(7):
                s = reader.get_snapshot()
                rows.append(s)
                print("    {:>6.1f}  {:>12}  {:>12}  {:>12}  {:>10}  {}".format(
                    time.time() - t0,
                    f"{s.position_age_s:.2f}" if s.position_age_s is not None else "-",
                    f"{s.battery_age_s:.2f}" if s.battery_age_s is not None else "-",
                    f"{s.link_age_s:.2f}" if s.link_age_s is not None else "-",
                    str(s.position_valid), mon.current_verdict().action.value))
                time.sleep(1.0)

            s = rows[-1]
            _check("live-5: position_age_s rose past POSITION_MAX_AGE_S",
                   s.position_age_s is not None and s.position_age_s > config.POSITION_MAX_AGE_S,
                   f"position_age={s.position_age_s}")
            _check("live-5: battery_age_s stayed fresh (its stream never stopped)",
                   s.battery_age_s is not None and s.battery_age_s < config.BATTERY_MAX_AGE_S,
                   f"battery_age={s.battery_age_s}")
            _check("live-5: link_age_s stayed fresh (HEARTBEAT never stopped)",
                   s.link_age_s is not None and s.link_age_s < 3.0,
                   f"link_age={s.link_age_s}")
            _check("live-5: the battery reading itself is still healthy (90%)",
                   s.battery_pct == 90.0, str(s.battery_pct))
            _check("live-5: position_valid flipped to False on the stale position",
                   s.position_valid is False)
            v = mon.current_verdict()
            _check("live-5: safety reacted to the stale position -- HOLD (not a "
                   "battery verdict, the battery is fine)",
                   v.action == SafetyAction.HOLD, v.action.value)

            fv.resume_message(_GPI)
            time.sleep(2.0)
            _check("live-5: position recovers and safety returns to ALLOW",
                   reader.get_snapshot().position_valid is True
                   and mon.current_verdict().action == SafetyAction.ALLOW,
                   mon.current_verdict().action.value)
        finally:
            mon.close()
            link.close()
            fv.stop()
    except Exception as exc:  # noqa: BLE001
        _check("live-5 (crashed)", False, repr(exc))

    # 6 -- pre-flight summon with battery at 30%: rejected, reason names the minimum
    try:
        wipe()
        rig = Rig(port=5803, airborne=False)
        try:
            rig.fv.set_battery(pct=30, voltage_v=22.8)
            time.sleep(2)
            c = insert(target_lat=25.60, target_lon=85.21)
            waitfor(lambda: command(c["id"])["status"] == "rejected", 12)
            row = command(c["id"])
            _check("live-6: summon at 30% battery rejected", row["status"] == "rejected",
                   str(row["status"]))
            _check("live-6: rejection reason names the launch minimum",
                   "launch minimum" in (row["rejected_reason"] or ""), row["rejected_reason"])
            _check("live-6: no mission created", row["mission_id"] is None)
        finally:
            rig.close()
    except Exception as exc:  # noqa: BLE001
        _check("live-6 (crashed)", False, repr(exc))

    # 7 -- summon target 5 m from the reported human position: rejected (R2)
    try:
        wipe()
        rig = Rig(port=5804, airborne=False)
        try:
            c = insert(
                target_lat=25.60, target_lon=85.21,
                params={"human_lat": 25.60 + 0.00004, "human_lon": 85.21})  # ~4.5 m
            waitfor(lambda: command(c["id"])["status"] == "rejected", 12)
            row = command(c["id"])
            _check("live-7: summon 5 m from the human rejected", row["status"] == "rejected",
                   str(row["status"]))
            _check("live-7: reason cites the lateral offset (R2)",
                   "lateral offset" in (row["rejected_reason"] or "")
                   and "R2" in (row["rejected_reason"] or ""), row["rejected_reason"])
        finally:
            rig.close()
    except Exception as exc:  # noqa: BLE001
        _check("live-7 (crashed)", False, repr(exc))

    # 9 -- R1: with Supabase URL unreachable, safety KEEPS evaluating + ordering,
    #      and the verdicts land in the LOCAL log with no database involved.
    try:
        fv = FakeVehicle(port=5805)
        fv.set_position(lat=25.5975, lon=85.2085, fix_type=3, satellites=12,
                        heading_deg=90.0, groundspeed_ms=8.0, rel_alt_m=25.0)
        fv.set_battery(pct=90, voltage_v=24.6)
        fv.set_armed(True)
        fv.start()
        fv.wait_for_client(2)
        link = MavlinkLink(connection_string="tcp:127.0.0.1:5805", heartbeat_timeout_s=4)
        reader = TelemetryReader(link)
        link.connect(timeout_s=8)
        time.sleep(3)

        calls: list = []

        def dead_sink(*a, **k):  # stands in for an unreachable Supabase
            calls.append(a)
            raise OSError("supabase unreachable (simulated broadband outage)")

        mon = SafetyMonitor(reader.get_snapshot, event_sink=dead_sink,
                            home_lat=25.5932, home_lon=85.2045, tick_hz=8.0)
        mon.set_context(SafetyContext(preflight=False, phase="on_station",
                                      mission_id=None, home_lat=25.5932, home_lon=85.2045))
        mark = len(logbuf.getvalue())
        mon.start()
        try:
            time.sleep(1.0)
            _check("live-9: safety evaluates with no DB -> ALLOW while healthy",
                   mon.current_verdict().action == SafetyAction.ALLOW,
                   mon.current_verdict().action.value)
            fv.set_battery(pct=14, voltage_v=20.5)
            waitfor(lambda: mon.current_verdict().action == SafetyAction.RTL_NOW, 5)
            v = mon.current_verdict()
            _check("live-9: safety still ORDERS correctly with the DB down (RTL_NOW)",
                   v.action == SafetyAction.RTL_NOW and v.latched,
                   f"{v.action.value} latched={v.latched}")
            seg = logbuf.getvalue()[mark:]
            _check("live-9: the verdict is in the LOCAL log", "SAFETY" in seg and "RTL_NOW" in seg)
            _check("live-9: the sink WAS attempted and failed, non-fatally (R8)",
                   len(calls) >= 1 and "event sink failed" in seg)
        finally:
            mon.close()
            link.close()
            fv.stop()
    except Exception as exc:  # noqa: BLE001
        _check("live-9 (crashed)", False, repr(exc))

    logging.getLogger().removeHandler(handler)
    wipe()
    return 0


# --------------------------------------------------------------------------


def main() -> int:
    for t in (
        test_scenario_table,
        test_broken_check_denies,
        test_watchdog_verdict,
        test_latch_clear_rule,
        test_discharge_rate,
        test_monitor_gps_and_link_timers,
        test_monitor_self_watchdog,
        test_monitor_latch_survives_flapping_battery,
        test_import_boundary,
        test_safety_is_a_hard_startup_requirement,
    ):
        print(f"\n--- {t.__name__} ---")
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            _check(t.__name__ + " (crashed)", False, repr(exc))

    if _LIVE:
        print("\n=== LIVE layer (fake vehicle + real Supabase) ===")
        try:
            _live_layer()
        except Exception as exc:  # noqa: BLE001
            _check("live layer (crashed)", False, repr(exc))

    failed = [n for n, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        print("FAILED:")
        for n in failed:
            print(f"  - {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
