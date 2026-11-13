"""Worker identity: one person = one ID (consent-based registration with photos).

Workers are registered on the dashboard with 1-5 face photos and get a unique ID (W-001 ...).
Here every tracked person's face is matched against those photos, so the same worker keeps
the same ID even when the tracker loses them and gives a new track number.

  face detector  : YuNet   (OpenCV zoo, models/face_detection_yunet_2023mar.onnx)
  face embedding : SFace   (OpenCV zoo, models/face_recognition_sface_2021dec.onnx), cosine match
  decision       : several frames must agree (votes) before an ID is given to a track
  unregistered   : get a temporary visitor ID (V-001 ...) that lives in memory only - their
                   face data is never written to disk
Privacy: only registered (consenting) workers' photos + embeddings are stored, on this computer.
identity.enabled: false in settings.yaml switches all of this off.
"""
import json
import os
import threading
import time
from collections import Counter, deque

import cv2
import numpy as np

from .config import path

DET_MODEL = ("models", "face_detection_yunet_2023mar.onnx")
REC_MODEL = ("models", "face_recognition_sface_2021dec.onnx")


class FaceEngine:
    """Thin, thread-safe wrapper around YuNet + SFace."""

    def __init__(self, det_score=0.8, max_side=None):
        self.det = cv2.FaceDetectorYN.create(path(*DET_MODEL), "", (320, 320), det_score)
        self.rec = cv2.FaceRecognizerSF.create(path(*REC_MODEL), "")
        self.lock = threading.Lock()
        # 4 Nov: YuNet ran on the FULL camera frame (a phone gives 1080x1920) on the CPU, under one lock shared by
        # every camera -> the cameras queued behind each other. Detect on a copy scaled to max_side (e.g. 960 px)
        # and scale the boxes + landmarks back: same faces, a fraction of the work. Embeddings still use the
        # full-resolution frame, so recognition quality is unchanged.
        self.max_side = max_side
        self.ms = []

    def faces(self, img, max_side=None):
        import time as _t
        t0 = _t.perf_counter()
        h, w = img.shape[:2]
        ms = max_side or self.max_side
        k = min(1.0, float(ms) / max(h, w)) if ms else 1.0
        src = cv2.resize(img, (max(1, int(w * k)), max(1, int(h * k))), interpolation=cv2.INTER_AREA) if k < 1.0 else img
        sh, sw = src.shape[:2]
        with self.lock:
            self.det.setInputSize((sw, sh))
            _, f = self.det.detect(src)
        out = [] if f is None else [r for r in f]
        if k < 1.0 and out:
            out = [np.concatenate([r[:14] / k, r[14:]]).astype(np.float32) for r in out]
        self.ms.append((_t.perf_counter() - t0) * 1000)
        if len(self.ms) > 400:
            del self.ms[:200]
        return out

    def embed(self, img, face):
        with self.lock:
            e = self.rec.feature(self.rec.alignCrop(img, face)).flatten().astype(np.float32)
        return e / (np.linalg.norm(e) + 1e-9)

    def best_face(self, img, min_px=60):
        """Largest face in a registration photo -> (face_row, embedding). Raises ValueError."""
        fs = [f for f in self.faces(img) if f[2] >= min_px]
        if not fs:
            raise ValueError("no clear face found - face the camera, good light, no mask")
        f = max(fs, key=lambda r: r[2] * r[3])
        return f, self.embed(img, f)


def face_crop(img, face, pad=0.5, size=160):
    x, y, w, h = [int(v) for v in face[:4]]
    p = int(max(w, h) * pad)
    H, W = img.shape[:2]
    c = img[max(0, y - p):min(H, y + h + p), max(0, x - p):min(W, x + w + p)]
    return cv2.resize(c, (size, size)) if c.size else img


