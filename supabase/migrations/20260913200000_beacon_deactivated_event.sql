-- Phase 12 -- Find-My-Drone beacon (spotlight + siren on link loss).
--
-- 'beacon_activated' was already in mission_events.event's CHECK ('beacon
-- payload activates here' was anticipated back in the initial schema).
-- 'beacon_deactivated' was not -- gss/beacon.py writes both, on every
-- transition into/out of an "on" phase (CONTINUOUS or PERIODIC_ON) for
-- whichever mission is currently active, if any.

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
    'beacon_deactivated'));
