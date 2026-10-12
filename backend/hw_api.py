"""Hardware endpoints + background loop (ESP32 safety station <-> NETRAX backend).

ESP32  -> POST /api/hw/telemetry   every 1 s   reply: LED / buzzer / relay command from the backend's view
ESP32  -> POST /api/hw/rfid        on a scan   reply: ENTERED / EXITED / UNKNOWN_UID + name
ESP32  -> POST /api/rfid           team's Google-Sheets sketch {name, uid, event, timestamp} (same handling)
Dashboard <- GET /api/hw/state, /api/hw/history/<device>, /api/attendance..., WebSocket kinds
           'hardware' (1 s), 'attendance' (each scan), 'event' (alerts, as before)

Fire / smoke cross-verification lives in backend/hardware.py (FireFusion); this file applies the verdicts:
camera event 'verifying' -> confirmed (siren) / suppressed (hardware clear, no alarm) / camera-only (station offline).
"""
import asyncio
import csv
import io
import json
import threading
import time
import uuid
from datetime import datetime

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

from . import db
from . import hardware as hw

router = APIRouter()
hw.init_db()
CFG = hw.load_config()
HUB = hw.Hub(CFG)
FUSION = hw.FireFusion(HUB, CFG)
CTX = {}            # filled by setup(): broadcast, respond, notify_async, announce, ALARM, ZONES
HW_OPEN = {}        # device -> {"event_id": str or None, "last": time}   hardware-only hazard alarms
PPE_OPEN = {}       # (device, item) -> {"since": t, "event_id": str or None}


def setup(**kw):
    CTX.update(kw)


def zone_name(z):
    return ((CTX.get("ZONES") or {}).get(z) or {}).get("name", z or "unknown area")


def _station_json(zone_id, now):
    st = HUB.zone_station(zone_id, now)
    if not st or not st.get("data"):
        return None
    return json.dumps({"device_id": st["device_id"], "online": st["online"], "age_s": st["age_s"],
                       "level": (st["eval"] or {}).get("level"), "reasons": (st["eval"] or {}).get("reasons"),
                       "data": st["data"]})


# ------------------------------------------------------------------ camera fire / smoke
def _apply(eid, verdict, why, now):
    """Write a FireFusion verdict into the event row."""
    zone = db.get_event(eid)["zone_id"]
    sensors = _station_json(zone, now)
    if verdict == FUSION.CONFIRMED:
        db.x("UPDATE events SET status='open', severity='Critical', verification='confirmed', "
             "sources='camera,hardware', sensors=?, updated_at=? WHERE event_id=?", (sensors, now, eid))
        db.add_action(eid, "hw_confirmed", "station", why)
    elif verdict == FUSION.PENDING:
        db.x("UPDATE events SET status='verifying', verification='pending', sources='camera', sensors=?, "
             "updated_at=? WHERE event_id=?", (sensors, now, eid))
        db.add_action(eid, "hw_verifying", "station", why)
    elif verdict == FUSION.CLEAR:
        db.x("UPDATE events SET status='suppressed', verification='hardware_clear', sources='camera', sensors=?, "
             "ended_at=?, end_reason=?, updated_at=? WHERE event_id=?",
             (sensors, now, "hardware clear: " + why, now, eid))
        db.add_action(eid, "hw_suppressed", "station", why)
    else:                                   # offline / no station -> camera-only alarm
        db.x("UPDATE events SET status='open', verification=?, sources='camera', sensors=?, updated_at=? "
             "WHERE event_id=?", (verdict, sensors, now, eid))
        db.add_action(eid, "camera_only", "station", why)
    return db.get_event(eid)


async def verify_new(e, now):
    """Called by ingest_event for every NEW camera fire / smoke event, before the siren decision."""
    verdict, why = FUSION.decide(e["zone_id"], now, now)
    return _apply(e["event_id"], verdict, why, now)


