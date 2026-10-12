"""Fire/smoke verification (F13, plan section 7.4).

Stage 1 detect  -> raw detections from the fire/smoke model
Stage 2 track   -> follow each region over frames: area growth + flicker
Stage 1b colour check -> a 'fire' box must contain real flame colours (bright red/orange/
                   yellow); a 'smoke' box must be greyish and not overexposed. This rejects
                   phone flashes, white lamps and ceiling lights.
Stage 3 suppress look-alikes -> region mostly on a tracked person (orange/yellow vest),
                                or static and non-flickering (painted machine, lamp, cone)
Stage 4 confirm -> N of last M fire-frames + grace; grade High (small/static) or
                   Critical (growing or persistent)
"""
import os
import time
import uuid
from collections import deque

import cv2
import numpy as np

from .config import path
from .geometry import iou, intersection, area, center
from .severity import fire_severity


class _Region:
    def __init__(self, cls, bbox, now, m):
        self.id = uuid.uuid4().hex[:6]
        self.cls = cls
        self.bbox = bbox
        self.conf = 0.0
        self.first_seen = now
        self.last_seen = now
        self.hits = deque(maxlen=m)
        self.areas = deque(maxlen=30)
        self.crops = deque(maxlen=6)
        self.hot = 0.0          # share of white-hot flame core in the last detection
        self.skin = 0.0         # share of skin-coloured pixels in the last smoke detection
        self.event_id = None
        self.severity = None
        self.status = "verifying"


