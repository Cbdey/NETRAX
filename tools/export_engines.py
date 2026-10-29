"""Export the live models to TensorRT engines (OPTIONAL speed-up; run on the laptop with the GPU).
    pip install tensorrt            (once, inside the venv)
    python -m tools.export_engines
Then set models.runtime: tensorrt in config/settings.yaml and compare with  python -m tools.latency_report.
FP32 on purpose (GTX 16xx FP16 issues); dynamic batch so the shared engine can batch the cameras.
If anything fails nothing changes: the .pt files keep working (runtime: pytorch)."""
import os
import shutil

from ultralytics import YOLO

from edge.config import load_all, path


def main():
    s, _, _ = load_all()
    m = s["models"]
    pose = path("models", "yolov8n-pose.pt")
    if not os.path.exists(pose) and os.path.exists(path("yolov8n-pose.pt")):
        shutil.copy(path("yolov8n-pose.pt"), pose)
    for p in [x for x in (m["ppe"], m.get("ppe_aux"), m["fire"], "models/yolov8n-pose.pt") if x]:
        full = path(p)
        if not os.path.exists(full):
            print(f"skip {p} (not found)")
            continue
        print(f"exporting {p} ...")
        try:
            out = YOLO(full).export(format="engine", imgsz=m["imgsz"], dynamic=True,
                                    batch=int(s["processing"].get("max_batch", 4)), half=False, device=0, verbose=False)
            print(f"  -> {out}")
        except Exception as e:                    # noqa: BLE001
            print(f"  FAILED ({e!r}) - this model keeps using the .pt file")


if __name__ == "__main__":
    main()
