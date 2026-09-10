-- GSS v0.5.1 -- safe spots + the DIVERT verdict
--
-- safety.py's point-of-no-return check can now conclude that home is NOT
-- reachable with the battery available (headwind-aware). The verdict is then
-- DIVERT: fly to the nearest reachable safe spot and land there, rather than
-- start a return it cannot finish. This table is that list of spots.
--
-- safety.py never reads this table (rule R1). gss/safe_spots.py loads it at
-- startup and every SAFE_SPOT_REFRESH_S, caches it to disk, and falls back to
-- gss/config.py SAFE_SPOTS_FALLBACK; the list reaches the pure safety core as
-- plain data.

-- ---------------------------------------------------------------------------
-- mission_events vocabulary gains 'divert'
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
    'divert'));

-- ---------------------------------------------------------------------------
-- safe_spots -- curated server-side, read-only to the browser.
create table safe_spots (
  id         uuid primary key default gen_random_uuid(),
  name       text not null,
  lat        double precision not null,
  lon        double precision not null,
  radius_m   double precision not null default 10.0,
  surface    text not null default 'open_ground'
               check (surface in ('open_ground', 'terrace', 'field', 'rooftop')),
  notes      text,
  active     boolean not null default true,
  priority   integer not null default 0,
  created_at timestamptz not null default now()
);

alter table safe_spots enable row level security;
create policy safe_spots_authenticated_read on safe_spots
  for select to authenticated using (true);
-- No client write policy: safe spots are added from the ground by a human who
-- has looked at the place, via the service_role key (which bypasses RLS).

-- Seed the one spot we already know is safe: the dock itself. Keep the
-- coordinates in sync with gss/config.py HOME_LAT / HOME_LON and the docks row.
insert into safe_spots (name, lat, lon, radius_m, surface, notes, priority)
select 'dock', 25.5932, 85.2045, 10.0, 'open_ground',
       'The home dock -- rooftop ArUco pad. Always the first choice.', 100
where not exists (select 1 from safe_spots where name = 'dock');
