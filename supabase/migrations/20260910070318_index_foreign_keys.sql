-- Cover the remaining foreign keys with indexes (Supabase performance
-- advisory 0001: unindexed_foreign_keys). Cheap now, avoids sequential
-- scans on joins and on cascading deletes once these tables have rows.
-- missions(drone_id) and mission_events(mission_id) are already covered by
-- indexes created in the initial migration.
create index if not exists alerts_mission_id_idx      on alerts (mission_id);
create index if not exists commands_drone_id_idx       on commands (drone_id);
create index if not exists commands_mission_id_idx     on commands (mission_id);
create index if not exists drones_home_dock_id_idx     on drones (home_dock_id);
create index if not exists missions_dock_id_idx        on missions (dock_id);
create index if not exists recordings_drone_id_idx     on recordings (drone_id);
create index if not exists recordings_mission_id_idx   on recordings (mission_id);
