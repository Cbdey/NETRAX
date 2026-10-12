# Hardware phase: design note (not built yet, on purpose)

The team decided to add hardware later. This note records the agreed design, so the software is
ready to plug it in. **No hardware code is in the repo right now.** Everything below is the plan.

## What the team wants (meeting, 23 Sep)
1. **Fire / smoke:** an IR flame sensor + MQ-2 smoke sensor per zone. The siren sounds only when **camera AND sensor** agree.
2. **PPE:** RFID tags on helmet, vest, gloves and shoes, plus a worker badge. Camera and RFID are checked together.
3. **Output:** laptop speaker calls the worker ID (done); laptop siren for fire (done). An ESP32 relay siren comes later.

## Where it plugs into the current code
| Hook | File | What changes |
|---|---|---|
| Fire / smoke decision | `backend/main.py` → `respond()` | Instead of `start_alarm()` directly, ask a `Fusion` object whether a sensor in the same zone agreed within `window_s` |
| PPE decision | `backend/main.py` → `respond()` / `ppe_text()` | Look up the last RFID gate scan in the zone and add a per-item verdict (`not_carried` / `carried_not_worn`) |
| New API | `backend/main.py` | `POST /api/sensors/reading`, `POST /api/rfid/scan`, `GET /api/devices/<id>/commands` (the ESP32 polls this) |
| Config | new `config/hardware.yaml` | devices per zone, fusion windows, tag and badge register |
| Siren relay | `backend/actuators.py` → `Alarm.start/stop` | Also queue `siren_on` / `siren_off` for ESP32 nodes |
| Worker ID | `edge/identity.py` | Face ID stays; RFID badge becomes a second source (the judging doc prefers badge / RFID) |
| Alert JSON | frozen contract | **Add** fields only: `verified_by`, `fusion`, `ppe_verdict` |

## Fusion rules agreed earlier (recommendation)
| Situation | Result |
|---|---|
| Camera fire + sensor in the same zone within 30 s | Confirmed: siren |
| Camera fire, sensor online but quiet (e.g. phone screen showing fire) | "Unverified", no siren |
| Sensor alone keeps triggering for 20 s (fire behind a machine) | Siren anyway (safety fallback; can be set to 0) |
| No sensor heard for 10 s (broken / Wi-Fi down) | Camera decides alone, as now (a dead sensor must never blind the system) |
| PPE: camera says missing + tag absent | Confirmed by both |
| PPE: camera says missing + tag present | "Carried but not worn": speaker asks to wear it properly |

## Parts per zone (to buy)
ESP32 DevKit · MQ-2 smoke sensor (analog, needs warm-up + clean-air calibration) · IR flame sensor (KY-026 or 5-channel) ·
RC522 RFID reader + 13.56 MHz tags / cards (about 3 cm range: tap badge, then each PPE tag) · active buzzer or relay + siren.
Production option: a UHF RFID gate that reads all tags at 1–5 m without tapping.

## Order of work when the hardware arrives
1. Flash one ESP32 that only posts MQ-2 + flame readings every second; watch them in the backend log.
2. Add `/api/sensors/reading` + the Fusion rules for fire (with the 4 fallbacks above) + selftests.
3. Add the siren relay via `/api/devices/<id>/commands`.
4. Add RFID: read the tag UIDs, write them into `config/hardware.yaml`, add `/api/rfid/scan` and the PPE verdicts.
5. Re-run the failure gallery: the phone-screen-fire case should turn from "known limitation" into "handled".
