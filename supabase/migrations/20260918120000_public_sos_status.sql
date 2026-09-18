-- Public live-status-page backend (Phase 11 follow-up).
--
-- gss/alerts.py's WhatsApp SOS message carries {{2}}, a link to
-- {STATUS_PAGE_BASE_URL}/{command_id} -- the CLAIMED COMMAND's id, chosen
-- specifically because the alert now fires before any mission exists (by
-- design: PROJECT.md Section 13, "none of that may ever stop this alert").
-- That page has NO login (it is handed to trusted contacts over WhatsApp,
-- not to an authenticated operator), so it can only ever use the anon /
-- publishable key -- the same key that is safe to ship in browser JS.
--
-- WHY A FUNCTION, NOT A PLAIN "create policy ... for select to anon" ROW:
--   1. Column narrowing. RLS is row-level, not column-level -- a table-level
--      anon SELECT policy on `commands` would still hand back target_lat,
--      target_lon, issued_by, params, drone_id, everything, to anyone who
--      can read one row. This page must expose only status/created_at/plain
--      status/reason/mission linkage/recent locations -- nothing else.
--   2. Anti-enumeration. A table-level policy (even "type = 'sos' and
--      created_at > now() - interval '48 hours'") is filtered by whatever
--      WHERE clause the CALLER chooses to send -- nothing stops a caller
--      from omitting `id=eq.<uuid>` and listing every live incident. A
--      function with a required `p_command_id uuid` parameter cannot be
--      called without one, and cannot return more than the one row it names.
--   3. `contacts` must never be reachable from this surface at all -- a
--      function is easy to audit for "does it touch that table" (it does
--      not); a table-level policy leaves that as a standing risk on every
--      future column added to `commands`/`missions`.
--
-- So: no new RLS policy on `commands`, `missions`, or `locations` for `anon`
-- -- they keep denying anon entirely, exactly as today. The ONLY new anon
-- privilege this migration grants is EXECUTE on the one function below,
-- which is `security definer` (deliberately unlike this project's other RPC,
-- claim_command, which is `security invoker` and service_role-only) so it
-- can read those three tables on the caller's behalf, narrowly, without
-- granting the caller any of its own read access to them.
--
-- TIME-BOXING: an incident older than 48 hours collapses to plain_status =
-- 'expired' with reason/mission_id nulled and recent_locations emptied --
-- a link from months ago must not keep serving live location data forever,
-- per PROJECT.md Section 13's "Call Police" page being a live-incident tool,
-- not a permanent public record. Tune the interval below if 48h is wrong;
-- there is deliberately no config.py knob for it (this is a public-facing
-- privacy boundary enforced in the database, not a GSS runtime setting --
-- gss/config.py's own R1 rule is "safety decides with no network", which
-- does not apply here, but the same instinct -- do not let this depend on
-- application code remembering to check -- does).
--
-- LOCATIONS CAVEAT: `locations` has no foreign key to `commands`/`missions`
-- (just a free-text device_id and a timestamp) -- there is currently no way
-- to scope a location row to one specific incident. This is consistent with
-- the rest of the project's single-operator design (one phone, one person,
-- see gss/config.py's SOS_PERSON_NAME): "the most recent location(s)" is
-- read as "this operator's most recent location(s)", not "this incident's".
-- A multi-person deployment would need a real link (e.g. a device_id column
-- on commands) before this function's location clause is still correct.

create or replace function public.get_sos_incident_status(p_command_id uuid)
returns table (
  command_id         uuid,
  command_status     text,
  command_created_at timestamptz,
  plain_status       text,
  reason             text,
  mission_id         uuid,
  recent_locations   jsonb
)
language sql
security definer
set search_path = ''
stable
as $$
  select
    c.id,
    c.status,
    c.created_at,
    case
      when now() - c.created_at > interval '48 hours' then 'expired'
      when m.id is null then 'not_dispatched'
      when m.status in ('queued', 'launching') then 'dispatched'
      when m.status in ('enroute', 'on_station') then 'enroute'
      when m.status in ('returning', 'landed') then 'returned'
      when m.status = 'aborted' then 'not_dispatched'
      else 'not_dispatched'
    end as plain_status,
    case
      when now() - c.created_at > interval '48 hours' then null
      else coalesce(c.rejected_reason, m.abort_reason)
    end as reason,
    case
      when now() - c.created_at > interval '48 hours' then null
      else m.id
    end as mission_id,
    case
      when now() - c.created_at > interval '48 hours' then '[]'::jsonb
      else (
        select coalesce(jsonb_agg(loc), '[]'::jsonb)
        from (
          select l.lat, l.lon, l.accuracy_m, l.recorded_at
          from public.locations l
          order by l.recorded_at desc
          limit 5
        ) loc
      )
    end as recent_locations
  from public.commands c
  left join public.missions m on m.id = c.mission_id
  where c.id = p_command_id
    and c.type = 'sos';
$$;

comment on function public.get_sos_incident_status(uuid) is
  'Public, unauthenticated (anon) read for the live SOS status page. Narrow '
  'by construction: takes one command id, returns one row of pre-selected '
  'columns, never contacts/other commands/full location history. See the '
  'migration file for the full design rationale.';

-- Anyone holding the anon/publishable key may call this ONE function, with
-- ONE required argument -- nothing else changes. `commands`, `missions`,
-- `locations` and `contacts` remain exactly as unreachable to anon as they
-- were before this migration.
revoke execute on function public.get_sos_incident_status(uuid) from public;
grant execute on function public.get_sos_incident_status(uuid) to anon;
