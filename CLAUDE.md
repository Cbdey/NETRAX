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

## 29 Oct 2026: FINAL ROUND (requirements brief "NETRAX Elaborated System Requirements", sections 1-12)
- Dashboard: / = NEW control room (dashboard/index.html): big camera wall (Enlarge), incidents with Active /
  Acknowledged / Resolved tabs (ack != resolve: POST /api/events/{id}/resolve), dashboard-wide red state + violation
  cards for active unacknowledged High/Critical PPE / access incidents (settings incidents.emergency_severities),
  fire panels L1 yellow / L2 critical with live feed / L3 full-screen evacuate overlay, entrance popup, AI assistant,
  alert-delivery table, measured latency, system health. Old dashboard -> /admin (dashboard/admin.html: workers,
  reports, analytics). /login page.
- Login + roles (backend/auth.py): viewer (names + snapshots hidden), supervisor (act, assistant), admin (workers,
  notify test, users, audit). config/users.local.yaml (PBKDF2), first start writes data/initial_passwords.txt.
  python -m tools.users set <name> <pw> [role]. Devices post without login (exact paths in auth.DEVICE_EXACT).
  Tests: SENTINELEYE_AUTH=0. Audit log table audit_log (/api/audit).
- Notifications (backend/notify.py rewrite): Telegram bot, Twilio voice call + SMS, SMTP e-mail; background
  Dispatcher thread, bounded retries, provider-confirmed status only (delivered / sent / failed / not_configured),
  one message per (event, channel, kind), fire + Critical never held by cooldown. Policy config/notify.yaml,
  credentials config/secrets.local.yaml (template secrets.example.yaml; git-ignored). python -m tools.notify_setup.
- Fire levels (backend/fire_levels.py, settings fire_levels): L1 WARNING / L2 CRITICAL / L3 EVACUATE from flame area,
  growth, duration, station sensors, sources; level only goes up; siren from L2 (L1 = voice warning). main.regrade_fire
  + fire_level_loop (1 s); every raise re-notifies.
- Entrance (edge/entrance.py): cameras.yaml role: entrance (cam_04 USB index 1, zone main_entrance helmet/vest/boots).
  One decision per person: approved / denied (names item) / manual (unclear, >1 person, too far); >= 6 frames and >= 1 s;
  recheck after 3 s; gate 'closed' unless approved AND entrance.controller_connected. Backend ingest_entrance ->
  events type entrance_check (details JSON) + entrance_checks table; POST /api/entrance/{id}/decide = manual check.
- AI assistant (backend/assistant.py): grounded parser (dates, clock times, windows, types, items, zones, cameras,
  workers, status) -> SQL -> answer only from rows + record links; no record -> "No matching record was found";
  optional Anthropic key in secrets (only re-words rows). POST /api/assistant, assistant_log.
- Location (backend/location.py, config/site.yaml): hardware GPS (POST /api/location/hardware, for the later GSM/GPS
  unit) > device GPS (browser) > configured site; stale readings marked; "Location unavailable" otherwise.
  Stored per event (events.location JSON).
- Latency: edge/engine.py ONE shared model set, frames of all cameras batched (processing.shared_engine, max_batch);
  stale frames (> max_frame_age_s) skipped; boxes drawn only when a preview is due; JPEG encode in publisher thread
  (preview_fps 10, X-Captured-At header); USB capture MJPG 1280x720 buffer 1. backend/perf.py p50/p95
  (/api/perf, dashboard reports picture age + event arrival). python -m tools.latency_report --label ... -> docs/eval/
  latency_report.md. Optional TensorRT FP32: python -m tools.export_engines + models.runtime: tensorrt.
- Overlay: per-worker item lines (HELMET OK / NO GLOVES / GOGGLES ?) smoothed over ~8 frames (display only).
- Health (/api/health, every 3 s on the websocket): edge, cameras, DB probe, each alert channel, queue, outbox,
  station, location, disk.
- Tests: python -m tools.final_selftest -> ALL FINAL TESTS PASSED (acceptance 01-14 with fake providers, auth, engine,
  edge pipeline) and writes docs/eval/final_selftest.md. Older selftests still pass.
- Hardware (GSM module, lock/turnstile controller) deliberately NOT done yet (user: software first).
- 29 Oct evening (user: "new missing item got no alarm/message; detect PPE off within ~1 s; bigger cameras; better admin"):
  backend main: update whose item list GROWS -> voice "Also missing: X" + notify_async("more PPE missing: X") (one per new
  set); same list / items put back -> silent. SPOKEN = (time, items). edge state_machine: by_risk high 6/10 + 0.4 s grace +
  0.4 s settle (~1.4 s after PPE off); _decide waits <= evidence_wait_s 1.5 for items PENDING/onset and (first look) unseen
  body parts -> first alarm complete. Tests never read real secrets / never send (SENTINELEYE_SECRETS, SENTINELEYE_NO_NOTIFY).
  Control room (index.html): live summary band (incidents, fire, cameras, RFID inside, compliance, alerts today), one-line
  health strip + Details, acknowledged list -> one strip + "Show list", camera wall 3:1, views Grid / Focus (alarm camera
  leads, click a small one to lead; ?view=big) / Full screen. Admin (admin.html, rewritten): sidebar app - Overview, Incidents
  (search /api/events/search + export), Workers (cards), Alerts & channels (status, test, delivery log), Cameras & zones,
  Users & audit, System health. Originals: _to_delete/before_ui_29oct/. Phone call/SMS still OFF in notify.yaml (team).
- 29 Oct night (user: "resolved the alert while still wearing nothing -> no new alert"): backend _act(resolve) on a
  ppe_missing / unauthorized_entry event -> OP_CLOSED; the reply to that camera's preview frame POST carries
  {"closed": [ids]} (30 s); edge publisher.take_closed -> state_machine.operator_closed / access.operator_closed forget
  the alert and take a fresh look (first_look_s, ~2 s) -> still missing = NEW event with field reopen_of; backend clears
  SPOKEN for that person, voice again, notify reason "still missing after resolve" (bypasses flood control).
  Acknowledge / False alarm (dismiss) do NOT re-check. Tests in edge.selftest + tools.access_selftest.
- 29 Oct night, Step 2 VIDEO CLIPS (brief 3/5/8): edge/clips.py ClipRecorder (one, shared; run_edge) keeps pre_s 4 s of the
  annotated picture per camera (6 fps, 640 px, memory); on an OPEN action (ppe/access/fire/smoke/entrance) + post_s 4 s ->
  background thread blurs faces (own YuNet) -> H.264 MP4 via PyAV (WebM VP8 via OpenCV fallback) -> POST /api/events/<id>/clip
  (device path, no login). Offline: file stays in data/clips, uploaded on the next try / edge restart. Backend data/backend_clips,
  events.clip_path, event field `clip` (/clips/<id>.mp4, supervisor+, viewer masked), retention = snapshot_retention_days.
  Shown in control-room + admin incident details (video + snapshot side by side), assistant cards "▶ clip". settings.yaml clips:.
  Tests: final_selftest [CLIP] x6. Phone call / SMS: user decided NOT now (Telegram + e-mail only; future work).

