"""weather.py / weather_feed.py verification (v0.5).

Layers, all OFFLINE except where noted:

  * a CLASSIFICATION scenario table driven through the pure core;
  * DECISION tests (pre-flight and in-flight, routine vs emergency);
  * WIND into the point of no return (safety.py);
  * the FEED: fake forecast source, disk cache, stale handling, API down (R10);
  * the IMPORT-BOUNDARY test for weather.py (R1);
  * WEATHER_ENABLED=false is identical to v0.4;
  * a LIVE layer (fake vehicle + a FAKE forecast source + the real Supabase
    project in .env) mirroring the phase verification list -- it NEVER touches
    the real weather API.

    python -m tests.test_weather
"""

from __future__ import annotations

import io
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

os.environ.setdefault("SUPABASE_ENABLED", "false")

from gss import config, safety, weather  # noqa: E402
from gss.snapshot import TelemetrySnapshot  # noqa: E402
from gss.weather import (  # noqa: E402
    WeatherAction,
    WeatherConditions,
    WeatherTier,
)
from gss.weather_feed import RawForecast, WeatherFeed, WeatherMonitor  # noqa: E402

_results: list[tuple[str, bool, str]] = []
_NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)


def _check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  ::  {detail}" if detail else ""))


# --------------------------------------------------------------------------
# fake forecast source
# --------------------------------------------------------------------------


class FakeForecastSource:
    def __init__(self, **fields) -> None:
        self._raw = RawForecast(
            wind_speed_ms=fields.get("wind_speed_ms", 2.0),
            wind_gust_ms=fields.get("wind_gust_ms", 3.0),
            precip_mmh=fields.get("precip_mmh", 0.0),
            visibility_m=fields.get("visibility_m", 12000.0),
            temperature_c=fields.get("temperature_c", 26.0),
            lightning_nearby=fields.get("lightning_nearby", False),
        )
        self.fail = False
        self.calls = 0

    def set(self, **fields) -> None:
        from dataclasses import replace

        self._raw = replace(self._raw, **fields)

    def fetch(self, lat: float, lon: float) -> RawForecast:
        self.calls += 1
        if self.fail:
            raise OSError("simulated weather API down")
        return self._raw


# --------------------------------------------------------------------------
# classification scenario table
# --------------------------------------------------------------------------


def _fc(**over) -> WeatherConditions:
    base = dict(
        source="forecast", age_s=30.0,
        wind_speed_ms=2.0, wind_gust_ms=3.0, precip_mmh=0.0,
        visibility_m=12000.0, temperature_c=26.0, lightning_nearby=False,
    )
    base.update(over)
    return WeatherConditions(**base)


@dataclass
class WScenario:
    name: str
    conditions: WeatherConditions
    expect: WeatherTier
    reason_contains: str | None = None


_C = WeatherTier.CLEAR
_M = WeatherTier.MARGINAL
_S = WeatherTier.SEVERE

_SCENARIOS = [
    WScenario("all clear", _fc(), _C),
    WScenario("wind at marginal", _fc(wind_speed_ms=config.WIND_MARGINAL_MS), _M, "sustained wind"),
    WScenario("wind at severe", _fc(wind_speed_ms=config.WIND_SEVERE_MS), _S, "sustained wind"),
    WScenario("gusts at marginal", _fc(wind_gust_ms=config.GUST_MARGINAL_MS), _M, "gusts"),
    WScenario("gusts at severe", _fc(wind_gust_ms=config.GUST_SEVERE_MS), _S, "gusts"),
    WScenario("precip at marginal", _fc(precip_mmh=config.PRECIP_MARGINAL_MMH), _M, "precipitation"),
    WScenario("precip at severe", _fc(precip_mmh=config.PRECIP_SEVERE_MMH), _S, "precipitation"),
    WScenario("visibility below minimum", _fc(visibility_m=config.VISIBILITY_MIN_M - 1), _S, "visibility"),
    WScenario("visibility near minimum", _fc(visibility_m=config.VISIBILITY_MIN_M + 100), _M, "visibility"),
    WScenario("temperature below min", _fc(temperature_c=config.TEMP_MIN_C - 1), _S, "temperature"),
    WScenario("temperature above max", _fc(temperature_c=config.TEMP_MAX_C + 1), _S, "temperature"),
    WScenario("temperature near the edge", _fc(temperature_c=config.TEMP_MAX_C - 2), _M, "temperature"),
    WScenario("lightning within radius", _fc(lightning_nearby=True), _S, "lightning"),
    WScenario("unknown data (wind None)", _fc(wind_speed_ms=None), _M, "unknown"),
    WScenario("stale forecast", _fc(age_s=config.WEATHER_MAX_AGE_S + 60), _M, "stale"),
    WScenario("no forecast at all", _fc(age_s=None), _M, "stale"),
]


def test_classification_table() -> None:
    print("\n  {:<40} {:>10}   {}".format("scenario", "tier", "result"))
    print("  " + "-" * 72)
    passed = 0
    for sc in _SCENARIOS:
        v = weather.classify_forecast(sc.conditions, now=_NOW)
        ok = v.tier == sc.expect
        if sc.reason_contains is not None:
            ok = ok and any(sc.reason_contains in r for r in v.reasons)
        passed += ok
        print("  {:<40} {:>10}   {} {}".format(
            sc.name[:40], v.tier.value, "PASS" if ok else "FAIL",
            "" if ok else f"(got {v.tier.value}; reasons={list(v.reasons)})"))
        _results.append((f"wx-scenario: {sc.name}", ok, ""))
    _check(f"classification table: {passed}/{len(_SCENARIOS)} rows", passed == len(_SCENARIOS))


