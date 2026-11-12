"""Inference engine in its OWN PROCESS (4 Nov, measured: AI too slow with several cameras).

Why: inside the edge process the models ran in a thread next to ~15 other Python threads (camera decoding, live
view, picture senders, face ID, tracking). A YOLO forward pass is hundreds of small steps that each need Python's
global interpreter lock, so the GPU sat waiting for the lock: one batch took 440 ms instead of ~70 ms per frame
(AI 1.7 frames/s per camera). Measured: letting the engine hold the lock longer alone doubled the AI rate, which
confirmed the cause. A separate process has its own interpreter and lock, so the models run at their own speed.

How: the camera thread copies its newest frame into a shared-memory buffer of its own (no pickling of 6 MB
frames) and sends a small message; the child process gathers the waiting frames, runs Detector.detect_batch on
each group of equal-sized frames and sends back the small result dicts. Same interface as edge/engine.py
(infer / stats / busy_share / det), so the camera workers do not change. If the process cannot start,
run_edge falls back to the thread engine."""
import multiprocessing as mp
import queue
import threading
import time
from collections import deque
from multiprocessing import shared_memory

import numpy as np


class _DetInfo:
    """What the camera workers read from the detector (the real one lives in the child process)."""
    def __init__(self, d):
        self.supported, self.has_parts = set(d["supported"]), d["has_parts"]
        self.device, self.runtime = d["device"], d["runtime"]


class _EchoDetector:
    """Test double used by tools/final_selftest (no GPU): returns the frame's mean value as a 'person'."""
    supported, has_parts, device, runtime = {"helmet", "vest"}, False, "cpu", "test"

    def __init__(self, settings=None):
        pass

    def detect_batch(self, frames, flags):
        time.sleep(0.01)
        return [{"persons": [{"cls": "person", "conf": 0.9, "bbox": [0, 0, 1, 1], "mean": float(f.mean())}],
                 "items": [], "parts": [], "fire": [] if fl else None, "timings": {"total_ms": 10}} for f, fl in zip(frames, flags)]


def _warm_shapes(det, settings, max_batch):
    """11 Nov: with cudnn.benchmark every NEW (picture shape, batch size) pair is tuned on first use - measured ~3 s each
    on this laptop (on battery). With 3 cameras those pairs turned up one by one during the first minute: every camera
    timed out ('inference engine did not answer in time'), tiles went DEGRADED / OFFLINE = 'the cameras closed'.
    Tune the usual camera shapes NOW, before the engine says ready. A real snapshot (with a person) is used so the PPE /
    shoe models are tuned too. settings processing.warm_shapes [[h, w], ...]; [] = off."""
    import glob
    import os
    shapes = (settings.get("processing") or {}).get("warm_shapes", [[720, 1280], [640, 480], [480, 640]])
    if not shapes:
        return
    t0 = time.perf_counter()
    img = None
    try:
        import cv2
        from .config import path
        snaps = sorted(glob.glob(path("data/snapshots/*.jpg")), key=os.path.getmtime)[-20:]
        for p in reversed(snaps):
            img = cv2.imread(p)
            if img is not None:
                break
    except Exception:                                    # noqa: BLE001
        img = None
    sizes = sorted({1, min(2, max_batch)})
    for h, w in shapes:
        try:
            if img is not None:
                import cv2
                frame = cv2.resize(img, (int(w), int(h)))
            else:
                frame = np.zeros((int(h), int(w), 3), np.uint8)
            for n in sizes:
                det.detect_batch([frame.copy() for _ in range(n)], [True] * n)
        except Exception as e:                           # noqa: BLE001 - warm-up must never stop the engine
            print(f"[engine] warm-up {h}x{w} skipped: {e!r}", flush=True)
    print(f"[engine] tuned for camera shapes {shapes} x batch {sizes} in {time.perf_counter() - t0:.1f} s", flush=True)


def _child(settings, factory, req_q, res_q, max_batch, gather_s):
    import os
    os.environ.setdefault("OMP_NUM_THREADS", "4")
    try:
        import torch
        torch.set_num_threads(4)
        torch.backends.cudnn.benchmark = True
    except Exception:                                    # noqa: BLE001
        pass
    if factory == "echo":
        det = _EchoDetector()
    else:
        from .detector import Detector
        det = Detector(settings)
        _warm_shapes(det, settings, max_batch)
    res_q.put(("ready", {"supported": sorted(det.supported), "has_parts": bool(det.has_parts),
                         "device": str(det.device), "runtime": getattr(det, "runtime", "pytorch")}))
    shms = {}

    def view(m):
        name, shape, dtype = m["shm"], m["shape"], m["dtype"]
        s = shms.get(name)
        if s is None:
            s = shms[name] = shared_memory.SharedMemory(name=name)
        return np.ndarray(shape, dtype=dtype, buffer=s.buf)

    while True:
        first = req_q.get()
        if first is None:
            break
        batch = [first]
        end = time.perf_counter() + gather_s
        while len(batch) < max_batch:
            left = end - time.perf_counter()
            try:
                m = req_q.get(timeout=left) if left > 0 else req_q.get_nowait()
            except queue.Empty:
                break
            if m is None:
                return
            batch.append(m)
        if any(m.get("drop") for m in batch):            # a camera's buffer was replaced: forget the old mapping
            for m in batch:
                if m.get("drop"):
                    s = shms.pop(m["drop"], None)
                    if s is not None:
                        s.close()
            batch = [m for m in batch if not m.get("drop")]
            if not batch:
                continue
        batch.sort(key=lambda m: (0 if m["fire"] == "urgent" else m["rank"], m["t"]))
        groups = {}
        for m in batch:                                  # equal-sized frames only (mixed shapes get padded = slower)
            groups.setdefault(tuple(m["shape"]), []).append(m)
        for grp in groups.values():
            t0 = time.perf_counter()
            try:
                out = det.detect_batch([view(m) for m in grp], [bool(m["fire"]) for m in grp])
                ms = (time.perf_counter() - t0) * 1000
                for m, o in zip(grp, out):
                    res_q.put(("result", m["id"], o, None, ms, len(grp)))
            except Exception as e:                       # noqa: BLE001 - hand the error to every waiting camera
                for m in grp:
                    res_q.put(("result", m["id"], None, repr(e), 0.0, len(grp)))


