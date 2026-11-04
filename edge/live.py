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


class LiveView(threading.Thread):
    def __init__(self, readers, workers, publisher, clips=None, fps=12, width=800, max_overlay_age_s=2.0):
        super().__init__(daemon=True, name="live-view")
        self.readers, self.workers, self.publisher, self.clips = readers, workers, publisher, clips
        self.gap = 1.0 / max(1.0, float(fps))
        self.width = int(width)
        self.max_age = float(max_overlay_age_s)
        self.last_no = {}
        self.ms = []                       # draw cost per frame (diagnostics)
        self.sent = {}                     # cam -> frames sent (diagnostics)

    def run(self):
        while True:
            t0 = time.perf_counter()
            for cam, r in self.readers.items():
                try:
                    self._one(cam, r)
                except Exception as e:     # noqa: BLE001 - the live view must never stop
                    print(f"[live] {cam}: {e!r}")
            spent = time.perf_counter() - t0
            time.sleep(max(0.003, self.gap - spent))

    def _one(self, cam, r):
        frame, ts, no = r.latest()
        if frame is None or no == self.last_no.get(cam):
            return
        self.last_no[cam] = no
        t = time.perf_counter()
        h, w = frame.shape[:2]
        k = min(1.0, self.width / float(w))
        small = cv2.resize(frame, (int(w * k), int(h * k)), interpolation=cv2.INTER_AREA) if k < 1.0 else frame.copy()
        wk = self.workers.get(cam)
        ov = getattr(wk, "overlay", None)
        persons = []
        if ov and time.time() - ov["at"] <= self.max_age:
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