class FireVerifier:
    def __init__(self, cfg):
        self.m = cfg["window_m"]
        self.n = cfg["confirm_n"]
        self.grace = cfg["grace_s"]
        self.smoke_grace = cfg.get("smoke_grace_s", self.grace)
        self.fire_color_min = cfg.get("fire_color_min", 0.08)
        self.smoke_max_sat = cfg.get("smoke_max_sat", 90)
        self.smoke_max_bright = cfg.get("smoke_max_bright", 225)
        self.smoke_min_texture = cfg.get("smoke_min_texture", 8)
        self.smoke_min_bright = cfg.get("smoke_min_bright", 70)
        self.smoke_max_skin = cfg.get("smoke_max_skin", 0.25)
        self.smoke_max_dark = cfg.get("smoke_max_dark", 0.30)
        self.smoke_max_glare = cfg.get("smoke_max_glare", 0.25)
        self.smoke_skin_on_person = cfg.get("smoke_skin_on_person", 0.12)
        self.smoke_person_iou = cfg.get("smoke_person_iou", 0.5)     # smoke box == a person box -> it is the person
        self.smoke_static_crops = cfg.get("smoke_static_crops", 6)   # smoke that never moves for this many crops
        self.hot_min = cfg.get("hot_core_min", 0.02)
        # dim / dark room: sensor noise and grey haze look like thin smoke -> demand stronger evidence
        self.dark_luma = cfg.get("dark_scene_luma", 70)
        self.dark_smoke_conf = cfg.get("dark_smoke_min_conf", 0.55)
        self.dark_fire_conf = cfg.get("dark_fire_min_conf", 0.45)
        self.debug_path = cfg.get("debug_log")
        self.scene_luma = {}   # cam -> mean brightness of the last checked frame
        if self.debug_path and not os.path.isabs(self.debug_path):
            self.debug_path = path(self.debug_path)
        self._dbg_last = {}
        self.rejected = {}     # cam -> [(cls, bbox, reason)] from the last fire-frame
        self.growth_ratio = cfg["growth_ratio"]
        self.persistent_s = cfg["persistent_s"]
        self.flicker_min = cfg["flicker_min"]
        self.person_overlap = cfg["person_overlap_suppress"]
        self.lost = cfg["lost_s"]
        self.cooldown = cfg["cooldown_s"]
        self.regions = {}      # cam -> [ _Region ]
        self.last_event = {}   # (cam, cls) -> time of last opened event

    @staticmethod
    def _crop(gray, bbox):
        h, w = gray.shape[:2]
        x1, y1 = max(0, int(bbox[0])), max(0, int(bbox[1]))
        x2, y2 = min(w, int(bbox[2])), min(h, int(bbox[3]))
        if x2 - x1 < 4 or y2 - y1 < 4:
            return None
        return cv2.resize(gray[y1:y2, x1:x2], (32, 32)).astype(np.float32)

    def colour_ok(self, frame, d):
        """Stage 1b: reject detections whose pixels do not look like flame / smoke."""
        h, w = frame.shape[:2]
        x1, y1 = max(0, int(d["bbox"][0])), max(0, int(d["bbox"][1]))
        x2, y2 = min(w, int(d["bbox"][2])), min(h, int(d["bbox"][3]))
        if x2 - x1 < 4 or y2 - y1 < 4:
            return False, "too small"
        roi = frame[y1:y2, x1:x2]
        if roi.shape[0] * roi.shape[1] > 160 * 160:
            roi = cv2.resize(roi, (160, 160))
        if d["cls"] == "Fire":
            b, g, r = [roi[:, :, i].astype(np.int16) for i in range(3)]
            flame = (r >= 170) & (r > g) & (g >= b) & ((r - b) >= 60)
            # a real flame is a light source: its core saturates the webcam to near-white
            # (a match / lighter / candle is mostly core with a thin orange rim)
            hot = (np.minimum(np.minimum(r, g), b) >= 200) & (r >= 235)
            share, hot_share = float(flame.mean()), float(hot.mean())
            d["hot"] = hot_share
            ok = share >= self.fire_color_min or (share >= 0.02 and hot_share >= self.hot_min)
            return ok, f"flame colour {share:.0%}, hot core {hot_share:.0%}"
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        sat, val = float(hsv[:, :, 1].mean()), float(hsv[:, :, 2].mean())
        # Measured on all 25 'smoke' false alarms from our live tests (faces, hair, phone flash):
        # skin / dark-hair / glare shares separate them from grey smoke (docs/failure_gallery).
        ycc = cv2.cvtColor(roi, cv2.COLOR_BGR2YCrCb)
        skin = float(((ycc[:, :, 1] >= 135) & (ycc[:, :, 1] <= 180)
                      & (ycc[:, :, 2] >= 85) & (ycc[:, :, 2] <= 135)).mean())
        d["skin"] = skin
        if skin >= self.smoke_max_skin:
            return False, f"skin {skin:.0%} (a face / arm, not smoke)"
        if float((hsv[:, :, 2] < 60).mean()) >= self.smoke_max_dark:
            return False, "dark hair / shadow"
        if float((hsv[:, :, 2] >= 250).mean()) >= self.smoke_max_glare:
            return False, "glare (phone flash / lamp)"
        if val > self.smoke_max_bright:
            return False, "overexposed (light/flash)"
        if val < self.smoke_min_bright:
            return False, "too dark for smoke (hair / shadow)"
        if sat > self.smoke_max_sat:
            return False, "too colourful for smoke"
        tex = float(cv2.Laplacian(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY), cv2.CV_32F).var())
        if tex < self.smoke_min_texture:
            return False, "no texture (lens covered / out of focus)"
        return True, "ok"

    def _flicker(self, r):
        c = [x for x in r.crops if x is not None]
        if len(c) < 3:
            return None
        return float(np.mean([np.mean(np.abs(c[i] - c[i - 1])) for i in range(1, len(c))]))

    def _growth(self, r):
        if len(r.areas) < 3:
            return 1.0
        early = min(list(r.areas)[:3]) or 1.0
        return r.areas[-1] / early

    def _lookalike(self, r, person_boxes):
        if self._growth(r) >= self.growth_ratio:
            return False       # a growing region is never suppressed
        a = area(r.bbox) or 1.0
        fl = self._flicker(r)
        flickering = fl is not None and fl >= self.flicker_min
        on_person = any(intersection(r.bbox, pb) / a >= self.person_overlap for pb in person_boxes)
        if on_person:
            # Clothing does not glow and smoke does not stand still. A flame held in the hand
            # (match, lighter) glows white-hot and flickers; smoke rising past a person drifts.
            if r.cls == "Fire" and not (flickering and r.hot >= self.hot_min):
                return True              # orange vest / red jacket on a worker
            if r.cls == "Smoke" and r.skin >= self.smoke_skin_on_person:
                return True              # box on a person with visible skin = a face, not smoke
            if r.cls == "Smoke" and fl is not None and not flickering and len(r.crops) >= 4:
                return True              # static grey shirt / hair
        if r.cls == "Fire" and fl is not None and not flickering and len(r.crops) >= 4:
            return True                  # static, uniform colour = machine/lamp/cone
        if r.cls == "Smoke" and fl is not None and not flickering and len(r.crops) >= self.smoke_static_crops:
            return True                  # smoke always drifts; a static grey patch = bedsheet / wall / shirt
        return False

    def _log(self, cam, r, msg, now):
        """Throttled trace of every fire/smoke candidate -> settings fire.debug_log (to tune on
        real footage: why did this match / agarbatti stay 'verifying'?)."""
        if not self.debug_path or now - self._dbg_last.get(r.id, 0) < 1.0:
            return
        self._dbg_last[r.id] = now
        fl = self._flicker(r)
        try:
            with open(self.debug_path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {cam} {r.cls:5s} id={r.id} conf={r.conf:.2f} "
                        f"hits={sum(r.hits)}/{len(r.hits)} age={now - r.first_seen:.1f}s "
                        f"flicker={'-' if fl is None else f'{fl:.1f}'} hot={r.hot:.0%} "
                        f"growth={self._growth(r):.2f} -> {msg}\n")
        except OSError:
            pass

    def update(self, cam, frame, dets, person_boxes, now):
        """dets: [{'cls','conf','bbox'}] or None when the fire model did not run this frame.
        Returns (actions, tile_status)."""
        regions = self.regions.setdefault(cam, [])
        actions = []
        if dets is not None:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            matched = set()
            kept, rej = [], []
            fires = [d["bbox"] for d in dets if d["cls"] == "Fire"]
            self.scene_luma[cam] = luma = float(gray.mean())
            dark = luma < self.dark_luma
            for d in dets:
                if dark and d["conf"] < (self.dark_smoke_conf if d["cls"] == "Smoke" else self.dark_fire_conf):
                    rej.append((d["cls"], d["bbox"], f"dark scene (brightness {luma:.0f}): conf {d['conf']:.2f} too weak"))
                    continue
                # the white-hot core of a flame also looks like 'smoke' to the model (soak test,
                # phone fire video): smoke sitting mostly inside a fire box is part of that fire
                if d["cls"] == "Smoke" and any(intersection(d["bbox"], fb) / (area(d["bbox"]) or 1.0)
                                               >= self.person_overlap for fb in fires):
                    rej.append((d["cls"], d["bbox"], "inside a fire box (flame core, not smoke)"))
                    continue
                # a "smoke" box that IS a person box (white T-shirt + face, live 10 Oct): real smoke
                # around a worker is a drifting plume, not a box the size and place of the person
                if d["cls"] == "Smoke" and any(iou(d["bbox"], pb) >= self.smoke_person_iou for pb in person_boxes):
                    rej.append((d["cls"], d["bbox"], "same box as a person (shirt / face), not smoke"))
                    continue
                ok, why = self.colour_ok(frame, d)
                (kept if ok else rej).append(d if ok else (d["cls"], d["bbox"], why))
            self.rejected[cam] = rej
            if self.debug_path and rej:
                with open(self.debug_path, "a", encoding="utf-8") as f:
                    for cls, _, why in rej:
                        f.write(f"{time.strftime('%H:%M:%S')} {cam} {cls:5s} rejected: {why}\n")
            for d in kept:
                best, best_score = None, 0.0
                for r in regions:
                    if r.cls != d["cls"] or r.id in matched:
                        continue
                    s = iou(r.bbox, d["bbox"])
                    if s < 0.1:
                        # smoke drifts and changes shape: accept the centre anywhere in the
                        # region grown by 50%, so a plume stays ONE region and keeps its hits
                        cx, cy = center(d["bbox"])
                        gx, gy = (r.bbox[2] - r.bbox[0]) * 0.5, (r.bbox[3] - r.bbox[1]) * 0.5
                        if r.bbox[0] - gx <= cx <= r.bbox[2] + gx and r.bbox[1] - gy <= cy <= r.bbox[3] + gy:
                            s = 0.1
                    if s > best_score:
                        best, best_score = r, s
                if best is None:
                    best = _Region(d["cls"], d["bbox"], now, self.m)
                    regions.append(best)
                matched.add(best.id)
                best.bbox, best.conf, best.last_seen = d["bbox"], d["conf"], now
                best.hot, best.skin = d.get("hot", 0.0), d.get("skin", 0.0)
                best.areas.append(area(d["bbox"]))
                best.crops.append(self._crop(gray, d["bbox"]))
            for r in regions:
                r.hits.append(r.id in matched)

        for r in list(regions):
            if now - r.last_seen > self.lost:
                if r.event_id:
                    actions.append(self._action("resolve", cam, r, now, "cleared"))
                regions.remove(r)
                continue
            if self._lookalike(r, person_boxes):
                r.status = "suppressed"
                self._log(cam, r, "suppressed (look-alike: on a person / static)", now)
                continue
            grace = self.smoke_grace if r.cls == "Smoke" else self.grace
            if sum(r.hits) >= self.n and now - r.first_seen >= grace:
                growing = self._growth(r) >= self.growth_ratio
                persistent = now - r.first_seen >= self.persistent_s
                sev = fire_severity(growing, persistent)
                r.status = "confirmed"
                if r.event_id is None:
                    if self._other_open(cam, r):
                        continue         # one open fire (or smoke) event per camera
                    r.event_id = "evt_" + uuid.uuid4().hex[:10]
                    r.severity = sev
                    self.last_event[(cam, r.cls)] = now
                    actions.append(self._action("open", cam, r, now))
                elif sev != r.severity and sev == "Critical":
                    r.severity = sev
                    actions.append(self._action("update", cam, r, now))
            else:
                r.status = "verifying"
            self._log(cam, r, r.status, now)
        return actions, self.tile_status(cam)

    def _other_open(self, cam, r):
        return any(o is not r and o.cls == r.cls and o.event_id for o in self.regions.get(cam, []))

    def tile_status(self, cam):
        """Badge for the camera tile: strongest current fire/smoke state."""
        best = None
        for r in self.regions.get(cam, []):
            if r.status == "confirmed":
                label = f"{r.cls.upper()} · {(r.severity or 'High').upper()}"
                rank = 3 if r.severity == "Critical" else 2
            elif r.status == "verifying" and sum(r.hits) > 0:
                label = f"verifying {r.cls.lower()} ({sum(r.hits)}/{self.n} frames)"
                rank = 1
            else:
                continue
            if best is None or rank > best[0]:
                best = (rank, label)
        return best[1] if best else None

    def boxes(self, cam):
        return [(r.cls, r.bbox, r.status, r.conf) for r in self.regions.get(cam, [])]

    def _action(self, kind, cam, r, now, reason=None):
        return {
            "action": kind,
            "event_id": r.event_id,
            "type": r.cls.lower(),                 # 'fire' | 'smoke'
            "items_missing": [],
            "severity": r.severity,
            "camera_id": cam,
            "zone_id": None,                        # filled in by the camera worker
            "track_id": None,
            "confidence": round(float(r.conf), 3),
            "bbox": [int(v) for v in r.bbox],
            "started_at": r.first_seen,
            "onset_at": r.first_seen,
            "at": now,
            "reason": reason,
            "growth": round(self._growth(r), 2),
        }
