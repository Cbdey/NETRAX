# NETRAX: architecture

An edge-first safety monitor. Every camera frame is analysed on a local PC. Only small alert
records and thumbnails go to the dashboard, and raw video never leaves the site.

```
 cameras (webcam / RTSP / phone / video files)
     │  one reader thread per camera, newest frame only (no backlog)       edge/stream.py
     ▼
 ┌──────────────────────────── EDGE (this laptop, GTX 1650) ────────────────────────────┐
 │ detection   COCO person + PPE model (YOLOv8s) + fire/smoke model (YOLOv8n) edge/detector │
 │ tracking    ByteTrack person IDs                                           edge/tracker  │
 │ identity    face match vs registered photos -> W-001 / visitor V-001      edge/identity │
 │ PPE logic   item-to-person + size / hi-vis checks -> worn | missing | can't tell         │
 │             (association.py, validate.py); zone rules (zones.yaml)                    │
 │ alert logic N-of-M frames + grace + cooldown + re-arm + resolve  edge/state_machine.py  │
 │ fire logic  colour, white-hot core, flicker, growth, skin/hair/glare, on-person rules  │
 │                                                                   edge/fire_verify.py  │
 │ severity    Critical / High / Medium / Low by event + zone risk      edge/severity.py   │
 │ load        slows low-risk zones first when the GPU is full          edge/load.py       │
 │ privacy     faces pixelated in stored snapshots; 30-day retention                      │
 │ outbox      SQLite queue, alerts survive network loss                edge/outbox.py     │
 └───────────────┬───────────────────────────────────────────────────────────────────────┘
                 │ alert JSON (~42 KB incl. snapshot) + 1/s health; LAN preview frames
                 ▼
 ┌──────────────────────────── BACKEND (FastAPI + SQLite) ──────────────────────────────┐
 │ events, ack/escalate/dismiss, auto-escalation, reports (CSV/PDF), analytics, shift     │
 │ summary, worker registration, WebSocket push                           backend/main.py │
 │ autonomous response: Windows voice calls the worker ID; siren for fire / smoke          │
 │                                                                   backend/actuators.py │
 │ notifications: Telegram if configured, otherwise shown as MOCK        backend/notify.py │
 └───────────────┬───────────────────────────────────────────────────────────────────────┘
                 ▼
   supervisor dashboard (dashboard/index.html): live tiles, alert feed, zones, workers,
   heatmap, shift summary, reports, ROI calculator (dashboard/roi.html)
```

## Why edge-first (measured where marked)
- **Latency:** inference about 42 ms per frame (measured). Edge → dashboard 98–175 ms (measured live). Violation → alert median 2.8 s (measured this shift), most of which is the deliberate confirmation wait against false alarms.
- **Bandwidth:** about 42 KB per alert (measured) versus 2–6 Mbps per camera for cloud video (assumption), roughly 53 MB/day versus 690 GB/day for 16 cameras. See `docs/cost_roi.md`.
- **Resilience:** the outbox keeps alerts when the network or backend is down, and syncs them later (F17).
- **Privacy:** no video leaves the site. Faces are pixelated in stored snapshots. Face ID is only for workers who registered with consent; visitors are never stored.

## Decisions that avoid false alarms
| Problem | Mechanism |
|---|---|
| Alert spam | 7 of 10 frames + 1.5 s grace, one alert per worker per violation, cooldown against tracker ID switches, 5 s re-arm for real repeats |
| Person half-visible | "can't tell" (unverifiable) state, never an alert (e.g. feet out of frame means no shoe alarm) |
| Shirt ≠ vest, cap ≠ helmet | size check against the person box + hi-vis colour check |
| Orange clothing ≠ fire | fire on a person must glow and flicker |
| Face / hair / flash ≠ smoke | skin, dark and glare shares (all 25 live false alarms rejected) |
| Flame core ≠ extra smoke | smoke inside a fire box counts as part of that fire |
| Many cameras, one GPU | load manager: medium-risk zones slowed first (measured, 4 streams) |

## Alert contract (plan 7.6, fields only added, never renamed)
`event_id, type, items_missing, severity, camera_id, zone_id, track_id, confidence, timestamp, snapshot, bbox`
plus `action, started_at, onset_at, captured_at, worker_id, worker_name, camera_name, zone_name, growth, schema_version`.

## Configuration
`config/settings.yaml` (thresholds, identity, privacy, response, load) · `config/zones.yaml` (required PPE + risk) · `config/cameras.yaml` (sources; `enabled: false` hides a camera).
Hardware (sensors, RFID, ESP32) is planned; see `docs/hardware_plan.md`.
