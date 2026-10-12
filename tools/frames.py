"""Own data: videos -> frames -> pre-labels -> Roboflow -> back into scene folders.

  python -m tools.frames extract --task ppe            # all videos in datasets/raw_videos/ppe/
  python -m tools.frames extract --task fire --fps 2   # 2 frames per second
  python -m tools.frames pack --task ppe               # zip of scenes that need labelling -> Roboflow
  python -m tools.frames import --task ppe --src <Roboflow YOLOv8 export folder or .zip>
  python -m tools.frames status                        # what is extracted / labelled so far
  python -m tools.frames split                         # write datasets/splits.yaml (train / val / hard test BY SCENE)

One video = one scene. The file name (without extension) is the scene name, e.g.
datasets/raw_videos/ppe/ppe_04_gloves_off_day.mp4 -> datasets/own/ppe/ppe_04_gloves_off_day/.
Scenes whose name starts with 'neg_' are negatives (no fire, no smoke): they get EMPTY labels and
need no labelling at all. Near-identical frames are skipped so the team does not label duplicates.

Pre-labels (to correct, not to trust):
  ppe  -> ppe_v2 for helmet / vest / mask + ppe_aux (settings.yaml, now ppe_v3B) for head, face, goggles, hand, gloves, foot
  fire -> settings.yaml 'fire' (now fire_smoke_v2B, falls back to v2A)
"""
import argparse
import glob
import os
import re
import shutil
import tempfile
import zipfile

import cv2
import numpy as np
import yaml

from edge.config import path

V3 = ["head", "helmet", "face", "goggles", "mask", "vest", "hand", "gloves", "foot"]
FIRE = ["Fire", "Smoke"]
NAMES = {"ppe": V3, "fire": FIRE}
VIDEO_EXT = (".mp4", ".mov", ".avi", ".mkv", ".3gp", ".webm", ".m4v")
ALIAS = {"boots": "foot", "safety_boots": "foot", "hardhat": "helmet", "safety vest": "vest", "glasses": "goggles"}


def scene_dir(task, scene):
    return path("datasets", "own", task, scene)


# ------------------------------------------------------------------ extract
def _frames(video, fps, max_side, min_change):
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        raise SystemExit(f"cannot open {video}")
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step = max(1, int(round(src_fps / fps)))
    i, last = 0, None
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if i % step == 0:
            h, w = frame.shape[:2]
            s = max_side / max(h, w)
            if s < 1:
                frame = cv2.resize(frame, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
            small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (64, 36)).astype(np.float32)
            if last is None or float(np.abs(small - last).mean()) >= min_change:
                last = small
                yield i, frame
        i += 1
    cap.release()


class _Prelabeler:
    def __init__(self, task):
        from ultralytics import YOLO
        from edge.config import load_all
        from edge.detector import pick_device
        s, _, _ = load_all()
        m = s["models"]
        self.dev = pick_device(m.get("device", "auto"))
        self.imgsz = m.get("imgsz", 640)
        self.models = []
        if task == "ppe":
            main = path(m["ppe"])
            aux = path(m["ppe_aux"]) if m.get("ppe_aux") else None
            aux_items = set(m.get("aux_items") or [])
            if aux and os.path.exists(aux):
                self.models.append((YOLO(main), lambda n: n not in aux_items))
                self.models.append((YOLO(aux), lambda n: n in aux_items))
            else:
                print(f"{m['ppe_aux']} not in models/ - pre-labels only for helmet / vest / gloves / boots / mask")
                self.models.append((YOLO(main), lambda n: True))
        else:
            f = path(m["fire"]) if os.path.exists(path(m["fire"])) else path(m.get("fire_fallback", m["fire"]))
            self.models.append((YOLO(f), lambda n: True))
        self.names = NAMES[task]

    def __call__(self, frame, conf):
        h, w = frame.shape[:2]
        lines = []
        for model, keep in self.models:
            r = model.predict(frame, imgsz=self.imgsz, conf=conf, device=self.dev, verbose=False)[0]
            for (x1, y1, x2, y2), k in zip(r.boxes.xyxy.tolist(), r.boxes.cls.tolist()):
                n = model.names[int(k)]
                n = ALIAS.get(n.lower(), n)
                if n not in self.names or not keep(n):
                    continue
                lines.append(f"{self.names.index(n)} {(x1 + x2) / 2 / w:.6f} {(y1 + y2) / 2 / h:.6f} "
                             f"{(x2 - x1) / w:.6f} {(y2 - y1) / h:.6f}")
        return lines


