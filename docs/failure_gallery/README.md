# Failure-case gallery (F18)

Real inputs from our own live webcam tests (22–24 Sep 2026). The **Now** column is re-computed by
`python -m tools.gallery` with the current code. Faces are pixelated.

| # | Scenario | Naive detector | NETRAX now | Check (computed) |
|---|---|---|---|---|
| 01 | Worker's face and bare shoulders in a warm-lit room | The smoke model fires at conf 0.57: a Critical SMOKE alarm on a person. | Colour check: 25% or more skin-coloured pixels means a face or arm, never smoke. Rejected. | REJECTED - skin 31% (a face / arm, not smoke) |
| 02 | Close-up: the top of a worker's head fills the frame | Dark, textured hair looks like dense smoke to the model. | 30% or more dark pixels means hair or shadow. Rejected. | REJECTED - dark hair / shadow |
| 03 | Phone flash pointed at the camera | The bright haze is read as smoke, giving a Critical alarm. | 25% or more blown-out pixels means glare from a flash or lamp. Rejected. | REJECTED - glare (phone flash / lamp) |
| 04 | Match / lighter flame held in the hand, close to the camera | Old rule: fire on a person = orange clothing, so it was suppressed and no alarm sounded. | A flame glows (white-hot core) and flickers; clothing does neither. CONFIRMED, siren on. | PASSES colour check - flame colour 14%, hot core 35% |
| 05 | Phone playing a fireplace video | Fire alarm. | KNOWN LIMITATION: still confirmed as fire. A camera alone cannot tell a screen from a flame. Fix planned: an IR flame sensor in the hardware phase. | PASSES colour check - flame colour 19%, hot core 2% |
| 06 | Seated close-up, legs out of the frame | 'No safety shoes' alarm: the feet are not visible, so no shoes are detected. | Feet not in view means 'unverifiable', never 'missing'. No alert. | boots -> unverifiable |
| 07 | Registered worker next to an unregistered visitor | The tracker gives a new number every time someone re-enters, so the same person gets many alerts. | Face match against registration photos: the worker is always W-001, the visitor gets a temporary V-id. The speaker calls the worker by ID. | offline identity run: W-001 on 6/6 snapshots of the registered worker (docs/model_card.md) |

Images: `docs/failure_gallery/<nn>_*.jpg`.

Still to record for the gallery (plan section 9): orange vest / red machinery (tested only synthetically in `edge.selftest`), cap vs helmet, plain shirt vs vest, dark / backlit room, crowded / occluded workers, steam from a kettle.
