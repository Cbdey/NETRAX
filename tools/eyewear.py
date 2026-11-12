"""Safety goggles vs normal glasses - second-stage check (edge/eyewear.py). Stop NETRAX first (GPU).

  python -m tools.eyewear fetch                      # CLIP ViT-B-32 weights -> models/clip_vit_b32_laion2b.safetensors (605 MB)
  python -m tools.eyewear check <image|folder|video> [--out DIR]
        every 'goggles' box the detector finds -> p(safety eyewear) + verdict (goggles / unclear / NORMAL GLASSES),
        annotated pictures in --out (default data/eyewear_check/)
  python -m tools.eyewear train [--neg VIDEO_OR_DIR ...] [--pos VIDEO_OR_DIR ...]
        rebuilds models/eyewear_head.npz (old one kept as .bak) and prints held-out numbers:
          positives  = 'goggles' boxes on our own labelled frames (datasets/own/ppe, label 'goggles')
          negatives  = sunglasses / bare faces in our own scenes, + public eyeglasses photos (Hugging Face
                       giqu/glasses_celeb, downloaded once to datasets/external/glasses_celeb) pasted at CCTV size
                       into our own room frames and degraded like our cameras
          --neg      = OUR OWN video / pictures of people wearing NORMAL spectacles (best improvement: 30 s per person,
                       walk towards the camera, turn the head) -> every goggles box in them is a negative
          --pos      = video / pictures of other SAFETY goggles (e.g. strap goggles) -> every goggles box is a positive
Measured 12 Nov (whole scenes / identities held out): real safety goggles called 'normal glasses' 0/151; spectacles the
detector called goggles -> normal glasses 150/150; END-TO-END (live detector + association) on 186 never-seen spectacles
faces pasted onto our workers: accepted as goggles 60 -> 1.
"""
import argparse
import glob
import os
import random
import shutil
import sys

sys.path.insert(0, os.getcwd())
os.environ.setdefault("YOLO_OFFLINE", "1")
import cv2
import numpy as np

from edge.config import load_all, path

ARCH, PRE_HF = "ViT-B-32", "laion2b_s34b_b79k"
LABEL_GOGGLES = 3          # v3 class list: head, helmet, face, goggles, ...
NO_GOGGLE_SCENES = ("ppe_05", "ppe_07", "ppe_08", "ppe_09", "ppe_10", "ppe_14", "ppe_15", "ppe_21")
SUN_SCENES = ("ppe_08", "ppe_09")
PROMPTS = {
    "goggles": ["a photo of a person wearing clear safety goggles", "a photo of a worker wearing protective safety glasses",
                "a photo of a face with wraparound industrial safety eyewear", "a photo of a person wearing lab safety goggles"],
    "spectacles": ["a photo of a person wearing prescription eyeglasses", "a photo of a person wearing spectacles with thin frames",
                   "a photo of a person wearing reading glasses", "a photo of a person wearing fashion eyeglasses"],
    "sunglasses": ["a photo of a person wearing sunglasses", "a photo of a person wearing dark tinted sunglasses"],
    "none": ["a photo of a face without glasses", "a close-up photo of a person's eyes with no eyewear"],
}


def settings():
    return load_all()[0]


def cmd_fetch(_a):
    m = settings()["models"]
    dst = path(m.get("eyewear_clip") or "models/clip_vit_b32_laion2b.safetensors")
    if os.path.exists(dst):
        print("already there:", dst)
        return
    from huggingface_hub import hf_hub_download
    src = hf_hub_download("laion/CLIP-ViT-B-32-laion2B-s34B-b79K", "open_clip_model.safetensors")
    shutil.copy(src, dst)
    print("saved", dst)


def media(src, step_s=0.5):
    """Frames from an image, a folder of images or a video (one every step_s)."""
    if os.path.isdir(src):
        for p in sorted(glob.glob(os.path.join(src, "*"))):
            if p.lower().endswith((".jpg", ".jpeg", ".png")):
                im = cv2.imread(p)
                if im is not None:
                    yield os.path.basename(p), im
        return
    if src.lower().endswith((".jpg", ".jpeg", ".png")):
        yield os.path.basename(src), cv2.imread(src)
        return
    cap = cv2.VideoCapture(src)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    k, every = 0, max(1, int(round(fps * step_s)))
    while True:
        ok, im = cap.read()
        if not ok:
            break
        if k % every == 0:
            yield f"{os.path.basename(src)}@{k / fps:.1f}s", im
        k += 1


