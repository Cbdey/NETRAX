"""Person tracking (F04). Uses ByteTrack from Supervision; falls back to a simple
IoU tracker automatically if the Supervision API is unavailable (plan Plan B)."""
from collections import deque

import numpy as np

from .geometry import iou


class IoUTracker:
    """Fallback tracker: IoU matching, with box-centre distance as a tie-breaker."""

    def __init__(self, max_age=15, min_iou=0.25):
        self.next_id = 1
        self.tracks = {}   # tid -> [bbox, age]
        self.max_age = max_age
        self.min_iou = min_iou

    def update(self, boxes, confs):
        assigned, out = set(), []
        for b, c in sorted(zip(boxes, confs), key=lambda x: -x[1]):
            best, best_iou = None, self.min_iou
            for tid, (tb, _) in self.tracks.items():
                if tid in assigned:
                    continue
                s = iou(tb, b)
                if s <= 0.05:                     # no overlap: allow a small drift match
                    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
                    tx, ty = (tb[0] + tb[2]) / 2, (tb[1] + tb[3]) / 2
                    span = max(1.0, (tb[2] - tb[0]))
                    if abs(cx - tx) < span * 0.6 and abs(cy - ty) < span * 0.9:
                        s = 0.26
                if s > best_iou:
                    best, best_iou = tid, s
            if best is None:
                best = self.next_id
                self.next_id += 1
            self.tracks[best] = [b, 0]
            assigned.add(best)
            out.append((best, b, c))
        for tid in list(self.tracks):
            if tid not in assigned:
                self.tracks[tid][1] += 1
                if self.tracks[tid][1] > self.max_age:
                    del self.tracks[tid]
        return out


class TrackConfidence:
    """Rolling mean of each track's person confidence. Static look-alikes (clothes on a
    hanger, a coat on a chair) sit around 0.5-0.6 frame after frame; real people score higher."""

    def __init__(self, window=20, forget_after=100):
        self.hist = {}      # tid -> (deque, last_tick)
        self.window, self.forget_after, self.tick = window, forget_after, 0

    def update(self, persons):
        self.tick += 1
        for p in persons:
            q = self.hist.get(p["tid"], (deque(maxlen=self.window), 0))[0]
            q.append(p["conf"])
            self.hist[p["tid"]] = (q, self.tick)
            p["track_conf"] = sum(q) / len(q)
        for tid in [t for t, (_, last) in self.hist.items() if self.tick - last > self.forget_after]:
            del self.hist[tid]
        return persons


class PersonTracker:
    def __init__(self, fps=10):
        self.kind = "iou"
        self.impl = None
        try:
            import supervision as sv
            try:
                self.impl = sv.ByteTrack(frame_rate=fps, lost_track_buffer=fps * 5,
                                         minimum_consecutive_frames=2,
                                         minimum_matching_threshold=0.85)
            except TypeError:
                self.impl = sv.ByteTrack()
            self.sv = sv
            self.kind = "bytetrack"
        except Exception:
            self.impl = IoUTracker(max_age=fps * 5)

    def update(self, boxes, confs):
        """boxes: list of [x1,y1,x2,y2]; returns list of (tid, bbox, conf)."""
        if self.kind == "iou":
            return self.impl.update(boxes, confs)
        if not boxes:
            det = self.sv.Detections.empty()
        else:
            det = self.sv.Detections(
                xyxy=np.array(boxes, dtype=np.float32),
                confidence=np.array(confs, dtype=np.float32),
                class_id=np.zeros(len(boxes), dtype=int),
            )
        try:
            tr = self.impl.update_with_detections(det)
        except Exception:
            # Supervision API changed - switch to the IoU tracker for good.
            self.kind, self.impl = "iou", IoUTracker()
            return self.impl.update(boxes, confs)
        if tr.tracker_id is None:
            return []
        return [(int(t), [float(v) for v in b], float(c))
                for t, b, c in zip(tr.tracker_id, tr.xyxy, tr.confidence)]
