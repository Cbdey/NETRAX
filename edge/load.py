"""Load-aware processing (plan section 7.5).

All cameras share one GPU. When cameras cannot keep their target FPS, slow down the
LOWEST-risk zones first (warehouse before welding bay), a step at a time, and give the FPS back
when the load drops. High-risk cameras are only slowed once every lower-risk camera is already
at the minimum. No camera is ever stopped, so fire/smoke checks keep running on every stream
(the fire model also switches to every frame as soon as a candidate appears, see worker.py).
"""
RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


class LoadManager:
    def __init__(self, cfg, base_fps):
        c = cfg or {}
        self.enabled = c.get("enabled", True)
        self.base = base_fps
        self.min_fps = c.get("min_fps", 4)
        self.lag_ratio = c.get("lag_ratio", 0.85)        # achieved < 85% of target = lagging
        self.lag_steps = c.get("lag_steps", 3)           # ... for this many checks in a row
        self.ok_steps = c.get("recover_steps", 10)       # all healthy this long -> give FPS back
        self._lag, self._ok = 0, 0
        self.note = None

    @staticmethod
    def _achievable(w):
        """A 5 fps video file can never be processed at 10 fps - that is not overload."""
        src = getattr(w.reader, "fps", 0) or 0
        return min(w.fps_target, src) if src > 0 else w.fps_target

    def step(self, workers, risk_of):
        """workers: {cam_id: CameraWorker}; risk_of: cam_id -> 'low'|'medium'|'high'.
        Returns a human-readable note when something changed, else None."""
        if not self.enabled or not workers:
            return None
        live = {c: w for c, w in workers.items() if w.reader.frame_ts and w.processed_fps() > 0}
        lagging = [c for c, w in live.items() if w.processed_fps() < self.lag_ratio * self._achievable(w)]
        if lagging:
            self._lag, self._ok = self._lag + 1, 0
        else:
            self._lag, self._ok = 0, self._ok + 1
        if self._lag >= self.lag_steps:
            self._lag = 0
            cands = sorted((RISK_ORDER.get(risk_of(c), 1), -w.fps_target, c)
                           for c, w in live.items() if w.fps_target > self.min_fps)
            if cands:
                c = cands[0][2]
                w = workers[c]
                w.set_target(max(self.min_fps, w.fps_target - 2))
                self.note = f"GPU busy: {c} ({risk_of(c)} risk) slowed to {w.fps_target} fps"
                return self.note
        if self._ok >= self.ok_steps:
            self._ok = 0
            cands = sorted((-RISK_ORDER.get(risk_of(c), 1), w.fps_target, c)
                           for c, w in workers.items() if w.fps_target < self.base)
            if cands:
                c = cands[0][2]
                w = workers[c]
                w.set_target(min(self.base, w.fps_target + 1))
                self.note = None if all(x.fps_target == self.base for x in workers.values()) \
                    else f"recovering: {c} back to {w.fps_target} fps"
                return f"load dropped: {c} back to {w.fps_target} fps"
        return None
