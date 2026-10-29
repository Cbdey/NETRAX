"""Short video clip per incident (requirements brief sections 3, 5 and 8: "saved snapshot or clip when available").

Each camera keeps the last `pre_s` seconds of its annotated picture (small, `fps` frames per second, in memory).
When an incident OPENS, the recorder keeps collecting for `post_s` more seconds, then a background thread
blurs the faces (same privacy rule as the snapshots), encodes an H.264 MP4 (PyAV; WebM/VP8 through OpenCV if
PyAV is missing) and uploads it to the backend (POST /api/events/<id>/clip). The camera thread only copies a
small frame - encoding and upload never slow detection down.

Offline: the file stays in `dir` and is uploaded when the backend answers again (also after an edge restart).
"""
import glob
import os
import queue
import threading
import time
from collections import deque

import cv2
import requests

from . import annotate

CLIP_TYPES = ("ppe_missing", "unauthorized_entry", "fire", "smoke", "entrance_check")


class ClipRecorder:
    def __init__(self, cfg, backend_url, out_dir, face_model=None, blur=True):
        c = cfg or {}
        self.enabled = c.get("enabled", True)
        self.pre, self.post = float(c.get("pre_s", 4.0)), float(c.get("post_s", 4.0))
        self.gap = 1.0 / max(1.0, float(c.get("fps", 6)))
        self.width = int(c.get("width", 640))
        self.types = set(c.get("types") or CLIP_TYPES)
        self.base = (backend_url or "").rstrip("/")
        self.dir = out_dir
        self.face_model, self.blur = face_model, blur
        os.makedirs(out_dir, exist_ok=True)
        self.buf = {}          # cam -> deque[(ts, small_frame, person_boxes)]
        self.last = {}         # cam -> ts of the last buffered frame
        self.jobs = {}         # event_id -> {"cam", "until", "frames"}
        self.lock = threading.Lock()
        self.todo = queue.Queue()
        self.uploaded = []     # event ids (tests / log)
        if self.enabled:
            threading.Thread(target=self._writer, daemon=True, name="clips").start()

    # ---- camera thread (cheap) ----
    def due(self, cam, now=None):
        now = time.time() if now is None else now
        return self.enabled and now - self.last.get(cam, 0) >= self.gap * 0.85     # camera timing jitters

    def add(self, cam, img, ts, persons=()):
        """One picture of this camera (annotated). Call when due(); keeps pre_s seconds."""
        if img is None or not self.enabled:
            return
        self.last[cam] = ts
        h, w = img.shape[:2]
        k = min(1.0, self.width / float(w))
        small = cv2.resize(img, (int(w * k) // 2 * 2, int(h * k) // 2 * 2)) if k < 1.0 else img.copy()
        boxes = [[v * k for v in p["bbox"]] for p in persons]
        with self.lock:
            d = self.buf.setdefault(cam, deque())
            d.append((ts, small, boxes))
            while d and ts - d[0][0] > self.pre:
                d.popleft()
            for eid, j in list(self.jobs.items()):
                if j["cam"] != cam:
                    continue
                j["frames"].append((ts, small, boxes))
                if ts >= j["until"]:
                    self.todo.put((eid, cam, j["frames"]))
                    del self.jobs[eid]

    def start(self, cam, event_id, ts, type_=None):
        """An incident opened on this camera: clip = the last pre_s seconds + the next post_s seconds."""
        if not self.enabled or (type_ and type_ not in self.types):
            return
        with self.lock:
            if event_id in self.jobs:
                return
            pre = [f for f in self.buf.get(cam, ()) if ts - f[0] <= self.pre]
            self.jobs[event_id] = {"cam": cam, "until": ts + self.post, "frames": list(pre)}

    # ---- background thread ----
    def _writer(self):
        for f in sorted(glob.glob(os.path.join(self.dir, "*.mp4")) + glob.glob(os.path.join(self.dir, "*.webm"))):
            self.todo.put(("upload", f, None))              # left from a time the backend was offline
        retry = []
        while True:
            try:
                eid, cam, frames = self.todo.get(timeout=10)
            except queue.Empty:
                eid = None
            if eid == "upload":
                retry.append(cam)
            elif eid:
                try:
                    f = self.encode(eid, frames)
                    if f:
                        retry.append(f)
                        print(f"[clips] {eid}: {len(frames)} frames -> {os.path.basename(f)}")
                except Exception as e:                       # noqa: BLE001 - a clip must never stop the edge
                    print(f"[clips] {eid}: could not write the clip: {e!r}")
            retry = [f for f in retry if os.path.exists(f) and not self.upload(f)]

    def encode(self, eid, frames):
        if len(frames) < 2:
            return None
        h, w = frames[0][1].shape[:2]
        fd = self._faces()
        imgs = []
        for _, img, boxes in frames:
            if img.shape[:2] != (h, w):
                img = cv2.resize(img, (w, h))
            if self.blur:
                img = annotate.blur_regions(img, self._face_boxes(fd, img, boxes))
            imgs.append(img)
        dur = max(0.5, frames[-1][0] - frames[0][0])
        fps = max(1, round((len(imgs) - 1) / dur))
        out = os.path.join(self.dir, f"{eid}.mp4")
        try:
            import av
            c = av.open(out, "w", options={"movflags": "+faststart"})
            s = c.add_stream("libx264", rate=fps)
            s.width, s.height, s.pix_fmt = w, h, "yuv420p"
            s.options = {"preset": "veryfast", "crf": "27"}
            for img in imgs:
                for p in s.encode(av.VideoFrame.from_ndarray(img, format="bgr24")):
                    c.mux(p)
            for p in s.encode():
                c.mux(p)
            c.close()
            return out
        except Exception:                                    # noqa: BLE001 - no PyAV / no x264: WebM fallback
            if os.path.exists(out):
                os.remove(out)
        out = os.path.join(self.dir, f"{eid}.webm")
        vw = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*"VP80"), fps, (w, h))
        if not vw.isOpened():
            return None
        for img in imgs:
            vw.write(img)
        vw.release()
        return out

    def _faces(self):
        if not self.blur or not self.face_model or not os.path.exists(self.face_model):
            return None
        try:
            return cv2.FaceDetectorYN.create(self.face_model, "", (320, 320), 0.6, 0.3, 50)
        except Exception:                                    # noqa: BLE001
            return None

    @staticmethod
    def _face_boxes(fd, img, persons):
        """Same rule as the stored snapshots: detected faces (enlarged), else the top of every person box."""
        if fd is not None:
            h, w = img.shape[:2]
            fd.setInputSize((w, h))
            _, faces = fd.detect(img)
            if faces is not None:
                return [(x - 0.25 * fw, y - 0.35 * fh, x + 1.25 * fw, y + 1.2 * fh) for x, y, fw, fh in faces[:, :4]]
        return [(b[0], b[1], b[2], b[1] + 0.2 * (b[3] - b[1])) for b in persons]

    def upload(self, f):
        """True when the backend has the clip (local copy then removed)."""
        if not self.base:
            return False
        eid, ext = os.path.splitext(os.path.basename(f))
        try:
            with open(f, "rb") as fh:
                r = requests.post(f"{self.base}/api/events/{eid}/clip", data=fh.read(), timeout=10,
                                  headers={"Content-Type": "video/webm" if ext == ".webm" else "video/mp4"})
            if r.status_code < 300:
                os.remove(f)
                self.uploaded.append(eid)
                return True
        except (requests.RequestException, OSError):
            pass
        return False
