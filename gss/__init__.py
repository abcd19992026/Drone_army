"""Ground Station Service (GSS) for the personal security drone.

v0.1 scope: MAVLink link management and telemetry to the console. The only
messages this package transmits are telemetry stream-rate requests (see the
send boundary in ``link.py``). Nothing here arms, changes mode, or moves the
aircraft.
"""

__version__ = "0.1.1"
