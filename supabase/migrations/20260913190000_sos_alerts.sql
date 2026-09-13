-- Phase 11 -- WhatsApp SOS alerts: the "sos" command type.
--
-- "sos" flies exactly like "summon" (fly to the phone's current location,
-- standoff offset and all -- see gss/commands.py's _mission_type_for and the
-- is_summon wiring in gss/safety.py / gss/mission.py) but is recorded as a
-- 'family_summon' mission, matching the existing missions.type CHECK (no
-- mission-side migration needed).
--
-- The WhatsApp alert this command also triggers (gss/alerts.py) is a
-- completely independent action -- it writes to the `alerts` table, which
-- already has every column it needs (contacts_notified, channel,
-- template_name, status, error_detail) from the initial schema.

alter table commands drop constraint commands_type_check;
alter table commands add constraint commands_type_check
  check (type in (
    'summon', 'goto', 'perch', 'return', 'land', 'abort', 'hold',
    'takeoff', 'set_altitude', 'set_heading', 'nudge', 'orbit',
    'set_roi', 'photo', 'start_recording', 'stop_recording', 'panorama',
    'follow_me', 'spotlight_on', 'spotlight_off', 'siren_on', 'siren_off',
    'speaker_talk', 'drop_release', 'find_my_drone',
    'weather_continue', 'weather_recall',
    'sos'));
