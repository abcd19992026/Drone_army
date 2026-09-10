"""GSS entry point: connect to the vehicle, print telemetry, mirror it upstream.

Run with::

    python -m gss.main

Connects to the MAVLink endpoint from :mod:`gss.config`, then prints one
telemetry line every ``TELEMETRY_INTERVAL_S`` seconds. If the link is down it
keeps printing a "link down" line and the background thread keeps retrying --
the process does not exit. Ctrl+C shuts down cleanly.

When ``SUPABASE_ENABLED`` is true it also starts :class:`~gss.store.TelemetryStore`,
which mirrors telemetry into the database on its own threads. That path is
strictly best-effort (rule R10): if it is slow, failing, or disabled, the
console output above is byte-for-byte unchanged.
"""

from __future__ import annotations

import logging
import sys
import threading

from gss import __version__, config
from gss.link import MavlinkLink
from gss.telemetry import TelemetryReader, format_console_line

log = logging.getLogger("gss.main")


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
    store = _make_store(reader)
    shutdown = threading.Event()

    try:
        if store is not None:
            store.start()

        # Give the link a short grace period so the first console line is
        # usually populated, but never block the telemetry loop on it -- the
        # background thread reconnects forever regardless.
        if link.connect(timeout_s=10.0):
            log.info("Initial telemetry link established.")
        else:
            log.warning("No link yet; printing status and retrying in the background.")

        while not shutdown.is_set():
            print(format_console_line(reader.get_snapshot()), flush=True)
            shutdown.wait(config.TELEMETRY_INTERVAL_S)
    except KeyboardInterrupt:
        log.info("Ctrl+C received, shutting down.")
    finally:
        if store is not None:
            log.info("Flushing Supabase queue...")
            store.close(timeout_s=5.0)
        log.info("Closing link...")
        link.close()
        log.info("GSS stopped.")
    return 0


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


if __name__ == "__main__":
    sys.exit(run())
