# NETRAX model card

## Models

| Model | Architecture | Classes | Training data | Validation mAP50 |
|---|---|---|---|---|
| `models/ppe_v2.pt` | YOLOv8s | person, helmet, vest, gloves, boots, mask | Roboflow construction-site-safety v27 + ppe-detection-ozhfb v14 (merged, NO-* classes dropped) | 0.872 overall (P 0.853, R 0.845). helmet 0.953, vest 0.944, person 0.862, boots 0.837, gloves 0.822, mask 0.815 |
| `models/fire_smoke_v1.pt` | YOLOv8n | Fire, Smoke | sayed-gamall/fire-smoke-detection-yolov11 v2 | 0.769 overall. Fire 0.841, Smoke 0.697 |
| `yolov8n.pt` (COCO) | YOLOv8n | person only | COCO pretrained | not re-measured. Used because the PPE model's person class misses indoor and close-up people |

The mAP values come from each dataset's own validation split, **not** from our own footage.
Results from our own hard test set are still to do (plan section 9, F18).

## Inference speed (F12): measured

Measured on 2026-09-23 by `python -m tools.benchmark --onnx` (raw numbers in `docs/benchmark.json`).
Setup: NVIDIA GTX 1650 4 GB, Intel CPU (family 6 model 154), 640 px input, 20 real frames from `data/snapshots`, 100 runs per model after 5 warm-up runs.

| Model | Runtime | mean ms | p50 ms | p95 ms |
|---|---|---|---|---|
| person (yolov8n COCO) | PyTorch CUDA FP32 | 11.1 | 10.1 | 15.8 |
| person (yolov8n COCO) | ONNX Runtime CPU | 50.6 | 48.1 | 58.9 |
| ppe_v2 (yolov8s) | PyTorch CUDA FP32 | 21.0 | 20.8 | 23.1 |
| ppe_v2 (yolov8s) | ONNX Runtime CPU | 118.8 | 115.8 | 132.9 |
| fire_smoke_v1 (yolov8n) | PyTorch CUDA FP32 | 10.2 | 9.4 | 13.5 |
| fire_smoke_v1 (yolov8n) | ONNX Runtime CPU | 41.7 | 40.7 | 46.1 |

What this means:
- On this GPU, all three models together take about **42 ms per frame**, so one camera at the configured 10 processed FPS uses about 40 % of the GPU.
- ONNX exports are in `models/*.onnx`. The installed ONNX Runtime is the CPU build only, so the ONNX rows are CPU numbers. They show what a GPU-less NUC-class edge box would get: about 210 ms per frame for all three models, so roughly 4–5 FPS for one camera.
- Not measured: ONNX Runtime CUDA, TensorRT, FP16 (GTX 16xx cards have FP16 problems), Jetson. Do not claim these.

## Response time: what the user actually waits for

| Stage | Before (v4) | Now | How it is measured |
|---|---|---|---|
| Inference, all models | ~42 ms | ~42 ms | benchmark above |
| PPE decision: N-of-M frames + grace period | 10/15 frames + 3.0 s ≈ **4.0 s** | 7/10 frames + 1.5 s ≈ **2.1 s** | selftest: "time-to-alert" check |
| Fire decision | 4/6 fire frames + 1.0 s grace | unchanged | |
| Smoke decision | 4/6 fire frames + 3.0 s grace | 4/6 fire frames + 2.0 s grace | |
| Edge → backend transport | 0.3–0.9 s (outbox polled every 0.3 s, new HTTP connection per alert) | the outbox wakes instantly and keeps one connection open | `latency_ms` per event (capture → received) |
| First alert after start-up | +~1 s CUDA warm-up | warm-up done at start-up | |

The dashboard KPI "Avg time to alert" shows the measured time from the violation's first frame to the alert arriving at the backend (`time_to_alert_ms`). Its subtitle shows the transport-only part.
The v4 transport numbers (0.3–0.9 s) come from the event database. The new transport number has to be re-measured on a live run: read it from the KPI after 10 or more alerts.

