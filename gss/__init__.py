"""Ground Station Service (GSS) for the personal security drone.

MAVLink link management, telemetry to the console, and (v0.2) a best-effort
Supabase mirror of the telemetry. The only messages this package transmits
over MAVLink are telemetry stream-rate requests (see the send boundary in
``link.py``); the only network calls live in ``store.py`` and are strictly
downstream of the flight path (rule R10). Nothing here arms, changes mode,
or moves the aircraft.
"""

__version__ = "0.2.0"
