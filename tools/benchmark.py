"""Measures per-model inference latency on THIS machine (F12).  Run:
    python -m tools.benchmark                 # PyTorch (CUDA if available)
    python -m tools.benchmark --onnx          # also export to ONNX and time ONNX Runtime
Writes docs/benchmark.json and prints a markdown table for docs/model_card.md.
Uses real frames from data/snapshots so the numbers reflect real inputs."""
import argparse
import glob
import json
import os
import platform
import statistics
import time

import cv2

from edge.config import load_all, path
from edge.detector import pick_device


def frames(n=20):
    files = sorted(glob.glob(path("data", "snapshots", "*.jpg")))[:n]
    imgs = [cv2.imread(f) for f in files]
    imgs = [i for i in imgs if i is not None]
    if not imgs:
        import numpy as np
        imgs = [np.full((480, 640, 3), 127, np.uint8)]
    return imgs


def time_model(model, imgs, device, imgsz, runs, **kw):
    for i in range(5):                                  # warm-up (CUDA kernels, allocations)
        model.predict(imgs[i % len(imgs)], imgsz=imgsz, device=device, verbose=False, **kw)
    ms = []
    for i in range(runs):
        t = time.perf_counter()
        model.predict(imgs[i % len(imgs)], imgsz=imgsz, device=device, verbose=False, **kw)
        ms.append((time.perf_counter() - t) * 1000)
    ms.sort()
    return {"mean_ms": round(statistics.mean(ms), 1), "p50_ms": round(ms[len(ms) // 2], 1),
            "p95_ms": round(ms[int(len(ms) * 0.95) - 1], 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=100)
    ap.add_argument("--onnx", action="store_true")
    a = ap.parse_args()
    from ultralytics import YOLO
    import torch
    s, _, _ = load_all()
    imgsz = s["models"]["imgsz"]
    dev = pick_device(s["models"].get("device", "auto"))
    imgs = frames()
    models = {"person (yolov8n COCO)": path("models", "yolov8n.pt") if os.path.exists(path("models", "yolov8n.pt"))
              else path("yolov8n.pt"),
              "ppe main " + os.path.basename(s["models"]["ppe"]) + " (yolov8s)": path(s["models"]["ppe"]),
              "fire (" + os.path.basename(s["models"]["fire"]) + ")": path(s["models"]["fire"])}
    if os.path.exists(path("models", "yolov8n-pose.pt")) or s["models"].get("person") == "pose":
        models["person+keypoints (yolov8n-pose)"] = (path("models", "yolov8n-pose.pt")
                                                     if os.path.exists(path("models", "yolov8n-pose.pt")) else "yolov8n-pose.pt")
    if s["models"].get("ppe_aux") and os.path.exists(path(s["models"]["ppe_aux"])):
        models["ppe_aux " + os.path.basename(s["models"]["ppe_aux"]) + " (yolov8s-P2)"] = path(s["models"]["ppe_aux"])
    shoe = s["models"].get("shoe_cls") or s.get("ppe", {}).get("shoe_cls")
    if shoe and os.path.exists(path(shoe)):
        models["shoe_cls (yolov8n-cls, 128, per foot crop)"] = path(shoe)
    if not os.path.exists(models["fire (" + os.path.basename(s["models"]["fire"]) + ")"]):
        models.pop("fire (" + os.path.basename(s["models"]["fire"]) + ")")
    gpu = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "none"
    rows = []
    for name, p in models.items():
        m = YOLO(p)
        sz = 128 if "-cls" in name else imgsz   # same size as edge/detector.py
        kw = {"classes": [0]} if "COCO" in name else {}
        r = time_model(m, imgs, dev, sz, a.runs, **kw)
        rows.append({"model": name, "runtime": f"PyTorch {'CUDA FP32' if dev != 'cpu' else 'CPU'}", **r})
        print(rows[-1])
        if a.onnx and "-cls" not in name:
            onnx_path = os.path.splitext(p)[0] + ".onnx"
            if not os.path.exists(onnx_path):
                onnx_path = m.export(format="onnx", imgsz=imgsz, simplify=True, dynamic=False)
            import onnxruntime as ort
            prov = "CUDAExecutionProvider" if "CUDAExecutionProvider" in ort.get_available_providers() \
                else "CPUExecutionProvider"
            om = YOLO(onnx_path, task="detect")
            r = time_model(om, imgs, 0 if prov.startswith("CUDA") else "cpu", imgsz, max(20, a.runs // 3))
            rows.append({"model": name, "runtime": f"ONNX Runtime {prov.replace('ExecutionProvider', '')}", **r})
            print(rows[-1])
    out = {"measured_at": time.strftime("%Y-%m-%d %H:%M"), "gpu": gpu, "cpu": platform.processor(),
           "imgsz": imgsz, "frames": len(imgs), "rows": rows}
    os.makedirs(path("docs"), exist_ok=True)
    with open(path("docs", "benchmark.json"), "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nGPU: {gpu} | CPU: {out['cpu']} | imgsz {imgsz} | {len(imgs)} real frames\n")
    print("| Model | Runtime | mean ms | p50 ms | p95 ms |\n|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['model']} | {r['runtime']} | {r['mean_ms']} | {r['p50_ms']} | {r['p95_ms']} |")


if __name__ == "__main__":
    main()
