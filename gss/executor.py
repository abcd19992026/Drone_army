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
        """Request an immediate abort. Takes effect by the next :meth:`poll`."""

    @property
    @abc.abstractmethod
    def abort_reason(self) -> str | None:
        """The reason passed to :meth:`abort`, or None."""


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
) -> Executor:
    """The one factory. Adding MavlinkExecutor in v0.5 changes only this body.

    While ``allow_vehicle_control`` is false, this returns a DryRunExecutor and
    nothing else -- the guard rail that stops a future session from quietly
    wiring a real executor in before safety.py exists.
    """
    if not allow_vehicle_control:
        return DryRunExecutor(
            mission_id, command, snapshot_source, on_station_alt_m=on_station_alt_m
        )
    raise RuntimeError(
        "ALLOW_VEHICLE_CONTROL is true but this version has no vehicle-control "
        "executor. safety.py (v0.4) and MavlinkExecutor (v0.5) must land first. "
        "Refusing to construct a real executor."
    )
