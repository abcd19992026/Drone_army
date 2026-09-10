"""Ground Station Service (GSS) for the personal security drone.

MAVLink link management, telemetry to the console, a best-effort Supabase
mirror of the telemetry (v0.2), and command intake -> dry-run missions (v0.3).
The only messages this package transmits over MAVLink are telemetry stream-rate
requests (see the send boundary in ``link.py``); network calls live in
``store.py`` / ``commands.py`` and are strictly downstream of the flight path
(rule R10). Nothing here arms, changes mode, or moves the aircraft -- v0.3
executes missions in dry-run only (rule R11).
"""

__version__ = "0.3.0"
