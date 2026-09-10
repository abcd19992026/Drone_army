-- Pin set_updated_at()'s search_path (Supabase security advisory 0011:
-- function_search_path_mutable). Redundant with the hardened definition now
-- in 20260910063937_initial_schema.sql -- kept as its own migration so
-- databases created before that edit still get the fix. Idempotent.
create or replace function set_updated_at() returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;
