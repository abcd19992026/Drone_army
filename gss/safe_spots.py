"""safe_spots.py -- the in-memory list of places the drone can land that are
NOT its dock (v0.5.1).

``safety.py`` decides to DIVERT when home is not reachable, but it cannot read
the database (rule **R1**). So the list lives here:

  * loaded from the ``safe_spots`` table at startup and every
    ``SAFE_SPOT_REFRESH_S`` (via an injected fetch function -- this module
    imports no networking library itself),
  * cached to disk so a Pi that boots with no network still has the last known
    set,
  * falling back to ``config.SAFE_SPOTS_FALLBACK`` (at minimum the dock) so the
    list is NEVER empty, even on first run with no database and no cache.

:class:`SafeSpotBook.current` is the callable handed to
:class:`gss.safety.SafetyMonitor` -- the pure safety core receives the spots as
plain :class:`~gss.snapshot.SafeSpot` data.

This module is OUTSIDE the safety/weather import boundary: it is wired in only
by ``gss.main`` and the fetch function is injected, so importing it pulls in no
socket / urllib / gss.store.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable, Sequence
from datetime import datetime, timezone

from gss import config
from gss.snapshot import SafeSpot

log = logging.getLogger(__name__)

_VALID_SURFACES = {"open_ground", "terrace", "field", "rooftop"}

FetchRows = Callable[[], "Sequence[dict]"]


def _to_spot(row: dict) -> SafeSpot | None:
    """One DB / cache / config row -> a :class:`SafeSpot`, or ``None`` if it is
    malformed or inactive (rule R8: skipped loudly, never silently swallowed)."""
    try:
        if row.get("active") is False:
            return None
        surface = str(row.get("surface") or "open_ground")
        if surface not in _VALID_SURFACES:
            surface = "open_ground"
        bearing = row.get("approach_bearing_deg")
        return SafeSpot(
            name=str(row["name"]),
            lat=float(row["lat"]),
            lon=float(row["lon"]),
            radius_m=float(row.get("radius_m") or 10.0),
            surface=surface,
            has_marker=bool(row.get("has_marker", False)),
            height_above_dock_m=float(row.get("height_above_dock_m") or 0.0),
            marker_id=(str(row["marker_id"]) if row.get("marker_id") else None),
            approach_bearing_deg=(float(bearing) if bearing is not None else None),
            hazards=(str(row["hazards"]) if row.get("hazards") else None),
        )
    except (KeyError, TypeError, ValueError):
        log.warning("safe_spots: ignoring malformed row %r", row)
        return None


def _parse_rows(rows: Sequence[dict] | None) -> list[SafeSpot]:
    return [s for s in (_to_spot(r) for r in (rows or [])) if s is not None]


class SafeSpotBook:
    """Holds the current safe-spot list, refreshes it in the background, and
    always has an answer (rule R1: the list is never empty)."""

    def __init__(
        self,
        fetch: FetchRows | None = None,
        *,
        cache_path: str | None = None,
        refresh_s: float | None = None,
    ) -> None:
        self._fetch = fetch
        self._cache_path = cache_path or config.SAFE_SPOT_CACHE_PATH
        self._refresh_s = (
            refresh_s if refresh_s is not None else config.SAFE_SPOT_REFRESH_S
        )
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Best guess available immediately: disk cache, else the config fallback.
        cached = self._load_cache()
        self._spots: tuple[SafeSpot, ...] = tuple(cached or self._fallback())
        self._source = "cache" if cached else "config fallback"

    # ------------------------------------------------------------------ API

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="safe-spots", daemon=True
        )
        self._thread.start()
        log.info(
            "safe_spots: book started (%d spot(s) from %s; refresh every %.0fs)",
            len(self._spots), self._source, self._refresh_s,
        )

    def close(self, timeout_s: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)
            self._thread = None

    def current(self) -> tuple[SafeSpot, ...]:
        with self._lock:
            return self._spots

    def refresh(self) -> bool:
        """Fetch the list once. Returns True only when a non-empty list was
        loaded and stored. Never raises (R8/R10): a failed refresh keeps the
        list that is already in hand."""
        if self._fetch is None:
            return False
        try:
            rows = self._fetch()
        except Exception:  # noqa: BLE001 -- a failed fetch is never fatal
            log.warning(
                "safe_spots: refresh failed; keeping the current list", exc_info=True
            )
            return False
        parsed = _parse_rows(rows)
        if not parsed:
            log.warning(
                "safe_spots: source returned no usable rows; keeping the current "
                "list (%d spot(s))", len(self._spots),
            )
            return False
        with self._lock:
            self._spots = tuple(parsed)
            self._source = "database"
        self._write_cache(parsed)
        log.info(
            "safe_spots: loaded %d spot(s) from the database: %s",
            len(parsed), ", ".join(s.name for s in parsed),
        )
        return True

    # ---------------------------------------------------------------- loop

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self._refresh_s)

    # -------------------------------------------------------------- helpers

    def _fallback(self) -> list[SafeSpot]:
        spots = _parse_rows(list(config.SAFE_SPOTS_FALLBACK))
        if not spots:
            # config validation already guards this, but never hand back [].
            log.error("safe_spots: config fallback produced no spots -- using the dock")
            return [SafeSpot("dock", config.HOME_LAT, config.HOME_LON)]
        return spots

    def _load_cache(self) -> list[SafeSpot] | None:
        try:
            with open(self._cache_path, encoding="utf-8") as fh:
                blob = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        spots = _parse_rows(blob.get("spots") if isinstance(blob, dict) else blob)
        return spots or None

    def _write_cache(self, spots: list[SafeSpot]) -> None:
        blob = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "spots": [
                {
                    "name": s.name,
                    "lat": s.lat,
                    "lon": s.lon,
                    "radius_m": s.radius_m,
                    "surface": s.surface,
                    "has_marker": s.has_marker,
                    "height_above_dock_m": s.height_above_dock_m,
                    "marker_id": s.marker_id,
                    "approach_bearing_deg": s.approach_bearing_deg,
                    "hazards": s.hazards,
                }
                for s in spots
            ],
        }
        try:
            directory = os.path.dirname(os.path.abspath(self._cache_path)) or "."
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".safe_spots_", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(blob, fh)
            os.replace(tmp, self._cache_path)
        except OSError as exc:
            log.warning("safe_spots: could not write cache %s: %s", self._cache_path, exc)
