"""Collect your OWN training images with pre-labels (plan section 8.2-8.4).

  python -m tools.collect --task ppe  --scene lab_full_kit_day
  python -m tools.collect --task fire --scene room_candle_tray  --every 0.5
  python -m tools.collect --task fire --scene red_shirt_negative   # negatives matter as much
  python -m tools.collect --task ppe  --source demo/clips/warehouse.mp4 --scene warehouse_clip

Saves every --every seconds (and on 's') to datasets/own/<task>/<scene>/images + labels (YOLO txt,
pre-labelled by the current model). Upload the scene folders to Roboflow/CVAT, CORRECT every box,
then export. Keep one scene = one recording session, and split train/val/test BY SCENE, never by
frame (near-identical frames leak into the test set). 'q' quits."""
import argparse
import os
import time

import cv2

from edge.config import load_all, path
from edge.detector import pick_device


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task", choices=["ppe", "fire"], required=True)
    ap.add_argument("--scene", required=True, help="short name of this session / scenario")
    ap.add_argument("--source", default="0")
    ap.add_argument("--every", type=float, default=1.0, help="seconds between saved frames")
    ap.add_argument("--conf", type=float, default=0.30, help="pre-label threshold (low: easier to delete than draw)")
    ap.add_argument("--no-window", action="store_true")
    a = ap.parse_args()
    from ultralytics import YOLO
    s, _, _ = load_all()
    model = YOLO(path(s["models"][a.task]))
    dev = pick_device(s["models"].get("device", "auto"))
    out = path("datasets", "own", a.task, a.scene)
    os.makedirs(os.path.join(out, "images"), exist_ok=True)
    os.makedirs(os.path.join(out, "labels"), exist_ok=True)
    with open(os.path.join(out, "..", "classes.txt"), "w") as f:
        f.write("\n".join(model.names[i] for i in sorted(model.names)))
    src = int(a.source) if a.source.isdigit() else a.source
    cap = cv2.VideoCapture(src, cv2.CAP_DSHOW) if isinstance(src, int) and os.name == "nt" else cv2.VideoCapture(src)
    if not cap.isOpened():
        raise SystemExit(f"cannot open {a.source}")
    saved, last = 0, 0.0
    print(f"saving to {out} - 's' save now, 'q' quit")
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        key = -1
        if not a.no_window:
            cv2.imshow("collect (s = save, q = quit)", frame)
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
        if time.time() - last < a.every and key != ord("s"):
            continue
        last = time.time()
        r = model.predict(frame, imgsz=s["models"]["imgsz"], conf=a.conf, device=dev, verbose=False)[0]
        h, w = frame.shape[:2]
        name = f"{a.scene}_{int(time.time() * 1000)}"
        cv2.imwrite(os.path.join(out, "images", name + ".jpg"), frame)
        with open(os.path.join(out, "labels", name + ".txt"), "w") as f:   # empty file = negative image
            for (x1, y1, x2, y2), k in zip(r.boxes.xyxy.tolist(), r.boxes.cls.tolist()):
                f.write(f"{int(k)} {(x1 + x2) / 2 / w:.6f} {(y1 + y2) / 2 / h:.6f} "
                        f"{(x2 - x1) / w:.6f} {(y2 - y1) / h:.6f}\n")
        saved += 1
        print(f"\r{saved} images", end="")
    cap.release()
    cv2.destroyAllWindows()
    print(f"\ndone: {saved} images in {out}")


if __name__ == "__main__":
    main()
