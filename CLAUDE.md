# NETRAX - PS6 (BPUT Hackathon 2026): PPE, smoke & fire safety AI

Read docs/ first: Problem_Statement_6.pdf, PS6_Project_Plan_v2.pdf (the master plan: feature IDs F01-F22,
architecture, section 7 module design, section 9 robustness, section 12 tests, Appendix D dashboard),
ppe-safety-ai-documentation.pdf (judging checklist).

## Machine
Windows, Python 3.11 venv in ./venv, NVIDIA GTX 1650 4 GB (CUDA torch cu128). Run inference in FP32
(no half/FP16 - GTX 16xx issues; ultralytics no longer accepts `half=`).

## Models (models/, trained in Colab, do not retrain without asking)
- ppe_v2.pt  YOLOv8s, classes person,helmet,vest,gloves,boots,mask. Val mAP50 0.872 (P .853 R .845).
  Per class mAP50: helmet .953, vest .944, person .862, boots .837, gloves .822, mask .815.
  Data: Roboflow construction-site-safety v27 + sdp-lfigk/ppe-detection-ozhfb v14, merged; NO-* classes dropped.
- fire_smoke_v1.pt  YOLOv8n, classes Fire,Smoke. Val mAP50 0.769 (Fire .841, Smoke .697).
  Data: sayed-gamall/fire-smoke-detection-yolov11 v2.
- yolov8n.pt (COCO, auto-downloaded) is used for PERSON detection - the PPE model's person class misses
  indoor/close-up people. PPE model supplies items only.

## Layout
config/  settings.yaml (all thresholds), zones.yaml (required PPE + risk), cameras.yaml (sources, zones)
edge/    detector, tracker (sv.ByteTrack, IoU fallback), association (compliant/missing/unverifiable),
         validate (item size + vest hi-vis colour), state_machine (N-of-M, grace, cooldown, resolve, ID handover),
         severity, fire_verify (colour, texture, person-overlap, flicker, growth), stream (health),
         outbox (SQLite offline buffer), publisher (preview frames + health), worker, run_edge, selftest
backend/ FastAPI: main.py (events ingest, ack/escalate/dismiss, cameras, zones, analytics, reports, MJPEG,
         websocket /ws/alerts), db.py (SQLite data/sentineleye.db), notify.py (escalation, Telegram or MOCK),
         reports.py (analytics, CSV, PDF)
         actuators.py (Windows SAPI voice calls the worker ID; winsound siren for every confirmed fire/smoke),
         worker registration API (/api/workers: photos -> SFace embeddings, W-001 ids; photos in data/workers/)
edge/identity.py  one person = one ID: YuNet face + SFace match vs registered photos, votes per track,
         visitors V-001 in memory only; worker._stable_ids + state_machine.rename keep alerts on one id
models/face_detection_yunet_2023mar.onnx, face_recognition_sface_2021dec.onnx (OpenCV zoo)
tools/   benchmark.py (F12), collect.py (own data + pre-labels), build_dataset.py (split BY SCENE from
         datasets/splits.yaml -> datasets/build/<task> + zip), evaluate.py (per-class P/R/mAP -> docs/eval),
         gallery.py (F18 failure gallery -> docs/failure_gallery, verdicts computed by current code)
edge/load.py (plan 7.5 load manager), edge/retention.py (snapshot retention)
datasets/ README.md, splits.example.yaml, own/<task>/<scene>/ (empty until the team records)
docs/    model_card.md (measured speed, 4-stream soak), data_plan.md, labelling_guide.md, cost_roi.md,
         architecture.md, demo_script.md, hardware_plan.md (design only), failure_gallery/
dashboard/index.html  single-file NETRAX dashboard (vanilla JS, no build step), served at /

## Commands
python -m edge.selftest                                   # must end with ALL TESTS PASSED
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
python -m edge.run_edge
python -m tools.benchmark --onnx                          # re-measure model latency
SENTINELEYE_MUTE=1 SENTINELEYE_DB=<scratch.db>            # env for backend tests: silent, scratch DB
SENTINELEYE_DATA_DIR / SENTINELEYE_CAMERAS / SENTINELEYE_BACKEND  # soak tests (data/loadtest/cameras.yaml)
python -m tools.gallery                                   # regenerate failure gallery
run_all.bat                                               # both + opens http://127.0.0.1:8000