# --------------------------------------------------------------------------
# decisions
# --------------------------------------------------------------------------


def test_preflight_decisions() -> None:
    clear = weather.classify_forecast(_fc(), now=_NOW)
    marg = weather.classify_forecast(_fc(wind_speed_ms=9.0), now=_NOW)
    sev = weather.classify_forecast(_fc(lightning_nearby=True), now=_NOW)

    _check("pre-flight CLEAR -> ALLOW (either mission)",
           weather.preflight_decision(clear, is_emergency=False, now=_NOW).action == WeatherAction.ALLOW
           and weather.preflight_decision(clear, is_emergency=True, now=_NOW).action == WeatherAction.ALLOW)
    _check("pre-flight MARGINAL routine -> CONFIRM",
           weather.preflight_decision(marg, is_emergency=False, now=_NOW).action == WeatherAction.CONFIRM)
    d = weather.preflight_decision(marg, is_emergency=True, now=_NOW)
    _check("pre-flight MARGINAL emergency -> ALLOW, reason says no confirmation asked",
           d.action == WeatherAction.ALLOW and any("no confirmation" in r for r in d.reasons))
    _check("pre-flight SEVERE routine -> BLOCK",
           weather.preflight_decision(sev, is_emergency=False, now=_NOW).action == WeatherAction.BLOCK)
    d = weather.preflight_decision(sev, is_emergency=True, now=_NOW)
    _check("pre-flight SEVERE emergency -> BLOCK (no exception), still marked emergency",
           d.action == WeatherAction.BLOCK and d.is_emergency)


def test_inflight_decisions() -> None:
    marg = weather.classify_observed(
        WeatherConditions(source="observed", age_s=0.0, wind_speed_ms=9.0), now=_NOW)
    sev = weather.classify_observed(
        WeatherConditions(source="observed", age_s=0.0, wind_speed_ms=13.0), now=_NOW)

    # routine
    d = weather.inflight_decision(marg, is_emergency=False, warn_elapsed_s=0.0, now=_NOW)
    _check("in-flight routine MARGINAL, within grace -> WARN", d.action == WeatherAction.WARN)
    d = weather.inflight_decision(
        marg, is_emergency=False, warn_elapsed_s=config.WEATHER_WARN_GRACE_S + 1, now=_NOW)
    _check("in-flight routine MARGINAL, past grace, no answer -> RETURN", d.action == WeatherAction.RETURN)
    d = weather.inflight_decision(
        marg, is_emergency=False, warn_elapsed_s=999, now=_NOW, human_continue=True)
    _check("in-flight routine MARGINAL, human said continue -> ALLOW", d.action == WeatherAction.ALLOW)
    d = weather.inflight_decision(sev, is_emergency=False, warn_elapsed_s=0.0, now=_NOW)
    _check("in-flight routine SEVERE -> RETURN immediately", d.action == WeatherAction.RETURN)

    # emergency -- the important ones
    d = weather.inflight_decision(
        marg, is_emergency=True, warn_elapsed_s=config.WEATHER_WARN_GRACE_S * 10, now=_NOW)
    _check("in-flight EMERGENCY MARGINAL, long past any grace -> STAY (no countdown)",
           d.action == WeatherAction.STAY)
    d = weather.inflight_decision(
        marg, is_emergency=True, warn_elapsed_s=0.0, now=_NOW, human_continue=True)
    _check("in-flight EMERGENCY MARGINAL is not affected by weather_continue -> still STAY",
           d.action == WeatherAction.STAY)
    d = weather.inflight_decision(sev, is_emergency=True, warn_elapsed_s=0.0, now=_NOW)
    _check("in-flight EMERGENCY SEVERE -> RETURN, reason says not overridable",
           d.action == WeatherAction.RETURN and any("not overridable" in r for r in d.reasons))
    d = weather.inflight_decision(marg, is_emergency=True, warn_elapsed_s=0.0, now=_NOW,
                                  human_recalled=True)
    _check("in-flight EMERGENCY, explicit human recall -> RETURN", d.action == WeatherAction.RETURN)


def test_observed_severe_from_throttle_alone() -> None:
    """Item 10: sustained high throttle, calm forecast, still SEVERE."""
    calm_forecast = weather.classify_forecast(_fc(), now=_NOW)
    _check("forecast is CLEAR", calm_forecast.tier == WeatherTier.CLEAR)
    obs = WeatherConditions(source="observed", age_s=0.0, wind_speed_ms=1.0,
                            throttle_pct=90.0)  # 10% margin, below THROTTLE_MARGIN_MIN_PCT
    v = weather.classify_observed(obs, now=_NOW, throttle_low_s=30.0)
    _check("sustained low throttle margin -> SEVERE from observed data alone",
           v.tier == WeatherTier.SEVERE and any("throttle" in r for r in v.reasons),
           str(v.reasons))
    v = weather.classify_observed(obs, now=_NOW, throttle_low_s=1.0)
    _check("brief low throttle margin -> only MARGINAL", v.tier == WeatherTier.MARGINAL)


