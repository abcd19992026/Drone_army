"""Mission executor interface + the v0.3 dry-run implementation.

An executor is the seam between an accepted mission and the vehicle. v0.3
ships exactly one implementation -- :class:`DryRunExecutor` -- which sends
NOTHING over MAVLink (rule R11). It walks a mission through its phases on a
timer and logs, in unmistakable ``[DRY RUN]`` lines, what a real executor
would do at each step. ``gss/commands.py`` observes the phase changes and
writes the same ``mission_events`` a real flight would.

v0.5 adds ``MavlinkExecutor`` behind this same interface, with ``safety.py``
(v0.4) between it and the vehicle. Adding it changes only :func:`make_executor`.

The ``ALLOW_VEHICLE_CONTROL`` guard: :func:`make_executor` refuses to build
anything other than a :class:`DryRunExecutor` while that flag is false, and
config.py refuses to start the GSS at all while it is true (no safety.py yet).
"""

from __future__ import annotations

import abc
import logging
import threading
import time
from collections.abc import Callable
from enum import Enum

from gss.telemetry import TelemetrySnapshot

log = logging.getLogger(__name__)

SnapshotSource = Callable[[], TelemetrySnapshot]


class MissionPhase(str, Enum):
    """The mission lifecycle. Values match the ``missions.status`` CHECK."""

    QUEUED = "queued"
    LAUNCHING = "launching"
    ENROUTE = "enroute"
    ON_STATION = "on_station"
    RETURNING = "returning"
    LANDED = "landed"
    ABORTED = "aborted"


TERMINAL_PHASES = frozenset({MissionPhase.LANDED, MissionPhase.ABORTED})

# The mission_events to write when a phase is first entered. Every value is in
# the mission_events.event CHECK set.
PHASE_EVENTS: dict[MissionPhase, tuple[str, ...]] = {
    MissionPhase.LAUNCHING: ("armed", "takeoff"),
    MissionPhase.ENROUTE: ("enroute",),
    MissionPhase.ON_STATION: ("arrived", "loiter_start"),
    MissionPhase.RETURNING: ("rtl",),
    MissionPhase.LANDED: ("landed",),
    MissionPhase.ABORTED: ("aborted",),
}


class Executor(abc.ABC):
    """What a mission needs from whatever is flying it."""

    @abc.abstractmethod
    def start(self) -> None:
        """Begin the mission. Non-blocking."""

    @abc.abstractmethod
    def poll(self) -> MissionPhase:
        """Return the current phase. Called frequently by the supervisor."""

    @abc.abstractmethod
    def abort(self, reason: str) -> None:
        """Request an immediate abort. Takes effect by the next :meth:`poll`.

        For a real executor (v0.6) this is a CONTROLLED RETURN (RTL), never a
        disarm -- see :class:`gss.mission.MavlinkExecutor`.
        """

    @property
    @abc.abstractmethod
    def abort_reason(self) -> str | None:
        """The reason passed to :meth:`abort`, or None."""

    def on_safety_action(
        self,
        action: str,
        reason: str,
        *,
        divert_spot: dict | None = None,
        hold_kind: str | None = None,
    ) -> None:
        """Receive a mid-mission directive from the supervisor (a safety.py or
        weather.py verdict): one of ALLOW / WARN / HOLD / DESCEND / RTL_NOW /
        DIVERT / LAND_NOW. ALLOW/WARN mean the verdict has returned to nominal
        -- a signal to RELEASE a prior HOLD/DESCEND, never to abort.

        ``hold_kind`` (only meaningful for ``action == "HOLD"``) names which
        check ordered it -- ``"lateral_offset"`` for safety.py's R2 check,
        ``None`` for every other HOLD cause -- so a HOLD-aware executor can run
        a check-specific response instead of one generic idle-in-place hold.

        Default: treat anything that interrupts the mission as an abort, and
        ALLOW/WARN as a no-op. The dry runner overrides this to keep its
        finer, log-only behaviour for HOLD/DESCEND; :class:`gss.mission.
        MavlinkExecutor` overrides it to fly the manoeuvre.
        """
        if action in ("ALLOW", "WARN"):
            return
        self.abort(f"{action}: {reason}")

    def close(self) -> None:
        """Stop watching this mission WITHOUT commanding anything (GSS shutdown).

        Default: nothing to do (the dry runner transmits nothing anyway).
        :class:`gss.mission.MavlinkExecutor` overrides this to stop its flight
        thread while leaving the vehicle exactly as it is -- a real flight in
        progress must NOT be aborted just because the GSS process is stopping
        (rule R5); a restart reconciles it.
        """

    def drain_events(self) -> list[tuple[str, dict]]:
        """Extra mission_events (name, detail) pairs this executor produced
        since the last call -- written by the supervisor off the flight path.

        Default: none (the dry runner has nothing extra to report; every event
        it needs comes from the phase transitions the supervisor already
        tracks). :class:`gss.mission.MavlinkExecutor` overrides this for
        standoff-recomputed / GPS-only-landing / pre-arm-block notes.
        """
        return []


# Dry-run timeline: seconds from start() -> phase entered. Short so tests are
# quick; a real flight is minutes, which is why MISSION_MAX_DURATION_S exists.
_DRY_RUN_TIMELINE: tuple[tuple[float, MissionPhase], ...] = (
    (0.0, MissionPhase.LAUNCHING),
    (2.5, MissionPhase.ENROUTE),
    (6.0, MissionPhase.ON_STATION),
    (11.0, MissionPhase.RETURNING),
    (15.0, MissionPhase.LANDED),
)


