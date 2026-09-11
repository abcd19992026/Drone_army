# GSS — Ground Station Service

Python service that owns the MAVLink link to the drone and, in this version,
prints live telemetry to the console. It runs on the dock's Raspberry Pi in
production; for now it runs on Windows against ArduPilot SITL.

**Scope so far:** MAVLink link with forever-reconnect and telemetry to the
console; a best-effort Supabase mirror of the telemetry plus a live status
page (v0.2); command intake -> **dry-run** missions (v0.3); `safety.py`, the
veto authority (v0.4); `weather.py`, conditions awareness and the
stay-or-return decision (v0.5); `mission.py`, the first **real** MAVLink flight
in SITL (v0.6). `ALLOW_VEHICLE_CONTROL=false` (the default) still runs exactly
as v0.5 -- transmitting nothing but telemetry stream-rate requests (send
boundary at the top of `gss/link.py`, rule **R11**). Set it true against a
loopback SITL endpoint and the GSS will arm, take off, fly, and land for real;
against anything else (a serial port, a non-loopback address) it also needs
`ALLOW_REAL_VEHICLE=true` or it refuses to start, by name. Network calls live
in `gss/store.py`, `gss/commands.py` and `gss/weather_feed.py` and never block
the flight path (rule **R10**). See `PROJECT.md`.

**mission.py (v0.6):** `MavlinkExecutor`, the real-flight `Executor` behind
`make_executor()` once `ALLOW_VEHICLE_CONTROL` is true. Every command --
mode change, arm, takeoff, waypoint -- is sent through **one** chokepoint
(`gss.link.MavlinkLink.transmit()`, built only from `gss/mission.py`) and then
**confirmed from telemetry**, never assumed from the ACK; a confirmation
timeout aborts by the normal path, and arming is never retried after ArduPilot
rejects it. The flight sequence is GUIDED -> arm -> takeoff to
`CRUISE_ALT_OUTBOUND` -> fly to the on-station point at that altitude ->
descend to `ON_STATION_ALT` -> loiter -> climb to `CRUISE_ALT_INBOUND` -> RTL
-> land (outbound/inbound cruise altitudes differ on purpose). For a summon,
the on-station point is `LATERAL_OFFSET_M` **downwind** of the person (a power
loss must not drift the aircraft toward them); with no wind reading it falls
back to the bearing from the person toward the dock, and it is recomputed --
repositioning without ending the mission -- as the wind changes (rule **R2**,
closing a Phase-4 gap where a plain summon's on-station point was never
actually computed). An abort in the air is **always** a controlled return,
never a disarm: HOLD holds position, DESCEND stops and comes down to a safe
altitude, RTL_NOW/an abort command RTL, DIVERT flies to a safe spot and lands,
LAND_NOW lands where it is -- and a safety.py/weather.py verdict interrupts a
manoeuvre already in progress rather than waiting for it to finish. No code
path disarms above `DISARM_MAX_ALT_M`
(`tests/test_mission.py` greps `gss/mission.py` for one and asserts there is
none). On startup, before Supabase sync/weather/command-intake start,
`recover_airborne_vehicle()` checks the vehicle's actual state: armed and above
`DISARM_MAX_ALT_M` means a previous GSS process is gone but the aircraft is
still flying -- it logs CRITICAL, adopts it, and orders RTL immediately, before
touching the database. `audit_failsafe_params()` also runs at startup and
**reads** (never writes) ArduPilot's own battery/GCS/fence/RTL failsafe
parameters, reporting any mismatch loudly -- a human fixes them in Mission
Planner; this code verifies ArduPilot's failsafes, it does not replace them.
Landing is ArduPilot's own RTL/LAND for now; every landing writes a
`landing_gps_only` mission_event marking the seam for ArUco precision landing
(a later phase).

