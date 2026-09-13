"""gss/beacon.py -- Find-My-Drone beacon: spotlight + siren on link loss (Phase 12).

PROJECT.md Section 14: if the link is lost and stays lost, the drone should
make itself findable by sight and sound. No payload hardware exists yet --
see gss/payload.py for the actuator seam and its fake/real split.

DESIGN DECISION (not up for re-derivation): the beacon's timing is PURELY
time-based, keyed off "seconds since link was lost". The moment the link
dies, the GSS has no fresh telemetry at all -- it cannot confirm the drone
has actually landed, so `ground_continuous_s` is a TIMER WINDOW ("how long
to run both continuously"), not a literal on-ground sensor check.

State machine (see :func:`beacon_phase` for the pure decision):
  * link_up at any point                          -> OFF (reset everything;
    a later loss starts the timer from zero, not where it left off)
  * 0 .. activate_delay_s                          -> PENDING (no beacon yet
    -- overlaps link.py's own short-outage reconnect window, rule R5)
  * activate_delay_s .. +continuous_s              -> CONTINUOUS (both on
    solid)
  * beyond that                                    -> PERIODIC_ON /
    PERIODIC_OFF, alternating (on for periodic_on_s, off for
    periodic_interval_s, repeating) to conserve the payload battery during a
    long, unresolved search.

Mirrors the pure/shell split used by weather.py and scheduler.py:

  * :func:`beacon_phase` -- pure. No I/O, no clock reads.
  * :class:`BeaconMonitor` -- the shell. A background thread ticking every
    ``config.BEACON_TICK_S`` (tighter than weather's -- this is time-
    sensitive for a lost drone), reading link status from the injected
    snapshot_source (the same ``connected`` field weather.py reads
    battery/position from) and driving the injected
    :class:`~gss.payload.PayloadActuator` on phase transitions only -- never
    re-calling an unchanged phase's actuator methods every tick.

R8/R10: a tick never raises, whether the fault is an unreachable store, a
missing system_config row, or (rule check, item 9) a real payload driver
that isn't wired yet raising out of the actuator.

Phase 13 adds a MANUAL override (the phone's "find_my_drone" command,
handled in gss/commands.py's ``_handle_find_my_drone``): a
:class:`BeaconPhase` value that forces the beacon on regardless of link
status, for a configurable duration, via :meth:`BeaconMonitor.trigger_manual`.
gss/commands.py routes through this ONE monitor instance rather than driving
the payload actuator directly -- there must be exactly one thing driving it.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from enum import Enum

from gss import config
from gss.payload import PayloadActuator, make_actuator
from gss.snapshot import TelemetrySnapshot

log = logging.getLogger(__name__)

SnapshotSource = Callable[[], TelemetrySnapshot]
EventSink = Callable[..., object]

# Keys in the `system_config` table (seeded by the initial schema migration).
# periodic_on_s has no system_config key -- it is config.py-sourced only
# (see gss/config.py's BEACON_PERIODIC_ON_S_DEFAULT).
_KEY_ACTIVATE_DELAY_S = "beacon.link_loss_activate_delay_s"
_KEY_CONTINUOUS_S = "beacon.ground_continuous_s"
_KEY_PERIODIC_INTERVAL_S = "beacon.periodic_interval_s"


class BeaconPhase(str, Enum):
    OFF = "OFF"
    PENDING = "PENDING"
    CONTINUOUS = "CONTINUOUS"
    PERIODIC_ON = "PERIODIC_ON"
    PERIODIC_OFF = "PERIODIC_OFF"
    # Phase 13: forced on by the phone's "find_my_drone" command, independent
    # of link status. Same actuator effect as CONTINUOUS (both "on") -- a
    # distinct enum value only so tests and mission_events can tell a manual
    # trigger apart from an automatic link-loss activation.
    MANUAL = "MANUAL"


_ON_PHASES = frozenset({BeaconPhase.CONTINUOUS, BeaconPhase.PERIODIC_ON, BeaconPhase.MANUAL})


# ===========================================================================
# pure core
# ===========================================================================


def beacon_phase(
    seconds_since_link_lost: float | None,
    activate_delay_s: float,
    continuous_s: float,
    periodic_interval_s: float,
    periodic_on_s: float,
    now_in_cycle: float,
    *,
    now_mono: float | None = None,
    manual_until_mono: float | None = None,
) -> BeaconPhase:
    """Pure. No I/O, no clock reads.

    ``seconds_since_link_lost=None`` (link is up) always -> OFF, regardless
    of every other argument -- UNLESS a manual override is active (see
    below), which wins even with link up: that is the whole point of a
    manual "I can't see/hear it" trigger.

    ``now_in_cycle`` is time elapsed since entering the periodic regime
    (``seconds_since_link_lost - activate_delay_s - continuous_s``) -- it
    need not already be wrapped to one cycle's length; this function wraps
    it internally (``% (periodic_on_s + periodic_interval_s)``), so passing
    either the raw elapsed value or a pre-wrapped one gives the same answer.

    ``manual_until_mono`` (Phase 13): when set and ``now_mono`` is before
    it, the phase is unconditionally MANUAL, regardless of link status --
    same actuator effect as CONTINUOUS, distinct enum value. Once
    ``now_mono >= manual_until_mono`` this falls straight through to the
    existing link-based computation below exactly as if no override had
    ever existed -- expiry does NOT force OFF; if link is independently
    down long enough to justify CONTINUOUS or PERIODIC, that is what it
    lands on.
    """
    if (
        manual_until_mono is not None
        and now_mono is not None
        and now_mono < manual_until_mono
    ):
        return BeaconPhase.MANUAL

    if seconds_since_link_lost is None:
        return BeaconPhase.OFF
    if seconds_since_link_lost < activate_delay_s:
        return BeaconPhase.PENDING
    if seconds_since_link_lost < activate_delay_s + continuous_s:
        return BeaconPhase.CONTINUOUS

    cycle_length = periodic_on_s + periodic_interval_s
    position = now_in_cycle % cycle_length if cycle_length > 0 else 0.0
    return BeaconPhase.PERIODIC_ON if position < periodic_on_s else BeaconPhase.PERIODIC_OFF


# ===========================================================================
# the shell
# ===========================================================================


class BeaconMonitor:
    """Ticks the link status through :func:`beacon_phase` and drives the
    injected :class:`~gss.payload.PayloadActuator` on phase transitions.

    Same posture as :class:`~gss.weather_feed.WeatherMonitor`: constructed
    with ``snapshot_source`` positional and everything else keyword-only,
    ``event_sink`` optional. Unlike weather's thresholds (which must work
    with zero network, rule R1, and so live in gss/config.py), the beacon's
    three timing knobs are operator-tunable at runtime via the
    `system_config` table -- read ONCE at construction time here (never
    per-tick: a slow or dead Supabase must not add latency to an
    already-degraded, link-down situation), falling back to the
    gss/config.py defaults on any read failure.
    """

    def __init__(
        self,
        snapshot_source: SnapshotSource,
        *,
        store=None,
        actuator: PayloadActuator | None = None,
        drone_id: str | None = None,
        activate_delay_s: float | None = None,
        continuous_s: float | None = None,
        periodic_interval_s: float | None = None,
        periodic_on_s: float | None = None,
        tick_s: float | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        self._snapshot_source = snapshot_source
        self._store = store
        self._actuator = actuator or make_actuator()
        self._drone_id = drone_id or config.DRONE_ID
        self._tick_s = tick_s if tick_s is not None else config.BEACON_TICK_S
        self._event_sink = event_sink

        self._activate_delay_s = (
            activate_delay_s if activate_delay_s is not None
            else self._read_timing(_KEY_ACTIVATE_DELAY_S, config.BEACON_ACTIVATE_DELAY_S_DEFAULT)
        )
        self._continuous_s = (
            continuous_s if continuous_s is not None
            else self._read_timing(_KEY_CONTINUOUS_S, config.BEACON_CONTINUOUS_S_DEFAULT)
        )
        self._periodic_interval_s = (
            periodic_interval_s if periodic_interval_s is not None
            else self._read_timing(_KEY_PERIODIC_INTERVAL_S, config.BEACON_PERIODIC_INTERVAL_S_DEFAULT)
        )
        # No system_config key for this one -- config.py only (see module
        # docstring / gss/config.py's BEACON_PERIODIC_ON_S_DEFAULT comment).
        self._periodic_on_s = (
            periodic_on_s if periodic_on_s is not None else config.BEACON_PERIODIC_ON_S_DEFAULT
        )

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._link_lost_since_mono: float | None = None
        self._last_phase = BeaconPhase.OFF

        # Phase 13: the manual "find_my_drone" override. Written from the
        # command-intake thread via trigger_manual(), read from the beacon's
        # own tick thread -- a separate lock from anything else here since
        # the two threads never otherwise touch shared state.
        self._manual_lock = threading.Lock()
        self._manual_until_mono: float | None = None

    # -- lifecycle --

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="beacon", daemon=True)
        self._thread.start()

    def close(self, timeout_s: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)

    @property
    def phase(self) -> BeaconPhase:
        return self._last_phase

    # -- manual override (Phase 13) --

    def trigger_manual(self, duration_s: float) -> None:
        """Force the beacon on for ``duration_s`` seconds, regardless of link
        status. Thread-safe -- called from the command-intake thread, not
        this monitor's own tick thread.

        There is exactly ONE thing driving the payload actuator: this method
        does not touch the actuator itself, it only sets/extends the
        override end-time that the next tick's :func:`beacon_phase` call
        reads. Calling it again while already within an active window just
        pushes the end-time forward (debounce) -- it never stacks durations
        and never restarts from some other reference point.
        """
        with self._manual_lock:
            self._manual_until_mono = time.monotonic() + duration_s

    # -- construction-time config read (R10) --

    def _read_timing(self, key: str, default: float) -> float:
        if self._store is None:
            return default
        try:
            value = self._store.get_system_config(key, default)
            return float(value)
        except Exception:  # noqa: BLE001 -- R10: never block startup on a bad read
            log.warning(
                "beacon: system_config %r unreadable/non-numeric; using default %s",
                key, default,
            )
            return default

    # -- the tick --

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self._tick_s)

    def tick(self, *, now_mono: float | None = None) -> None:
        """Run one beacon decision pass. Safe to call directly (used by
        tests). Never raises (R8/R10) -- an actuator that raises (item 9: a
        prematurely-enabled RealPayloadActuator stub), an unreachable store,
        or anything else in here is caught, logged, and left for the next
        tick rather than crashing the GSS.
        """
        try:
            self._tick(now_mono if now_mono is not None else time.monotonic())
        except Exception:  # noqa: BLE001
            log.exception("beacon: tick failed")

    def _tick(self, now_mono: float) -> None:
        snap = self._snapshot_source()
        link_up = bool(snap.connected)

        if link_up:
            self._link_lost_since_mono = None
            seconds_since_link_lost = None
        else:
            if self._link_lost_since_mono is None:
                self._link_lost_since_mono = now_mono
            seconds_since_link_lost = now_mono - self._link_lost_since_mono

        now_in_cycle = 0.0
        if seconds_since_link_lost is not None:
            now_in_cycle = max(
                0.0, seconds_since_link_lost - self._activate_delay_s - self._continuous_s
            )

        with self._manual_lock:
            manual_until_mono = self._manual_until_mono

        phase = beacon_phase(
            seconds_since_link_lost,
            self._activate_delay_s,
            self._continuous_s,
            self._periodic_interval_s,
            self._periodic_on_s,
            now_in_cycle,
            now_mono=now_mono,
            manual_until_mono=manual_until_mono,
        )

        if phase == self._last_phase:
            return
        prev_phase = self._last_phase
        # _last_phase is updated only AFTER a successful transition: if the
        # actuator raises (item 9 -- a prematurely-enabled RealPayloadActuator
        # stub, or a real future driver fault), the phase must not silently
        # "latch" as changed while the light/siren never actually moved --
        # the next tick retries the same transition instead of skipping it.
        self._handle_transition(prev_phase, phase)
        self._last_phase = phase

    def _handle_transition(self, prev_phase: BeaconPhase, phase: BeaconPhase) -> None:
        prev_on = prev_phase in _ON_PHASES
        new_on = phase in _ON_PHASES

        event: str | None = None
        if new_on and not prev_on:
            self._actuator.spotlight_on()
            self._actuator.siren_on()
            event = "beacon_activated"
            log.warning("beacon: ACTIVATED (%s -> %s)", prev_phase.value, phase.value)
        elif prev_on and not new_on:
            self._actuator.spotlight_off()
            self._actuator.siren_off()
            event = "beacon_deactivated"
            log.info("beacon: deactivated (%s -> %s)", prev_phase.value, phase.value)
        else:
            log.debug("beacon: %s -> %s (no actuator change)", prev_phase.value, phase.value)

        if event is not None:
            self._write_mission_event(event, phase)

    def _write_mission_event(self, event: str, phase: BeaconPhase) -> None:
        """Only when a mission is currently active (non-terminal) -- see the
        module docstring. No active mission -> just the log line above, no
        forced write, no crash."""
        if self._event_sink is None or self._store is None:
            return
        try:
            missions = self._store.list_active_missions(self._drone_id)
        except Exception:  # noqa: BLE001 -- R10
            log.exception("beacon: could not check for an active mission")
            return
        if not missions:
            return
        mission_id = missions[0]["id"]
        try:
            self._event_sink(mission_id, event, {"phase": phase.value}, sync=True)
        except Exception:  # noqa: BLE001 -- R8/R10
            log.exception("beacon: mission_events write failed")
