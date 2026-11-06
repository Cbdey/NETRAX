"""Smooth live view, independent of the AI (4 Nov: "video hangs when all cameras run").

Before: the dashboard picture of a camera was produced by its AI worker, so the live view could never be faster
than the AI (with people in view and 2+ cameras that fell to ~1 frame per second -> the video "hung").
Now: this thread takes the NEWEST camera frame at `fps` (e.g. 12/s) for every camera, draws the LATEST AI result
on it (boxes, worker labels, item lines, zone lines, entrance banner) and hands it to the publisher. The video
stays smooth whatever the AI rate is; the boxes update whenever the AI finishes a frame. A small corner label
shows the AI rate, so nobody mistakes the overlay for a per-frame result.

Cheap by design: the frame is first scaled down to the preview width, then drawn on (boxes scaled), so 4 cameras
at 12 fps cost a few ms each. The short incident clips (edge/clips.py) are fed from here as well, so clips are smooth.
Alerts, snapshots and every safety decision still come from the AI worker, on the exact frame it judged."""
import threading
import time

import cv2

from . import annotate


def _scale_box(b, k):
    return [v * k for v in b]


def _scale_overlay(ov, k):
    persons = []
    for p in ov["persons"]:
        q = dict(p)
        q["bbox"] = _scale_box(p["bbox"], k)
        q.pop("kps", None)
        persons.append(q)
    items = [dict(i, bbox=_scale_box(i["bbox"], k)) for i in ov["items"]]
    parts = [dict(i, bbox=_scale_box(i["bbox"], k)) for i in ov["parts"]]
    fire = [(c, _scale_box(b, k), s, conf) for c, b, s, conf in ov["fire"]]
    rej = [(c, _scale_box(b, k), why) for c, b, why in ov["rejected"]]
    return persons, items, parts, fire, rej


class Motion:
    """6 Nov (user: "the live video must move like the real action"): the picture runs at camera speed, but boxes /
    labels only changed when the AI finished a frame (2-8 times a second) - they trailed a walking person and then
    jumped. From the last two AI results each person's velocity is known; the boxes are moved forward to the time of
    the picture being shown (at most `horizon` s ahead), so they glide with the person. Display only: alerts,
    snapshots and every safety decision still use the exact AI result."""

    def __init__(self, horizon=0.5, smooth=0.6):
        self.horizon, self.smooth = horizon, smooth
        self.prev = {}        # cam -> (ai_ts, {tid: bbox})
        self.vel = {}         # (cam, tid) -> [vx1, vy1, vx2, vy2] px/s

    def learn(self, cam, ov):
        ts = ov.get("ts")
        if ts is None:
            return
        last = self.prev.get(cam)
        if last is not None and last[0] == ts:
            return                                    # same AI result as before
        now = {p["tid"]: list(p["bbox"]) for p in ov["persons"] if p.get("tid") is not None}
        if last is not None:
            dt = ts - last[0]
            for tid, b in now.items():
                old = last[1].get(tid)
                if old is None or not 0.03 <= dt <= 1.0:
                    self.vel.pop((cam, tid), None)
                    continue
                v = [(b[i] - old[i]) / dt for i in range(4)]
                pv = self.vel.get((cam, tid))
                self.vel[(cam, tid)] = v if pv is None else [self.smooth * v[i] + (1 - self.smooth) * pv[i] for i in range(4)]
            for key in [k for k in self.vel if k[0] == cam and k[1] not in now]:
                del self.vel[key]
        self.prev[cam] = (ts, now)

    def shift(self, cam, ov, frame_ts, frame_w):
        """-> {tid: [dx1, dy1, dx2, dy2]} in original pixels for the picture taken at frame_ts."""
        ts = ov.get("ts")
        if ts is None or frame_ts is None:
            return {}
        dt = max(0.0, min(self.horizon, frame_ts - ts))
        if dt <= 0:
            return {}
        cap = 1.5 * frame_w                            # never more than 1.5 picture widths per second
        out = {}
        for p in ov["persons"]:
            v = self.vel.get((cam, p.get("tid")))
            if v:
                out[p["tid"]] = [max(-cap, min(cap, x)) * dt for x in v]
        return out


def _move(b, d):
    return [b[0] + d[0], b[1] + d[1], b[2] + d[2], b[3] + d[3]]


