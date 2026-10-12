"""NETRAX backend (F09/F10/F15/F16). Run from the project root:
    python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
Dashboard: http://127.0.0.1:8000
"""
import asyncio
import base64
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime

import cv2
import numpy as np
import yaml
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import actuators, db, notify, reports

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("SENTINELEYE_DATA_DIR") or os.path.join(ROOT, "data")   # override: tests only
SNAP_DIR = os.path.join(DATA_DIR, "backend_snapshots")
WORKER_DIR = os.path.join(DATA_DIR, "workers")              # registration photos (consented)
os.makedirs(SNAP_DIR, exist_ok=True)
os.makedirs(WORKER_DIR, exist_ok=True)
# SENTINELEYE_DB / SENTINELEYE_MUTE=1 let tests use a scratch database and stay silent
db.init(os.environ.get("SENTINELEYE_DB") or os.path.join(ROOT, "data", "sentineleye.db"))


def load_cfg(name):
    with open(os.path.join(ROOT, "config", name), encoding="utf-8") as f:
        return yaml.safe_load(f)


ZONES = load_cfg("zones.yaml")["zones"]
from edge.config import load_cameras                      # honours enabled: false + test override
CAMERAS = load_cameras()
SETTINGS = load_cfg("settings.yaml")
RESP = SETTINGS.get("response") or {}
SPK = dict(RESP.get("speaker") or {})
ALM = dict(RESP.get("alarm") or {})
if os.environ.get("SENTINELEYE_MUTE"):
    SPK["local"] = ALM["local"] = False
EDGE = {"state": None, "at": 0.0}
FRAMES = {}          # camera_id -> (annotated jpeg, time)   live tiles
RAW = {}             # camera_id -> (clean jpeg, time)       registration photos only
CLIENTS = set()

# Autonomous response on this laptop: speaker calls the worker, siren for fire / smoke
SPEAKER = actuators.Speaker(SPK)
ALARM = actuators.Alarm(ALM)
ACCESS_ALIAS = {}    # camera 'unauthorized_entry' id -> the RFID alert for the same person (merged)
CALLS = {}           # ppe event_id -> {"n": announcements made, "last": time, "items": (items, worker)}
SPOKEN = {}          # (camera, person, type) -> last time the speaker called that person (one call per quiet_s)
from . import hw_api  # noqa: E402  ESP32 safety station(s): telemetry, fire cross-check, RFID attendance


async def broadcast(kind, data):
    msg = json.dumps({"kind": kind, "data": data}, default=str)
    for ws in list(CLIENTS):
        try:
            await ws.send_text(msg)
        except Exception:
            CLIENTS.discard(ws)


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
    """Privacy: delete alert snapshots older than privacy.snapshot_retention_days (hourly)."""
    from edge.retention import purge
    days = (SETTINGS.get("privacy") or {}).get("snapshot_retention_days", 30)
    while True:
        for eid in await asyncio.to_thread(purge, SNAP_DIR, days):
            db.x("UPDATE events SET snapshot_path=NULL WHERE event_id=?", (eid,))
        await asyncio.sleep(3600)


async def speaker_loop():
    """Repeat the call to a worker who is still not wearing their PPE."""
    while True:
        await asyncio.sleep(1.0)
        try:
            await repeat_calls(time.time())
        except Exception as ex:           # a timer bug must never stop the loop
            print(f"[speaker] loop error: {ex!r}")


@asynccontextmanager
async def lifespan(app):
    hw_api.setup(broadcast=broadcast, respond=respond, notify_async=notify_async, announce=announce,
                 ALARM=ALARM, ZONES=ZONES, list_workers=list_workers)
    tasks = [asyncio.create_task(escalation_loop()), asyncio.create_task(speaker_loop()),
             asyncio.create_task(retention_loop()), asyncio.create_task(hw_api.loop())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="NETRAX API", lifespan=lifespan)
app.include_router(hw_api.router)


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
        e = db.get_event(eid)
        if e["type"] in ("fire", "smoke") and action != "resolve":
            e = await hw_api.verify_new(e, now)      # wait for / ask the safety station before any siren
        await respond(e, action, now)
        await broadcast("event", e)
        if action != "resolve" and e["status"] in ("open", "escalated"):
            await notify_async(e)
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
        db.x("""UPDATE events SET items_missing=?, severity=?, track_id=?, confidence=?, bbox=?,
                snapshot_path=COALESCE(?, snapshot_path), growth=?, updated_at=?,
                worker_id=COALESCE(?, worker_id), worker_name=COALESCE(?, worker_name) WHERE event_id=?""",
             (json.dumps(p.get("items_missing") or []), p.get("severity"), p.get("track_id"),
              p.get("confidence"), json.dumps(p.get("bbox")), snap, p.get("growth"), now,
              p.get("worker_id"), p.get("worker_name"), eid))
        e = db.get_event(eid)
        await respond(e, action, now)
        await broadcast("event", e)
        if e["severity"] == "Critical" and not was_critical:
            await notify_async(e, "severity raised to CRITICAL")
    return {"ok": True}


