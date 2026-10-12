# NETRAX - edge pipeline

## Files
config/settings.yaml   thresholds (N-of-M, grace, cooldown, fire verification, backend url)
config/zones.yaml      zone rules: required PPE + risk level
config/cameras.yaml    camera registry: source + default zone + optional polygons
edge/association.py    PPE-to-person: compliant / missing / unverifiable      (F02)
edge/tracker.py        ByteTrack person IDs, IoU fallback                     (F04)
edge/state_machine.py  N-of-M, grace, dedup, cooldown, resolve, ID handover   (F05)
edge/zones.py          which zone a worker stands in                          (F06)
edge/severity.py       Critical / High / Medium / Low                         (F07)
edge/stream.py         reader per camera, reconnect, ONLINE/DEGRADED/OFFLINE  (F08)
edge/fire_verify.py    growth, flicker, orange-vest / lamp suppression        (F13)
edge/outbox.py         SQLite offline buffer + auto sync                      (F17)
edge/worker.py         per-camera pipeline, snapshots, events
edge/run_edge.py       starts everything
edge/selftest.py       logic tests without camera or model

## Commands (from the sentineleye folder, venv active)
python -m edge.selftest      # logic tests - must say ALL TESTS PASSED
python -m edge.run_edge      # run the live pipeline (Ctrl+C to stop)

## Backend + dashboard
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000   # terminal 1
python -m edge.run_edge                                          # terminal 2
open http://127.0.0.1:8000
or just double-click run_all.bat

Optional real notifications: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
environment variables before starting the backend. Otherwise they are shown as MOCK.


## Worker ID + autonomous response
edge/identity.py       registered-photo face match -> one person = one ID (W-001), visitors V-001 (memory only)
backend/actuators.py   Windows voice calls the worker ID; siren for every confirmed fire / smoke
Dashboard -> "Workers & autonomous response": register with 1-5 photos or 3 shots from CAM-01.
data/fire_debug.log    one line per fire/smoke candidate per second: why it was confirmed / suppressed