class IdentityService:
    """Shared by all cameras. update() writes worker_id / worker_name / identity into persons."""

    def __init__(self, cfg, backend_url=None, engine=None):
        c = cfg or {}
        self.enabled = c.get("enabled", True)
        # 12 Oct: two registered workers' photos are 0.38 alike (Chinmaya ~ Chandrabhanu), above the old
        # 0.36 - so names got mixed up. Stricter match + margin, and a name needs a clear MAJORITY of votes.
        self.match = c.get("match_threshold", 0.45)      # SFace cosine to a registered photo
        self.margin = c.get("margin", 0.08)              # best worker must beat the 2nd best by this
        self.votes_needed = c.get("votes_needed", 3)
        self.min_share = c.get("min_share", 0.6)         # ... and be >= 60 % of the named votes
        self.switch_votes = c.get("switch_votes", 5)     # changing an already given name needs this many
        self.switch_share = c.get("switch_share", 0.75)  # ... and this share of the named votes
        self.unknown_votes = c.get("unknown_votes", 5)
        self.every_n = c.get("every_n_frames", 3)
        self.min_face = c.get("min_face_px", 32)
        self.min_unknown_face = c.get("min_unknown_face_px", 56)   # 13 Nov: smaller unmatched faces never vote "unregistered"
        self.max_faces = c.get("max_faces_per_frame", 3)   # face embeddings per frame (many people in view)
        self.visitor_match = c.get("visitor_match", 0.42)
        self.learn_above = c.get("learn_above", 0.65)    # adapt to today's light only on very sure matches
        self.learn_margin = c.get("learn_margin", 0.15)
        self.debug_log = path(c["debug_log"]) if c.get("debug_log") else None
        self.backend = (backend_url or "").rstrip("/")
        self.cache = path(c.get("cache") or "data/gallery_cache.json")
        self.lock = threading.Lock()
        self.gallery = {}          # worker_id -> {"name", "embs": np.ndarray(k,128), "learned": [..]}
        self.visitors = []         # [{"id", "embs": [..]}]  memory only
        self.tracks = {}           # (cam, tid) -> state
        self.tlock = threading.RLock()  # tracks are shared with the background face thread (async mode)
        self.async_mode = False
        self.async_ms = deque(maxlen=200)   # background face work per frame (diagnostics)
        self.version = None
        self.engine = None
        if not self.enabled:
            return
        try:
            self.engine = engine or FaceEngine(max_side=c.get("detect_max_side", 960))
        except Exception as e:
            print(f"[identity] face models not available ({e!r}) - identity disabled")
            self.enabled = False
            return
        self._load_cache()
        if self.backend:
            threading.Thread(target=self._sync_loop, daemon=True, name="gallery-sync").start()

    # ---------- gallery ----------
    def set_gallery(self, workers):
        """workers: [{"worker_id", "name", "embeddings": [[128 floats], ...]}]"""
        with self.lock:
            new = {}
            for w in workers:
                embs = np.array(w.get("embeddings") or [], np.float32).reshape(-1, 128)
                if not len(embs):
                    continue
                old = self.gallery.get(w["worker_id"], {})
                new[w["worker_id"]] = {"name": w.get("name", ""), "embs": embs,
                                       "learned": old.get("learned", [])}
            removed = set(self.gallery) - set(new)
            self.gallery = new
            for st in self.tracks.values():            # a deleted worker loses their ID at once
                if st["id"] in removed:
                    st.update(id=None, name=None, kind=None)

    def _load_cache(self):
        try:
            with open(self.cache, encoding="utf-8") as f:
                self.set_gallery(json.load(f))
        except (OSError, ValueError):
            pass

    def _sync_loop(self):
        import requests
        s = requests.Session()
        while True:
            try:
                r = s.get(f"{self.backend}/api/workers/gallery", timeout=3)
                if r.ok:
                    data = r.json()
                    if data.get("version") != self.version:
                        self.version = data.get("version")
                        self.set_gallery(data["workers"])
                        with open(self.cache, "w", encoding="utf-8") as f:
                            json.dump(data["workers"], f)
                        print(f"[identity] gallery: {len(self.gallery)} registered worker(s)")
            except Exception:
                pass                                    # offline: keep the cached gallery
            time.sleep(5)

    # ---------- matching ----------
    def _score_workers(self, emb):
        scores = []
        for wid, g in self.gallery.items():
            embs = g["embs"] if not g["learned"] else np.vstack([g["embs"], np.array(g["learned"])])
            scores.append((float((embs @ emb).max()), wid))
        scores.sort(reverse=True)
        return scores

    def classify(self, emb):
        """-> (worker_id or None, best score)"""
        wid, best, _ = self.classify_full(emb)
        return wid, best

    def classify_full(self, emb):
        """-> (worker_id or None, best score, margin over the 2nd best worker)"""
        with self.lock:
            s = self._score_workers(emb)
        if not s:
            return None, 0.0, 0.0
        best, wid = s[0]
        second = s[1][0] if len(s) > 1 else -1.0
        if best >= self.match and best - second >= self.margin:
            return wid, best, best - second
        return None, best, best - second

    def _visitor(self, embs):
        for v in self.visitors:
            if max(float(np.dot(e, x)) for e in embs for x in v["embs"]) >= self.visitor_match:
                v["embs"] = (v["embs"] + embs)[-10:]
                return v["id"]
        vid = f"V-{len(self.visitors) + 1:03d}"
        self.visitors.append({"id": vid, "embs": list(embs)[-10:]})
        return vid

    # ---------- per frame ----------
    @staticmethod
    def _owner(face, persons):
        """Person whose box contains the face centre (smallest box wins). Not only the top of the
        box: in a webcam close-up (body cut off) the face sits low in the person box."""
        fx, fy = face[0] + face[2] / 2, face[1] + face[3] / 2
        best = None
        for p in persons:
            x1, y1, x2, y2 = p["bbox"]
            if x1 <= fx <= x2 and y1 - 0.1 * (y2 - y1) <= fy <= y2:
                a = (x2 - x1) * (y2 - y1)
                if best is None or a < best[0]:
                    best = (a, p)
        return best[1] if best else None

    def update(self, cam, frame, persons, n):
        """Synchronous: recognise on this frame (when due) and write the ids into persons."""
        if not self.enabled:
            return persons
        self._init(cam, persons)
        if self.wants(cam, persons, n):
            self._recognise(cam, frame, persons, routine=n % self.every_n == 0)
        return self._assign(cam, persons)

    # ---------- 4 Nov: asynchronous mode ----------
    # Face detection + embedding is CPU work (~100 ms per frame on the laptop, measured). Done inside the camera's
    # AI loop it delayed every 2nd frame exactly while an alert was being decided (new person = name pending).
    # Now the AI loop only hands the frame over (submit) and reads the names already known (annotate); one
    # background thread works on the newest frame of each camera. Votes, thresholds and naming rules are unchanged.
    def start_async(self):
        if not self.enabled or self.async_mode:
            return
        self._jobs, self._wake = {}, threading.Event()
        self.async_mode = True
        threading.Thread(target=self._async_loop, daemon=True, name="face-id").start()

    def wants(self, cam, persons, n):
        """Is a face check due on this frame? (every frame-pair while someone is unnamed, else every_n frames)"""
        if not persons:
            return False
        with self.tlock:
            pending = any((self.tracks.get((cam, p["tid"])) or {}).get("kind") is None for p in persons)
        # async: an unnamed person is checked on EVERY frame (the face thread just takes the newest one), so the
        # 3 votes a name needs arrive within ~1 s and the alarm can say the right name
        return n % self.every_n == 0 or (pending and (self.async_mode or n % 2 == 0))

    def submit(self, cam, frame, persons, n):
        """Non-blocking: queue this frame for the face thread (an older queued frame of the camera is replaced)."""
        self._init(cam, persons)
        snap = [{"tid": p["tid"], "bbox": list(p["bbox"])} for p in persons]
        self._jobs[cam] = (frame, snap, n % self.every_n == 0)
        self._wake.set()

    def annotate(self, cam, persons):
        """Write the ids known so far into persons (cheap; used by the AI loop in async mode)."""
        if not self.enabled:
            return persons
        self._init(cam, persons)
        return self._assign(cam, persons)

    def _async_loop(self):
        while True:
            self._wake.wait(1.0)
            self._wake.clear()
            for cam in list(self._jobs):
                job = self._jobs.pop(cam, None)
                if job is None:
                    continue
                t0 = time.perf_counter()
                try:
                    self._recognise(cam, job[0], job[1], routine=job[2])
                except Exception as e:                  # noqa: BLE001 - face ID must never stop
                    print(f"[identity] {cam}: {e!r}")
                self.async_ms.append((time.perf_counter() - t0) * 1000)

    def face_boxes(self, cam, tids, max_age=3.0):
        """Last seen face box per track id (for blurring snapshots without running the detector again)."""
        now = time.time()
        out = {}
        with self.tlock:
            for t in tids:
                st = self.tracks.get((cam, t))
                if st and st.get("face") and now - st["face"][1] <= max_age:
                    out[t] = st["face"][0]
        return out

    # ---------- shared steps ----------
    def _init(self, cam, persons):
        now = time.time()
        with self.tlock:
            for p in persons:
                st = self.tracks.setdefault((cam, p["tid"]), {"votes": deque(maxlen=8), "embs": deque(maxlen=6),
                                                               "id": None, "name": None, "kind": None,
                                                               "score": 0.0, "last": now})
                st["last"] = now

    def _recognise(self, cam, frame, persons, routine):
        # many people in view: embedding every face every time made alerts late. Unidentified people
        # first, biggest (closest) faces first, at most max_faces per frame; the rest wait a frame.
        cand = []
        now = time.time()
        for f in self.engine.faces(frame):
            if f[2] < self.min_face:
                continue
            p = self._owner(f, persons)
            if p is None:
                continue
            with self.tlock:
                st = self.tracks.get((cam, p["tid"]))
                if st is None:
                    continue
                st["face"] = ((float(f[0]), float(f[1]), float(f[2]), float(f[3])), now)
                unknown = st["kind"] is None
            if unknown or routine:
                cand.append((0 if unknown else 1, -float(f[2]) * float(f[3]), f, p))
        cand.sort(key=lambda c: (c[0], c[1]))
        for _, _, f, p in cand[:self.max_faces]:
            emb = self.engine.embed(frame, f)
            wid, score, margin = self.classify_full(emb)
            # 13 Nov: a SMALL face (far away, overhead CCTV) that matches nobody proves nothing - its embedding is too poor.
            # It may still confirm a name, but it never counts as an 'unregistered' vote.
            if wid is None and float(f[2]) < self.min_unknown_face:
                continue
            with self.tlock:
                st = self.tracks.get((cam, p["tid"]))
                if st is None:
                    continue
                st["votes"].append(wid)
                st["embs"].append(emb)
                before = st["id"]
                self._decide(st, wid, score, emb, margin)
                after = st["id"]
            if self.debug_log and after != before:
                self._log(f"{cam} track {p['tid']}: {before} -> {after} (vote {wid} score {score:.2f} "
                          f"margin {margin:.2f}, votes {list(st['votes'])})")

    def _assign(self, cam, persons):
        now = time.time()
        with self.tlock:
            self._unique(cam, persons)
            for p in persons:
                st = self.tracks[(cam, p["tid"])]
                p["worker_id"], p["worker_name"], p["identity"] = st["id"], st["name"], st["kind"]
            for k in [k for k, v in self.tracks.items() if now - v["last"] > 30]:
                del self.tracks[k]
        return persons

    def _decide(self, st, wid, score, emb, margin=1.0):
        votes = Counter(v for v in st["votes"] if v)
        if votes:
            top, cnt = votes.most_common(1)[0]
            share = cnt / sum(votes.values())
            if st["id"] != top:
                named = st["kind"] == "worker"          # already named: renaming needs much stronger evidence
                need, need_share = (self.switch_votes, self.switch_share) if named else (self.votes_needed, self.min_share)
                if cnt >= need and share >= need_share:
                    st.update(id=top, name=self.gallery.get(top, {}).get("name"), kind="worker", score=score)
        # 13 Nov (user: "registered people called unregistered"; log: votes ['W-004', None x5] -> V-014): a person who
        # matched a registered worker at least once needs 3x the unmatched faces before being called unregistered.
        n_unknown = sum(1 for v in st["votes"] if v is None)
        need = self.unknown_votes * (3 if any(st["votes"]) else 1)
        if st["kind"] is None and n_unknown >= need:
            st.update(id=self._visitor(list(st["embs"])), name=None, kind="visitor")
        if (st["kind"] == "worker" and wid == st["id"] and score >= self.learn_above
                and margin >= self.learn_margin):
            with self.lock:                             # adapt to today's light / helmet
                g = self.gallery.get(wid)
                if g is not None:
                    g["learned"] = (g["learned"] + [emb])[-15:]

    def _log(self, line):
        try:
            with open(self.debug_log, "a", encoding="utf-8") as f:
                f.write(time.strftime("%H:%M:%S ") + line + "\n")
        except OSError:
            pass

    def _unique(self, cam, persons):
        """One registered worker can be only one person in a camera at a time."""
        seen = {}
        for p in persons:
            st = self.tracks[(cam, p["tid"])]
            if st["kind"] != "worker":
                continue
            other = seen.get(st["id"])
            if other is not None:
                loser = st if len(st["votes"]) < len(other["votes"]) else other
                loser.update(id=None, name=None, kind=None)
                loser["votes"].clear()
            else:
                seen[st["id"]] = st
