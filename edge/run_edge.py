"""Starts the edge pipeline for every camera in config/cameras.yaml.

Run from the project root:   python -m edge.run_edge
"""
import os
import time

for _k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ.setdefault(_k, "4")      # 4 Nov: measured AI 2 fps -> 6.7 fps with this cap (laptop, 2 cameras)

import cv2                              # noqa: E402

from .config import load_all, path
from .detector import Detector
from .engine import InferenceEngine
from .identity import IdentityService
from .live import LiveView
from .load import LoadManager
from .retention import purge
from .clips import ClipRecorder
from .outbox import Outbox
from .publisher import Publisher
from .share import ShareGroups
from .power import Power
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


_ONE = None


def _single_instance(port=48765):
    """11 Nov: a second edge (run_all.bat started twice) fought the first one for the USB cameras - both lost their
    picture ('cameras closed'). Hold a local port while running; a second copy says so and stops.
    SENTINELEYE_EDGE_MULTI=1 allows several (load tests with their own camera files)."""
    global _ONE
    if os.environ.get("SENTINELEYE_EDGE_MULTI") == "1":
        return True
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("127.0.0.1", port))
        s.listen(1)
    except OSError:
        s.close()
        return False
    _ONE = s
    return True


def main():
    if not _single_instance():
        print("[edge] NETRAX edge is ALREADY RUNNING (another window). Close that one first - two copies take the "
              "cameras from each other.", flush=True)
        return
    cv2.setNumThreads(4)
    try:
        import torch
        torch.set_num_threads(4)
        torch.backends.cudnn.benchmark = True        # fixed input sizes per camera -> fastest kernels picked once
    except Exception:                                # noqa: BLE001
        pass
    settings, zones, cameras = load_all()
    os.makedirs(path("data"), exist_ok=True)
    backend = settings["backend"]["url"]
    outbox = Outbox(path(settings["outbox_db"]), backend)
    publisher = Publisher(backend, settings["backend"]["preview_fps"])
    fps = settings["processing"]["target_fps"]
    identity = IdentityService(settings.get("identity"), backend)     # shared by all cameras
    if (settings.get("identity") or {}).get("async", True):
        identity.start_async()                                          # 4 Nov: face ID off the AI loop
    clips = ClipRecorder(settings.get("clips"), backend, path(settings.get("clips", {}).get("dir", "data/clips")),
                         face_model=path("models/face_detection_yunet_2023mar.onnx"),
                         blur=settings.get("privacy", {}).get("blur_faces_in_snapshots", True))

    readers, workers = {}, {}
    off = [c for c, cam in cameras.items() if cam.get("enabled", True) is False]
    for c in off:
        print(f"[edge] {c} '{cameras[c].get('name')}' switched off (enabled: false in cameras.yaml)")
    cameras = {c: cam for c, cam in cameras.items() if c not in off}
    # ONE copy of every model for all cameras, batched on the GPU (edge/engine.py)
    proc = settings.get("processing", {})
    engine = None
    if proc.get("shared_engine", True) and proc.get("engine_process", True):
        # 4 Nov: models in their own process -> no fight with the camera / video threads for Python's lock
        try:
            from .engine_proc import ProcessEngine
            engine = ProcessEngine(settings, max_batch=proc.get("max_batch", 4), gather_ms=proc.get("batch_gather_ms", 3))
            print(f"[edge] AI engine in its own process: models loaded once, batch up to {engine.max_batch} cameras")
        except Exception as e:                       # noqa: BLE001 - fall back to the in-process engine
            print(f"[edge] AI process could not start ({e!r}) - using the in-process engine")
            engine = None
    if engine is None and proc.get("shared_engine", True):
        engine = InferenceEngine(Detector(settings), max_batch=proc.get("max_batch", 4),
                                 gather_ms=proc.get("batch_gather_ms", 3))
        engine.start()
        print(f"[edge] shared inference engine: models loaded once, batch up to {engine.max_batch} cameras")
    share = ShareGroups(cameras, settings.get("processing", {}).get("share_failover_s", 15))
    for g, members in share.groups.items():
        print(f"[edge] camera pair '{g}': {' / '.join(members)} - one at a time, now {share.active[g]}")
    power = Power()                       # 11 Nov: camera on / off from the dashboard (data/camera_power.json)
    for c in sorted(power.off & set(cameras)):
        print(f"[edge] {c} '{cameras[c].get('name')}' is switched OFF from the dashboard - turn it on there")
    for cam_id, cam in cameras.items():
        r = StreamReader(cam_id, cam["source"], fps, cam.get("rotate", 0), cam.get("capture"))
        r.allowed = (lambda c=cam_id: power.is_on(c) and share.allowed(c))
        r.start()
        wkr = CameraWorker(cam_id, cam, zones, settings, r, outbox, publisher, identity, engine=engine,
                           clips=clips)
        wkr.start()
        readers[cam_id], workers[cam_id] = r, wkr
        print(f"[edge] {cam_id} '{cam.get('name')}' source={cam['source']} "
              f"zone={cam['default_zone']} tracker={wkr.tracker.kind} device={wkr.detector.device}")

    live = None
    if proc.get("live_view", True):
        # 4 Nov: smooth live picture from the newest camera frame + the latest AI result (edge/live.py)
        live = LiveView(readers, workers, publisher, clips if clips.enabled else None,
                        fps=proc.get("live_fps", settings["backend"]["preview_fps"]),
                        width=proc.get("live_width", 800), predict=proc.get("live_predict", True))
        live.start()
        print(f"[edge] live view: {live.gap and round(1 / live.gap)} fps per camera, independent of the AI rate")
    print(f"[edge] running {len(cameras)} camera(s). Backend: {backend}. Ctrl+C to stop.")
    load = LoadManager(settings.get("load"), fps)
    if engine is not None:
        load.gpu_busy = engine.busy_share
    risk_of = lambda c: zones[cameras[c]["default_zone"]]["risk"]
    last_load, last_purge = 0, 0
    keep_days = settings.get("privacy", {}).get("snapshot_retention_days", 30)
    every = settings["backend"]["health_every_s"]
    last_print = 0
    t_start = time.time()
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
                    "detail": ("switched off from the dashboard" if not power.is_on(cam_id) else share.standby_detail(cam_id))
                              if r.health(pf) == "STANDBY" else r.detail,
                    "share": cam.get("share"),
                    "fps": round(pf, 1),
                    "source_fps": round(r.fps, 1),
                    "last_frame_at": r.frame_ts or None,
                    "badge": wkr.tile_badge,
                    "people": wkr.people if wkr.people and time.time() - wkr.people["at"] < 5 else None,
                    "infer_ms": round(sum(wkr.infer_ms) / len(wkr.infer_ms), 1) if wkr.infer_ms else None,
                    "decisions": dict(wkr.decisions),
                    "target_fps": wkr.fps_target,
                    "role": wkr.role,
                    "latency": _lat(wkr),
                    "stages_ms": wkr.stage_stats(),
                    "live_fps": round(live.sent.get(cam_id, 0) / max(1.0, time.time() - t_start), 1) if live else None,
                })
            online = publisher.health({
                "at": time.time(),
                "edge_started": t_start,          # 11 Nov: backend closes PPE alerts of an earlier edge run
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
                purge(clips.dir, keep_days, patterns=("*.mp4", "*.webm"))      # clips never uploaded
                if gone:
                    print(f"[edge] retention: deleted {len(gone)} snapshot(s) older than {keep_days} days")
            if time.time() - last_load >= 1.0:
                last_load = time.time()
                msg = load.step(workers, risk_of)
                if msg:
                    print(f"[edge] {msg}")
            if time.time() - last_print > 10:
                line = " | ".join(f"{c['camera_id']}:{c['status']} cam {c['source_fps']}fps AI {c['fps']}fps"
                                  for c in cams)
                print(f"[edge] {line} | backend={'up' if online else 'DOWN (buffering)'} "
                      f"| outbox pending={outbox.pending()}")
                # profiler (4 Nov): where each camera's AI time goes, median ms per stage
                for c in cams:
                    st = c.get("stages_ms") or {}
                    if st:
                        print(f"[perf] {c['camera_id']} " + " ".join(f"{k}={v['p50']:.0f}/{v['p95']:.0f}"
                                                                      for k, v in st.items()) + " (ms p50/p95)")
                es = engine.stats() if engine else {}
                fms = sorted(getattr(identity.engine, "ms", []) or []) if getattr(identity, "engine", None) else []
                lms = sorted(live.ms) if live else []
                print(f"[perf] GPU engine batch {es.get('batch_ms_p50')} ms, busy {es.get('gpu_busy_share')}, "
                      f"cams/batch {es.get('batch_size_avg')} | face detect "
                      f"{fms[len(fms) // 2] if fms else '-'} ms | live view draw {lms[len(lms) // 2] if lms else '-'} ms")
                last_print = time.time()
            time.sleep(every)
    except KeyboardInterrupt:
        print("[edge] stopping")


if __name__ == "__main__":
    main()
