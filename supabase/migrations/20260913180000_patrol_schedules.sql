-- Phase 9 -- patrol_schedules: recurring flights on a timer.
--
-- One row = one recurring target lat/lon (v1: not a multi-waypoint route --
-- several plots means several rows). gss/scheduler.py reads active rows here
-- and, when one is due, writes a single ordinary `goto` row to `commands`;
-- it never writes to missions/mission_events/drones directly.
--
-- RLS differs from `commands` on purpose: this table is not safety-critical
-- -- it never touches the vehicle directly, it only ever produces a normal,
-- fully-gated command that goes through the exact same commands.py pipeline
-- (safety.py veto, weather.py hold) as any human-issued one. So, unlike
-- every other table in this project, `authenticated` gets full
-- SELECT/INSERT/UPDATE/DELETE here -- a future React screen can manage
-- schedules with no server-side proxy. service_role (the GSS) bypasses RLS
-- as always.
create table patrol_schedules (
  id                  uuid primary key default gen_random_uuid(),
  name                text not null,
  target_lat          double precision not null
    check (target_lat between -90 and 90),
  target_lon          double precision not null
    check (target_lon between -180 and 180),
  cruise_alt_m        double precision,   -- null -> gss/config.py ON_STATION_ALT
  loiter_seconds      integer not null default 120
    check (loiter_seconds > 0),
  cadence             text not null check (cadence in ('daily', 'weekly')),
  -- Local Asia/Kolkata wall-clock time. gss/scheduler.py converts this to UTC
  -- explicitly (zoneinfo), independent of the host OS's timezone setting --
  -- see that module's docstring for why.
  time_of_day         time not null,
  -- Only used when cadence='weekly'. 0=Sunday .. 6=Saturday.
  days_of_week        integer[]
    check (days_of_week is null or days_of_week <@ array[0, 1, 2, 3, 4, 5, 6]),
  active              boolean not null default true,
  last_run_at         timestamptz,
  last_skipped_reason text,
  next_run_at         timestamptz not null,
  dock_id             uuid references docks(id),
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  constraint patrol_schedules_days_of_week_matches_cadence check (
    (cadence = 'daily' and days_of_week is null) or
    (cadence = 'weekly' and days_of_week is not null and array_length(days_of_week, 1) > 0)
  )
);

create trigger patrol_schedules_updated_at before update on patrol_schedules
  for each row execute function set_updated_at();

create index patrol_schedules_dock_id_idx on patrol_schedules (dock_id);
-- Supports gss/scheduler.py's get_active_patrol_schedules() read every tick.
create index patrol_schedules_active_next_run_idx
  on patrol_schedules (next_run_at) where active;

alter table patrol_schedules enable row level security;

create policy patrol_schedules_authenticated_select on patrol_schedules
  for select to authenticated using (true);
create policy patrol_schedules_authenticated_insert on patrol_schedules
  for insert to authenticated with check (true);
create policy patrol_schedules_authenticated_update on patrol_schedules
  for update to authenticated using (true) with check (true);
create policy patrol_schedules_authenticated_delete on patrol_schedules
  for delete to authenticated using (true);
