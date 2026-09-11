"""GSS entry point: connect to the vehicle, print telemetry, mirror it upstream.

Run with::

    python -m gss.main

Connects to the MAVLink endpoint from :mod:`gss.config`, then prints one
telemetry line every ``TELEMETRY_INTERVAL_S`` seconds. If the link is down it
keeps printing a "link down" line and the background thread keeps retrying --
the process does not exit. Ctrl+C shuts down cleanly.

When ``SUPABASE_ENABLED`` is true it also starts
:class:`~gss.store.TelemetryStore` (telemetry mirror) and
:class:`~gss.commands.CommandIntake` (command intake -> dry-run missions),
each on its own threads. Both are strictly downstream of the MAVLink path
(rule R10): if they are slow, failing, or disabled, the console output above
is byte-for-byte unchanged.
"""

from __future__ import annotations

import logging
import sys
import threading
import time

from gss import __version__, config
from gss.link import MavlinkLink
from gss.telemetry import TelemetryReader, format_console_line

log = logging.getLogger("gss.main")

# How long to wait, after the link connects, for basic telemetry (armed state,
# relative altitude) to arrive before running the v0.6 airborne-recovery check.
# HEARTBEAT (armed/mode) is unconditional; altitude needs the stream request
# to have landed -- this is a bound on that, not a hard requirement (if it
# times out, recovery runs on whatever it has and says so).
_RECOVERY_TELEMETRY_GRACE_S = 5.0


def _configure_logging() -> None:
    """Set up timestamped logging at the configured level."""
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def run() -> int:
    """Start the link, stream telemetry to the console, and return an exit code."""
    _configure_logging()
    log.info("GSS v%s starting. MAVLink endpoint: %s", __version__, config.MAVLINK_CONNECTION)

    link = MavlinkLink()
    reader = TelemetryReader(link)
    store = None
    safe_spots = None
    safety = None
    weather = None
    intake = None
    shutdown = threading.Event()

    try:
        store = _make_store(reader)

        # The safe-spot book: the list safety.py diverts to when home is not
        # reachable. Never None -- config.SAFE_SPOTS_FALLBACK guarantees a
        # non-empty list even with no database and no cache. Constructing it
        # touches no network; only the background thread started below does.
        safe_spots = _make_safe_spots(store)
        safe_spots.start()

        # Safety is a HARD start-up requirement -- see _make_safety. If it
        # cannot be built or started, the GSS does not run. This is checked
        # before the link is even asked to connect, so a construction failure
        # exits fast and touches no network.
        safety = _make_safety(reader, store, safe_spots)
        try:
            safety.start()
        except Exception:
            log.critical(
                "Safety monitor failed to start; the GSS will not run without it "
                "(R12 -- fail safe, never fail open).",
                exc_info=True,
            )
            return 3

        # v0.6: NOW connect -- and before Supabase sync, weather, or command
        # intake starts a single thread, check whether there is an aircraft in
        # the sky that this process did not launch. Recovering it outranks
        # every other startup concern.
        if link.connect(timeout_s=10.0):
            log.info("Initial telemetry link established.")
        else:
            log.warning(
                "No link yet; the startup recovery/failsafe checks are skipped "
                "this run (nothing to check with no vehicle). The background "
                "thread keeps retrying."
            )

        adopted_detail = None
        if link.is_connected:
            _await_basic_telemetry(reader, _RECOVERY_TELEMETRY_GRACE_S)
            from gss import mission

            adopted_detail = mission.recover_airborne_vehicle(link, reader.get_snapshot)
            try:
                mission.audit_failsafe_params(link)
            except Exception:
                log.exception(
                    "failsafe parameter audit failed; continuing "
                    "(ArduPilot's own failsafes are unverified this run)"
                )

        if store is not None and adopted_detail is not None:
            store.log_event(None, "vehicle_adopted", detail=adopted_detail, sync=True)

        weather = _make_weather(reader, store)
        if weather is not None:
            weather.start()

        intake = _make_intake(store, reader, safety, weather, link, adopted_detail)
        if store is not None:
            store.start()
        if intake is not None:
            intake.start()

        while not shutdown.is_set():
            print(format_console_line(reader.get_snapshot()), flush=True)
            shutdown.wait(config.TELEMETRY_INTERVAL_S)
    except KeyboardInterrupt:
        log.info("Ctrl+C received, shutting down.")
    finally:
        if intake is not None:
            log.info("Stopping command intake...")
            intake.close(timeout_s=5.0)
        if weather is not None:
            log.info("Stopping weather monitor...")
            weather.close(timeout_s=3.0)
        if safety is not None:
            log.info("Stopping safety monitor...")
            safety.close(timeout_s=3.0)
        if safe_spots is not None:
            safe_spots.close(timeout_s=2.0)
        if store is not None:
            log.info("Flushing Supabase queue...")
            store.close(timeout_s=5.0)
        log.info("Closing link...")
        link.close()
        log.info("GSS stopped.")
    return 0


