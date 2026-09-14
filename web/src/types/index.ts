export interface DroneRow {
  id: string;
  name: string;
  status: 'docked' | 'charging' | 'flying' | 'perched' | 'returning' | 'error';
  control_mode: 'auto' | 'manual';
  battery_pct: number | null;
  battery_voltage_v: number | null;
  battery_current_a: number | null;
  mode: string | null;
  armed: boolean | null;
  lat: number | null;
  lon: number | null;
  alt_m_relative: number | null;
  alt_m_amsl: number | null;
  heading_deg: number | null;
  groundspeed_ms: number | null;
  gps_fix_type: number | null;
  gps_satellites: number | null;
  position_valid: boolean;
  home_dock_id: string | null;
  last_heartbeat_at: string | null;
  last_telemetry_at: string | null;
  link_up: boolean;
  firmware_version: string | null;
  // Payload fields (read-only)
  spotlight_on: boolean;
  siren_on: boolean;
  beacon_active: boolean;
  speaker_active: boolean;
  drop_loaded: boolean;
  drop_released: boolean;
  recording_active: boolean;
  camera_mode: string | null;
  lidar_distance_m: number | null;
  optical_flow_quality: number | null;
  // Last trustworthy fix
  last_known_lat: number | null;
  last_known_lon: number | null;
  last_known_alt_m: number | null;
  last_known_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface DockRow {
  id: string;
  name: string;
  lat: number | null;
  lon: number | null;
  address_label: string | null;
  status: 'ok' | 'degraded' | 'offline';
  has_drone: boolean;
  power_source: string | null;
  charging_active: boolean;
  last_health_at: string | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

export type MissionType =
  | 'summon'
  | 'patrol'
  | 'inspect'
  | 'test'
  | 'flood_survey'
  | 'search'
  | 'accident'
  | 'family_summon'
  | 'manual'
  | 'photo'
  | 'video'
  | 'orbit'
  | 'roi'
  | 'panorama'
  | 'timelapse'
  | 'follow'
  | 'survey_grid';

export type MissionStatus =
  | 'queued'
  | 'awaiting_confirmation'
  | 'launching'
  | 'enroute'
  | 'on_station'
  | 'returning'
  | 'landed'
  | 'aborted';

export interface MissionRow {
  id: string;
  drone_id: string;
  dock_id: string | null;
  type: MissionType;
  status: MissionStatus;
  target_lat: number | null;
  target_lon: number | null;
  cruise_alt_m: number | null;
  loiter_seconds: number | null;
  started_at: string | null;
  ended_at: string | null;
  abort_reason: string | null;
  distance_m: number | null;
  battery_used_pct: number | null;
  triggered_by: string | null;
  created_at: string;
}

export interface MissionEventRow {
  id: string;
  mission_id: string | null;
  at: string;
  event: string;
  detail: Record<string, any> | null;
  lat: number | null;
  lon: number | null;
  alt_m: number | null;
  battery_pct: number | null;
  battery_voltage_v: number | null;
  link_up: boolean | null;
  created_at: string;
}

export type CommandType =
  | 'summon'
  | 'hold'
  | 'abort'
  | 'weather_continue'
  | 'weather_recall'
  | 'sos'
  | 'find_my_drone'
  | string;

export type CommandStatus =
  | 'pending'
  | 'accepted'
  | 'executing'
  | 'done'
  | 'rejected'
  | 'expired';

export interface CommandRow {
  id: string;
  issued_at: string;
  issued_by: string | null;
  drone_id: string;
  type: CommandType;
  target_lat: number | null;
  target_lon: number | null;
  target_alt_m: number | null;
  params: Record<string, any> | null;
  status: CommandStatus;
  mission_id: string | null;
  rejected_reason: string | null;
  acked_at: string | null;
  completed_at: string | null;
  expires_at: string | null;
  created_at: string;
}

export interface AlertRow {
  id: string;
  mission_id: string | null;
  triggered_at: string;
  contacts_notified: any | null;
  channel: 'whatsapp' | 'sms' | 'voice';
  status: 'queued' | 'sending' | 'sent' | 'failed' | 'partial';
  template_name: string | null;
  error_detail: string | null;
  detail: Record<string, any> | null;
  created_at: string;
}

export interface ContactRow {
  id: string;
  name: string;
  phone: string | null;
  relation: string | null;
  priority: number;
  active: boolean;
  created_at: string;
}

export type ScheduleCadence = 'daily' | 'weekly';

export interface PatrolScheduleRow {
  id: string;
  name: string;
  target_lat: number;
  target_lon: number;
  cruise_alt_m: number | null;
  loiter_seconds: number;
  cadence: ScheduleCadence;
  time_of_day: string; // HH:MM or HH:MM:SS
  days_of_week: number[] | null; // 0=Sunday .. 6=Saturday (weekly only)
  active: boolean;
  last_run_at: string | null;
  last_skipped_reason: string | null;
  next_run_at: string;
  dock_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface PatrolScheduleFormData {
  name: string;
  target_lat: number | '';
  target_lon: number | '';
  cruise_alt_m: number | '' | null;
  loiter_seconds: number | '';
  cadence: ScheduleCadence;
  time_of_day: string; // HH:MM
  days_of_week: number[]; // 0..6
  active: boolean;
}

export type ActiveScreen = 'status' | 'map' | 'commands' | 'missions' | 'schedules' | 'alerts';