def goggles_boxes(aux, im, conf):
    r = aux.predict(im, imgsz=640, conf=conf, verbose=False)[0]
    return [(b, c) for b, c, k in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist(), r.boxes.cls.tolist())
            if aux.names[int(k)] == "goggles"]


def cmd_check(a):
    from ultralytics import YOLO
    from edge.eyewear import load, eye_crop
    S = settings()
    e = load(S["models"], 0)
    if e is None:
        raise SystemExit("eyewear check is off / weights missing (python -m tools.eyewear fetch)")
    aux = YOLO(path(S["models"]["ppe_aux"]))
    thr = S["thresholds"]["ppe_conf"].get("goggles", 0.45)
    out = a.out or path("data", "eyewear_check")
    os.makedirs(out, exist_ok=True)
    tally = {"goggles": 0, "unclear": 0, "normal_glasses": 0}
    for name, im in media(a.src):
        bx = goggles_boxes(aux, im, thr)
        ps = e.probs([eye_crop(im, b) for b, _ in bx])
        for (b, c), p in zip(bx, ps):
            v = e.verdict(p)
            tally[v] += 1
            col = (0, 200, 0) if v == "goggles" else ((0, 200, 255) if v == "unclear" else (0, 0, 255))
            x1, y1, x2, y2 = map(int, b)
            cv2.rectangle(im, (x1, y1), (x2, y2), col, 2)
            cv2.putText(im, f"{v.replace('_', ' ').upper()} {p:.2f}", (x1, max(14, y1 - 6)), 0, 0.6, col, 2)
            print(f"{name}: detector goggles {c:.2f} -> p(safety) {p:.2f} = {v}")
        if bx:
            cv2.imwrite(os.path.join(out, name.replace("@", "_").replace("/", "_") + ".jpg"), im)
    print("total:", tally, "| pictures:", out)


# ------------------------------------------------------------------ training
def _crop(im, b):
    from edge.eyewear import eye_crop
    return eye_crop(im, b)


def _degrade(img):
    if random.random() < 0.5:                                   # washed-out USB / phone look
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.float32)
        hsv[..., 1] *= random.uniform(0.4, 0.9)
        hsv[..., 2] = hsv[..., 2] * random.uniform(0.6, 1.0) + random.uniform(0, 60)
        img = cv2.cvtColor(np.clip(hsv, 0, 255).astype(np.uint8), cv2.COLOR_HSV2BGR)
    if random.random() < 0.3:                                   # dim room
        img = np.clip(img.astype(np.float32) * random.uniform(0.35, 0.7), 0, 255).astype(np.uint8)
    s = random.uniform(0, 1.3)
    if s > 0.3:
        img = cv2.GaussianBlur(img, (0, 0), s)
    _, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, random.randint(35, 85)])
    return cv2.imdecode(enc, 1)


def _paste_rows(aux, faces, walls, reps, label_of):
    rows = []
    for fi, (face, kind, grp) in enumerate(faces):
        for _ in range(reps):
            canvas = random.choice(walls).copy()
            side = random.randint(90, 330)
            x0, y0 = random.randint(0, 1280 - side), random.randint(0, 720 - side)
            canvas[y0:y0 + side, x0:x0 + side] = cv2.resize(face, (side, side), interpolation=cv2.INTER_AREA)
            canvas = _degrade(canvas)
            for b, c in goggles_boxes(aux, canvas, 0.05):
                cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
                if x0 <= cx <= x0 + side and y0 <= cy <= y0 + side:
                    rows.append((_crop(canvas, b), label_of(kind), kind, grp, c))
    return rows


def _fit(X, t, l2=1e-2):
    import torch
    X = torch.tensor(X, dtype=torch.float32); t = torch.tensor(t, dtype=torch.float32)
    w = torch.zeros(X.shape[1], requires_grad=True); b = torch.zeros(1, requires_grad=True)
    opt = torch.optim.LBFGS([w, b], max_iter=400, line_search_fn="strong_wolfe")
    pw = torch.where(t > 0, (1 - t).sum() / max(1, t.sum()), torch.tensor(1.0))

    def closure():
        opt.zero_grad()
        loss = torch.nn.functional.binary_cross_entropy_with_logits(X @ w * 10 + b, t, weight=pw) + l2 * (w ** 2).sum()
        loss.backward()
        return loss
    opt.step(closure)
    return w.detach().numpy(), float(b.detach())


