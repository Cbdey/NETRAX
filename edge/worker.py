"""Camera worker: newest frame -> detection -> tracking -> association -> zones ->
state machine / fire verification -> events (outbox) + preview frames."""
import base64
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

from . import annotate
from .access import AccessGuard
from .association import evaluate
from .config import path
from .detector import Detector
from .entrance import EntranceChecker
from .fire_verify import FireVerifier
from .priority import GATE, RANK
from .state_machine import PPEStateMachine
from .tracker import PersonTracker, TrackConfidence
from .zones import ZoneMap


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class CameraWorker(threading.Thread):
    def __init__(self, cam_id, cam_cfg, zones, settings, reader, outbox, publisher, identity=None,
                 detector=None, engine=None):
        super().__init__(daemon=True, name=f"worker-{cam_id}")
        self.cam_id, self.cam_cfg, self.s = cam_id, cam_cfg, settings
        self.reader, self.outbox, self.publisher = reader, outbox, publisher
        # Only items the active model detects reliably are judged; other required items are skipped
        # (never 'missing') until a model that handles them is switched on (settings.yaml).
        self.trusted = set(settings["models"].get("trusted_items") or ["helmet", "vest", "gloves", "boots", "mask"])
        self.zonemap = ZoneMap(cam_cfg, zones, self.trusted)
        self.zones = zones
        self.fps_target = settings["processing"]["target_fps"]
        self.fire_every = settings["processing"]["fire_every_n"]
        self.engine = engine                              # shared, batched inference (edge/engine.py)
        self.detector = engine.det if engine is not None else (detector or Detector(settings))
        self.trusted &= self.detector.supported          # e.g. goggles only when the v3 model is loaded
        self.zonemap.trusted = self.trusted
        skipped = sorted({i for z in zones.values() for i in z["required_ppe"]} - self.trusted)
        if skipped:
            print(f"[{cam_id}] not judged yet with this model: {', '.join(skipped)}")
        self.tracker = PersonTracker(self.fps_target)
        self.track_conf = TrackConfidence(window=self.fps_target * 2)
        self.sm = PPEStateMachine(settings["state_machine"])
        self.fv = FireVerifier(settings["fire"])
        self.access = AccessGuard(zones, settings.get("access"))     # restricted zones (zones.yaml allowed_workers)
        self.gpu_rank = RANK.get(zones[cam_cfg["default_zone"]]["risk"], 1)   # high-risk zone -> GPU first
        self.running = True
        self.proc_times = deque(maxlen=30)
        self.infer_ms = deque(maxlen=30)
        self.frame_ms = deque(maxlen=200)      # frame captured -> fully processed (latency, sec. 7)
        self.wait_ms = deque(maxlen=200)       # waiting for the shared engine
        self.dropped_stale = 0
        self.max_age = float(settings["processing"].get("max_frame_age_s", 0.6))
        # entrance camera (cameras.yaml role: entrance) -> one APPROVED / DENIED / MANUAL decision per person
        self.role = cam_cfg.get("role", "monitor")
        self.entrance = None
        if self.role == "entrance":
            ez = cam_cfg["default_zone"]
            self.entrance = EntranceChecker(cam_id, ez, self.zonemap.required(ez), settings.get("entrance"))
            print(f"[{cam_id}] ENTRANCE check for zone {ez}: {', '.join(self.entrance.required)}")
        self.tile_badge = None
        self.decisions = {"compliant": 0, "missing": 0, "unverifiable": 0}
        self.identity = identity
        self.stable = {}              # worker/visitor id -> stable integer id for the state machine
        self.aliased = {}             # raw tracker id -> stable id already handed over
        self.snap_dir = path(settings["snapshots_dir"])
        os.makedirs(self.snap_dir, exist_ok=True)

    def set_target(self, fps):
        """Load manager (edge/load.py) changes the processing rate at run time."""
        self.fps_target = fps
        self.reader.target_fps = fps          # health thresholds follow the new target

    def processed_fps(self):
        if len(self.proc_times) < 2:
            return 0.0
        span = self.proc_times[-1] - self.proc_times[0]
        return (len(self.proc_times) - 1) / span if span > 0 else 0.0

    def run(self):
        last_no, n = -1, 0
        while self.running:
            t0 = time.time()
            frame, frame_ts, frame_no = self.reader.latest()
            if frame is None or frame_no == last_no:
                time.sleep(0.01)
                continue
            last_no = frame_no
            if time.time() - frame_ts > self.max_age:      # never judge a stale picture (camera lag)
                self.dropped_stale += 1
                continue
            n += 1
            try:
                self.process(frame, frame_ts, n)
            except Exception as e:           # a bad frame must never kill the camera
                print(f"[{self.cam_id}] processing error: {e!r}")
            self.proc_times.append(time.time())
            time.sleep(max(0.0, 1.0 / self.fps_target - (time.time() - t0)))

    def process(self, frame, frame_ts, n):
        h, w = frame.shape[:2]
        now = time.time()
        busy = bool(self.fv.regions.get(self.cam_id))       # something fire-like is being verified
        want_fire = busy or (self.fire_every > 0 and n % self.fire_every == 0)
        if self.engine is not None:
            t_req = time.time()
            r = self.engine.infer(self.cam_id, frame, "urgent" if busy else want_fire, self.gpu_rank)
            raw_persons, items, parts, fire_dets = r["persons"], r["items"], r["parts"], r["fire"]
            self.infer_ms.append(r["timings"]["total_ms"])
            self.wait_ms.append((time.time() - t_req) * 1000 - r["timings"]["total_ms"])
        else:
            with GATE.slot(0 if busy else self.gpu_rank):    # fire candidate anywhere = top priority
                t_inf = time.time()                          # model time only, not the wait for the GPU
                raw_persons, items = self.detector.ppe_detect(frame)
                parts = self.detector.last_parts
                fire_dets = self.detector.fire_detect(frame) if want_fire else None
            self.infer_ms.append((time.time() - t_inf) * 1000)

        tracks = self.tracker.update([p["bbox"] for p in raw_persons],
                                     [p["conf"] for p in raw_persons])
        persons = self.track_conf.update([{"tid": t, "bbox": b, "conf": c} for t, b, c in tracks])
        if raw_persons and raw_persons[0].get("kps") is not None:     # pose model: keep the body keypoints
            from .geometry import iou as _iou
            for p in persons:
                m = max(raw_persons, key=lambda r: _iou(r["bbox"], p["bbox"]))
                if _iou(m["bbox"], p["bbox"]) > 0.5:
                    p["kps"] = m["kps"]
        if self.identity is not None:
            persons = self.identity.update(self.cam_id, frame, persons, n)
            self._stable_ids(persons)
        zone_of = {p["tid"]: self.zonemap.zone_of(p["bbox"], w, h) for p in persons}
        required = {tid: self.zonemap.required(z) for tid, z in zone_of.items()}
        statuses, bad_items = evaluate(persons, items, required, w, h, self.s["association"],
                                       frame, self.s.get("validation", {}),
                                       parts=parts if self.detector.has_parts else None)
        for st in statuses.values():
            for s, _ in st.values():
                self.decisions[s] += 1

        take = getattr(self.publisher, "take_closed", None)
        closed = take(self.cam_id) if take else None
        if closed:                                   # supervisor pressed Resolve: look at that person again
            self.sm.operator_closed(closed, now)
            self.access.operator_closed(closed, now)
            print(f"[{self.cam_id}] resolved by supervisor: {', '.join(sorted(closed))} - checking again")
        if self.entrance is not None:
            # entrance: one gate decision per person instead of zone PPE alerts
            actions = self.entrance.update(persons, statuses, w, h, now)
            for a in actions:
                print(f"[{self.cam_id}] ENTRANCE {a['result'].upper()} missing={a['items_missing']} "
                      f"unclear={a['items_uncertain']} in {a['decision_ms']} ms")
        else:
            actions = self.sm.update(self.cam_id, persons, statuses, zone_of,
                                     self.zonemap.risk, w, h, now)
        fire_actions, self.tile_badge = self.fv.update(
            self.cam_id, frame, fire_dets, [p["bbox"] for p in persons], now)
        for a in fire_actions:
            a["zone_id"] = self.zonemap.zone_of(a["bbox"], w, h)
        actions += fire_actions
        if self.entrance is None:
            actions += self.access.update(self.cam_id, persons, zone_of, now)

        shown = self._smooth(statuses)                       # steady on-screen PPE list (no frame flicker)
        publish = self.publisher.due(self.cam_id)
        annotated = None
        if publish or actions:                               # draw only when someone will see it
            annotated = annotate.draw(frame, persons, shown, items, self.fv.boxes(self.cam_id),
                                      self.zonemap.polys,
                                      self.fv.rejected.get(self.cam_id, []) + bad_items,
                                      parts=parts)
            if self.entrance is not None:
                annotate.entrance_banner(annotated, self.entrance)
        if not self.tile_badge and self.entrance is not None and self.entrance.s and self.entrance.s.get("decided"):
            self.tile_badge = {"approved": "ACCESS APPROVED", "denied": "ACCESS DENIED",
                               "manual": "MANUAL CHECK REQUIRED"}[self.entrance.s["decided"]]
        if not self.tile_badge:
            intruders = sorted({v.get("name") or k[1] for k, v in self.access.visits.items()
                                if k[0] == self.cam_id and v["event_id"]})
            if intruders:
                self.tile_badge = "NOT AUTHORISED: " + ", ".join(intruders)
        if not self.tile_badge:
            open_ppe = [w_ for w_ in self.sm.workers.values() if w_.event_id]
            if open_ppe:
                items_ = sorted({i for w_ in open_ppe for i in w_.open_items})
                self.tile_badge = "NO " + " / ".join(i.upper() for i in items_)
        for a in actions:
            a["frame_w"], a["frame_h"] = w, h
            self.emit(a, annotated, frame_ts, frame, persons)
        if publish:
            self.publisher.frame(self.cam_id, annotated, frame_ts)
        self.publisher.raw(self.cam_id, frame)
        self.frame_ms.append((time.time() - frame_ts) * 1000)

    def _smooth(self, statuses, keep=8):
        """Display only: each worker's item state = majority of the last `keep` frames (~0.7 s), so the
        overlay does not flicker between OK / NO / ? on single frames. Alerts still come from the state
        machine (its own N-of-M + settle rules)."""
        from collections import Counter
        hist = self.__dict__.setdefault("_disp", {})
        out = {}
        for tid, st in statuses.items():
            h = hist.setdefault(tid, {})
            h.pop("_gone", None)
            out[tid] = {}
            for item, (state, conf) in st.items():
                q = h.setdefault(item, deque(maxlen=keep))
                q.append(state)
                top, n = Counter(q).most_common(1)[0]
                out[tid][item] = (top if n * 2 > len(q) else "unverifiable", conf)
        for tid in [t for t in hist if t not in statuses]:
            hist[tid].setdefault("_gone", 0)
            hist[tid]["_gone"] += 1
            if hist[tid]["_gone"] > 60:
                del hist[tid]
        return out

    def _stable_ids(self, persons):
        """One person = one id: a recognised person always uses the same state-machine id,
        whatever number the tracker gives them this time."""
        used = set()
        for p in persons:
            p["track_id"] = p["tid"]
            wid = p.get("worker_id")
            if not wid:
                continue
            sid = self.stable.setdefault(wid, 100000 + len(self.stable) + 1)
            if sid in used:                  # same id on two boxes this frame: keep the first
                continue
            used.add(sid)
            if self.aliased.get(p["tid"]) != sid:
                self.sm.rename(self.cam_id, p["tid"], sid)
                self.aliased[p["tid"]] = sid
            p["tid"] = sid

    def _privacy_boxes(self, frame, persons):
        """Faces to blur in the stored snapshot: detected faces (slightly enlarged), or - without
        the face model - the top of every person box."""
        if not self.s.get("privacy", {}).get("blur_faces_in_snapshots", True):
            return []
        eng = getattr(self.identity, "engine", None)
        if eng is not None:
            out = []
            for f in eng.faces(frame):
                x, y, fw, fh = f[:4]
                out.append((x - 0.25 * fw, y - 0.35 * fh, x + 1.25 * fw, y + 1.2 * fh))
            return out
        return [(p["bbox"][0], p["bbox"][1], p["bbox"][2],
                 p["bbox"][1] + 0.2 * (p["bbox"][3] - p["bbox"][1])) for p in persons]

    def emit(self, a, annotated, frame_ts, frame=None, persons=()):
        payload = dict(a)
        payload["schema_version"] = 1
        payload["timestamp"] = iso(a["at"])
        payload["started_at"] = iso(a["started_at"]) if a.get("started_at") else None
        payload["captured_at"] = iso(frame_ts)
        payload["onset_at"] = iso(a["onset_at"]) if a.get("onset_at") else None
        payload["camera_name"] = self.cam_cfg.get("name", self.cam_id)
        z = self.zones.get(payload.get("zone_id") or "", {})
        payload["zone_name"] = z.get("name", payload.get("zone_id"))
        payload["worker_id"] = a.get("worker_id")
        payload["worker_name"] = a.get("worker_name")
        payload.pop("at", None)
        if a["action"] in ("open", "update"):
            snap = annotate.highlight(annotated, a["bbox"])
            if frame is not None:
                snap = annotate.blur_regions(snap, self._privacy_boxes(frame, persons))
            jpg = annotate.to_jpeg(snap)
            if jpg:
                with open(os.path.join(self.snap_dir, f"{a['event_id']}.jpg"), "wb") as f:
                    f.write(jpg)
                payload["snapshot_b64"] = base64.b64encode(jpg).decode("ascii")
        self.outbox.put(payload)
        what = ",".join(a["items_missing"]) or a["type"]
        print(f"[{self.cam_id}] {a['action'].upper():7s} {a['severity'] or '':8s} "
              f"{what:20s} zone={payload['zone_id']} track={a['track_id']} "
              f"conf={a['confidence']} {a.get('reason') or ''}")
