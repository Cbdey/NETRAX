# NETRAX final acceptance self-test

Run: 05 Nov 2026, 21:13:25 IST - `python -m tools.final_selftest`

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
| 13 | internet down: the alert is stored + shown at once (sending never blocks detection) | PASS | ingest 17 ms |
| 13 | Telegram failure retried (bounded) then shown as FAILED with the reason - never 'delivered' | PASS |  |
| 13 | system health shows the failing channel | PASS |  |
| 13 | no credentials -> NOT CONFIGURED (clearly labelled), nothing claimed as sent | PASS |  |
| 13 | health lists unconfigured channels as warnings | PASS |  |
| 05 | same continuous incident is not sent twice on a channel | PASS |  |
| 10 | 'What happened yesterday at 4:00 PM?' -> the saved record at 16:02 yesterday, nothing else | PASS | Between 04 Nov 2026, 15:30:00 IST to 16:30:00 IST: 1 incident - 1 ppe violation. |
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
| 12 | live video: pictures are pushed over one WebSocket with camera + capture time; a viewer gets only its cameras | PASS | ['cam_02/1793893375.960/1793893376.163', 'cam_02/1793893376.124/1793893376.164'] |
| REP | report generator: time range -> summary (totals, most missed PPE, by zone / camera / worker) + records | PASS | ['Period: 29 Oct 2026 21:13 to 05 Nov 2026 21:13 (IST) - 20 record(s).', 'By type: Entrance check 12, PPE violation 6, Smoke 1, Fire 1.'] |
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
| ZX | questions about the past are answered from the records (grounded history assistant) | PASS | Between 05 Nov 2026, 00:00:00 IST to 21:12:57 IST: 1 incident - 1 fire (level 3). |
| ZX | a viewer can ask and move around but cannot change records | PASS | Your role (viewer) cannot resolve alerts - ask a supervisor or admin. |
| ZX | entrance commands: allow / refuse the person at the entrance | PASS | ['approved', 'denied', None] |
| 12 | camera health: a black picture (covered lens / stuck USB camera, seen live 5 Nov) is reported as such | PASS |  |
| 12 | camera health: a fresh 30 fps camera is ONLINE although the shared AI checks it only 3.5x/s (live video is separate) | PASS |  |
| 12 | 3 cameras at once -> ONE batched GPU call, each camera gets its own result back | PASS | batches [3], 45 ms for 3 cameras |
| 12 | AI process: every camera gets ITS OWN frame's result back (portrait + landscape + a resized camera) | PASS | stats {'batch_ms_p50': 11.1, 'batch_ms_p95': 11.2, 'wait_ms_p50': 16.9, 'wait_ms_p95': 27.2, 'batch_size_avg': 1.5, 'gpu_busy_share': 0.01, 'process': True} |
| 01 | edge pipeline: entrance camera -> ONE 'approved' decision with full PPE (frame size attached) | PASS | approved 1001 ms, 9 frames, ok ['helmet', 'vest', 'boots'] missing [] unclear [] {'helmet': {'compliant': 9, 'missing': 0, 'unverifiable': 0, 'frames': 9}, 'vest': {'compliant': 9, 'missing': 0, 'unverifiable': 0, 'frames': 9}, 'boots': {'compliant': 9, 'missing': 0, 'unverifiable': 0, 'frames': 9}} |
| 04 | edge pipeline: monitored zone, helmet off -> one PPE alert naming the helmet | PASS | items ['goggles', 'helmet'] after ? |
| 12 | edge pipeline: per-camera latency samples recorded (capture -> processed) | PASS |  |
| LIVE | profiler: every AI stage of a camera is timed (engine, tracker, face ID, PPE match, decisions, drawing) | PASS | ai_engine 21.6 ms, tracker 1.5 ms, face_id 0.0 ms, ppe_match 0.5 ms, decisions_fire 2.2 ms, draw_emit 0.1 ms |
| LIVE | AI at ~1 fps, live picture still ~12 fps (video no longer hangs at the AI rate) | PASS | 12.0 pictures/s at [640] px, draw 1.3 ms |
| LIVE | incident clips are fed from the smooth live picture | PASS |  |
| LIVE | face detection runs on a <= 960 px copy (phone frames are 1920 px) and boxes map back exactly | PASS | detector input (540, 960), face x=540 y=480 |
