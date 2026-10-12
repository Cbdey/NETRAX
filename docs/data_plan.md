# Own data + retraining plan (answer to "should we collect our own data and retrain?")

**Short answer: yes, collect our own data. Retrain once, carefully. Do not retrain from scratch.**
Both current models only saw public datasets: construction sites, outdoor scenes and internet fire photos.
Our demo runs on a laptop webcam in a room, with our own helmet, vest, gloves and shoes, and small indoor fires.
That gap (a "domain shift") is the main reason for real-input failures such as the phone-screen fire and shirt-or-cap confusion. More tuning of the rules cannot close it.

## What to record (with `python -m tools.collect`)

One `--scene` per recording session. Aim for about 30–80 images per scene (1 frame per second, not every frame).

**PPE** (`--task ppe`). Use the exact kit we will demo with. **Safety shoes are a priority**: the current boots class misses our shoes, so shoes are switched off until this data exists.
- Full kit, then each item removed one at a time (helmet off, vest off, gloves off, shoes off)
- Helmet held in the hand; vest over the arm (the "carried, not worn" case)
- Caps, hoods, orange/yellow T-shirts, plain shirts (the look-alikes that fooled v3)
- Far / near / cut off at the frame edge; 2–3 people overlapping
- Dark room, backlit window, lamp-lit night
- Camera mounted high (about 2 m, angled down) as well as at desk height

**Fire / smoke** (`--task fire`). Only with a fire extinguisher or water bucket at hand, on a metal tray, outdoors or near an open window, with an adult supervising.
- Candle, lighter, a small paper fire in a metal tray, incense sticks, smoke from a candle that was just blown out
- **Negatives (just as important):** a phone or monitor playing fire videos, red/orange clothes, a sunset, a tube light, a phone flash, a room heater coil, hair, a white wall, steam from a kettle, a hand over the lens

## Labelling
1. Upload each scene folder (`datasets/own/<task>/<scene>/`) to Roboflow or CVAT. It already contains pre-labels from the current model.
2. **Correct every box.** Pre-labels are a starting point, not ground truth. Leave negative images with no boxes.
3. Labelling rule for a helmet: a box only when the helmet is on the head. A helmet in the hand is not labelled as `helmet`. The camera must learn "worn".

## Split (plan 8.4)
- Split **by scene**, never by frame: about 70 % of scenes for training, 15 % for validation, 15 % as a **hard test set**.
- The hard test set is never used for training. It produces the "normal vs hard" table the judges ask for.

## Training recipe (Colab, same as before)
- Start from **our current weights** (`ppe_v2.pt`, `fire_smoke_v1.pt`), not from `yolov8s.pt`, so the new model does not lose what it already knows.
- Train on **public data + our data together**. Training on our data alone makes the model forget the public data.
- 640 px, 40–60 epochs, default augmentation (mosaic, HSV jitter), patience 15.
- Save the results as **new files**: `ppe_v3.pt`, `fire_smoke_v2.pt`. Never overwrite v2.
- Compare v2 and v3 on the **same** public validation set **and** our hard test set, and write both into `docs/model_card.md`.
  Switch `config/settings.yaml` to v3 only if it wins on the hard set without losing much on the public set.

## What retraining will not fix (so do not wait for it)
- A phone screen showing fire: needs an IR flame sensor (hardware phase).
- Tiny or occluded workers: the `unverifiable` state handles this. Mount cameras so a whole person is at least 80 px tall.
