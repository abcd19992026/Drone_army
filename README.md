# GSS — Ground Station Service

Python service that owns the MAVLink link to the drone and, in this version,
prints live telemetry to the console. It runs on the dock's Raspberry Pi in
production; for now it runs on Windows against ArduPilot SITL.

**Scope so far:** MAVLink link with forever-reconnect and telemetry to the
console; a best-effort Supabase mirror of the telemetry plus a live status
page (v0.2); command intake -> **dry-run** missions (v0.3). The GSS transmits
nothing over MAVLink but telemetry stream-rate requests (send boundary at the
top of `gss/link.py`, rule **R11**); network calls live in `gss/store.py` and
`gss/commands.py` and never block the flight path (rule **R10**). It does not
arm, change mode, take off, or send a waypoint -- v0.3 walks accepted missions
through their states on a timer and logs what a real flight *would* do.
`safety.py` (v0.4) and a real executor (v0.5) come next. See `PROJECT.md`.

## Layout

```
gss/
  config.py       all settings + import-time validation (only place with a connection string)
  link.py         MavlinkLink: connect, receive on a daemon thread, reconnect,
                  on-connect callbacks, stream watchdog, stream-rate requests
  telemetry.py    TelemetrySnapshot + TelemetryReader: latest-known state
  store.py        TelemetryStore: Supabase telemetry mirror + command/mission DB ops
  commands.py     CommandIntake: Realtime + poller -> claim -> validate -> dry-run mission
  executor.py     Executor interface + DryRunExecutor (v0.3) + make_executor()
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
sends **only HEARTBEAT until asked** for streams.

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
```

`test_commands` needs a reachable Supabase (the `.env` project) and creates /
deletes rows for `DRONE_ID` as it runs.

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