def extract(a):
    vids = sorted(p for p in glob.glob(path("datasets", "raw_videos", a.task, "*")) if p.lower().endswith(VIDEO_EXT))
    if a.scene:
        vids = [v for v in vids if os.path.splitext(os.path.basename(v))[0] == a.scene]
    if not vids:
        raise SystemExit(f"no videos in datasets/raw_videos/{a.task}/")
    pre = None if a.no_prelabel else _Prelabeler(a.task)
    for v in vids:
        scene = re.sub(r"[^A-Za-z0-9_-]+", "_", os.path.splitext(os.path.basename(v))[0])
        out = scene_dir(a.task, scene)
        if os.path.isdir(os.path.join(out, "images")) and not a.overwrite:
            print(f"skip {scene} (already extracted, --overwrite to redo)")
            continue
        shutil.rmtree(out, ignore_errors=True)
        os.makedirs(os.path.join(out, "images"))
        os.makedirs(os.path.join(out, "labels"))
        negative = scene.lower().startswith("neg_")
        n = boxes = 0
        for i, frame in _frames(v, a.fps, a.max_side, a.min_change):
            stem = f"{scene}__f{i:05d}"
            cv2.imwrite(os.path.join(out, "images", stem + ".jpg"), frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
            lines = [] if (negative or pre is None) else pre(frame, a.conf)
            boxes += len(lines)
            with open(os.path.join(out, "labels", stem + ".txt"), "w") as f:
                f.write("\n".join(lines))
            n += 1
        tag = "negative: no labelling needed" if negative else f"{boxes} pre-label boxes to check"
        print(f"{scene}: {n} frames ({tag})")


# ------------------------------------------------------------------ pack for Roboflow
def pack(a):
    root = path("datasets", "own", a.task)
    scenes = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)) and not d.lower().startswith("neg_"))
    if a.scenes:
        scenes = [s for s in scenes if s in a.scenes.split(",")]
    os.makedirs(path("datasets", "upload"), exist_ok=True)
    zpath = path("datasets", "upload", f"{a.task}_to_label.zip")
    n = 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("data.yaml", yaml.safe_dump({"train": "train/images", "val": "train/images",
                                                "nc": len(NAMES[a.task]), "names": NAMES[a.task]}, sort_keys=False))
        for sc in scenes:
            for img in sorted(glob.glob(os.path.join(root, sc, "images", "*.jpg"))):
                stem = os.path.splitext(os.path.basename(img))[0]
                z.write(img, f"train/images/{stem}.jpg")
                lab = os.path.join(root, sc, "labels", stem + ".txt")
                if os.path.exists(lab):
                    z.write(lab, f"train/labels/{stem}.txt")
                n += 1
    print(f"{n} images from {len(scenes)} scenes -> {zpath}")
    print("Roboflow: New project (Object Detection) -> Upload -> drop this zip -> it keeps the pre-labels.")


# ------------------------------------------------------------------ import labels back
_RF = re.compile(r"^(?P<stem>.+?__f\d{5})(?:_(?:jpg|jpeg|png))?(?:\.rf\.[0-9a-f]+)?$", re.I)


def _names_from(root):
    for y in glob.glob(os.path.join(root, "**", "data.yaml"), recursive=True):
        d = yaml.safe_load(open(y, encoding="utf-8"))
        n = d.get("names")
        return [n[k] for k in sorted(n)] if isinstance(n, dict) else list(n)
    raise SystemExit("no data.yaml in the export")


def import_labels(a):
    src = a.src
    tmp = None
    if src.lower().endswith(".zip"):
        tmp = tempfile.mkdtemp()
        zipfile.ZipFile(src).extractall(tmp)
        src = tmp
    exp_names = _names_from(src)
    ours = NAMES[a.task]
    remap = {}
    for i, n in enumerate(exp_names):
        n2 = ALIAS.get(str(n).lower(), n)
        if n2 not in ours:
            print(f"warning: class '{n}' in the export is not in our list - its boxes are dropped")
            continue
        remap[i] = ours.index(n2)
    root = path("datasets", "own", a.task)
    done = missing = 0
    for lab in glob.glob(os.path.join(src, "**", "labels", "*.txt"), recursive=True):
        m = _RF.match(os.path.splitext(os.path.basename(lab))[0])
        if not m:
            continue
        stem = m.group("stem")
        scene = stem.rsplit("__f", 1)[0]
        dst = os.path.join(root, scene, "labels", stem + ".txt")
        if not os.path.isdir(os.path.dirname(dst)):
            missing += 1
            continue
        out = []
        for line in open(lab, encoding="utf-8"):
            v = line.split()
            if len(v) >= 5 and int(v[0]) in remap:
                xy = [float(t) for t in v[1:]]
                if len(xy) > 4:                                  # polygon -> box
                    xs, ys = xy[0::2], xy[1::2]
                    xy = [(min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, max(xs) - min(xs), max(ys) - min(ys)]
                out.append(f"{remap[int(v[0])]} " + " ".join(f"{t:.6f}" for t in xy[:4]))
        with open(dst, "w") as f:
            f.write("\n".join(out))
        done += 1
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"imported {done} label files into datasets/own/{a.task}/<scene>/labels" +
          (f"; {missing} files had no matching scene folder" if missing else ""))


