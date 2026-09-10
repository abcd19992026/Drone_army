"""weather_feed.py -- the networked side of weather awareness (v0.5).

This module is allowed to touch the network; :mod:`gss.weather` is not, and
nothing that imports THIS file may be imported by :mod:`gss.safety` or
:mod:`gss.weather` (rule R1 -- the import-boundary test enforces it).

  * :class:`WeatherFeed` -- fetches the Open-Meteo forecast on a background
    thread, caches the last good result to disk (so a Pi that just booted
    with no internet still has something), and hands normalised conditions to
    the pure core. A slow or dead API never blocks a caller: everything the
    caller sees comes from an in-memory copy of the last good fetch (R10).
  * :class:`WeatherMonitor` -- the shell that ties the feed (pre-flight
    forecast) and the aircraft's own measurements (in-flight observed) to the
    pure decisions in :mod:`gss.weather`. It owns the throttle-sustained and
    vibration-trend tracking; the warn grace period and the human's answers
    are owned by the mission supervisor in ``gss/commands.py``.

FORECAST is pre-flight only. OBSERVED is in-flight only. See the
:mod:`gss.weather` docstring for why the split is not negotiable.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from gss import config, weather
from gss.snapshot import TelemetrySnapshot
from gss.weather import WeatherConditions, WeatherVerdict

log = logging.getLogger(__name__)

# WMO weather codes that mean a thunderstorm (lightning) at/near the location.
_THUNDERSTORM_CODES = frozenset({95, 96, 99})
_HTTP_TIMEOUT_S = 8.0
# Rolling window for the "vibration rising sharply" trend check.
_VIB_WINDOW = 12
_VIB_RISE_FACTOR = 1.8       # current vs window baseline
_VIB_RISE_MIN_ABS = 8.0      # ignore tiny absolute rises


# ===========================================================================
# forecast source
# ===========================================================================


@dataclass(frozen=True)
class RawForecast:
    """Normalised forecast, provider-independent."""

    wind_speed_ms: float | None
    wind_gust_ms: float | None
    precip_mmh: float | None
    visibility_m: float | None
    temperature_c: float | None
    lightning_nearby: bool | None

    def as_dict(self) -> dict:
        return {
            "wind_speed_ms": self.wind_speed_ms,
            "wind_gust_ms": self.wind_gust_ms,
            "precip_mmh": self.precip_mmh,
            "visibility_m": self.visibility_m,
            "temperature_c": self.temperature_c,
            "lightning_nearby": self.lightning_nearby,
        }

    @classmethod
    def from_dict(cls, d: dict) -> RawForecast:
        return cls(
            wind_speed_ms=d.get("wind_speed_ms"),
            wind_gust_ms=d.get("wind_gust_ms"),
            precip_mmh=d.get("precip_mmh"),
            visibility_m=d.get("visibility_m"),
            temperature_c=d.get("temperature_c"),
            lightning_nearby=d.get("lightning_nearby"),
        )


class ForecastSource(Protocol):
    def fetch(self, lat: float, lon: float) -> RawForecast: ...


class OpenMeteoSource:
    """Open-Meteo (https://api.open-meteo.com) -- free, keyless."""

    def __init__(self, url: str | None = None) -> None:
        self._url = url or config.WEATHER_PROVIDER_URL

    def fetch(self, lat: float, lon: float) -> RawForecast:
        params = {
            "latitude": f"{lat:.4f}",
            "longitude": f"{lon:.4f}",
            "wind_speed_unit": "ms",
            "current": ",".join(
                (
                    "wind_speed_10m", "wind_gusts_10m", "precipitation",
                    "visibility", "temperature_2m", "weather_code",
                )
            ),
            "hourly": "weather_code,wind_gusts_10m",
            "forecast_hours": "6",
        }
        url = self._url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "gss-weather/0.5"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return self._parse(body)

    @staticmethod
    def _parse(body: dict) -> RawForecast:
        cur = body.get("current") or {}
        hourly = body.get("hourly") or {}
        codes = [cur.get("weather_code")] + list(hourly.get("weather_code") or [])[:3]
        lightning = None
        known = [c for c in codes if c is not None]
        if known:
            lightning = any(int(c) in _THUNDERSTORM_CODES for c in known)
        gusts = [cur.get("wind_gusts_10m")] + list(hourly.get("wind_gusts_10m") or [])[:3]
        gusts = [g for g in gusts if g is not None]
        return RawForecast(
            wind_speed_ms=_f(cur.get("wind_speed_10m")),
            wind_gust_ms=max(gusts) if gusts else None,
            precip_mmh=_f(cur.get("precipitation")),
            visibility_m=_f(cur.get("visibility")),
            temperature_c=_f(cur.get("temperature_2m")),
            lightning_nearby=lightning,
        )


def _f(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# ===========================================================================
# the feed
# ===========================================================================


class WeatherFeed:
    """Background forecast fetcher with a disk cache. Everything a caller reads
    comes from an in-memory copy of the last good fetch -- a dead API never
    blocks anything (R10)."""

    def __init__(
        self,
        lat: float,
        lon: float,
        *,
        source: ForecastSource | None = None,
        cache_path: str | None = None,
        fetch_interval_s: float | None = None,
    ) -> None:
        self._lat = lat
        self._lon = lon
        self._source = source or OpenMeteoSource()
        self._cache_path = cache_path or config.WEATHER_CACHE_PATH
        self._interval_s = fetch_interval_s or config.WEATHER_FETCH_INTERVAL_S

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last: RawForecast | None = None
        self._last_fetched_at: datetime | None = None

        self._load_cache()

    # -- lifecycle --

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="weather-feed", daemon=True
        )
        self._thread.start()

    def close(self, timeout_s: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)

    # -- reads (no network, never block) --

    def current(self, now: datetime) -> WeatherConditions:
        with self._lock:
            raw = self._last
            fetched_at = self._last_fetched_at
        if raw is None or fetched_at is None:
            return WeatherConditions(source="forecast", age_s=None)
        return WeatherConditions(
            source="forecast",
            age_s=(now - fetched_at).total_seconds(),
            wind_speed_ms=raw.wind_speed_ms,
            wind_gust_ms=raw.wind_gust_ms,
            precip_mmh=raw.precip_mmh,
            visibility_m=raw.visibility_m,
            temperature_c=raw.temperature_c,
            lightning_nearby=raw.lightning_nearby,
        )

    def last_lightning(self) -> bool | None:
        with self._lock:
            return self._last.lightning_nearby if self._last is not None else None

    def fetch_once(self) -> bool:
        """Fetch synchronously (used by the loop and by tests). Returns success."""
        try:
            raw = self._source.fetch(self._lat, self._lon)
        except Exception as exc:  # noqa: BLE001 -- R10/R8: log, never propagate
            log.warning("weather: forecast fetch failed (%s: %s)", type(exc).__name__, exc)
            return False
        now = datetime.now(timezone.utc)
        with self._lock:
            self._last = raw
            self._last_fetched_at = now
        self._write_cache(raw, now)
        log.info(
            "weather: forecast updated (wind %s m/s, gust %s, precip %s mm/h, "
            "vis %s m, temp %s C, lightning %s)",
            raw.wind_speed_ms, raw.wind_gust_ms, raw.precip_mmh,
            raw.visibility_m, raw.temperature_c, raw.lightning_nearby,
        )
        return True

    # -- internals --

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.fetch_once()
            self._stop.wait(self._interval_s)

    def _load_cache(self) -> None:
        try:
            with open(self._cache_path, encoding="utf-8") as fh:
                blob = json.load(fh)
            self._last = RawForecast.from_dict(blob["data"])
            self._last_fetched_at = datetime.fromisoformat(blob["fetched_at"])
            log.info(
                "weather: loaded cached forecast from %s (fetched %s)",
                self._cache_path, blob["fetched_at"],
            )
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("weather: could not read cache %s (%s)", self._cache_path, exc)

    def _write_cache(self, raw: RawForecast, now: datetime) -> None:
        try:
            tmp = self._cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"fetched_at": now.isoformat(), "data": raw.as_dict()}, fh)
            import os

            os.replace(tmp, self._cache_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("weather: could not write cache %s (%s)", self._cache_path, exc)


# ===========================================================================
# the monitor
# ===========================================================================

EventSink = Callable[..., object]
SnapshotSource = Callable[[], TelemetrySnapshot]


class WeatherMonitor:
    """Ties the feed and the aircraft's measurements to the pure decisions.

    Pre-flight: :meth:`forecast_verdict`. In-flight: :meth:`observed_verdict`,
    called each supervisor tick, which also advances the throttle-sustained and
    vibration-trend trackers. When ``WEATHER_ENABLED`` is false this class is
    never constructed and ``gss/commands.py`` skips every weather branch.
    """

    def __init__(
        self,
        snapshot_source: SnapshotSource,
        *,
        feed: WeatherFeed | None = None,
        lat: float | None = None,
        lon: float | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        self._snapshot_source = snapshot_source
        self._event_sink = event_sink
        self._feed = feed or WeatherFeed(
            lat if lat is not None else config.HOME_LAT,
            lon if lon is not None else config.HOME_LON,
        )
        self._lock = threading.Lock()
        self._throttle_low_since: float | None = None
        self._vib_window: deque[float] = deque(maxlen=_VIB_WINDOW)
        # lightning from the forecast that was current when the mission started,
        # carried forward as cached LOCAL data (no in-flight network call).
        self._mission_forecast_lightning: bool | None = None

    # -- lifecycle --

    def start(self) -> None:
        self._feed.start()

    def close(self, timeout_s: float = 3.0) -> None:
        self._feed.close(timeout_s=timeout_s)

    @property
    def feed(self) -> WeatherFeed:
        return self._feed

    # -- pre-flight --

    def forecast_verdict(self, *, now: datetime | None = None) -> WeatherVerdict:
        now = now or datetime.now(timezone.utc)
        return weather.classify_forecast(self._feed.current(now), now=now)

    def begin_mission(self) -> None:
        """Snapshot the forecast lightning status for the mission and reset the
        in-flight trackers."""
        with self._lock:
            self._mission_forecast_lightning = self._feed.last_lightning()
            self._throttle_low_since = None
            self._vib_window.clear()

    def end_mission(self) -> None:
        with self._lock:
            self._mission_forecast_lightning = None
            self._throttle_low_since = None
            self._vib_window.clear()

    # -- in-flight --

    def observed_conditions(self, snapshot: TelemetrySnapshot) -> WeatherConditions:
        """Build in-flight conditions from the aircraft's own telemetry. A
        stale or missing field becomes ``None`` -> the pure classifier treats
        it as MARGINAL, never CLEAR."""
        wind = snapshot.wind_speed_ms
        if snapshot.wind_age_s is None or snapshot.wind_age_s > config.GPS_MAX_AGE_S:
            wind = None
        vib_max = None
        if (
            snapshot.vibration_age_s is not None
            and snapshot.vibration_age_s <= config.GPS_MAX_AGE_S
        ):
            vibs = [
                v for v in (snapshot.vibration_x, snapshot.vibration_y, snapshot.vibration_z)
                if v is not None
            ]
            vib_max = max(vibs) if vibs else None
        throttle = snapshot.throttle_pct
        if snapshot.attitude_age_s is None or snapshot.attitude_age_s > config.GPS_MAX_AGE_S:
            throttle = None
        return WeatherConditions(
            source="observed",
            age_s=0.0,
            wind_speed_ms=wind,
            wind_direction_deg=snapshot.wind_direction_deg,
            throttle_pct=throttle,
            vibration_max=vib_max,
        )

    def observed_verdict(self, *, now: datetime | None = None) -> WeatherVerdict:
        now = now or datetime.now(timezone.utc)
        snapshot = self._snapshot_source()
        conditions = self.observed_conditions(snapshot)
        mono = time.monotonic()

        with self._lock:
            # throttle-sustained tracker
            margin = (
                None if conditions.throttle_pct is None
                else 100.0 - conditions.throttle_pct
            )
            if margin is not None and margin < config.THROTTLE_MARGIN_MIN_PCT:
                if self._throttle_low_since is None:
                    self._throttle_low_since = mono
            else:
                self._throttle_low_since = None
            throttle_low_s = (
                0.0 if self._throttle_low_since is None
                else mono - self._throttle_low_since
            )

            # vibration trend
            rising = False
            if conditions.vibration_max is not None:
                if len(self._vib_window) >= max(3, _VIB_WINDOW // 2):
                    baseline = sum(self._vib_window) / len(self._vib_window)
                    rising = (
                        conditions.vibration_max > baseline * _VIB_RISE_FACTOR
                        and conditions.vibration_max - baseline >= _VIB_RISE_MIN_ABS
                    )
                self._vib_window.append(conditions.vibration_max)

            forecast_lightning = self._mission_forecast_lightning

        return weather.classify_observed(
            conditions,
            now=now,
            throttle_low_s=throttle_low_s,
            vibration_rising=rising,
            forecast_lightning=forecast_lightning,
        )
