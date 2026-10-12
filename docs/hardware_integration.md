# NETRAX: hardware integration (ESP32 safety station)

## What it does
- **Fire/smoke cross-check.** When a camera detects fire or smoke in a zone that has a station, the alert waits
  (status `verifying`, up to `fusion.verify_window_s` = 8 s) for the station in that zone.
  - Station sees flame / MQ-2 / PM2.5 smoke / high temperature (now, or up to 10 s before) → **CONFIRMED**:
    siren, voice call, notification, station goes red, relay on.
  - Station online and green → **NOT alarmed** (status `suppressed`, "camera only · station clear"). It is still
    logged and shown on the feed and the hardware page, so nothing is hidden.
  - Station offline or no station in the zone → the camera alarm goes ahead (labelled "camera only"). A dead
    station is never treated as "safe".
- **Hardware-only alarm.** If the station detects fire or smoke with no camera event, a "sensor alarm" is raised.
  It clears by itself after 10 s without a hazard.
- **PPE** is still checked continuously by the cameras. The helmet/shoe FSR and touch sensors show as extra
  evidence on the hardware page. They are not used as proof that PPE is worn (`hardware_ppe.alerts: false`).
- **RFID attendance.** Each scan toggles that worker between ENTERED and EXITED for the station's zone. Scans of
  the same card within 3 s are ignored. Unknown cards show "Unregistered card" with a Link button. You can export
  the log as CSV. Optionally, every scan is also copied to Google Sheets (`attendance.sheets_url`).

## Files
| File | Purpose |
|---|---|
| `firmware/netrax_esp32/netrax_esp32.ino` | ESP32 sketch with the team pin map: 1 s telemetry, RFID, a local alarm that works without Wi-Fi |
| `config/hardware.yaml` | station → zone, thresholds, fusion window, attendance |
| `backend/hardware.py`, `backend/hw_api.py` | ingest, hazard evaluation, fusion, attendance, API |
| `dashboard/hardware.html` (`/hardware`) | live sensors, fire cross-check table, who is in the area, attendance |
| `dashboard/index.html` | "Safety station" panel, plus `verifying` / `suppressed` statuses in the feed |
| `tools/hw_sim.py` | fake ESP32 for demos and tests |
| `tools/hw_selftest.py` | 30 backend tests (`python -m tools.hw_selftest`) |

## Setup (about 10 minutes)
1. **Laptop IP:** run `ipconfig` and note the Wi-Fi IPv4 address (e.g. 192.168.1.10). Laptop and ESP32 must be on
   the same Wi-Fi network. A phone hotspot works.
2. **Firewall:** allow inbound TCP 8000. From an admin PowerShell:
   `netsh advfirewall firewall add rule name="NETRAX 8000" dir=in action=allow protocol=TCP localport=8000`
3. **Backend** must listen on all interfaces. `run_all.bat` already uses `--host 0.0.0.0`.
4. **Firmware:** in Arduino IDE, install *DHT sensor library* (Adafruit), *Adafruit Unified Sensor* and *MFRC522*.
   Set `WIFI_SSID`, `WIFI_PASS` and `SERVER = "http://<laptop-ip>:8000"`. Select board *ESP32 Dev Module* and
   upload. If the upload or boot fails, unplug the TTP223 wires on GPIO 2/12/15 (boot-strapping pins).
5. **Check the board:** open the Serial Monitor at 115200. Each second it prints a line such as `T=29.8 MQ2=640 ... wifi=1`.
   The station turns green on `/hardware`.
6. **Polarity:** if the flame LED is on with no flame, set `FLAME_ACTIVE_LOW 0`. If the relay switches the wrong
   way, set `RELAY_ACTIVE_LOW 1`.
7. **Calibrate:** watch the MQ-2, MQ-135 and PM2.5 values in clean air for 2 minutes, then adjust
   `config/hardware.yaml` → `thresholds` (and the same numbers at the top of the sketch). Raw ADC values are not ppm.

## Linking RFID cards to workers
Scan a new card, then open `/hardware`. Under "Attendance", the row shows "Unregistered card" with **Link**.
Pick an existing worker (the face-ID worker list) or type a new name for a card-only worker. The same worker
keeps one ID (W-00x) for both face ID and RFID.

## Testing without the board
```
python -m tools.hw_sim                                   # green station
python -m tools.hw_sim --camera-fire                     # camera fire + green station -> NOT alarmed
python -m tools.hw_sim --scenario flame --camera-fire    # camera fire + flame -> CONFIRMED, siren
python -m tools.hw_sim --scenario smoke                  # sensor-only smoke alarm
python -m tools.hw_sim --scenario dust                   # orange WARNING (PM2.5)
python -m tools.hw_sim --scenario offline --camera-fire  # station silent -> camera-only alarm
python -m tools.hw_sim --rfid A1B2C3D4                   # card scan (again = exit)
```
Simulated camera events use `camera_id sim_cam`. Dismiss them in the feed after a demo.

## Honest limits (say these in the pitch)
- All thresholds are prototype values and are not calibrated against reference instruments. MQ readings are raw
  ADC counts, not ppm.
- FSR/touch sensors show contact or pressure only. They do not prove that PPE is worn, so the cameras decide PPE.
- The prototype network is plain HTTP on a trusted LAN. Production would need HTTPS or MQTT-TLS and a per-device key.
- If Wi-Fi is down, the ESP32 does not queue scans. It beeps long, and the scan is not recorded. It never claims it was.