## 2 Nov 2026: guide's review (entrance = who + what, popups, separate entrance dashboard)
- Cameras: laptop is on the phone's own hotspot "OPPO Reno14" (laptop 172.24.255.235, phone gateway 172.24.255.85). cam_02 = http://192.0.0.4:8080/video
  (address the IP Webcam app shows; rotate 90). cam_03 enabled:false until a 2nd phone (tools.find_phones --write sets + enables it; keeps a phone that still answers
  even outside the scanned subnet). backend load_cameras(include_off=True) so its zone tile still shows. cam_04 USB entrance: index 1, not plugged in at last check.
- Entrance (edge/entrance.py + main.ingest_entrance / entrance_scan): identity from face (waits id_wait_s) or RFID; verdict sentences in main.entrance_verdict:
  "W-001 Chinmaya verified - all required PPE worn. You may enter." / "Unregistered worker - PPE verified. You may enter." / "... missing. Entry not permitted.".
  details.verdict + verdict_public (viewer role) + identity{kind,by,worker_id,name,face,card}; details.message keeps the brief's exact text. One decision per person: a re-check
  sends a new one only when the verdict changes. Card != face -> manual, never auto-approved. Card valid rfid_window_s 15 before the decision, rfid_after_s 6 after it (only
  a decision with no recognised face). Refused person is taken out of attendance again, back in once approved (main._attendance_fix). Tap-out names nobody.
- hardware.yaml: reader esp32-rfid-01 -> main_entrance (DECISION: the one reader stands at the site entrance; welding bay stays guarded by face ID, edge/access.py. A 2nd reader
  -> map it to welding_bay). ESP32 sketch DASHBOARD_URL still has the OLD laptop IP (172.20.163.235) and Wi-Fi: must be changed + reflashed to work on the hotspot.
- UI: dashboard/nx.css (aurora + glass look, Bahnschrift, offline; loaded after each page's style on index, entrance, admin, login), dashboard/alerts.js NetraxAlerts: alert popup per new /
  worse incident ("1 of 3", Acknowledge / Resolve / Details / Later, snapshot or live feed), entrance popup green VERIFIED / red DO NOT ENTER / amber MANUAL; control room + admin show
  approved as a small corner card. Page /entrance = dashboard/entrance.html: USB feed, latest decision, today's counts, devices, inside now, searchable log, manual approve / refuse;
  /api/entrance/status. Control room wall: 3 zone cameras + entrance tile (link to /entrance). Violation panel = compact strip.
- Acceptance: backend/acceptance.py, admin page #acceptance: tests 01-14, pass / fail / n/a, note, screenshot, evidence read from the DB (never marks a pass itself), export .md.
- Latency: docs/eval/latency_report.md has 1 REAL run (1 camera, nobody in view). Multi-camera + incident numbers still to measure with phone + USB live.
- Tests: final_selftest [ENT] + [BUILD] (all .py compile). UI checked with headless Edge over DevTools on a scratch backend (no JS errors).
- REMINDER: shell heredocs mangle backslashes AND apostrophes here: write patch scripts with the Write tool. One such patch silently broke tools/find_phones.py.

## 4 Nov 2026: smooth video with all cameras ("video hangs / not continuous", ChatGPT plan reviewed)
- Evidence (data/edge_run.log, 2 Nov): 1 camera, nobody in view = 11.9 fps AI (latency_report). 2 cameras with people = AI fell
  to 3 fps then 0.7-1.7 fps; the dashboard picture was drawn by the AI worker, so the VIDEO ran at the AI rate (= "hangs").
  GPU batch only ~57 ms -> the time went to CPU work: face detection (YuNet on the full 1920 px phone frame, one lock for all
  cameras), phone MJPEG decoding at 1080p/30 fps, plus the load manager slowing cameras although the GPU was idle.
- edge/live.py LiveView (processing.live_view/live_fps 12/live_width 800): newest camera frame + latest AI result (worker.overlay)
  drawn at 12 fps, independent of the AI; corner label "AI x fps"; clips fed from it. live_view: false = old behaviour.
- identity.detect_max_side 960: YuNet on a scaled copy, boxes/landmarks mapped back; embeddings still full resolution;
  backend registration (FaceEngine default) unchanged.
- worker stage profiler (ai_engine, tracker, face_id, ppe_match, decisions_fire, draw_emit) -> health stages_ms + [perf] lines
  in the edge window every 10 s (also GPU busy share, face detect ms, live draw ms).
- engine.busy_share; LoadManager slows a camera only if GPU busy >= load.gpu_busy_min 0.85. Missing USB camera retried every 30 s.
- tools/camdiag.py: all cameras together, NO AI -> fps, longest freeze, freezes > 0.5 s, verdict -> docs/eval/camdiag.md.
- tools/phone_tune.py: reads IP Webcam /status.json?show_avail=1, --apply sets video_size ~960x540 + quality 55 (read back).
- Rejected from the ChatGPT plan: FP16 (GTX 16xx issue, rule above), switching to YOLOv8n / retraining (GPU is not the
  bottleneck, accuracy loss), rebuilding a separate pipeline (ours already has per-camera reader threads + latest frame +
  one shared engine). Kept: raw-vs-AI split (as overlay), lower phone resolution/fps, dedicated network, camera-only test.
- Tests: final_selftest [LIVE] x4. Backups of replaced files: _to_delete/before_live_view_4nov/.
- 4 Nov evening (user: "video hangs, big popups irritating, need sudden alerts, calm dashboards"), MEASURED on the laptop:
  * AI starved by Python's GIL (camera/live/publisher/face threads): in-process batch 440 ms for 2 frames vs ~70 ms/frame alone.
    edge/engine_proc.py ProcessEngine = models in their OWN process (spawn), frames via shared memory per camera, same
    interface (infer/stats/busy_share/det); processing.engine_process (false = old thread engine, also the fallback).
    3 cameras: AI 1.7 -> 3.4-3.9 checks/s each. Engine groups frames by SHAPE (mixed portrait/landscape in one call was
    ~30 % slower: ultralytics pads to squares). Detector: pose first, PPE models only on frames with a person.
    run_edge caps OMP/MKL/torch/cv2 threads at 4, cudnn.benchmark. Prescaling frames before the models: tested, no gain (reverted).
  * Face ID async (identity.async: true): IdentityService.start_async/wants/submit/annotate, one background thread on the newest
    frame per camera; unnamed person checked every frame -> name in ~1 s. face_boxes() = last face per track -> snapshot blur
    without a 2nd YuNet run (worker._privacy_boxes; else head from pose keypoints, else top of box).
  * Video: publisher = one sender thread per camera; backend /ws/video pushes 'cam|captured|server\n'+JPEG (client may
    subscribe {"cams":[...]}); dashboard/video.js shows any <img data-live=cam>, falls back to /api/frame polling.
    Measured: 12 fps per camera, picture age 44 ms median, frame -> alert on dashboard 0.74 s (docs/eval/latency_report.md).
  * Camera health: DEGRADED only if picture stale, camera < 4 fps or AI < 1 check/s (not "AI below target"); tiles show
    "LIVE · 12 fps · AI 3.8/s · 40 ms".
  * Alerts: NO big popups. Control room: new alert slides into the "Live alerts" panel (keyed cards, NEW / UPDATED pill,
    glowing border, "also missing" item outlined), short tone (bell button mutes), newest first, fire first. Violation strip,
    zones / delivery / latency / health tables moved off the control room (links to admin). alerts.js: mode "toast" (small
    corner card) on admin/entrance, fire = red frame on every page + fixed red banner with LEVEL (index has its own fire panel
    + L3 overlay), entrance result = compact card except on /entrance (full green/red/amber screen).
  * nx.css calm rework: solid panels, one teal accent, no aurora/glow except alerts and fire.
  * state_machine id_wait_s 1.5 -> 1.0, evidence_wait_s 1.5 -> 1.0 (replay at 4 AI checks/s: helmet off -> alert 2.25 s,
    hands hidden / name pending 3.25 s; gloves-in-first-alarm and right-name tests still pass).
  * Tests: edge.selftest async face ID x2; final_selftest live-video WebSocket, camera health rule, AI process.
- 5 Nov: cameras on THIS laptop with the USB webcam plugged in: index 0 = USB Logi C270 (cam_04 entrance), index 1 = laptop HP
  (cam_01 welding bay) - checked on the live pictures. cam_02 phone http://192.0.0.4:8080/video (IP Webcam app keeps stopping:
  keep it open, screen on; its picture was ~1.5 s old at 1080p -> python -m tools.phone_tune --apply). cam_03 enabled:false (no 2nd phone).