# ---------- autonomous response: speaker + siren ----------
async def notify_async(e, reason="new alert", recipient=None):
    n = await asyncio.to_thread(notify.send, e, reason, recipient)   # Telegram must not block the loop
    if n:
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
    return f"Attention {who}. Please wear your {_words(items)} now." if items else ""


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
        announce(f"Fire alarm. {e['type'].title()} detected in the {zone}. Evacuate the {zone} now.",
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
            await start_alarm(e, f"{e['type']} detected on {e.get('camera_id')}")
        return
    if e["type"] in ("ppe_missing", "unauthorized_entry"):
        if action == "resolve":
            CALLS.pop(eid, None)
            return
        # speak again ONLY when the decided set of missing items changes - a name arriving later, a tracker
        # handover or a new snapshot is not a new alarm (12 Oct: "one alarm, not continuous")
        key = tuple(e["items_missing"])
        c = CALLS.get(eid)
        if e["status"] in ("open", "escalated") and (c is None or c["items"] != key):
            kind = "access" if e["type"] == "unauthorized_entry" else "ppe"
            # one call per person per camera per quiet_s: an item confirmed a moment later (goggles need the
            # face) or a tracker slip that opens a second record for the same person updates the dashboard
            # silently; the one reminder later says the full list
            sk = _speech_key(e)
            if now - SPOKEN.get(sk, -1e9) < SPK.get("same_person_quiet_s", 20):
                db.add_action(eid, "voice_merged", "system", "same person was called less than "
                              f"{SPK.get('same_person_quiet_s', 20)} s ago - dashboard updated, no extra call")
                CALLS[eid] = {"n": 1, "last": c["last"] if c else now, "items": key}
            else:
                announce(alert_text(e), e.get("zone_id"), eid, kind)
                SPOKEN[sk] = now
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
        if now - SPOKEN.get(sk, -1e9) < every:       # another record of the same person was just called
            continue
        announce(alert_text(e), e.get("zone_id"), eid,
                 "access_repeat" if e["type"] == "unauthorized_entry" else "ppe_repeat")
        SPOKEN[sk] = now


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


@app.get("/api/workers")
def list_workers():
    return [_worker_out(r) for r in db.q("SELECT * FROM workers ORDER BY worker_id")]


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
    FRAMES[camera_id] = (await request.body(), time.time())
    return {"ok": True}


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
            "infer_ms": h.get("infer_ms"), "share": cam.get("share"),
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


async def _act(event_id, action, status, body):
    e = db.get_event(event_id)
    if e is None:
        raise HTTPException(404, "event not found")
    user = (body or {}).get("by_user") or "supervisor"
    db.x("UPDATE events SET status=?, updated_at=? WHERE event_id=?", (status, time.time(), event_id))
    db.add_action(event_id, action, user, (body or {}).get("note"))
    if action in ("ack", "dismiss"):
        CALLS.pop(event_id, None)                     # supervisor took over: stop repeat calls
        await stop_alarm(event_id, f"{action} by {user}")
    e = db.get_event(event_id)
    await broadcast("event", e)
    if action == "escalate":
        await notify_async(e, "ESCALATED by supervisor", "Plant safety manager")
    return e


@app.post("/api/events/{event_id}/ack")
async def ack(event_id: str, body: dict = Body(default=None)):
    return await _act(event_id, "ack", "acknowledged", body)


@app.post("/api/events/{event_id}/escalate")
async def escalate(event_id: str, body: dict = Body(default=None)):
    return await _act(event_id, "escalate", "escalated", body)


@app.post("/api/events/{event_id}/dismiss")
async def dismiss(event_id: str, body: dict = Body(default=None)):
    return await _act(event_id, "dismiss", "dismissed", body)


@app.get("/api/notifications")
def notifications(limit: int = 20):
    return db.q("SELECT * FROM notifications ORDER BY at DESC LIMIT ?", (limit,))


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
            jpg, t = FRAMES.get(camera_id, (None, 0))
            if jpg and t != last:
                last = t
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
            await asyncio.sleep(0.1)
    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/api/frame/{camera_id}")
def latest_frame(camera_id: str):
    jpg, t = FRAMES.get(camera_id, (None, 0))
    if not jpg or time.time() - t > 10:
        return Response(status_code=204)
    return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.websocket("/ws/alerts")
async def ws_alerts(ws: WebSocket):
    await ws.accept()
    CLIENTS.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        CLIENTS.discard(ws)
    except Exception:
        CLIENTS.discard(ws)


@app.get("/")
def index():
    return FileResponse(os.path.join(ROOT, "dashboard", "index.html"))


@app.get("/hardware")
def hardware_page():
    return FileResponse(os.path.join(ROOT, "dashboard", "hardware.html"))


app.mount("/snapshots", StaticFiles(directory=SNAP_DIR), name="snapshots")
app.mount("/workers", StaticFiles(directory=WORKER_DIR), name="workers")
app.mount("/static", StaticFiles(directory=os.path.join(ROOT, "dashboard")), name="static")
