"""Hardware integration: ESP32 safety station(s) -> backend (plan: hardware phase, 11 Oct 2026).

Pure logic, no web framework, so it can be unit-tested (tools/hw_selftest.py):
  Hub          latest telemetry per device, short history, sensor baselines, hazard evaluation, stale check
  FireFusion   camera fire/smoke event -> confirmed / pending / hardware_clear / hardware_offline
  attendance   RFID smart attendance: first scan ENTERED, next EXITED (state from the database, not the ESP32)
  commands     what the station's LEDs / buzzer / relay should show (backend view; the ESP32 keeps its own
               local fire alarm even if Wi-Fi is down)

Rules (from the team's ESP32 guides):
  * Fire / smoke / heat are SENSOR-LED. A hardware hazard alarms on its own, even with no camera and no RFID.
  * A camera fire/smoke detection waits `verify_window_s` for the station of that zone:
      hardware hazard seen  -> CONFIRMED (Critical, siren)
      hardware stays green  -> HARDWARE_CLEAR (logged, no alarm: e.g. a phone playing a fire video)
      station offline/stale -> HARDWARE_OFFLINE (camera-only alarm, labelled - never silently ignored)
      zone has no station   -> NO_STATION (camera-only alarm, as before)
  * Missing / stale readings are UNKNOWN / FAULT, never "safe".
  * Raw MQ / FSR values are ADC counts, not ppm. Thresholds are prototype values (config/hardware.yaml).
"""
import json
import os
import re
import time
from collections import deque

import yaml

from . import db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULTS = {
    "stale_after_s": 6,
    "history_s": 600,
    "fusion": {"verify_window_s": 8, "lookback_s": 10},
    "thresholds": {"temp_critical_c": 50, "temp_rise_c": 6, "mq2_abs": 1800, "mq2_rise": 400,
                   "mq135_rise": 450, "pm25_warn": 35, "pm25_critical": 150, "fsr_contact": 700,
                   "mq_warmup_s": 60, "flame_min_hits": 2, "clear_after_s": 10, "baseline_alpha": 0.02},
    "attendance": {"cooldown_s": 3, "sheets_url": "", "announce": True},
    "hardware_ppe": {"alerts": False, "persist_s": 5},
}


def load_config(path=None):
    path = path or os.path.join(ROOT, "config", "hardware.yaml")
    cfg = json.loads(json.dumps(DEFAULTS))
    # hardware.local.yaml (next to it, NOT in git) holds private values such as attendance.sheets_url
    for p in (path, os.path.splitext(path)[0] + ".local.yaml"):
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    cfg.setdefault("devices", {})
    return cfg


# ------------------------------------------------------------------ database
def init_db():
    db.x("""CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, uid TEXT, worker_id TEXT, name TEXT,
        zone_id TEXT, event TEXT, status TEXT, device_id TEXT, message TEXT)""")
    db.x("CREATE INDEX IF NOT EXISTS ix_att_uid ON attendance(uid, zone_id, at)")
    db.x("""CREATE TABLE IF NOT EXISTS hw_readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, device_id TEXT, level TEXT, data TEXT)""")
    have = {r["name"] for r in db.q("PRAGMA table_info(workers)")}
    for col, typ in (("badge_uid", "TEXT"), ("active", "INTEGER DEFAULT 1")):
        if col not in have:
            db.x(f"ALTER TABLE workers ADD COLUMN {col} {typ}")
    have = {r["name"] for r in db.q("PRAGMA table_info(events)")}
    for col in ("sources", "verification", "device_id", "sensors"):      # alert contract: add fields only
        if col not in have:
            db.x(f"ALTER TABLE events ADD COLUMN {col} TEXT")


# ------------------------------------------------------------------ telemetry
FIELDS = ["temperature_c", "humidity_pct", "thermistor_adc", "mq2_adc", "mq135_adc", "flame_detected",
          "pm1_0_ugm3", "pm2_5_ugm3", "pm10_ugm3", "helmet_fsr_adc", "shoe_fsr_adc", "touch_kit",
          "touch_helmet", "touch_shoe", "mpu6050_available", "vibration_metric", "alarm_level",
          "green_led", "orange_led", "red_led", "buzzer_on", "relay_on", "rssi", "uptime_s", "fw"]