## Known limitations
- The fire model confirmed a **phone screen playing a fireplace video** as a real fire (event `evt_cec486edda`). Vision alone cannot tell a screen from a flame. The planned fix is an IR flame sensor (hardware phase, not built yet).
- Smoke is the weakest class (mAP50 0.697). Hair and white walls produced smoke false alarms before the v4 texture and colour checks.
- Gloves are small. Safety shoes (boots) are **not required in any zone for now**: the public boots class missed real shoes on our webcam. They will return after training on our own shoe photos.
- None of the training data comes from our own cameras. See `docs/data_plan.md`.

## Fire / smoke changes after live tests (2026-09-23), not yet re-measured live
- A flame held in the hand was suppressed by the "orange vest on a person" rule. A region on a person now counts as fire if it glows (white-hot core, at least 2 %) **and** flickers.
- A small match or lighter flame is mostly white core: a white-hot core plus at least 2 % orange now passes the colour check. A phone flash (white with no orange) is still rejected.
- Thin agarbatti smoke wavered and kept starting new regions. Region matching now tolerates a 50 % drift. The fire model runs every frame, with thresholds Fire 0.45 and Smoke 0.35, and 5 of the last 10 frames confirm.
- Dark regions (hair) are rejected as smoke. The earlier hair false alarms were at conf 0.53–0.68.
- `data/fire_debug.log` records every candidate with conf, hits, flicker and hot-core share, and says why it was confirmed or suppressed. Use it to tune on real footage.

## Worker identity (F19, face-based, consent)
YuNet face detector + SFace embeddings (OpenCV zoo), matched by cosine similarity against 1–5 registration photos (threshold 0.36).
Offline check on our own snapshots: one worker was registered from 3 photos. In 6 other snapshots they were identified as W-001 every time, and 3 other people got visitor IDs. That is 6 images, not a benchmark.

## Multi-stream throughput (F08 / F12): measured
Measured on 2026-09-24 on this laptop (GTX 1650): 4 cameras at once, full pipeline (person + PPE + fire models, tracking, face ID, snapshots), 90 s sample after warm-up.
The sources were four synthetic 15 fps test clips built from our own snapshots (`data/loadtest/`), not demo footage.

| Camera | Zone risk | Target fps | Achieved fps (mean / min) | Status |
|---|---|---|---|---|
| cam_01 Welding bay | high | 10 | 7.0 / 6.0 | ONLINE |
| cam_03 Assembly line | high | 10 | 6.9 / 5.8 | ONLINE |
| cam_02 Warehouse | medium | 10 → **4** (load manager) | 4.0 / 4.0 | ONLINE |
| cam_04 Warehouse 2 | medium | 10 → **4** (load manager) | 4.1 / 3.9 | ONLINE |

That is about 22 processed frames per second in total on one 4 GB GPU. The load manager (`edge/load.py`, plan 7.5) slowed the medium-risk zones first and kept the high-risk zones as fast as the GPU allowed. No stream stopped, and fire/smoke checks kept running on all four.
One fix came out of this run: the white core of a fire was also confirmed as "smoke". A smoke box that lies mostly inside a fire box now counts as part of that fire (regression test added).

## v3 models (trained 10-11 Oct 2026 on Colab T4) - measured

### Fire/smoke v2A (`models/fire_smoke_v2A.pt`, YOLOv8n)
Fine-tuned from our `fire_smoke_v1` on D-Fire (21,527 images, 9,838 of them with no fire or smoke) plus our
low-light / haze copies (2,824). Same 7,405-image validation set (D-Fire val + test) for both rows:

| Model | Fire mAP50 | Smoke mAP50 | All mAP50 | P | R |
|---|---|---|---|---|---|
| fire_smoke_v1 | 0.223 | 0.285 | 0.254 | 0.393 | 0.305 |
| **fire_smoke_v2A** | **0.651** | **0.747** | **0.699** | 0.711 | 0.638 |

