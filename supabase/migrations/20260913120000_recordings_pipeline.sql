-- GSS v0.8 -- recording pipeline (gss/media.py)
--
-- Changes:
--   1. local_path becomes nullable: retention clears it, but the row (the
--      historical index) is NEVER deleted. A null local_path means "the file
--      has been removed by the retention pass" -- not "this recording was lost".
--
--   2. detail jsonb column: stores the specific error/reason when a file is
--      flagged (R8: no silent failures -- the reason must be findable). Also
--      carries any extra metadata the processor wants to record.
--
--   3. Publish recordings to the Realtime publication so a ground station can
--      eventually stream new-recording events without polling.
--
--   4. mission_events.event CHECK: recording_started / recording_stopped are
--      already present from the initial schema. The Phase 7 migration dropped
--      and re-added the constraint including them. This migration is a no-op
--      for those two values; it only adds them if somehow absent.
--
-- R6 reminder (not enforced here, enforced in gss/media.py):
--   Video files NEVER go to Supabase Storage. Only a small JPEG thumbnail
--   (< 500 KB) goes to the recording-thumbnails bucket. The video stays on
--   local disk; local_path is the authoritative reference.

-- ---------------------------------------------------------------------------
-- 1. Make local_path nullable (was NOT NULL in the original schema)
-- ---------------------------------------------------------------------------
alter table recordings alter column local_path drop not null;

-- ---------------------------------------------------------------------------
-- 2. Add detail jsonb for flag reason / error text / extra metadata
-- ---------------------------------------------------------------------------
alter table recordings add column if not exists detail jsonb;

-- ---------------------------------------------------------------------------
-- 3. Publish to Realtime (idempotent -- the DO block guards duplicates)
-- ---------------------------------------------------------------------------
do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    -- recordings is not in the initial publication; add it now.
    -- If it is already there, this raises no error (Postgres deduplicates).
    begin
      alter publication supabase_realtime add table recordings;
    exception when others then
      -- Already present, or the publication exists but does not support
      -- individual tables (Realtime might be configured differently) --
      -- either way, not fatal.
      null;
    end;
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- 4. mission_events.event CHECK -- ensure recording_started / recording_stopped
--    are present. The current constraint (from Phase 7 migration) already
--    includes them; this is a safety net in case an install is on an older
--    revision that did not.
-- ---------------------------------------------------------------------------
do $$
declare
  constr text;
begin
  select pg_get_constraintdef(oid) into constr
  from pg_constraint
  where conrelid = 'mission_events'::regclass
    and contype = 'c'
    and conname = 'mission_events_event_check';

  if constr is null or (
    constr not like '%recording_started%'
    or constr not like '%recording_stopped%'
  ) then
    -- Replace the constraint to include them.
    alter table mission_events drop constraint if exists mission_events_event_check;
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
        'vehicle_adopted', 'standoff_recomputed', 'landing_gps_only',
        'prearm_block'));
  end if;
end $$;