def flatten(p):
    """Accept the flat firmware payload or the nested schema from the guide; unknown keys ignored."""
    flat = {}
    for k, v in p.items():
        if isinstance(v, dict) and k not in ("worker",):
            for k2, v2 in v.items():
                flat[k2] = v2
        else:
            flat[k] = v
    if "online" in flat:
        flat.pop("online")
    out = {k: flat.get(k) for k in FIELDS}
    for k in ("flame_detected", "touch_kit", "touch_helmet", "touch_shoe", "mpu6050_available",
              "green_led", "orange_led", "red_led", "buzzer_on", "relay_on"):
        if out[k] is not None:
            out[k] = bool(out[k])
    for k in ("temperature_c", "humidity_pct", "vibration_metric"):
        if out[k] is not None:
            try:
                out[k] = float(out[k])
                if out[k] != out[k]:          # NaN from a failed DHT read
                    out[k] = None
            except (TypeError, ValueError):
                out[k] = None
    return out


class Hub:
    def __init__(self, cfg):
        self.cfg = cfg
        self.t = cfg["thresholds"]
        self.latest = {}                     # device -> {"at", "data", "eval"}
        self.hist = {}                       # device -> deque[(at, data)]
        self.base = {}                       # device -> {"mq2", "mq135", "temp"}
        self.flame_hits = {}                 # device -> deque[bool]
        self.hazard_at = {}                  # (device, kind) -> last time the hazard was seen
        self.last_store = {}

    def device_zone(self, device_id):
        d = self.cfg["devices"].get(device_id) or {}
        return d.get("zone_id")

    def devices_for_zone(self, zone_id):
        return [d for d, c in self.cfg["devices"].items() if (c or {}).get("zone_id") == zone_id]

    # ---- ingest
    def ingest(self, device_id, payload, now=None):
        now = time.time() if now is None else now
        data = flatten(payload)
        hist = self.hist.setdefault(device_id, deque())
        hist.append((now, data))
        while hist and now - hist[0][0] > self.cfg["history_s"]:
            hist.popleft()
        ev = self.evaluate(device_id, data, now)
        self.latest[device_id] = {"at": now, "data": data, "eval": ev}
        if now - self.last_store.get(device_id, 0) >= 10:                   # downsampled history in the DB
            self.last_store[device_id] = now
            db.x("INSERT INTO hw_readings (at, device_id, level, data) VALUES (?,?,?,?)",
                 (now, device_id, ev["level"], json.dumps(data)))
        return ev

    def _baseline(self, device_id, data, hazard):
        b = self.base.setdefault(device_id, {})
        a = self.t["baseline_alpha"]
        for key, field in (("mq2", "mq2_adc"), ("mq135", "mq135_adc"), ("temp", "temperature_c")):
            v = data.get(field)
            if v is None:
                continue
            if key not in b:
                b[key] = float(v)
            elif not hazard:                                                  # freeze baseline during a hazard
                b[key] = (1 - a) * b[key] + a * float(v)
        return b

    def evaluate(self, device_id, d, now):
        t = self.t
        reasons, hz = [], {"fire": False, "smoke": False, "heat": False}
        fh = self.flame_hits.setdefault(device_id, deque(maxlen=3))
        prev = self.latest.get(device_id)
        if prev and now - prev["at"] > self.cfg["stale_after_s"]:
            fh.clear()                                    # a gap in telemetry: old flame hits do not count
        fh.append(bool(d.get("flame_detected")))
        if sum(fh) >= t["flame_min_hits"]:
            hz["fire"] = True
            reasons.append("flame sensor active")
        b = self.base.get(device_id, {})
        warm = (d.get("uptime_s") is None) or (d["uptime_s"] >= t["mq_warmup_s"])
        mq2 = d.get("mq2_adc")
        if mq2 is not None:
            if mq2 >= t["mq2_abs"]:
                hz["smoke"] = True
                reasons.append(f"MQ-2 {mq2} >= {t['mq2_abs']}")
            elif warm and "mq2" in b and mq2 - b["mq2"] >= t["mq2_rise"]:
                hz["smoke"] = True
                reasons.append(f"MQ-2 rose {mq2 - b['mq2']:.0f} above baseline {b['mq2']:.0f}")
        mq135 = d.get("mq135_adc")
        if mq135 is not None and warm and "mq135" in b and mq135 - b["mq135"] >= t["mq135_rise"]:
            hz["smoke"] = True
            reasons.append(f"MQ-135 rose {mq135 - b['mq135']:.0f} above baseline")
        pm = d.get("pm2_5_ugm3")
        if pm is not None and pm >= t["pm25_critical"]:
            hz["smoke"] = True
            reasons.append(f"PM2.5 {pm} >= {t['pm25_critical']} ug/m3")
        temp = d.get("temperature_c")
        if temp is not None:
            if temp >= t["temp_critical_c"]:
                hz["heat"] = True
                reasons.append(f"temperature {temp:.1f} C >= {t['temp_critical_c']}")
            elif "temp" in b and temp - b["temp"] >= t["temp_rise_c"]:
                hz["heat"] = True
                reasons.append(f"temperature rose {temp - b['temp']:.1f} C above baseline")
        self._baseline(device_id, d, any(hz.values()))
        for k, v in hz.items():
            if v:
                self.hazard_at[(device_id, k)] = now
        air = "ok"
        if pm is not None and pm >= t["pm25_warn"]:
            air = "warning"
            if not hz["smoke"]:
                reasons.append(f"PM2.5 {pm} >= {t['pm25_warn']} ug/m3")
        missing = [k for k in ("temperature_c", "mq2_adc", "flame_detected") if d.get(k) is None]
        if any(hz.values()):
            level = "CRITICAL"
        elif air == "warning":
            level = "WARNING"
        elif missing:
            level = "FAULT"
            reasons.append("no reading: " + ", ".join(missing))
        else:
            level = "NORMAL"
        return {"level": level, "hazards": hz, "air": air, "reasons": reasons,
                "ppe": self.ppe_contact(d), "baseline": {k: round(v, 1) for k, v in b.items()}}

    def ppe_contact(self, d):
        thr = self.t["fsr_contact"]
        out = {}
        for item, fsr, touch in (("helmet", "helmet_fsr_adc", "touch_helmet"), ("boots", "shoe_fsr_adc", "touch_shoe")):
            a, tv = d.get(fsr), d.get(touch)
            if a is None and tv is None:
                out[item] = "UNKNOWN"
            else:
                out[item] = "PRESENT" if ((a is not None and a >= thr) or tv) else "NOT_DETECTED"
        out["kit"] = "UNKNOWN" if d.get("touch_kit") is None else ("PRESENT" if d["touch_kit"] else "NOT_DETECTED")
        return out

    # ---- state
    def online(self, device_id, now=None):
        now = time.time() if now is None else now
        x = self.latest.get(device_id)
        return bool(x) and now - x["at"] <= self.cfg["stale_after_s"]

    def zone_station(self, zone_id, now=None):
        """None = no station configured for this zone; else {"online", "device_id", "eval"}."""
        devs = self.devices_for_zone(zone_id)
        if not devs:
            return None
        now = time.time() if now is None else now
        on = [d for d in devs if self.online(d, now)]
        d = on[0] if on else devs[0]
        x = self.latest.get(d)
        return {"device_id": d, "online": bool(on), "eval": x["eval"] if x else None,
                "data": x["data"] if x else None, "age_s": round(now - x["at"], 1) if x else None}

    def hazard_since(self, zone_id, since):
        """Most recent combustion hazard (fire / smoke / heat) in that zone at or after `since`."""
        best = None
        for d in self.devices_for_zone(zone_id):
            for k in ("fire", "smoke", "heat"):
                at = self.hazard_at.get((d, k))
                if at is not None and at >= since and (best is None or at > best[1]):
                    best = (k, at, d)
        return best

    def snapshot(self, device_id, now=None):
        now = time.time() if now is None else now
        x = self.latest.get(device_id)
        c = self.cfg["devices"].get(device_id) or {}
        return {"device_id": device_id, "name": c.get("name", device_id), "zone_id": c.get("zone_id"),
                "online": self.online(device_id, now), "age_s": round(now - x["at"], 1) if x else None,
                "data": x["data"] if x else None, "eval": x["eval"] if x else None}

    def history(self, device_id, seconds=300):
        h = self.hist.get(device_id) or ()
        if not h:
            return []
        end = h[-1][0]
        return [dict(d, at=round(t, 1)) for t, d in h if end - t <= seconds]


