"""GSS v0.1 entry point: connect to the vehicle and print telemetry.

Run with::

    python -m gss.main

Connects to the MAVLink endpoint from :mod:`gss.config`, then prints one
telemetry line every ``TELEMETRY_INTERVAL_S`` seconds. If the link is down it
keeps printing a "link down" line and the background thread keeps retrying --
the process does not exit. Ctrl+C shuts down cleanly.
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
    shutdown = threading.Event()

    try:
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
        log.info("Closing link...")
        link.close()
        log.info("GSS stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
