"""Score models on OUR hard test scenes (datasets/splits.yaml -> test), on the laptop.

  python -m tools.eval_hardtest --task ppe  --models models/ppe_v3A.pt models/ppe_v3B.pt
  python -m tools.eval_hardtest --task fire --models models/fire_smoke_v2A.pt models/fire_smoke_v2B.pt

Writes docs/eval/hardtest_<task>.md (mAP50 per class). FP32 on purpose (GTX 16xx).
"""
import argparse
import os
import tempfile

import yaml

from edge.config import path

NAMES = {"ppe": ["head", "helmet", "face", "goggles", "mask", "vest", "hand", "gloves", "foot"],
         "fire": ["Fire", "Smoke"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", choices=["ppe", "fire"], required=True)
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--split", default="test", choices=["test", "val"])
    a = ap.parse_args()
    from ultralytics import YOLO
    scenes = yaml.safe_load(open(path("datasets", "splits.yaml"), encoding="utf-8"))[a.task][a.split]
    dirs = [path("datasets", "own", a.task, s, "images") for s in scenes]
    dirs = [d for d in dirs if os.path.isdir(d)]
    names = NAMES[a.task]
    tmp = os.path.join(tempfile.mkdtemp(), "hard.yaml")
    yaml.safe_dump({"path": path("datasets"), "train": dirs, "val": dirs,
                    "names": {i: n for i, n in enumerate(names)}}, open(tmp, "w"))
    rows = []
    for m in a.models:
        if not os.path.exists(path(m)):
            print("missing", m); continue
        r = YOLO(path(m)).val(data=tmp, imgsz=640, batch=8, half=False, plots=False, verbose=False,
                              device=0 if __import__("torch").cuda.is_available() else "cpu")
        per = {r.names[int(c)]: r.box.ap50[i] for i, c in enumerate(r.box.ap_class_index)}
        rows.append((os.path.basename(m), r.box.map50, r.box.mp, r.box.mr, per))
        print(f"{os.path.basename(m)}: mAP50={r.box.map50:.3f} P={r.box.mp:.3f} R={r.box.mr:.3f}  " +
              "  ".join(f"{k}={v:.2f}" for k, v in per.items()))
    cls = sorted({k for *_, p in rows for k in p}, key=lambda n: names.index(n) if n in names else 99)
    os.makedirs(path("docs", "eval"), exist_ok=True)
    out = path("docs", "eval", f"hardtest_{a.task}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"# {a.task} on our {a.split} scenes: {', '.join(scenes)}\n\n")
        f.write("| model | mAP50 | P | R | " + " | ".join(cls) + " |\n|" + "---|" * (4 + len(cls)) + "\n")
        for n, mp, p, rr, per in rows:
            f.write(f"| {n} | {mp:.3f} | {p:.3f} | {rr:.3f} | " +
                    " | ".join(f"{per[c]:.2f}" if c in per else "-" for c in cls) + " |\n")
        f.write("\nLabels: machine-made + reviewed (datasets/own/LABELS_README.md) - indicative, not a certified benchmark.\n")
    print("->", out)


if __name__ == "__main__":
    main()