Fair note: v2A trained on D-Fire's training split, v1 never saw D-Fire, so part of the gain is "seen this
kind of data". The real check is our own hard test set (stage B) and live tests.
First live test (11 Oct, 06:25-06:48, webcam + phone): no smoke alarm confirmed; weak smoke candidates
(conf 0.35-0.47) stayed "verifying". A phone playing a fire video was confirmed as fire (known limitation:
the hardware flame sensor decides real vs screen).

### PPE v3A (`models/ppe_v3A.pt`) - NETRAX-YOLO: YOLOv8s + extra P2 (stride-4) head
Our model definition `colab/sentineleye-yolov8s-p2.yaml`: 4 detection scales instead of 3 (10.6 M params,
37.0 GFLOPs), initialised from COCO yolov8s (219/437 weight tensors transferred, the new P2 head trained from
scratch). Trained on SH17 (8,099 images, official train/val split; 17 classes remapped to our 9) plus 1,943
low-light / haze / motion-blur copies. Validation: SH17 val, 1,620 images.

| head | helmet | face | goggles | mask | vest | hand | gloves | foot | **all** |
|---|---|---|---|---|---|---|---|---|---|
| 0.922 | 0.696 | 0.933 | 0.714 | 0.716 | 0.465 | 0.870 | 0.577 | 0.587 | **0.720** (P 0.803, R 0.664) |

SH17 conventions checked on the data: 'head' = any head (83% of helmets sit inside a head box);
'hand' = a BARE hand (gloves are a separate class; 2.5% overlap). That is why a detected hand is direct
evidence of "no gloves".
Weak spots: helmet / vest (few examples in SH17: 973 / 547 boxes) - the live system keeps `ppe_v2` for helmet,
vest and mask (hybrid, `models.ppe_aux` in settings.yaml); gloves / foot are small and partly hidden.

### Live pipeline (hybrid, 11 Oct)
person + body keypoints: YOLOv8n-pose -> helmet / vest / mask: ppe_v2 -> gloves / goggles / hand / face / head /
foot: ppe_v3A -> safety shoe vs normal footwear: our classifier on the ankle crop (`tools/shoes.py`, needs our
shoe videos) -> fire / smoke: fire_smoke_v2A. Speed with all models: to be re-measured (`python -m tools.benchmark`).

### Fire/smoke v2B (`models/fire_smoke_v2B.pt`, YOLOv8n) - Stage B, 12 Oct 2026, Colab T4
Fine-tuned from v2A on D-Fire + our own fire / no-fire scenes (x3) + low-light / haze copies; 10 epochs, 0.84 h.
Two test sets: "D-Fire+own val" (7,463 images) and OUR HARD TEST (142 images never trained on: lighter video
fire_02 + white T-shirt negative neg_04; 57 fire boxes, no smoke boxes).

| Model | D-Fire+own val mAP50 (Fire / Smoke) | Our hard test mAP50 | Hard test P / R |
|---|---|---|---|
| fire_smoke_v1 | 0.253 (0.22 / 0.28) | 0.982 | 1.000 / 0.946 |
| fire_smoke_v2A | 0.699 (0.65 / 0.75) | 0.161 | 0.124 / 0.421 |
| **fire_smoke_v2B** | **0.706** (0.66 / 0.75) | **0.870** | 0.955 / 0.772 |

Reading it honestly:
- v2B is the only model that is good on BOTH sets, so it is the live model.
- v2A had learned D-Fire's big outdoor fires and almost forgot small indoor flames (0.161); our own data fixed that.
- v1 is best on the lighter test: its training set has close-up small flames. It is weak on everything else (0.253).
- The hard test is small (one fire scene, one negative scene, no smoke) and its labels are machine-made + reviewed
  (datasets/own/LABELS_README.md): treat it as indicative. Smoke on our own footage is not measured yet.

