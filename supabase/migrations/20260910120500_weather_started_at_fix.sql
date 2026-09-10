-- GSS v0.5 -- follow-up: started_at should mark when a mission actually began
-- flying, not when a mission held for weather confirmation is cancelled before
-- it ever launched.

create or replace function stamp_mission_lifecycle() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  if old.status in ('queued', 'awaiting_confirmation')
     and new.status in ('launching', 'enroute', 'on_station', 'returning', 'landed')
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
