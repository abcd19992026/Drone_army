# PROJECT.md — Personal Security Drone System

**Status:** Planning complete. Software build not started.
**Owner:** Alok, Agamkuan, Patna
**Purpose of this file:** Full technical context for anyone (human or AI tool) working on this project. Paste this into Claude Code / Antigravity / a new chat before asking for any work.

---

## 1. Kya bana rahe hain

Ek udne wala camera jo hamesha taiyaar khada rehta hai. Phone se button dabao, wo apne aap udkar tumhare paas aa jaata hai, upar mandrata hai, sab record karta hai, aur khud dock par laut kar charge ho jaata hai.

**Yeh personal use ke liye hai, business nahi.** Do kaam karta hai:
1. Bahar hone par ek gawah jo hamesha maujood ho
2. Gaon ki zameen ka rozana/hafte-war aerial record, kyunki wahan aksar jaana nahi hota

**Yeh drone kisi ko rok nahi sakta.** Wo 25-30 meter upar rehta hai kyunki uske propeller khatarnak hain. Wo **gawah** hai, **guard** nahi. Uski taakat — dikhna, awaaz karna, record karna.

---

## 2. Tod nahi sakte — hard rules

Yeh niyam design ke faisle hain, preference nahi. Inhe badalne se pehle poora system dobara sochna padega.

| # | Rule | Wajah |
|---|---|---|
| R1 | `safety.py` kabhi internet ya Supabase par nirbhar nahi karega | Broadband gaya aur drone ud raha hai — failsafe us waqt kaam karna chahiye jab uski sabse zyada zarurat ho |
| R2 | Drone kabhi seedhe insaan ke sar ke upar nahi | Gira to 900 joule. Hamesha 10-15 m lateral offset |
| R3 | Hexacopter, quad nahi | Quad mein ek motor gaya = turant crash. Hex mein paanch sambhal lete hain |
| R4 | Parachute ka trigger aur battery flight controller se alag | Jab girega tab FC shayad mara hua hoga |
| R5 | Link tootne se feature jaate hain, drone nahi | ArduPilot waypoints onboard chalata hai. Link sirf commands aur video ke liye |
| R6 | Video kabhi Supabase Storage par nahi | 1 udaan = 1-1.5 GB. Bill phaad dega, faayda zero |
| R7 | Landing hamesha ArUco marker se, sirf GPS se nahi | Tang galiyon mein GPS 10-30 m tak galat hota hai |
| R8 | Gemini kabhi `gss/` ya `supabase/` nahi chhuega | Yahan galtiyan chup-chaap hoti hain aur drone girta hai |

---

## 3. Physical architecture

```
[ Drone ]  ---- 915 MHz radio (control + telemetry) ---->  [ Dock ka Pi ]
   |                                                             |
   |  (landing ke baad wifi par recording transfer)              |
   +------------------------------------------------------------+
                                                                 |
                                                          ghar ka wifi
                                                                 |
                                                          [ Supabase ]
                                                                 |
                                                     [ Phone PWA / Laptop ]
```