## Rules
- Alert JSON contract (plan 7.6) is frozen; add fields only, never rename/remove.
- Only `missing` may raise a PPE alert; `unverifiable` never does.
- Never claim metrics that were not measured. Label mocked parts (notifications, RFID, ROI) as mocked.
- Keep selftest passing; add a test for every bug fixed.
- Annotated frames are saved per event in data/snapshots/<event_id>.jpg - inspect them to verify behaviour.
- Hardware phase STARTED 11 Oct (ESP32 station, see docs/hardware_integration.md). Camera fire/smoke in a zone with an
  online station alarms ONLY if the station confirms; station offline -> camera-only alarm (never treat offline as safe).
- Face ID is consent-based: only registered workers' photos/embeddings are stored; visitors never persisted.
  The judging doc prefers badge/RFID over face recognition - say so honestly in the pitch.
- Safety shoes (boots) are NOT required in zones.yaml until trained on own shoe data (logic kept).
- Fire/smoke tuning: read data/fire_debug.log after a live test before changing thresholds.
- Heredocs through the Bash tool mangle `\` - use the Edit tool for Python edits containing backslashes.

## Status vs plan (checked 2026-09-24)
Done: F01 (boots paused) F02-F07 F09-F17 F19 F21 F22; F14 heatmap; F20 Plan B (templated shift summary on
dashboard + PDF); F18 gallery v1; plan 7.5 load manager (4-stream soak measured); snapshot retention (sec 14);
docs: architecture, demo script, cost/ROI, labelling guide, hardware design note.
Partial: F08 (no real demo clips / phone camera yet), F18 hard-test METRICS (needs own labelled data).
Not started: slides deck + backup demo video (sec 16), F20 real LLM (optional).

## Update 11 Oct 2026 (v3 work, see docs/NETRAX_Model_Upgrade_Plan.pdf)
- New models: fire_smoke_v2A.pt (D-Fire fine-tune), ppe_v3A.pt (YOLOv8s-P2, 9 classes head,helmet,face,goggles,
  mask,vest,hand,gloves,foot). Hybrid in detector.py: ppe_v2 keeps helmet/vest/mask, ppe_aux=v3A gives the rest.
- person: pose (yolov8n-pose) -> keypoints; association.py owner = nearest body keypoint, hidden part -> unverifiable.
- Evidence rule: gloves/goggles/boots 'missing' only if bare hand / face / foot seen. Boots = foot + shoe classifier
  (models/shoe_cls.pt from tools/shoes.py); not judged until that file exists.
- fire_verify: dark-scene gate (dark_scene_luma, dark_smoke_min_conf). Per-class box colours (annotate.py + legend).
- cameras.yaml: cam_03 enabled:false (tile shows "switched off"); cam_04 overhead USB webcam (index 1).
- Own data: tools/frames.py (extract/pack/import/split/status), tools/shoes.py, docs/NETRAX_Own_Data_Guide.pdf.
- Colab: colab/*StageA.ipynb (done), colab/*StageB.ipynb (tonight: + old helmet/vest sets pseudo-labelled + own data x3).

## Next work (in order)
1. [team, in 2 days] Own data per docs/data_plan.md + docs/labelling_guide.md -> tools/build_dataset.py
   -> tools/evaluate.py (hard-set table) -> retrain v3 in Colab (ask first) -> re-enable boots.
2. [team] Demo clips demo/clips/warehouse.mp4 + fire.mp4, phone as camera 4 (IP Webcam) -> F08 done.
3. Slides (10, plan 16.2) from docs/architecture.md + demo_script.md + gallery; record backup demo video.
4. Gallery v2 with real clips: orange vest, cap vs helmet, shirt vs vest, dark room, crowd, kettle steam.
5. Hardware: flash firmware, calibrate thresholds on real sensors, link RFID cards (docs/hardware_integration.md).

## Update 11 Oct 2026 afternoon (hardware integration)
- backend/hardware.py (Hub: telemetry eval, FireFusion, RFID attendance toggle) + backend/hw_api.py (/api/hw/*,
  /api/attendance*, badge link); config/hardware.yaml (station -> zone, thresholds, fusion window).
- Event statuses added: verifying (waiting for station), suppressed (station clear, camera only); field verification.
- dashboard/hardware.html at /hardware; index.html has a Safety station strip + attendance toasts.
- firmware/netrax_esp32/netrax_esp32.ino (team pin map); tools/hw_sim.py simulator;
  python -m tools.hw_selftest must end with ALL HARDWARE TESTS PASSED.

## Update 11 Oct 2026 night (own data labelled)
- 27 own videos in datasets/raw_videos/{ppe,fire}; frames + labels in datasets/own/ (method: datasets/own/LABELS_README.md).
- Colab zips: datasets/upload/ppe.zip, datasets/upload/fire.zip (-> Drive MyDrive/PS6_models/own/). Splits in datasets/splits.yaml.
- Shoe crops checked: datasets/build/shoe_cls -> `python -m tools.shoes train` on the laptop -> models/shoe_cls.pt.
- Next: Stage B Colab (fire then PPE), then hard-test numbers into docs/model_card.md, then re-enable boots in zones.yaml.

## Update 12 Oct 2026 morning (Stage B done)
- Live models: fire_smoke_v2B.pt (own hard test 0.870, D-Fire+own val 0.706); ppe_aux = ppe_v3B.pt (own hard test 0.959
  vs v3A 0.940); shoe_cls.pt (14/15 val) -> boots judged again. Numbers + caveats: docs/model_card.md.
- tools/eval_hardtest.py scores any model on our hard-test scenes on the laptop.
- Deck: claude.ai artifact "NETRAX Hackathon Deck" (45 slides; image placeholders to fill).

## 12 Oct, morning (software while hardware is deferred)
- Smoke false alarms: `edge/fire_verify.py` now rejects a smoke box that is the same box as a person (IoU >= `smoke_person_iou` 0.5) and a smoke track that never drifts (`smoke_static_crops` 6). Selftest: 69 PASS, 31/31 live smoke false alarms rejected; real agarbatti smoke still alarms.
- `tools/benchmark.py`: labels the aux model by file name (v3B) and also times `shoe_cls` (224 px, no ONNX).
- `docs/demo_script.md`: 14 steps incl. phone-fire suppressed by station, real flame confirmed, RFID tap in/out; limits + numbers updated. Old copy in `_to_delete/`.
- Deck slide 17 (labelling) now shows real frames from datasets/own (uploaded as deck assets).
- Pending: hardware connection + cross-verification test (user will say when); re-run benchmark; shoe live test.
- Camera pair (12 Oct): cameras.yaml `share: webcams` on cam_01 (laptop, zone A welding_bay) and cam_04 (USB, now zone C assembly_line). One at a time (edge/share.py), other tile STANDBY + "Use this camera" (POST /api/cameras/{id}/activate -> data/camera_share.json), auto-failover after 15 s without picture. Backups in _to_delete/*_before_pair.*
- 12 Oct afternoon: DEMO CAMERA SETUP = laptop webcam (cam_01 welding_bay, same zone as the ESP32 station + RFID)
  + phone 1 (cam_02 warehouse) + phone 2 (cam_03 assembly_line), all on the friend's Wi-Fi "NOTHING". USB webcam
  (cam_04) and the camera pair removed. Phone IPs change: `python -m tools.find_phones [--write]` scans the Wi-Fi
  for IP Webcam (/shot.jpg must be a JPEG) and writes cam_02/cam_03. ESP32 simple sketch can post to /api/sensors
  (adapter hardware.from_simple_station); our firmware posts to /api/hw/telemetry. Laptop IP on NOTHING: 172.20.163.235.
- 12 Oct check_all: vest moved to ppe_v3B (aux_items), helmet stays ppe_v2; live set ~70 ms/frame. tools/compare_ppe_main.py (v2 vs v3B for helmet/vest/mask), tools/check_all.py (all checks -> docs/eval/check_all.log).

## 12 Oct afternoon: restricted zone, RFID reader, zone-priority alerts
- zones.yaml welding_bay `allowed_workers: [W-001, W-003]` (Chinmaya, Bibhu). Other REGISTERED workers -> HIGH
  `unauthorized_entry` (camera: edge/access.py AccessGuard, 1 s confirm, resolves 5 s after leaving; RFID: hw_api._rfid_access,
  resolves on tap-out). Camera + RFID for the same person are merged into one alert (main.ACCESS_ALIAS). Visitors unchanged.
- RFID reader: firmware/rfid_reader/rfid_reader.ino (friend's Google-Sheets sketch, now GET /api/hw/rfid?uid=&device_id=
  to the laptop, falls back to Google). Backend accepts uid/UID/cardUID.., deviceId/device_id, GET (plain-text reply with
  STUDENT ENTERED / EXITED / ACCESS DENIED) or POST. hardware.yaml attendance.readers/default_zone/sheets_url (laptop forwards
  every scan to the same Google Sheet). Dashboards: zone panel shows "Only: ..." + who is inside; hardware page NOT AUTHORISED chips.
- Zone priority: edge/priority.py GPU gate (high-risk camera first, fire candidate = top, no starvation);
  state_machine.by_risk (high 5/10 + 1.0 s ~1.4 s, medium ~2.1 s); identity max_faces_per_frame 3 (unidentified first);
  speaker = priority queue (fire 0, high-risk 1, medium 2, low 3, repeats +3, welcome 6; full queue drops least important);
  dashboard feed sorted: Critical, then zone risk, then severity, then newest.
- Tests: python -m tools.access_selftest -> ALL ACCESS TESTS PASSED (plus edge.selftest, tools.hw_selftest).
- 12 Oct evening: the friend's Google-Sheets RFID sketch (flashed, DASHBOARD_URL http://172.20.163.235:8000/api/rfid) got 404 -
  no scans reached the dashboards. Added POST/GET /api/rfid (hw_api) -> same handle_rfid; the sketch's event ENTERED/EXITED
  (decided by its Google Sheet) is used as-is and NOT mirrored to the sheet again; "UNKNOWN CARD" -> laptop worker list decides.
  Badges linked: W-001, W-003, W-004 only (W-002/005/006 have no card). Tests in tools/access_selftest (15 PASS).
- 12 Oct evening: PROJECT RENAMED SentinelEye -> NETRAX (all visible text: dashboards, API title, reports, notifications,
  docs, firmware; firmware/netrax_esp32/netrax_esp32.ino; docs/NETRAX_*.pdf; report downloads netrax_*.pdf/csv).
  Deliberately UNCHANGED (data safety / no breakage): folder name, data/sentineleye.db, SENTINELEYE_* env vars, colab/
  notebooks + colab/sentineleye-yolov8s-p2.yaml, runs/, models/. Backup of every changed file: _to_delete/before_netrax_rename/.
- 12 Oct evening: dashboard look = dashboard/theme.css + theme.js (linked from index/hardware/roi, no content change):
  aqua/teal palette, Day/Night switch in the header (saved per browser, ?theme=dark|light), pop-in/hover/bump motion,
  active unacknowledged alerts -> red reason + glow; station/sensor WARNING/CRITICAL glow. Originals: _to_delete/before_theme/.
- 12 Oct night: friend's DIRECT RFID sketch (no Google; POST /api/rfid {name,uid,event,timestamp}, toggles ENTERED/EXITED in
  ESP32 RAM, skips unknown cards). Backend now ignores the reader's event by default (hardware.yaml attendance.trust_reader_event:
  false -> laptop DB decides, survives ESP32 reboot); reply JSON has `result` (STUDENT ENTERED/EXITED/ACCESS DENIED/DUPLICATE).
  Adapted sketch firmware/rfid_direct/rfid_direct.ino: sends every card + device_id, beeps from the reply, boot ping.
- 12 Oct night: ONE DECISION PER LOOK (user: "analyse what the worker wears, then ONE alarm; no repeats; wrong names").
  edge/state_machine._decide: item states are only evidence; per worker ONE action after first_look_s (new person) or
  settle_s (after the missing set changes): open (all items) / update (full new list) / resolve, or info type ppe_ok
  ("PPE complete", only if every required item SEEN worn, max 1 per person per 10 min). Waits id_wait_s for the name.
  settings.yaml state_machine first_look_s/settle_s/id_wait_s (+ by_risk: high ~2.2 s, medium ~3.1 s, low ~3.8 s).
  backend: voice only when the item set changes (not on name/handover), SPOKEN = one call per (camera, person) per
  same_person_quiet_s 20, ONE reminder after 60 s (max_repeats 1), "PPE complete. Thank you." on info / compliant resolve
  (voice rank 5); action 'info' never creates an event row; stale info (>30 s) ignored. Dashboard toast kind ppe_ok.
  Identity: measured registered photos - Chinmaya~Chandrabhanu 0.383 > old threshold 0.36. Now match 0.45, margin 0.08,
  3 votes with >=60 % share, renaming needs 5 votes / 75 %, learning only >=0.65 & margin 0.15; data/identity_debug.log.
  SUSHREE has only 1 registered photo -> re-register with 3-5 (different angles/light). Live check: 1 call + 1 reminder
  in 2 min (was ~10). Known: tracker splits still open extra dashboard records (voice merged). Backups: _to_delete/before_one_decision/.
