"""Starts the edge pipeline for every camera in config/cameras.yaml.

Run from the project root:   python -m edge.run_edge
"""
import os
import time

from .config import load_all, path
from .detector import Detector
from .engine import InferenceEngine
from .identity import IdentityService
from .load import LoadManager
from .retention import purge
from .outbox import Outbox
from .publisher import Publisher
from .share import ShareGroups
from .stream import StreamReader
from .worker import CameraWorker


def _pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    return round(s[min(len(s) - 1, int(round((len(s) - 1) * p)))], 1)


def _lat(w):
    """Per-camera latency numbers for the dashboard / perf report (milliseconds)."""
    fm, im, wm = list(w.frame_ms), list(w.infer_ms), list(w.wait_ms)
    return {"frame_p50": _pct(fm, .5), "frame_p95": _pct(fm, .95), "infer_p50": _pct(im, .5),
            "infer_p95": _pct(im, .95), "wait_p50": _pct(wm, .5), "wait_p95": _pct(wm, .95),
            "dropped_stale": w.dropped_stale}


def main():
    settings, zones, cameras = load_all()
    os.makedirs(path("data"), exist_ok=True)
    backend = settings["backend"]["url"]
    outbox = Outbox(path(settings["outbox_db"]), backend)
    publisher = Publisher(backend, settings["backend"]["preview_fps"])
    fps = settings["processing"]["target_fps"]
    identity = IdentityService(settings.get("identity"), backend)     # shared by all cameras

    readers, workers = {}, {}
    off = [c for c, cam in cameras.items() if cam.get("enabled", True) is False]
    for c in off:
        print(f"[edge] {c} '{cameras[c].get('name')}' switched off (enabled: false in cameras.yaml)")
    cameras = {c: cam for c, cam in cameras.items() if c not in off}
    # ONE copy of every model for all cameras, batched on the GPU (edge/engine.py)
    proc = settings.get("processing", {})
    engine = None
    if proc.get("shared_engine", True):
        engine = InferenceEngine(Detector(settings), max_batch=proc.get("max_batch", 4),
                                 gather_ms=proc.get("batch_gather_ms", 3))
        engine.start()
        print(f"[edge] shared inference engine: models loaded once, batch up to {engine.max_batch} cameras")
    share = ShareGroups(cameras, settings.get("processing", {}).get("share_failover_s", 15))
    for g, members in share.groups.items():
        print(f"[edge] camera pair '{g}': {' / '.join(members)} - one at a time, now {share.active[g]}")
    for cam_id, cam in cameras.items():
        r = StreamReader(cam_id, cam["source"], fps, cam.get("rotate", 0), cam.get("capture"))
        r.allowed = (lambda c=cam_id: share.allowed(c))
        r.start()
        wkr = CameraWorker(cam_id, cam, zones, settings, r, outbox, publisher, identity, engine=engine)
        wkr.start()
        readers[cam_id], workers[cam_id] = r, wkr
        print(f"[edge] {cam_id} '{cam.get('name')}' source={cam['source']} "
              f"zone={cam['default_zone']} tracker={wkr.tracker.kind} device={wkr.detector.device}")

    print(f"[edge] running {len(cameras)} camera(s). Backend: {backend}. Ctrl+C to stop.")
    load = LoadManager(settings.get("load"), fps)
    risk_of = lambda c: zones[cameras[c]["default_zone"]]["risk"]
    last_load, last_purge = 0, 0
    keep_days = settings.get("privacy", {}).get("snapshot_retention_days", 30)
    every = settings["backend"]["health_every_s"]
    last_print = 0
    try:
        while True:
            for msg in share.step({c: r.frame_ts for c, r in readers.items()}):
                print(f"[edge] {msg}")
            cams = []
            for cam_id, cam in cameras.items():
                r, wkr = readers[cam_id], workers[cam_id]
                pf = wkr.processed_fps()
                cams.append({
                    "camera_id": cam_id,
                    "name": cam.get("name", cam_id),
                    "zone_id": cam["default_zone"],
                    "zone_name": zones[cam["default_zone"]]["name"],
                    "risk": zones[cam["default_zone"]]["risk"],
                    "status": r.health(pf),
                    "detail": share.standby_detail(cam_id) if r.health(pf) == "STANDBY" else r.detail,
                    "share": cam.get("share"),
                    "fps": round(pf, 1),
                    "source_fps": round(r.fps, 1),
                    "last_frame_at": r.frame_ts or None,
                    "badge": wkr.tile_badge,
                    "infer_ms": round(sum(wkr.infer_ms) / len(wkr.infer_ms), 1) if wkr.infer_ms else None,
                    "decisions": dict(wkr.decisions),
                    "target_fps": wkr.fps_target,
                    "role": wkr.role,
                    "latency": _lat(wkr),
                })
            online = publisher.health({
                "at": time.time(),
                "device": str(next(iter(workers.values())).detector.device) if workers else "cpu",
                "runtime": ("TensorRT" if getattr(next(iter(workers.values())).detector, "runtime", "") == "tensorrt"
                            else "PyTorch (ultralytics)") + (" + shared batched engine" if engine else "") if workers else "-",
                "engine": engine.stats() if engine else None,
                "outbox_pending": outbox.pending(),
                "load_note": load.note,
                "cameras": cams,
            })
            if time.time() - last_purge >= 3600:
                last_purge = time.time()
                gone = purge(path(settings["snapshots_dir"]), keep_days)
                if gone:
                    print(f"[edge] retention: deleted {len(gone)} snapshot(s) older than {keep_days} days")
            if time.time() - last_load >= 1.0:
                last_load = time.time()
                msg = load.step(workers, risk_of)
                if msg:
                    print(f"[edge] {msg}")
            if time.time() - last_print > 10:
                line = " | ".join(f"{c['camera_id']}:{c['status']} {c['fps']}fps" for c in cams)
                print(f"[edge] {line} | backend={'up' if online else 'DOWN (buffering)'} "
                      f"| outbox pending={outbox.pending()}")
                last_print = time.time()
            time.sleep(every)
    except KeyboardInterrupt:
        print("[edge] stopping")


if __name__ == "__main__":
    main()
