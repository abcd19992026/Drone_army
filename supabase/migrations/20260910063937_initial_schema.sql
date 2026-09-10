-- GSS v0.2 -- initial schema
--
-- Conventions (every table):
--   * primary key: uuid default gen_random_uuid()   (system_config is the
--     one documented exception -- its key IS the identifier)
--   * every timestamp: timestamptz, UTC
--   * enum-like columns: text + CHECK, never a Postgres enum -- this
--     project's vocabulary keeps growing and altering a CHECK is far
--     cheaper than altering an enum type
--   * created_at timestamptz not null default now() on every table
--   * RLS enabled on every table, with policies, from this first migration
--
-- SECURITY: the service_role key bypasses RLS entirely. It is used ONLY by
-- the GSS (gss/store.py) on the dock Pi. It must NEVER appear in any
-- browser-delivered file -- nothing under web/. The browser uses the
-- anon / publishable key, constrained by the policies at the bottom of this
-- file: read-only on everything, insert only on `commands`.

-- ---------------------------------------------------------------------------
-- helper: touch updated_at on UPDATE
-- ---------------------------------------------------------------------------
create or replace function set_updated_at() returns trigger
language plpgsql
security invoker
set search_path = ''   -- pin: never resolve unqualified names from a caller's path
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

-- ---------------------------------------------------------------------------
-- docks
-- ---------------------------------------------------------------------------
create table docks (
  id              uuid primary key default gen_random_uuid(),
  name            text not null,
  lat             double precision,
  lon             double precision,
  address_label   text,
  status          text not null default 'ok'
                    check (status in ('ok', 'degraded', 'offline')),
  has_drone       boolean not null default false,
  power_source    text,
  charging_active boolean not null default false,
  last_health_at  timestamptz,
  notes           text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);
create trigger docks_updated_at before update on docks
  for each row execute function set_updated_at();

-- ---------------------------------------------------------------------------
-- drones -- ONE row per drone, forever. UPDATED in place, never appended to.
-- ---------------------------------------------------------------------------
create table drones (
  id                   uuid primary key default gen_random_uuid(),
  name                 text not null,
  status               text not null default 'docked'
    check (status in ('docked', 'charging', 'flying', 'perched', 'returning', 'error')),
  control_mode         text not null default 'auto'
    check (control_mode in ('auto', 'manual')),

  -- telemetry mirror (written by gss/store.py)
  battery_pct          double precision,
  battery_voltage_v    double precision,
  battery_current_a    double precision,
  mode                 text,
  armed                boolean,
  lat                  double precision,
  lon                  double precision,
  alt_m_relative       double precision,
  alt_m_amsl           double precision,
  heading_deg          double precision,
  groundspeed_ms       double precision,
  gps_fix_type         integer,
  gps_satellites       integer,
  position_valid       boolean not null default false,
  home_dock_id         uuid references docks(id),
  last_heartbeat_at    timestamptz,
  last_telemetry_at    timestamptz,
  link_up              boolean not null default false,
  firmware_version     text,

  -- payload & sensor state (populated from v1.0; present now so no
  -- migration is needed later)
  spotlight_on         boolean not null default false,
  siren_on             boolean not null default false,
  beacon_active        boolean not null default false,
  speaker_active       boolean not null default false,
  drop_loaded          boolean not null default false,
  drop_released        boolean not null default false,
  recording_active     boolean not null default false,
  camera_mode          text,
  lidar_distance_m     double precision,
  optical_flow_quality integer,

  -- last trustworthy fix. Written whenever position is valid, and
  -- deliberately NOT cleared when the link drops -- "where do I go
  -- looking for it".
  last_known_lat       double precision,
  last_known_lon       double precision,
  last_known_alt_m     double precision,
  last_known_at        timestamptz,

  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);
create trigger drones_updated_at before update on drones
  for each row execute function set_updated_at();

-- Realtime UPDATE events should carry the whole row so the status page can
-- render everything without a follow-up fetch.
alter table drones replica identity full;

