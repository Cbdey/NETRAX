"""Shared inference engine (final-round section 7: latency).

Before: every camera thread had its OWN copy of all models (3 x 5 models in 4 GB of GPU memory) and the
cameras took turns on the GPU, each running five models one after another.
Now: ONE engine thread owns the models (loaded once). Camera threads hand in their newest frame and wait;
the engine takes every frame that is waiting (up to max_batch), runs each model ONCE on the whole batch and
hands the results back. Fire candidates and high-risk zones are served first; no camera can starve
because every waiting frame joins the next batch. A frame older than max_frame_age_s is dropped by the
camera (it never processes stale pictures)."""
import queue
import threading
import time
from collections import deque


class _Req:
    __slots__ = ("cam", "frame", "fire", "rank", "t", "done", "result", "error")

    def __init__(self, cam, frame, fire, rank):
        self.cam, self.frame, self.fire, self.rank = cam, frame, fire, rank
        self.t = time.perf_counter()
        self.done = threading.Event()
        self.result = self.error = None


class InferenceEngine(threading.Thread):
    def __init__(self, detector, max_batch=4, gather_ms=3.0):
        super().__init__(daemon=True, name="inference-engine")
        self.det = detector
        self.q = queue.Queue()
        self.max_batch = max(1, int(max_batch))
        self.gather = gather_ms / 1000.0
        self.batch_ms = deque(maxlen=200)          # model time per batch
        self.wait_ms = deque(maxlen=200)           # queue wait per frame
        self.batch_sizes = deque(maxlen=200)
        self.busy = deque(maxlen=400)              # (start, end) of each batch -> busy share of the last seconds
        self.running = True

    @property
    def device(self):
        return self.det.device

    def infer(self, cam, frame, want_fire, rank=1, timeout=5.0):
        r = _Req(cam, frame, want_fire, rank)
        self.q.put(r)
        if not r.done.wait(timeout):
            raise TimeoutError("inference engine did not answer in time")
        if r.error:
            raise r.error
        return r.result

    def run(self):
        while self.running:
            try:
                first = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            batch = [first]
            end = time.perf_counter() + self.gather
            while len(batch) < self.max_batch:
                left = end - time.perf_counter()
                try:
                    batch.append(self.q.get(timeout=max(0.0, left)) if left > 0 else self.q.get_nowait())
                except queue.Empty:
                    break
            # one camera can have only one frame in flight, but keep the newest per camera to be safe
            newest = {}
            for r in batch:
                old = newest.get(r.cam)
                if old is not None:
                    old.error = RuntimeError("superseded by a newer frame")
                    old.done.set()
                newest[r.cam] = r
            batch = sorted(newest.values(), key=lambda r: (0 if r.fire == "urgent" else r.rank, r.t))
            t0 = time.perf_counter()
            try:
                res = self.det.detect_batch([r.frame for r in batch], [bool(r.fire) for r in batch])
                for r, out in zip(batch, res):
                    r.result = out
            except Exception as e:                # noqa: BLE001 - hand the error to every waiting camera
                for r in batch:
                    r.error = e
            t1 = time.perf_counter()
            ms = (t1 - t0) * 1000
            self.busy.append((t0, t1))
            self.batch_ms.append(ms)
            self.batch_sizes.append(len(batch))
            for r in batch:
                self.wait_ms.append((t0 - r.t) * 1000)
                r.done.set()

    def busy_share(self, window_s=5.0):
        """Share of the last window_s seconds the GPU engine spent running models (0..1)."""
        now = time.perf_counter()
        t = sum(min(e, now) - max(s, now - window_s) for s, e in list(self.busy) if e > now - window_s)
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
                "gpu_busy_share": round(self.busy_share(), 2)}