**weather.py (v0.5):** a pure core (conditions in, verdict out -- no I/O, no
clock reads, no network import) plus `gss/weather_feed.py`, the networked
fetcher kept on the far side of that boundary. The default flips on mission
type: for a **routine** mission (patrol/inspect/survey/test) deteriorating
weather means GO HOME; for an **emergency** mission (summon/family_summon/
search/accident) it means STAY and keep working, leaving only when conditions
reach SEVERE (unflyable) or a human explicitly recalls it. Authority order is
`safety.py > weather.py > commands > the human` -- weather can only make the
system more conservative, and never overrides a safety.py verdict. Pre-flight
uses the internet forecast (Open-Meteo, keyless; cached to disk); in flight it
classifies from the aircraft's OWN measurements (WIND, VIBRATION, VFR_HUD
throttle -- R1: no internet while airborne). Lightning within
`LIGHTNING_RADIUS_KM` is SEVERE with no emergency exception. The observed wind
feeds safety.py's point-of-no-return as a headwind component. New command types
`weather_continue` / `weather_recall`; new mission status `awaiting_confirmation`;
new `mission_events` `weather_warning` / `weather_hold` / `weather_return` /
`weather_launch_blocked`; `alerts` rows written for the (later) notification
channel. `WEATHER_ENABLED=false` runs exactly as v0.4.

**safety.py (v0.4):** a pure decision core (state in, verdict out -- no I/O, no
clock reads) wrapped in a thin loop thread (`SafetyMonitor`) at
`SAFETY_TICK_HZ`, with a watchdog on itself. It answers two questions: may this
command start a flight (pre-flight `REJECT`/`ALLOW`), and given the state right
now must the drone do something else
(`WARN`/`HOLD`/`DESCEND`/`RTL_NOW`/`DIVERT`/`LAND_NOW`). It is the final
authority -- no override, no bypass. When the headwind-aware point-of-no-return
budget says home is not reachable, the verdict is **`DIVERT`** -- fly to the
nearest reachable safe spot (`safe_spots` table + a disk cache + a config
fallback of at least the dock; safety.py gets the list as plain data, never
reads the DB) -- or **`LAND_NOW`** if nothing is reachable. Safe spots are two
tiers: an ArUco-marker pad lands accurately (a small pad is fine, and it is
preferred over a closer GPS-only spot by up to `SAFE_SPOT_MARKER_PREFERENCE_M`);
a GPS-only spot must be a real clear circle of `SAFE_SPOT_MIN_GPS_RADIUS_M` and
at dock level, or the selector refuses it. It **fails safe** (any check it
cannot evaluate -> DENY,
rule **R12**), its critical verdicts **latch** until the drone is on the ground
and disarmed (rule **R13**), and it imports **no** network and **no** database
code, directly or transitively (rule **R1**; `tests/test_safety.py` asserts the
boundary). It is a **hard start-up requirement**: if the monitor cannot be
built or started, the GSS logs why and exits non-zero -- it never runs without a
veto authority. `gss/commands.py` calls its pre-flight gate before accepting any
flight command, and the mission supervisor consults it every tick and drives the
`DryRunExecutor` on its verdict (every action handled explicitly; an
unrecognised one aborts, never falls through).

## Layout

```
gss/
  config.py       all settings + import-time validation (only place with a connection string)
  link.py         MavlinkLink: connect, receive on a daemon thread, reconnect,
                  on-connect callbacks, stream watchdog, stream-rate requests
  snapshot.py     TelemetrySnapshot -- the immutable state type, no imports beyond stdlib
  telemetry.py    TelemetryReader: latest-known state, per-field staleness (re-exports the snapshot)
  store.py        TelemetryStore: Supabase telemetry mirror + command/mission DB ops
  commands.py     CommandIntake: Realtime + poller -> claim -> validate -> dry-run mission
  executor.py     Executor interface + DryRunExecutor (v0.3) + make_executor()
  safety.py       the veto authority (v0.4): pure decision core + SafetyMonitor loop
  safe_spots.py   the divert-target list (v0.5.1): DB + disk cache + config fallback (outside R1)
  weather.py      conditions awareness (v0.5): pure classification + stay-or-return
  weather_feed.py the networked forecast fetcher + WeatherMonitor (outside the R1 boundary)
  mission.py      MavlinkExecutor (v0.6): real flight, confirm-every-step, standoff,
                  startup recovery of an airborne vehicle, failsafe-parameter audit
  main.py         entry point: wire it together, print a line every 2s
supabase/
  migrations/     the schema -- the source of truth (apply with the Supabase CLI)
web/
  status.html     single-file live status page + command buttons (Supabase Auth + Realtime)
  config.example.js  copy to config.js (gitignored) and fill in URL + anon key
tests/
  fake_vehicle.py     MAVLink vehicle over TCP with driveable state, clean/abrupt disconnect
  test_v011_fixes.py  MAVLink-path regressions (18)
  test_store.py       store.py vs a local mock HTTP endpoint (24)
  test_commands.py    v0.3 command intake vs the real Supabase project in .env (37)
  test_safety.py      v0.4 safety: pure scenario table + shell + import boundary + live layer
  test_weather.py     v0.5 weather: classification table + decisions + feed + boundary + live layer
  test_mission.py     v0.6 real flight vs fake_vehicle.py's kinematic flight sim (57)
```

