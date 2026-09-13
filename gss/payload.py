"""gss/payload.py -- the beacon's actuator boundary (Phase 12).

No payload hardware exists yet. :class:`PayloadActuator` is the seam
gss/beacon.py drives; :class:`FakePayloadActuator` records every call
(mirrors tests/fake_camera.py's role for the recording pipeline);
:class:`RealPayloadActuator` is an HONEST stub -- every method raises
``NotImplementedError`` rather than guess a MAVLink ``DO_SET_RELAY`` channel
or a GPIO pin scheme with no hardware to test either guess against. A wrong
guess here is worse than an honest failure: gss/beacon.py's tick catches it
(R8), so flipping :data:`gss.config.PAYLOAD_HARDWARE_ENABLED` on prematurely
degrades loudly instead of taking the GSS down.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod


class PayloadActuator(ABC):
    """Everything gss/beacon.py needs from the physical beacon payload."""

    @abstractmethod
    def spotlight_on(self) -> None: ...

    @abstractmethod
    def spotlight_off(self) -> None: ...

    @abstractmethod
    def siren_on(self) -> None: ...

    @abstractmethod
    def siren_off(self) -> None: ...


class FakePayloadActuator(PayloadActuator):
    """Records every call as ``(method_name, monotonic_timestamp)`` for tests
    to assert against. The default actuator whenever
    ``PAYLOAD_HARDWARE_ENABLED`` is false (which is every deployment today --
    no payload hardware exists yet)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []

    def spotlight_on(self) -> None:
        self.calls.append(("spotlight_on", time.monotonic()))

    def spotlight_off(self) -> None:
        self.calls.append(("spotlight_off", time.monotonic()))

    def siren_on(self) -> None:
        self.calls.append(("siren_on", time.monotonic()))

    def siren_off(self) -> None:
        self.calls.append(("siren_off", time.monotonic()))


class RealPayloadActuator(PayloadActuator):
    """No payload hardware exists yet -- every method is an honest stub.

    Do not fill these in with a guessed MAVLink ``DO_SET_RELAY`` channel or
    GPIO pin scheme: there is no hardware to test either guess against, and a
    wrong guess that "looks like it works" (wrong relay, wrong pin, inverted
    logic) is strictly worse than a loud, honest failure. Wire the real
    driver here once the payload hardware exists and has been bench-tested.
    """

    def spotlight_on(self) -> None:
        raise NotImplementedError("no payload hardware wired yet")

    def spotlight_off(self) -> None:
        raise NotImplementedError("no payload hardware wired yet")

    def siren_on(self) -> None:
        raise NotImplementedError("no payload hardware wired yet")

    def siren_off(self) -> None:
        raise NotImplementedError("no payload hardware wired yet")


def make_actuator() -> PayloadActuator:
    """The one place that decides Fake vs Real, gated on
    ``config.PAYLOAD_HARDWARE_ENABLED`` (default False)."""
    from gss import config

    if config.PAYLOAD_HARDWARE_ENABLED:
        return RealPayloadActuator()
    return FakePayloadActuator()
