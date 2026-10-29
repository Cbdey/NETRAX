"""Sends live preview frames and camera health to the backend (best effort, not buffered).
Preview frames stay on the local network; only events/thumbnails are the 'real' payload.

Latency (final-round section 7): the camera thread only hands over the picture; JPEG encoding and the
HTTP post happen in this publisher's own thread, newest frame only (an old frame is replaced, never
queued). Each frame carries its capture time (X-Captured-At) so the dashboard can measure picture age."""
import threading
import time

import requests

from . import annotate


class Publisher:
    def __init__(self, backend_url, preview_fps, raw_every_s=1.0, width=800, quality=72):
        self.base = backend_url.rstrip("/")
        self.min_gap = 1.0 / max(1.0, preview_fps)
        self.raw_gap = raw_every_s
        self.width, self.quality = width, quality
        self.last = {}
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.pending = {}                       # key -> (image, captured_ts)
        self.posted_ms = []
        self.closed = {}                        # cam -> event ids the supervisor resolved (from the frame reply)
        threading.Thread(target=self._loop, daemon=True, name="publisher").start()

    def due(self, cam_id):
        """True when this camera's next preview frame is due (the worker only draws boxes then)."""
        return time.time() - self.last.get(cam_id, 0) >= self.min_gap

    def frame(self, cam_id, img, captured=None):
        if img is None:
            return
        self.last[cam_id] = time.time()
        with self.cv:
            self.pending[cam_id] = (img, captured or time.time())
            self.cv.notify()

    def raw(self, cam_id, img):
        """Clean (un-annotated) frame, about 1 per second, used only for worker registration photos."""
        now = time.time()
        if now - self.last.get(("raw", cam_id), 0) < self.raw_gap:
            return
        self.last[("raw", cam_id)] = now
        with self.cv:
            self.pending[("raw", cam_id)] = (img, now)
            self.cv.notify()

    def _loop(self):
        s = requests.Session()
        while True:
            with self.cv:
                while not self.pending:
                    self.cv.wait(timeout=1.0)
                batch, self.pending = self.pending, {}
            for key, (img, ts) in batch.items():
                raw = isinstance(key, tuple)
                jpg = annotate.to_jpeg(img, width=960 if raw else self.width, quality=85 if raw else self.quality)
                if not jpg:
                    continue
                url = f"{self.base}/api/cameras/{key[1] if raw else key}/{'raw' if raw else 'frame'}"
                try:
                    r = s.post(url, data=jpg, timeout=1, headers={"Content-Type": "image/jpeg",
                                                                  "X-Captured-At": f"{ts:.3f}"})
                    if not raw and r.ok and r.content and b"closed" in r.content:
                        ids = (r.json() or {}).get("closed") or []
                        if ids:
                            with self.lock:
                                self.closed.setdefault(key, set()).update(ids)
                except (requests.RequestException, ValueError):
                    pass

    def take_closed(self, cam_id):
        """Event ids the supervisor resolved on this camera since the last call (each id handled once)."""
        with self.lock:
            ids = self.closed.pop(cam_id, None)
        seen = self.__dict__.setdefault("_closed_seen", set())
        new = set(ids or ()) - seen
        seen |= new
        return new

    def health(self, payload):
        try:
            requests.post(f"{self.base}/api/edge/health", json=payload, timeout=1)
            return True
        except requests.RequestException:
            return False
