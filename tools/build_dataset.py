"""Build a YOLO training set from our own labelled scenes, split BY SCENE (plan 8.4).

    python -m tools.build_dataset --task ppe          # uses datasets/splits.yaml
    python -m tools.build_dataset --task fire --zip   # also writes datasets/build/fire.zip for Colab

datasets/splits.yaml lists scene folder names (under datasets/own/<task>/) per split:
    ppe:
      train: [lab_fullkit_day, lab_no_helmet, ...]
      val:   [lab_evening]
      test:  [corridor_backlit]        # hard test set: NEVER used for training
Frames of one scene never end up in two splits - near-identical frames would leak into the
test set and make the numbers look better than the live demo."""
import argparse
import os
import shutil

import yaml

from edge.config import path

# ppe = v3 class list (same order as ppe_v3A / the Colab notebook); fire = same order as fire_smoke_v1/v2
NAMES = {"ppe": ["head", "helmet", "face", "goggles", "mask", "vest", "hand", "gloves", "foot"], "fire": ["Fire", "Smoke"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task", choices=["ppe", "fire"], required=True)
    ap.add_argument("--zip", action="store_true")
    a = ap.parse_args()
    splits_file = path("datasets", "splits.yaml")
    if not os.path.exists(splits_file):
        raise SystemExit("datasets/splits.yaml missing - copy datasets/splits.example.yaml and fill in scene names")
    with open(splits_file, encoding="utf-8") as f:
        splits = (yaml.safe_load(f) or {}).get(a.task) or {}
    src_root, out = path("datasets", "own", a.task), path("datasets", "build", a.task)
    seen, counts = {}, {}
    for split in ("train", "val", "test"):
        for scene in splits.get(split) or []:
            if scene in seen:
                raise SystemExit(f"scene '{scene}' is in both {seen[scene]} and {split} - one split per scene")
            seen[scene] = split
    if os.path.exists(out):
        shutil.rmtree(out)
    for scene, split in seen.items():
        img_dir, lab_dir = os.path.join(src_root, scene, "images"), os.path.join(src_root, scene, "labels")
        if not os.path.isdir(img_dir):
            raise SystemExit(f"missing {img_dir}")
        for sub in ("images", "labels"):
            os.makedirs(os.path.join(out, split, sub), exist_ok=True)
        for fn in os.listdir(img_dir):
            stem = os.path.splitext(fn)[0]
            lab = os.path.join(lab_dir, stem + ".txt")
            shutil.copy2(os.path.join(img_dir, fn), os.path.join(out, split, "images", fn))
            if os.path.exists(lab):
                shutil.copy2(lab, os.path.join(out, split, "labels", stem + ".txt"))
            else:                                           # no label file = negative image
                open(os.path.join(out, split, "labels", stem + ".txt"), "w").close()
            counts[split] = counts.get(split, 0) + 1
    with open(os.path.join(out, "data.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump({"path": ".", "train": "train/images", "val": "val/images", "test": "test/images",
                        "names": NAMES[a.task]}, f, sort_keys=False)
    print(f"{a.task}: " + ", ".join(f"{k} {v} images" for k, v in counts.items()) + f" -> {out}")
    if a.zip:
        z = shutil.make_archive(out, "zip", out)
        print(f"zip for Colab: {z}  (upload, unzip, then train with data=<folder>/data.yaml)")


if __name__ == "__main__":
    main()
