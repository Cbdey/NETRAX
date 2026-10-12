"""Zone lookup (F06): which zone is a worker standing in?"""
from .geometry import point_in_polygon


class ZoneMap:
    def __init__(self, cam_cfg, zones, trusted=None):
        self.default = cam_cfg["default_zone"]
        self.trusted = set(trusted) if trusted else None   # items the active model can judge
        self.polys = [(z["zone"], z["polygon"]) for z in (cam_cfg.get("zones") or [])]
        self.zones = zones

    def zone_of(self, bbox, frame_w, frame_h):
        fx = ((bbox[0] + bbox[2]) / 2.0) / frame_w   # feet point, normalised
        fy = bbox[3] / frame_h
        for zone_id, poly in self.polys:
            if point_in_polygon(fx, fy, poly):
                return zone_id
        return self.default

    def required(self, zone_id):
        """Required items that are actually judged. Items the active model cannot judge reliably
        (not in settings models.trusted_items) are skipped - never reported as 'missing'."""
        req = list(self.zones[zone_id]["required_ppe"])
        return [i for i in req if self.trusted is None or i in self.trusted]

    def risk(self, zone_id):
        return self.zones[zone_id]["risk"]
