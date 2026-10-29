"""Location for incidents (final-round section 6, software side).

Every incident gets a location record saying WHERE the number came from and WHEN:
  {"valid": bool, "lat", "lon", "accuracy_m", "source": hardware|device|configured, "source_label",
   "at", "age_s", "stale": bool, "zone_label", "camera_label", "map_url", "note"}
No valid source -> valid False and the text "Location unavailable" (zone label still shown)."""
import os
import time

import yaml

from . import db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LABEL = {"hardware": "hardware GPS", "device": "device GPS / network location", "configured": "configured site position"}


def cfg():
    p = os.path.join(ROOT, "config", "site.yaml")
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _valid(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0)


def record(source, lat, lon, accuracy_m=None, at=None, device_id=None):
    """Store a reading (hardware unit or dashboard device). Returns (ok, message)."""
    if source not in ("hardware", "device"):
        return False, "source must be hardware or device"
    if not _valid(lat, lon):
        return False, "invalid coordinates"
    now = time.time()
    at = float(at) if at else now
    if at > now + 60:
        return False, "timestamp is in the future"
    db.x("INSERT INTO locations (source, device_id, lat, lon, accuracy_m, at, received_at) VALUES (?,?,?,?,?,?,?)",
         (source, device_id, float(lat), float(lon), accuracy_m, at, now))
    return True, "stored"


def latest(source):
    r = db.q("SELECT * FROM locations WHERE source=? ORDER BY at DESC LIMIT 1", (source,))
    return r[0] if r else None


def current(zone_id=None, camera_id=None, now=None):
    now = now or time.time()
    c = cfg()
    ages = c.get("max_age_s") or {}
    zl = ((c.get("zones") or {}).get(zone_id) or {}).get("label")
    cl = ((c.get("cameras") or {}).get(camera_id) or {}).get("label")
    out = {"valid": False, "zone_label": zl, "camera_label": cl, "site": (c.get("site") or {}).get("name"),
           "note": "Location unavailable"}
    stale_seen = []
    for src in ("hardware", "device"):
        r = latest(src)
        if not r:
            continue
        age = now - r["at"]
        if age > float(ages.get(src, 900)):
            stale_seen.append(f"{LABEL[src]} is stale ({int(age // 60)} min old)")
            continue
        out.update(valid=True, lat=r["lat"], lon=r["lon"], accuracy_m=r["accuracy_m"], source=src,
                   source_label=LABEL[src], at=r["at"], age_s=round(age), stale=False)
        break
    if not out["valid"]:
        s = c.get("site") or {}
        if _valid(s.get("lat"), s.get("lon")):
            out.update(valid=True, lat=float(s["lat"]), lon=float(s["lon"]), accuracy_m=None, source="configured",
                       source_label=LABEL["configured"], at=None, age_s=None, stale=False)
    if out["valid"]:
        out["map_url"] = f"https://maps.google.com/?q={out['lat']:.6f},{out['lon']:.6f}"
        out["note"] = (f"{LABEL[out['source']]}; site/zone position - not the exact spot of the hazard"
                       if out["source"] == "configured" else f"{LABEL[out['source']]}, accuracy "
                       f"{'%.0f m' % out['accuracy_m'] if out.get('accuracy_m') else 'unknown'}")
    if stale_seen:
        out["warnings"] = stale_seen
    return out


def status(now=None):
    now = now or time.time()
    c = cfg()
    ages = c.get("max_age_s") or {}
    srcs = {}
    for src in ("hardware", "device"):
        r = latest(src)
        if r:
            age = now - r["at"]
            srcs[src] = {"lat": r["lat"], "lon": r["lon"], "accuracy_m": r["accuracy_m"], "at": r["at"],
                         "age_s": round(age), "stale": age > float(ages.get(src, 900))}
        else:
            srcs[src] = None
    s = c.get("site") or {}
    srcs["configured"] = {"lat": s.get("lat"), "lon": s.get("lon"), "set_by": s.get("set_by")} \
        if _valid(s.get("lat"), s.get("lon")) else None
    return {"site": s.get("name"), "address": s.get("address"), "sources": srcs, "current": current(now=now)}
