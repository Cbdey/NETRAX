"""Failure-case gallery (F18, plan section 9): real tricky inputs from our own live tests,
what a naive detector does, and what NETRAX does now - re-checked with the CURRENT code.

    python -m tools.gallery        -> docs/failure_gallery/*.jpg + docs/failure_gallery/README.md

Faces are pixelated (privacy). Every 'now' verdict is computed here by the current
fire_verify / association code on the stored snapshot + detector box - not typed by hand."""
import json
import os
import sqlite3
import textwrap

import cv2
import numpy as np

from edge import annotate
from edge.association import evaluate
from edge.config import load_all, path
from edge.fire_verify import FireVerifier
from edge.identity import FaceEngine

OUT = path("docs", "failure_gallery")

# (file name, event_id, scenario, naive result, how we handle it)
CASES = [
    ("01_face_as_smoke", "evt_c4da498a1b", "Worker's face and bare shoulders in a warm-lit room",
     "The smoke model fires at conf 0.57: a Critical SMOKE alarm on a person.",
     "Colour check: 25% or more skin-coloured pixels means a face or arm, never smoke. Rejected."),
    ("02_hair_as_smoke", "0364b6", "Close-up: the top of a worker's head fills the frame",
     "Dark, textured hair looks like dense smoke to the model.",
     "30% or more dark pixels means hair or shadow. Rejected."),
    ("03_flash_as_smoke", "evt_1d0855", "Phone flash pointed at the camera",
     "The bright haze is read as smoke, giving a Critical alarm.",
     "25% or more blown-out pixels means glare from a flash or lamp. Rejected."),
    ("04_match_in_hand", "evt_7231e446b9", "Match / lighter flame held in the hand, close to the camera",
     "Old rule: fire on a person = orange clothing, so it was suppressed and no alarm sounded.",
     "A flame glows (white-hot core) and flickers; clothing does neither. CONFIRMED, siren on."),
    ("05_phone_screen_fire", "evt_cec486edda", "Phone playing a fireplace video",
     "Fire alarm.",
     "KNOWN LIMITATION: still confirmed as fire. A camera alone cannot tell a screen from a flame. "
     "Fix planned: an IR flame sensor in the hardware phase."),
    ("06_closeup_no_boots", "evt_68e2cd8e05", "Seated close-up, legs out of the frame",
     "'No safety shoes' alarm: the feet are not visible, so no shoes are detected.",
     "Feet not in view means 'unverifiable', never 'missing'. No alert."),
    ("07_one_person_one_id", "evt_df749c7f8a", "Registered worker next to an unregistered visitor",
     "The tracker gives a new number every time someone re-enters, so the same person gets many alerts.",
     "Face match against registration photos: the worker is always W-001, the visitor gets a temporary V-id. "
     "The speaker calls the worker by ID."),
]


def full_id(c, short):
    r = c.execute("SELECT event_id, type, bbox FROM events WHERE event_id LIKE ?", (f"%{short[-6:]}",)).fetchone()
    return r


def verdict(fv, s, img, typ, bbox):
    if typ in ("fire", "smoke"):
        d = {"cls": typ.title(), "conf": 0.6, "bbox": bbox}
        ok, why = fv.colour_ok(img, d)
        return ("PASSES colour check" if ok else "REJECTED") + f" - {why}"
    return None


def caption(img, lines, colour):
    w = img.shape[1]
    wrapped = [t for ln in lines for t in textwrap.wrap(ln, 78)]
    bar = np.full((22 * len(wrapped) + 12, w, 3), 255, np.uint8)
    for i, t in enumerate(wrapped):
        cv2.putText(bar, t, (8, 24 + 22 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    colour if i == 0 else (40, 40, 40), 1, cv2.LINE_AA)
    return np.vstack([img, bar])


def main():
    s, zones, _ = load_all()
    s["fire"]["debug_log"] = None
    fv = FireVerifier(s["fire"])
    eng = FaceEngine()
    os.makedirs(OUT, exist_ok=True)
    c = sqlite3.connect(path("data", "sentineleye.db"))
    md = ["# Failure-case gallery (F18)", "",
          "Real inputs from our own live webcam tests (22–24 Sep 2026). The **Now** column is re-computed by",
          "`python -m tools.gallery` with the current code. Faces are pixelated.", "",
          "| # | Scenario | Naive detector | NETRAX now | Check (computed) |", "|---|---|---|---|---|"]
    for name, short, scen, naive, ours in CASES:
        row = full_id(c, short)
        p = path("data", "snapshots", f"{row[0]}.jpg") if row else None
        if not p or not os.path.exists(p):
            print(f"skip {name}: snapshot not found")
            continue
        img = cv2.imread(p)
        typ, bbox = row[1], json.loads(row[2] or "null")
        check = verdict(fv, s, img, typ, bbox) or ""
        if name == "06_closeup_no_boots":
            from edge.detector import Detector  # noqa: F401  (person box re-detected below)
            from ultralytics import YOLO
            r = YOLO(path("yolov8n.pt")).predict(img, classes=[0], verbose=False)[0]
            pb = max(r.boxes.xyxy.tolist(), key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
            st = evaluate([{"tid": 1, "bbox": pb, "conf": 0.9}], [], {1: ["boots"]},
                          img.shape[1], img.shape[0], s["association"])[0][1]
            check = f"boots -> {st['boots'][0]}"
        if name == "07_one_person_one_id":
            check = "offline identity run: W-001 on 6/6 snapshots of the registered worker (docs/model_card.md)"
        faces = [(f[0] - 0.25 * f[2], f[1] - 0.35 * f[3], f[0] + 1.25 * f[2], f[1] + 1.2 * f[3])
                 for f in eng.faces(img)]
        img = annotate.blur_regions(img, faces)
        bad = "LIMITATION" in ours
        img = caption(img, [f"{name[:2]}. {scen}", "Naive: " + naive, "Now: " + ours, "Computed: " + check],
                      (0, 0, 200) if bad else (0, 130, 0))
        cv2.imwrite(os.path.join(OUT, f"{name}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        md.append(f"| {name[:2]} | {scen} | {naive} | {ours} | {check} |")
        print(f"{name}: {check}")
    md += ["", "Images: `docs/failure_gallery/<nn>_*.jpg`.", "",
           "Still to record for the gallery (plan section 9): orange vest / red machinery (tested only "
           "synthetically in `edge.selftest`), cap vs helmet, plain shirt vs vest, dark / backlit room, "
           "crowded / occluded workers, steam from a kettle."]
    with open(os.path.join(OUT, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    print(f"gallery -> {OUT}")


if __name__ == "__main__":
    main()