class _Req:
    __slots__ = ("done", "result", "error", "t")

    def __init__(self):
        self.done, self.result, self.error, self.t = threading.Event(), None, None, time.perf_counter()


class ProcessEngine:
    def __init__(self, settings, max_batch=4, gather_ms=3.0, factory="detector", start_timeout=180):
        ctx = mp.get_context("spawn")
        self.max_batch = max_batch
        self.req_q, self.res_q = ctx.Queue(), ctx.Queue()
        self.proc = ctx.Process(target=_child, args=(settings, factory, self.req_q, self.res_q, max_batch, gather_ms / 1000.0),
                                daemon=True, name="netrax-ai")
        self.proc.start()
        kind, info = self.res_q.get(timeout=start_timeout)          # models loaded + warmed up in the child
        assert kind == "ready"
        self.det = _DetInfo(info)
        self.pending, self.lock, self.n = {}, threading.Lock(), 0
        self.inflight = {}                              # cam -> its newest _Req (one request per camera at a time)
        self.shm = {}                                   # cam -> SharedMemory
        self.batch_ms, self.wait_ms, self.batch_sizes = deque(maxlen=200), deque(maxlen=200), deque(maxlen=200)
        self.busy = deque(maxlen=400)
        threading.Thread(target=self._results, daemon=True, name="ai-results").start()

    def start(self):                                    # same call as the thread engine
        return self

    def _buffer(self, cam, frame):
        s = self.shm.get(cam)
        if s is None or s.size < frame.nbytes:
            if s is not None:
                self.req_q.put({"drop": s.name})
                s.close()
                s.unlink()
            s = self.shm[cam] = shared_memory.SharedMemory(create=True, size=frame.nbytes)
        np.ndarray(frame.shape, dtype=frame.dtype, buffer=s.buf)[...] = frame
        return s

    def infer(self, cam, frame, want_fire, rank=1, timeout=5.0):
        if not self.proc.is_alive():
            raise RuntimeError("AI process stopped")
        old = self.inflight.get(cam)
        if old is not None and not old.done.is_set():
            # 11 Nov: the previous request of this camera timed out but is still queued / running in the AI process.
            # Sending another one piled stale frames into the queue (every camera fell further behind) and overwrote
            # the shared picture while the AI was reading it. Wait for that answer first; never queue a second one.
            if not old.done.wait(timeout):
                raise TimeoutError("inference engine did not answer in time")
        s = self._buffer(cam, frame)                    # one frame per camera in flight: safe to overwrite now
        r = _Req()
        with self.lock:
            self.n += 1
            rid = self.n
            self.pending[rid] = r
            self.inflight[cam] = r
        self.req_q.put({"id": rid, "cam": cam, "shm": s.name, "shape": tuple(frame.shape), "dtype": str(frame.dtype),
                        "fire": want_fire, "rank": rank, "t": time.perf_counter()})
        if not r.done.wait(timeout):
            raise TimeoutError("inference engine did not answer in time")     # stays in pending/inflight: see above
        if r.error:
            raise RuntimeError(r.error)
        return r.result

    def _results(self):
        while True:
            try:
                msg = self.res_q.get(timeout=1.0)
            except queue.Empty:
                continue
            except (EOFError, OSError):
                return
            if msg[0] != "result":
                continue
            _, rid, out, err, ms, n = msg
            with self.lock:
                r = self.pending.pop(rid, None)
            now = time.perf_counter()
            self.batch_ms.append(ms)
            self.batch_sizes.append(n)
            self.busy.append((now - ms / 1000.0, now))
            if r is None:
                continue
            self.wait_ms.append(max(0.0, (now - r.t) * 1000 - ms))
            r.result, r.error = out, err
            r.done.set()

    def busy_share(self, window_s=5.0):
        now = time.perf_counter()
        spans = sorted({(round(a, 4), round(b, 4)) for a, b in list(self.busy) if b > now - window_s})
        t, last = 0.0, now - window_s
        for a, b in spans:                              # merge overlaps (one batch reports once per camera)
            a = max(a, last)
            if b > a:
                t += b - a
                last = b
        return max(0.0, min(1.0, t / window_s))

    def stats(self):
        def pct(d, p):
            if not d:
                return None
            s = sorted(d)
            return round(s[min(len(s) - 1, int(round((len(s) - 1) * p)))], 1)
        return {"batch_ms_p50": pct(self.batch_ms, .5), "batch_ms_p95": pct(self.batch_ms, .95),
                "wait_ms_p50": pct(self.wait_ms, .5), "wait_ms_p95": pct(self.wait_ms, .95),
                "batch_size_avg": round(sum(self.batch_sizes) / len(self.batch_sizes), 2) if self.batch_sizes else None,
                "gpu_busy_share": round(self.busy_share(), 2), "process": True}

    def close(self):
        try:
            self.req_q.put(None)
        except Exception:                               # noqa: BLE001
            pass
        for s in self.shm.values():
            try:
                s.close()
                s.unlink()
            except Exception:                           # noqa: BLE001
                pass
