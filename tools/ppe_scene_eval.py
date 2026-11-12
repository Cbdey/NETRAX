"""PPE decision quality per scene (12 Nov) - LIVE pipeline (detector + association + validation + cap check) on our own
Largest person per frame. Truth from labels; boots truth from the scene (14 = normal shoes, 15 = safety shoes).
  OK      = worn & called worn, or not worn & called missing
  FALSE   = worn but called MISSING (false alarm)
  MISSED  = not worn but called WORN (violation missed - e.g. cap accepted as helmet)
  UNSURE  = 'unverifiable' (no alarm either way)
usage: python -m tools.ppe_scene_eval [tag] [json-out]   (stop NETRAX first: it uses the GPU)
HELMET_MEM=0 = without the cap-vs-helmet memory. EYEWEAR=0 = without the safety-goggles-vs-normal-glasses check. Frames of one scene are fed in order, 1 s apart."""
import glob, os, sys, collections, json, time
sys.path.insert(0, os.getcwd())
os.environ["YOLO_OFFLINE"] = "1"
import cv2
from edge.config import load_all
from edge.detector import Detector
from edge.association import evaluate

S = load_all()[0]
if os.environ.get("EYEWEAR", "1") == "0":            # EYEWEAR=0 = without the goggles-vs-normal-glasses check
    S["models"]["eyewear_head"] = ""
det = Detector(S)
V = S.get("validation", {})
NAMES = ["head", "helmet", "face", "goggles", "mask", "vest", "hand", "gloves", "foot"]
ITEMS = ["helmet", "vest", "gloves", "goggles", "boots"]
BARE = {"helmet": "head", "gloves": "hand", "goggles": "face"}


def inside(b, p, pad=0.15):
    w, h = p[2] - p[0], p[3] - p[1]
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    return p[0] - pad * w <= cx <= p[2] + pad * w and p[1] - pad * h <= cy <= p[3] + pad * h


res = collections.defaultdict(collections.Counter)
times = []
MEM = os.environ.get("HELMET_MEM", "1") == "1"
mem, cur_scene, tick = {}, None, 0.0
for path in sorted(glob.glob("datasets/own/ppe/*/images/*.jpg")):
    scene = path.replace("\\", "/").split("/")[-3]
    if scene != cur_scene:
        cur_scene, mem = scene, {}
    tick += 1.0
    im = cv2.imread(path)
    H, W = im.shape[:2]
    gt = collections.defaultdict(list)
    lab = path.replace("images", "labels")[:-4] + ".txt"
    for line in open(lab) if os.path.exists(lab) else []:
        c, x, y, w, h = map(float, line.split()[:5])
        gt[NAMES[int(c)]].append([(x - w / 2) * W, (y - h / 2) * H, (x + w / 2) * W, (y + h / 2) * H])
    t0 = time.perf_counter()
    out = det.detect_batch([im], [False])[0]
    times.append(time.perf_counter() - t0)
    persons = [dict(d, tid=i, track_conf=d["conf"]) for i, d in enumerate(out["persons"])]
    if not persons:
        res[scene]["no_person"] += 1
        continue
    p = max(persons, key=lambda q: (q["bbox"][2] - q["bbox"][0]) * (q["bbox"][3] - q["bbox"][1]))
    p["tid"] = 0
    st, _ = evaluate([p], out["items"], {p["tid"]: ITEMS}, W, H, S["association"], frame=im, vcfg=V, parts=out.get("parts"), helmet_mem=mem if MEM else None, now=tick)
    st = st[p["tid"]]
    seen_parts = any(inside(b, p["bbox"]) for k in ("face", "head", "helmet") for b in gt.get(k, []))
    for item in ITEMS:
        if item == "boots":
            if not any(inside(b, p["bbox"]) for b in gt.get("foot", [])):
                continue
            truth = {"ppe_15_footwear_safety": "compliant", "ppe_14_footwear_normal": "missing"}.get(scene)
        elif any(inside(b, p["bbox"]) for b in gt.get(item, [])):
            truth = "compliant"
        elif item == "vest":
            truth = "missing" if seen_parts else None
        else:
            truth = "missing" if any(inside(b, p["bbox"]) for b in gt.get(BARE[item], [])) else None
        if truth is None:
            continue
        got = st[item][0]
        k = "UNSURE" if got == "unverifiable" else "OK" if got == truth else "FALSE" if truth == "compliant" else "MISSED"
        res[scene][f"{item}:{k}"] += 1
        res["ALL"][f"{item}:{k}"] += 1
tag = sys.argv[1] if len(sys.argv) > 1 else "run"
print(f"== {tag}   detector {1000 * sorted(times)[len(times) // 2]:.0f} ms/frame median (single frame, this laptop)")
hdr = "scene".ljust(30) + "".join(i.ljust(16) for i in ITEMS)
print(hdr + "   (OK/FALSE alarm/MISSED violation/unsure)")
for scene in sorted(res):
    c = res[scene]
    row = scene.ljust(30)
    for i in ITEMS:
        n = [c[f"{i}:{k}"] for k in ("OK", "FALSE", "MISSED", "UNSURE")]
        row += ("{}/{}/{}/{}".format(*n) if sum(n) else "-").ljust(16)
    print(row + (f" no person {c['no_person']}" if c["no_person"] else ""))
if len(sys.argv) > 2:
    json.dump({s: dict(c) for s, c in res.items()}, open(sys.argv[2], "w"))
