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

    def __init__(self, det_score=0.8):
        self.det = cv2.FaceDetectorYN.create(path(*DET_MODEL), "", (320, 320), det_score)
        self.rec = cv2.FaceRecognizerSF.create(path(*REC_MODEL), "")
        self.lock = threading.Lock()

    def faces(self, img):
        h, w = img.shape[:2]
        with self.lock:
            self.det.setInputSize((w, h))
            _, f = self.det.detect(img)
        return [] if f is None else [r for r in f]

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
        self.version = None
        self.engine = None
        if not self.enabled:
            return
        try:
            self.engine = engine or FaceEngine()
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
        if not self.enabled:
            return persons
        now = time.time()
        for p in persons:
            st = self.tracks.setdefault((cam, p["tid"]), {"votes": deque(maxlen=8), "embs": deque(maxlen=6),
                                                           "id": None, "name": None, "kind": None,
                                                           "score": 0.0, "last": now})
            st["last"] = now
        pending = any(self.tracks[(cam, p["tid"])]["kind"] is None for p in persons)   # not yet worker/visitor
        if persons and (n % self.every_n == 0 or (pending and n % 2 == 0)):
            # many people in view: embedding every face every time made alerts late. Unidentified people
            # first, biggest (closest) faces first, at most max_faces per frame; the rest wait a frame.
            routine = n % self.every_n == 0
            cand = []
            for f in self.engine.faces(frame):
                if f[2] < self.min_face:
                    continue
                p = self._owner(f, persons)
                if p is None:
                    continue
                unknown = self.tracks[(cam, p["tid"])]["kind"] is None
                if unknown or routine:
                    cand.append((0 if unknown else 1, -float(f[2]) * float(f[3]), f, p))
            cand.sort(key=lambda c: (c[0], c[1]))
            for _, _, f, p in cand[:self.max_faces]:
                emb = self.engine.embed(frame, f)
                wid, score, margin = self.classify_full(emb)
                st = self.tracks[(cam, p["tid"])]
                st["votes"].append(wid)
                st["embs"].append(emb)
                before = st["id"]
                self._decide(st, wid, score, emb, margin)
                if self.debug_log and st["id"] != before:
                    self._log(f"{cam} track {p['tid']}: {before} -> {st['id']} (vote {wid} score {score:.2f} "
                              f"margin {margin:.2f}, votes {list(st['votes'])})")
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
        if st["kind"] is None and sum(1 for v in st["votes"] if v is None) >= self.unknown_votes:
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