-- ---------------------------------------------------------------------------
-- missions
-- ---------------------------------------------------------------------------
create table missions (
  id               uuid primary key default gen_random_uuid(),
  drone_id         uuid not null references drones(id),
  dock_id          uuid references docks(id),
  type             text not null
    check (type in (
      'summon', 'patrol', 'inspect', 'test', 'flood_survey', 'search',
      'accident', 'family_summon', 'manual', 'photo', 'video', 'orbit',
      'roi', 'panorama', 'timelapse', 'follow', 'survey_grid')),
  status           text not null default 'queued'
    check (status in (
      'queued', 'launching', 'enroute', 'on_station', 'returning',
      'landed', 'aborted')),
  target_lat       double precision,
  target_lon       double precision,
  cruise_alt_m     double precision,
  loiter_seconds   integer,
  started_at       timestamptz,
  ended_at         timestamptz,
  abort_reason     text,
  distance_m       double precision,
  battery_used_pct double precision,
  triggered_by     text,
  created_at       timestamptz not null default now()
);
create index missions_drone_id_created_at_idx on missions (drone_id, created_at desc);

-- ---------------------------------------------------------------------------
-- mission_events -- the full story of every flight; the debugging table.
-- ---------------------------------------------------------------------------
create table mission_events (
  id                uuid primary key default gen_random_uuid(),
  -- nullable: link_lost / link_restored / manual_takeover can be detected
  -- by the GSS outside any active mission and are still worth recording.
  mission_id        uuid references missions(id) on delete cascade,
  at                timestamptz not null default now(),
  event             text not null
    check (event in (
      'armed', 'takeoff', 'enroute', 'arrived', 'loiter_start',
      'battery_warning', 'point_of_no_return', 'rtl', 'link_lost',
      'link_restored', 'landed', 'aborted', 'beacon_activated',
      'spotlight_on', 'spotlight_off', 'siren_on', 'siren_off',
      'drop_released', 'geofence_block', 'safety_veto', 'manual_takeover',
      'photo_captured', 'recording_started', 'recording_stopped')),
  detail            jsonb,
  lat               double precision,
  lon               double precision,
  alt_m             double precision,
  -- battery + voltage on every event on purpose: the first debugging
  -- question about any incident is "what was the battery doing then".
  battery_pct       double precision,
  battery_voltage_v double precision,
  link_up           boolean,
  created_at        timestamptz not null default now()
);
create index mission_events_mission_id_at_idx on mission_events (mission_id, at);

-- ---------------------------------------------------------------------------
-- commands -- the joint of the whole system: phone inserts, GSS reacts.
-- ---------------------------------------------------------------------------
create table commands (
  id              uuid primary key default gen_random_uuid(),
  issued_at       timestamptz not null default now(),
  issued_by       text,
  drone_id        uuid not null references drones(id),   -- always addressed
  type            text not null
    check (type in (
      'summon', 'goto', 'perch', 'return', 'land', 'abort', 'hold',
      'takeoff', 'set_altitude', 'set_heading', 'nudge', 'orbit',
      'set_roi', 'photo', 'start_recording', 'stop_recording', 'panorama',
      'follow_me', 'spotlight_on', 'spotlight_off', 'siren_on', 'siren_off',
      'speaker_talk', 'drop_release', 'find_my_drone')),
  target_lat      double precision,
  target_lon      double precision,
  target_alt_m    double precision,
  params          jsonb,
  status          text not null default 'pending'
    check (status in ('pending', 'accepted', 'executing', 'done', 'rejected', 'expired')),
  mission_id      uuid references missions(id),
  rejected_reason text,
  acked_at        timestamptz,
  completed_at    timestamptz,
  -- a stale command is dangerous: if the GSS was offline for ten minutes it
  -- must not then execute a summon issued ten minutes ago. v0.3 enforces
  -- this; the column belongs in the schema now.
  expires_at      timestamptz,
  created_at      timestamptz not null default now()
);
create index commands_pending_idx on commands (issued_at) where status = 'pending';