### Drone
- Hexacopter, ~2.5-3 kg
- Li-ion 21700 pack (LiPo nahi — energy density zyada)
- Bade (15-18"), dheere ghoomne wale propeller — endurance + shor + wind stability, teeno ek saath
- Prop guards
- Ballistic parachute, alag battery aur trigger
- Raspberry Pi + camera (ArUco landing + recording)
- ELRS 915 MHz receiver
- ~45 min hover endurance, ~18 ghante perched

### Dock — ghar ki chhat, Agamkuan
- **Koi dhakkan nahi.** Fixed chhajja, uske neeche landing pad. Jo hilta hai wahi tootta hai.
- Landing pad par ArUco marker
- Gold-plated pogo pins (ghar par hai, mahine mein ek baar saaf kar dena)
- Raspberry Pi — **yahi GSS chalayega**
- Ghar ke wifi se juda
- Mains bijli + inverter. Solar v1 mein nahi.
- Antenna chhat par jitna ooncha ho sake (paani ki tanki ke upar 3-4 m pole)

### Sirf ek dock — v1 mein
Naapa hua: sabhi jagah 6 km ke andar hain.

| Jagah | Ghar se doori |
|---|---|
| Gulzarbagh Ghumti (ghar 2) | 0.5 km |
| Pashchim Darwaza (ghar 3) | 1.1 km |
| Kumhrar | 1.9 km |
| Dost — Patna City Chowk | 2.4 km |
| Rajendra Nagar | 4.2 km |
| Kankarbagh | 4.4 km |
| Bhikhana Pahari | 4.7 km |
| Patna Junction | 6.8 km — **daayre se bahar** |

---

## 4. Software architecture

Do hisse. Yeh SmartDentist se sabse bada farak hai.

### GSS — Ground Station Service (naya hissa)
Python, dock ke Raspberry Pi par, hamesha chalta hua.

Serverless mein yeh nahi ho sakta kyunki:
- MAVLink ko lagatar juda hua connection chahiye
- Safety ka faisla internet par nahi chhoda ja sakta (R1)

Alag PC ki zarurat nahi — dock ke Pi ko waise bhi hamesha on rehna hai (charging control). Wo ~6 watt khaata hai; desktop ~80 watt khata.

### Supabase + React (jaana-pehchana hissa)
SmartDentist wala hi stack: Supabase (Postgres, Auth, RLS, Realtime) + React + Vite + TypeScript + Tailwind + shadcn/ui.

---

## 5. Database schema

### `drones`
`id`, `name`, `status` (docked / charging / flying / perched / error), `battery_pct`, `mode`, `lat`, `lon`, `alt_m`, `heading`, `home_dock_id`, `last_heartbeat_at`, `firmware_version`, `created_at`

### `docks`
`id`, `name`, `lat`, `lon`, `address_label`, `status` (ok / degraded / offline), `has_drone` (bool), `power_source`, `last_health_at`, `notes`

### `missions`
`id`, `drone_id`, `dock_id`, `type` (summon / patrol / inspect / test), `status` (queued / launching / enroute / on_station / returning / landed / aborted), `target_lat`, `target_lon`, `cruise_alt_m`, `loiter_seconds`, `started_at`, `ended_at`, `abort_reason`, `distance_m`, `battery_used_pct`

### `mission_events`
`id`, `mission_id`, `at`, `event` (armed / takeoff / enroute / arrived / loiter_start / battery_warning / point_of_no_return / rtl / link_lost / landed / aborted), `detail` (jsonb), `lat`, `lon`, `alt_m`

Har udaan ki poori kahani. Debugging ke liye sabse kaam ki table.

### `commands`
`id`, `issued_at`, `issued_by`, `type` (summon / goto / perch / return / land / abort), `target_lat`, `target_lon`, `params` (jsonb), `status` (pending / accepted / executing / done / rejected), `mission_id`, `rejected_reason`, `acked_at`

**Yeh poore system ka jod hai.** Phone ek row daalta hai → GSS Realtime se turant sunta hai → mission chalata hai → status wapas likhta hai.

### `recordings`
`id`, `mission_id`, `local_path`, `started_at`, `duration_s`, `size_mb`, `thumbnail_url`, `flagged` (bool), `delete_after` (date), `lat`, `lon`, `sha256`

Video **local disk par**. Supabase mein sirf yeh row + thumbnail.

### `locations`
`id`, `device_id`, `lat`, `lon`, `accuracy_m`, `recorded_at`, `phone_battery_pct`

### `system_config`
`key`, `value` (jsonb), `updated_at`

Runtime settings — redeploy kiye bina badalne ke liye.

**RLS har table par pehle din se.** Abhi sirf ek user hai, par aadat sahi rakhni hai.

---

## 6. GSS ke module

| File | Scope |
|---|---|
| `config.py` | Saari settings ek jagah. Connection string yahan — asli drone par sirf yahi badlega. |
| `link.py` | MAVLink connection, heartbeat, toot jaye to dobara jodna |
| `telemetry.py` | Position/battery/mode padhkar har 2 sec Supabase mein |
| `commands.py` | `commands` table sunna, validate karna, accept/reject |
| `mission.py` | Udaan chalana — arm, takeoff, goto, loiter, RTL, land |
| `safety.py` | Battery, geofence, altitude, link-loss, point of no return |
| `dock.py` | Charging, drone maujood hai ya nahi, roz ki health report |
| `media.py` | Landing ke baad recording uthana, thumbnail, hash, 30-din safai |

`safety.py` ke paas **veto** hai. Wo kisi bhi command ko rok sakta hai, aur uska faisla `mission.py` ya `commands.py` overrule nahi kar sakte.

---

## 7. Command flow

```
1. Phone PWA: location padha, button daba
2. commands mein row: type=summon, target_lat/lon, status=pending
3. GSS (Realtime) ne suna
4. safety.py check: daayre ke andar? battery kaafi? drone ready?
   -> nahi to status=rejected + rejected_reason. Yahin ruk jao.
5. status=accepted, missions mein row banao
6. mission.py: GUIDED -> arm -> takeoff 45m
7. Target ki taraf, har mod mission_events mein
8. Pahunchkar 25-30m par utro, 10-15m lateral offset (R2)
9. Loiter. safety.py har second point of no return check karta hai
10. Battery limit ya loiter khatam -> RTL -> ArUco landing
11. media.py: recording uthao, thumbnail, index, delete_after set
12. dock.py: charging shuru, drone status=charging
```

---

## 8. Safety ladder

### Battery
- **40%** — phone par warning
- **Point of no return** — har second hisaab. Ghar lautne mein jitna lagega + 20% reserve. Yahan drone command nahi maanega, wo nikal jayega.
- **20%** — hard floor. Ghar pahunch nahi sakta to paas ke jaane-pehchane safe spot par utro. Koshish karke raaste mein girne se behtar hai utar jaana.

### Geofence
- Dock se max **6 km**
- Max altitude **60 m**
- Cruise **45 m** (aane wala) / **55 m** (jaane wala) — takraane se bachne ke liye
- On-station **25-30 m**

### Link loss
```
link gaya
  -> current waypoint poora karo
  -> 30 sec tak nahi aaya
  -> surakshit unchai par chadho
  -> nazdeeki dock par lauto
  -> wo bhi na ho -> nazdeeki known safe spot par utro
```

### Fixed constants
```
HOME_LAT = 25.5932
HOME_LON = 85.2045
CRUISE_ALT_INBOUND  = 45
CRUISE_ALT_OUTBOUND = 55
ON_STATION_ALT      = 28
LATERAL_OFFSET_M    = 12
MAX_RADIUS_M        = 6000
MAX_ALT_M           = 60
BATTERY_WARN_PCT    = 40
BATTERY_FLOOR_PCT   = 20
RECORDING_KEEP_DAYS = 30
```

---

## 9. Build order

**v0.1 se v0.8 tak sab kuch simulator (ArduPilot SITL) par ban jayega. Ek bhi part kharide bina.**

| Version | Kya banega | Kahan test |
|---|---|---|
| v0.1 | Connect + telemetry console par | SITL |
| v0.2 | Telemetry Supabase mein + ek live web page | SITL |
| v0.3 | `commands` table se hukum uthakar drone tak | SITL |
| v0.4 | **`safety.py`** — battery, geofence, altitude, link loss | SITL |
| v0.5 | React UI — map, drone icon, mission history | SITL |
| v0.6 | PWA location + "mere paas aao" button | SITL |
| v0.7 | Recording pipeline — transfer, thumbnail, index, safai | SITL |
| v0.8 | Rozana ka patrol schedule | SITL |
| v1.0 | Dock control + ek se zyada drone | Asli hardware |

**`safety.py` UI se pehle hai, jaan-boojh kar.** System jitna bada hoga, safety baad mein ghusana utna hi mushkil.

Jis din asli drone udega, `config.py` mein ek line badlegi — `tcp:127.0.0.1:5762` se serial port par. Baaki poora code waisa ka waisa.

---

## 10. v1 mein jaan-boojh kar NAHI hai

| Chhoda | Wajah |
|---|---|
| Live video | Ghar ki chhat chaaron taraf se oonchi buildings se ghiri hai. Recording landing ke baad milti hai. SIM (₹250/mahina) ya Ghumti antenna node baad mein. |
| Doosra dock | Sab kuch 6 km ke andar hai. Ek dock kaafi. |
| Relay drone | Tab, jab 6 km se aage **live** video chahiye ho. v3. |
| Solar | Ghar mein bijli hai. Jab dock aisi jagah jaye jahan bijli na ho, tab. |
| Ghar ke andar udna | Andar GPS nahi milta. Alag problem, alag hal. |
| Thermal / raat ka full version | Din wala pehle. Sheher ke liye low-light camera, thermal nahi. |
| Jeb wala beacon | Phone ka PWA kaafi hai. |
| Udta hua drone station | Physics allow nahi karti — hisaab kiya ja chuka hai. |

---

## 11. AI tools ka batwara

Wahi discipline jo SmartDentist mein chalti hai.

**Claude Code:**
`gss/` ka poora code, `supabase/migrations/`, RLS policies, auth, safety logic, MAVLink

**Gemini:**
React UI — map component, forms, tables, modals, layout, CSS

**Har Gemini prompt mein yeh guard:**
> Do not modify `gss/`, `supabase/`, or anything related to MAVLink, safety, or mission logic. Do not create Postgres functions, triggers, policies, or migrations. If the task requires it, stop and report.

**Escalation:** 2 Gemini failures → seedha Claude Code.
**Order:** Claude Code (backend + types) pehle, Gemini (UI) baad mein. Kabhi ulta nahi.

**Verification:** "build passed" kaafi nahi hai. Safety ka har badlav SITL mein test hoga — battery khatam karke, geofence tod kar, link kaat kar. Sirf padh kar approve mat karna.

---

## 12. Hardware — abhi nahi kharidna

Software v0.8 tak simulator par ban jaayega. Hardware uske baad.

Jab shuru karo to sabse pehle **₹15-20k ka practice drone** — udna, crash karna, repair karna seekhne ke liye. Asli hexacopter (₹1.2-2 lakh) uske baad, jab haath saaf ho jaye.

**Aaj ka kaam:** ArduPilot SITL Mission Planner ke SIMULATION tab se chalao, home location `--home=25.5932,85.2045,50,0` set karo, aur v0.1 shuru karo.

---

## 13. Purpose v2 — safety system, not just a camera

Drone alone can't intervene (props are dangerous, must stay 25m+ up). So it's one output of a bigger **personal emergency response system**. One SOS action must trigger, in parallel:

1. WhatsApp alert to 3-4 trusted contacts, with a link to a live status page (his live location, drone feed if available, "Call Police" button, elapsed time)
2. Phone's own siren + recording upload — starts instantly, doesn't wait for the drone
3. Drone launch, if in range

**Key principle:** in the first ~90 seconds of an incident, the phone/software does the real work. The drone arrives after and extends it (deterrence, aerial view, evidence, delivery). Don't market this as a bodyguard — it's a witness + responder-summoner + logistics link.

### Non-security use cases (same hardware)
- Flood survey (annual in Bihar) — road status, water level, checking on his land
- Search for a missing person (child/elder) — aerial search of fields/riverbank is faster than ground search
- Accident scene recording — strong evidence for insurance/police
- Family summon — same as personal SOS, for a family member
- Land patrol — already planned

### New DB additions
- `contacts`: id, name, phone, relation, priority
- `alerts`: id, mission_id, triggered_at, contacts_notified (jsonb), channel (whatsapp/sms), status
- New mission `type` values: `flood_survey`, `search`, `accident`, `family_summon`

### WhatsApp alert (v1 channel)
Reuses existing Meta setup (WABA ID, phone_number_id, template flow) from SmartDentist. Template category must be UTILITY, no CTA in the template (else Meta reclassifies as Marketing, ~8x cost — same lesson as SmartDentist). Body carries the live-status-page link. SMS/voice call are future channels, not v1.

---

## 14. Deterrent + utility payload (final list)

| Item | Weight | Purpose |
|---|---|---|
| Spotlight, narrow beam 15-20°, software-controlled steady/flash modes | ~100 g | Steady = illuminate/deter. Flash = beacon mode (accident/flood location marker) |
| Siren (~100+ dB) | ~60 g | Deterrence, draws neighbourhood attention |
| Speaker, with live talk-down (someone at home laptop speaks, plays through drone) | ~80 g | Deterrence — live human voice, stronger than a recording |
| Drop mechanism (empty) | ~70 g | Family loads item at dock; drone delivers to his location |
| **Total added payload** | **~310 g** | ~10% of drone weight |

**Rejected:** onboard mic (propeller noise at 25m makes ground audio unusable — phone's mic covers this instead, since it's in his pocket, right at the scene). Separate night-only drone (spotlight is only ~100g, not worth a whole second airframe — same drone carries it always, mount is modular for camera swaps only). Any weaponized payload (spray/projectile/shock/laser) — illegal to weaponize a drone in India, and high risk of hitting the wrong person; this line will not move regardless of framing.

**Drop delivery note:** never hand-delivered directly to him (propellers). Drone descends to 2-3m over a pre-designated open spot (his terrace, a field) and releases; he collects it. Endurance cost is one-way only (return flight is at normal weight): ~44 min empty -> ~36 min at 500g -> ~30 min at 1kg payload.
