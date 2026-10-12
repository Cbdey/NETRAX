"""Sends live preview frames and camera health to the backend (best effort, not buffered).
Preview frames stay on the local network; only events/thumbnails are the 'real' payload."""
import threading
import time

import requests

from . import annotate


class Publisher:
    def __init__(self, backend_url, preview_fps):
        self.base = backend_url.rstrip("/")
        self.min_gap = 1.0 / preview_fps
        self.last = {}
        self.lock = threading.Lock()
        self.pending = {}
        threading.Thread(target=self._loop, daemon=True, name="publisher").start()

    def frame(self, cam_id, img):
        now = time.time()
        if now - self.last.get(cam_id, 0) < self.min_gap:
            return
        self.last[cam_id] = now
        jpg = annotate.to_jpeg(img, width=640, quality=70)
        if jpg:
            with self.lock:
                self.pending[cam_id] = jpg

    def raw(self, cam_id, img):
        """Clean (un-annotated) frame, 2 per second, used only for worker registration photos."""
        now = time.time()
        if now - self.last.get(("raw", cam_id), 0) < 0.5:
            return
        self.last[("raw", cam_id)] = now
        jpg = annotate.to_jpeg(img, width=960, quality=85)
        if jpg:
            with self.lock:
                self.pending[("raw", cam_id)] = jpg

    def _loop(self):
        s = requests.Session()
        while True:
            with self.lock:
                batch, self.pending = self.pending, {}
            for key, jpg in batch.items():
                raw = isinstance(key, tuple)
                url = f"{self.base}/api/cameras/{key[1] if raw else key}/{'raw' if raw else 'frame'}"
                try:
                    s.post(url, data=jpg, timeout=1, headers={"Content-Type": "image/jpeg"})
                except requests.RequestException:
                    pass
            time.sleep(0.02)

    def health(self, payload):
        try:
            requests.post(f"{self.base}/api/edge/health", json=payload, timeout=1)
            return True
        except requests.RequestException:
            return False
