# GSS — Ground Station Service

Python service that owns the MAVLink link to the drone and, in this version,
prints live telemetry to the console. It runs on the dock's Raspberry Pi in
production; for now it runs on Windows against ArduPilot SITL.

**v0.1 scope:** project scaffold, MAVLink connection with forever-reconnect,
telemetry to the console. The only messages the GSS transmits are telemetry
stream-rate requests (`MAV_CMD_SET_MESSAGE_INTERVAL` / `REQUEST_DATA_STREAM` —
see the send boundary at the top of `gss/link.py`). No arming, no mode
changes, no database, no UI. See `PROJECT.md` for the full roadmap.

## Layout

```
gss/
  config.py       all settings + import-time validation (only place with a connection string)
  link.py         MavlinkLink: connect, receive on a daemon thread, reconnect with backoff,
                  on-connect callbacks, stream watchdog, stream-rate requests
  telemetry.py    TelemetrySnapshot + TelemetryReader: latest-known state; owns which
                  streams to request at what rate
  main.py         entry point: wire it together, print a line every 2s
tests/
  fake_vehicle.py test double: a MAVLink vehicle over TCP with driveable state
  test_v011_fixes.py  regression tests for the v0.1.1 fixes (uses fake_vehicle)
```

## Setup (Windows)

```powershell
# from the project root
python -m venv venv
venv\Scripts\Activate.ps1        # cmd: venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Configuration is optional — the defaults match SITL. To override anything:

```powershell
copy .env.example .env
# edit .env
```

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
2026-09-10 14:03:01 INFO    gss.main: GSS v0.1.1 starting. MAVLink endpoint: tcp:127.0.0.1:5762
2026-09-10 14:03:01 INFO    gss.link: Link up. system=1 component=1 autopilot=MAV_AUTOPILOT_ARDUPILOTMEGA
2026-09-10 14:03:01 INFO    gss.telemetry: Telemetry streams requested via SET_MESSAGE_INTERVAL (5/5 messages)
[14:03:03] STABILIZE disarm alt    0.0m rel spd   0.0m/s hdg 354.6 POS  25.593200, 85.204500 bat 100% 12.59V   0.0A gps fix6/10sat link  0.2s
```

The position label reads `POS ` when the fix is valid (real coordinates **and**
a 3D GPS fix) and `pos?` otherwise. Stop with **Ctrl+C**.

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

Run the regression tests:

```powershell
python -m tests.test_v011_fixes
```

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