def _await_basic_telemetry(reader: TelemetryReader, timeout_s: float) -> None:
    """Best-effort wait for armed-state + relative-altitude to populate, so the
    v0.6 startup recovery check has something to look at. Never raises; a
    caller that still sees ``None`` fields treats that as "unknown" (fail
    safe), it does not skip the check."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        snap = reader.get_snapshot()
        if snap.armed is not None and snap.alt_m_relative is not None:
            return
        time.sleep(0.2)


def _make_safe_spots(store):
    """Build the safe-spot book. Never None: ``config.SAFE_SPOTS_FALLBACK``
    (the dock, at least) makes the list non-empty even with no database and no
    cache. R1 holds -- safety.py gets this list as plain data via a callable,
    it never reads the database itself. A ``None`` store just means the book
    runs on the disk cache + the config fallback.
    """
    from gss.safe_spots import SafeSpotBook

    fetch = store.list_safe_spots if store is not None else None
    return SafeSpotBook(fetch=fetch)


def _make_safety(reader: TelemetryReader, store, safe_spots):
    """Build the safety monitor. It runs regardless of Supabase (rule R1): a
    ``None`` store just means no best-effort event mirror, local logs stand.

    This is a HARD start-up requirement. If the monitor cannot be constructed,
    the GSS logs the reason and EXITS non-zero -- it does not run without a
    veto authority (R12: fail safe, never fail open).

    This is deliberately NOT gated on ALLOW_VEHICLE_CONTROL or dry-run mode.
    The danger is not that a dry run flies -- it is that "we will make this
    strict later" notes get lost, and the day someone wires MavlinkExecutor
    in, this door must already be shut.
    """
    try:
        from gss.safety import SafetyMonitor

        return SafetyMonitor(
            reader.get_snapshot,
            event_sink=(store.log_event if store is not None else None),
            safe_spots_source=(safe_spots.current if safe_spots is not None else None),
        )
    except Exception:
        log.critical(
            "Safety monitor could not be constructed; the GSS will not run "
            "without it (R12 -- fail safe, never fail open).",
            exc_info=True,
        )
        raise SystemExit(3)


def _make_weather(reader: TelemetryReader, store):
    """Build the weather monitor, or None.

    Weather is NOT a hard requirement: with ``WEATHER_ENABLED=false`` (or a
    construction failure) the GSS runs exactly as v0.4 did. weather.py can only
    make the system more conservative, never less, and it never overrides
    safety.py -- so its absence removes a feature, it does not remove a
    guarantee (rules R1/R10).
    """
    if not config.WEATHER_ENABLED:
        log.info("Weather awareness disabled (WEATHER_ENABLED=false); v0.4 behaviour.")
        return None
    try:
        from gss.weather_feed import WeatherMonitor

        return WeatherMonitor(
            reader.get_snapshot,
            event_sink=(store.log_event if store is not None else None),
        )
    except Exception:
        log.exception("Could not initialise weather awareness; continuing without it")
        return None


def _make_store(reader: TelemetryReader):
    """Build the telemetry store, or return None when persistence is disabled.

    A construction failure is logged and downgraded to None -- the GSS must
    still fly the drone and print to the console (rule R10).
    """
    if not config.SUPABASE_ENABLED:
        log.info("Supabase sync disabled (SUPABASE_ENABLED=false); console only.")
        return None
    try:
        from gss.store import TelemetryStore

        return TelemetryStore(reader.get_snapshot)
    except Exception:
        log.exception("Could not initialise Supabase sync; continuing without it")
        return None


def _make_intake(store, reader: TelemetryReader, safety, weather, link=None,
                  adopted_detail: dict | None = None):
    """Build the command intake, or None. Requires the store (Supabase).

    A construction failure is logged and downgraded to None -- the GSS still
    flies the drone and prints to the console (R10). ``link`` is passed through
    so an accepted flight command can build a real MavlinkExecutor (v0.6);
    ``adopted_detail`` (from the startup recovery check, run before this
    function) tells the orphan-mission reconciliation the truth about a vehicle
    found airborne, instead of a generic "orphaned" message.
    """
    if store is None:
        if config.SUPABASE_ENABLED:
            log.warning("Command intake needs Supabase; the store failed, so intake is off.")
        else:
            log.info("Command intake disabled (Supabase off).")
        return None
    try:
        from gss.commands import CommandIntake

        return CommandIntake(
            store, reader.get_snapshot, safety=safety, weather=weather,
            link=link, adopted_detail=adopted_detail,
        )
    except Exception:
        log.exception("Could not initialise command intake; continuing without it")
        return None


if __name__ == "__main__":
    sys.exit(run())
