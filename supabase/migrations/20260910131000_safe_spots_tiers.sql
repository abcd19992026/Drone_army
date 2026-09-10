-- GSS v0.5.1 -- safe_spots: the marker / GPS-only tier distinction
--
-- A spot WITH an ArUco marker can be landed on accurately by the vision
-- pipeline (v0.7); a small pad is fine. A spot WITHOUT a marker is GPS-only,
-- with 3-10 m of real-world error, so the selection function ignores a small
-- radius_m claim and refuses a raised surface. height_above_dock_m matters
-- because the aircraft measures altitude relative to home -- landing on a
-- terrace three floors up is a different manoeuvre from landing in a field.

alter table safe_spots
  add column if not exists height_above_dock_m  double precision not null default 0.0,
  add column if not exists has_marker           boolean not null default false,
  add column if not exists marker_id            text,
  add column if not exists hazards              text,
  add column if not exists approach_bearing_deg double precision,
  add column if not exists permission_notes     text;

-- The dock has the landing marker and is at dock level by definition.
update safe_spots set has_marker = true, height_above_dock_m = 0.0
  where name = 'dock';