## Setup (Windows)

```powershell
# from the project root
python -m venv venv
venv\Scripts\Activate.ps1        # cmd: venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and edit. The MAVLink defaults match SITL. The
Supabase side is on by default and **requires** `SUPABASE_URL` and
`SUPABASE_SERVICE_ROLE_KEY` — set `SUPABASE_ENABLED=false` to run console-only
with zero network calls (identical to v0.1.1).

```powershell
copy .env.example .env
# edit .env
```

`.env` is gitignored. The `SUPABASE_SERVICE_ROLE_KEY` bypasses RLS — it lives
only on the dock Pi and must never appear in a committed file or anything
under `web/`.

## Start ArduPilot SITL

Use Mission Planner's **SIMULATION** tab: pick a multirotor frame, set the
home location, and start. Home for this project:

```
lat 25.5932, lon 85.2045   (--home=25.5932,85.2045,50,0)
```

Mission Planner exposes SITL on TCP `127.0.0.1:5762`, which is the default
`MAVLINK_CONNECTION`. The GSS asks the vehicle for the telemetry streams it
needs on every connection, so it works on this port with nothing else running.

## Run the GSS

```powershell
python -m gss.main
```

One telemetry line every 2 seconds:

```
2026-09-10 14:03:01 INFO    gss.main: GSS v0.2.0 starting. MAVLink endpoint: tcp:127.0.0.1:5762
2026-09-10 14:03:01 INFO    gss.store: store: Supabase sync enabled -> https://<ref>.supabase.co ...
2026-09-10 14:03:01 INFO    gss.link: Link up. system=1 component=1 autopilot=MAV_AUTOPILOT_ARDUPILOTMEGA
2026-09-10 14:03:01 INFO    gss.telemetry: Telemetry streams requested via SET_MESSAGE_INTERVAL (5/5 messages)
[14:03:03] STABILIZE disarm alt    0.0m rel spd   0.0m/s hdg 354.6 POS  25.593200, 85.204500 bat 100% 12.59V   0.0A gps fix6/10sat link  0.2s
```

## Database (Supabase)

The schema lives in `supabase/migrations/` and is the source of truth. Apply
it with the [Supabase CLI](https://supabase.com/docs/guides/cli):

```powershell
# one-time
supabase login
supabase link --project-ref <your-project-ref>

