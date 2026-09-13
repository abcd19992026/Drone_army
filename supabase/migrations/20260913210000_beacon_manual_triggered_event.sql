-- Phase 13 -- manual "find_my_drone" command.
--
-- gss/commands.py writes 'beacon_manual_triggered' when the operator's
-- find_my_drone command forces gss/beacon.py's BeaconMonitor into its
-- MANUAL phase (via BeaconMonitor.trigger_manual()) and there is a
-- currently-active mission to attach the event to. Separate migration file
-- from Phase 12's beacon_deactivated one, as instructed -- that file is not
-- edited here.

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
    'beacon_deactivated',
    'beacon_manual_triggered'));
