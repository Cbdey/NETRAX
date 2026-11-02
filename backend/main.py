"""NETRAX backend (F09/F10/F15/F16). Run from the project root:
    python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
Dashboard: http://127.0.0.1:8000
"""
import asyncio
import base64
import json
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime

import cv2
import numpy as np
import yaml
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import actuators, assistant, auth, db, fire_levels, location, notify, perf, reports, timeutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("SENTINELEYE_DATA_DIR") or os.path.join(ROOT, "data")   # override: tests only
SNAP_DIR = os.path.join(DATA_DIR, "backend_snapshots")
WORKER_DIR = os.path.join(DATA_DIR, "workers")              # registration photos (consented)
os.makedirs(SNAP_DIR, exist_ok=True)
CLIP_DIR = os.path.join(DATA_DIR, "backend_clips")    # short video per incident (edge/clips.py)
os.makedirs(CLIP_DIR, exist_ok=True)
os.makedirs(WORKER_DIR, exist_ok=True)
# SENTINELEYE_DB / SENTINELEYE_MUTE=1 let tests use a scratch database and stay silent
db.init(os.environ.get("SENTINELEYE_DB") or os.path.join(ROOT, "data", "sentineleye.db"))


def load_cfg(name):
    with open(os.path.join(ROOT, "config", name), encoding="utf-8") as f:
        return yaml.safe_load(f)


ZONES = load_cfg("zones.yaml")["zones"]
from edge.config import load_cameras                      # honours enabled: false + test override
CAMERAS = load_cameras(include_off=True)
SETTINGS = load_cfg("settings.yaml")
RESP = SETTINGS.get("response") or {}
SPK = dict(RESP.get("speaker") or {})
ALM = dict(RESP.get("alarm") or {})
if os.environ.get("SENTINELEYE_MUTE"):
    SPK["local"] = ALM["local"] = False
EDGE = {"state": None, "at": 0.0}
FRAMES = {}          # camera_id -> (annotated jpeg, time)   live tiles
RAW = {}             # camera_id -> (clean jpeg, time)       registration photos only
CLIENTS = {}         # websocket -> role (viewer copies are masked)
LOOP = {"loop": None}
FIRE_CFG = dict(SETTINGS.get("fire_levels") or {})
SIREN_FROM = int(FIRE_CFG.get("siren_from_level", 2))
EMERGENCY_SEV = set((SETTINGS.get("incidents") or {}).get("emergency_severities") or ["Critical", "High"])

# Autonomous response on this laptop: speaker calls the worker, siren for fire / smoke
SPEAKER = actuators.Speaker(SPK)
ALARM = actuators.Alarm(ALM)
ACCESS_ALIAS = {}    # camera 'unauthorized_entry' id -> the RFID alert for the same person (merged)
CALLS = {}           # ppe event_id -> {"n": announcements made, "last": time, "items": (items, worker)}
SPOKEN = {}          # (camera, person, type) -> (last time the speaker called that person, items said then)
OP_CLOSED = {}       # event_id -> (camera_id, time): supervisor resolved it; the camera re-checks that person
from . import hw_api  # noqa: E402  ESP32 safety station(s): telemetry, fire cross-check, RFID attendance
from . import acceptance  # noqa: E402  live acceptance checklist (brief section 11)


def _for_viewer(kind, data):
    if kind == "event":
        return auth.mask_event(data)
    if kind in ("entrance",):
        return dict(data, worker_id=None, worker_name=None, snapshot=None, clip=None, identity=None,
                    verdict=data.get("verdict_public")) if isinstance(data, dict) else data
    if kind in ("workers",):
        return [{"worker_id": "W-***", "name": "(hidden)", "photos": w.get("photos")} for w in data] \
            if isinstance(data, list) else data
    if kind == "notification" and isinstance(data, dict):
        return {k: data.get(k) for k in ("id", "event_id", "channel", "status", "at", "queued_at", "finished_at")}
    return data


async def broadcast(kind, data):
    msg = json.dumps({"kind": kind, "data": data}, default=str)
    vmsg = None
    for ws, role in list(CLIENTS.items()):
        try:
            if role == "viewer":
                vmsg = vmsg or json.dumps({"kind": kind, "data": _for_viewer(kind, data)}, default=str)
                await ws.send_text(vmsg)
            else:
                await ws.send_text(msg)
        except Exception:
            CLIENTS.pop(ws, None)


def broadcast_threadsafe(kind, data):
    """For background threads (notification dispatcher)."""
    loop = LOOP["loop"]
    if loop is not None and loop.is_running():
        asyncio.run_coroutine_threadsafe(broadcast(kind, data), loop)


async def escalation_loop():
    while True:
        await asyncio.sleep(5)
        now = time.time()
        for eid in notify.due_for_escalation(now):
            db.x("UPDATE events SET status='escalated', updated_at=? WHERE event_id=?", (now, eid))
            db.add_action(eid, "auto_escalate", "system", "not acknowledged in time")
            e = db.get_event(eid)
            await broadcast("event", e)
            await notify_async(e, "ESCALATED (unacknowledged)", "Plant safety manager")


async def retention_loop():
    """Privacy: delete alert snapshots and clips older than privacy.snapshot_retention_days (hourly)."""
    from edge.retention import purge
    days = (SETTINGS.get("privacy") or {}).get("snapshot_retention_days", 30)
    while True:
        for eid in await asyncio.to_thread(purge, SNAP_DIR, days):
            db.x("UPDATE events SET snapshot_path=NULL WHERE event_id=?", (eid,))
        for eid in await asyncio.to_thread(purge, CLIP_DIR, days, None, ("*.mp4", "*.webm")):
            db.x("UPDATE events SET clip_path=NULL WHERE event_id=?", (eid,))
        await asyncio.sleep(3600)


async def speaker_loop():
    """Repeat the call to a worker who is still not wearing their PPE."""
    while True:
        await asyncio.sleep(1.0)
        try:
            await repeat_calls(time.time())
        except Exception as ex:           # a timer bug must never stop the loop
            print(f"[speaker] loop error: {ex!r}")


async def fire_level_loop():
    """Re-grade active fire / smoke incidents every second (duration, growth, station readings).
    The level only goes up; a raise re-notifies immediately (no cooldown) and starts the siren at Level 2."""
    while True:
        await asyncio.sleep(1.0)
        try:
            for r in db.q("SELECT event_id FROM events WHERE type IN ('fire','smoke') AND status IN "
                          "('open','acknowledged','escalated') AND ended_at IS NULL"):
                await regrade_fire(db.get_event(r["event_id"]), time.time())
        except Exception as ex:                 # noqa: BLE001
            print(f"[fire-level] loop error: {ex!r}")


async def health_loop():
    while True:
        await asyncio.sleep(3.0)
        try:
            await broadcast("health", system_health())
        except Exception as ex:                 # noqa: BLE001
            print(f"[health] loop error: {ex!r}")


@asynccontextmanager
async def lifespan(app):
    LOOP["loop"] = asyncio.get_running_loop()
    if auth.enabled():
        auth.ensure_users()
    notify.DISPATCHER.on_update = lambda row: broadcast_threadsafe("notification", row)
    notify.DISPATCHER.start()
    timeutil.set_tz(notify.load_policy().get("timezone"))
    hw_api.setup(broadcast=broadcast, respond=respond, notify_async=notify_async, announce=announce,
                 ALARM=ALARM, ZONES=ZONES, list_workers=list_workers, entrance_scan=entrance_scan)
    tasks = [asyncio.create_task(escalation_loop()), asyncio.create_task(speaker_loop()),
             asyncio.create_task(retention_loop()), asyncio.create_task(hw_api.loop()),
             asyncio.create_task(fire_level_loop()), asyncio.create_task(health_loop())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="NETRAX API", lifespan=lifespan)
app.include_router(hw_api.router)
app.include_router(acceptance.router)
ACCEPT_DIR = acceptance.setup(DATA_DIR)


def _ip(request):
    return request.client.host if request.client else "?"


def current_user(request):
    """{'user','role'} - login disabled -> everyone is 'admin' (tests, single-PC demo)."""
    if not auth.enabled():
        return {"user": "local", "role": "admin"}
    return getattr(request.state, "user", None) or auth.read_token(request.cookies.get(auth.COOKIE)) or \
        {"user": None, "role": None}


