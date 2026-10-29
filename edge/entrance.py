"""Automatic PPE verification at the entrance (final-round section 4).

A camera with `role: entrance` in cameras.yaml checks the person in front of it against the required PPE
of its zone (zones.yaml) and gives ONE decision per person:
  APPROVED  every required item seen worn in most frames of the look window
  DENIED    a required item clearly missing (its body part seen bare) in most frames -> names the item(s)
  MANUAL    'Verification failed - manual check required': view unclear / occluded / too far / too many
            people / items that cannot be judged within max_look_s. Never an automatic approval.
A single frame is never proof: the decision needs >= min_frames frames AND >= min_look_s of looking.
A DENIED or MANUAL person who fixes their PPE is checked again after recheck_s (new decision).
The gate is never opened by software alone: 'gate' is 'closed' unless APPROVED, and without an access
controller connected the decision is advisory ('no controller connected')."""
import uuid

from .geometry import iou


class EntranceChecker:
    def __init__(self, cam_id, zone_id, required, cfg=None):
        c = cfg or {}
        self.cam, self.zone, self.required = cam_id, zone_id, list(required)
        self.min_frames = int(c.get("min_frames", 6))
        self.min_look_s = float(c.get("min_look_s", 1.0))
        self.max_look_s = float(c.get("max_look_s", 4.0))
        self.share = float(c.get("share", 0.6))         # majority needed per item
        self.min_votes = int(c.get("min_votes", 3))
        self.min_height_frac = float(c.get("min_height_frac", 0.35))   # person must fill >= 35 % of the height
        self.leave_s = float(c.get("leave_s", 2.0))
        self.recheck_s = float(c.get("recheck_s", 3.0))
        self.controller = bool(c.get("controller_connected", False))
        self.s = None                                    # current session
        self.last_result = None

    def _subject(self, persons, h):
        big = [p for p in persons if (p["bbox"][3] - p["bbox"][1]) >= self.min_height_frac * h]
        if not big:
            return None, len(persons) > 0
        big.sort(key=lambda p: -(p["bbox"][3] - p["bbox"][1]) * (p["bbox"][2] - p["bbox"][0]))
        crowd = len(big) > 1 and iou(big[0]["bbox"], big[1]["bbox"]) > 0.15     # two people overlapping
        return big[0], crowd

    def update(self, persons, statuses, w, h, now):
        """persons: tracked persons (with tid, bbox, conf, worker_id ...); statuses: association output
        {tid: {item: (status, conf)}} computed with this entrance's required items. Returns actions."""
        subj, crowd_or_far = self._subject(persons, h)
        out = []
        s = self.s
        if subj is None:
            if s and now - s["last_seen"] >= self.leave_s:
                self.s = None                             # person walked through / away: next person
            elif s and not s.get("decided") and crowd_or_far and now - s["start"] >= self.max_look_s:
                out.append(self._decide(now, forced="person too far from the camera - step closer"))
            return out
        if s is None or (s["tid"] != subj["tid"] and now - s["last_seen"] > 0.5):
            s = self.s = {"tid": subj["tid"], "start": now, "last_seen": now, "frames": 0, "votes": {},
                          "decided": None, "decided_at": None, "bbox": subj["bbox"], "conf": [], "crowd": 0,
                          "worker_id": None, "worker_name": None, "id": "ent_" + uuid.uuid4().hex[:10]}
        s["last_seen"], s["bbox"] = now, subj["bbox"]
        s["frames"] += 1
        s["conf"].append(subj.get("conf", 0.0))
        if crowd_or_far:
            s["crowd"] += 1
        if subj.get("worker_id"):
            s["worker_id"], s["worker_name"] = subj["worker_id"], subj.get("worker_name")
        st = statuses.get(subj["tid"], {})
        for item in self.required:
            status = (st.get(item) or ("unverifiable", 0.0))[0]
            v = s["votes"].setdefault(item, {"compliant": 0, "missing": 0, "unverifiable": 0})
            v[status] += 1
        if s["decided"]:
            # re-check a denied / manual person who keeps standing there (they may have put the item on)
            if s["decided"] != "approved" and now - s["decided_at"] >= self.recheck_s:
                s.update(start=now, frames=0, votes={}, decided=None, conf=[], crowd=0,
                         id="ent_" + uuid.uuid4().hex[:10])
            return out
        looked = now - s["start"]
        if s["frames"] >= self.min_frames and looked >= self.min_look_s:
            res = self._verdicts()
            if res["result"] in ("approved", "denied") or looked >= self.max_look_s:
                out.append(self._decide(now, res))
        return out

    def _verdicts(self):
        s = self.s
        ok, missing, unclear, evidence = [], [], [], {}
        for item in self.required:
            v = s["votes"].get(item, {"compliant": 0, "missing": 0, "unverifiable": 0})
            n = sum(v.values()) or 1
            evidence[item] = dict(v, frames=n)
            if v["compliant"] >= self.min_votes and v["compliant"] / n >= self.share:
                ok.append(item)
            elif v["missing"] >= self.min_votes and v["missing"] / n >= self.share:
                missing.append(item)
            else:
                unclear.append(item)
        crowded = s["crowd"] > s["frames"] / 2
        if missing:
            result = "denied"
        elif not unclear and not crowded:
            result = "approved"
        else:
            result = "manual"
        return {"result": result, "ok": ok, "missing": missing, "unclear": unclear, "evidence": evidence,
                "crowded": crowded}

    def _decide(self, now, res=None, forced=None):
        s = self.s
        if res is None:
            res = {"result": "manual", "ok": [], "missing": [], "unclear": list(self.required), "evidence": {},
                   "crowded": False}
        reason = forced
        if res["result"] == "manual" and not reason:
            reason = ("two or more people in front of the camera - one at a time" if res["crowded"] else
                      "could not see clearly: " + ", ".join(res["unclear"]))
        s["decided"], s["decided_at"] = res["result"], now
        self.last_result = res["result"]
        conf = sum(s["conf"]) / len(s["conf"]) if s["conf"] else 0.0
        gate = "open_requested" if (res["result"] == "approved" and self.controller) else "closed"
        return {"action": "open", "event_id": s["id"], "type": "entrance_check", "result": res["result"],
                "items_missing": res["missing"], "items_ok": res["ok"], "items_uncertain": res["unclear"],
                "required": list(self.required), "evidence": res["evidence"], "reason": reason,
                "severity": {"approved": "Low", "denied": "Medium", "manual": "Medium"}[res["result"]],
                "camera_id": self.cam, "zone_id": self.zone, "track_id": s["tid"], "worker_id": s["worker_id"],
                "worker_name": s["worker_name"], "confidence": round(conf, 3),
                "bbox": [round(float(x), 1) for x in s["bbox"]], "at": now, "started_at": s["start"],
                "onset_at": s["start"], "decision_ms": round((now - s["start"]) * 1000), "frames": s["frames"],
                "gate": gate, "controller_connected": self.controller}
