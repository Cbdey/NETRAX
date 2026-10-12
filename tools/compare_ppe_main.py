"""Which model should supply helmet / vest / mask: ppe_v2 (main today) or ppe_v3B (aux)?

    python -m tools.compare_ppe_main
    python -m tools.compare_ppe_main --split val      # also check the validation scenes

Both models are scored on OUR unseen scenes (datasets/splits.yaml), per class, FP32.
ppe_v2 has a different class order, so its labels are remapped (helmet, vest, mask kept) in a temp copy.
Nothing in the project is changed: the script prints which setting to use. Writes docs/eval/ppe_main_<split>.md.
"""
import argparse
import glob
import os
import shutil
import tempfile

import yaml

from edge.config import load_all, path

V3 = ["head", "helmet", "face", "goggles", "mask", "vest", "hand", "gloves", "foot"]
ITEMS = ["helmet", "vest", "mask"]


def build(scenes, names_to, tmp, tag):
    """Copy the scene images + labels; keep only ITEMS, renumbered to the model's class ids."""
    img_dir, lab_dir = os.path.join(tmp, tag, "images"), os.path.join(tmp, tag, "labels")
    os.makedirs(img_dir), os.makedirs(lab_dir)
    to = {n: i for i, n in names_to.items()}
    n_img = 0
    for sc in scenes:
        for im in glob.glob(path("datasets", "own", "ppe", sc, "images", "*.jpg")):
            stem = sc + "__" + os.path.splitext(os.path.basename(im))[0]
            shutil.copy(im, os.path.join(img_dir, stem + ".jpg"))
            src = im.replace(os.sep + "images" + os.sep, os.sep + "labels" + os.sep)[:-4] + ".txt"
            src = src if os.path.exists(src) else im.replace("/images/", "/labels/")[:-4] + ".txt"
            out = []
            if os.path.exists(src):
                for line in open(src):
                    v = line.split()
                    if v and V3[int(v[0])] in ITEMS and V3[int(v[0])] in to:
                        out.append(" ".join([str(to[V3[int(v[0])]])] + v[1:5]))
            open(os.path.join(lab_dir, stem + ".txt"), "w").write("\n".join(out) + ("\n" if out else ""))
            n_img += 1
    y = os.path.join(tmp, tag + ".yaml")
    yaml.safe_dump({"path": os.path.join(tmp, tag), "train": "images", "val": "images",
                    "names": {int(k): v for k, v in names_to.items()}}, open(y, "w"))
    return y, n_img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="test", choices=["test", "val"])
    a = ap.parse_args()
    from ultralytics import YOLO
    import torch
    s, _, _ = load_all()
    m = s["models"]
    scenes = yaml.safe_load(open(path("datasets", "splits.yaml"), encoding="utf-8"))["ppe"][a.split]
    dev = 0 if torch.cuda.is_available() else "cpu"
    tmp = tempfile.mkdtemp()
    res = {}
    for key in ("ppe", "ppe_aux"):
        model = YOLO(path(m[key]))
        y, n = build(scenes, model.names, tmp, key)
        r = model.val(data=y, imgsz=m.get("imgsz", 640), batch=8, half=False, plots=False, verbose=False,
                      device=dev, project=tmp, name="val_" + key)
        per = {r.names[int(c)]: (r.box.ap50[i], r.box.p[i], r.box.r[i]) for i, c in enumerate(r.box.ap_class_index)}
        res[os.path.basename(m[key])] = {k: v for k, v in per.items() if k in ITEMS}
        print(f"{os.path.basename(m[key])} on {n} images: " +
              "  ".join(f"{k} AP50={v[0]:.3f} P={v[1]:.2f} R={v[2]:.2f}" for k, v in res[os.path.basename(m[key])].items()))
    main_n, aux_n = os.path.basename(m["ppe"]), os.path.basename(m["ppe_aux"])
    os.makedirs(path("docs", "eval"), exist_ok=True)
    out = path("docs", "eval", f"ppe_main_{a.split}.md")
    win = []
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"# helmet / vest / mask: {main_n} vs {aux_n} on our {a.split} scenes ({', '.join(scenes)})\n\n")
        f.write(f"| item | {main_n} AP50 (P / R) | {aux_n} AP50 (P / R) | better |\n|---|---|---|---|\n")
        for it in ITEMS:
            a_, b_ = res[main_n].get(it), res[aux_n].get(it)
            if not a_ or not b_:
                f.write(f"| {it} | {'-' if not a_ else f'{a_[0]:.3f}'} | {'-' if not b_ else f'{b_[0]:.3f}'} | no labels |\n")
                continue
            better = aux_n if b_[0] > a_[0] + 0.02 else main_n if a_[0] > b_[0] + 0.02 else "tie (keep main)"
            if better == aux_n:
                win.append(it)
            f.write(f"| {it} | {a_[0]:.3f} ({a_[1]:.2f} / {a_[2]:.2f}) | {b_[0]:.3f} ({b_[1]:.2f} / {b_[2]:.2f}) | {better} |\n")
        f.write("\nA difference under 0.02 AP50 counts as a tie. Labels: machine-made + reviewed - indicative only.\n")
    print("->", out)
    if win:
        items = sorted(set(m.get("aux_items") or []) | set(win))
        print(f"\nRECOMMEND: let {aux_n} supply {win}. In config/settings.yaml set\n  aux_items: [{', '.join(items)}]"
              f"\nThen re-run the live test (helmet on/off, vest on/off) before the demo.")
    else:
        print(f"\nRECOMMEND: keep {main_n} for helmet / vest / mask (no clear win for {aux_n}).")


if __name__ == "__main__":
    main()