def test_observed_lightning_carried_forward() -> None:
    """Item 9: lightning is absolute in flight too -- from the pre-flight
    forecast, carried forward as cached local data."""
    obs = WeatherConditions(source="observed", age_s=0.0, wind_speed_ms=1.0, throttle_pct=40.0)
    v = weather.classify_observed(obs, now=_NOW, forecast_lightning=True)
    _check("in-flight: forecast lightning carried forward -> SEVERE",
           v.tier == WeatherTier.SEVERE and any("lightning" in r for r in v.reasons))
    v = weather.classify_observed(obs, now=_NOW, forecast_lightning=False)
    _check("in-flight: no lightning -> CLEAR", v.tier == WeatherTier.CLEAR)


# --------------------------------------------------------------------------
# wind into the point of no return (item 12)
# --------------------------------------------------------------------------


def test_wind_into_point_of_no_return() -> None:
    now = _NOW
    snap = TelemetrySnapshot(
        timestamp=now, connected=True, lat=25.60, lon=85.26, position_valid=True,
        alt_m_amsl=600.0, alt_m_relative=28.0, heading_deg=90.0, groundspeed_ms=9.0,
        mode="GUIDED", armed=True, battery_pct=55.0, battery_voltage_v=23.0,
        battery_current_a=12.0, gps_fix_type=3, gps_satellites=12, link_age_s=0.4,
        telemetry_age_s=0.3, position_age_s=0.3, battery_age_s=0.4, attitude_age_s=0.3,
        gps_age_s=0.6, gps_hdop=0.9,
    )
    ctx = safety.SafetyContext(
        preflight=False, home_lat=config.HOME_LAT, home_lon=config.HOME_LON,
        target_lat=25.60, target_lon=85.26,
    )
    calm = safety.estimate_return_budget_pct(
        snap, ctx, discharge_rate_pct_per_s=0.05, wind_ms=0.0)
    headwind = safety.estimate_return_budget_pct(
        snap, ctx, discharge_rate_pct_per_s=0.05, wind_ms=10.0)
    print(f"\n  point-of-no-return battery budget: calm = {calm:.1f}%, "
          f"10 m/s headwind = {headwind:.1f}%")
    _check("a 10 m/s headwind raises the PNR budget measurably (turns around earlier)",
           headwind > calm + 20.0, f"calm {calm:.1f}% vs headwind {headwind:.1f}%")

    # v0.5.1 -- test 12 re-run: with the 10 m/s headwind the return budget is
    # 162% (more battery than a battery holds), so home is NOT reachable. The
    # verdict is DIVERT to a reachable safe spot, not RTL_NOW on a flight it
    # cannot finish. A moderate 3 m/s headwind still leaves home reachable ->
    # still RTL_NOW. Show all three side by side.
    from gss.snapshot import SafeSpot

    spot = SafeSpot("riverbank-clearing", 25.598, 85.26, 15.0, "field",
                    has_marker=True)  # ~220 m, ArUco pad
    ctx_spots = safety.SafetyContext(
        preflight=False, home_lat=config.HOME_LAT, home_lon=config.HOME_LON,
        target_lat=25.60, target_lon=85.26, safe_spots=(spot,))
    v_calm = safety.evaluate(snap, ctx_spots, now=now, discharge_rate_pct_per_s=0.05,
                             wind_ms=0.0)
    v_mod = safety.evaluate(snap, ctx_spots, now=now, discharge_rate_pct_per_s=0.05,
                            wind_ms=3.0, wind_speed_ms=3.0, wind_from_deg=90.0)
    v_wind = safety.evaluate(snap, ctx_spots, now=now, discharge_rate_pct_per_s=0.05,
                             wind_ms=10.0, wind_speed_ms=10.0, wind_from_deg=90.0)
    print(f"  same 55% battery:  calm -> {v_calm.action.value}   |   "
          f"3 m/s headwind -> {v_mod.action.value}   |   "
          f"10 m/s headwind -> {v_wind.action.value}")
    _check("test 12 re-run: calm -> ALLOW (home reachable)",
           v_calm.action == safety.SafetyAction.ALLOW, v_calm.action.value)
    _check("test 12 re-run: 10 m/s headwind -> DIVERT, NOT RTL_NOW (home unreachable)",
           v_wind.action == safety.SafetyAction.DIVERT
           and any("riverbank-clearing" in r for r in v_wind.reasons),
           f"{v_wind.action.value}  {list(v_wind.reasons)}")
    _check("test 12 re-run: a moderate 3 m/s headwind still -> RTL_NOW, not DIVERT "
           "(boundary works both ways)",
           v_mod.action == safety.SafetyAction.RTL_NOW, v_mod.action.value)

    # headwind geometry sanity
    _check("headwind: wind from the bearing you're flying -> full positive component",
           abs(weather.headwind_component_ms(90.0, 10.0, 90.0) - 10.0) < 1e-6)
    _check("headwind: wind from behind -> negative (tailwind, not credited by safety)",
           weather.headwind_component_ms(270.0, 10.0, 90.0) < 0)


# --------------------------------------------------------------------------
# the feed
# --------------------------------------------------------------------------


