"""Safety-shoe classifier (our second-stage model): safety_shoe vs other_footwear.

  python -m tools.shoes crops      # foot boxes from the shoe scenes -> datasets/build/shoe_cls/
  python -m tools.shoes train      # trains on the laptop GPU (a few minutes) -> models/shoe_cls.pt

Which scenes are used (datasets/own/ppe/<scene>/, after `python -m tools.frames extract --task ppe`):
  name contains 'footwear_safety' or 'shoes_safety'               -> safety_shoe
  name contains 'footwear_normal', 'shoes_normal' or 'barefoot'   -> other_footwear
The foot boxes come from the labels (pre-labels from ppe_v3A are fine for this; check a few crops in
datasets/build/shoe_cls/ by eye and delete wrong ones before training).
With 2+ scenes per class the split is BY SCENE; with 1 scene per class every 7th crop goes to val.
"""
import argparse
import glob
import os
import shutil

import cv2

from edge.config import path

FOOT_ID = 8          # 'foot' in the v3 class list (tools/frames.py V3)
SAFE_KEYS = ("footwear_safety", "shoes_safety")
OTHER_KEYS = ("footwear_normal", "shoes_normal", "barefoot")


def label_of(scene):
    s = scene.lower()
    if any(k in s for k in SAFE_KEYS):
        return "safety_shoe"
    if any(k in s for k in OTHER_KEYS):
        return "other_footwear"
    return None


def crops(_a):
    root = path("datasets", "own", "ppe")
    out = path("datasets", "build", "shoe_cls")
    shutil.rmtree(out, ignore_errors=True)
    scenes = {}
    for sc in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        lab = label_of(sc)
        if lab:
            scenes.setdefault(lab, []).append(sc)
    if len(scenes) < 2:
        raise SystemExit("need at least one safety-shoe scene and one normal-footwear scene "
                         "(e.g. ppe_15_footwear_safety, ppe_14_footwear_normal)")
    counts = {}
    for lab, scs in scenes.items():
        val_scenes = set(scs[-1:]) if len(scs) >= 2 else set()
        k = 0
        for sc in scs:
            for img_p in sorted(glob.glob(os.path.join(root, sc, "images", "*.jpg"))):
                stem = os.path.splitext(os.path.basename(img_p))[0]
                lab_p = os.path.join(root, sc, "labels", stem + ".txt")
                if not os.path.exists(lab_p):
                    continue
                img = cv2.imread(img_p)
                if img is None:
                    continue
                H, W = img.shape[:2]
                for j, line in enumerate(open(lab_p)):
                    v = line.split()
                    if len(v) < 5 or int(v[0]) != FOOT_ID:
                        continue
                    cx, cy, w, h = (float(t) for t in v[1:5])
                    mx, my = w * 0.15, h * 0.15
                    x1, y1 = max(0, int((cx - w / 2 - mx) * W)), max(0, int((cy - h / 2 - my) * H))
                    x2, y2 = min(W, int((cx + w / 2 + mx) * W)), min(H, int((cy + h / 2 + my) * H))
                    if x2 - x1 < 12 or y2 - y1 < 12:
                        continue
                    split = "val" if (sc in val_scenes or (not val_scenes and k % 7 == 6)) else "train"
                    d = os.path.join(out, split, lab)
                    os.makedirs(d, exist_ok=True)
                    cv2.imwrite(os.path.join(d, f"{stem}_{j}.jpg"), img[y1:y2, x1:x2])
                    counts[(split, lab)] = counts.get((split, lab), 0) + 1
                    k += 1
    for (split, lab), n in sorted(counts.items()):
        print(f"{split:5s} {lab:15s} {n} crops")
    print(f"-> {out}  (open it and delete crops that are not a foot/shoe before training)")


def train(a):
    from ultralytics import YOLO
    from edge.detector import pick_device
    data = path("datasets", "build", "shoe_cls")
    if not os.path.isdir(os.path.join(data, "train")):
        raise SystemExit("run `python -m tools.shoes crops` first")
    r = YOLO("yolov8n-cls.pt").train(data=data, epochs=a.epochs, imgsz=128, batch=32, device=pick_device("auto"),
                                     project=path("runs", "shoe_cls"), name="train", exist_ok=True,
                                     hsv_v=0.6, degrees=10, fliplr=0.5, plots=False)
    best = path("runs", "shoe_cls", "train", "weights", "best.pt")
    shutil.copy(best, path("models", "shoe_cls.pt"))
    print(f"top-1 accuracy (val): {getattr(r, 'top1', 'see above')}  -> models/shoe_cls.pt")
    print("restart the edge pipeline: safety shoes are now judged ('boots' in zones.yaml).")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("crops")
    t = sub.add_parser("train")
    t.add_argument("--epochs", type=int, default=30)
    a = ap.parse_args()
    {"crops": crops, "train": train}[a.cmd](a)


if __name__ == "__main__":
    main()
