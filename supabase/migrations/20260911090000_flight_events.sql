-- GSS v0.6 -- mission.py: real MAVLink flight in SITL
--
-- mission_events vocabulary gains four new event types:
--   * vehicle_adopted     -- startup found the vehicle armed and airborne with
--                            no software flying it; adopted, RTL ordered.
--   * standoff_recomputed -- the R2 downwind on-station point was (re)computed
--                            (wind changed, or DESCEND lowered the ceiling).
--   * landing_gps_only    -- a landing happened without the ArUco marker (RTL,
--                            LAND_NOW, or a DIVERT) -- precision landing is a
--                            later phase; this flags which landings were not.
--   * prearm_block        -- ArduPilot's own pre-arm check refused the flight;
--                            the executor did not attempt to bypass it.

alter table mission_events drop constraint mission_events_event_check;
alter table mission_events add constraint mission_events_event_check
  check (event in (
    'armed', 'takeoff', 'enroute', 'arrived', 'loiter_start',
    'battery_warning', 'point_of_no_return', 'rtl', 'link_lost',
    'link_restored', 'landed', 'aborted', 'beacon_activated',
    'spotlight_on', 'spotlight_off', 'siren_on', 'siren_off',
    'drop_released', 'geofence_block', 'safety_veto', 'manual_takeover',
    'photo_captured', 'recording_started', 'recording_stopped',
    'mission_created',
    'weather_warning', 'weather_hold', 'weather_return',
    'weather_launch_blocked',
    'divert',
    'vehicle_adopted', 'standoff_recomputed', 'landing_gps_only', 'prearm_block'));