- 5 Nov alerts in a SIDE PANEL on every page (user: "slight popup with border colour, not covering the page"): alerts.js rail "dock"
  (fixed right panel, body padding-right; collapse button) on admin / entrance / hardware: violations newest first + entrance cards;
  shared card()/keyed() (no re-render flicker; NEW / UPDATED pill, glow on new / worse). Control room: "Entrance" side panel above
  Live alerts (green verified / red do-not-enter / amber manual with Allow / Refuse). Full entrance screen only on /entrance (gate(g,{screen:true})).
  hardware.html now loads nx.css + alerts.js. No toasts / popups for alerts any more.
- 5 Nov admin REPORT GENERATOR (#reports): presets (last hour, this shift, today, yesterday, 7 days, month) or custom from/to + type, zone,
  camera, worker, severity, status, title -> preview (KPIs, summary lines, most missed PPE, by zone / camera / worker, records) and PDF / CSV.
  GET /api/reports/custom (reports.custom_events / custom_summary; to_pdf adds breakdown tables; audit-logged). final_selftest [REP] x3.
- 5 Nov entrance id_wait_s 2.0 -> 1.0 (entrance decision was ~6 s with a manual result).
- 5 Nov LIVE measurement (real cameras, docs/eval/latency_report.md): laptop cam 12 fps live, picture age 39 ms median; USB C270 7.5 fps
  (camera limit in a dim room); AI 6.9 / 4.0 checks/s; frame -> alert on the dashboard 0.28 s.
- 5 Nov evening ZENTRIX command agent (teacher: "portal run by the AI when supervisor/admin is absent, but only on their command"):
  backend/agent.py plan() (pure) + POST /api/zentrix (main.zentrix, _zx_execute through the normal _act / entrance_override paths,
  audit 'zentrix_command', action note "via Zentrix: <command>"). Understands typed + spoken commands, wake word "hey zentrix",
  misspellings (difflib onto VOCAB): navigate (pages + admin sections), cameras (enlarge by zone/number, all cameras, focus view,
  full screen), analyse = LIVE VERIFICATION per alert (camera tile badge still shows it / station confirmed / camera offline /
  weak confidence / acked but unfixed) -> verdict + recommendation; 'apply' runs them; act on alerts (ack / resolve / false alarm /
  escalate; "acknowledge OR resolve" = per alert the verified choice), entrance allow / refuse, reports, mute, theme, questions ->
  assistant.answer. NEVER automatic. Confirmation (ZX_PENDING per user, 120 s) for bulk resolve / dismiss, fire, >1 escalation,
  overriding an entrance refusal. Viewer: questions + navigation only, names masked. dashboard/zentrix.js on every page
  (floating bottom-left panel, Alt+Z, push-to-talk 🎙 Alt+M, 👂 always-listen for "Hey Zentrix", spoken replies; Web Speech API
  needs Edge/Chrome, a secure page (http://127.0.0.1:8000 or https) and internet; commands for another page are queued in
  sessionStorage and finish there). Pages register handlers (index: focus/unfocus/view/fullscreen/event/mute/theme; admin:
  event/report). Old control-room assistant panel removed (Zentrix answers history questions). Tests: final_selftest [ZX] x11.
- 5 Nov evening control room: two separate sections - "Entrance alerts" (green/red/amber, today's counts) and "PPE · Fire · Smoke
  alerts" (entrance checks no longer in that list).
- 5 Nov evening: rearm_s 5.0 -> 2.5 (fixed PPE then took it off again: new alert 3.8 s -> 1.4-1.8 s; edge.selftest test added).
  live_fps 12 -> 15. video.js decodes each picture off-screen and swaps only decoded ones, newest-only while busy.
  edge/stream.black_picture: a black picture -> DEGRADED + detail "picture is black ..." (tile: BLACK PICTURE · re-plug camera).
  USB C270 gave black frames at 1 fps until the edge reopened it after a stop (then 7.5 fps again).
- 5 Nov evening live video measured (20 s, 3 cameras, /ws/video): laptop 14.9 fps, longest gap 98 ms, age 32 ms; phone 13.5 fps,
  max gap 157 ms, age 42 ms (after the edge receives it; phone already 960x720 q49); USB 7.4 fps (camera), age 31 ms.
- 5 Nov night (user: "nothing you did shows"): ROOT CAUSE = browsers served OLD cached pages/scripts (no cache header). Fix: main
  middleware no_stale_dashboard (Cache-Control: no-cache on pages + /static) and ?v=5nov2 on every /static link. Bump ?v= when
  changing dashboard JS/CSS. Entrance: cards say "VERIFIED · PERMITTED" (green lamp) / "NOT VERIFIED · NOT PERMITTED" (red lamp) /
  "MANUAL CHECK · WAIT"; full-screen entrance overlay OFF everywhere (alerts.js gate needs opt.gateScreen); entrance page shows the
  same small "Entrance alerts" container (hero banner removed). New alert cards pop (nxpop) + glow in their own container.
  Zentrix: big "🎙 Zentrix AI" launcher, "🤖 Zentrix" button in every top menu, "🎙 Speak" button always visible (explains when the
  browser blocks the mic: only http://127.0.0.1:8000 or https). Browser-measured live video (control room, real cameras):
  laptop 14.9 fps max gap 123 ms, phone 13.9 fps max gap 182 ms, USB 7.4 fps. USB C270 intermittently gives BLACK frames at 1 fps
  (brightness 0, every exposure setting, any process) = lens covered / no light / loose USB - hardware, not software.
- 5 Nov late: ENTRANCE FALSE PERSON fixed (chair with clothes = "unregistered worker", detections 0.53-0.60, people 0.83+):
  EntranceChecker._real_body (conf >= min_person_conf 0.65 AND >= 5 pose keypoints incl. head/shoulder) + _still_object (a weak
  < 0.8 detection that never moves 6 s without a face -> background for 10 min). settings entrance.*; final_selftest [ENT] x3.
- 5 Nov late: USB C270 black at 1 fps = DirectShow "no frame" timeout (camera stopped streaming). Windows USB selective suspend
  was ON -> switched OFF for the current power plan (powercfg ... 2a737441-... 48e6b7a6-... 0; undo: set 1). Re-plugging needed
  once (device restart needs admin). edge/stream: black for 8 s -> release + reopen the camera automatically. MSMF backend tested:
  29 fps but HANGS on read with two cameras -> stay on DirectShow. Index map: DSHOW 0 = USB, 1 = laptop (MSMF reversed).
- 5 Nov late live view: frames sent AS THEY ARRIVE (4 ms poll), capped by live_fps 16 (fixed 15/s clock beat against the phone's
  15/s -> skipped frames = stutter). 30 tested: AI 6 -> 1.5/s + hitches -> 16. Server output: laptop 18.9 fps max gap 102 ms,
  phone 14.7 fps max gap 145 ms. Headless-browser 0.35 s hitches are the software-rendering test browser, not the stream.
- 6 Nov: Wi-Fi 192.168.60.x (laptop .49). cam_02 = http://192.168.60.53:8080/video, cam_03 = http://192.168.60.58:8080/video
  (ENABLED), both rotate 90, phones already 640x480 q49. 4 cameras live.
- 6 Nov USB C270 7.5 fps ROOT CAUSE: DirectShow ignores MJPG set BEFORE size -> uncompressed YUY2 = 7.5 fps at 720p.
  edge/stream._open now sets size, fps, THEN MJPG (reads the format back, warns if not MJPG): USB 29.8 fps. Test usb_order_test.
- 6 Nov live boxes follow the person: edge/live.Motion learns each track's velocity from the last two AI results
  (worker.overlay now has "ts" = AI frame time) and moves person / item / part boxes to the shown frame's time (<= 0.5 s ahead,
  <= 1.5 frame widths/s). Display only. settings processing.live_predict. Tests motion_tests x2.
- 6 Nov measured, 4 cameras (laptop, 2 phones, USB), real headless browser on the control room: 19 / 14.8 / 14.5 / 7.5 (before
  the USB fix) pictures/s, no gap > 250 ms, longest 105-168 ms, picture age 21-36 ms; server stream after USB fix 19 / 14.6 / 14.7 /
  19 fps, max gap 97-166 ms over 30 s; AI 4-7 checks/s per camera; NETRAX CPU ~20 % of 12 threads, browser ~7.7 %.
  Display method compared (decode-then-swap / plain swap / createImageBitmap+canvas): same CPU, canvas worst -> kept current.
- 6 Nov: team's industrial safety sketch connected -> firmware/netrax_station_team/netrax_station_team.ino. POSTs JSON every 1 s to
  http://192.168.60.49:8000/api/hw/telemetry as esp32-ppe-01 (hardware.yaml, zone welding_bay); fields temperature_c, humidity_pct,
  flame_detected, mq2_adc, mq135_adc, helmet_fsr_adc, pm_adc, pm_sensor_v (raw, NOT calibrated -> no pm2_5 sent), accel/gyro,
  uptime_s, alarm_level, buzzer_on, relay_on, rssi; reply led red/orange drives the LEDs + buzzer + relay (stronger of local/NETRAX).
  Fixes vs the team version: never sent anything; URL was old IP + /api/rfid; DHT22 read every 2 s; helmet FSR = WARNING (was
  CRITICAL -> buzzer always on); Wi-Fi auto-reconnect; 1.2 s HTTP timeout. Verified on a scratch backend (login on): normal -> green,
  2 flame readings -> CRITICAL red / alarm / relay. Laptop Wi-Fi "CoE In HPC and EmTek" (Public), firewall allows python.

## 11 Nov 2026: "Industrial Safety Intelligence" dashboard (team's ChatGPT 9-section spec)
- NEW page /intelligence = dashboard/intelligence.html (one self-contained file, vanilla JS + inline SVG charts, offline; loads only
  video.js + zentrix.js). ADDITIVE: it only READS existing APIs (/api/hw/state, /api/hw/history, /api/cameras, /api/zones, /api/events,
  /api/workers, /api/attendance, /api/health, /api/analytics/summary, /api/reports/custom, /ws/alerts, /ws/video); the only writes are the
  normal ack / resolve / dismiss calls. No pipeline, alert logic or existing page changed (only a "Safety Intelligence" menu link added to
  index / entrance / admin / hardware, route + PAGE_PATHS in main.py, one PAGES entry in agent.py: "open safety intelligence / factory map").
- Sections: Overview (SAFE / WARNING / CRITICAL / NOT MONITORED - offline is never shown as safe), Live Sensors (10-min graphs, limit +
  baseline lines, "no reading" = sensor failure, CSV), AI Monitoring (live tiles, per-camera PPE chips), Worker Management (RFID, entry /
  exit, safety score = 100 - Critical 20 / High 12 / Medium 6 / Low 3 per violation in 30 days), Alerts Centre (tabs + actions + system
  notices), Predictive Analytics, Factory Map (schematic of zones.yaml, not to scale), Device Health, Reports (PDF / CSV).
- HONEST LABELS (keep): Predictive = least-squares trend line over 3 min projected 5 min + vibration z-score, NOT a trained ML model;
  battery "not measured"; AI accuracy table = model-card numbers, not live accuracy; compliance % needs the live edge.
- Not adopted from the ChatGPT plan: React / Tailwind / MQTT / Firebase / Chart.js (our dashboards are offline, no build step; HTTP + WebSocket
  already do the job). The team station sketch sends accel / gyro / pm_adc, which hardware.FIELDS drops -> vibration and PM2.5 show "no reading"
  for that firmware (the simulator sends them).
- Tests: final_selftest [INT] x3 (116 PASS). Checked in headless Edge (dark / light, 1500 px and 390 px): 0 JS errors, no sideways scroll.
- 11 Nov REDESIGN (user: "eye-catching, each panel its own job, no repeated data, every section its own bordered container, Zentrix at the top,
  a new alert pops inside its PPE / fire box"). Backups: _to_delete/before_redesign_11nov/.
  * zentrix.js: launcher is a gradient "Zentrix AI" pill in each page's TOP BAR (pages mark the place with <span data-zx-slot>; fallback
    .nxnav / header nav / fixed top-right). Panel drops down under it (top-right). The old bottom-left launcher and the extra menu button are gone.
  * Control room (index.html rewritten, same features + ids used by Zentrix): KPI band; left = Live cameras (3 cameras -> 1 big + 2 small) and
    under it Entrance alerts (gate camera live + today's permitted / not permitted / manual + latest cards), Zones & people, Safety station
    (live ESP32 readings via /api/hw/state + ws 'hardware'), System health (no camera / station rows - they have their own boxes);
    right = separate 🔥 Fire & smoke box (L1/L2 details + live feed INSIDE the card; L3 = full-screen overlay) and 🦺 PPE & access box, each with
    its own Active / Acknowledged / History tabs. New alert -> card pops (nx-new) + its box border pops / glows (.box.bump). The old alertzone
    panels and the health strip were merged into these boxes (no duplicates).
  * Safety Intelligence (intelligence.html rewritten): one colour per section, banner + bordered boxes; Overview = status + 6 KPIs + "Needs
    attention now" (links, no alert list); AI = judging mix + camera feeds (no alert lines); Alerts Centre = 3 boxes (fire / PPE / restricted
    access) keyed, pop + box bump on new; Predictive no longer shows 7-day bars (moved to Reports "trends"); Map detail = counts + links (no
    sensor table); Device Health hides camera / station rows from the component table (shown above). final_selftest [INT] still passes.
  * Checked in headless Edge (dark / light, 1600 px and 390 px): 0 JS errors, no sideways scroll; new-alert pop verified (ppeBox bump + nx-new).
- 11 Nov evening (user: "3 cams 50/25/25, camera on/off from the dashboard, same alerts in every panel"):
  * Control room camera wall with 3 zone cameras = ONE row: first camera (welding bay) 50 %, the two phones 25 % each, same height
    (phones are upright, so the tall tiles suit them). < 1100 px: big on top, two below.
  * Camera ON / OFF from the dashboard: edge/power.py (data/camera_power.json, env SENTINELEYE_POWER_FILE for tests). OFF -> the edge
    reader goes STANDBY (device / phone stream released) = no picture, no AI, no alarms; survives restarts. POST /api/cameras/{id}/power
    {"on": bool} (supervisor + admin; auth.classify special case, the rest of /api/cameras/ stays admin), audit camera_on / camera_off,
    broadcast 'cameras'. cameras_payload adds fields power ("on"/"off") + can_power; a powered-off camera shows status OFF, no badge.
    cameras.yaml enabled:false cameras cannot be switched on from the dashboard (config decision). Buttons: control room tiles (⏻ Off next
    to Enlarge, green "Turn on" in the off tile, confirm before off), intelligence AI tiles, admin Cameras & zones.
  * Same alerts everywhere: alerts.js side panel (admin / entrance / hardware) now has the control room's sections - 🔥 Fire & smoke,
    🦺 PPE & access, 🚪 Entrance - each a bordered group; a new alert pops inside its group and the group border glows (nx.css .rgrp.bump).
    Admin camera tiles and intelligence AI tiles list each camera's active alerts (same records as the control room).
  * Tests: final_selftest [CAM] x2 (118 PASS).
- 11 Nov late: STALE RFID "inside" fixed: ANUSKA (W-004) entered welding_bay 13 Oct 12:59, exit tap never recorded -> shown inside +
  NOT AUTHORISED for a month. Added a manual EXITED attendance row (device_id 'dashboard') + audit_log 'attendance_checkout'.
  DB backup: _to_delete/db_backup_11nov/sentineleye_before_anuska_fix.db. (Future idea: a "check out" button for stale entries.)
- 11 Nov late PHONE CALLS ONLY FOR CRITICAL FIRE / SMOKE (user: "Twilio not good, call the supervisor for fire/smoke from software or
  hardware"): notify.py phone.provider (config/notify.yaml) = callmebot (default: free CallMeBot API, Telegram VOICE call that reads
  msg["call"] <= 250 chars; recipient sends /start to @CallMeBot_txtbot once; no audio on iPhone Telegram) | android (adb on a USB
  Android phone: am start CALL tel:+91..., hang up after ring_s; no internet, rings only) | twilio (kept). Recipients: secrets.local.yaml
  call.to (@telegram_name or +91 number; env ALERT_CALL_TO). notify.call_allowed: fire/smoke, Level >= 2 or severity Critical (a
  station-only fire before its level), not verifying/suppressed; enforced in channels_for (phone.only_fire_critical). One call per
  fire (recall_after_s 120); again for Level 3 or an unacknowledged escalation. 'sent' = provider accepted (never 'delivered').
  enabled_channels now [telegram, email, call]; PPE / access / Critical rules have no call. Tests: final_selftest [CALL] x5 (fake HTTP /
  fake adb; legacy tests run with NETRAX_CALL_PROVIDER=twilio) -> 123 PASS. Backups of notify.py / notify.yaml in _to_delete/db_backup_11nov/.
- SECURITY 11 Nov: config/secrets.example.yaml (tracked) had the real Gmail app password since commit fb74e29 (local only, NOT pushed -
  origin/main does not contain it). Template blanked; the real values are in the git-ignored secrets.local.yaml. Before any push:
  revoke that Google app password (or rewrite the commit).
- 11 Nov evening CALLS v2 (user: "Telegram + e-mail as before; a NORMAL phone call for EVERY fire / smoke, critical or not"):
  notify.call_allowed = any fire / smoke not verifying / suppressed / closed; channels_for ADDS "call" to the unchanged Telegram /
  e-mail rules (phone.call_every_fire) and strips it for everything else (phone.only_fire_smoke). phone.provider = "android,callmebot"
  (comma TEXT on purpose - the older running code could not read a YAML list): send_call tries each in order -> Android SIM call
  (adb; call.adb_device optional serial or Wi-Fi "ip:5555" -> adb connect) then CallMeBot Telegram voice call; the record says
  which one worked and why the others failed. call.to = phone numbers (both providers accept +91...). tools/phone_call_setup.py
  (status, --connect, --test with a yes-prompt). adb is NOT installed: unzip Google platform-tools into tools/platform-tools
  (git-ignored) - the download is in the scratchpad, running it from here was blocked. Tests [CALL] x6.
- 11 Nov evening ZENTRIX v2 (user: "open every panel / sub-panel / section, camera on/off, do everything we do by hand, only on
  command"): agent.INTEL (intelligence#sensors/ai/workers/alerts/predict/map/devices/reports/overview), BOXES + _box (control-room
  box + tab: "show acknowledged fire alerts", "show ppe history", "show zones and people"; op 'box', index handler scrolls + bumps
  + clicks the tab; zentrix.js HOME box -> /), _power ("turn off camera 2" -> confirm; "turn on the warehouse camera" -> at once;
  "all cameras"), siren ("silence the siren" / "stop the alarm"; "mute the alarm SOUND" stays the screen mute), notify test
  (admin, confirm). main._zx_execute kinds camera_power / silence / notify_test call the SAME endpoint functions (audit, broadcast).
  FIX: normalise() turned "acknowledged" into "acknowledge" -> "show acknowledged alerts" was read as an ACKNOWLEDGE command; past
  tense now kept, _action_of / _actions_all ignore "acknowledged". "worker" -> "workers" by difflib (regex accepts both).
  Tests [ZX] +4 (127 PASS when the live system is not running; [12] GPU batch / [CLIP] are timing tests that can fail while the
  real NETRAX uses the GPU).
- 11 Nov night: SETUP_PHONE_CALL.bat (project root, one double-click): downloads platform-tools into tools/ (curl.exe + tar; downloads
  are blocked for Claude), asks the supervisor's number -> tools.phone_call_setup --to (writes only call.to in secrets.local.yaml;
  10-digit Indian numbers -> +91...), checks the USB phone (data/phone_call_setup.log), offers one real test call. Not run yet.
- 11 Nov 18:10 TEAM DECISION: phone call = TWILIO again. config/notify.yaml phone.provider: twilio (was android,callmebot; backup
  _to_delete/before_twilio_11nov/). Who is called = twilio.to_numbers in secrets.local.yaml (1 number set). Rules unchanged: a call for
  every fire / smoke, never for PPE / access; SMS channel still off. Trial account -> Twilio's template voice, verified numbers only.
  SETUP_PHONE_CALL.bat / call.to are only for the android / callmebot providers (not needed now).
- 11 Nov night (user: "cameras got closed while running; test the fire call; latency / smooth video"). MEASURED causes:
  * cudnn.benchmark tunes every NEW (picture shape, batch size) on first use: ~3 s each on battery -> first ~16 s every camera
    timed out, tiles DEGRADED / OFFLINE. engine_proc._warm_shapes tunes [[720,1280],[640,480],[480,640]] x batch 1-2 with a real
    snapshot BEFORE 'ready' (settings processing.warm_shapes, [] = off; ~18 s extra start on battery). Startup timeouts: 0.
  * A timed-out request stayed queued while the camera sent another (stale-frame pile-up + shared picture overwritten while read):
    ProcessEngine.inflight = one request per camera; a camera waits for its late answer instead of queuing more.
  * USB camera reopened after a black picture failed ("can't be used to capture by index") and the doubling wait reached 30 s:
    stream waits 1 s after release, retries every <= 3 s for 10 tries, then slows down.
  * run_edge._single_instance (127.0.0.1:48765): a second edge (run_all.bat twice) stops at once (two copies stole the cameras);
    SENTINELEYE_EDGE_MULTI=1 allows several. Tests [12] x2 (final_selftest 129 PASS).
  * ON BATTERY the laptop GPU / CPU are throttled: AI batch 140-180 ms, 2.5-3.8 checks/s per camera; ON CHARGER 68 ms, 5.4-10.5/s.
    Keep the charger in for demos. Fire call path checked (no call made): any open fire / smoke -> telegram + call + email.
- 11 Nov late night PPE ACCURACY + ALERT LOGIC (user: "vest seen on other cams but not at the entrance; one alert per violation").
  MEASURED on own labelled frames (scratchpad decide_eval / reject_eval, live detector + association):
  * Vest colour check threw away 301/406 REAL vests (the team's fluorescent vest is washed out to saturation 74-85 on the USB C270;
    entrance snapshots showed 'vest? rejected: not hi-vis' at conf 0.95). validate.hivis_share also counts pale yellow / lime
    (hue 24-48, S > vest_pale_s_min 50, V > vest_pale_v_min 130), vest_hivis_min 0.25 -> 0.20. Per-frame vest false alarms 283 -> 16
    (not-worn caught 211 -> 203; the 14 'missed' frames are crowd / far scenes, checked: no shirt accepted as a vest).
  * Size: helmet max width 0.60 -> 0.95 (slim standing people; close-up max 0.75), vest max height 0.62 -> 0.85. Helmet false alarms 36 -> 23.
  * association: box top within 2.5 % of the picture (top_cut_frac) -> helmet / goggles / mask 'unverifiable' UNLESS the bare head /
    face is seen (HEAD_EVIDENCE) - entrance close-up with the head above the picture said 'NO HELMET'. Caught counts unchanged.
  * Entrance (edge/entrance.py): same person under a new tracker id (same place <= same_person_gap_s 3 s) or the same registered worker
    within same_person_quiet_s 30 s inherits what was announced -> no repeated identical popup (live: 12 decisions in 2 min);
    head cut at the top for most frames -> manual 'too close to the camera - step back so head to feet are visible'.
  * Backend main._ppe_merge (settings incidents.merge_same_person): ONE active ppe_missing alert per person (W-id; V-id only within one
    edge run) per camera; another edge alert of that person = update (new items -> one 'more PPE missing' message) and the alert resolves
    only when every merged edge alert ended (PPE_SOURCES). Live 20:21-20:23: one worker had 5 open alerts on cam_01.
    Edge health has edge_started; on a new edge run main.close_orphaned_ppe resolves older open PPE alerts of its cameras (end_reason
    edge_restarted). Tests: final_selftest [PPE] x6 + [ENT] x2 (138 PASS); older tests run with merge off (they reuse W-001).
  * final_selftest now always uses a scratch camera power file (data/camera_power.json had cam_02 OFF from the dashboard, 20:20 -
    the Warehouse phone is switched OFF there; turn it on from its tile).
  * Not changed: models (no retraining), timings (helmet off -> alert ~1.4-2.2 s high-risk), goggles false alarms (39 frames = model
    itself, no validation) - remove goggles from a zone's required list if they disturb the demo.
- 11 Nov night SHARED WI-FI + TEAM'S NEW STATION SKETCH (user: "everybody on friend's Wi-Fi NOTHING; hardware data on the dashboard;
  teammates open the project from their laptops"). Backend already listens on 0.0.0.0:8000; login works over plain http (cookie not Secure).
  * tools/lan_setup.py: Wi-Fi name + laptop IPv4 + network type, links to share (/, /intelligence, /hardware, /entrance, /admin), ESP32
    URLs, firewall check (Python allowed on Public only - a Private network would block teammates; prints the netsh fix), backend
    reachable; --write sets SECRET_SERVER (firmware/rfid_direct/secrets.h) + SECRET_NETRAX_HOST (firmware/netrax_station_team/secrets.h).
    run_all.bat prints the share link (--brief) and pauses.
  * firmware/netrax_station_team: merged the team's NEO-6M GPS version - TinyGPSPlus on UART2 (GPIO16/17, 9600, rx buffer 2048),
    POST /api/location/hardware every 10 s with a fix (accuracy ~ HDOP x 5 m, rough); buzzer ON for WARNING or CRITICAL (team's rule),
    relay CRITICAL only, blinking LEDs; vibration_metric = |total accel - 1 g| (mpu6050_available sent); laptop address from
    secrets.h SECRET_NETRAX_HOST. Not compiled here (no Arduino toolchain on this laptop). Backup: _to_delete/before_gps_station_11nov/.
    Verified on a scratch backend: telemetry + GPS accepted without login, vibration stored, welding-bay location = hardware GPS.
  * The RFID sketch the friend sent = the old team version; firmware/rfid_direct (adapted 12 Oct) is the one to flash.
  * Laptop IP on NOTHING unknown until it joins (on OPPO hotspot 11 Nov: 10.27.101.235). After joining: lan_setup --write, flash both
    boards, tools.find_phones --write for the phone cameras.
- 12 Nov: ALL devices on the friend's Wi-Fi NOTHING. Laptop 10.248.34.235 (dashboard http://10.248.34.235:8000), warehouse phone
  10.248.34.173. PHONE ADDRESS FOLLOW: edge/phones.py - a phone camera (http://...:8080) unreachable twice -> subnet scan for IP
  Webcam (/shot.jpg JPEG, ~1.3 s measured), takes an address no other camera uses, switches + rewrites only that camera's source:
  line in cameras.yaml (one scan per camera per 20 s). final_selftest [CAM] phone_follow_test (139 PASS).
  Final sketches for the friend (git-ignored, Wi-Fi password inline): firmware/friend_final/rfid_reader_final (his code, only
  DASHBOARD_URL -> /api/rfid) and firmware/friend_final/safety_station_final (his code + lines marked [NETRAX]: Wi-Fi, telemetry
  every 1 s, GPS every 10 s, vibration_metric). His RFID code skips unknown cards (only 3 known cards reach the dashboard). Not compiled here.
- 12 Nov PM2.5 + GPS ON THE DASHBOARDS (user: "PM2.5 works on the Serial Monitor but never reached the dashboard; GPS instead of the
  thermistor; live map"). backend/hardware.py: FIELDS + pm_adc, pm_sensor_v, pm_estimated, pm_zero_v; Hub._pm turns the Sharp GP2Y1010
  voltage into pm2_5_ugm3 = (V - zero) / 0.005 (datasheet slope 0.5 V per 100 ug/m3), zero = lowest voltage of the last 30 min
  (self-zeroing; default 0.6 V for the first 30 readings). pm_estimated=True -> NEVER a warning / smoke hazard (evaluate ignores it;
  dashboards show no limit, "estimated · not calibrated"); a real PM sensor sending pm2_5_ugm3 alarms as before.
  Thermistor removed (not fitted). hardware.html: GPS card (LIVE <= 60 s / LAST KNOWN / NO FIX, click -> map) + "Live location" panel
  (OpenStreetMap embed iframe, moved only when the position changes > ~10 m, coordinates / accuracy / Google Maps / OSM / copy;
  /api/location every 10 s + ws 'location'). intelligence.html: GPS card in Live Sensors, NEO-6M row in Device Health, persistent
  "Live location" box under the Factory Map (built once: the section re-renders every second). Map tiles need internet; coordinates
  work offline. Tests final_selftest [HW] x3 (142 PASS), hw_selftest ok; headless Edge on a scratch backend: 0 JS errors.
- 12 Nov JURY FEEDBACK ("detect every PPE correctly + smoothly: safety vs normal goggles, jacket vs vest, helmet vs cap, safety vs
  normal shoes"). tools/ppe_scene_eval.py = per-scene OK / FALSE alarm / MISSED / unsure for helmet, vest, gloves, goggles, boots on
  datasets/own (live pipeline, frames in order). MEASURED before -> after:
  * CAP vs HELMET: main model called caps 'helmet' (up to 0.8); v3B never did (0/35) and confirms real helmets in 67 % of frames
    (near people 56-100 %, far ~22 %). detector._confirm_helmets gives each helmet 'confirm' (+ v3-only helmets >= 0.5);
    association._helmet_check per-person memory (worker.helmet_mem, only when has_parts): confirmed within helmet_confirm_keep_s 30
    -> compliant; first helmet_confirm_wait_s 1.0 -> unverifiable; then unconfirmed = cap = missing; only for people >=
    helmet_confirm_min_height 0.35 of the picture. Cap scene ppe_08: 23/32 missed -> 0/32; ALL helmet OK/FALSE/MISSED 464/23/34 ->
    487/19/16. Test final_selftest cap_vs_helmet_test.
  * Goggles: v3B confidence overlaps worn / not-worn (thr 0.45: 77 % worn seen, 7 % bare faces wrongly seen; dim scene bare faces up to
    0.69) -> no threshold fixes it; needs more training data (spectacles vs safety goggles) = retraining (ASK the user first).
  * Jacket vs vest: vest needs hi-vis colour (>= 20 %): dark / blue / grey jackets rejected, mustard T-shirts rejected (32/32); a
    hi-vis jacket passes (it is PPE). No jacket footage -> not measured.
  * Shoes: safety vs normal 64/1 already (shoe_cls).
  * Speed: live new-person PPE alarms took 4-6 s (median) - mostly detection flicker (+ first look). first_look_s high 2.0 -> 1.6,
    medium 2.5 -> 2.0, low 3.0 -> 2.5 (backup _to_delete/settings_before_fast_12nov.yaml); 1.2 s broke 'one complete alarm with the
    right name' -> rejected. Simulated: high 2.0 -> 1.6 s at 8 AI checks/s. state_machine.operator_closed now also clears the person's
    item history (truly fresh look; with old 'missing' frames a worker who had put the PPE on got a new alarm).
  * TensorRT: not installed / never measured -> not switched.
  All suites pass (edge.selftest, final 143, access, hw).
- 12 Nov evening SAFETY GOGGLES vs NORMAL GLASSES (user: "normal glasses are detected as industrial goggles - do whatever you need").
  v3B 'goggles' cannot tell them apart: MEASURED with public eyeglasses photos (HF giqu/glasses_celeb) pasted at CCTV size onto our
  workers' faces in our own frames, live pipeline: 60 of 186 never-seen spectacles faces judged 'goggles worn'. Our own footage has
  NO clear prescription spectacles (only the team's clear safety glasses, sunglasses in ppe_08/09/19, bare faces).
  * edge/eyewear.py: second stage on every goggles box (like shoe_cls): square eye crop (eye_crop, 2.2 x box width) -> CLIP ViT-B-32
    (open_clip, models/clip_vit_b32_laion2b.safetensors 605 MB, git-ignored, `python -m tools.eyewear fetch`) image embedding + zero-shot
    text directions -> logistic head models/eyewear_head.npz -> p(safety eyewear). p >= eyewear_hi 0.5 goggles | lo 0.2..0.5 'unclear'
    (validate reason 'unclear' -> association `unsure` -> goggles UNVERIFIABLE, no alarm) | < lo 'normal glasses' (rejected; face seen ->
    goggles MISSING). Runs in the AI process, one CLIP call per batch, max 6 crops (12 ms per crop on the GTX 1650, only when goggles are
    seen). Live picture / snapshot: red "NORMAL GLASSES - not safety goggles". settings models.eyewear_* (eyewear_head: '' = off).
  * Zero-shot CLIP alone REJECTED: it threw away 25/26 of the team's real clear safety glasses on the overhead camera (ppe_20) -> head
    trained on own goggles + own sunglasses/bare + pasted public spectacles (+ pasted own faces so 'pasted' is no cue; pasted own BARE faces
    left out: they taught the head to reject blurry real goggles). ViT-B-16 tested: same accuracy, 4x slower -> B-32.
  * MEASURED, whole scenes / identities held out: own safety goggles 148 kept / 3 unclear / 0 'normal glasses' (of 151); spectacles the
    detector called goggles -> normal glasses 150/150; sunglasses 16/19 (3 unclear). END-TO-END (186 never-seen spectacles faces pasted onto
    our workers, live detector + association): accepted as goggles 60 -> 1, NO GOGGLES 120 -> 158, unverifiable 6 -> 27. ppe_scene_eval
    goggles OK/FALSE/MISSED/unsure 363/39/18/43 -> 374/41/4/44 (sunglasses scenes 08/09 missed 14 -> 0; +2 'false' = sunglasses labelled
    goggles in ppe_19); every other item identical. CAVEAT: spectacles test = pasted public photos, not real camera footage.
  * tools/eyewear.py: fetch | check <image|folder|video> (verdict per goggles box, pictures in data/eyewear_check/) | train [--neg video
    of the team in NORMAL spectacles] [--pos video of other safety goggles] (rebuilds the head, prints held-out table, old head -> .bak).
    Best next improvement: record 30 s per person in their own spectacles + jury-style strap goggles, then `train --neg ... --pos ...`.
  * ppe_scene_eval EYEWEAR=0 = without the check. requirements: open_clip_torch. Tests final_selftest [PPE] eyewear x3 (146 total; [12]
    batch + [CLIP] x2 failed only because the live NETRAX was running - FakeDet / encoder timing). edge, access, hw selftests pass.

## 12-13 Nov 2026: POLISH (team "NETRAX Claude Code prompt pack", prompts 0-11). Backups: _to_delete/before_polish_12nov/ (+ shots/)
- DESIGN RULES (keep on every page): one system in dashboard/nx.css (bottom "12 Nov POLISH" section). Type tokens --fs-xs 13 (time stamps
  only) / --fs-s 14 / --fs-m 16 body / --fs-l 19 panel titles / --fs-xl 24 / --fs-num 34; NOTHING below 13 px (tools/font_tokens.py
  converts any px font size to a token, re-run after edits). Space --sp-1..5 = 4/8/12/16/24, radius 12. Colour = status only: --ok green
  compliant, --crit red violation / fire, --warn amber unauthorised / warning, --unk grey cannot see / offline. Components: .nx-panel >
  .nx-panel-head (icon + title + count chip + max 2 buttons, NO subtitle; explanations = (i) .nx-info tooltip) > .nx-panel-body (scrolls
  inside); .nx-grid-2/3/4 (1 column < 1100 px); .btn 36 px (.btn-primary / -ok / -danger / -ghost); .nx-kpi (big number + one word);
  .nx-card (the alert card); .nx-tabs. Shared header .nxbar on every page (logo · NETRAX + page · clock · nav · Zentrix · theme · user);
  every page honours ?theme=dark|light. Bump ?v=12nov1 on /static links when JS/CSS change.
- ICONS + PERSON: dashboard/ppe_icons.js ppeIcon(item, ok|miss|unk) / ppeRow / ppeRowFor(event) (original flat SVGs: green worn, red
  prohibition = missing, grey + ? = cannot see; order helmet vest gloves goggles mask boots). dashboard/people.js personLabel(e):
  "Name · W-004" / "Unregistered person #17" / "Name · W-002" + amber UNAUTHORISED; W-*** stays "Registered worker". NO "NO HELMET" /
  "Missing PPE:" text anywhere on the dashboards (camera badges show icons too). Video overlay (annotate.py) unchanged = AI view.
- PAGES: control room = 4 KPI tiles (Workers in view / Compliant = edge counts worker.people -> health -> cameras_payload "people",
  live cameras only, "–" when the AI is off; Open violations; Fire / smoke), cameras 2/3 (tiles stretch to the row), ONE Alerts panel 1/3
  (green "Fire / smoke: clear" line, big fire cards only during a fire, Active / Acknowledged / History, icon cards with Acknowledge +
  Resolve, details drawer = snapshot, clip, all actions incl. Escalate / False alarm, messages, audit), bottom row Entrance · Zones ·
  Safety station · System health (overall state + location moved there). Entrance: PERMITTED / NOT PERMITTED result block + icons, Required
  PPE per zone as icons, log with icons. Hardware: station strip + 3 groups Environment / Safety / People & location, equal sensor tiles.
  Admin: 6 tabs (Overview · Incidents & reports · Workers · Alerts & delivery · Site setup · System; old #hashes still work, last tab
  remembered). Intelligence: sections as top tabs, subtitles -> tooltips, status colours only. ROI + login: same style.
- DATA (Prompt 5): edge state_machine._item_view -> every PPE action carries items_ok / items_uncertain / items_required (display only;
  decision unchanged). events columns items_ok / items_uncertain / items_required (ALTER TABLE; old rows = None = unknown). A W- id always
  gets its name from the workers table (main._fill_name, db._worker_name). notify.who_text / ppe_line: same identity rule in messages.
- ONE ALERT PER VIOLATION (Prompt 6): main._same_violation key = worker ID + type + ZONE (unidentified: same camera + zone + box nearby);
  active alert -> update; ended (left view / PPE on / AI restart / supervisor resolve / false alarm) < notify.yaml one_alert.regrace_s 120 s
  ago -> the SAME alert is RE-OPENED (dismissed ones stay quiet); fire / smoke never merged; access re-entry too (ACCESS_REOPEN). Update
  messages ("more PPE missing", "still missing after resolve") -> Telegram only. MEASURED replay of all 967 recorded PPE alerts: 967 -> 338
  alerts, Telegram 967 -> 471, e-mail 527 -> 255 (scratchpad replay_alerts.py). Supervisor resolve + still violating within 120 s = same
  alert back (spoken once + one Telegram), after 120 s = new alert (replaces the 29 Oct "new alert at once" behaviour; tests updated).
- VOICE (Prompt 7): main.ppe_text / access_text fixed sentences, name first, icon order: "Attention Ritika, worker W 0 0 4. Helmet and gloves
  missing. Please wear them now." / "Attention, unregistered person in the Welding bay. Helmet missing. Please wear it now. Report to the
  supervisor." / "Ritika, gloves also missing." / "Attention Sourav, worker W 0 0 2. You are not authorised in the Control room. Please
  leave now." Browser speech (zentrix.js) only reads Zentrix replies, never alerts.
- FIRE CALL (Prompt 8): main.fire_call_tick (in fire_level_loop, 1 s): first call with the alert (camera fire in a zone with an ONLINE station
  still waits for the station check - project rule), then every phone.repeat_every_s 60 while open / escalated, failed / busy / no answer /
  network -> phone.retry_after_failed_s 20, max_calls 10, each attempt its own row ("call #n"); Acknowledge / Resolve stops at once
  (Dispatcher._call_still_wanted skips queued calls). Call text "NETRAX fire alarm. Fire detected in <zone> by <camera> at <time>. Open the
  dashboard and acknowledge." (read twice).
- DELIVERY (Prompt 9): e-mail only for notify.yaml email.send_now_for (fire, smoke, Critical / High PPE, unauthorised) + escalations;
  everything else -> status 'digest' -> notify.send_digest one e-mail every 15 min (health_loop); email.daily_max 300 -> PPE e-mail 'skipped',
  fire still sent, health says so. No internet (notify.is_network) = status 'waiting', NOT a failed try: resend after offline.waits_s
  5/15/30/60 s, in order, up to 2 h then 'expired'. notify.requeue_unfinished() at start-up resends rows left queued / sending / waiting.
  SMTP: one reused connection (_smtp_conn), 20 s timeout, one retry on a dropped connection, Gmail limit recognised. Telegram: photo + short
  caption (person, "Helmet ❌ · Vest ✅ · Goggles ❔", place, action, link); updates are REPLIES to the first message. Admin delivery log:
  plain-word reasons, per channel today sent / waiting / failed.
- SUPERVISOR (Prompt 10): unacknowledged cards blink red, acknowledged steady + "Ack by <user> <time>"; Resolve asks a one-tap reason (note);
  viewer sees no buttons (API refuses anyway). Telegram buttons ✅ Acknowledge / ✔ Resolve on the first message (notify.telegram.buttons):
  notify._tg_poll (getUpdates, no public URL needed) -> handle_tg_callback (only the configured group chat) -> main._telegram_action ->
  the same _act (audit user "telegram:<name>"); stale presses change nothing.
- TESTS: final_selftest 163 PASS (new: [PPE] polish data x6 incl. browser test of people.js / ppe_icons.js via Edge --dump-dom, [CALL]
  repeat x4, [NOTIFY] x7), access_selftest (voice sentences x4, re-open), edge.selftest, hw_selftest pass. UI audited in headless Edge
  (1920 / 1366 / 390 px, day + night): 0 JS errors, no sideways scroll, no "NO <ITEM>" text. Before / after shots: docs/polish_12nov/.
- NOT done (needs people / hardware): the 5-minute live run with 3 cameras (helmet off -> 1 card + 1 Telegram + 1 voice; test fire -> call
  repeats, stops on Acknowledge) and the real Wi-Fi-off test - simulated in the tests above, still to be done live.
- REMINDER: headless Edge left running from a test keeps its port and its page cache -> later checks see OLD files. Kill test browsers
  (taskkill /T) before measuring.
