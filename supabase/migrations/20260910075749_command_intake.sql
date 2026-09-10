-- GSS v0.3 -- command intake support
--
-- * claim_command()   -- atomic claim so two GSS instances (or the Realtime
--                         and poll paths racing) never execute the same
--                         command. acked_at is stamped with the DATABASE's
--                         now(), never the Pi's clock.
-- * lifecycle triggers -- completed_at / started_at / ended_at stamped
--                         server-side so timestamps are correct even on a Pi
--                         whose clock has not been NTP-corrected after a
--                         power cut.
-- * 'mission_created' added to the mission_events vocabulary.
-- * commands.replica identity full -- so the status page sees status changes.

-- ---------------------------------------------------------------------------
-- claim_command: pending + ours -> accepted, atomically. Returns the row and
-- the server clock, or SQL NULL when nothing matched.
-- ---------------------------------------------------------------------------
create or replace function claim_command(p_command_id uuid, p_drone_id uuid)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $$
declare
  r public.commands;
begin
  update public.commands
     set status = 'accepted', acked_at = now()
   where id = p_command_id
     and status = 'pending'
     and drone_id = p_drone_id
  returning * into r;

  if not found then
    return null;   -- already claimed, not ours, or gone -- a normal outcome
  end if;

  return jsonb_build_object('command', to_jsonb(r), 'server_now', now());
end;
$$;

-- Only the GSS (service_role) may claim commands.
revoke execute on function claim_command(uuid, uuid) from public;
revoke execute on function claim_command(uuid, uuid) from anon;
revoke execute on function claim_command(uuid, uuid) from authenticated;
grant execute on function claim_command(uuid, uuid) to service_role;

-- ---------------------------------------------------------------------------
-- server-side timestamp stamping
-- ---------------------------------------------------------------------------
create or replace function stamp_command_terminal() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  if new.status in ('done', 'rejected', 'expired')
     and old.status is distinct from new.status
     and new.completed_at is null then
    new.completed_at = now();
  end if;
  return new;
end;
$$;
create trigger commands_stamp_terminal before update on commands
  for each row execute function stamp_command_terminal();

create or replace function stamp_mission_lifecycle() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  if old.status = 'queued' and new.status <> 'queued' and new.started_at is null then
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
create trigger missions_stamp_lifecycle before update on missions
  for each row execute function stamp_mission_lifecycle();

-- ---------------------------------------------------------------------------
-- mission_events vocabulary: add 'mission_created'
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
    'mission_created'));

-- ---------------------------------------------------------------------------
alter table commands replica identity full;