# ------------------------------------------------------------------ fire / smoke cross-verification
class FireFusion:
    CONFIRMED, PENDING, CLEAR, OFFLINE, NO_STATION = "confirmed", "pending", "hardware_clear", "hardware_offline", "no_station"

    def __init__(self, hub, cfg):
        self.hub = hub
        self.window = cfg["fusion"]["verify_window_s"]
        self.lookback = cfg["fusion"]["lookback_s"]

    def decide(self, zone_id, t0, now=None):
        """t0 = when the camera event reached the backend. Returns (verdict, detail)."""
        now = time.time() if now is None else now
        st = self.hub.zone_station(zone_id, now)
        if st is None:
            return self.NO_STATION, "no safety station in this zone"
        hz = self.hub.hazard_since(zone_id, t0 - self.lookback)
        if hz:
            k, _, dev = hz
            ev = (self.hub.latest.get(dev) or {}).get("eval") or {}
            return self.CONFIRMED, "; ".join(ev.get("reasons") or [f"{k} sensor"])
        if not st["online"]:
            return self.OFFLINE, f"station {st['device_id']} offline / stale"
        if now - t0 < self.window:
            return self.PENDING, f"waiting for {st['device_id']} ({self.window - (now - t0):.0f} s left)"
        return self.CLEAR, f"{st['device_id']}: flame, smoke and temperature normal"