### PPE v3B (`models/ppe_v3B.pt`, NETRAX-YOLO P2) - Stage B, 12 Oct 2026, Colab T4
Fine-tuned from v3A on SH17 + Construction-Site-Safety v27 + PPE-ozhfb v14 (their missing classes pseudo-labelled
by v3A) + our own scenes (x3) + degraded copies; 20 epochs. Measured on the laptop with `tools/eval_hardtest.py`
on OUR HARD TEST (123 images, 944 boxes; scenes ppe_02 full kit dim, ppe_17 two in a line, ppe_22 backlit, never
trained on). Full table: `docs/eval/hardtest_ppe.md`.

| Model | mAP50 | P | R | mAP50-95 | helmet | goggles | gloves | hand | foot |
|---|---|---|---|---|---|---|---|---|---|
| ppe_v3A | 0.940 | 0.910 | 0.899 | 0.833 | 0.92 | 0.70 | 0.97 | 0.97 | 0.99 |
| **ppe_v3B** | **0.959** | 0.926 | 0.936 | 0.786 | **0.99** | **0.77** | 0.97 | 0.97 | 0.99 |

Reading it honestly:
- These scores are HIGH partly because the hard-test labels were drafted by our own models (v2 + v3A), then
  cleaned by rules and review (datasets/own/LABELS_README.md). That favours v3A; v3B still wins on mAP50,
  precision, recall, helmet (+0.07) and goggles (+0.07). The lower mAP50-95 is expected: the box edges were
  drawn by v3A, so v3A matches them more tightly.
- Treat the numbers as "agreement with reviewed labels on unseen scenes", not as a certified benchmark.
- Live system: v2 still supplies helmet / vest / mask; v3B (ppe_aux) supplies head, face, goggles, hand, gloves, foot.

## 12 Oct: which model gives helmet / vest, and speed of the live set
`python -m tools.check_all` on the laptop (GTX 1650, FP32). Full log: `docs/eval/check_all.log`.

| item | scenes | ppe_v2 AP50 (P / R) | ppe_v3B AP50 (P / R) | decision |
|---|---|---|---|---|
| helmet | hard test (123 img) | 0.993 (1.00 / 0.96) | 0.988 (0.95 / 0.95) | tie |
| vest | hard test | 0.995 (0.99 / 1.00) | 0.991 (0.98 / 0.98) | tie |
| helmet | val (ppe_06 vest off, ppe_21 far, 66 img) | 0.943 (0.92 / 0.91) | 0.896 (0.87 / 0.79) | **keep v2** |
| vest | val | 0.802 (0.54 / 0.96) | 0.903 (0.70 / 1.00) | **v3B** |

ppe_v2 kept "seeing" a vest in the vest-off scene (low precision = a missing vest would pass as worn), so
`aux_items` now includes `vest`. Caveat: the val scenes picked v3B's best epoch, so they slightly favour v3B.

| live model | median ms / frame |
|---|---|
| person + keypoints (yolov8n-pose) | 10.0 |
| PPE main ppe_v2 (helmet, mask) | 20.9 |
| PPE aux ppe_v3B (vest, gloves, goggles, body parts) | 27.9 |
| fire_smoke_v2B | 8.4 |
| shoe_cls (per batch of foot crops, only when feet are seen) | 5.6 |
| **sum** | **about 67–73 ms → about 14 frames/s for one camera** |

Fire on the hard test (unchanged): v2A 0.161, v2B 0.870.

PPE v3B training (Colab, 20 epochs, results.csv epoch 19): val mAP50 **0.815**, mAP50-95 0.527, P 0.856, R 0.768
on the combined Stage B val set (SH17 val + Roboflow CSS v27 + ozhfb v14 val + own val scenes). Not directly comparable
with v3A's 0.720 (SH17 val only); compare the two on the own hard test (0.940 → 0.959).