@app.middleware("http")
async def access_control(request: Request, call_next):
    if not auth.enabled():
        request.state.user = {"user": "local", "role": "admin"}
        return await call_next(request)
    path, method, ip = request.url.path, request.method, _ip(request)
    need = auth.classify(method, path, ip)
    if need in ("public", "device"):
        request.state.user = auth.read_token(request.cookies.get(auth.COOKIE))
        return await call_next(request)
    if need == "local":
        if auth.is_local(ip):
            return await call_next(request)
        return JSONResponse({"detail": "only available on the edge computer"}, status_code=403)
    u = auth.read_token(request.cookies.get(auth.COOKIE))
    if not u:
        if method == "GET" and not path.startswith(("/api/", "/snapshots", "/clips", "/workers/", "/ws")):
            return RedirectResponse(f"/login?next={path}", status_code=303)
        return JSONResponse({"detail": "login required"}, status_code=401)
    if not auth.allowed(u["role"], need):
        db.audit(u["user"], u["role"], "denied", path, f"{method} needs {need}", ip)
        return JSONResponse({"detail": f"your role ({u['role']}) cannot do this - needs {need}"}, status_code=403)
    request.state.user = u
    return await call_next(request)


# ---------- edge -> backend ----------
def _ts(iso_str, default):
    return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).timestamp() if iso_str else default


