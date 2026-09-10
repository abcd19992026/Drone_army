-- GSS v0.2 -- seed one dock and one drone (v1 is a single-dock, single-drone
-- system).
--
-- The ids below match the DEFAULTS in gss/config.py (DOCK_ID / DRONE_ID) so
-- the GSS talks to these rows out of the box. Override both in .env if you
-- seed different rows.
--
-- SQL migrations cannot read environment variables, so the coordinates here
-- are written by hand -- keep them in sync with gss/config.py HOME_LAT /
-- HOME_LON (see PROJECT.md section 3 for the dock location).

insert into docks (id, name, lat, lon, address_label, status, has_drone, power_source, notes)
values (
  'd0c00000-0000-4000-8000-000000000001',
  'Agamkuan Dock',
  25.5932, 85.2045,
  'Agamkuan, Patna',
  'ok',
  true,
  'mains',
  'v1 single fixed dock -- rooftop chhajja, ArUco landing pad'
)
on conflict (id) do nothing;

insert into drones (id, name, status, control_mode, home_dock_id)
values (
  'd5030000-0000-4000-8000-000000000001',
  'Sentinel-1',
  'docked',
  'auto',
  'd0c00000-0000-4000-8000-000000000001'
)
on conflict (id) do nothing;