def _prob(X, wb):
    return 1 / (1 + np.exp(-(X @ wb[0] * 10 + wb[1])))


def cmd_train(a):
    import torch
    import open_clip
    from PIL import Image
    from ultralytics import YOLO
    random.seed(0); np.random.seed(0)
    S = settings()
    m = S["models"]
    aux = YOLO(path(m["ppe_aux"]))
    lo, hi = float(m.get("eyewear_lo", 0.2)), float(m.get("eyewear_hi", 0.5))
    rows = []      # crop, label, kind, group, det conf
    # 1) own labelled frames
    for p in sorted(glob.glob(path("datasets", "own", "ppe", "*", "images", "*.jpg"))):
        scene = p.replace("\\", "/").split("/")[-3]
        im = cv2.imread(p); H, W = im.shape[:2]
        gt = []
        for line in open(p.replace("images", "labels")[:-4] + ".txt"):
            c, x, y, w, h = map(float, line.split()[:5])
            if int(c) == LABEL_GOGGLES:
                gt.append([(x - w / 2) * W, (y - h / 2) * H, (x + w / 2) * W, (y + h / 2) * H])
        for b, c in goggles_boxes(aux, im, 0.15):
            cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            pos = any(g[0] - 10 <= cx <= g[2] + 10 and g[1] - 10 <= cy <= g[3] + 10 for g in gt)
            if pos:
                rows.append((_crop(im, b), 1, "own_goggles", scene, c))
            elif scene[:6] in NO_GOGGLE_SCENES:               # only scenes with NO goggles at all (labels elsewhere are noisy)
                rows.append((_crop(im, b), 0, "own_sunglasses" if scene[:6] in SUN_SCENES else "own_bare", scene, c))
    # 2) public eyeglasses photos + our own faces, pasted at CCTV size into our room frames
    ext = path("datasets", "external", "glasses_celeb")
    if not glob.glob(os.path.join(ext, "glasses", "*.jpg")):
        from huggingface_hub import snapshot_download
        snapshot_download("giqu/glasses_celeb", repo_type="dataset", local_dir=ext)
    walls = [cv2.resize(cv2.imread(p), (1280, 720)) for p in sorted(glob.glob(path("datasets", "own", "ppe", "ppe_1[45]*", "images", "*.jpg")))[::4]]
    celeb = [(cv2.imread(p), "celeb_glasses", f"celeb{i}") for i, p in enumerate(sorted(glob.glob(os.path.join(ext, "glasses", "glasses", "*.jpg"))))]
    celeb += [(cv2.imread(p), "celeb_noglasses", f"celebn{i}") for i, p in enumerate(sorted(glob.glob(os.path.join(ext, "without glasses", "without glasses", "*.jpg"))))]
    rows += _paste_rows(aux, [f for f in celeb if f[1] == "celeb_glasses"], walls, 4, lambda k: 0)
    rows += _paste_rows(aux, [f for f in celeb if f[1] == "celeb_noglasses"], walls, 1, lambda k: 0)
    own_faces = []
    for p in sorted(glob.glob(path("datasets", "own", "ppe", "*", "images", "*.jpg"))):
        scene = p.replace("\\", "/").split("/")[-3]
        im = cv2.imread(p); H, W = im.shape[:2]
        L = [list(map(float, l.split()[:5])) for l in open(p.replace("images", "labels")[:-4] + ".txt")]
        gg = [l for l in L if int(l[0]) == LABEL_GOGGLES]
        for c, x, y, w, h in L:
            if int(c) != 2 or w * W < 50:
                continue
            has = any(abs(g[1] - x) < w and abs(g[2] - y) < h for g in gg)
            if not (has or scene[:6] in SUN_SCENES):           # pasted bare faces made the head reject blurry goggles
                continue
            s = max(w * W, h * H) / 0.45 / 2
            pad = cv2.copyMakeBorder(im, 600, 600, 600, 600, cv2.BORDER_REPLICATE)
            sq = pad[int(y * H - s) + 600:int(y * H + s) + 600, int(x * W - s) + 600:int(x * W + s) + 600]
            own_faces.append((cv2.resize(sq, (250, 250)), "paste_goggles" if has else "paste_sunglasses", scene))
    rows += _paste_rows(aux, own_faces, walls, 3, lambda k: int(k == "paste_goggles"))
    # 3) the team's extra videos
    for src, lab in [(s, 0) for s in a.neg or []] + [(s, 1) for s in a.pos or []]:
        n0 = len(rows)
        for name, im in media(src):
            for b, c in goggles_boxes(aux, im, 0.15):
                rows.append((_crop(im, b), lab, "extra_neg" if lab == 0 else "extra_pos", os.path.basename(src), c))
        print(f"{src}: {len(rows) - n0} goggles boxes as {'NEGATIVES (normal glasses)' if lab == 0 else 'POSITIVES'}")
    print("crops:", {k: sum(1 for r in rows if r[2] == k) for k in sorted(set(r[2] for r in rows))})
    # 4) CLIP features + zero-shot prior
    model, _, prep = open_clip.create_model_and_transforms(ARCH, pretrained=path(m["eyewear_clip"]), device="cuda")
    tok = open_clip.get_tokenizer(ARCH)
    model.eval()
    feats = []
    with torch.no_grad():
        T = torch.stack([torch.nn.functional.normalize(torch.nn.functional.normalize(
            model.encode_text(tok(v).cuda()), dim=-1).mean(0), dim=-1) for v in PROMPTS.values()]).float().cpu().numpy()
        for i in range(0, len(rows), 64):
            x = torch.stack([prep(Image.fromarray(cv2.cvtColor(r[0], cv2.COLOR_BGR2RGB))) for r in rows[i:i + 64]]).cuda()
            feats.append(torch.nn.functional.normalize(model.encode_image(x), dim=-1).float().cpu().numpy())
    F = np.vstack(feats)
    Z = 100 * F @ T.T
    Z = np.exp(Z - Z.max(1, keepdims=True)); Z /= Z.sum(1, keepdims=True)
    y = np.array([r[1] for r in rows]); kind = np.array([r[2] for r in rows]); group = np.array([r[3] for r in rows])
    conf = np.array([r[4] for r in rows])
    # own GT 'goggles' that are clearly sunglasses (ppe_19 labels) -> negative
    fix = (kind == "own_goggles") & (Z[:, 2] > 0.5) & (group == "ppe_19_group_mixed")
    y[fix] = 0
    kind = np.where(fix, "own_sunglasses", kind)
    X = np.hstack([F, Z])
    # 5) held-out check: whole scenes / identities / videos out
    grp = sorted(set(group))
    fold = np.array([grp.index(g) % 5 for g in group])
    pr = np.zeros(len(y))
    for k in range(5):
        pr[fold == k] = _prob(X[fold == k], _fit(X[fold != k], y[fold != k]))
    live = conf >= S["thresholds"]["ppe_conf"].get("goggles", 0.45)
    print(f"HELD-OUT (detector conf >= live threshold), lo {lo} hi {hi}:")
    for k in sorted(set(kind)):
        mm = live & (kind == k)
        if mm.sum():
            print(f"  {k:18s} n={int(mm.sum()):4d}  goggles {int((pr[mm] >= hi).sum()):4d}  unclear "
                  f"{int(((pr[mm] >= lo) & (pr[mm] < hi)).sum()):4d}  normal glasses {int((pr[mm] < lo).sum()):4d}")
    wb = _fit(X, y)
    dst = path(m["eyewear_head"])
    if os.path.exists(dst):
        shutil.copy(dst, dst + ".bak")
    np.savez(dst, w=wb[0], b=wb[1], T=T)
    print("saved", dst, "(previous: .bak) - restart NETRAX to use it")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    c = sub.add_parser("check"); c.add_argument("src"); c.add_argument("--out")
    t = sub.add_parser("train"); t.add_argument("--neg", nargs="*"); t.add_argument("--pos", nargs="*")
    a = ap.parse_args()
    {"fetch": cmd_fetch, "check": cmd_check, "train": cmd_train}[a.cmd](a)


if __name__ == "__main__":
    main()