def test_feed_cache_and_staleness() -> None:
    cache = os.path.join(
        os.environ.get("TEMP", "/tmp"), f"wx_cache_{os.getpid()}.json")
    try:
        src = FakeForecastSource(wind_speed_ms=3.0)
        feed = WeatherFeed(config.HOME_LAT, config.HOME_LON, source=src,
                           cache_path=cache, fetch_interval_s=999)
        _check("feed with no data yet -> conditions age_s is None (classifies MARGINAL)",
               feed.current(_NOW).age_s is None)
        _check("fetch_once succeeds", feed.fetch_once() is True)
        cond = feed.current(datetime.now(timezone.utc))
        _check("after a fetch, conditions carry the source data and a small age",
               cond.wind_speed_ms == 3.0 and cond.age_s is not None and cond.age_s < 5.0)
        _check("cache file written", os.path.exists(cache))

        # a brand-new feed loads that cache from disk (Pi rebooted, no internet)
        feed2 = WeatherFeed(config.HOME_LAT, config.HOME_LON,
                            source=FakeForecastSource(), cache_path=cache,
                            fetch_interval_s=999)
        c2 = feed2.current(datetime.now(timezone.utc))
        _check("a fresh feed reads the disk cache on construction (offline boot)",
               c2.wind_speed_ms == 3.0)

        # a forecast older than WEATHER_MAX_AGE_S -> MARGINAL, not good news
        far = datetime.now(timezone.utc).replace(year=2020)
        stale_cond = feed2.current(datetime.now(timezone.utc))
        v_fresh = weather.classify_forecast(stale_cond, now=datetime.now(timezone.utc))
        v_stale = weather.classify_forecast(
            weather.WeatherConditions(source="forecast",
                                      age_s=config.WEATHER_MAX_AGE_S + 1,
                                      wind_speed_ms=1.0, wind_gust_ms=1.0,
                                      precip_mmh=0.0, visibility_m=9e9,
                                      temperature_c=25.0, lightning_nearby=False),
            now=datetime.now(timezone.utc))
        _check("fresh cached forecast classifies normally (CLEAR here)",
               v_fresh.tier == WeatherTier.CLEAR)
        _check("a forecast older than WEATHER_MAX_AGE_S -> MARGINAL",
               v_stale.tier == WeatherTier.MARGINAL and v_stale.stale)
        del far
    finally:
        for p in (cache, cache + ".tmp"):
            try:
                os.remove(p)
            except OSError:
                pass


def test_feed_api_down_is_harmless() -> None:
    """Item 13 / R10: a dead API never blocks; last-good is still served."""
    src = FakeForecastSource(wind_speed_ms=4.0)
    feed = WeatherFeed(config.HOME_LAT, config.HOME_LON, source=src,
                       cache_path="/nonexistent/dir/nope.json", fetch_interval_s=999)
    _check("first fetch OK", feed.fetch_once() is True)
    src.fail = True
    t0 = time.monotonic()
    ok = feed.fetch_once()
    dt = time.monotonic() - t0
    _check("a failing fetch returns quickly and False, never raises", ok is False and dt < 2.0,
           f"{dt:.2f}s")
    _check("last-good forecast is still served after the API goes down",
           feed.current(datetime.now(timezone.utc)).wind_speed_ms == 4.0)


# --------------------------------------------------------------------------
# import boundary (item 14)
# --------------------------------------------------------------------------


def test_import_boundary() -> None:
    probe = (
        "import sys\n"
        "import gss.weather\n"
        "roots = {'urllib','http','websockets','requests','socket','ssl','_socket',"
        "'_ssl','aiohttp','httpx','asyncio','ftplib','smtplib','poplib','imaplib',"
        "'telnetlib','xmlrpc','selectors'}\n"
        "exact = {'gss.store','gss.commands','gss.executor','gss.link',"
        "'gss.telemetry','gss.weather_feed','gss.safe_spots'}\n"
        "bad = sorted(m for m in sys.modules if m in exact or m.split('.')[0] in roots)\n"
        "print(repr(bad))\n"
    )
    env = dict(os.environ, SUPABASE_ENABLED="false")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                         text=True, env=env, timeout=60)
    printed = (out.stdout or "").strip()
    _check("import boundary: probe ran", out.returncode == 0, out.stderr.strip()[:300])
    _check(
        "import boundary: gss.weather pulls in NO networking module and none of "
        "gss.store / gss.commands / gss.executor / gss.link / gss.telemetry / "
        "gss.weather_feed (R1)",
        printed == "[]", f"leaked: {printed}",
    )


# --------------------------------------------------------------------------
# WEATHER_ENABLED=false is identical to v0.4 (item 15)
# --------------------------------------------------------------------------


def test_weather_disabled_is_v04() -> None:
    """Item 15: WEATHER_ENABLED=false runs exactly as v0.4 -- no monitor, no
    branch. Checked through main._make_weather (which main uses to wire it)."""
    import gss.main as gmain

    saved = config.WEATHER_ENABLED
    try:
        config.WEATHER_ENABLED = False
        _check("WEATHER_ENABLED=false -> _make_weather returns None (no monitor built)",
               gmain._make_weather(_FakeReader(), None) is None)

        config.WEATHER_ENABLED = True
        got = gmain._make_weather(_FakeReader(), None)
        _check("WEATHER_ENABLED=true -> a WeatherMonitor is built",
               isinstance(got, WeatherMonitor))
        if got is not None:
            got.close(timeout_s=1)
    finally:
        config.WEATHER_ENABLED = saved

    _check("weather.is_emergency_mission is a pure helper (summon=emergency, patrol=routine)",
           weather.is_emergency_mission("summon") is True
           and weather.is_emergency_mission("patrol") is False)


