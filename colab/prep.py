"""SentinelEye v3 data preparation (runs in Colab).

Finds image/label pairs in any downloaded dataset layout, remaps classes to our schema,
resizes huge images once (fast training), adds our industrial-degradation copies
(low light + sensor noise, haze/dust, motion blur) and writes a YOLO dataset + data.yaml.
"""
import glob
import os
import random
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
import yaml

IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

PPE_V3 = ["head", "helmet", "face", "goggles", "mask", "vest", "hand", "gloves", "foot"]
FIRE_V2 = ["Fire", "Smoke"]          # same order as fire_smoke_v1.pt

# Official order from sh17.yaml in github.com/ahmadmughees/SH17dataset (the README lists them differently)
SH17_NAMES = ["person", "ear", "ear-mufs", "face", "face-guard", "face-mask", "foot", "tool", "glasses",
              "gloves", "helmet", "hands", "head", "medical-suit", "shoes", "safety-suit", "safety-vest"]

SH17_TO_V3 = {"head": "head", "helmet": "helmet", "face": "face", "glasses": "goggles",
              "face-mask-medical": "mask", "face-mask": "mask", "hands": "hand", "gloves": "gloves",
              "foot": "foot", "shoes": "foot", "safety-vest": "vest"}
FIRE_TO_V2 = {"fire": "Fire", "flame": "Fire", "smoke": "Smoke"}


def norm(name):
    return re.sub(r"[\s_]+", "-", str(name).strip().lower())


# ---------------------------------------------------------------- discovery
def find_pairs(root):
    """Return [(image_path, label_path_or_None)] for every image under root."""
    labels = {}
    for p in glob.glob(os.path.join(root, "**", "*.txt"), recursive=True):
        stem = os.path.splitext(os.path.basename(p))[0]
        if stem.lower() in ("classes", "readme", "train_files", "test_files", "val_files", "valid_files"):
            continue
        labels.setdefault(stem, []).append(p)
    pairs = []
    for p in glob.glob(os.path.join(root, "**", "*"), recursive=True):
        if os.path.splitext(p)[1].lower() not in IMG_EXT:
            continue
        stem = os.path.splitext(os.path.basename(p))[0]
        cands = labels.get(stem, [])
        lbl = None
        if len(cands) == 1:
            lbl = cands[0]
        elif cands:   # same stem in several folders: pick the one whose path matches best
            img_parts = set(re.split(r"[\\/]", p.replace("images", "labels")))
            lbl = max(cands, key=lambda c: len(img_parts & set(re.split(r"[\\/]", c))))
        pairs.append((p, lbl))
    return pairs


def find_names(root):
    """Class names from the first data.yaml / classes.txt found, else None."""
    for y in sorted(glob.glob(os.path.join(root, "**", "*.yaml"), recursive=True)):
        try:
            d = yaml.safe_load(open(y, encoding="utf-8"))
        except Exception:
            continue
        if isinstance(d, dict) and "names" in d:
            n = d["names"]
            return [n[k] for k in sorted(n)] if isinstance(n, dict) else list(n)
    for c in glob.glob(os.path.join(root, "**", "classes.txt"), recursive=True):
        names = [l.strip() for l in open(c, encoding="utf-8") if l.strip()]
        if names:
            return names
    return None


def split_hint(path):
    p = path.lower().replace("\\", "/")
    if re.search(r"/(test|testing)/", p):
        return "test"
    if re.search(r"/(val|valid|validation)/", p):
        return "val"
    if re.search(r"/(train|training)/", p):
        return "train"
    return None


def split_lists(root):
    """SH17 style train_files.txt / val_files.txt -> {stem: split}."""
    out = {}
    for name, split in (("train_files", "train"), ("val_files", "val"), ("valid_files", "val"), ("test_files", "val")):
        for f in glob.glob(os.path.join(root, "**", name + ".txt"), recursive=True):
            for line in open(f, encoding="utf-8", errors="ignore"):
                line = line.strip()
                if line:
                    out[os.path.splitext(os.path.basename(line))[0]] = split
    return out


