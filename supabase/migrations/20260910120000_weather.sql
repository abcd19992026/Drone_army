-- GSS v0.5 -- weather.py support
--
-- * missions.status gains 'awaiting_confirmation' -- a routine mission held
--   pre-flight while a human decides whether to launch into marginal weather.
-- * mission_events vocabulary gains the weather events.
-- * commands.type gains weather_continue / weather_recall -- the human's
--   answer to a weather hold or a request to bring an emergency mission home.
-- * alerts gains a `detail` jsonb column -- the exact condition, threshold and
--   measured value that triggered the alert (WhatsApp delivery is a later
--   phase; the row is written now).
-- * the mission-lifecycle trigger learns that 'awaiting_confirmation' is a
--   pre-start state, so started_at is stamped on the real launch.

-- ---------------------------------------------------------------------------
alter table missions drop constraint missions_status_check;
alter table missions add constraint missions_status_check
  check (status in (
    'queued', 'awaiting_confirmation', 'launching', 'enroute', 'on_station',
    'returning', 'landed', 'aborted'));

-- ---------------------------------------------------------------------------
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
    'weather_launch_blocked'));

-- ---------------------------------------------------------------------------
alter table commands drop constraint commands_type_check;
alter table commands add constraint commands_type_check
  check (type in (
    'summon', 'goto', 'perch', 'return', 'land', 'abort', 'hold',
    'takeoff', 'set_altitude', 'set_heading', 'nudge', 'orbit',
    'set_roi', 'photo', 'start_recording', 'stop_recording', 'panorama',
    'follow_me', 'spotlight_on', 'spotlight_off', 'siren_on', 'siren_off',
    'speaker_talk', 'drop_release', 'find_my_drone',
    'weather_continue', 'weather_recall'));

-- ---------------------------------------------------------------------------
alter table alerts add column if not exists detail jsonb;

-- ---------------------------------------------------------------------------
create or replace function stamp_mission_lifecycle() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  if old.status in ('queued', 'awaiting_confirmation')
     and new.status not in ('queued', 'awaiting_confirmation')
     and new.started_at is null then
    new.started_at = now();
  end if;
  if new.status in ('landed', 'aborted')
     and old.status not in ('landed', 'aborted')
     and new.ended_at is null then
    new.ended_at = now();
  end if;
  return new;
end;
$$;
