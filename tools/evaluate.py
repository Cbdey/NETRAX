"""Per-class precision / recall / mAP on a labelled test set (plan section 12.1).

    python -m tools.evaluate --task ppe  --data datasets/hard_test/ppe   --name hard_v2
    python -m tools.evaluate --task fire --data datasets/hard_test/fire  --name hard_v1
    python -m tools.evaluate --task ppe  --data path/to/data.yaml --split test --name public_v2

--data is either a YOLO data.yaml, or a folder with images/ + labels/ (e.g. a corrected, exported
tools.collect scene) - the class order must match the model (see datasets/own/<task>/classes.txt).
Writes docs/eval/<task>_<name>.json and prints a markdown table for docs/model_card.md.
Report the hard set and the normal set side by side; never mix frames of one scene into both."""
import argparse
import json
import os
import tempfile
import time

import yaml

from edge.config import load_all, path
from edge.detector import pick_device


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task", choices=["ppe", "fire"], required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--split", default="val")
    ap.add_argument("--name", required=True)
    ap.add_argument("--model", help="weights to test (default: the one in settings.yaml)")
    a = ap.parse_args()
    from ultralytics import YOLO
    s, _, _ = load_all()
    weights = a.model or path(s["models"][a.task])
    model = YOLO(weights)
    data = a.data
    if os.path.isdir(data):                       # images/ + labels/ folder -> temporary data.yaml
        names = [model.names[i] for i in sorted(model.names)]
        tmp = os.path.join(tempfile.mkdtemp(), "data.yaml")
        with open(tmp, "w") as f:
            yaml.safe_dump({"path": os.path.abspath(data), "train": "images", "val": "images",
                            "test": "images", "names": names}, f)
        data, a.split = tmp, "val"
    r = model.val(data=data, split=a.split, imgsz=s["models"]["imgsz"], device=pick_device("auto"),
                  plots=False, verbose=False)
    rows = []
    for i, c in enumerate(r.box.ap_class_index):
        p, rec, ap50, ap = r.box.class_result(i)
        rows.append({"class": model.names[int(c)], "precision": round(float(p), 3),
                     "recall": round(float(rec), 3), "mAP50": round(float(ap50), 3),
                     "mAP50-95": round(float(ap), 3)})
    out = {"task": a.task, "name": a.name, "weights": os.path.basename(weights), "data": a.data,
           "measured_at": time.strftime("%Y-%m-%d %H:%M"), "images": int(r.seen) if hasattr(r, "seen") else None,
           "overall": {"precision": round(float(r.box.mp), 3), "recall": round(float(r.box.mr), 3),
                       "mAP50": round(float(r.box.map50), 3), "mAP50-95": round(float(r.box.map), 3)},
           "per_class": rows}
    os.makedirs(path("docs", "eval"), exist_ok=True)
    with open(path("docs", "eval", f"{a.task}_{a.name}.json"), "w") as f:
        json.dump(out, f, indent=2)
    print(f"\n{a.task} / {a.name} ({out['weights']}) on {a.data}")
    print("| Class | P | R | mAP50 |\n|---|---|---|---|")
    for x in rows:
        print(f"| {x['class']} | {x['precision']} | {x['recall']} | {x['mAP50']} |")
    o = out["overall"]
    print(f"| **all** | {o['precision']} | {o['recall']} | {o['mAP50']} |")


if __name__ == "__main__":
    main()