# ------------------------------------------------------------------ events created by the station itself
def _create_event(etype, zone_id, device_id, severity, items, why, now, worker=None, sources="hardware",
                  verification="hardware_only"):
    eid = "hw_" + uuid.uuid4().hex[:10]
    st = CFG["devices"].get(device_id) or {}
    db.x("""INSERT INTO events (event_id, type, items_missing, severity, camera_id, camera_name, zone_id, zone_name,
            track_id, worker_id, worker_name, confidence, bbox, snapshot_path, captured_at, started_at, received_at,
            latency_ms, status, updated_at, onset_at, time_to_alert_ms, sources, verification, device_id, sensors)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
         (eid, etype, json.dumps(items or []), severity, None, st.get("name", device_id), zone_id, zone_name(zone_id),
          None, (worker or {}).get("worker_id"), (worker or {}).get("name"), 1.0, "null", None,
          datetime.now().isoformat(), now, now, 0.0, "open", now, now, 0.0, sources, verification, device_id,
          _station_json(zone_id, now)))
    db.add_action(eid, "hw_raised", "station", why)
    return db.get_event(eid)


async def _emit(e, action, now, notify=True):
    await CTX["respond"](e, action, now)
    await CTX["broadcast"]("event", e)
    if notify and action != "resolve":
        await CTX["notify_async"](e)


async def tick(now):
    # 1) camera events waiting for the station
    for r in db.q("SELECT event_id, zone_id, received_at FROM events WHERE status='verifying'"):
        verdict, why = FUSION.decide(r["zone_id"], r["received_at"], now)
        if verdict == FUSION.PENDING:
            continue
        e = _apply(r["event_id"], verdict, why, now)
        if verdict == FUSION.CLEAR:
            await CTX["broadcast"]("event", e)
        else:
            await _emit(e, "open", now)

    # 2) hazards the station sees by itself (alarm even without a camera / RFID / Wi-Fi to the camera)
    clear_after = CFG["thresholds"]["clear_after_s"]
    for dev, c in CFG["devices"].items():
        zone = (c or {}).get("zone_id")
        if not HUB.online(dev, now):
            continue
        active = [k for k in ("fire", "heat", "smoke") if now - HUB.hazard_at.get((dev, k), -1e9) <= 3]
        st = HW_OPEN.get(dev)
        if active:
            if st is None:
                why = "; ".join((HUB.latest[dev]["eval"] or {}).get("reasons") or active)
                cam = db.q("SELECT event_id FROM events WHERE zone_id=? AND type IN ('fire','smoke') AND camera_id "
                           "IS NOT NULL AND status IN ('open','escalated','acknowledged') AND ended_at IS NULL", (zone,))
                if cam:                                   # a camera alarm is already running here: confirm it
                    for x in cam:
                        db.x("UPDATE events SET verification='confirmed', sources='camera,hardware', severity="
                             "'Critical', updated_at=? WHERE event_id=?", (now, x["event_id"]))
                        db.add_action(x["event_id"], "hw_confirmed", "station", why)
                        await CTX["broadcast"]("event", db.get_event(x["event_id"]))
                    HW_OPEN[dev] = {"event_id": None, "last": now}
                else:
                    etype = "fire" if ("fire" in active or "heat" in active) else "smoke"
                    e = _create_event(etype, zone, dev, "Critical", [], why, now)
                    HW_OPEN[dev] = {"event_id": e["event_id"], "last": now}
                    await _emit(e, "open", now)
            else:
                st["last"] = now
        elif st and now - st["last"] >= clear_after:
            HW_OPEN.pop(dev, None)
            if st["event_id"]:
                e = db.get_event(st["event_id"])
                if e and e["status"] not in ("dismissed", "resolved"):
                    db.x("UPDATE events SET status='resolved', ended_at=?, end_reason=?, updated_at=? WHERE event_id=?",
                         (now, "station sensors back to normal", now, st["event_id"]))
                    db.add_action(st["event_id"], "auto_resolve", "station", "sensors back to normal")
                    await _emit(db.get_event(st["event_id"]), "resolve", now, notify=False)

        # 3) optional: helmet / shoe contact sensors of the checked-in worker
        if (CFG.get("hardware_ppe") or {}).get("alerts"):
            await _hardware_ppe(dev, zone, now)

    # 4) live push for the dashboards
    await CTX["broadcast"]("hardware", state(light=True))


async def _hardware_ppe(dev, zone, now):
    persist = CFG["hardware_ppe"].get("persist_s", 5)
    x = HUB.latest.get(dev)
    contact = ((x or {}).get("eval") or {}).get("ppe") or {}
    who = hw.session_worker(dev, zone)
    for item in ("helmet", "boots"):
        key = (dev, item)
        st = PPE_OPEN.get(key)
        bad = bool(who) and contact.get(item) == "NOT_DETECTED"
        if bad:
            if st is None:
                PPE_OPEN[key] = st = {"since": now, "event_id": None}
            if st["event_id"] is None and now - st["since"] >= persist:
                risk = ((CTX.get("ZONES") or {}).get(zone) or {}).get("risk", "medium")
                sev = "High" if risk == "high" else "Medium"
                e = _create_event("ppe_missing", zone, dev, sev, [item],
                                  f"{item} contact sensor not detected for {persist} s", now, worker=who,
                                  verification="hardware_contact")
                st["event_id"] = e["event_id"]
                await _emit(e, "open", now)
        elif st is not None:
            PPE_OPEN.pop(key, None)
            if st["event_id"]:
                db.x("UPDATE events SET status='resolved', ended_at=?, end_reason=?, updated_at=? "
                     "WHERE event_id=? AND status NOT IN ('dismissed')",
                     (now, "contact detected again" if who else "worker checked out", now, st["event_id"]))
                await _emit(db.get_event(st["event_id"]), "resolve", now, notify=False)


async def loop():
    while True:
        await asyncio.sleep(1.0)
        try:
            await tick(time.time())
        except Exception as ex:                       # a hardware glitch must never stop the backend
            print(f"[hardware] loop error: {ex!r}")


# ------------------------------------------------------------------ ESP32 endpoints
@router.post("/api/hw/telemetry")
async def telemetry(p: dict = Body(...)):
    dev = str(p.get("device_id") or "").strip()
    if not dev:
        raise HTTPException(400, "device_id required")
    if dev not in CFG["devices"]:
        CFG["devices"][dev] = {"name": dev, "zone_id": p.get("zone_id")}     # unknown station: shown, no zone
        print(f"[hardware] new station {dev} - add it to config/hardware.yaml to give it a zone")
    now = time.time()
    ev = HUB.ingest(dev, p, now)
    zone = HUB.device_zone(dev)
    a = CTX["ALARM"].state() if CTX.get("ALARM") else None
    alarm_zone = bool(a and a.get("zone") == zone) or ev["level"] == "CRITICAL"
    ppe_warn = bool(zone) and db.q("SELECT COUNT(*) AS n FROM events WHERE zone_id=? AND type='ppe_missing' "
                                   "AND status IN ('open','escalated')", (zone,))[0]["n"] > 0
    cmd = hw.command(ev["level"], alarm_zone, ppe_warn, ev["air"] == "warning")
    return dict(cmd, ok=True, level=ev["level"], reasons=ev["reasons"][:3], server_time=int(now))


@router.post("/api/sensors")
async def simple_station(p: dict = Body(...)):
    """Endpoint for the team's simple sketch: set its SERVER_URL to http://<laptop-ip>:8000/api/sensors"""
    return await telemetry(hw.from_simple_station(p))


def _sheets_mirror(r):
    """Every scan is also sent to the team's Google Sheet (hardware.yaml attendance.sheets_url), in the
    same form the RFID sketch used before: GET <script>/exec?name=..&uid=.. (extra fields are ignored)."""
    url = (CFG.get("attendance") or {}).get("sheets_url")
    if not url or not r.get("uid"):
        return
    import requests

    def send():
        try:
            requests.get(url, params={"name": r.get("name") or "UNKNOWN", "uid": r["uid"],
                                      "deviceId": r.get("device_id"), "area": zone_name(r.get("zone_id"))},
                         timeout=15, allow_redirects=True)
        except Exception as ex:
            print(f"[attendance] Google Sheets mirror failed: {ex!r}")
    threading.Thread(target=send, daemon=True).start()


ACCESS_OPEN = {}    # (worker_id, zone) -> event_id of an open RFID 'not authorised' alert


def allowed_in(zone, worker_id):
    """None = zone not restricted; True / False = worker may / may not be in this zone (zones.yaml)."""
    z = (CTX.get("ZONES") or {}).get(zone) or {}
    if z.get("allowed_workers") is None:
        return None
    return worker_id in set(z["allowed_workers"])


async def _rfid_access(r, now):
    """Restricted zone + card of a worker who is NOT on its list -> HIGH 'unauthorized_entry' alert (voice,
    dashboard, notification); it closes when the same card taps out. Allowed workers -> access GRANTED."""
    zone, wid = r.get("zone_id"), r.get("worker_id")
    ok = allowed_in(zone, wid)
    if ok is None:
        return
    r["access"] = "GRANTED" if ok else "DENIED"
    if ok:
        return
    key = (wid, zone)
    if r.get("event") == "ENTERED" and key not in ACCESS_OPEN:
        cam = db.q("SELECT event_id FROM events WHERE type='unauthorized_entry' AND worker_id=? AND zone_id=? "
                   "AND status IN ('open','acknowledged','escalated') LIMIT 1", (wid, zone))
        if cam:                          # the camera already caught this person: one alert, now confirmed by card
            ACCESS_OPEN[key] = cam[0]["event_id"]
            db.add_action(cam[0]["event_id"], "seen", "rfid", f"card {r['uid']} tapped in - confirmed")
            return
        e = _create_event("unauthorized_entry", zone, r.get("device_id"), "High", [],
                          f"RFID {r['uid']}: {r.get('name')} is not allowed in {zone_name(zone)}", now,
                          worker={"worker_id": wid, "name": r.get("name")}, sources="rfid", verification="rfid")
        ACCESS_OPEN[key] = e["event_id"]
        await _emit(e, "open", now)
    elif r.get("event") == "EXITED" and key in ACCESS_OPEN:
        eid = ACCESS_OPEN.pop(key)
        db.x("UPDATE events SET status='resolved', ended_at=?, end_reason=?, updated_at=? "
             "WHERE event_id=? AND status IN ('open','acknowledged','escalated')", (now, "left the zone (RFID)", now, eid))
        db.add_action(eid, "auto_resolve", "rfid", "card tapped out")
        e = db.get_event(eid)
        if e:
            await _emit(e, "resolve", now, notify=False)


def _rfid_params(p):
    """Accept the field names of every reader sketch we have seen: uid / UID / cardUID / card_uid / card / tag,
    device_id / deviceId / device, optional zone_id / area. The reader's own name lookup is ignored -
    the worker list on this laptop decides who the card belongs to."""
    p = {str(k): v for k, v in (p or {}).items()}
    uid = next((p[k] for k in ("uid", "UID", "cardUID", "card_uid", "card", "tag", "rfid") if p.get(k)), None)
    dev = next((p[k] for k in ("device_id", "deviceId", "device") if p.get(k)), None)
    return uid, (str(dev) if dev else None), p.get("zone_id")


def _reader_event(p):
    """ENTERED / EXITED sent by the reader, else None. Used ONLY when hardware.yaml attendance.trust_reader_event
    is true (Google-Sheets sketch: the sheet decided). Default false: the laptop decides from its database, because
    a sketch that toggles in its own RAM forgets everything on reboot ("entered" twice). 'UNKNOWN CARD' -> laptop."""
    e = str((p or {}).get("event") or "").strip().upper()
    return e if e in ("ENTERED", "EXITED") else None


READER_SEEN = {}    # RFID reader device_id -> {"at": last contact, "ip": .., "how": "ping" / "scan"}


def reader_seen(dev, ip=None, how="scan", now=None):
    READER_SEEN[dev] = {"at": time.time() if now is None else now, "ip": ip, "how": how}


def readers(now=None):
    """Every RFID reader in hardware.yaml (+ any that called in): online = contact within 10 min (the reader
    only talks on a scan / at boot, so this is 'link worked recently', not a live heartbeat)."""
    now = time.time() if now is None else now
    att = CFG.get("attendance") or {}
    ids = list((att.get("readers") or {}).keys()) + [d for d in READER_SEEN if d not in (att.get("readers") or {})]
    out = []
    for d in ids:
        s = READER_SEEN.get(d)
        out.append({"device_id": d, "zone_name": zone_name((att.get("readers") or {}).get(d) or att.get("default_zone")),
                    "last_seen": s and s["at"], "ip": s and s["ip"], "how": s and s["how"],
                    "online": bool(s and now - s["at"] < 600)})
    return out


async def handle_rfid(p, now=None, ip=None):
    now = time.time() if now is None else now
    uid, dev, zone = _rfid_params(p)
    att = CFG.get("attendance") or {}
    dev = dev or att.get("default_reader", "esp32-rfid-01")
    reader_seen(dev, ip, "scan", now)
    zone = zone or (att.get("readers") or {}).get(dev) or HUB.device_zone(dev) or att.get("default_zone") or "unknown"
    forced = _reader_event(p) if att.get("trust_reader_event") else None
    r = hw.scan(uid, dev, zone, now=now, cooldown_s=att.get("cooldown_s", 3), event=forced)
    if r["status"] == "DUPLICATE":
        return dict(r, led="none", result=_reader_text(r))
    r["zone_name"] = zone_name(zone)
    if r["status"] == "OK":
        await _rfid_access(r, now)
        r["message"] = f"{r['name']} {'entered' if r['event'] == 'ENTERED' else 'exited'} {r['zone_name']}."
        if r.get("access") == "DENIED" and r["event"] == "ENTERED":
            r["message"] = f"{r['name']} is NOT authorised for {r['zone_name']}."
        elif att.get("announce", True) and CTX.get("announce"):
            word = "Welcome" if r["event"] == "ENTERED" else "Goodbye"
            CTX["announce"](f"{word} {r['name']}. {r['zone_name']}.", zone, None, "attendance")
    elif r["status"] == "UNKNOWN_UID":
        rn = str(p.get("name") or "").strip()
        if rn and rn.upper() != "UNKNOWN":       # reader knows the name, laptop has no badge for it yet
            r["message"] += f" (reader says {rn} - link the card on the Workers page)"
    await CTX["broadcast"]("attendance", r)
    if not forced:                               # the sketch already wrote this scan to its Google Sheet
        _sheets_mirror(r)
    # result = one keyword line for simple sketches: STUDENT ENTERED / STUDENT EXITED / ACCESS DENIED / ERROR
    return dict(r, led="red" if (not r["ok"] or r.get("access") == "DENIED") else "green", result=_reader_text(r))


def _reader_text(r):
    """Plain-text reply for simple reader sketches that look for keywords (they used Google's reply before)."""
    if r.get("status") == "DUPLICATE":
        return "DUPLICATE - card held at the reader"
    if r.get("access") == "DENIED" and r.get("event") == "ENTERED":
        return f"ACCESS DENIED - STUDENT ENTERED - {r.get('name')} is not authorised for {r.get('zone_name')}"
    if r.get("event") == "ENTERED":
        return f"STUDENT ENTERED - {r.get('name')}"
    if r.get("event") == "EXITED":
        return f"STUDENT EXITED - {r.get('name')}"
    return f"ERROR - {r.get('message') or r.get('status')}"


@router.post("/api/hw/rfid")
async def rfid(request: Request):
    """Our firmware: POST JSON {uid, device_id} -> JSON reply. Form posts are accepted too."""
    try:
        p = await request.json()
    except Exception:
        p = dict(await request.form())
    p = dict(request.query_params, **(p or {}))
    return await handle_rfid(p, ip=request.client and request.client.host)


@router.post("/api/rfid")
async def rfid_sheet_sketch(request: Request):
    """The team's Google-Sheets reader sketch (DASHBOARD_URL = http://<laptop-ip>:8000/api/rfid) posts
    {name, uid, event: ENTERED|EXITED|"UNKNOWN CARD", timestamp} after the sheet answered. No reflash needed."""
    return await rfid(request)


@router.get("/api/rfid")
async def rfid_sheet_sketch_get(request: Request):
    return await rfid_get(request)


@router.get("/api/hw/rfid")
async def rfid_get(request: Request):
    """Simple reader sketches: GET /api/hw/rfid?uid=7366A320&name=..&device_id=esp32-rfid-01
    -> plain text 'STUDENT ENTERED - Chinmaya' / 'STUDENT EXITED - ..' / 'ACCESS DENIED - ..'."""
    return PlainTextResponse(_reader_text(await handle_rfid(dict(request.query_params),
                                                            ip=request.client and request.client.host)))


@router.get("/api/hw/rfid/ping")
async def rfid_ping(request: Request):
    """Reader boot check (firmware checkLaptop): proves the reader can reach this laptop; dashboard shows it."""
    att = CFG.get("attendance") or {}
    dev = _rfid_params(dict(request.query_params))[1] or att.get("default_reader", "esp32-rfid-01")
    ip = request.client and request.client.host
    reader_seen(dev, ip, "ping")
    print(f"[rfid] reader {dev} connected from {ip}")
    await CTX["broadcast"]("hardware", state(light=True))
    return PlainTextResponse(f"OK {dev}")


# ------------------------------------------------------------------ dashboard API
def _acc(x):
    """Add zone name + restricted-zone verdict (access: GRANTED / DENIED / None) to an attendance row."""
    ok = allowed_in(x.get("zone_id"), x.get("worker_id")) if x.get("worker_id") else None
    return dict(x, zone_name=zone_name(x.get("zone_id")), access=None if ok is None else ("GRANTED" if ok else "DENIED"))


def restricted_zones():
    names = {r["worker_id"]: r["name"] for r in db.q("SELECT worker_id, name FROM workers")}
    return [{"zone_id": z, "zone_name": zone_name(z), "allowed": [{"worker_id": w, "name": names.get(w, w)}
                                                                   for w in v["allowed_workers"]]}
            for z, v in (CTX.get("ZONES") or {}).items() if v.get("allowed_workers") is not None]


def state(light=False):
    now = time.time()
    out = {"devices": [HUB.snapshot(d, now) for d in CFG["devices"]],
           "present": [_acc(x) for x in hw.present()], "restricted": restricted_zones(), "readers": readers(now),
           "thresholds": CFG["thresholds"], "verify_window_s": CFG["fusion"]["verify_window_s"],
           "hardware_ppe_alerts": bool((CFG.get("hardware_ppe") or {}).get("alerts")), "server_time": now}
    if not light:
        out["scans"] = [_acc(r) for r in hw.recent_scans(40)]
        out["fire_checks"] = [db.row_to_event(r) for r in db.q(
            "SELECT * FROM events WHERE type IN ('fire','smoke') AND received_at >= ? "
            "ORDER BY received_at DESC LIMIT 40", (now - 86400,))]
    return out


@router.get("/api/hw/state")
def get_state():
    return state()


@router.get("/api/hw/history/{device_id}")
def history(device_id: str, seconds: int = 300):
    return HUB.history(device_id, min(seconds, CFG["history_s"]))


@router.get("/api/attendance")
def attendance(limit: int = 100, zone: str = None):
    return [_acc(r) for r in hw.recent_scans(min(limit, 1000), zone)]


@router.get("/api/attendance/present")
def attendance_present(zone: str = None):
    return [_acc(x) for x in hw.present(zone)]


@router.get("/api/attendance/export")
def attendance_export(days: int = 7):
    rows = db.q("SELECT * FROM attendance WHERE at >= ? ORDER BY at", (time.time() - days * 86400,))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Timestamp", "UID", "WorkerID", "Name", "Area", "Event", "Status", "DeviceID", "Message"])
    for r in rows:
        w.writerow([datetime.fromtimestamp(r["at"]).strftime("%Y-%m-%d %H:%M:%S"), r["uid"], r["worker_id"] or "",
                    r["name"] or "", zone_name(r["zone_id"]), r["event"], r["status"], r["device_id"], r["message"] or ""])
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    return Response("﻿" + buf.getvalue(), media_type="text/csv",          # BOM: Excel opens it as UTF-8
                    headers={"Content-Disposition": f'attachment; filename="attendance_{stamp}.csv"'})


@router.post("/api/workers/{worker_id}/badge")
async def set_badge(worker_id: str, p: dict = Body(...)):
    uid = hw.norm_uid(p.get("uid"))
    if not uid:
        raise HTTPException(400, "card UID required")
    if not db.q("SELECT 1 FROM workers WHERE worker_id=?", (worker_id,)):
        raise HTTPException(404, "worker not found")
    db.x("UPDATE workers SET badge_uid=NULL WHERE badge_uid=?", (uid,))       # one card = one worker
    db.x("UPDATE workers SET badge_uid=? WHERE worker_id=?", (uid, worker_id))
    await CTX["broadcast"]("workers", CTX["list_workers"]())
    return {"ok": True, "worker_id": worker_id, "uid": uid}


@router.delete("/api/workers/{worker_id}/badge")
async def clear_badge(worker_id: str):
    db.x("UPDATE workers SET badge_uid=NULL WHERE worker_id=?", (worker_id,))
    await CTX["broadcast"]("workers", CTX["list_workers"]())
    return {"ok": True}


@router.post("/api/workers/badge_only")
async def badge_only_worker(p: dict = Body(...)):
    """Register a worker with an RFID card only (no face photos): attendance works, face ID does not."""
    name, uid = (p.get("name") or "").strip(), hw.norm_uid(p.get("uid"))
    if not name or not uid:
        raise HTTPException(400, "name and card UID required")
    nums = [int(r["worker_id"][2:]) for r in db.q("SELECT worker_id FROM workers")]
    wid = f"W-{(max(nums) + 1) if nums else 1:03d}"
    db.x("UPDATE workers SET badge_uid=NULL WHERE badge_uid=?", (uid,))
    db.x("INSERT INTO workers (worker_id, name, embeddings, photos, created_at, badge_uid) VALUES (?,?,?,?,?,?)",
         (wid, name, "[]", 0, time.time(), uid))
    await CTX["broadcast"]("workers", CTX["list_workers"]())
    return {"ok": True, "worker_id": wid, "name": name, "uid": uid}
