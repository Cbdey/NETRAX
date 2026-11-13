# NETRAX final acceptance self-test

Run: 13 Nov 2026, 11:35:05 IST - `python -m tools.final_selftest`

Providers (Telegram / Twilio / SMTP) are replaced by fakes that answer like the real services; the live-provider and camera tests are in docs/NETRAX_Final_Test_Guide.pdf.

| Test | Check | Result | Evidence |
|---|---|---|---|
| 01 | complete PPE -> ACCESS APPROVED after >= 6 frames / 1 s (never on one frame) | PASS | 8 frames, 1050 ms |
| 01 | approval recorded (entrance_checks + event history, gate stays closed without controller) | PASS |  |
| 02 | vest missing -> ACCESS DENIED naming the missing item | PASS |  |
| 02 | denial recorded with required items, evidence and missing item | PASS |  |
| 03 | feet not visible -> 'Verification failed - manual check required', no approval | PASS |  |
| 03 | two people overlapping in front of the camera -> manual check, never auto-approved | PASS |  |
| 03 | flickering helmet evidence (50/50) -> manual check, not approved | PASS |  |
| 04 | serious PPE violation stored open with items, camera, zone, time, worker | PASS |  |
| 04 | dashboard-wide state rule: High/Critical + open -> emergency severities from settings | PASS |  |
| 11 | two simultaneous violations kept as two separate active incidents | PASS |  |
| 04 | acknowledged != resolved: ack keeps the incident (status acknowledged, acked_by recorded) | PASS |  |
| 04 | resolve closes it with who/when (traceable) | PASS |  |
| 11 | an update changes only its own incident | PASS |  |
| 05 | fire event has type, timestamp, camera, zone, status, level for the big panel | PASS | level 1, status open |
| 06 | small flame, no other evidence -> Level 1 WARNING (severity High, voice warning, no siren) | PASS |  |
| 06 | flame grows (x1.4, 4 % of picture) -> raised to Level 2 CRITICAL + siren | PASS | flame covers 4.3% of the picture; growing (x1.4) |
| 06 | flame looks smaller in a later frame -> level is NOT downgraded | PASS |  |
| 06 | large / doubling fire -> Level 3 VERY CRITICAL (evacuate) | PASS |  |
| 06 | tiny visible flame but station flame + 64 C -> Level 3 (danger evidence beats flame size) | PASS | flame covers 0.0% of the picture; safety station confirms: flame sensor active; temperature 64.0 C >= 50; station: flame with high heat / heavy smoke |
| 06 | each level raise re-notifies immediately (no cooldown for fire) | PASS |  |
| 07 | Telegram message reaches the (fake) group and the outcome is logged as delivered | PASS |  |
| 07 | message has incident type, level, zone, camera, time, action and event ID | PASS |  |
| 08 | phone call placed via Twilio, followed until Twilio reports 'completed' -> delivered | PASS | delivered/completed, delivered/completed |
| 08 | Twilio TRIAL account (custom text refused) -> falls back to Twilio's template call + SMS, labelled TRIAL | PASS | queued / TRIAL: Twilio template voice, not the alert text / queued / TRIAL: Twilio template text, not the alert text |
| 08 | phone numbers typed without '+' are fixed (916372863454 -> +916372863454) | PASS |  |
| 09 | SMS: first try fails (HTTP 503) -> retried -> delivered, attempts logged | PASS | attempts 2 |
| 09 | SMS text has type, severity, location, time and recommended action | PASS |  |
| 09 | e-mail: SMTP drop -> retried -> accepted by the mail server ('sent', never claimed 'delivered') | PASS |  |
| 13 | internet down: the alert is stored + shown at once (sending never blocks detection) | PASS | ingest 19 ms |
| 13 | internet down: Telegram message kept and WAITING for the internet (reason shown, not a failed try) - never 'delivered' | PASS | ('waiting', 0, 'network: ConnectionError') |
| 13 | system health shows the channel waiting for the internet | PASS |  |
| 13 | no credentials -> NOT CONFIGURED (clearly labelled), nothing claimed as sent | PASS | [('telegram', 'not_configured')] |
| 13 | health lists unconfigured channels as warnings | PASS |  |
| 05 | same continuous incident is not sent twice on a channel | PASS |  |
| 10 | 'What happened yesterday at 4:00 PM?' -> the saved record at 16:02 yesterday, nothing else | PASS | Between 12 Nov 2026, 15:30:00 IST to 16:30:00 IST: 1 incident - 1 ppe violation. |
| 10 | no record in the window -> says 'No matching record was found' (no invented events) | PASS |  |
| 10 | type filter (smoke) + whole day | PASS |  |
| 10 | viewer role: worker names hidden in the answer | PASS |  |
| 10 | every question logged | PASS |  |
| 12 | latency measured as median + 95th percentile (event e2e, time to alert, notification delivery) | PASS | {"n": 5, "p50": 0.0, "p95": 0.0, "max": 0.0, "mean": 0.0} |
| 14 | no GPS + no configured site -> 'Location unavailable' + zone label (never invented) | PASS |  |
| 14 | valid device GPS -> shown with source, accuracy and map link | PASS |  |
| 14 | 1-hour-old reading -> marked STALE and not used | PASS |  |
| 14 | 0,0 / invalid coordinates rejected | PASS |  |
| BUILD | backend, edge and tools all compile | PASS |  |
| BUILD | find_phones: sets the 2nd phone and switches its camera on, the first phone is untouched | PASS |  |
| ENT | denied person keeps standing there -> ONE decision (one popup / call / message), not one every 3 s | PASS | 1 decisions in 30 s |
| ENT | ... puts the vest on -> a NEW decision: approved | PASS |  |
| ENT | face still being matched -> waits for the name instead of saying 'unregistered' | PASS |  |
| ENT | ... registered face found -> decision carries W-077 Chinmaya | PASS |  |
| ENT | visitor id is not a worker -> identity 'unregistered' | PASS |  |
| ENT | registered + all PPE -> 'W-077 Chinmaya verified - all required PPE worn. You may enter.' (popup + voice) | PASS | W-077 Chinmaya verified - all required PPE worn. You may enter. |
| ENT | registered + vest missing -> names the worker and the item, 'Entry not permitted' | PASS | W-077 Chinmaya - safety vest missing. Entry not permitted. |
| ENT | unregistered + all PPE -> 'Unregistered worker - PPE verified. You may enter.' | PASS | Unregistered worker - PPE verified. You may enter. |
| ENT | unregistered + helmet missing -> 'Unregistered worker - ... Entry not permitted.' | PASS | Unregistered worker - helmet missing. Entry not permitted. |
| ENT | viewer copy of the verdict hides the worker's name (event + entrance payload) | PASS |  |
| ENT | a card tapped right after ANOTHER person's face-checked decision does not rename that person | PASS |  |
| ENT | card tapped, face not recognised -> identified by RFID | PASS | W-077 Chinmaya verified - all required PPE worn. You may enter. |
| ENT | tap + approved -> counted inside the site | PASS |  |
| ENT | tap out -> no longer inside; names nobody at the check | PASS |  |
| ENT | tap + PPE missing -> entry refused AND not counted as inside | PASS |  |
| ENT | same person puts the vest on -> approved with the same card, counted inside again | PASS |  |
| ENT | camera faster than the hand: card tapped just AFTER the decision -> 'Unregistered' corrected to the card's owner | PASS | W-077 Chinmaya verified - all required PPE worn. You may enter. |
| ENT | somebody else's card (face W-077, card W-078) -> NEVER auto-approved: manual check with the reason | PASS | card belongs to W-078 Bibhu but the face matches W-077 - check the person |
| AUTH | no login -> API refuses (401) and the page redirects to /login | PASS |  |
| AUTH | edge / ESP32 posts work without login (devices) | PASS |  |
| AUTH | but actions on events need a login (POST /api/events/<id>/ack -> 401) | PASS |  |
| AUTH | viewer: worker names and snapshots hidden | PASS |  |
| AUTH | viewer cannot acknowledge (403) and the refusal is audited | PASS | 403 {"detail":"your role (viewer) cannot do this - needs supervisor"} |
| AUTH | 5 wrong passwords -> locked for a minute | PASS |  |
| AUTH | supervisor acknowledges; audit log names the user | PASS |  |
| AUTH | supervisor cannot register workers (admin only) | PASS |  |
| AUTH | assistant over HTTP returns the record with a link | PASS |  |
| 03 | manual check result recorded (who, decision) and audited | PASS |  |
| AUTH | health endpoint lists components | PASS |  |
| CLIP | incident clip = ~4 s before + ~4 s after the alert, H.264 MP4 (plays in every browser), 640 px | PASS | 49 frames (640, 360) h264 |
| CLIP | edge uploads the clip without login (device path) -> linked to the SAME event id | PASS | {"ok":true,"clip":"/clips/evt_t09.mp4"} |
| CLIP | no login -> clip file refused | PASS |  |
| CLIP | viewer role: clip hidden (no link, file 403) | PASS |  |
| CLIP | supervisor: dashboard record has the clip and it downloads (video/mp4) | PASS |  |
| CLIP | retention: clips older than snapshot_retention_days are deleted like snapshots | PASS |  |
| 12 | live video: pictures are pushed over one WebSocket with camera + capture time; a viewer gets only its cameras | PASS | ['cam_02/1794549860.139/1794549860.332', 'cam_02/1794549860.292/1794549860.333'] |
| REP | report generator: time range -> summary (totals, most missed PPE, by zone / camera / worker) + records | PASS | ['Period: 06 Nov 2026 11:35 to 13 Nov 2026 11:35 (IST) - 20 record(s).', 'By type: Entrance check 12, PPE violation 6, Smoke 1, Fire 1.'] |
| REP | filters work (PPE violations in the welding bay only) | PASS |  |
| REP | PDF and CSV downloads (named by the time range); end before start refused | PASS |  |
| ZX | commands open pages and sections (wake word + misspellings: 'hey zentrix open the admin pannel') | PASS | [{'op': 'navigate', 'url': '/admin'}, None] |
| ZX | camera commands: enlarge by zone or number, back to all cameras; a PPE question is not a camera command | PASS | [{'op': 'focus', 'cam': 'cam_01'}, {'op': 'focus', 'cam': 'cam_02'}] |
| ZX | analyse = live verification: violation still on camera -> acknowledge; no longer seen -> resolve; NOTHING changes without a command | PASS | STILL HAPPENING: Chinmaya without helmet, safety vest / No longer seen - looks finished / I checked 7 active alerts. 4 still need attention, 2 look finished and can be resolved, 1 cannot be verified because the |
| ZX | 'apply' carries out the recommendations through the normal actions (audited: user + 'via Zentrix') | PASS | Done: 3 acknowledged, 2 resolved. |
| ZX | a single clear command runs at once ('acknowlege the latest alert' -> newest alert acknowledged) | PASS | Acknowledged: Missing safety vest - Chinmaya. |
| ZX | bulk resolve asks for confirmation first; 'cancel' changes nothing; 'confirm' resolves them all | PASS | Please confirm: Resolve 7 alerts: Missing safety vest - Chinmaya; Missing helmet - Chinmay / Done: 7 resolved. |
| ZX | 'analyse ... and acknowledge them' = analysis + the commanded action on exactly those alerts | PASS | I checked 1 active alert. 1 still need attention. Acknowledged: Missing helmet - Chinmaya. |
| ZX | 'analyse the recent alerts and acknowledge OR resolve' (user's words, misspelt) -> each alert gets the verified choice: still on camera = acknowledged, no longer seen = resolved | PASS | I checked 3 active alerts. 2 still need attention, 1 look finished and can be resolved. 1 alert left for you to decide (cannot be verified). Done as verified: a |
| ZX | questions about the past are answered from the records (grounded history assistant) | PASS | Between 13 Nov 2026, 00:00:00 IST to 11:34:21 IST: 1 incident - 1 fire (level 3). |
| ZX | a viewer can ask and move around but cannot change records | PASS | Your role (viewer) cannot resolve alerts - ask a supervisor or admin. |
| ZX | entrance commands: allow / refuse the person at the entrance | PASS | ['approved', 'denied', None] |
| 12 | camera health: a black picture (covered lens / stuck USB camera, seen live 5 Nov) is reported as such | PASS |  |
| ZX | opens Safety Intelligence sections by name (live sensors, factory map, alerts centre, device health, predictive) | PASS | [[{'op': 'navigate', 'url': '/intelligence#sensors'}], [{'op': 'navigate', 'url': '/intelligence#workers'}]] |
| ZX | control-room boxes + tabs ('show acknowledged fire alerts', 'show ppe history', 'show zones and people'); 'show ACKNOWLEDGED alerts' only opens the list - it never acknowledges anything | PASS | [{'op': 'box', 'box': 'fire', 'tab': 'ack'}, {'op': 'box', 'box': 'ppe', 'tab': 'ack'}] |
| ZX | camera OFF by command asks first and switches only after 'confirm'; ON runs at once (same path + audit as the button) | PASS | ['Please confirm: Turn OFF Welding bay (cam_01) - no picture, ', 'on', 'off', 'on'] |
| ZX | siren silenced by command; a real test call needs confirmation first | PASS | ['Silencing the site siren. The alert itself stays active until it is resolved.', "Please confirm: Send a REAL test phone call. Say 'confirm' o"] |
| INT | safety intelligence page is served, never cached, and has all nine sections | PASS | no-cache, must-revalidate |
| INT | the page uses only existing endpoints (every one answers) and changes data only through the normal alert actions and the camera on/off switch | PASS | {'/api/analytics/summary': 200, '/api/attendance': 200, '/api/attendance/export': 200, '/api/cameras': 200, '/api/events': 200, '/api/health': 200, '/api/hw/state': 200, '/api/location': 200, '/api/me': 200, '/api/workers': 200, '/api/zones': 200} |
| INT | Zentrix opens it by name, and the older page commands still go where they did | PASS | [{'op': 'navigate', 'url': '/intelligence'}, {'op': 'navigate', 'url': '/intelligence#map'}] |
| CAM | camera turned OFF on the dashboard: shown OFF with no alarm badge, the edge releases it within 1 s; ON again brings it back | PASS | ['OFF', True, True, ['camera_on', 'camera_off']] |
| CAM | only a supervisor / admin may switch cameras (viewer refused by the role check) | PASS |  |
| CALL | EVERY fire / smoke rings the supervisor (Level 2 camera fire, Level 1 smoke, station-only fire); never PPE, never a fire still being checked with the station; Telegram + e-mail unchanged | PASS | [['telegram', 'call', 'email'], ['telegram', 'sms', 'email']] |
| CALL | normal SIM call (Android) is tried first; phone missing -> CallMeBot Telegram call, and the record says why | PASS | CallMeBot call placed / before: android: no Android phone connected |
| CALL | the spoken call text fits CallMeBot (<= 256 characters) and says what, where and that it came from the station | PASS | NETRAX fire alarm. Fire detected in Warehouse by safety station sensors at 13 Nov 2026, 11:34:21 IST. Open the dashboard and acknowledge. |
| CALL | CallMeBot: accepted -> 'sent' (never claimed 'delivered'); not authorised -> FAILED with the /start hint; server down -> retried | PASS | ['sent', 'CallMeBot refused: You need to authorize CallMeBot to contact you - the person must send /start to @CallMeBot_txtbot once', 'failed', 'en-IN-Standard-A'] |
| CALL | Android phone: dials tel:+91... over adb and hangs up after ring_s; a Telegram name is refused for a SIM call | PASS | ['Android phone dialling +919800000000 (hangs up after 40 s)', [['get-state'], ['shell', 'am', 'start', '-a', 'android.intent.action.CALL', '-d', 'tel:+919800000000']]] |
| CALL | one call per fire: a quick level change does not ring again; Level 3 (evacuate) calls again | PASS | [1, 1, 2] |
| CALL | fire detected -> the phone call is queued within 5 s (no waiting for a level or an escalation timer) | PASS | ['open', [('not_configured', 0.04)]] |
| CALL | not acknowledged -> called again every 60 s; busy / no answer / failed -> next try after 20 s (own log row) | PASS | [1, 1, 2, 2, 3] |
| CALL | Acknowledge stops the calls at once (no new call, a call still in the queue is not placed) | PASS | [3, False] |
| CALL | call text: 'NETRAX fire alarm. Fire detected in <zone> by <camera> at <time>. Open the dashboard and acknowledge.' | PASS | NETRAX fire alarm. Fire detected in Warehouse by camera cam_02 at 13 Nov 2026, 11:34:21 IST. Open the dashboard and acknowledge. |
| NOTIFY | Wi-Fi off: Telegram + e-mail WAIT for the internet (not counted as failed tries) | PASS | [('telegram', 'waiting', 0), ('email', 'waiting', 0)] |
| NOTIFY | Wi-Fi back: every message arrives exactly once (no loss, no duplicate) | PASS | [('telegram', 'delivered'), ('email', 'sent')] |
| NOTIFY | Telegram caption: person + PPE icons (Helmet ❌ · Vest ✅ · Gloves ❔); an update is a REPLY to the first message | PASS | [['NETRAX · 🦺 PPE VIOLATION', '👷 Net test · W-096', 'Helmet ❌ · Vest ✅ · Gloves ❔'], 42] |
| NOTIFY | low-priority e-mail (entrance refused) -> collected into ONE digest e-mail (every 15 min) | PASS | [[{'channel': 'email', 'status': 'digest', 'attempts': 0, 'error': None}], 46] |
| NOTIFY | daily e-mail cap reached -> PPE e-mail skipped (reason shown), fire e-mail still sent | PASS | ['skipped', 'queued'] |
| NOTIFY | after a crash / restart, a message left 'sending' is picked up again and delivered | PASS | [1, 'delivered'] |
| NOTIFY | Telegram: first message has Acknowledge / Resolve buttons; a press from the group acknowledges (audited), another chat is refused, a second press changes nothing | PASS | ['Not allowed from this chat.', 'Acknowledged · Assembly line', 'Already acknowledged.', 'acknowledged', [{'user': 'telegram:boss', 'action': 'ack'}]] |
| PPE | vest: washed-out fluorescent yellow (entrance USB camera) ACCEPTED; blue / beige / white shirt still rejected | PASS | [True, True, False, False, False] |
| PPE | helmet on a slim person (0.75 of the box width) accepted; a shirt-sized box is still not a helmet | PASS | [True, False] |
| PPE | head above the picture (person close to the camera) -> helmet 'can't tell', never 'NO HELMET' | PASS | {'helmet': ('unverifiable', 0.92), 'vest': ('missing', 0.92)} |
| ENT | same person, new tracker id: no second 'vest missing' popup; putting the vest on -> one APPROVED | PASS | ['denied', 'approved'] |
| ENT | standing too close (head cut at the top) -> manual 'too close - step back', not 'denied' | PASS | ('manual', 'too close to the camera - step back so head to feet are visible') |
| PPE | same worker, same camera: 3 edge alerts -> ONE alert (items updated to helmet + vest), another worker separate | PASS | [['helmet', 'vest'], [None, None]] |
| PPE | merged alert stays open until every merged camera track has ended | PASS | ['open', 'resolved'] |
| PPE | edge restarted -> its old alerts that can no longer be tracked are closed (no orphan 'open' alerts) | PASS | ['resolved', 'edge_restarted'] |
| CAM | phone camera changed its address -> found on the Wi-Fi (not the other phone's), switched + saved in cameras.yaml | PASS | ['http://10.0.0.173:8080/video', None] |
| CAM | phone follow never overwrites the Axis CCTV (cam_01) or the laptop webcam (cam_02) | PASS | [False, False] |
| HW | Sharp dust sensor voltage -> PM2.5 shown (estimated, self-zeroed), clean air ~0, dust high | PASS | [0, 316, 0.62] |
| HW | an uncalibrated PM estimate NEVER raises a warning or counts as smoke (station stays NORMAL) | PASS | ['NORMAL', []] |
| HW | a real PM sensor (pm2_5_ugm3 sent) still alarms as before | PASS | ['PM2.5 200 >= 150 ug/m3'] |
| PPE | cap (never confirmed by the 2nd model) -> 'checking' then NO HELMET; real helmet confirmed once stays OK 30 s; far person / no memory = old behaviour | PASS | [['unverifiable', 'unverifiable', 'missing', 'missing'], ['compliant', 'compliant', 'missing'], 'compliant', 'compliant'] |
| PPE | eyewear check: safety goggles OK, unclear -> no alarm, NORMAL GLASSES (face seen) -> NO GOGGLES, no check -> old behaviour | PASS | ['compliant', 'unverifiable', 'missing', 'compliant'] |
| PPE | a box rejected as normal glasses is labelled so (snapshot / live picture say WHY) | PASS | (False, 'normal glasses') |
| PPE | eyewear model: team's safety goggles -> goggles; sunglasses the detector called 'goggles' -> not goggles | PASS | [[0.97, 0.97, 0.97, 0.96], [0.02, 0.01, 0.04, 0.06, 0.01, 0.01, 0.01, 0.01, 0.01, 0.04, 0.02]] |
| PPE | an alert payload carries items_ok / items_uncertain / items_required (icons), decision unchanged | PASS | [{'action': 'open', 'event_id': 'evt_cb3d366aed', 'type': 'ppe_missing', 'items_missing': ['helmet'], 'items_ok': ['vest'], 'items_uncertain': ['gloves'], 'items_required': ['gloves', 'helmet', 'vest'], 'severity': 'High', 'camera_id': 'cam_t', 'zone_id': 'welding_bay', 'track_id': 5, 'worker_id': None, 'worker_name': None, 'confidence': 0.9, 'bbox': [400, 100, 700, 700], 'started_at': 1003.125, 'onset_at': 1000.0, 'at': 1003.125, 'reason': None}] |
| PPE | backend stores + returns the 3 lists; an event without them still opens (lists = unknown) | PASS | [['vest'], ['gloves'], None] |
| PPE | a registered worker ID always comes with the name (filled from the worker list) | PASS | Ritika |
| PPE | messages: same identity rule + PPE line with icons (Helmet ❌ · Vest ✅ · Gloves ❔) | PASS | [['Ritika · W-091', 'Unregistered person', 'Sourav · W-002 · UNAUTHORISED'], 'Helmet ❌ · Vest ✅ · Gloves ❔'] |
| PPE | dashboard identity: 'Ritika · W-004' · 'Unregistered person' (no fake name) · 'Sourav · W-002' + UNAUTHORISED | PASS | {'reg': '<span class="nx-person">Ritika · W-004</span>', 'unreg': '<span class="nx-person unreg">Unregistered person <small>#17</small></span>', 'unauth': '<span class="nx-person">Sourav · W-002</span> <span class="nx-badge warn">UNAUTH', 'hidden': '<span class="nx-person">Registered worker</span>'} |
| PPE | icon row: new alert = 1 missing, 1 worn, 1 cannot see; old alert (missing list only) still shows its icons | PASS | [1, 1, 1] |
| ENT | a chair with clothes is not a person: weak detection or no head / shoulders -> no entrance decision at all | PASS | 0 / 0 decisions |
| ENT | a real person (head + shoulders, confident) is still checked; standing still never turns them into background | PASS | 1 / 1 |
| ENT | a weak 'person' that never moves for 6 s is remembered as background (one decision at most, no repeats) | PASS | 1 decisions in 18 s |
| 12 | live view: boxes + their helmet label move WITH the walking person between AI results (100 px/s, 0.3 s -> +30 px) | PASS | [180.00000000000006, 100.0, 280.00000000000006, 400.0] |
| 12 | live view: prediction never runs ahead more than 0.5 s, and none for the AI's own frame | PASS | [200.0, 100.0, 300.0, 400.0] |
| 12 | USB camera: MJPG is requested AFTER size + frame rate (else the C270 stays uncompressed at 7.5 fps) | PASS | [3, 4, 5, 6, 38] |
| 12 | camera health: a fresh 30 fps camera is ONLINE although the shared AI checks it only 3.5x/s (live video is separate) | PASS |  |
| 12 | 3 cameras at once -> ONE batched GPU call, each camera gets its own result back | PASS | batches [3], 44 ms for 3 cameras |
| 12 | AI process: every camera gets ITS OWN frame's result back (portrait + landscape + a resized camera) | PASS | stats {'batch_ms_p50': 11.6, 'batch_ms_p95': 11.6, 'wait_ms_p50': 10.7, 'wait_ms_p95': 21.7, 'batch_size_avg': 1.5, 'gpu_busy_share': 0.01, 'process': True} |
| 12 | AI process: a camera whose request timed out waits for it - no pile-up of stale frames, next frame correct | PASS | queued 0, timeouts 3 |
| 12 | edge: a second copy is refused (two copies took the cameras from each other) | PASS | first True, second False |
| 01 | edge pipeline: entrance camera -> ONE 'approved' decision with full PPE (frame size attached) | PASS | approved 1067 ms, 10 frames, ok ['helmet', 'vest', 'boots'] missing [] unclear [] {'helmet': {'compliant': 10, 'missing': 0, 'unverifiable': 0, 'frames': 10}, 'vest': {'compliant': 10, 'missing': 0, 'unverifiable': 0, 'frames': 10}, 'boots': {'compliant': 10, 'missing': 0, 'unverifiable': 0, 'frames': 10}} |
| 04 | edge pipeline: monitored zone, helmet off -> one PPE alert naming the helmet | PASS | items ['goggles', 'helmet'] after ? |
| 12 | edge pipeline: per-camera latency samples recorded (capture -> processed) | PASS |  |
| LIVE | profiler: every AI stage of a camera is timed (engine, tracker, face ID, PPE match, decisions, drawing) | PASS | ai_engine 16.9 ms, tracker 1.8 ms, face_id 0.0 ms, ppe_match 0.7 ms, decisions_fire 3.0 ms, draw_emit 0.1 ms |
| LIVE | AI at ~1 fps, live picture still ~12 fps (video no longer hangs at the AI rate) | PASS | 14.5 pictures/s at [640] px, draw 2.2 ms |
| LIVE | incident clips are fed from the smooth live picture | PASS |  |
| LIVE | face detection runs on a <= 960 px copy (phone frames are 1920 px) and boxes map back exactly | PASS | detector input (540, 960), face x=540 y=480 |