def _owner(b, persons):
    """The person an item box belongs to (its centre inside the person box, nearest centre wins)."""
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    best, bd = None, None
    for p in persons:
        q = p["bbox"]
        if q[0] <= cx <= q[2] and q[1] <= cy <= q[3]:
            d = (cx - (q[0] + q[2]) / 2) ** 2 + (cy - (q[1] + q[3]) / 2) ** 2
            if bd is None or d < bd:
                best, bd = p.get("tid"), d
    return best


def predict_overlay(ov, shifts):
    """Copy of the AI overlay with person / item / body-part boxes moved by their person's predicted shift."""
    if not shifts:
        return ov
    persons = [dict(p, bbox=_move(p["bbox"], shifts[p.get("tid")])) if p.get("tid") in shifts else p
               for p in ov["persons"]]

    def mv(lst):
        out = []
        for i in lst:
            t = _owner(i["bbox"], ov["persons"])
            out.append(dict(i, bbox=_move(i["bbox"], shifts[t])) if t in shifts else i)
        return out
    return dict(ov, persons=persons, items=mv(ov["items"]), parts=mv(ov["parts"]))


class LiveView(threading.Thread):
    def __init__(self, readers, workers, publisher, clips=None, fps=12, width=800, max_overlay_age_s=2.0, predict=True):
        super().__init__(daemon=True, name="live-view")
        self.readers, self.workers, self.publisher, self.clips = readers, workers, publisher, clips
        self.gap = 1.0 / max(1.0, float(fps))
        self.width = int(width)
        self.max_age = float(max_overlay_age_s)
        self.last_no = {}
        self.ms = []                       # draw cost per frame (diagnostics)
        self.sent = {}                     # cam -> frames sent (diagnostics)
        self.motion = Motion() if predict else None

    def run(self):
        # 5 Nov (user: "still looks hanging"): a fixed 15/s clock sampling a camera that itself delivers ~15/s beats
        # against it - now and then a frame is skipped and the next one shows twice as late (a visible stutter).
        # Now every NEW camera frame is sent as soon as it arrives (checked every 4 ms), only capped at `fps`, so each
        # camera plays at its own natural, even rhythm (laptop 30/s, phone 15/s, USB as fast as it delivers).
        last_sent = {}
        while True:
            for cam, r in self.readers.items():
                now = time.perf_counter()
                if now - last_sent.get(cam, 0.0) < self.gap * 0.8:
                    continue
                try:
                    if self._one(cam, r):
                        last_sent[cam] = now
                except Exception as e:     # noqa: BLE001 - the live view must never stop
                    print(f"[live] {cam}: {e!r}")
            time.sleep(0.004)

    def _one(self, cam, r):
        frame, ts, no = r.latest()
        if frame is None or no == self.last_no.get(cam):
            return False
        self.last_no[cam] = no
        t = time.perf_counter()
        h, w = frame.shape[:2]
        k = min(1.0, self.width / float(w))
        small = cv2.resize(frame, (int(w * k), int(h * k)), interpolation=cv2.INTER_AREA) if k < 1.0 else frame.copy()
        wk = self.workers.get(cam)
        ov = getattr(wk, "overlay", None)
        persons = []
        if ov and time.time() - ov["at"] <= self.max_age:
            if self.motion is not None:                  # boxes follow the person between AI results (display only)
                self.motion.learn(cam, ov)
                ov = predict_overlay(ov, self.motion.shift(cam, ov, ts, w))
            persons, items, parts, fire, rej = _scale_overlay(ov, k)
            polys = ov["polys"]
            img = annotate.draw(small, persons, ov["shown"], items, fire, polys, rej, parts=parts)
            if ov.get("entrance") is not None:
                annotate.entrance_banner(img, ov["entrance"])
            ai = wk.processed_fps() if wk else 0.0
            label = f"AI {ai:.1f} fps"
        else:
            img = small
            label = "AI starting..." if wk is not None and not wk.proc_times else "AI: no recent result"
        cv2.putText(img, label, (8, img.shape[0] - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(img, label, (8, img.shape[0] - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)
        self.publisher.frame(cam, img, ts)
        if self.clips is not None and self.clips.due(cam, ts):
            self.clips.add(cam, img, ts, [{"bbox": p["bbox"]} for p in persons])
        self.sent[cam] = self.sent.get(cam, 0) + 1
        self.ms.append((time.perf_counter() - t) * 1000)
        if len(self.ms) > 400:
            del self.ms[:200]
        return True