# apply every migration to the linked (remote) database
supabase db push
```

For a local Postgres instead: `supabase start` then `supabase db reset`
(applies all migrations to the local stack).

The migrations create ten tables with RLS enabled from the first migration
(a logged-in browser may read everything and insert into `commands` only;
every other write is the GSS using the service_role key), enable Realtime on
`drones` and `commands`, seed one dock + one drone whose ids match the
`DRONE_ID` / `DOCK_ID` defaults in `gss/config.py`, and (v0.3) add the
`claim_command` RPC (service_role only) plus triggers that stamp
`acked_at` / `completed_at` / `started_at` / `ended_at` with the **database**
clock -- a Pi's clock is not trustworthy after a power cut.

## Command intake (v0.3)

When Supabase is on, the GSS also runs `CommandIntake`: it learns about new
rows in `commands` two ways -- a Realtime websocket (fast) and a poll every
`COMMAND_POLL_INTERVAL_S` (correct) -- claims each one atomically (so the two
paths, or two GSS instances, never double-execute), validates it, and for an
accepted `summon` / `goto` creates a mission and walks it through its states
with the **dry-run** executor. Nothing is sent to the vehicle. Rejections
write a specific `rejected_reason`. `abort` stops a running dry run promptly.

`ALLOW_VEHICLE_CONTROL` must stay `false` -- config refuses to start otherwise,
and the executor factory refuses to build anything but the dry runner. That is
the guard rail until `safety.py` (v0.4) and `MavlinkExecutor` (v0.5) exist.

## Live status page

`web/status.html` is a single self-contained file (Supabase JS from a CDN, no
build step — the React app is v0.5). Serve `web/` over HTTP so Realtime works:

```powershell
copy web\config.example.js web\config.js
# edit web/config.js -- URL + ANON key only, never the service_role key
python -m http.server 8000 --directory web
# open http://localhost:8000/status.html, sign in with a Supabase Auth user
```

It shows status, mode, armed, battery %/V, position, GPS fix, altitude,
heading, speed and link state, updating over Realtime. If `last_telemetry_at`
is older than 10 s it shows a prominent STALE banner instead of presenting old
numbers as current. Last-known position is shown separately with its own
timestamp.

A **Commands** card has Summon (uses browser geolocation), Hold, Abort, and a
deliberately-invalid "Summon 20 km away" button so you can watch a rejection
happen. Below it, the last 10 commands with their status and `rejected_reason`,
live over Realtime. These INSERTs into `commands` are the only writes the page
can do.

The console position label reads `POS ` when the fix is valid (real coordinates
**and** a 3D GPS fix) and `pos?` otherwise. Stop with **Ctrl+C**.

## Testing with the fake vehicle

`tests/fake_vehicle.py` is a stand-in for SITL: a MAVLink server over TCP whose
battery, position, GPS fix, armed state and mode are all driveable from a test,
and which can force a clean or an abrupt disconnect. Like real ArduPilot it
sends **only HEARTBEAT until asked** for streams. `enable_flight_sim()` (v0.6)
turns it into a small kinematic simulator: it answers mode changes, arming,
takeoff and GUIDED position targets, and moves the vehicle toward its target
at a driveable ground-speed/climb/descend rate -- enough to exercise
`mission.py`'s confirm-every-step state machine end to end without real
ArduPilot SITL. `test_mission.py`'s 14 verification items run against this
simulator; the real SITL/Mission Planner pass is still the user's to run.

Run it as a standalone server (defaults to port **5799**, never 5762):

```powershell
python -m tests.fake_vehicle --port 5799
python -m tests.fake_vehicle --port 5799 --heartbeat-only     # never streams (tests the watchdog)
python -m tests.fake_vehicle --port 5799 --battery-pct 15 --fix-type 2
```

Point the GSS at it:

```powershell
$env:MAVLINK_CONNECTION = "tcp:127.0.0.1:5799"; python -m gss.main
```

Drive it from Python (see `tests/test_v011_fixes.py` for worked examples):

```python
from tests.fake_vehicle import FakeVehicle
fv = FakeVehicle(port=5799); fv.start(); fv.wait_for_client(5)
fv.set_battery(pct=15, voltage_v=20.4, current_a=22.0)
fv.set_position(lat=25.5932, lon=85.2045, fix_type=3, satellites=11)
fv.disconnect_abrupt()      # RST — exercises the link's abrupt-loss path
fv.stop()
```

Run the test suites:

```powershell
python -m tests.test_v011_fixes   # MAVLink path, fake vehicle       (18 checks)
python -m tests.test_store        # store.py vs a local mock HTTP     (24 checks)
python -m tests.test_commands     # v0.3 intake vs the real project   (37 checks, ~5 min)
python -m tests.test_safety       # v0.4 safety vs the real project   (94 checks)
python -m tests.test_weather      # v0.5 weather vs the real project  (88 checks)
python -m tests.test_mission      # v0.6 real flight, fake_vehicle sim (57 checks, ~2 min)
```

`test_commands`, `test_safety` and `test_weather` need a reachable Supabase
(the `.env` project), create/delete rows for `DRONE_ID`, and **must be run one
at a time, never in parallel** -- they share `DRONE_ID` and each wipes the
others' rows on start. `test_mission` needs no network at all.

### Demoing the status page with the fake vehicle

```powershell
# terminal 1
python -m tests.fake_vehicle --port 5799
# terminal 2  (Supabase on, MAVLink pointed at the fake)
$env:MAVLINK_CONNECTION = "tcp:127.0.0.1:5799"; python -m gss.main
# terminal 3
python -m http.server 8000 --directory web   # open http://localhost:8000/status.html
```

Then drive values from a Python shell and watch the page update over Realtime:

```python
from tests.fake_vehicle import FakeVehicle   # or attach to the running one
fv.set_battery(pct=18, voltage_v=20.1)
fv.set_position(lat=25.9, lon=85.9, fix_type=1)   # -> page shows "no valid fix", position_valid false
fv.disconnect_abrupt()                            # -> page STALE banner after 10 s
```

## Verifying the acceptance criteria (v0.6)

Run `python -m tests.test_mission` (57 checks, against `fake_vehicle.py`'s
kinematic flight sim -- **not** real ArduPilot SITL; that pass is still the
user's to run in Mission Planner). It covers all 14 verification items:

1. a full summon flight lands with the outbound/on-station/inbound altitude
   profile confirmed at each phase (55/28/45 m in the test's config);
2. the on-station point is exactly `LATERAL_OFFSET_M` downwind, and moves when
   the wind flips 180°;
3. a mode change ArduPilot silently ignores aborts with a specific reason in
   ~`MODE_CONFIRM_TIMEOUT_S x MAVLINK_CMD_RETRIES`, never forever;
4. a rejected arm aborts on the ground with exactly one arm command sent, never
   retried;
5. battery draining mid-flight fires RTL_NOW **while a goto leg is still in
   progress** (proven by position samples, not by waiting for the leg to
   finish) and the vehicle actually turns around;
6. a link cut past the (shortened, for the test) grace period fires our own
   RTL_NOW, the link reconnects on its own, and the same mission id -- not a
   new mission -- is what continues;
7. an abort command RTLs and disarms only after it is back on the ground; a
   static grep of `gss/mission.py` proves there is no code path that requests
   a disarm at all;
8. **the most important test in this phase**: kill the executor thread
   mid-flight (simulating a crashed GSS process) and call
   `recover_airborne_vehicle()` cold -- it finds the still-armed, still-flying
   vehicle, logs CRITICAL, orders RTL, and the aircraft comes home and lands;
9. an emergency mission holds station through and well past
   `WEATHER_WARN_GRACE_S` under MARGINAL weather, proven with real position
   samples over time, not just the absence of a return event;
10. SEVERE weather on station during an emergency mission returns anyway, with
    measured numbers (wind speed) in the event detail, marked not overridable;
11. a chokepoint test greps every module under `gss/` for a flight-command
    MAVLink send and confirms only `gss/mission.py` builds one;
12. `ALLOW_VEHICLE_CONTROL=true` with a serial connection string and
    `ALLOW_REAL_VEHICLE` unset refuses to start, by name; a loopback SITL
    endpoint needs no second switch;
13. a deliberately misconfigured fake vehicle (`FENCE_ENABLE=0`,
    `FENCE_RADIUS=99999`, `RTL_ALT=0`) makes the failsafe audit report all
    three mismatches by name, and changes nothing;
14. all five earlier suites still pass (18+24+37+94+88, run individually).

Two things worth knowing about how the suite gets there:

- Item 5's battery level is chosen so RTL_NOW fires **and** home stays
  reachable within `RTL_RESERVE_PCT` -- safety.py's separate point-of-no-return
  escalation to a LAND-in-place (home genuinely unreachable) is real,
  correct behaviour, and is exercised on its own by items 9/10's low-battery
  paths, not conflated with this one.
- Item 6 sets `LINK_LOSS_GRACE_S=0.5` s for the run (production default is
  30 s): `MavlinkLink`'s reconnect backoff is read from config only right
  after a connection succeeds, not on every disconnect, so shortening the
  grace is the reliable way to prove "past the grace period, still
  disconnected" against a fake vehicle that reconnects in under ~2 s.

`hold` (the Phase-3 command type) is deliberately **not** wired to affect a
real `MavlinkExecutor` flight in v0.6 -- there is no `resume` command yet to
release it, and stranding a real mission indefinitely was judged worse than an
honest log line saying so. Only safety.py's own HOLD verdict reaches the
executor.

## Verifying the acceptance criteria (v0.3)

Run `python -m tests.test_commands` (37 checks). It covers, against the real
project: a valid summon walking the dry-run states with battery-stamped
events; a 20 km summon rejected with the distance and the limit in the reason;
a past `expires_at` rejected as expired; a foreign `drone_id` left `pending`;
a second summon rejected while one runs; the same command delivered via both
paths executed once; a stale-link summon rejected; `abort` taking a running
mission to `aborted` in well under a second; expiry unchanged with the local
clock warped to 2020; and the poller alone picking a command up with Realtime
off.

`ALLOW_VEHICLE_CONTROL=true` -> the GSS refuses to start.

## Verifying the acceptance criteria (v0.2)

**Database**

- `supabase db push` (or `supabase db reset` locally) applies all
  migrations to an empty database with no errors.

**Telemetry sync**

- With SITL up and `SUPABASE_ENABLED=true`, `python -m gss.main` UPDATEs the
  single `drones` row. It is rate-limited and skips redundant cycles, so an
  idle drone gets ~1 write per `SUPABASE_HEARTBEAT_WRITE_S`; a moving one gets
  one per `SUPABASE_TELEMETRY_INTERVAL_S`. `last_known_*` is written only when
  `position_valid` and never nulled.
- Point `SUPABASE_URL` at an unreachable host (`https://192.0.2.1`): the
  console keeps printing at full rate, the store logs retry warnings and
  `dropped N queued update(s)`, and nothing slows or crashes (**R10**).