# ------------------------------------------------------------------ RFID attendance
def norm_uid(uid):
    return re.sub(r"[^A-Fa-f0-9]", "", str(uid or "")).upper()


def scan(uid, device_id, zone_id, now=None, cooldown_s=3, event=None):
    """One RFID scan -> result dict. State (ENTERED/EXITED) comes from the database, unless the reader already
    decided it (event='ENTERED'/'EXITED': the team's Google-Sheets sketch asks the sheet first) - then the
    reader's answer is used so the sheet and the dashboards never disagree."""
    now = time.time() if now is None else now
    uid = norm_uid(uid)
    if not uid:
        return {"ok": False, "status": "BAD_REQUEST", "message": "missing UID"}
    last = db.q("SELECT at FROM attendance WHERE uid=? AND device_id=? ORDER BY at DESC LIMIT 1", (uid, device_id))
    if last and now - last[0]["at"] < cooldown_s:
        return {"ok": False, "status": "DUPLICATE", "uid": uid, "message": "card held at the reader - ignored"}
    w = db.q("SELECT worker_id, name, COALESCE(active, 1) AS active FROM workers WHERE badge_uid=?", (uid,))

    def log(event, status, worker_id, name, msg):
        db.x("INSERT INTO attendance (at, uid, worker_id, name, zone_id, event, status, device_id, message) "
             "VALUES (?,?,?,?,?,?,?,?,?)", (now, uid, worker_id, name, zone_id, event, status, device_id, msg))

    if not w:
        msg = f"Unregistered card scanned: {uid}"
        log("UNKNOWN_UID", "UNKNOWN_UID", None, None, msg)
        return {"ok": False, "status": "UNKNOWN_UID", "uid": uid, "zone_id": zone_id, "event": "UNKNOWN_UID",
                "message": msg, "at": now, "device_id": device_id}
    w = w[0]
    if not w["active"]:
        msg = f"{w['name']} is inactive"
        log("REJECTED", "INACTIVE", w["worker_id"], w["name"], msg)
        return {"ok": False, "status": "INACTIVE", "uid": uid, "worker_id": w["worker_id"], "name": w["name"],
                "zone_id": zone_id, "message": msg, "at": now, "device_id": device_id}
    prev = db.q("SELECT event FROM attendance WHERE uid=? AND zone_id=? AND status='OK' "
                "AND event IN ('ENTERED','EXITED') ORDER BY at DESC, id DESC LIMIT 1", (uid, zone_id))
    if event not in ("ENTERED", "EXITED"):
        event = "EXITED" if prev and prev[0]["event"] == "ENTERED" else "ENTERED"
    msg = f"{w['name']} {'entered' if event == 'ENTERED' else 'exited'} {zone_id}"
    log(event, "OK", w["worker_id"], w["name"], msg)
    return {"ok": True, "status": "OK", "uid": uid, "worker_id": w["worker_id"], "name": w["name"],
            "zone_id": zone_id, "event": event, "at": now, "device_id": device_id, "message": msg}


