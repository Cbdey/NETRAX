# Deployment, bandwidth and cost / ROI (F21, plan section 13)

Every number is marked **measured** (from our own system) or **assumption** (change it for your plant).
We do not quote incident savings as fact. The calculator is at `http://127.0.0.1:8000/static/roi.html`.

## 1. Bandwidth: why edge-first

| Item | Value | Source |
|---|---|---|
| Alert sent from the edge, incl. JPEG snapshot | **41.7 KB** on average (329 alerts) | measured, `data/outbox.db` |
| Stored snapshot | **45 KB** on average (121 snapshots, 640 px) | measured, `data/snapshots` |
| Alerts per camera per day | 8–80 during our tests | measured (test days, not a real shift) |
| One 1080p H.264 CCTV stream | 2–6 Mbps | assumption (typical range, plan 13.2) |

**Cloud video (not our design):** 16 cameras × 4 Mbps = **64 Mbps upload, all day**, which is about **690 GB per day**.
**NETRAX (edge):** 16 cameras × 80 alerts × 41.7 KB ≈ **53 MB per day**. That's roughly 13,000 times less, and only when something happens.
Live tiles (5 fps, 640 px JPEG) stay on the local network and never go to the internet.

## 2. Latency: measured on this laptop
- Inference, all three models: about **42 ms per frame** (GTX 1650, `docs/model_card.md`).
- Edge → backend transport: **98–175 ms** (measured live, 23 Sep).
- Violation first seen → alert on the dashboard: **about 3.0–3.3 s** (live). Most of this is the deliberate confirmation wait (7 of 10 frames plus a 1.5 s grace period) that prevents false alarms.
- A cloud round trip would add upload and queueing time and needs the internet to be up. The edge keeps working offline (outbox buffer, F17).

## 3. Cost framework (plan 13.3)

| Item | Formula / input | Example value (**assumption**) |
|---|---|---|
| Edge device | 1 GPU mini-PC per ~4 cameras | ₹70,000 each |
| Cameras | most plants already have CCTV; new ones only where missing | ₹3,000 per camera |
| Speaker / siren | laptop speaker or PA speaker per zone | ₹2,500 per zone |
| Installation + integration | one-time | ₹50,000 |
| Yearly running cost | electricity + maintenance + model updates | ₹40,000 per year |
| **Yearly benefit** | incidents avoided × cost per incident + supervisor hours saved × hourly cost + insurance / audit savings | inputs in the calculator |
| **Payback (years)** | one-time cost ÷ (yearly benefit − yearly running cost) | computed |

Show three cases (pessimistic / expected / optimistic) by changing only the benefit inputs.
All rupee values above are placeholders to illustrate the formula, not quotes.

## 4. Hardware options (plan 13.1)
| Option | Runtime | Status |
|---|---|---|
| Laptop / PC with NVIDIA GPU | PyTorch CUDA FP32 | **measured**: this laptop, GTX 1650 |
| Intel NUC-class, no GPU | ONNX Runtime CPU | **measured on this CPU**: ~210 ms per frame for 3 models, so about 4–5 FPS for 1 camera |
| Jetson-class | TensorRT | **not tested**, do not claim |
