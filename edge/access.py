"""Restricted zones (zones.yaml -> allowed_workers): only the listed registered workers may be in the zone.

Camera side. A person whose face ID says registered worker W-00x, standing in a restricted zone W-00x is NOT
allowed in, gets one 'unauthorized_entry' alert per visit:
  * confirmed after `confirm_s` of being seen there (face ID itself already needs 2 agreeing frames),
  * resolved when they have not been seen in that zone for `leave_s` (walked out),
  * a new visit after leaving = a new alert.
Unregistered people (visitor IDs V-00x) are NOT judged here - they keep the normal PPE handling.
The RFID side (card tapped at the restricted zone's reader) is in backend/hw_api.py.
"""
import uuid


class AccessGuard:
    def __init__(self, zones, cfg=None):
        c = cfg or {}
        self.allowed = {z: set(v.get("allowed_workers") or []) for z, v in zones.items()
                        if v.get("allowed_workers") is not None}
        self.zones = zones
        self.confirm_s = c.get("confirm_s", 1.0)
        self.leave_s = c.get("leave_s", 5.0)
        self.visits = {}           # (cam, worker_id, zone) -> {"first", "last", "event_id", "bbox", "tid", "name"}

    def restricted(self, zone_id):
        return zone_id in self.allowed

    def update(self, cam, persons, zone_of, now):
        """persons: [{tid, bbox, conf, worker_id, worker_name, identity}]; returns state-machine-like actions."""
        actions = []
        for p in persons:
            wid, zone = p.get("worker_id"), zone_of.get(p["tid"])
            if p.get("identity") != "worker" or not wid or zone not in self.allowed or wid in self.allowed[zone]:
                continue
            v = self.visits.setdefault((cam, wid, zone), {"first": now, "event_id": None})
            v.update(last=now, bbox=p["bbox"], tid=p["tid"], name=p.get("worker_name"), conf=p.get("conf", 0.0))
            if v["event_id"] is None and now - v["first"] >= self.confirm_s:
                v["event_id"] = "evt_" + uuid.uuid4().hex[:10]
                actions.append(self._action("open", cam, wid, zone, v, now))
        for key, v in list(self.visits.items()):
            if key[0] != cam or now - v["last"] < self.leave_s:
                continue
            if v["event_id"]:
                actions.append(self._action("resolve", cam, key[1], key[2], v, now, reason="left the zone"))
            del self.visits[key]
        return actions

    def _action(self, action, cam, wid, zone, v, now, reason=None):
        allowed = sorted(self.allowed.get(zone, []))
        return {"action": action, "event_id": v["event_id"], "type": "unauthorized_entry",
                "items_missing": [], "severity": "High", "camera_id": cam, "zone_id": zone,
                "track_id": v["tid"], "worker_id": wid, "worker_name": v.get("name"),
                "confidence": round(float(v.get("conf") or 0.0), 3), "bbox": [round(float(x), 1) for x in v["bbox"]],
                "at": now, "started_at": v["first"], "onset_at": v["first"], "reason": reason,
                "allowed_workers": allowed, "source": "camera"}