# ------------------------------------------------------------------ split
# Hard test = scenes never trained on (the "hard" numbers for the judges). Validation = picks the best epoch.
HARD = {"ppe": ["ppe_02_fullkit_dim", "ppe_17_two_in_line", "ppe_22_backlit"],
        "fire": ["fire_02_lighter", "neg_04_white_tshirt"]}
VAL = {"ppe": ["ppe_06_vest_off", "ppe_21_far"], "fire": ["neg_06_phone_flash"]}
# phone screen showing fire: the camera SHOULD still see fire (the hardware flame sensor decides) -> never trained on
EXCLUDE = {"fire": ["neg_13_phone_screen_fire"]}


def split(_a):
    out = {}
    for task in ("ppe", "fire"):
        root = path("datasets", "own", task)
        scenes = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d, "images"))) \
            if os.path.isdir(root) else []
        ex = set(EXCLUDE.get(task, []))
        test = [s_ for s_ in scenes if s_ in HARD[task]]
        val = [s_ for s_ in scenes if s_ in VAL[task]]
        train = [s_ for s_ in scenes if s_ not in test and s_ not in val and s_ not in ex]
        out[task] = {"train": train, "val": val, "test": test}
        print(f"[{task}] train {len(train)} scenes, val {len(val)}, hard test {len(test)}"
              + (f", left out: {', '.join(sorted(ex & set(scenes)))}" if ex & set(scenes) else ""))
    with open(path("datasets", "splits.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump(out, f, sort_keys=False)
    print("-> datasets/splits.yaml  (edit it if you want a different split; one scene = one split)")


# ------------------------------------------------------------------ status
def status(_a):
    for task in ("ppe", "fire"):
        root = path("datasets", "own", task)
        vids = [p for p in glob.glob(path("datasets", "raw_videos", task, "*")) if p.lower().endswith(VIDEO_EXT)]
        print(f"[{task}] videos: {len(vids)}")
        if not os.path.isdir(root):
            continue
        for sc in sorted(os.listdir(root)):
            d = os.path.join(root, sc)
            if not os.path.isdir(os.path.join(d, "images")):
                continue
            n = len(os.listdir(os.path.join(d, "images")))
            boxes = sum(len([l for l in open(f) if l.strip()]) for f in glob.glob(os.path.join(d, "labels", "*.txt")))
            print(f"  {sc:40s} {n:4d} frames {boxes:6d} boxes" + ("  (negative)" if sc.lower().startswith("neg_") else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract")
    e.add_argument("--task", choices=["ppe", "fire"], required=True)
    e.add_argument("--scene", help="only this scene (video file name without extension)")
    e.add_argument("--fps", type=float, default=1.0, help="frames per second of video to keep")
    e.add_argument("--max-side", type=int, default=1280)
    e.add_argument("--min-change", type=float, default=4.0, help="skip frames almost identical to the last kept one")
    e.add_argument("--conf", type=float, default=0.30, help="pre-label threshold (low: deleting is faster than drawing)")
    e.add_argument("--no-prelabel", action="store_true")
    e.add_argument("--overwrite", action="store_true")
    p = sub.add_parser("pack")
    p.add_argument("--task", choices=["ppe", "fire"], required=True)
    p.add_argument("--scenes", help="comma-separated subset")
    i = sub.add_parser("import")
    i.add_argument("--task", choices=["ppe", "fire"], required=True)
    i.add_argument("--src", required=True)
    sub.add_parser("status")
    sub.add_parser("split")
    a = ap.parse_args()
    {"extract": extract, "pack": pack, "import": import_labels, "status": status, "split": split}[a.cmd](a)


if __name__ == "__main__":
    main()