-- ---------------------------------------------------------------------------
-- recordings -- video stays on local disk (R6); only this row is in the DB.
-- ---------------------------------------------------------------------------
create table recordings (
  id            uuid primary key default gen_random_uuid(),
  mission_id    uuid references missions(id),
  drone_id      uuid references drones(id),
  local_path    text not null,
  started_at    timestamptz,
  duration_s    double precision,
  size_mb       double precision,
  thumbnail_url text,
  flagged       boolean not null default false,
  delete_after  date,
  lat           double precision,
  lon           double precision,
  sha256        text,
  media_type    text not null default 'video'
                  check (media_type in ('video', 'photo')),
  width         integer,
  height        integer,
  created_at    timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- locations -- phone position history
-- ---------------------------------------------------------------------------
create table locations (
  id                uuid primary key default gen_random_uuid(),
  device_id         text,
  lat               double precision,
  lon               double precision,
  accuracy_m        double precision,
  recorded_at       timestamptz,
  phone_battery_pct double precision,
  created_at        timestamptz not null default now()
);
create index locations_device_recorded_idx on locations (device_id, recorded_at desc);

-- ---------------------------------------------------------------------------
-- contacts -- trusted people notified on an alert
-- ---------------------------------------------------------------------------
create table contacts (
  id         uuid primary key default gen_random_uuid(),
  name       text not null,
  phone      text,
  relation   text,
  priority   integer not null default 100,
  active     boolean not null default true,
  created_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- alerts -- record of contact notifications
-- ---------------------------------------------------------------------------
create table alerts (
  id                uuid primary key default gen_random_uuid(),
  mission_id        uuid references missions(id),
  triggered_at      timestamptz not null default now(),
  contacts_notified jsonb,
  channel           text not null default 'whatsapp'
                      check (channel in ('whatsapp', 'sms', 'voice')),
  status            text not null default 'queued'
                      check (status in ('queued', 'sending', 'sent', 'failed', 'partial')),
  template_name     text,
  error_detail      text,
  created_at        timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- system_config -- runtime-tunable values, changeable without a redeploy.
--
-- Deliberately NOT the home of anything safety.py depends on: those stay in
-- gss/config.py so they work with no network (R1). This table is only for
-- things that are safe to change at runtime.
-- ---------------------------------------------------------------------------
create table system_config (
  key         text primary key,
  value       jsonb not null,
  description text,
  updated_at  timestamptz not null default now(),
  created_at  timestamptz not null default now()
);
create trigger system_config_updated_at before update on system_config
  for each row execute function set_updated_at();

insert into system_config (key, value, description) values
  ('beacon.link_loss_activate_delay_s', to_jsonb(30),
   'Seconds after link loss before the spotlight + siren activate'),
  ('beacon.ground_continuous_s', to_jsonb(120),
   'Seconds the beacon runs continuously once on the ground before switching to periodic'),
  ('beacon.periodic_interval_s', to_jsonb(300),
   'Interval between periodic beacon pulses after the continuous phase'),
  ('recording.retention_days', to_jsonb(30),
   'Days to keep local recordings before media.py deletes them'),
  ('station.on_station_alt_m', to_jsonb(28),
   'Nominal loiter altitude over a target, metres relative. Fallback: gss/config.py ON_STATION_ALT'),
  ('station.lateral_offset_m', to_jsonb(12),
   'Nominal lateral offset from directly overhead, metres. Fallback: gss/config.py LATERAL_OFFSET_M')
on conflict (key) do nothing;

-- ---------------------------------------------------------------------------
-- Realtime
-- ---------------------------------------------------------------------------
do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    alter publication supabase_realtime add table drones;    -- status page
    alter publication supabase_realtime add table commands;  -- v0.3 command intake
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- Row Level Security
--
-- RLS is enabled on every table. The `authenticated` role (a logged-in
-- browser session) may SELECT everything and INSERT into `commands` only --
-- that is how the phone summons the drone in v0.6. Every other write is done
-- by the GSS with the service_role key, which bypasses RLS and needs no
-- policy.
-- ---------------------------------------------------------------------------
do $$
declare
  t text;
begin
  foreach t in array array[
    'docks', 'drones', 'missions', 'mission_events', 'commands',
    'recordings', 'locations', 'contacts', 'alerts', 'system_config'
  ] loop
    execute format('alter table %I enable row level security', t);
    execute format(
      'create policy %I on %I for select to authenticated using (true)',
      t || '_authenticated_read', t);
  end loop;
end $$;

create policy commands_authenticated_insert on commands
  for insert to authenticated with check (true);