- `SUPABASE_ENABLED=false`: `gss.store` is never imported, no store threads,
  console output identical to v0.1.1.

**MAVLink path (unchanged from v0.1.1)**

## Verifying the acceptance criteria (v0.1.1)

1. **Full telemetry within seconds, nothing priming the port.** Start SITL,
   then `python -m gss.main`. Position, battery, voltage, GPS fix, satellites,
   heading and speed all populate within ~2 s; the log shows
   `Telemetry streams requested via SET_MESSAGE_INTERVAL`.
2. **Kill SITL abruptly.** One `WARNING gss.link: Link lost (...)` line, no
   traceback, and the console keeps printing `LINK DOWN, reconnecting`.
   Backoff is `1 → 2 → 4 → 5 → 5 s`.
3. **Restart SITL, don't touch the GSS.** It reconnects and telemetry returns
   automatically — the GSS re-requests its streams on every reconnection.
4. **`python -m tests.fake_vehicle --heartbeat-only`**, point the GSS at it:
   after `STREAM_WATCHDOG_S` the log shows
   `Stream watchdog: no telemetry ... re-requesting streams`, repeating no more
   often than `STREAM_REREQUEST_MIN_S`.
5. **fix / position guard.** With the fake vehicle: `lat=0, lon=0` leaves
   `lat`/`lon` `None` and `position_valid` `False`; `fix_type=2` with real
   coordinates also leaves `position_valid` `False`.
6. **Connect log reports `component=1`.**

## No hardcoded values outside `config.py`

```powershell
Get-ChildItem gss -Filter *.py -Recurse |
  Where-Object Name -ne config.py |
  Select-String -Pattern '127\.0\.0\.1|:576\d|tcp:|\b(45|55|28|60|6000)\b'
```

Expect no matches. (bash: `grep -nE '127\.0\.0\.1|:576[0-9]|tcp:' gss/*.py | grep -v config.py`)
