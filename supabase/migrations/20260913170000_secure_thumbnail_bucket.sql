-- Phase 8.1 -- secure the recording-thumbnails bucket
--
-- Gap found: gss/media.py + gss/store.py have referenced the
-- "recording-thumbnails" Storage bucket since Phase 8, but nothing ever
-- created it in Postgres -- storage.buckets was empty on the live project.
-- This migration creates it correctly the first time: private, with a
-- server-side size/type cap that backs up the R6 convention ("only a JPEG
-- thumbnail, never video") at the infrastructure level, not just by
-- convention in media.py's docstring.
--
-- Access model matches every table in this project (see the RLS block at
-- the bottom of 20260910063937_initial_schema.sql):
--   * the GSS writes with the service_role key, which bypasses storage RLS
--     exactly like it bypasses table RLS -- so no insert/update/delete
--     policy is added for this bucket.
--   * a logged-in browser session (`authenticated` role) may read objects
--     in this bucket only -- the same "read-everything" shape used for
--     every table, scoped here to one bucket instead of one table.
--   * nobody unauthenticated can read or write; the bucket is NOT public.

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('recording-thumbnails', 'recording-thumbnails', false, 1048576, array['image/jpeg'])
on conflict (id) do update
  set public = false,
      file_size_limit = 1048576,
      allowed_mime_types = array['image/jpeg'];

create policy recording_thumbnails_authenticated_read
  on storage.objects
  for select
  to authenticated
  using (bucket_id = 'recording-thumbnails');