class DryRunExecutor(Executor):
    """Walks a mission through its phases on a timer. Transmits nothing."""

    def __init__(
        self,
        mission_id: str,
        command: dict,
        snapshot_source: SnapshotSource,
        *,
        on_station_alt_m: float,
    ) -> None:
        self._mission_id = mission_id
        self._command = command
        self._snapshot = snapshot_source
        self._on_station_alt_m = on_station_alt_m
        self._lock = threading.Lock()
        self._start_mono: float | None = None
        self._aborted = False
        self._abort_reason: str | None = None
        tgt = (command.get("target_lat"), command.get("target_lon"))
        self._target_str = (
            f"{tgt[0]:.6f},{tgt[1]:.6f}" if None not in tgt else "(no target)"
        )

    def start(self) -> None:
        with self._lock:
            if self._start_mono is not None:
                return
            self._start_mono = time.monotonic()
        log.warning(
            "[DRY RUN] mission %s (%s): would arm, take off, and fly to %s, "
            "hold at %.0f m -- NOTHING is being sent to the vehicle",
            self._mission_id, self._command.get("type"), self._target_str,
            self._on_station_alt_m,
        )

    def poll(self) -> MissionPhase:
        with self._lock:
            if self._aborted:
                return MissionPhase.ABORTED
            if self._start_mono is None:
                return MissionPhase.QUEUED
            elapsed = time.monotonic() - self._start_mono
        phase = MissionPhase.QUEUED
        for at_s, ph in _DRY_RUN_TIMELINE:
            if elapsed >= at_s:
                phase = ph
        return phase

    def abort(self, reason: str) -> None:
        with self._lock:
            if self._aborted:
                return
            self._aborted = True
            self._abort_reason = reason
        log.warning(
            "[DRY RUN] mission %s: would abort and RTL now -- reason: %s",
            self._mission_id, reason,
        )

    @property
    def abort_reason(self) -> str | None:
        with self._lock:
            return self._abort_reason

    def on_safety_action(
        self,
        action: str,
        reason: str,
        *,
        divert_spot: dict | None = None,
        hold_kind: str | None = None,
    ) -> None:
        """Dry-run behaviour: ALLOW/WARN (verdict back to nominal) are ignored;
        HOLD / DESCEND are logged only (the timeline keeps walking -- a known
        dry-run limitation); everything else aborts, exactly as v0.4/v0.5 did
        before this method existed."""
        if action in ("ALLOW", "WARN"):
            return
        if action in ("HOLD", "DESCEND"):
            log.warning(
                "[DRY RUN] mission %s: would %s now -- %s",
                self._mission_id,
                "hold position" if action == "HOLD"
                else "stop and descend to a safe altitude",
                reason,
            )
            return
        where = ""
        if action == "DIVERT" and divert_spot:
            where = f" to '{divert_spot.get('spot_name')}'"
        self.abort(f"{action}{where}: {reason}")

    def describe_phase(self, phase: MissionPhase) -> str:
        """A '[DRY RUN] would ...' line for a newly entered phase."""
        return {
            MissionPhase.LAUNCHING: "would ARM and take off",
            MissionPhase.ENROUTE: f"would fly toward {self._target_str}",
            MissionPhase.ON_STATION: (
                f"would be on station at {self._on_station_alt_m:.0f} m, loitering"
            ),
            MissionPhase.RETURNING: "would return to the dock",
            MissionPhase.LANDED: "would land on the ArUco pad",
            MissionPhase.ABORTED: "would abort and RTL",
        }.get(phase, "would continue")


def make_executor(
    mission_id: str,
    command: dict,
    snapshot_source: SnapshotSource,
    *,
    allow_vehicle_control: bool,
    on_station_alt_m: float,
    link: object | None = None,
    safety: object | None = None,
    weather: object | None = None,
    home_lat: float | None = None,
    home_lon: float | None = None,
    loiter_seconds: float | None = None,
) -> Executor:
    """The one factory. It is the ONLY place that chooses which executor flies a
    mission.

    ``allow_vehicle_control`` false  -> :class:`DryRunExecutor` (transmits
    nothing). This stays the default and the guard rail.

    ``allow_vehicle_control`` true   -> :class:`gss.mission.MavlinkExecutor`
    (real MAVLink flight, v0.6). Requires a ``link``. config._validate() has
    already refused to start the GSS if the connection string is not a loopback
    SITL endpoint and ALLOW_REAL_VEHICLE is false.
    """
    if not allow_vehicle_control:
        return DryRunExecutor(
            mission_id, command, snapshot_source, on_station_alt_m=on_station_alt_m
        )
    if link is None:
        raise RuntimeError(
            "ALLOW_VEHICLE_CONTROL is true but make_executor() got no link -- "
            "refusing to build a MavlinkExecutor that cannot transmit."
        )
    from gss.mission import MavlinkExecutor

    return MavlinkExecutor(
        mission_id, command, snapshot_source,
        link=link, safety=safety, weather=weather,
        home_lat=home_lat, home_lon=home_lon,
        on_station_alt_m=on_station_alt_m, loiter_seconds=loiter_seconds,
    )