@app.post("/api/events")
async def ingest_event(p: dict = Body(...)):
    eid = p.get("event_id")
    if not eid:
        raise HTTPException(400, "event_id required")
    now = time.time()
    snap = None
    if p.get("snapshot_b64"):
        snap = os.path.join(SNAP_DIR, f"{eid}.jpg")
        with open(snap, "wb") as f:
            f.write(base64.b64decode(p["snapshot_b64"]))
    action = p.get("action", "open")
    if action == "info":                 # one-off "PPE complete" from the camera: voice only, no alert record
        if now - _ts(p.get("captured_at"), now) <= 30 and p.get("type") == "ppe_ok":
            announce(ok_text(p), p.get("zone_id"), None, "ppe_ok")
            await broadcast("ppe_ok", {k: p.get(k) for k in ("camera_id", "zone_id", "zone_name", "worker_id", "worker_name")})
        return {"ok": True, "info": True}
    if p.get("type") == "entrance_check":
        return await ingest_entrance(p, snap, now)
    if eid in ACCESS_ALIAS:              # same person already alerted by RFID / camera: one alert, not two
        return {"ok": True, "merged_into": ACCESS_ALIAS[eid]}
    if p.get("type") == "unauthorized_entry" and action == "open" and not db.get_event(eid):
        same = db.q("SELECT event_id FROM events WHERE type='unauthorized_entry' AND worker_id=? AND zone_id=? "
                    "AND status IN ('open','acknowledged','escalated') LIMIT 1", (p.get("worker_id"), p.get("zone_id")))
        if same:
            ACCESS_ALIAS[eid] = same[0]["event_id"]
            db.add_action(same[0]["event_id"], "seen", "edge", f"also seen on {p.get('camera_id')} (face ID)")
            return {"ok": True, "merged_into": same[0]["event_id"]}
    existing = db.get_event(eid)
    started = _ts(p.get("started_at"), now)
    if existing is None:
        captured = _ts(p.get("captured_at"), now)
        onset = _ts(p.get("onset_at"), started)
        db.x("""INSERT INTO events (event_id, type, items_missing, severity, camera_id, camera_name,
                zone_id, zone_name, track_id, worker_id, confidence, bbox, snapshot_path, captured_at,
                started_at, received_at, latency_ms, status, growth, updated_at, onset_at,
                time_to_alert_ms, worker_name)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
             (eid, p["type"], json.dumps(p.get("items_missing") or []), p.get("severity"),
              p.get("camera_id"), p.get("camera_name"), p.get("zone_id"), p.get("zone_name"),
              p.get("track_id"), p.get("worker_id"), p.get("confidence"), json.dumps(p.get("bbox")),
              snap, p.get("captured_at"), started, now, max(0.0, (now - captured) * 1000),
              "open", p.get("growth"), now, onset, max(0.0, (now - onset) * 1000), p.get("worker_name")))
        db.x("UPDATE events SET frame_w=?, frame_h=?, location=? WHERE event_id=?",
             (p.get("frame_w"), p.get("frame_h"), json.dumps(location.current(p.get("zone_id"), p.get("camera_id"), now)),
              eid))
        for ext in (".mp4", ".webm"):
            if os.path.exists(os.path.join(CLIP_DIR, eid + ext)):
                db.x("UPDATE events SET clip_path=? WHERE event_id=?", (os.path.join(CLIP_DIR, eid + ext), eid))
        perf.add("event_e2e_ms", (now - captured) * 1000)
        perf.add("time_to_alert_ms", (now - onset) * 1000)
        e = db.get_event(eid)
        if e["type"] in ("fire", "smoke") and action != "resolve":
            e = await hw_api.verify_new(e, now)      # wait for / ask the safety station before any siren
            e = grade_fire(e, now)
        reopen = p.get("reopen_of")
        if reopen:     # resolved by the supervisor, but the camera still sees the same violation: a NEW alarm
            db.add_action(eid, "reopened", "edge", f"still the same violation after {reopen} was resolved")
            SPOKEN.pop(_speech_key(e), None)
        await respond(e, action, now)
        await broadcast("event", e)
        if action != "resolve" and e["status"] in ("open", "escalated"):
            await notify_async(e, "still missing after resolve" if reopen and e["type"] == "ppe_missing" else
                               "still in restricted zone after resolve" if reopen else "new alert")
    elif action == "resolve":
        new_status = existing["status"] if existing["status"] in ("dismissed", "suppressed") else "resolved"
        db.x("UPDATE events SET status=?, ended_at=?, end_reason=?, updated_at=? WHERE event_id=?",
             (new_status, now, p.get("reason"), now, eid))
        db.add_action(eid, "auto_resolve", "edge", p.get("reason"))
        await respond(existing, "resolve", now)
        if (existing["type"] == "ppe_missing" and p.get("reason") == "compliant"
                and existing["status"] in ("open", "acknowledged", "escalated")):
            announce(ok_text(existing), existing.get("zone_id"), eid, "ppe_ok")     # one "thank you", no alarm
        await broadcast("event", db.get_event(eid))
    else:  # update: new items / higher severity / tracker handover
        was_critical = existing["severity"] == "Critical"
        added = [i for i in (p.get("items_missing") or []) if i not in (existing.get("items_missing") or [])]
        db.x("""UPDATE events SET items_missing=?, severity=?, track_id=?, confidence=?, bbox=?,
                snapshot_path=COALESCE(?, snapshot_path), growth=?, updated_at=?,
                worker_id=COALESCE(?, worker_id), worker_name=COALESCE(?, worker_name) WHERE event_id=?""",
             (json.dumps(p.get("items_missing") or []), p.get("severity"), p.get("track_id"),
              p.get("confidence"), json.dumps(p.get("bbox")), snap, p.get("growth"), now,
              p.get("worker_id"), p.get("worker_name"), eid))
        db.x("UPDATE events SET frame_w=COALESCE(?, frame_w), frame_h=COALESCE(?, frame_h) WHERE event_id=?",
             (p.get("frame_w"), p.get("frame_h"), eid))
        e = db.get_event(eid)
        if e["type"] in ("fire", "smoke"):
            await regrade_fire(e, now, broadcast_always=False)
            e = db.get_event(eid)
        await respond(e, action, now)
        await broadcast("event", e)
        if e["severity"] == "Critical" and not was_critical and e["type"] not in ("fire", "smoke"):
            await notify_async(e, "severity raised to CRITICAL")
        elif added and e["type"] == "ppe_missing" and e["status"] in ("open", "escalated", "acknowledged"):
            # more PPE found missing on the same person = a new finding: one message per new item set
            await notify_async(e, "more PPE missing: " + ", ".join(i.upper() for i in added))
    return {"ok": True}


# ---------- fire / smoke severity levels (section 9) ----------
def _other_sources(e):
    r = db.q("SELECT COUNT(DISTINCT COALESCE(camera_id, device_id)) AS n FROM events WHERE zone_id=? AND type IN "
             "('fire','smoke') AND event_id != ? AND status IN ('open','acknowledged','escalated') AND ended_at IS NULL",
             (e.get("zone_id"), e["event_id"]))
    return r[0]["n"] if r else 0


def grade_fire(e, now):
    """Set the level of a NEW fire / smoke incident (no notification here - ingest sends the first one)."""
    if e.get("camera_id") and e.get("zone_id"):
        sensors = hw_api._station_json(e["zone_id"], now)
        if sensors:
            db.x("UPDATE events SET sensors=? WHERE event_id=?", (sensors, e["event_id"]))
            e = db.get_event(e["event_id"])
    lvl, why, _ = fire_levels.update_level(e, now, FIRE_CFG, _other_sources(e))
    sev = e["severity"] if e["status"] in ("suppressed", "verifying") else fire_levels.SEVERITY[lvl]
    db.x("UPDATE events SET fire_level=?, fire_reasons=?, severity=?, updated_at=? WHERE event_id=?",
         (lvl, json.dumps(why), sev, now, e["event_id"]))
    db.add_action(e["event_id"], "fire_level", "system", f"Level {lvl} {fire_levels.NAMES[lvl]}: " + "; ".join(why))
    return db.get_event(e["event_id"])


async def regrade_fire(e, now, broadcast_always=False):
    if not e or e["status"] in ("suppressed", "verifying", "resolved", "dismissed", "closed"):
        return
    if e.get("camera_id") and e.get("zone_id"):
        sensors = hw_api._station_json(e["zone_id"], now)
        if sensors:
            db.x("UPDATE events SET sensors=? WHERE event_id=?", (sensors, e["event_id"]))
            e = db.get_event(e["event_id"])
    lvl, why, raised = fire_levels.update_level(e, now, FIRE_CFG, _other_sources(e))
    if not raised:
        if broadcast_always:
            await broadcast("event", e)
        return
    db.x("UPDATE events SET fire_level=?, fire_reasons=?, severity=?, updated_at=? WHERE event_id=?",
         (lvl, json.dumps(why), fire_levels.SEVERITY[lvl], now, e["event_id"]))
    db.add_action(e["event_id"], "fire_level", "system", f"raised to Level {lvl} {fire_levels.NAMES[lvl]}: "
                  + "; ".join(why))
    e = db.get_event(e["event_id"])
    if lvl >= SIREN_FROM:
        await start_alarm(e, f"{e['type']} Level {lvl}")
    await broadcast("event", e)
    await notify_async(e, f"LEVEL RAISED to {lvl} ({fire_levels.NAMES[lvl]})")


# ---------- entrance PPE verification (section 4) ----------
ENTRANCE_TEXT = {"approved": "ACCESS APPROVED — All required PPE detected. You may proceed.",
                 "denied": "ACCESS DENIED — Required PPE is incomplete. Entry is not permitted.",
                 "manual": "Verification failed — manual check required"}
ENTRANCE_GATE = {"controller": None}          # a real lock / turnstile controller registers here later
ENTRANCE_CARD = {}    # last card tapped at the entrance reader: worker_id, name, uid, device_id, zone_id, at, track, undone


def _ent_cfg(key, default):
    return (SETTINGS.get("entrance") or {}).get(key, default)


def _item_list(items):
    names = [SPK.get("item_names", {}).get(i, i) for i in items]
    return (names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]) if names else ""


def entrance_verdict(result, wid, name, missing):
    """The sentence the guide asked for, e.g. 'W-001 Chinmaya verified - all required PPE worn. You may enter.'
    Returns (with the name, without the name: viewer role)."""
    def line(who, registered):
        if result == "approved":
            return f"{who} verified - all required PPE worn. You may enter." if registered else \
                f"{who} - PPE verified. You may enter."
        if result == "denied":
            return f"{who} - {_item_list(missing)} missing. Entry not permitted." if missing else \
                f"{who} - refused at the manual check. Entry not permitted."
        return f"{who} - verification failed. Manual check required."
    reg = bool(wid and str(wid).startswith("W-"))
    return line(f"{wid} {name or ''}".strip() if reg else "Unregistered worker", reg), \
        line("Registered worker" if reg else "Unregistered worker", reg)


def _card_valid(track, now):
    """A card names the next decision within rfid_window_s; a re-check of that same person keeps it for a minute."""
    c = ENTRANCE_CARD
    if not c:
        return None
    if (c.get("track") is None or c.get("corrected")) and abs(now - c["at"]) <= _ent_cfg("rfid_window_s", 15):
        return c
    return c if c.get("track") == track and now - c["at"] <= 60 else None


def _apply_card(res, face_id, face_name, card):
    """(result, worker_id, name, by, reason) from what the face says and what the card says. Disagreeing face and
    card are never approved automatically - somebody may be using another person's card."""
    if card and face_id and card["worker_id"] != face_id:
        return "manual", card["worker_id"], card["name"], "face+rfid", \
            f"card belongs to {card['worker_id']} {card['name']} but the face matches {face_id} - check the person"
    if card:
        return res, card["worker_id"], card["name"], ("face+rfid" if face_id == card["worker_id"] else "rfid"), None
    return res, face_id, face_name, ("face" if face_id else None), None


def _attendance_fix(card, res, now):
    """The tap counted as ENTERED; a person refused at the PPE check is not inside (and is again once approved)."""
    if not card or not card.get("uid"):
        return
    event = {"denied": "EXITED", "approved": "ENTERED"}.get(res)
    if (event == "EXITED" and not card.get("undone")) or (event == "ENTERED" and card.get("undone")):
        card["undone"] = event == "EXITED"
        db.x("INSERT INTO attendance (at, uid, worker_id, name, zone_id, event, status, device_id, message) "
             "VALUES (?,?,?,?,?,?,?,?,?)", (now, card["uid"], card["worker_id"], card["name"], card["zone_id"], event, "OK",
                                            card.get("device_id"), f"entrance PPE check: {res}"))


def _entrance_say(e, res, wid, name):
    who = f"worker {spoken_id(wid)}, {name or ''}".rstrip(", ") if wid else "Unregistered worker"
    if res == "approved":
        text = f"{who}, verified. All P P E worn. You may enter." if wid else f"{who}, P P E verified. You may enter."
        announce(text, e["zone_id"], e["event_id"], "ppe_ok")
    elif res == "denied":
        announce(f"{who}, entry not permitted. Please wear your {_words(e['items_missing'])}.", e["zone_id"], e["event_id"], "ppe")
    else:
        announce(f"{who}, verification failed. Please wait for a manual check.", e["zone_id"], e["event_id"], "ppe")


async def ingest_entrance(p, snap, now):
    eid, res = p["event_id"], p.get("result") or "manual"
    if db.get_event(eid):
        return {"ok": True, "duplicate": True}
    captured = _ts(p.get("captured_at"), now)
    started = _ts(p.get("started_at"), now)
    face_id = p.get("worker_id") if str(p.get("worker_id") or "").startswith("W-") else None
    card = _card_valid(p.get("track_id"), now)
    res, wid, name, by, why = _apply_card(res, face_id, p.get("worker_name"), card)
    if card:
        card["track"] = p.get("track_id")
    p["worker_id"], p["worker_name"] = wid, name
    if why:
        p["reason"] = why
    verdict, public = entrance_verdict(res, wid, name, p.get("items_missing") or [])
    details = {"result": res, "message": ENTRANCE_TEXT.get(res), "verdict": verdict, "verdict_public": public,
               "identity": {"kind": "registered" if wid else "unregistered", "by": by, "worker_id": wid, "name": name,
                            "face": face_id, "card": card["worker_id"] if card else None},
               "required": p.get("required") or [],
               "items_ok": p.get("items_ok") or [], "items_uncertain": p.get("items_uncertain") or [],
               "evidence": p.get("evidence") or {}, "reason": p.get("reason"), "decision_ms": p.get("decision_ms"),
               "frames": p.get("frames"), "gate": p.get("gate") or "closed",
               "controller": "connected" if p.get("controller_connected") else "no access controller connected"}
    status = "closed" if res == "approved" else "open"
    db.x("""INSERT INTO events (event_id, type, items_missing, severity, camera_id, camera_name, zone_id, zone_name,
            track_id, worker_id, worker_name, confidence, bbox, snapshot_path, captured_at, started_at, received_at,
            latency_ms, status, updated_at, onset_at, time_to_alert_ms, frame_w, frame_h, location, details, ended_at,
            end_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
         (eid, "entrance_check", json.dumps(p.get("items_missing") or []), p.get("severity") or "Low",
          p.get("camera_id"), p.get("camera_name"), p.get("zone_id"), p.get("zone_name"), p.get("track_id"),
          p.get("worker_id"), p.get("worker_name"), p.get("confidence"), json.dumps(p.get("bbox")), snap,
          p.get("captured_at"), started, now, max(0.0, (now - captured) * 1000), status, now, started,
          p.get("decision_ms"), p.get("frame_w"), p.get("frame_h"),
          json.dumps(location.current(p.get("zone_id"), p.get("camera_id"), now)), json.dumps(details),
          now if status == "closed" else None, "approved" if status == "closed" else None))
    db.x("""INSERT INTO entrance_checks (check_id, camera_id, zone_id, at, result, required, items_ok, items_missing,
            items_uncertain, evidence, confidence, worker_id, worker_name, gate, snapshot_path, decision_ms, reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
         (eid, p.get("camera_id"), p.get("zone_id"), now, res, json.dumps(details["required"]),
          json.dumps(details["items_ok"]), json.dumps(p.get("items_missing") or []),
          json.dumps(details["items_uncertain"]), json.dumps(details["evidence"]), p.get("confidence"),
          p.get("worker_id"), p.get("worker_name"), details["gate"], snap, p.get("decision_ms"), p.get("reason")))
    db.add_action(eid, f"entrance_{res}", "edge", p.get("reason") or ENTRANCE_TEXT.get(res))
    perf.add("event_e2e_ms", (now - captured) * 1000)
    perf.add("entrance_decision_ms", p.get("decision_ms"))
    e = db.get_event(eid)
    _attendance_fix(card, res, now)
    _entrance_say(e, res, wid, name)
    await broadcast("event", e)
    await broadcast("entrance", entrance_payload(e))
    if res != "approved":
        await notify_async(e)
    return {"ok": True, "result": res}


async def entrance_scan(r, now):
    """A card was tapped at the entrance reader. Before the decision: remembered for it. Just after it (the camera was
    faster than the hand): the latest decision is corrected - 'unregistered' becomes the card's owner."""
    ENTRANCE_CARD.clear()
    if r.get("event") != "ENTERED":              # tapping out on the way home names nobody at the check
        return
    ENTRANCE_CARD.update(worker_id=r.get("worker_id"), name=r.get("name"), uid=r.get("uid"), device_id=r.get("device_id"),
                         zone_id=r.get("zone_id"), at=now, track=None, undone=False)
    rows = db.q("SELECT event_id FROM events WHERE type='entrance_check' AND received_at >= ? ORDER BY received_at DESC LIMIT 1",
                (now - _ent_cfg("rfid_after_s", 6),))
    e = db.get_event(rows[0]["event_id"]) if rows else None
    d = (e or {}).get("details") or {}
    who = d.get("identity") or {}
    # only a decision that has no name yet: a recognised face belongs to somebody else's check, so that card waits
    # for its own decision (the face / card comparison happens there)
    if not e or d.get("override") or who.get("card") or who.get("face"):
        return
    res, wid, name, by, why = _apply_card(d.get("result"), None, None, ENTRANCE_CARD)
    ENTRANCE_CARD.update(track=e.get("track_id"), corrected=True)
    d["result"], d["message"] = res, ENTRANCE_TEXT.get(res)
    d["verdict"], d["verdict_public"] = entrance_verdict(res, wid, name, e.get("items_missing") or [])
    d["identity"] = dict(who, kind="registered", by=by, worker_id=wid, name=name, card=wid)
    if why:
        d["reason"] = why
    status = "closed" if res == "approved" else "open"
    db.x("UPDATE events SET details=?, worker_id=?, worker_name=?, status=?, ended_at=?, updated_at=? WHERE event_id=?",
         (json.dumps(d), wid, name, status, now if status == "closed" else None, now, e["event_id"]))
    db.x("UPDATE entrance_checks SET worker_id=?, worker_name=?, result=?, reason=? WHERE check_id=?",
         (wid, name, res, why or d.get("reason"), e["event_id"]))
    db.add_action(e["event_id"], "entrance_card", "reader", f"card {wid} {name}" + (f" - {why}" if why else ""))
    e = db.get_event(e["event_id"])
    _attendance_fix(ENTRANCE_CARD, res, now)
    _entrance_say(e, res, wid, name)
    await broadcast("event", e)
    await broadcast("entrance", entrance_payload(e))


def entrance_payload(e):
    d = e.get("details") or {}
    return {"event_id": e["event_id"], "result": d.get("result"), "message": d.get("message"),
            "verdict": d.get("verdict") or d.get("message"), "verdict_public": d.get("verdict_public") or d.get("message"),
            "identity": d.get("identity") or {"kind": "registered" if str(e.get("worker_id") or "").startswith("W-")
                                              else "unregistered", "worker_id": e.get("worker_id"), "name": e.get("worker_name")},
            "clip": e.get("clip"),
            "missing": e.get("items_missing") or [], "ok": d.get("items_ok") or [],
            "uncertain": d.get("items_uncertain") or [], "required": d.get("required") or [],
            "reason": d.get("reason"), "camera_id": e.get("camera_id"), "zone_id": e.get("zone_id"),
            "zone_name": e.get("zone_name"), "at": e.get("started_at"), "time": timeutil.fmt(e.get("started_at")),
            "worker_id": e.get("worker_id"), "worker_name": e.get("worker_name"), "snapshot": e.get("snapshot"),
            "decision_ms": d.get("decision_ms"), "gate": d.get("gate"), "controller": d.get("controller"),
            "status": e.get("status"), "override": d.get("override")}


# ---------- autonomous response: speaker + siren ----------
async def notify_async(e, reason="new alert", recipient=None):
    """Queue Telegram / call / SMS / e-mail for this incident. Never blocks detection or the dashboard:
    the dispatcher thread delivers, retries and reports each provider result over the websocket."""
    if not e:
        return
    rows = await asyncio.to_thread(notify.enqueue, e, reason, recipient)
    for n in rows:
        await broadcast("notification", n)


def _words(items):
    names = [SPK.get("item_names", {}).get(i, i) for i in items]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def spoken_id(wid):
    """'W-007' -> 'W 0 0 7' so the voice reads the digits instead of 'W minus seven'."""
    return " ".join(wid.replace("-", "")) if wid else ""


def ppe_text(e):
    zone = e.get("zone_name") or e.get("zone_id")
    wid = e.get("worker_id") or ""
    if wid.startswith("W-"):
        who = f"worker {spoken_id(wid)}, {e.get('worker_name') or ''}".rstrip(", ")
    else:
        who = f"unregistered person in the {zone}"
    items = e.get("items_missing") or []
    added = [i for i in (e.get("_added") or []) if i in items]
    also = f" Also missing: {_words(added)}." if added else ""
    return f"Attention {who}.{also} Please wear your {_words(items)} now." if items else ""


def ok_text(e):
    """'PPE complete' after the camera analysed the worker (once; see state_machine._decide)."""
    wid = e.get("worker_id") or ""
    if wid.startswith("W-"):
        return f"{e.get('worker_name') or 'Worker ' + spoken_id(wid)}, PPE complete. Thank you."
    return "PPE complete. Thank you."


RISK_RANK = {"high": 1, "medium": 2, "low": 3}


def voice_rank(zone, kind, priority="normal"):
    """Zone-wise priority for the speaker: fire 0; new alert in a HIGH-risk zone 1, medium 2, low 3;
    repeats +3 (a new alert anywhere goes before a reminder); welcome / goodbye 6."""
    if priority == "urgent" or kind == "alarm":
        return 0
    if kind == "attendance":
        return 6
    if kind == "ppe_ok":
        return 5
    rank = RISK_RANK.get((ZONES.get(zone) or {}).get("risk"), 2)
    return rank + 3 if kind.endswith("_repeat") else rank


def access_text(e):
    zone = e.get("zone_name") or e.get("zone_id")
    wid = e.get("worker_id") or ""
    who = f"{e.get('worker_name') or ''}, worker {spoken_id(wid)}".strip(", ")
    return f"Attention {who}. You are not authorised in the {zone}. Please leave the {zone} now."


def alert_text(e):
    return access_text(e) if e.get("type") == "unauthorized_entry" else ppe_text(e)


def announce(text, zone, event_id, kind, priority="normal"):
    if not text:
        return None
    entry = SPEAKER.say(text, zone=zone, event_id=event_id, priority=voice_rank(zone, kind, priority))
    if entry:
        db.x("INSERT INTO announcements (event_id, zone_id, kind, text, channel, at) VALUES (?,?,?,?,?,?)",
             (event_id, zone, kind, text, entry["channel"], entry["at"]))
        if event_id:
            db.add_action(event_id, "announce", "system", text)
        asyncio.get_event_loop().create_task(broadcast("response", response_payload()))
    return entry


async def start_alarm(e, why):
    zone = e.get("zone_name") or e.get("zone_id")
    if ALARM.start(e["event_id"], e.get("zone_id"), why):
        lvl = int(e.get("fire_level") or 2)
        text = (f"Very critical. Major {e['type']} in the {zone}. Evacuate the {zone} immediately." if lvl >= 3 else
                f"Critical alert. {e['type'].title()} detected in the {zone}. Emergency response required. "
                f"Evacuate the {zone} now.")
        announce(text,
                 e.get("zone_id"), e["event_id"], "alarm", priority="urgent")
        db.add_action(e["event_id"], "alarm_on", "system", why)
        await broadcast("response", response_payload())


async def stop_alarm(event_id, why):
    if ALARM.stop(why, event_id=event_id):
        db.add_action(event_id, "alarm_off", "system", why)
        await broadcast("response", response_payload())


async def respond(e, action, now):
    """Fire / smoke -> siren + evacuation call. PPE -> the speaker calls the worker by ID,
    once per worker per new violation (and again when items or the identified worker change)."""
    eid = e["event_id"]
    if e["type"] in ("fire", "smoke"):
        if action == "resolve":
            await stop_alarm(eid, "fire cleared")
        elif e["status"] in ("open", "escalated"):
            lvl = int(e.get("fire_level") or 2)
            if lvl >= SIREN_FROM or not e.get("camera_id"):        # station-only hazards keep the siren
                await start_alarm(e, f"{e['type']} Level {lvl} on {e.get('camera_id') or 'safety station'}")
            elif eid not in CALLS:
                zone = e.get("zone_name") or e.get("zone_id")
                announce(f"Warning. Small {e['type']} detected in the {zone}. Immediate inspection required.",
                         e.get("zone_id"), eid, "alarm", priority="urgent")
                CALLS[eid] = {"n": 1, "last": now, "items": ("fire_warning",)}
        return
    if e["type"] in ("ppe_missing", "unauthorized_entry"):
        if action == "resolve":
            CALLS.pop(eid, None)
            return
        # speak again ONLY when the decided set of missing items changes - a name arriving later, a tracker
        # handover or a new snapshot is not a new alarm (12 Oct: "one alarm, not continuous")
        key = tuple(e["items_missing"])
        c = CALLS.get(eid)
        if e["status"] in ("open", "escalated", "acknowledged") and (c is None or c["items"] != key):
            kind = "access" if e["type"] == "unauthorized_entry" else "ppe"
            sk = _speech_key(e)
            last_t, last_items = SPOKEN.get(sk, (-1e9, ()))
            heard = set(c["items"]) if c else set(last_items)
            added = [i for i in key if i not in heard]
            quiet = SPK.get("same_person_quiet_s", 20)
            # silent: items only put back on / same list again, or a tracker slip re-opened the same person
            # (inside quiet_s with nothing new). A NEW missing item is always called out (29 Oct).
            if not added or \
                    (c is None and now - last_t < quiet and set(key) <= set(last_items)):
                why = ("same person was called less than " + str(quiet) + " s ago" if c is None
                       else "no new missing item")
                db.add_action(eid, "voice_merged", "system", why + " - dashboard updated, no extra call")
                CALLS[eid] = {"n": c["n"] if c else 1, "last": c["last"] if c else now, "items": key}
            else:
                announce(alert_text(dict(e, _added=added if c is not None else [])), e.get("zone_id"), eid, kind)
                SPOKEN[sk] = (now, key)
                CALLS[eid] = {"n": 1, "last": now, "items": key}


def _speech_key(e):
    """Who the speaker is talking to: a registered worker by id, anyone else as 'unregistered' (visitor ids
    V-001 / V-002 for the same face after a tracker slip must not make two calls)."""
    wid = e.get("worker_id") or ""
    return e.get("camera_id"), wid if wid.startswith("W-") else "unregistered", e.get("type")


async def repeat_calls(now):
    every, limit = SPK.get("repeat_after_s", 30), SPK.get("max_repeats", 2)
    for eid, c in list(CALLS.items()):
        if now - c["last"] < every:
            continue
        e = db.get_event(eid)
        if e is None or e["status"] not in ("open", "escalated") or (limit and c["n"] > limit):
            CALLS.pop(eid, None)
            continue
        sk = _speech_key(e)
        c["n"], c["last"] = c["n"] + 1, now
        if now - SPOKEN.get(sk, (-1e9, ()))[0] < every:   # another record of the same person was just called
            continue
        announce(alert_text(e), e.get("zone_id"), eid,
                 "access_repeat" if e["type"] == "unauthorized_entry" else "ppe_repeat")
        SPOKEN[sk] = (now, tuple(e.get("items_missing") or ()))


def response_payload():
    return {"alarm": ALARM.state(), "speaker": {"mode": SPEAKER.mode, "log": list(SPEAKER.log)[:8]}}


@app.get("/api/response")
def get_response():
    return response_payload()


@app.post("/api/alarm/silence")
async def silence(body: dict = Body(default=None)):
    a = ALARM.state()
    if a and ALARM.stop("silenced by " + ((body or {}).get("by_user") or "supervisor")):
        db.add_action(a["event_id"], "alarm_off", (body or {}).get("by_user") or "supervisor", "silenced")
    await broadcast("response", response_payload())
    return {"ok": True}


# ---------- worker registration (one person = one ID) ----------
_FACES = None


def faces():
    global _FACES
    if _FACES is None:
        from edge.identity import FaceEngine
        _FACES = FaceEngine()
    return _FACES


def _worker_out(r):
    return {"worker_id": r["worker_id"], "name": r["name"], "created_at": r["created_at"],
            "photos": r["photos"], "badge_uid": r.get("badge_uid"), "photo": f"/workers/{r['worker_id']}.jpg?v={int(r['created_at'])}"}


def list_workers():
    return [_worker_out(r) for r in db.q("SELECT * FROM workers ORDER BY worker_id")]


@app.get("/api/workers")
def list_workers_route(request: Request):
    ws = list_workers()
    return _for_viewer("workers", ws) if current_user(request).get("role") == "viewer" else ws


@app.get("/api/workers/gallery")
def worker_gallery():
    """Embeddings for the edge identity layer (never raw photos)."""
    rows = db.q("SELECT worker_id, name, embeddings, created_at FROM workers "
                "WHERE embeddings IS NOT NULL AND embeddings != '[]' ORDER BY worker_id")   # card-only workers have no face
    version = ",".join(f"{r['worker_id']}:{r['created_at']}" for r in rows)
    return {"version": version, "workers": [{"worker_id": r["worker_id"], "name": r["name"],
                                             "embeddings": json.loads(r["embeddings"])} for r in rows]}


def _decode(data_url):
    raw = base64.b64decode(data_url.split(",", 1)[-1])
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "could not read one of the photos")
    return img


@app.post("/api/workers")
async def register_worker(p: dict = Body(...)):
    """{name, photos: [data-URL, ...]}  or  {name, camera_id}: 3 photos from that camera, 1 s apart."""
    from edge.identity import face_crop
    name = (p.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    imgs = [_decode(x) for x in (p.get("photos") or [])[:5]]
    if p.get("camera_id"):
        for k in range(3):
            jpg, t = RAW.get(p["camera_id"], (None, 0))
            if jpg is None or time.time() - t > 3:
                raise HTTPException(409, "no live picture from that camera - is the edge running?")
            imgs.append(cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR))
            if k < 2:
                await asyncio.sleep(1.0)
    if not imgs:
        raise HTTPException(400, "add at least one photo or capture from a camera")
    embs, crop = [], None
    for img in imgs:
        try:
            f, e = await asyncio.to_thread(faces().best_face, img)
        except ValueError:
            continue
        embs.append(e)
        if crop is None:
            crop = face_crop(img, f)
    if not embs:
        raise HTTPException(400, "no clear face found - look at the camera, good light, no mask or helmet visor")
    for r in db.q("SELECT worker_id, name, embeddings FROM workers"):     # already registered?
        g = np.array(json.loads(r["embeddings"] or "[]"), np.float32)
        if g.size == 0:
            continue
        if float((g @ np.array(embs).T).max()) >= 0.55:
            raise HTTPException(409, f"this person looks already registered as {r['worker_id']} ({r['name']})")
    nums = [int(r["worker_id"][2:]) for r in db.q("SELECT worker_id FROM workers")]
    wid = f"W-{(max(nums) + 1) if nums else 1:03d}"
    cv2.imwrite(os.path.join(WORKER_DIR, f"{wid}.jpg"), crop)
    now = time.time()
    db.x("INSERT INTO workers (worker_id, name, embeddings, photos, created_at) VALUES (?,?,?,?,?)",
         (wid, name, json.dumps([e.round(5).tolist() for e in embs]), len(embs), now))
    w = _worker_out(db.q("SELECT * FROM workers WHERE worker_id=?", (wid,))[0])
    await broadcast("workers", list_workers())
    return dict(w, used_photos=len(embs), total_photos=len(imgs))


@app.delete("/api/workers/{worker_id}")
async def delete_worker(worker_id: str):
    db.x("DELETE FROM workers WHERE worker_id=?", (worker_id,))
    try:
        os.remove(os.path.join(WORKER_DIR, f"{worker_id}.jpg"))
    except OSError:
        pass
    await broadcast("workers", list_workers())
    return {"ok": True}


@app.post("/api/edge/health")
async def edge_health(p: dict = Body(...)):
    EDGE["state"], EDGE["at"] = p, time.time()
    await broadcast("cameras", cameras_payload())
    return {"ok": True}


@app.post("/api/cameras/{camera_id}/frame")
async def camera_frame(camera_id: str, request: Request):
    now = time.time()
    try:
        cap = float(request.headers.get("x-captured-at") or now)
    except ValueError:
        cap = now
    FRAMES[camera_id] = (await request.body(), now, cap)
    perf.add("preview_edge_to_backend_ms", (now - cap) * 1000)
    for k in [k for k, (_, t) in OP_CLOSED.items() if now - t > 30]:
        del OP_CLOSED[k]
    closed = [k for k, (c, _) in OP_CLOSED.items() if c == camera_id]
    return {"ok": True, "closed": closed} if closed else {"ok": True}


@app.post("/api/events/{event_id}/clip")
async def event_clip(event_id: str, request: Request):
    """Edge uploads the short incident video (edge/clips.py) - linked to the same event id everywhere."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,80}", event_id):
        raise HTTPException(400, "bad event id")
    ext = ".webm" if "webm" in (request.headers.get("content-type") or "") else ".mp4"
    data = await request.body()
    if not data or len(data) > 50 * 1024 * 1024:
        raise HTTPException(400, "empty or too large")
    f = os.path.join(CLIP_DIR, event_id + ext)
    await asyncio.to_thread(lambda: open(f, "wb").write(data))
    if db.get_event(event_id):
        db.x("UPDATE events SET clip_path=? WHERE event_id=?", (f, event_id))
        db.add_action(event_id, "clip_saved", "edge", f"{len(data) // 1024} KB video")
        await broadcast("event", db.get_event(event_id))
    return {"ok": True, "clip": f"/clips/{event_id}{ext}"}


@app.post("/api/cameras/{camera_id}/raw")
async def camera_raw(camera_id: str, request: Request):
    RAW[camera_id] = (await request.body(), time.time())       # kept in memory only
    return {"ok": True}


# ---------- dashboard API ----------
def _hw_badge(zone_id, badge):
    """Camera says FIRE/SMOKE but the station decides: show 'verifying' / 'hardware clear' on the tile."""
    if not badge or not any(w in badge for w in ("FIRE", "SMOKE")):
        return badge
    r = db.q("SELECT status, verification FROM events WHERE zone_id=? AND type IN ('fire','smoke') "
             "AND camera_id IS NOT NULL ORDER BY received_at DESC LIMIT 1", (zone_id,))
    if r and r[0]["status"] == "verifying":
        return "verifying " + badge.split(" ")[0].lower() + " with safety station"
    if r and r[0]["status"] == "suppressed":
        return badge.split(" ")[0].title() + "? camera only - hardware clear"
    return badge


def cameras_payload():
    edge_alive = time.time() - EDGE["at"] < 5
    by_id = {c["camera_id"]: c for c in (EDGE["state"] or {}).get("cameras", [])} if edge_alive else {}
    out = []
    for cam_id, cam in CAMERAS.items():
        z = ZONES[cam["default_zone"]]
        h = by_id.get(cam_id, {})
        out.append({
            "camera_id": cam_id, "name": cam.get("name", cam_id), "zone_id": cam["default_zone"],
            "zone_name": z["name"], "risk": z["risk"],
            "status": "OFF" if cam.get("enabled", True) is False else h.get("status", "OFFLINE"),
            "detail": "Camera switched off" if cam.get("enabled", True) is False else
                      h.get("detail", "edge not running" if not edge_alive else "no data"),
            "fps": h.get("fps", 0), "badge": _hw_badge(cam["default_zone"], h.get("badge")),
            "infer_ms": h.get("infer_ms"), "share": cam.get("share"), "role": cam.get("role", "monitor"),
        })
    st = EDGE["state"] or {}
    return {"edge_online": edge_alive, "cameras": out,
            "edge": {"device": st.get("device"), "runtime": st.get("runtime"),
                     "outbox_pending": st.get("outbox_pending")}}


@app.get("/api/cameras")
def get_cameras():
    return cameras_payload()


@app.post("/api/cameras/{camera_id}/activate")
def activate_camera(camera_id: str):
    """Camera pair (cameras.yaml share:): give the device slot to this camera; the edge switches within ~1 s."""
    from edge.share import write_choice
    cam = CAMERAS.get(camera_id)
    if not cam:
        raise HTTPException(404, "unknown camera")
    if not cam.get("share"):
        raise HTTPException(400, "camera is not part of a camera pair")
    write_choice(str(cam["share"]), camera_id)
    return {"ok": True, "pair": cam["share"], "active": camera_id}


@app.get("/api/zones")
def get_zones():
    open_by_zone = {r["zone_id"]: r["n"] for r in db.q(
        "SELECT zone_id, COUNT(*) AS n FROM events WHERE status IN ('open','escalated') "
        "AND ended_at IS NULL GROUP BY zone_id")}
    cams = cameras_payload()["cameras"]
    out = []
    for zid, z in ZONES.items():
        badges = [c["badge"] for c in cams if c["zone_id"] == zid and c["badge"]]
        fire = next((b for b in badges if "verifying" in b or "FIRE" in b or "SMOKE" in b), None)
        n = open_by_zone.get(zid, 0)
        status = ("Verifying fire" if fire and "verifying" in fire else
                  fire.title() if fire else f"{n} open" if n else "Compliant")
        trusted = set((SETTINGS.get("models") or {}).get("trusted_items") or z["required_ppe"])
        allowed = z.get("allowed_workers")
        names = {r["worker_id"]: r["name"] for r in db.q("SELECT worker_id, name FROM workers")} if allowed else {}
        out.append({"zone_id": zid, "name": z["name"], "required_ppe": z["required_ppe"],
                    "not_checked": [i for i in z["required_ppe"] if i not in trusted],
                    "risk": z["risk"], "open": n, "status": status,
                    "allowed": None if allowed is None else [names.get(w, w) for w in allowed],
                    "inside": [{"name": p["name"], "worker_id": p["worker_id"], "since": p["since"],
                                "allowed": None if allowed is None else p["worker_id"] in allowed}
                               for p in hw_api.hw.present(zid)]})
    return out


@app.get("/api/events")
def list_events_route(request: Request, status: str = None, severity: str = None, zone: str = None,
                      camera: str = None, limit: int = 100):
    rows = list_events(status, severity, zone, camera, limit)
    return [auth.mask_event(r) for r in rows] if current_user(request).get("role") == "viewer" else rows


def list_events(status: str = None, severity: str = None, zone: str = None,
                camera: str = None, limit: int = 100):
    sql, args = "SELECT * FROM events WHERE 1=1", []
    for col, val in (("status", status), ("severity", severity), ("zone_id", zone), ("camera_id", camera)):
        if val:
            sql += f" AND {col} IN ({','.join('?' * len(val.split(',')))})"
            args += val.split(",")
    sql += " ORDER BY COALESCE(updated_at, received_at) DESC LIMIT ?"
    args.append(min(limit, 500))
    return [db.row_to_event(r) for r in db.q(sql, args)]


async def _act(event_id, action, status, body, request=None):
    e = db.get_event(event_id)
    if e is None:
        raise HTTPException(404, "event not found")
    u = current_user(request) if request is not None else {"user": None, "role": None}
    user = u.get("user") or (body or {}).get("by_user") or "supervisor"
    now = time.time()
    if action == "resolve" and e["status"] in ("resolved", "closed", "dismissed"):
        return e
    db.x("UPDATE events SET status=?, updated_at=? WHERE event_id=?", (status, now, event_id))
    if action == "ack":
        db.x("UPDATE events SET acked_at=?, acked_by=? WHERE event_id=?", (now, user, event_id))
    if action in ("resolve", "dismiss"):
        db.x("UPDATE events SET ended_at=COALESCE(ended_at, ?), closed_by=?, end_reason=COALESCE(end_reason, ?) "
             "WHERE event_id=?", (now, user, f"{action} by {user}", event_id))
    db.add_action(event_id, action, user, (body or {}).get("note"))
    db.audit(user, u.get("role"), action, event_id, (body or {}).get("note"), _ip(request) if request else None)
    if action == "resolve" and e["type"] in ("ppe_missing", "unauthorized_entry") and e.get("camera_id"):
        OP_CLOSED[event_id] = (e["camera_id"], now)   # the camera looks again; still missing -> new alert
    if action in ("ack", "dismiss", "resolve"):
        CALLS.pop(event_id, None)                     # supervisor took over: stop repeat calls
        await stop_alarm(event_id, f"{action} by {user}")
    e = db.get_event(event_id)
    await broadcast("event", e)
    if action == "escalate":
        await notify_async(e, "ESCALATED by supervisor", "Plant safety manager")
    return e


@app.post("/api/events/{event_id}/ack")
async def ack(event_id: str, request: Request, body: dict = Body(default=None)):
    return await _act(event_id, "ack", "acknowledged", body, request)


@app.post("/api/events/{event_id}/escalate")
async def escalate(event_id: str, request: Request, body: dict = Body(default=None)):
    return await _act(event_id, "escalate", "escalated", body, request)


@app.post("/api/events/{event_id}/dismiss")
async def dismiss(event_id: str, request: Request, body: dict = Body(default=None)):
    return await _act(event_id, "dismiss", "dismissed", body, request)


@app.post("/api/events/{event_id}/resolve")
async def resolve(event_id: str, request: Request, body: dict = Body(default=None)):
    """Supervisor closes an incident after the hazard is gone (acknowledged != resolved)."""
    return await _act(event_id, "resolve", "resolved", body, request)


@app.post("/api/entrance/{event_id}/decide")
async def entrance_override(event_id: str, request: Request, body: dict = Body(...)):
    """Manual check result after 'Verification failed': {decision: approved|denied, note}. Recorded + audited."""
    e = db.get_event(event_id)
    if not e or e["type"] != "entrance_check":
        raise HTTPException(404, "entrance check not found")
    dec = (body or {}).get("decision")
    if dec not in ("approved", "denied"):
        raise HTTPException(400, "decision must be approved or denied")
    u = current_user(request)
    d = e.get("details") or {}
    d["override"] = {"decision": dec, "by": u.get("user"), "at": time.time(), "note": (body or {}).get("note")}
    now = time.time()
    d["verdict"], d["verdict_public"] = entrance_verdict(dec, e.get("worker_id"), e.get("worker_name"), [])
    if ENTRANCE_CARD.get("worker_id") and ENTRANCE_CARD["worker_id"] == e.get("worker_id"):
        _attendance_fix(ENTRANCE_CARD, dec, now)
    db.x("UPDATE events SET details=?, status='closed', ended_at=?, end_reason=?, closed_by=?, updated_at=? "
         "WHERE event_id=?", (json.dumps(d), now, f"manual check: {dec}", u.get("user"), now, event_id))
    db.x("UPDATE entrance_checks SET reason=COALESCE(reason,'') || ? WHERE check_id=?",
         (f" | manual check by {u.get('user')}: {dec}", event_id))
    db.add_action(event_id, f"manual_{dec}", u.get("user") or "supervisor", (body or {}).get("note"))
    db.audit(u.get("user"), u.get("role"), f"entrance_manual_{dec}", event_id, (body or {}).get("note"), _ip(request))
    e = db.get_event(event_id)
    await broadcast("event", e)
    await broadcast("entrance", entrance_payload(e))
    return e


@app.get("/api/notifications")
def notifications(request: Request, limit: int = 20):
    rows = db.q("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (min(limit, 500),))
    if current_user(request).get("role") == "viewer":
        rows = [_for_viewer("notification", r) for r in rows]
    return rows


@app.get("/api/analytics/summary")
def analytics():
    return reports.summary(EDGE["state"] if time.time() - EDGE["at"] < 5 else None)


@app.get("/api/reports/export")
def export(format: str = "csv", kind: str = "shift", zone: str = None, shift: str = None, days: int = 1):
    evs = reports.events_between(time.time() - days * 86400, zone, shift)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    if format == "pdf":
        title = "NETRAX compliance report" + (f" - {shift} shift" if shift else "") + \
                (f" - {ZONES[zone]['name']}" if zone in ZONES else "")
        return Response(reports.to_pdf(evs, title, reports.shift_summary()), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="netrax_{stamp}.pdf"'})
    body = reports.zone_summary_csv(evs) if kind == "zone" else reports.to_csv(evs)
    return Response(body, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="netrax_{kind}_{stamp}.csv"'})


@app.get("/api/stream/{camera_id}")
async def stream(camera_id: str):
    async def gen():
        last = 0.0
        while True:
            jpg, t = FRAMES.get(camera_id, (None, 0, 0))[:2]
            if jpg and t != last:
                last = t
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
            await asyncio.sleep(0.1)
    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/api/frame/{camera_id}")
def latest_frame(camera_id: str):
    jpg, t, cap = FRAMES.get(camera_id, (None, 0, 0))
    if not jpg or time.time() - t > 10:
        return Response(status_code=204)
    return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store", "X-Captured-At": f"{cap:.3f}",
                                                           "X-Server-Time": f"{time.time():.3f}"})


@app.websocket("/ws/alerts")
async def ws_alerts(ws: WebSocket):
    role = "admin"
    if auth.enabled():
        u = auth.read_token(ws.cookies.get(auth.COOKIE))
        if not u:
            await ws.close(code=4401)
            return
        role = u["role"]
    await ws.accept()
    CLIENTS[ws] = role
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        CLIENTS.pop(ws, None)
    except Exception:
        CLIENTS.pop(ws, None)


@app.get("/")
def index():
    return FileResponse(os.path.join(ROOT, "dashboard", "index.html"))


@app.get("/admin")
def admin_page():
    """Analytics, reports, worker registration (the earlier dashboard)."""
    return FileResponse(os.path.join(ROOT, "dashboard", "admin.html"))


@app.get("/login")
def login_page():
    return FileResponse(os.path.join(ROOT, "dashboard", "login.html"))


@app.post("/api/login")
async def login(request: Request, body: dict = Body(...)):
    u, err = auth.check_password((body or {}).get("username", ""), (body or {}).get("password", ""), _ip(request))
    if not u:
        db.audit((body or {}).get("username"), None, "login_failed", None, err, _ip(request))
        raise HTTPException(401, err)
    db.audit(u["user"], u["role"], "login", None, None, _ip(request))
    r = JSONResponse({"ok": True, "user": u["user"], "role": u["role"]})
    r.set_cookie(auth.COOKIE, auth.make_token(u["user"], u["role"]), httponly=True, samesite="lax", max_age=auth.TTL_S)
    return r


@app.post("/api/logout")
async def logout(request: Request):
    u = current_user(request)
    if u.get("user"):
        db.audit(u["user"], u["role"], "logout", None, None, _ip(request))
    r = JSONResponse({"ok": True})
    r.delete_cookie(auth.COOKIE)
    return r


@app.get("/api/me")
def me(request: Request):
    u = current_user(request)
    return {"user": u.get("user"), "role": u.get("role"), "auth": auth.enabled()}


# ---------- AI assistant (section 3) ----------
@app.post("/api/assistant")
async def ask(request: Request, body: dict = Body(...)):
    q = ((body or {}).get("question") or "").strip()
    if not q:
        raise HTTPException(400, "ask a question, e.g. 'What happened yesterday at 4 PM?'")
    u = current_user(request)
    sec = notify.load_secrets().get("assistant") or {}
    workers = db.q("SELECT worker_id, name FROM workers")
    res = await asyncio.to_thread(assistant.answer, q[:500], u.get("user") or "?", u.get("role") or "viewer",
                                  workers, ZONES, CAMERAS, None, sec.get("anthropic_api_key") or None,
                                  sec.get("model"))
    perf.add("assistant_ms", res["ms"])
    db.audit(u.get("user"), u.get("role"), "assistant_query", None, q[:200], _ip(request))
    return res


# ---------- history search (section 10) ----------
@app.get("/api/events/search")
def search_events(request: Request, start: float = None, end: float = None, type: str = None, zone: str = None,
                  camera: str = None, worker: str = None, severity: str = None, status: str = None,
                  limit: int = 200):
    sql, args = "SELECT * FROM events WHERE 1=1", []
    if start:
        sql += " AND started_at >= ?"
        args.append(start)
    if end:
        sql += " AND started_at <= ?"
        args.append(end)
    for col, val in (("type", type), ("zone_id", zone), ("camera_id", camera), ("worker_id", worker),
                     ("severity", severity), ("status", status)):
        if val:
            vals = val.split(",")
            sql += f" AND {col} IN ({','.join('?' * len(vals))})"
            args += vals
    sql += " ORDER BY started_at DESC LIMIT ?"
    args.append(min(limit, 1000))
    rows = [db.row_to_event(r) for r in db.q(sql, args)]
    if current_user(request).get("role") == "viewer":
        rows = [auth.mask_event(r) for r in rows]
    return rows


@app.get("/api/events/{event_id}")
def get_one_event(event_id: str, request: Request):
    e = db.get_event(event_id)
    if not e:
        raise HTTPException(404, "event not found")
    e["notifications"] = db.q("SELECT id, channel, recipient, status, attempts, provider_status, error, queued_at, "
                              "finished_at, latency_ms FROM notifications WHERE event_id=? ORDER BY id", (event_id,))
    return auth.mask_event(e) if current_user(request).get("role") == "viewer" else e


@app.get("/api/entrance/status")
def entrance_status(request: Request):
    """Everything the entrance dashboard shows besides the log: camera, required PPE, reader, gate."""
    cam = next((c for c in cameras_payload()["cameras"] if c.get("role") == "entrance"), None)
    zone = ZONES.get((cam or {}).get("zone_id")) or {}
    viewer = current_user(request).get("role") == "viewer"
    card = dict(ENTRANCE_CARD) if ENTRANCE_CARD and time.time() - ENTRANCE_CARD["at"] < 120 else None
    readers = [r for r in hw_api.readers(time.time()) if r.get("zone_name") == zone.get("name")]
    return {"camera": cam, "required": zone.get("required_ppe") or [], "zone_name": zone.get("name"),
            "controller_connected": bool(_ent_cfg("controller_connected", False)), "readers": readers,
            "card": None if viewer or not card else {k: card.get(k) for k in ("worker_id", "name", "at")},
            "rfid_window_s": _ent_cfg("rfid_window_s", 15)}


@app.get("/api/entrance")
def entrance_list(request: Request, limit: int = 30):
    rows = [entrance_payload(db.row_to_event(r)) for r in
            db.q("SELECT * FROM events WHERE type='entrance_check' ORDER BY started_at DESC LIMIT ?", (min(limit, 200),))]
    if current_user(request).get("role") == "viewer":
        rows = [dict(r, worker_id=None, worker_name=None, snapshot=None, clip=None, identity=None,
                     verdict=r.get("verdict_public")) for r in rows]
    return rows


# ---------- location (section 6, software side) ----------
@app.get("/api/location")
def get_location():
    return location.status()


@app.post("/api/location/device")
async def device_location(request: Request, body: dict = Body(...)):
    """Browser geolocation of the dashboard device (needs the user's permission in the browser)."""
    ok, msg = location.record("device", body.get("lat"), body.get("lon"), body.get("accuracy_m"),
                              (body.get("at") or time.time() * 1000) / 1000.0, device_id=_ip(request))
    if not ok:
        raise HTTPException(400, msg)
    await broadcast("location", location.status())
    return location.status()


@app.post("/api/location/hardware")
async def hardware_location(body: dict = Body(...)):
    """Ready for the hardware GPS / GSM unit: {device_id, lat, lon, accuracy_m, fix: true, at: unix seconds}."""
    if body.get("fix") is False:
        raise HTTPException(400, "no GPS fix - reading not stored")
    ok, msg = location.record("hardware", body.get("lat"), body.get("lon"), body.get("accuracy_m"), body.get("at"),
                              device_id=body.get("device_id"))
    if not ok:
        raise HTTPException(400, msg)
    await broadcast("location", location.status())
    return {"ok": True}


# ---------- performance (section 7) ----------
@app.get("/api/perf")
def get_perf(window_s: float = None):
    st = EDGE["state"] or {}
    return {"metrics": perf.summary(window_s), "edge": {"engine": st.get("engine"), "runtime": st.get("runtime"),
            "cameras": {c["camera_id"]: dict(c.get("latency") or {}, fps=c.get("fps"), status=c.get("status"))
                        for c in st.get("cameras", [])}} if time.time() - EDGE["at"] < 5 else None,
            "notifications": notify.status()}


@app.post("/api/perf/client")
async def client_perf(body: dict = Body(...)):
    """The dashboard reports what the operator actually sees: picture age and event arrival delay."""
    for k in ("preview_age_ms", "dashboard_event_ms"):
        for v in (body.get(k) or [])[:200]:
            perf.add(k, v)
    return {"ok": True}


# ---------- notifications (section 5) ----------
@app.get("/api/notify/status")
def notify_status():
    return notify.status()


@app.post("/api/notify/test")
async def notify_test(request: Request, body: dict = Body(default=None)):
    """Admin: send a clearly-marked TEST message on the chosen channels and log the provider result."""
    chans = (body or {}).get("channels") or ["telegram", "call", "sms", "email"]
    now = time.time()
    eid = "test_" + str(int(now))
    db.x("INSERT INTO events (event_id, type, items_missing, severity, zone_id, zone_name, camera_id, started_at, "
         "received_at, status, updated_at, ended_at, end_reason, location) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (eid, "test", "[]", "Low", None, "TEST - no incident", None, now, now, "closed", now, now, "notification test",
          json.dumps(location.current(None, None, now))))
    e = db.get_event(eid)
    rows = await asyncio.to_thread(notify.enqueue, e, "TEST MESSAGE - please ignore", None, "test", chans)
    u = current_user(request)
    db.audit(u.get("user"), u.get("role"), "notify_test", eid, ",".join(chans), _ip(request))
    return {"event_id": eid, "queued": rows}


# ---------- system health (section 10) ----------
def system_health():
    now = time.time()
    comps = []
    def add(name, state, msg):
        comps.append({"name": name, "state": state, "message": msg})
    edge_alive = now - EDGE["at"] < 5
    add("Edge AI computer", "ok" if edge_alive else "fail",
        (EDGE["state"] or {}).get("runtime", "running") if edge_alive else "not reporting - live detection stopped")
    for c in cameras_payload()["cameras"]:
        st = c["status"]
        add(f"Camera {c['camera_id']} ({c['name']})", "ok" if st == "ONLINE" else "off" if st in ("OFF", "STANDBY")
            else "warn" if st == "DEGRADED" else "fail", f"{st} - {c.get('detail') or ''} {c.get('fps') or 0} fps".strip())
    ok, msg = db.health()
    add("Database", "ok" if ok else "fail", msg)
    ns = notify.status()
    for ch, v in ns["channels"].items():
        last = v.get("last") or {}
        if not v["configured"]:
            add(f"Alerts: {ch}", "warn", "NOT CONFIGURED - messages are not sent on this channel")
        elif last.get("status") == "failed":
            add(f"Alerts: {ch}", "fail", f"last attempt failed: {last.get('error') or ''}"[:160])
        else:
            add(f"Alerts: {ch}", "ok", f"ready{' - last ' + last['status'] if last.get('status') else ''}")
    if ns["queue"]:
        add("Alert queue", "warn" if ns["queue"] > 5 else "ok", f"{ns['queue']} message(s) waiting / being retried")
    pend = (EDGE["state"] or {}).get("outbox_pending")
    if pend:
        add("Edge offline buffer", "warn", f"{pend} alert(s) waiting to reach this server")
    try:
        for d in (hw_api.state(light=True).get("devices") or []):
            add(f"Safety station {d.get('device_id')}", "ok" if d.get("online") else "warn",
                "online" if d.get("online") else "offline - camera alerts go ahead without hardware check")
    except Exception:                          # noqa: BLE001
        pass
    loc = location.current(now=now)
    add("Location", "ok" if loc["valid"] else "warn", loc["note"] + ("; " + "; ".join(loc.get("warnings") or [])
                                                                     if loc.get("warnings") else ""))
    try:
        import shutil
        free = shutil.disk_usage(DATA_DIR).free / 1e9
        add("Disk", "ok" if free > 2 else "warn", f"{free:.1f} GB free")
    except OSError:
        pass
    worst = "fail" if any(c["state"] == "fail" for c in comps) else "warn" if any(c["state"] == "warn" for c in comps) else "ok"
    return {"at": now, "state": worst, "components": comps}


@app.get("/api/health")
def get_health():
    return system_health()


@app.get("/api/audit")
def audit_log(limit: int = 100):
    return db.q("SELECT * FROM audit_log ORDER BY at DESC LIMIT ?", (min(limit, 1000),))


@app.get("/api/users")
def users_list():
    return [{"user": n, "role": u["role"], "changed_at": u.get("changed_at")} for n, u in auth.load_users().items()]


@app.get("/entrance")
def entrance_page():
    return FileResponse(os.path.join(ROOT, "dashboard", "entrance.html"))


@app.get("/hardware")
def hardware_page():
    return FileResponse(os.path.join(ROOT, "dashboard", "hardware.html"))


app.mount("/snapshots", StaticFiles(directory=SNAP_DIR), name="snapshots")
app.mount("/clips", StaticFiles(directory=CLIP_DIR), name="clips")
app.mount("/acceptance_photos", StaticFiles(directory=ACCEPT_DIR), name="acceptance_photos")
app.mount("/workers", StaticFiles(directory=WORKER_DIR), name="workers")
app.mount("/static", StaticFiles(directory=os.path.join(ROOT, "dashboard")), name="static")