class _FakeReader:
    def get_snapshot(self):
        return None


# --------------------------------------------------------------------------
# LIVE layer -- fake vehicle + FAKE forecast source + real Supabase
# --------------------------------------------------------------------------

_LIVE = False
try:
    from dotenv import dotenv_values as _dv

    for _k, _v in _dv(".env").items():
        if _v:
            os.environ.setdefault(_k, _v)
    _LIVE = bool(os.environ.get("SUPABASE_SERVICE_ROLE_KEY"))
except Exception:  # noqa: BLE001
    pass


def _live_layer() -> None:
    import json
    import urllib.request

    from gss.commands import CommandIntake
    from gss.link import MavlinkLink
    from gss.safety import SafetyMonitor
    from gss.store import TelemetryStore
    from gss.telemetry import TelemetryReader
    from tests.fake_vehicle import FakeVehicle

    config.WEATHER_ENABLED = True
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
            t = r.read().decode()
            return json.loads(t) if t.strip() else None

    def wipe():
        rest("PATCH", "/rest/v1/missions",
             {"status": "aborted", "abort_reason": "test cleanup"},
             {"drone_id": f"eq.{DRONE}", "status": "not.in.(landed,aborted)"})
        rest("DELETE", "/rest/v1/mission_events", params={"id": "not.is.null"})
        rest("DELETE", "/rest/v1/alerts", params={"id": "not.is.null"})
        rest("DELETE", "/rest/v1/commands", params={"drone_id": f"eq.{DRONE}"})
        rest("DELETE", "/rest/v1/missions", params={"drone_id": f"eq.{DRONE}"})

    def cmd_row(cid, sel="status,rejected_reason,mission_id"):
        r = rest("GET", "/rest/v1/commands", params={"id": f"eq.{cid}", "select": sel})
        return r[0] if r else None

    def mrow(mid, sel="status,abort_reason,started_at,ended_at,created_at"):
        r = rest("GET", "/rest/v1/missions", params={"id": f"eq.{mid}", "select": sel})
        return r[0] if r else None

    def events(mid):
        return rest("GET", "/rest/v1/mission_events",
                    params={"mission_id": f"eq.{mid}", "select": "event,detail,at",
                            "order": "at.asc"}) or []

    def alerts(mid=None):
        p = {"select": "mission_id,channel,status,detail,triggered_at", "order": "triggered_at.asc"}
        if mid is not None:
            p["mission_id"] = f"eq.{mid}"
        return rest("GET", "/rest/v1/alerts", params=p) or []

    def insert(**kw):
        row = {"drone_id": DRONE, "type": "summon", "status": "pending",
               "issued_by": "test_weather", **kw}
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
        def __init__(self, *, forecast: FakeForecastSource, port=5831, airborne=True):
            self.src = forecast
            self.fv = FakeVehicle(port=port)
            self.fv.set_position(lat=25.5975, lon=85.2085, fix_type=3, satellites=12,
                                 heading_deg=90.0, groundspeed_ms=9.0)
            self.fv.set_battery(pct=90, voltage_v=24.6)
            self.fv.set_wind(speed_ms=1.0, direction_deg=270.0)
            self.fv.set_throttle(pct=40, climb_ms=0.0)
            if airborne:
                self.fv.set_armed(True)
                self.fv.set_position(rel_alt_m=25.0)
            self.fv.start()
            self.fv.wait_for_client(2)
            self.link = MavlinkLink(connection_string=f"tcp:127.0.0.1:{port}",
                                    heartbeat_timeout_s=4)
            self.reader = TelemetryReader(self.link)
            self.link.connect(timeout_s=8)
            time.sleep(3)
            self.store = TelemetryStore(self.reader.get_snapshot)
            cache = os.path.join(os.environ.get("TEMP", "/tmp"),
                                 f"wx_live_{port}.json")
            try:
                os.remove(cache)
            except OSError:
                pass
            self.feed = WeatherFeed(25.5932, 85.2045, source=forecast,
                                    cache_path=cache, fetch_interval_s=999)
            self.feed.fetch_once()
            self.weather = WeatherMonitor(self.reader.get_snapshot, feed=self.feed)
            self.safety = SafetyMonitor(self.reader.get_snapshot, home_lat=25.5932,
                                        home_lon=85.2045)
            self.safety.start()
            self.intake = CommandIntake(
                self.store, self.reader.get_snapshot, home_lat=25.5932, home_lon=85.2045,
                realtime_enabled=True, safety=self.safety, weather=self.weather)
            self.intake.start()
            time.sleep(2)

        def close(self):
            self.intake.close(4)
            self.safety.close(2)
            self.link.close()
            self.fv.stop()

    logbuf = io.StringIO()
    handler = logging.StreamHandler(logbuf)
    handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)

    # 2 -- routine (goto) marginal pre-flight: awaiting_confirmation + alert, no launch;
    #      timeout -> cancelled
    try:
        wipe()
        saved_timeout = config.WEATHER_CONFIRM_TIMEOUT_S
        config.WEATHER_CONFIRM_TIMEOUT_S = 6.0
        rig = Rig(forecast=FakeForecastSource(wind_speed_ms=9.0), port=5831, airborne=False)
        try:
            c = insert(type="goto", target_lat=25.60, target_lon=85.21)
            got = waitfor(lambda: cmd_row(c["id"])["mission_id"] is not None, 10)
            mid = cmd_row(c["id"])["mission_id"] if got else None
            _check("live-2: routine + marginal -> mission created, not launched", bool(mid),
                   str(cmd_row(c["id"])))
            _check("live-2: mission is awaiting_confirmation",
                   waitfor(lambda: mrow(mid)["status"] == "awaiting_confirmation", 4),
                   str(mrow(mid)) if mid else "")
            _check("live-2: an alert row was written for the pending decision",
                   any(a["detail"] and a["detail"].get("kind") == "awaiting_confirmation"
                       for a in alerts(mid)))
            _check("live-2: a weather_warning event names the conditions",
                   any(e["event"] == "weather_warning"
                       and any("wind" in r for r in (e["detail"] or {}).get("reasons", []))
                       for e in events(mid)))
            _check("live-2: never started (no started_at)", mrow(mid)["started_at"] is None)
            # let the confirm window expire
            _check("live-2: timeout with no answer -> mission cancelled (aborted)",
                   waitfor(lambda: mrow(mid)["status"] == "aborted", 10), str(mrow(mid)))
            _check("live-2: originating command rejected, reason mentions weather",
                   cmd_row(c["id"])["status"] == "rejected"
                   and "weather" in (cmd_row(c["id"])["rejected_reason"] or "").lower())
        finally:
            rig.close()
            config.WEATHER_CONFIRM_TIMEOUT_S = saved_timeout
    except Exception as exc:  # noqa: BLE001
        _check("live-2 (crashed)", False, repr(exc))

    # 3 -- routine marginal, weather_continue arrives -> launches
    try:
        wipe()
        rig = Rig(forecast=FakeForecastSource(wind_speed_ms=9.0), port=5832, airborne=False)
        try:
            c = insert(type="goto", target_lat=25.60, target_lon=85.21)
            waitfor(lambda: cmd_row(c["id"])["mission_id"] is not None, 10)
            mid = cmd_row(c["id"])["mission_id"]
            waitfor(lambda: mrow(mid)["status"] == "awaiting_confirmation", 5)
            rest("POST", "/rest/v1/commands",
                 {"drone_id": DRONE, "type": "weather_continue", "status": "pending",
                  "issued_by": "test_weather", "mission_id": mid})
            _check("live-3: weather_continue launches the held mission",
                   waitfor(lambda: mrow(mid)["status"] in
                           ("launching", "enroute", "on_station", "returning", "landed"), 10),
                   str(mrow(mid)))
            _check("live-3: mission eventually lands normally",
                   waitfor(lambda: mrow(mid)["status"] == "landed", 25), str(mrow(mid)))
        finally:
            rig.close()
    except Exception as exc:  # noqa: BLE001
        _check("live-3 (crashed)", False, repr(exc))

    # 4 -- emergency (summon) marginal pre-flight: launches immediately, no confirmation
    try:
        wipe()
        rig = Rig(forecast=FakeForecastSource(wind_speed_ms=9.0), port=5833, airborne=False)
        try:
            t0 = time.time()
            c = insert(type="summon", target_lat=25.60, target_lon=85.21)
            launched = waitfor(
                lambda: cmd_row(c["id"])["status"] in ("executing", "done"), 8)
            dt = time.time() - t0
            mid = cmd_row(c["id"])["mission_id"]
            _check("live-4: emergency + marginal launched", launched and bool(mid),
                   str(cmd_row(c["id"])))
            _check("live-4: nothing waited -- launched within a couple of poll cycles",
                   dt < 6.0, f"{dt:.1f}s")
            _check("live-4: no awaiting_confirmation was ever set",
                   mrow(mid)["status"] != "awaiting_confirmation")
            _check("live-4: a weather_warning event records it launched into marginal",
                   any(e["event"] == "weather_warning"
                       and (e["detail"] or {}).get("launched_into") == "marginal"
                       for e in events(mid)))
        finally:
            rig.close()
    except Exception as exc:  # noqa: BLE001
        _check("live-4 (crashed)", False, repr(exc))

    # 5 -- emergency severe pre-flight: refused, alert names condition/value/threshold
    try:
        wipe()
        rig = Rig(forecast=FakeForecastSource(wind_speed_ms=20.0), port=5834, airborne=False)
        try:
            c = insert(type="summon", target_lat=25.60, target_lon=85.21)
            _check("live-5: emergency + severe -> rejected",
                   waitfor(lambda: cmd_row(c["id"])["status"] == "rejected", 10),
                   str(cmd_row(c["id"])))
            r = cmd_row(c["id"])["rejected_reason"] or ""
            _check("live-5: reason names the condition, the measured value AND the threshold",
                   "wind" in r and "20." in r and f"{config.WIND_SEVERE_MS:.1f}" in r, r)
            al = [a for a in alerts() if a["detail"] and a["detail"].get("kind") == "emergency_refused"]
            _check("live-5: a loud alert row was written (no_drone_is_coming)",
                   bool(al) and al[0]["detail"].get("no_drone_is_coming") is True, str(al))
        finally:
            rig.close()
    except Exception as exc:  # noqa: BLE001
        _check("live-5 (crashed)", False, repr(exc))

    # 9 -- lightning in an emergency mission: SEVERE, no exception (pre-flight)
    try:
        wipe()
        rig = Rig(forecast=FakeForecastSource(lightning_nearby=True, wind_speed_ms=2.0),
                  port=5839, airborne=False)
        try:
            c = insert(type="summon", target_lat=25.60, target_lon=85.21)
            _check("live-9: emergency summon with lightning -> rejected (no exception)",
                   waitfor(lambda: cmd_row(c["id"])["status"] == "rejected", 10),
                   str(cmd_row(c["id"])))
            r = cmd_row(c["id"])["rejected_reason"] or ""
            _check("live-9: reason names lightning + the radius, not generic 'bad weather'",
                   "lightning" in r.lower()
                   and f"{config.LIGHTNING_RADIUS_KM:.0f}" in r, r)
            al = [a for a in alerts() if a["detail"]
                  and a["detail"].get("kind") == "emergency_refused"]
            _check("live-9: loud alert written for the refused emergency", bool(al))
        finally:
            rig.close()
    except Exception as exc:  # noqa: BLE001
        _check("live-9 (crashed)", False, repr(exc))

    # 6 -- routine mission in flight, conditions degrade -> warns, returns after grace
    try:
        wipe()
        saved_grace = config.WEATHER_WARN_GRACE_S
        config.WEATHER_WARN_GRACE_S = 3.0
        rig = Rig(forecast=FakeForecastSource(), port=5835, airborne=True)
        try:
            c = insert(type="goto", target_lat=25.60, target_lon=85.21)
            waitfor(lambda: cmd_row(c["id"])["status"] == "executing", 10)
            mid = cmd_row(c["id"])["mission_id"]
            waitfor(lambda: mrow(mid)["status"] in ("launching", "enroute"), 8)
            rig.fv.set_wind(speed_ms=9.0)  # observed MARGINAL
            _check("live-6: routine mission warns on degrading weather",
                   waitfor(lambda: any(e["event"] == "weather_warning" for e in events(mid)), 6),
                   str([e["event"] for e in events(mid)]))
            _check("live-6: and returns after the grace with no answer",
                   waitfor(lambda: mrow(mid)["status"] == "aborted"
                           and any(e["event"] == "weather_return" for e in events(mid)), 10),
                   str([e["event"] for e in events(mid)]))
        finally:
            rig.close()
            config.WEATHER_WARN_GRACE_S = saved_grace
    except Exception as exc:  # noqa: BLE001
        _check("live-6 (crashed)", False, repr(exc))

    # 7 -- EMERGENCY mission in flight, marginal: warns and STAYS. The key test.
    try:
        wipe()
        saved_grace = config.WEATHER_WARN_GRACE_S
        config.WEATHER_WARN_GRACE_S = 3.0
        rig = Rig(forecast=FakeForecastSource(), port=5836, airborne=True)
        try:
            c = insert(type="summon", target_lat=25.60, target_lon=85.21)
            waitfor(lambda: cmd_row(c["id"])["status"] == "executing", 10)
            mid = cmd_row(c["id"])["mission_id"]
            waitfor(lambda: mrow(mid)["status"] in ("launching", "enroute"), 8)
            rig.fv.set_wind(speed_ms=9.0)  # observed MARGINAL
            _check("live-7: emergency mission logs weather_hold (STAY), not weather_return",
                   waitfor(lambda: any(e["event"] == "weather_hold" for e in events(mid)), 8),
                   str([e["event"] for e in events(mid)]))
            time.sleep(config.WEATHER_WARN_GRACE_S * 3 + 3)  # well past any grace
            evs = [e["event"] for e in events(mid)]
            st = mrow(mid)["status"]
            _check("live-7: NO weather_return, NO weather abort -- it stayed and worked "
                   "through it, well past WEATHER_WARN_GRACE_S",
                   "weather_return" not in evs
                   and not (mrow(mid)["abort_reason"] or "").lower().startswith("weather"),
                   f"events={evs} status={st} abort={mrow(mid)['abort_reason']}")
            _check("live-7: mission runs to a natural landing, not a weather abort",
                   waitfor(lambda: mrow(mid)["status"] == "landed", 15), str(mrow(mid)))
        finally:
            rig.close()
            config.WEATHER_WARN_GRACE_S = saved_grace
    except Exception as exc:  # noqa: BLE001
        _check("live-7 (crashed)", False, repr(exc))

    # 8 -- emergency, conditions reach SEVERE in flight: returns, not overridable, numbers + alert
    try:
        wipe()
        rig = Rig(forecast=FakeForecastSource(), port=5837, airborne=True)
        try:
            c = insert(type="summon", target_lat=25.60, target_lon=85.21)
            waitfor(lambda: cmd_row(c["id"])["status"] == "executing", 10)
            mid = cmd_row(c["id"])["mission_id"]
            waitfor(lambda: mrow(mid)["status"] in ("launching", "enroute"), 8)
            rig.fv.set_wind(speed_ms=13.5)  # observed SEVERE
            _check("live-8: emergency + SEVERE in flight -> weather_return + mission aborted",
                   waitfor(lambda: mrow(mid)["status"] == "aborted"
                           and any(e["event"] == "weather_return" for e in events(mid)), 10),
                   str([e["event"] for e in events(mid)]))
            wr = [e for e in events(mid) if e["event"] == "weather_return"][0]
            _check("live-8: the reason carries NUMBERS (measured wind), not 'weather bad'",
                   isinstance((wr["detail"] or {}).get("measured", {}).get("wind_speed_ms"), (int, float))
                   and any("13." in r for r in (wr["detail"] or {}).get("reasons", [])),
                   str(wr["detail"]))
            _check("live-8: detail marks it not overridable (SEVERE)",
                   (wr["detail"] or {}).get("overridable") is False)
            _check("live-8: an alert row for the notification channel was written",
                   any(a["detail"] and a["detail"].get("kind") == "weather_return"
                       and a["detail"].get("returning_to") == "dock" for a in alerts(mid)))
        finally:
            rig.close()
    except Exception as exc:  # noqa: BLE001
        _check("live-8 (crashed)", False, repr(exc))

    # 11 -- weather says STAY, safety says RTL_NOW (battery floor): safety wins
    try:
        wipe()
        saved_grace = config.WEATHER_WARN_GRACE_S
        config.WEATHER_WARN_GRACE_S = 3.0
        rig = Rig(forecast=FakeForecastSource(), port=5838, airborne=True)
        try:
            c = insert(type="summon", target_lat=25.60, target_lon=85.21)
            waitfor(lambda: cmd_row(c["id"])["status"] == "executing", 10)
            mid = cmd_row(c["id"])["mission_id"]
            waitfor(lambda: mrow(mid)["status"] in ("launching", "enroute"), 8)
            rig.fv.set_wind(speed_ms=9.0)     # weather MARGINAL -> emergency would STAY
            waitfor(lambda: any(e["event"] == "weather_hold" for e in events(mid)), 8)
            mark = len(logbuf.getvalue())
            rig.fv.set_battery(pct=15, voltage_v=21.0)  # safety: below floor -> RTL_NOW
            _check("live-11: safety drove it home despite weather saying STAY",
                   waitfor(lambda: mrow(mid)["status"] == "aborted"
                           and any(e["event"] == "safety_veto" for e in events(mid)), 10),
                   str([e["event"] for e in events(mid)]))
            _check("live-11: abort reason is the SAFETY veto, not weather",
                   "safety veto" in (mrow(mid)["abort_reason"] or "").lower(),
                   mrow(mid)["abort_reason"])
            seg = logbuf.getvalue()[mark:]
            _check("live-11: the local log shows SAFETY RTL_NOW winning",
                   "SAFETY" in seg and "RTL_NOW" in seg)
        finally:
            rig.close()
            config.WEATHER_WARN_GRACE_S = saved_grace
    except Exception as exc:  # noqa: BLE001
        _check("live-11 (crashed)", False, repr(exc))

    # 13 -- weather API unreachable in flight: MAVLink + safety unaffected
    try:
        wipe()
        src = FakeForecastSource()
        src.fail = True  # API "down" from the start
        rig = Rig.__new__(Rig)
        # minimal rig: just the feed + monitor, no mission
        cache = os.path.join(os.environ.get("TEMP", "/tmp"), "wx_live_down.json")
        try:
            os.remove(cache)
        except OSError:
            pass
        feed = WeatherFeed(25.5932, 85.2045, source=src, cache_path=cache, fetch_interval_s=999)
        ok = feed.fetch_once()
        v = weather.classify_forecast(feed.current(datetime.now(timezone.utc)),
                                      now=datetime.now(timezone.utc))
        _check("live-13: API down, no cache -> forecast UNKNOWN -> MARGINAL (not SEVERE, not CLEAR)",
               ok is False and v.tier == WeatherTier.MARGINAL and v.stale)
        del rig
    except Exception as exc:  # noqa: BLE001
        _check("live-13 (crashed)", False, repr(exc))

    logging.getLogger().removeHandler(handler)
    wipe()


def main() -> int:
    for t in (
        test_classification_table,
        test_preflight_decisions,
        test_inflight_decisions,
        test_observed_severe_from_throttle_alone,
        test_observed_lightning_carried_forward,
        test_wind_into_point_of_no_return,
        test_feed_cache_and_staleness,
        test_feed_api_down_is_harmless,
        test_import_boundary,
        test_weather_disabled_is_v04,
    ):
        print(f"\n--- {t.__name__} ---")
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            _check(t.__name__ + " (crashed)", False, repr(exc))

    if _LIVE:
        print("\n=== LIVE layer (fake vehicle + FAKE forecast + real Supabase) ===")
        try:
            _live_layer()
        except Exception as exc:  # noqa: BLE001
            _check("live layer (crashed)", False, repr(exc))
    else:
        print("\nSKIP live layer: no .env with SUPABASE_SERVICE_ROLE_KEY")

    failed = [n for n, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        print("FAILED:")
        for n in failed:
            print(f"  - {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