# ---------------------------------------------------------------- labels
def read_label(path):
    """YOLO boxes [(cls, cx, cy, w, h)]; polygons converted to boxes."""
    out = []
    if not path or not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8", errors="ignore"):
        v = line.split()
        if len(v) < 5:
            continue
        try:
            c = int(float(v[0]))
            xy = [float(t) for t in v[1:]]
        except ValueError:
            continue
        if len(xy) > 4 and len(xy) % 2 == 0:
            xs, ys = xy[0::2], xy[1::2]
            xy = [(min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, max(xs) - min(xs), max(ys) - min(ys)]
        cx, cy, w, h = xy[:4]
        if w <= 0 or h <= 0:
            continue
        out.append((c, min(max(cx, 0), 1), min(max(cy, 0), 1), min(w, 1), min(h, 1)))
    return out


def iou_cxcywh(a, b):
    ax1, ay1, ax2, ay2 = a[0] - a[2] / 2, a[1] - a[3] / 2, a[0] + a[2] / 2, a[1] + a[3] / 2
    bx1, by1, bx2, by2 = b[0] - b[2] / 2, b[1] - b[3] / 2, b[0] + b[2] / 2, b[1] + b[3] / 2
    iw, ih = max(0, min(ax2, bx2) - max(ax1, bx1)), max(0, min(ay2, by2) - max(ay1, by1))
    inter = iw * ih
    u = a[2] * a[3] + b[2] * b[3] - inter
    return inter / u if u > 0 else 0


def dedupe(boxes, thr=0.7):
    """Same class, heavily overlapping (e.g. SH17 'foot' + 'shoes' on one foot) -> keep the larger."""
    boxes = sorted(boxes, key=lambda b: -b[3] * b[4])
    keep = []
    for b in boxes:
        if all(k[0] != b[0] or iou_cxcywh(k[1:], b[1:]) < thr for k in keep):
            keep.append(b)
    return keep


def build_idmap(src_names, mapping, target):
    idmap = {}
    for i, n in enumerate(src_names):
        t = mapping.get(norm(n))
        if t is not None:
            idmap[i] = target.index(t)
    return idmap


# ---------------------------------------------------------------- degradation (our augmentation)
def low_light(img, rng):
    g = rng.uniform(1.8, 3.2)                                    # gamma darkening keeps light sources bright
    x = np.power(img.astype(np.float32) / 255.0, g)
    x = x * rng.uniform(0.7, 1.0)
    noise = rng.normal(0, rng.uniform(0.01, 0.04), x.shape).astype(np.float32)   # sensor noise
    x = np.clip(x + noise, 0, 1)
    if rng.random() < 0.5:                                       # warm / cold cast of cheap lamps
        x[:, :, 0] *= rng.uniform(0.8, 1.1)
        x[:, :, 2] *= rng.uniform(0.9, 1.2)
    out = (np.clip(x, 0, 1) * 255).astype(np.uint8)
    if rng.random() < 0.5:
        out = cv2.GaussianBlur(out, (3, 3), 0)
    return out


def haze(img, rng):
    a = rng.uniform(0.25, 0.5)
    grey = np.full_like(img, int(rng.uniform(150, 215)))
    out = cv2.addWeighted(img, 1 - a, grey, a, 0)
    return cv2.GaussianBlur(out, (5, 5), 0)


def motion_blur(img, rng):
    k = int(rng.integers(5, 13))
    kern = np.zeros((k, k), np.float32)
    if rng.random() < 0.5:
        kern[k // 2, :] = 1
    else:
        kern[:, k // 2] = 1
    out = cv2.filter2D(img, -1, kern / k)
    q = int(rng.integers(35, 70))                                # cheap-camera JPEG
    ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, q])
    return cv2.imdecode(buf, cv2.IMREAD_COLOR) if ok else out


DEGRADE = {"lowlight": low_light, "haze": haze, "blur": motion_blur}


# ---------------------------------------------------------------- writing
def _write_one(job):
    img_p, lines, out_img, out_lbl, max_side = job
    img = cv2.imread(img_p, cv2.IMREAD_COLOR)
    if img is None:
        return False
    h, w = img.shape[:2]
    s = max_side / max(h, w)
    if s < 1:
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    cv2.imwrite(out_img, img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    with open(out_lbl, "w") as f:
        f.write("\n".join(lines))
    return True


def convert(root, src_names, mapping, target, out, prefix, max_side=1280, default_split=None,
            val_frac=0.12, seed=0, keep_empty=True, workers=8):
    """Remap one downloaded dataset into out/{train,val}/{images,labels}. Returns Counter of written boxes."""
    rng = random.Random(seed)
    idmap = build_idmap(src_names, mapping, target)
    lists = split_lists(root)
    jobs, counts, skipped = [], Counter(), 0
    for split in ("train", "val"):
        os.makedirs(os.path.join(out, split, "images"), exist_ok=True)
        os.makedirs(os.path.join(out, split, "labels"), exist_ok=True)
    for img_p, lbl_p in find_pairs(root):
        stem = os.path.splitext(os.path.basename(img_p))[0]
        split = lists.get(stem) or split_hint(img_p) or default_split
        if split == "test":
            split = "val"
        if split is None:
            split = "val" if rng.random() < val_frac else "train"
        boxes = [(idmap[c], cx, cy, w, h) for c, cx, cy, w, h in read_label(lbl_p) if c in idmap]
        boxes = dedupe(boxes)
        if not boxes and not keep_empty:
            skipped += 1
            continue
        for b in boxes:
            counts[target[b[0]]] += 1
        lines = [f"{b[0]} {b[1]:.6f} {b[2]:.6f} {b[3]:.6f} {b[4]:.6f}" for b in boxes]
        name = f"{prefix}_{stem}"
        jobs.append((img_p, lines, os.path.join(out, split, "images", name + ".jpg"),
                     os.path.join(out, split, "labels", name + ".txt"), max_side))
    with ThreadPoolExecutor(workers) as ex:
        ok = sum(ex.map(_write_one, jobs))
    counts["_images"] += ok
    counts["_skipped_empty"] += skipped
    return counts


def add_degraded(out, frac=0.25, seed=1, kinds=("lowlight", "lowlight", "haze", "blur"), workers=8):
    """Add degraded copies of a fraction of the TRAIN images (labels copied). Our low-light/haze/blur policy."""
    rng = np.random.default_rng(seed)
    imgs = sorted(glob.glob(os.path.join(out, "train", "images", "*.jpg")))
    imgs = [p for p in imgs if "_deg-" not in p]
    pick = [imgs[i] for i in rng.choice(len(imgs), int(len(imgs) * frac), replace=False)] if imgs else []

    def one(args):
        i, p = args
        r = np.random.default_rng(seed * 100003 + i)
        kind = kinds[i % len(kinds)]
        img = cv2.imread(p)
        if img is None:
            return 0
        stem = os.path.splitext(os.path.basename(p))[0]
        cv2.imwrite(os.path.join(out, "train", "images", f"{stem}_deg-{kind}.jpg"), DEGRADE[kind](img, r),
                    [cv2.IMWRITE_JPEG_QUALITY, 90])
        src = os.path.join(out, "train", "labels", stem + ".txt")
        dst = os.path.join(out, "train", "labels", f"{stem}_deg-{kind}.txt")
        with open(src) as a, open(dst, "w") as b:
            b.write(a.read())
        return 1
    with ThreadPoolExecutor(workers) as ex:
        return sum(ex.map(one, enumerate(pick)))


def write_yaml(out, names):
    p = os.path.join(out, "data.yaml")
    yaml.safe_dump({"path": out, "train": "train/images", "val": "val/images",
                    "names": {i: n for i, n in enumerate(names)}}, open(p, "w"), sort_keys=False)
    return p


def stats(out, names):
    res = {}
    for split in ("train", "val"):
        c, n_img, n_empty = Counter(), 0, 0
        for f in glob.glob(os.path.join(out, split, "labels", "*.txt")):
            n_img += 1
            lines = [l for l in open(f) if l.strip()]
            n_empty += not lines
            for l in lines:
                c[names[int(l.split()[0])]] += 1
        res[split] = {"images": n_img, "empty(negatives)": n_empty, **{n: c[n] for n in names}}
    return res


# ---------------------------------------------------------------- checks
def containment(root, src_names, inner, outer, sample=3000):
    """Share of `inner` boxes whose centre lies in an `outer` box (e.g. helmet inside head)."""
    ids = {norm(n): i for i, n in enumerate(src_names)}
    if norm(inner) not in ids or norm(outer) not in ids:
        return None
    a, b = ids[norm(inner)], ids[norm(outer)]
    tot = hit = 0
    for _, lbl in find_pairs(root)[:sample]:
        bx = read_label(lbl)
        outs = [x for x in bx if x[0] == b]
        for x in bx:
            if x[0] != a:
                continue
            tot += 1
            if any(abs(x[1] - o[1]) <= o[3] / 2 and abs(x[2] - o[2]) <= o[4] / 2 for o in outs):
                hit += 1
    return (hit / tot if tot else None), tot


def guess_fire_names(root, sample=400):
    """No class names shipped: the class whose boxes are redder/brighter is fire."""
    score = {}
    for img_p, lbl in find_pairs(root):
        if len(score) and sum(len(v) for v in score.values()) > sample:
            break
        bx = read_label(lbl)
        if not bx:
            continue
        img = cv2.imread(img_p)
        if img is None:
            continue
        H, W = img.shape[:2]
        for c, cx, cy, w, h in bx:
            x1, y1 = int((cx - w / 2) * W), int((cy - h / 2) * H)
            x2, y2 = int((cx + w / 2) * W), int((cy + h / 2) * H)
            roi = img[max(0, y1):y2, max(0, x1):x2]
            if roi.size == 0:
                continue
            b, g, r = [roi[:, :, i].astype(np.float32).mean() for i in range(3)]
            hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
            score.setdefault(c, []).append((r - b) + 0.5 * hsv[:, :, 1].mean())
    means = {c: float(np.mean(v)) for c, v in score.items()}
    if len(means) < 2:
        return None, means
    fire_id = max(means, key=means.get)
    names = ["smoke"] * (max(means) + 1)
    names[fire_id] = "fire"
    return names, means


# ---------------------------------------------------------------- stage B helpers
CSS_TO_V3 = {"hardhat": "helmet", "mask": "mask", "safety-vest": "vest"}
OZHFB_TO_V3 = {"hard-hat": "helmet", "mask": "mask", "vest": "vest", "gloves": "gloves", "safety-boots": "foot"}


def _box_iou_xyxy(a, b):
    iw = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    ih = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = iw * ih
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / u if u > 0 else 0.0


def _xyxy(cx, cy, w, h):
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def pseudo_label(out, prefix, model, add_classes, target, conf=0.5, imgsz=640, batch=32):
    """Fill classes a public dataset never labelled (e.g. head/hand/foot/goggles in a helmet dataset)
    with predictions of our stage-A model. Original labels are kept; a predicted box is skipped if it
    overlaps an existing box of the same class, and a predicted bare 'hand' is skipped where a 'gloves'
    box already is (a gloved hand is not a bare hand)."""
    add_ids = {target.index(c) for c in add_classes}
    names = model.names
    gl = target.index("gloves") if "gloves" in target else -1
    added = Counter()
    for split in ("train", "val"):
        imgs = sorted(glob.glob(os.path.join(out, split, "images", f"{prefix}_*.jpg")))
        for i in range(0, len(imgs), batch):
            chunk = imgs[i:i + batch]
            for img_p, r in zip(chunk, model.predict(chunk, imgsz=imgsz, conf=conf, verbose=False)):
                lbl = os.path.join(out, split, "labels", os.path.splitext(os.path.basename(img_p))[0] + ".txt")
                have = read_label(lbl)
                h, w = r.orig_shape
                new = []
                for (x1, y1, x2, y2), k in zip(r.boxes.xyxy.tolist(), r.boxes.cls.tolist()):
                    n = names[int(k)]
                    if n not in target or target.index(n) not in add_ids:
                        continue
                    c = target.index(n)
                    nb = (x1 / w, y1 / h, x2 / w, y2 / h)
                    if any(b[0] == c and _box_iou_xyxy(nb, _xyxy(*b[1:])) > 0.3 for b in have):
                        continue
                    if n == "hand" and any(b[0] == gl and _box_iou_xyxy(nb, _xyxy(*b[1:])) > 0.2 for b in have):
                        continue
                    new.append((c, (nb[0] + nb[2]) / 2, (nb[1] + nb[3]) / 2, nb[2] - nb[0], nb[3] - nb[1]))
                    added[n] += 1
                if new:
                    with open(lbl, "a") as f:
                        for b in new:
                            f.write(("\n" if os.path.getsize(lbl) else "") + f"{b[0]} {b[1]:.6f} {b[2]:.6f} {b[3]:.6f} {b[4]:.6f}")
    return added


def add_own(own_root, out, repeat=3, prefix="own"):
    """Our own labelled scenes (tools/build_dataset.py output: train/val/test). Train images are copied
    `repeat` times so they weigh more; val goes to val; test is kept apart as the HARD TEST set."""
    import shutil as _sh
    counts = Counter()
    for split in ("train", "val", "test"):
        src_i = os.path.join(own_root, split, "images")
        if not os.path.isdir(src_i):
            continue
        dst_split = "hardtest" if split == "test" else split
        os.makedirs(os.path.join(out, dst_split, "images"), exist_ok=True)
        os.makedirs(os.path.join(out, dst_split, "labels"), exist_ok=True)
        for img in sorted(os.listdir(src_i)):
            stem, ext = os.path.splitext(img)
            lab = os.path.join(own_root, split, "labels", stem + ".txt")
            reps = repeat if split == "train" else 1
            for r in range(reps):
                name = f"{prefix}{r}_{stem}" if reps > 1 else f"{prefix}_{stem}"
                _sh.copy(os.path.join(src_i, img), os.path.join(out, dst_split, "images", name + ext))
                if os.path.exists(lab):
                    _sh.copy(lab, os.path.join(out, dst_split, "labels", name + ".txt"))
                else:
                    open(os.path.join(out, dst_split, "labels", name + ".txt"), "w").close()
                counts[dst_split] += 1
    return counts


def write_hard_yaml(out, names):
    p = os.path.join(out, "hardtest.yaml")
    yaml.safe_dump({"path": out, "train": "train/images", "val": "hardtest/images",
                    "names": {i: n for i, n in enumerate(names)}}, open(p, "w"), sort_keys=False)
    return p
