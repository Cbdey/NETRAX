# Demo script (plan 16.1): about 4 minutes, rehearse twice

**Before judges arrive:** run `run_all.bat` 5 minutes early (warm-up). Register the demo worker (Workers panel → Capture from CAM-01) and link the demo RFID card to W-001.
**Hardware:** power the ESP32 station 2 minutes early (MQ-2 warm-up is 60 s). Open `hardware.html`: the station must show **ONLINE, green**. If it is not online, the camera keeps working alone; say so and skip steps 8, 9 and 11 (the station must be mapped to the same zone as the demo camera).
Turn the laptop volume up. Close other GPU apps. Keep a backup screen recording of this exact script open in a tab.

| # | Do | Say (short) | Judges see |
|---|---|---|---|
| 1 | Show the dashboard | "Every camera is analysed on this laptop. Only alerts leave it." | Live tiles, green status, 4 KPI cards |
| 2 | Worker walks in with no helmet or vest | "It checks each item per person, per zone." | After about 3 s: HIGH alert "NO HELMET + VEST", worker **W-001** named, snapshot with face blurred |
| 3 | Wait | "Nobody has to watch the screen." | Speaker: *"Attention worker W 0 0 1 … please wear your helmet and safety vest now"*, repeated every 20 s |
| 4 | Worker puts on helmet + vest | "When they comply, the alert closes itself." | Alert → RESOLVED |
| 5 | Worker takes the helmet off again | "A new violation is a new alert, not spam." | New alert, new call |
| 6 | Worker leaves and re-enters | "Same person, same ID. We don't count people twice." | Still W-001 |
| 7 | Orange / red cloth or T-shirt in view | "Orange clothing is the classic false fire alarm." | No fire alert |
| 8 | Play a fire video on a phone in front of the camera | "A camera alone can be fooled by a screen. Our station checks the air." | Camera chip says **CHECKING**, then **CAMERA ONLY · hardware clear**: no siren |
| 9 | Light a match / lighter near the station (metal tray, extinguisher ready) | "Real flame: the camera AND the flame sensor agree." | FIRE alert **CONFIRMED by hardware**, station LED red + buzzer, siren, *"Fire alarm … evacuate"* |
| 10 | Acknowledge on the dashboard | "Supervisor stays in control." | Siren stops, station back to green, action logged |
| 11 | Tap the RFID card on the station, then tap again | "Attendance without a register: tap in, tap out." | Worker list: W-001 **ENTERED**, then **EXITED**, with time |
| 12 | Unplug the webcam (or stop a clip) | "A dead camera is visible, never silent." | Tile → OFFLINE, others keep running |
| 13 | Show heatmap + shift summary, download the PDF | "Supervisors get the shift in one paragraph." | Summary, heatmap, PDF |
| 14 | Open the ROI calculator + failure gallery | "Here is what fools a naive detector, and what we measured." | `docs/failure_gallery`, `roi.html` |


**If the station drops off Wi-Fi during the demo:** the dashboard shows *hardware offline* and camera fire alarms go through as camera-only (never silently suppressed). Say: "If the station fails, we fail safe."
**Rehearse without the ESP32:** `python -m tools.hw_sim --scenario flame` (or `smoke`, `normal`, `offline`, `--rfid <UID>`, `--camera-fire`).

## Honest-limitations slide (say it before they ask)
- A phone fire video is still *detected* by the camera; the ESP32 station is what stops it becoming an alarm. Without a station in that zone, it would alarm.
- The station covers its own zone only (a few metres); other zones rely on the camera checks.
- Safety shoes: classifier trained on our own 101 shoe photos, live test still to do.
- Face ID is consent-based; RFID badge is the main attendance method.
- Our hard-test sets are small (123 PPE / 142 fire images) and their labels were drafted by our models, then reviewed.
- Notifications are MOCK unless a Telegram bot is configured; cost figures are labelled assumptions.

## Numbers to quote (all measured)
About 70 ms per frame with all 5 live models (GTX 1650, measured 12 Oct) · 4 cameras at once, about 22 frames/s in total (earlier 3-model set) ·
edge → dashboard 98–175 ms · median time to alert about 2.8 s · 41.7 KB per alert · 31/31 live smoke false alarms rejected (selftest) ·
own unseen scenes mAP50: PPE v3B 0.959, fire v2B 0.870 · vest-off scene: vest precision 0.70 with v3B vs 0.54 with v2 (why vest now comes from v3B).
