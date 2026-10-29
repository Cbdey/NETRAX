# NETRAX final acceptance self-test

Run: 29 Oct 2026, 18:15:22 IST - `python -m tools.final_selftest`

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
| 09 | SMS: first try fails (HTTP 503) -> retried -> delivered, attempts logged | PASS | attempts 2 |
| 09 | SMS text has type, severity, location, time and recommended action | PASS |  |
| 09 | e-mail: SMTP drop -> retried -> accepted by the mail server ('sent', never claimed 'delivered') | PASS |  |
| 13 | internet down: the alert is stored + shown at once (sending never blocks detection) | PASS | ingest 23 ms |
| 13 | Telegram failure retried (bounded) then shown as FAILED with the reason - never 'delivered' | PASS |  |
| 13 | system health shows the failing channel | PASS |  |
| 13 | no credentials -> NOT CONFIGURED (clearly labelled), nothing claimed as sent | PASS |  |
| 13 | health lists unconfigured channels as warnings | PASS |  |
| 05 | same continuous incident is not sent twice on a channel | PASS |  |
| 10 | 'What happened yesterday at 4:00 PM?' -> the saved record at 16:02 yesterday, nothing else | PASS | Between 28 Oct 2026, 15:30:00 IST to 16:30:00 IST: 1 incident - 1 ppe violation. |
| 10 | no record in the window -> says 'No matching record was found' (no invented events) | PASS |  |
| 10 | type filter (smoke) + whole day | PASS |  |
| 10 | viewer role: worker names hidden in the answer | PASS |  |
| 10 | every question logged | PASS |  |
| 12 | latency measured as median + 95th percentile (event e2e, time to alert, notification delivery) | PASS | {"n": 5, "p50": 0.0, "p95": 0.0, "max": 0.0, "mean": 0.0} |
| 14 | no GPS + no configured site -> 'Location unavailable' + zone label (never invented) | PASS |  |
| 14 | valid device GPS -> shown with source, accuracy and map link | PASS |  |
| 14 | 1-hour-old reading -> marked STALE and not used | PASS |  |
| 14 | 0,0 / invalid coordinates rejected | PASS |  |
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
| 12 | 3 cameras at once -> ONE batched GPU call, each camera gets its own result back | PASS | batches [3], 50 ms for 3 cameras |
| 01 | edge pipeline: entrance camera -> ONE 'approved' decision with full PPE (frame size attached) | PASS | approved 1119 ms, 10 frames, ok ['helmet', 'vest', 'boots'] missing [] unclear [] {'helmet': {'compliant': 10, 'missing': 0, 'unverifiable': 0, 'frames': 10}, 'vest': {'compliant': 10, 'missing': 0, 'unverifiable': 0, 'frames': 10}, 'boots': {'compliant': 10, 'missing': 0, 'unverifiable': 0, 'frames': 10}} |
| 04 | edge pipeline: monitored zone, helmet off -> one PPE alert naming the helmet | PASS | items ['goggles', 'helmet'] after ? |
| 12 | edge pipeline: per-camera latency samples recorded (capture -> processed) | PASS |  |