def present(zone_id=None):
    """Workers whose latest OK event in a zone is ENTERED."""
    rows = db.q("SELECT a.* FROM attendance a JOIN (SELECT uid, zone_id, MAX(id) AS mid FROM attendance "
                "WHERE status='OK' AND event IN ('ENTERED','EXITED') GROUP BY uid, zone_id) l ON a.id = l.mid "
                "WHERE a.event='ENTERED'" + (" AND a.zone_id=?" if zone_id else "") + " ORDER BY a.at DESC",
                (zone_id,) if zone_id else ())
    return [{"uid": r["uid"], "worker_id": r["worker_id"], "name": r["name"], "zone_id": r["zone_id"],
             "since": r["at"], "device_id": r["device_id"]} for r in rows]


def session_worker(device_id, zone_id):
    """The worker who last checked IN at this station and has not checked out (owner of the hardware PPE kit)."""
    p = [x for x in present(zone_id) if x["device_id"] == device_id]
    return p[0] if p else None


def recent_scans(limit=50, zone_id=None):
    return db.q("SELECT * FROM attendance" + (" WHERE zone_id=?" if zone_id else "") +
                " ORDER BY at DESC, id DESC LIMIT ?", ((zone_id, limit) if zone_id else (limit,)))


# ------------------------------------------------------------------ station outputs (what the LEDs should show)
def command(level_local, alarm_zone, ppe_warning, air_warning):
    """Backend's view for one station. The firmware uses the stronger of this and its own local rule."""
    if alarm_zone:
        return {"led": "red", "buzzer": "alarm", "relay": True}
    if ppe_warning or air_warning or level_local == "WARNING":
        return {"led": "orange", "buzzer": "warn", "relay": False}
    if level_local == "FAULT":
        return {"led": "orange", "buzzer": "off", "relay": False}
    return {"led": "green", "buzzer": "off", "relay": False}


SIMPLE_MAP = {                     # team's simple sketch field -> our telemetry field
    "humidity_percent": "humidity_pct",
    "helmet_fsr_raw": "helmet_fsr_adc",
    "dust_estimate_ug_m3": "pm2_5_ugm3",   # Sharp GP2Y estimate, UNCALIBRATED (not real PM2.5)
    "overall_status": "alarm_level",
    "buzzer": "buzzer_on",
    "relay": "relay_on",
}


def from_simple_station(p, default_device="esp32-ppe-01"):
    """The team's stand-alone ESP32 sketch posts {mq2_adc, mq135_adc, flame_detected, temperature_c,
    humidity_percent, dust_estimate_ug_m3, ...} to /api/sensors with no device_id. Translate it to the
    /api/hw/telemetry format so the same station logic, fusion and dashboard apply."""
    out = {SIMPLE_MAP.get(k, k): v for k, v in (p or {}).items()}
    out.setdefault("device_id", default_device)
    if "dust_estimate_ug_m3" in (p or {}):
        out["pm_uncalibrated"] = True
    return out
