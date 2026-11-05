"""Final-round acceptance tests (requirements brief section 11, tests 01-14) + login / roles + shared engine.
No cameras, GPU, internet or provider accounts needed: providers are replaced by fakes that answer like
Telegram / Twilio / SMTP do, so delivery, retries and failures are tested for real.
    python -m tools.final_selftest          -> must end with ALL FINAL TESTS PASSED
Writes docs/eval/final_selftest.md (the table for the acceptance checklist)."""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time

TMP = tempfile.mkdtemp()
os.environ.setdefault("SENTINELEYE_MUTE", "1")
os.environ["SENTINELEYE_DB"] = os.path.join(TMP, "final_test.db")
os.environ["SENTINELEYE_DATA_DIR"] = TMP
os.environ["SENTINELEYE_AUTH"] = "0"           # HTTP auth is switched on per test below
os.environ["NETRAX_CHANNELS"] = "telegram,call,sms,email"   # test every channel even if the site disabled some
os.environ["SENTINELEYE_SECRETS"] = os.path.join(TMP, "no_secrets.yaml")   # never the real keys of this laptop
for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM",
          "ALERT_PHONE_TO", "SMTP_USER", "SMTP_PASSWORD", "ALERT_EMAIL_TO"):
    os.environ.pop(k, None)

import backend.main as m                       # noqa: E402
from backend import auth, assistant, db, fire_levels, location, notify, perf, timeutil   # noqa: E402
from edge.engine import InferenceEngine        # noqa: E402
from edge.entrance import EntranceChecker      # noqa: E402

RESULTS, ok_all = [], True


def check(tid, name, cond, evidence=""):
    global ok_all
    ok_all &= bool(cond)
    RESULTS.append((tid, name, "PASS" if cond else "FAIL", evidence))
    print(("PASS " if cond else "FAIL ") + f"[{tid}] {name}" + (f"  ({evidence})" if evidence else ""))


# ------------------------------------------------------------------ fake providers
class FakeResp:
    def __init__(self, code, body):
        self.status_code, self._b = code, body
        self.text = json.dumps(body)

    def json(self):
        return self._b


class FakeNet:
    """Answers like api.telegram.org / api.twilio.com. Behaviour per channel is switchable."""
    def __init__(self):
        self.mode = {"telegram": "ok", "call": "completed", "sms": "delivered"}
        self.calls, self.fail_first = [], {}
        self.lock = threading.Lock()

    def _flaky(self, key):
        with self.lock:
            n = self.fail_first.get(key, 0)
            if n > 0:
                self.fail_first[key] = n - 1
                return True
        return False

    def post(self, url, json=None, data=None, files=None, auth=None, timeout=None):
        self.calls.append(("POST", url, json or data))
        if "api.telegram.org" in url:
            if self.mode["telegram"] == "down" or self._flaky("telegram"):
                import requests
                raise requests.ConnectionError("no internet")
            if self.mode["telegram"] == "badtoken":
                return FakeResp(401, {"ok": False, "description": "Unauthorized"})
            return FakeResp(200, {"ok": True, "result": {"message_id": 42}})
        if self.mode.get("twilio_trial") and ("Calls.json" in url or "Messages.json" in url):
            d = data or {}
            if "Twiml" in d or "From" in d or (d.get("Body") and not str(d["Body"]).startswith("sms_")):
                return FakeResp(400, {"message": "Invalid or disallowed parameters provided - trial accounts have "
                                                 "limited parameter access"})
        if "Calls.json" in url:
            return FakeResp(201, {"sid": "CA123", "status": "queued"})
        if "Messages.json" in url:
            if self._flaky("sms"):
                return FakeResp(503, {"message": "Service Unavailable"})
            return FakeResp(201, {"sid": "SM123", "status": "queued"})
        return FakeResp(404, {})

    def get(self, url, auth=None, timeout=None):
        if "/Calls/" in url:
            return FakeResp(200, {"sid": "CA123", "status": self.mode["call"]})
        if "/Messages/" in url:
            return FakeResp(200, {"sid": "SM123", "status": self.mode["sms"]})
        return FakeResp(404, {})


NET = FakeNet()
import requests as _real_requests              # noqa: E402


class _ReqShim:
    RequestException = _real_requests.RequestException
    ConnectionError = _real_requests.ConnectionError
    post = staticmethod(NET.post)
    get = staticmethod(NET.get)


notify.requests = _ReqShim
SMTP_SENT = []


def fake_email(sec, subject, body, photo=None):
    if not (sec["email"]["username"] and sec["email"]["app_password"] and sec["email"]["to"]):
        return notify.Result("not_configured", error="SMTP not set")
    if NET._flaky("email"):
        return notify.Result("failed", error="SMTPServerDisconnected", recoverable=True)
    SMTP_SENT.append((subject, body))
    return notify.Result("sent", provider_status="accepted by mail server")


notify.send_email = fake_email


def set_creds(on=True):
    vals = {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "-100200", "TWILIO_ACCOUNT_SID": "AC1",
            "TWILIO_AUTH_TOKEN": "tok", "TWILIO_FROM": "+15550001", "ALERT_PHONE_TO": "+919800000000",
            "SMTP_USER": "netrax@example.com", "SMTP_PASSWORD": "app-pass", "ALERT_EMAIL_TO": "safety@example.com"}
    for k, v in vals.items():
        if on:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)


def wait_notifs(eid, n_final, timeout=20):
    t = time.time()
    while time.time() - t < timeout:
        rows = db.q("SELECT * FROM notifications WHERE event_id=?", (eid,))
        if rows and sum(1 for r in rows if r["status"] in notify.FINAL) >= n_final:
            return rows
        time.sleep(0.2)
    return db.q("SELECT * FROM notifications WHERE event_id=?", (eid,))


# ------------------------------------------------------------------ helpers
def persons(*boxes):
    return [{"tid": i + 1, "bbox": b, "conf": 0.9} for i, b in enumerate(boxes)]


def run_entrance(statuses_per_frame, n=10, box=(100, 40, 300, 700), h=720, dt=0.15, crowd=False):
    ec = EntranceChecker("cam_04", "main_entrance", ["helmet", "vest", "boots"], {})
    t0, out = 1000.0, []
    for i in range(n):
        ps = persons(box) + (persons(box, (120, 50, 310, 690))[1:] if crowd else [])
        st = {1: statuses_per_frame(i)}
        out += ec.update(ps, st, 1280, h, t0 + i * dt)
        if out:
            break
    return out


def ppe_payload(eid, items, sev="High", cam="cam_01", zone="welding_bay", action="open", t=None):
    t = t or time.time()
    iso = timeutil.local(t).isoformat()
    return {"event_id": eid, "action": action, "type": "ppe_missing", "items_missing": items, "severity": sev,
            "camera_id": cam, "camera_name": cam, "zone_id": zone, "zone_name": m.ZONES[zone]["name"],
            "track_id": 7, "worker_id": "W-001", "worker_name": "Chinmaya", "confidence": 0.88, "bbox": [10, 10, 200, 600],
            "captured_at": iso, "started_at": iso, "onset_at": iso, "frame_w": 1280, "frame_h": 720}


def fire_payload(eid, bbox, growth=1.0, action="open", typ="fire", cam="cam_02", zone="warehouse", t=None):
    t = t or time.time()
    iso = timeutil.local(t).isoformat()
    return {"event_id": eid, "action": action, "type": typ, "items_missing": [], "severity": "Critical",
            "camera_id": cam, "camera_name": cam, "zone_id": zone, "zone_name": m.ZONES[zone]["name"], "track_id": None,
            "confidence": 0.8, "bbox": bbox, "growth": growth, "captured_at": iso, "started_at": iso, "onset_at": iso,
            "frame_w": 1280, "frame_h": 720}


def pipeline_test():
    import types
    import numpy as np
    if "ultralytics" not in sys.modules:
        try:
            import ultralytics  # noqa: F401
        except Exception:                          # noqa: BLE001 - CI box without torch: stub the import only
            sys.modules["ultralytics"] = types.SimpleNamespace(YOLO=None)
    from edge.config import load_all
    from edge.worker import CameraWorker
    settings, zones, cameras = load_all()

    class Det:
        device, runtime, has_parts, last_parts = "cpu", "pytorch", False, []
        supported = {"helmet", "vest", "gloves", "boots", "mask", "goggles"}

        def __init__(self):
            self.helmet = True

        def detect_batch(self, frames, flags):
            out = []
            for f, fl in zip(frames, flags):
                p = {"cls": "person", "conf": 0.92, "bbox": [500.0, 60.0, 780.0, 680.0], "kps": None}
                items = [{"cls": "vest", "conf": 0.9, "bbox": [520.0, 200.0, 760.0, 400.0]},
                         {"cls": "boots", "conf": 0.8, "bbox": [560.0, 640.0, 630.0, 680.0]},
                         {"cls": "boots", "conf": 0.8, "bbox": [650.0, 640.0, 720.0, 680.0]},
                         {"cls": "gloves", "conf": 0.8, "bbox": [500.0, 380.0, 560.0, 440.0]}]
                if self.helmet:
                    items.append({"cls": "helmet", "conf": 0.9, "bbox": [580.0, 50.0, 700.0, 130.0]})
                out.append({"persons": [p], "items": items, "parts": [], "fire": [] if fl else None,
                            "timings": {"total_ms": 20.0}})
            return out

    class Box:
        def __init__(self):
            self.items = []

        def put(self, payload):
            self.items.append(payload)

    class Pub:
        def __init__(self):
            self.frames = 0

        def due(self, cam):
            return True

        def frame(self, cam, img, ts=None):
            self.frames += 1

        def raw(self, cam, img):
            pass

    class Reader:
        target_fps = 12

    det = Det()
    eng = InferenceEngine(det, max_batch=4, gather_ms=2)
    eng.start()
    frame = np.zeros((720, 1280, 3), np.uint8)
    frame[200:400, 520:760] = (0, 255, 200)               # lime hi-vis vest pixels
    box, pub = Box(), Pub()
    s2 = json.loads(json.dumps(settings))
    s2["snapshots_dir"] = os.path.join(TMP, "snaps")
    s2.setdefault("identity", {})["enabled"] = False
    w_ent = CameraWorker("cam_04", dict(cameras["cam_04"], role="entrance"), zones, s2, Reader(), box, pub, None,
                         engine=eng)
    t0 = time.time()
    while time.time() - t0 < 1.6:
        w_ent.process(frame, time.time(), 1)
        time.sleep(0.1)
    ent = [p for p in box.items if p["type"] == "entrance_check"]
    check("01", "edge pipeline: entrance camera -> ONE 'approved' decision with full PPE (frame size attached)",
          len(ent) == 1 and ent[0]["result"] == "approved" and ent[0]["frame_w"] == 1280
          and (pub.frames > 5 or (w_ent.live_view and w_ent.overlay and w_ent.overlay["persons"])),
          ent and f"{ent[0]['result']} {ent[0]['decision_ms']} ms, {ent[0]['frames']} frames, ok {ent[0]['items_ok']} "
          f"missing {ent[0]['items_missing']} unclear {ent[0]['items_uncertain']} {ent[0]['evidence']}")
    det.helmet = False
    box.items.clear()
    w_mon = CameraWorker("cam_01", cameras["cam_01"], zones, s2, Reader(), box, pub, None, engine=eng)
    t0 = time.time()
    while time.time() - t0 < 3.5 and not box.items:
        w_mon.process(frame, time.time(), 2)
        time.sleep(0.08)
    ppe = [p for p in box.items if p["type"] == "ppe_missing"]
    check("04", "edge pipeline: monitored zone, helmet off -> one PPE alert naming the helmet",
          ppe and "helmet" in ppe[0]["items_missing"] and ppe[0]["camera_id"] == "cam_01",
          ppe and f"items {ppe[0]['items_missing']} after {ppe[0].get('time_to_alert_ms') or '?'}")
    check("12", "edge pipeline: per-camera latency samples recorded (capture -> processed)",
          len(w_mon.frame_ms) > 5 and w_mon.infer_ms and w_mon.infer_ms[-1] == 20.0)
    st = w_mon.stage_stats()
    check("LIVE", "profiler: every AI stage of a camera is timed (engine, tracker, face ID, PPE match, decisions, drawing)",
          all(k in st for k in ("ai_engine", "tracker", "face_id", "ppe_match", "decisions_fire", "draw_emit")),
          ", ".join(f"{k} {v['p50']} ms" for k, v in st.items()))
    live_test(w_mon, frame)


def live_test(worker, frame):
    """4 Nov: the live picture must stay smooth while the AI is slow (it used to hang at the AI rate)."""
    from edge.live import LiveView

    class Reader:
        def __init__(self):
            self.no = 0
            self.lock = threading.Lock()

        def latest(self):
            with self.lock:
                self.no += 1                    # a new camera frame every call (camera at full speed)
                return frame, time.time(), self.no

    class Pub:
        def __init__(self):
            self.n, self.sizes = 0, set()

        def frame(self, cam, img, ts=None):
            self.n += 1
            self.sizes.add(img.shape[1])

    class Clips:
        def __init__(self):
            self.n = 0

        def due(self, cam, ts):
            return True

        def add(self, cam, img, ts, persons=()):
            self.n += 1

    worker.overlay = dict(worker.overlay, at=time.time() + 60)     # an AI result that stays fresh during the test
    worker.proc_times.clear()
    worker.proc_times.extend([time.time() - 2, time.time() - 1])  # AI at ~1 frame per second
    pub, clips = Pub(), Clips()
    lv = LiveView({"cam_01": Reader()}, {"cam_01": worker}, pub, clips, fps=12, width=640)
    lv.start()
    time.sleep(2.0)
    rate = pub.n / 2.0
    check("LIVE", "AI at ~1 fps, live picture still ~12 fps (video no longer hangs at the AI rate)",
          rate >= 9 and pub.sizes == {640}, f"{rate:.1f} pictures/s at {sorted(pub.sizes)} px, draw "
          f"{sorted(lv.ms)[len(lv.ms) // 2]:.1f} ms")
    check("LIVE", "incident clips are fed from the smooth live picture", clips.n >= 9)
    # face detection on a scaled copy: boxes + landmarks come back in full-frame pixels
    import numpy as np
    from edge.identity import FaceEngine

    class Det:
        def setInputSize(self, wh):
            self.wh = wh

        def detect(self, img):
            h, w = img.shape[:2]
            row = np.array([w * .5, h * .25, w * .1, h * .1] + [w * .55, h * .3] * 5 + [0.95], np.float32)
            return 1, row[None, :]

    fe = FaceEngine.__new__(FaceEngine)
    fe.det, fe.lock, fe.max_side, fe.ms = Det(), threading.Lock(), 960, []
    big = np.zeros((1920, 1080, 3), np.uint8)
    f = fe.faces(big)[0]
    check("LIVE", "face detection runs on a <= 960 px copy (phone frames are 1920 px) and boxes map back exactly",
          max(fe.det.wh) == 960 and abs(f[0] - 540) < 2 and abs(f[1] - 480) < 2 and abs(f[2] - 108) < 2
          and abs(f[4] - 594) < 2 and abs(f[14] - 0.95) < 1e-6, f"detector input {fe.det.wh}, face x={f[0]:.0f} y={f[1]:.0f}")


async def main():
    m.hw_api.setup(broadcast=m.broadcast, respond=m.respond, notify_async=m.notify_async, announce=m.announce,
                   ALARM=m.ALARM, ZONES=m.ZONES, list_workers=m.list_workers)
    m.LOOP["loop"] = asyncio.get_running_loop()
    notify.DISPATCHER.start()
    # ---------------- 01-03 entrance
    out = run_entrance(lambda i: {"helmet": ("compliant", .9), "vest": ("compliant", .9), "boots": ("compliant", .8)})
    check("01", "complete PPE -> ACCESS APPROVED after >= 6 frames / 1 s (never on one frame)",
          out and out[0]["result"] == "approved" and out[0]["frames"] >= 6, out and f"{out[0]['frames']} frames, {out[0]['decision_ms']} ms")
    out[0].update(event_id="ent_t01", captured_at=timeutil.local(time.time()).isoformat(),
                  started_at=timeutil.local(time.time()).isoformat(), camera_name="Main entrance", zone_name="Main entrance")
    await m.ingest_event(out[0])
    rec = db.q("SELECT * FROM entrance_checks WHERE check_id='ent_t01'")
    ev = db.get_event("ent_t01")
    check("01", "approval recorded (entrance_checks + event history, gate stays closed without controller)",
          rec and rec[0]["result"] == "approved" and ev and ev["status"] == "closed" and ev["details"]["gate"] == "closed")
    out = run_entrance(lambda i: {"helmet": ("compliant", .9), "vest": ("missing", .9), "boots": ("compliant", .8)})
    check("02", "vest missing -> ACCESS DENIED naming the missing item",
          out and out[0]["result"] == "denied" and out[0]["items_missing"] == ["vest"])
    out[0].update(event_id="ent_t02", captured_at=timeutil.local(time.time()).isoformat(),
                  started_at=timeutil.local(time.time()).isoformat())
    await m.ingest_event(out[0])
    ev = db.get_event("ent_t02")
    check("02", "denial recorded with required items, evidence and missing item",
          ev["status"] == "open" and ev["items_missing"] == ["vest"] and "helmet" in ev["details"]["required"]
          and ev["details"]["evidence"]["vest"]["missing"] >= 3)
    out = run_entrance(lambda i: {"helmet": ("compliant", .9), "vest": ("compliant", .9), "boots": ("unverifiable", .5)}, n=40)
    check("03", "feet not visible -> 'Verification failed - manual check required', no approval",
          out and out[0]["result"] == "manual" and "boots" in out[0]["items_uncertain"])
    out = run_entrance(lambda i: {"helmet": ("compliant", .9), "vest": ("compliant", .9), "boots": ("compliant", .9)},
                       n=40, crowd=True)
    check("03", "two people overlapping in front of the camera -> manual check, never auto-approved",
          out and out[0]["result"] == "manual")
    flip = run_entrance(lambda i: {"helmet": ("compliant" if i % 2 else "missing", .6), "vest": ("compliant", .9),
                                   "boots": ("compliant", .9)}, n=40)
    check("03", "flickering helmet evidence (50/50) -> manual check, not approved",
          flip and flip[0]["result"] == "manual")
    out[0].update(event_id="ent_t03", captured_at=timeutil.local(time.time()).isoformat(),
                  started_at=timeutil.local(time.time()).isoformat())
    await m.ingest_event(out[0])
    # ---------------- 04 serious violation, 11 multiple
    set_creds(True)
    await m.ingest_event(ppe_payload("evt_t04a", ["helmet", "vest"], "High", "cam_01", "welding_bay"))
    await m.ingest_event(ppe_payload("evt_t04b", ["gloves"], "High", "cam_03", "assembly_line"))
    a, b = db.get_event("evt_t04a"), db.get_event("evt_t04b")
    check("04", "serious PPE violation stored open with items, camera, zone, time, worker",
          a["status"] == "open" and a["items_missing"] == ["helmet", "vest"] and a["camera_id"] == "cam_01"
          and a["zone_id"] == "welding_bay" and a["started_at"] and a["worker_id"] == "W-001")
    check("04", "dashboard-wide state rule: High/Critical + open -> emergency severities from settings",
          a["severity"] in m.EMERGENCY_SEV)
    check("11", "two simultaneous violations kept as two separate active incidents",
          a["event_id"] != b["event_id"] and b["status"] == "open" and db.get_event("evt_t04a")["status"] == "open")
    await m._act("evt_t04a", "ack", "acknowledged", {"by_user": "sup1"})
    e = db.get_event("evt_t04a")
    check("04", "acknowledged != resolved: ack keeps the incident (status acknowledged, acked_by recorded)",
          e["status"] == "acknowledged" and e["acked_by"] == "sup1" and e["ended_at"] is None)
    await m._act("evt_t04a", "resolve", "resolved", {"by_user": "sup1"})
    e = db.get_event("evt_t04a")
    check("04", "resolve closes it with who/when (traceable)", e["status"] == "resolved" and e["closed_by"] == "sup1" and e["ended_at"])
    await m.ingest_event(ppe_payload("evt_t04b", ["gloves", "goggles"], "High", "cam_03", "assembly_line", action="update"))
    check("11", "an update changes only its own incident", db.get_event("evt_t04b")["items_missing"] == ["gloves", "goggles"]
          and db.get_event("evt_t04a")["status"] == "resolved")
    # ---------------- 05/06 fire panel data + levels
    await m.ingest_event(fire_payload("evt_t05", [600, 300, 640, 340]))          # 0.17 % of the picture
    f = db.get_event("evt_t05")
    check("05", "fire event has type, timestamp, camera, zone, status, level for the big panel",
          f["type"] == "fire" and f["started_at"] and f["camera_id"] == "cam_02" and f["zone_id"] == "warehouse"
          and f["status"] == "open" and f["fire_level"] == 1, f"level {f['fire_level']}, status {f['status']}")
    check("06", "small flame, no other evidence -> Level 1 WARNING (severity High, voice warning, no siren)",
          f["fire_level"] == 1 and f["severity"] == "High" and not m.ALARM.state())
    await m.ingest_event(fire_payload("evt_t05", [560, 260, 760, 460], growth=1.4, action="update"))
    f = db.get_event("evt_t05")
    check("06", "flame grows (x1.4, 4 % of picture) -> raised to Level 2 CRITICAL + siren", f["fire_level"] == 2
          and f["severity"] == "Critical" and m.ALARM.state() is not None, "; ".join(f["fire_reasons"] or []))
    await m.ingest_event(fire_payload("evt_t05", [580, 280, 600, 300], growth=1.0, action="update"))
    check("06", "flame looks smaller in a later frame -> level is NOT downgraded", db.get_event("evt_t05")["fire_level"] == 2)
    await m.ingest_event(fire_payload("evt_t05", [200, 100, 900, 650], growth=2.3, action="update"))
    f = db.get_event("evt_t05")
    check("06", "large / doubling fire -> Level 3 VERY CRITICAL (evacuate)", f["fire_level"] == 3)
    lv, why = fire_levels.classify({"type": "fire", "bbox": [600, 300, 620, 320], "frame_w": 1280, "frame_h": 720,
                                    "sensors": json.dumps({"level": "CRITICAL", "reasons": ["flame sensor active",
                                                           "temperature 64.0 C >= 50"], "data": {"flame_detected": 1,
                                                           "temperature_c": 64.0}}), "status": "open"})
    check("06", "tiny visible flame but station flame + 64 C -> Level 3 (danger evidence beats flame size)", lv == 3,
          "; ".join(why))
    rows = wait_notifs("evt_t05", 4)
    kinds = {r["kind"] for r in rows}
    check("06", "each level raise re-notifies immediately (no cooldown for fire)",
          any("LEVEL RAISED to 2" in k for k in kinds) and any("LEVEL RAISED to 3" in k for k in kinds))
    # ---------------- 07 Telegram
    tg = [r for r in rows if r["channel"] == "telegram"]
    check("07", "Telegram message reaches the (fake) group and the outcome is logged as delivered",
          tg and all(r["status"] == "delivered" for r in tg) and tg[0]["provider_id"] == "42")
    body = tg[0]["message"]
    check("07", "message has incident type, level, zone, camera, time, action and event ID",
          all(x in body for x in ("fire", "Level 1", "Warehouse", "cam_02", "IST", "Action:", "evt_t05")))
    # ---------------- 08 phone call
    rows = wait_notifs("evt_t05", 8, timeout=25)
    calls = [r for r in rows if r["channel"] == "call"]
    check("08", "phone call placed via Twilio, followed until Twilio reports 'completed' -> delivered",
          calls and any(r["status"] == "delivered" and r["provider_status"] == "completed" for r in calls),
          ", ".join(f"{r['status']}/{r['provider_status']}" for r in calls))
    # ---------------- 08b Twilio TRIAL account: custom text refused -> Twilio template call / SMS (still rings)
    NET.mode["twilio_trial"] = True
    sec = notify.load_secrets()
    rc = notify.send_call(sec, "+919800000000", "test", {})
    rs = notify.send_sms(sec, "+919800000000", "test")
    check("08", "Twilio TRIAL account (custom text refused) -> falls back to Twilio's template call + SMS, labelled TRIAL",
          rc.status == "sending" and "TRIAL" in rc.provider_status and rs.status == "sending" and "TRIAL" in rs.provider_status,
          f"{rc.provider_status} / {rs.provider_status}")
    check("08", "phone numbers typed without '+' are fixed (916372863454 -> +916372863454)",
          notify._e164(916372863454) == "+916372863454")
    NET.mode["twilio_trial"] = False
    notify._TRIAL["on"] = None
    # ---------------- 09 SMS + email, retry
    NET.fail_first["sms"] = 1
    NET.fail_first["email"] = 1
    await m.ingest_event(ppe_payload("evt_t09", ["helmet"], "Critical", "cam_01", "welding_bay"))
    rows = wait_notifs("evt_t09", 4, timeout=40)
    sms = [r for r in rows if r["channel"] == "sms"]
    em = [r for r in rows if r["channel"] == "email"]
    check("09", "SMS: first try fails (HTTP 503) -> retried -> delivered, attempts logged",
          sms and sms[0]["status"] == "delivered" and sms[0]["attempts"] >= 2, sms and f"attempts {sms[0]['attempts']}")
    check("09", "SMS text has type, severity, location, time and recommended action",
          sms and all(x in sms[0]["message"] for x in ("Critical", "Welding bay", "IST", "wear")))
    check("09", "e-mail: SMTP drop -> retried -> accepted by the mail server ('sent', never claimed 'delivered')",
          em and em[0]["status"] == "sent" and em[0]["attempts"] >= 2 and SMTP_SENT)
    # ---------------- 13 provider / network failure
    NET.mode["telegram"] = "down"
    t0 = time.time()
    await m.ingest_event(ppe_payload("evt_t13", ["vest"], "High", "cam_01", "welding_bay"))
    ingest_ms = (time.time() - t0) * 1000
    check("13", "internet down: the alert is stored + shown at once (sending never blocks detection)",
          db.get_event("evt_t13")["status"] == "open" and ingest_ms < 500, f"ingest {ingest_ms:.0f} ms")
    rows = wait_notifs("evt_t13", 1, timeout=40)
    tg = [r for r in rows if r["channel"] == "telegram"]
    check("13", "Telegram failure retried (bounded) then shown as FAILED with the reason - never 'delivered'",
          tg and tg[0]["status"] == "failed" and tg[0]["attempts"] == 3 and "network" in (tg[0]["error"] or ""))
    h = m.system_health()
    check("13", "system health shows the failing channel", any(c["name"] == "Alerts: telegram" and c["state"] == "fail"
                                                              for c in h["components"]))
    NET.mode["telegram"] = "ok"
    set_creds(False)
    await m.ingest_event(ppe_payload("evt_t13b", ["vest"], "High", "cam_01", "welding_bay"))
    rows = wait_notifs("evt_t13b", 1)
    check("13", "no credentials -> NOT CONFIGURED (clearly labelled), nothing claimed as sent",
          rows and all(r["status"] == "not_configured" and not r["delivered"] for r in rows))
    check("13", "health lists unconfigured channels as warnings", any(c["name"].startswith("Alerts:") and c["state"] == "warn"
                                                                       for c in m.system_health()["components"]))
    set_creds(True)
    # dedupe: same incident, same reason -> no second message
    n_before = len(db.q("SELECT id FROM notifications WHERE event_id='evt_t09'"))
    await m.notify_async(db.get_event("evt_t09"))
    check("05", "same continuous incident is not sent twice on a channel",
          len(db.q("SELECT id FROM notifications WHERE event_id='evt_t09'")) == n_before)
    # ---------------- 10 AI assistant
    known = timeutil.local(time.time()).replace(hour=16, minute=2, second=0, microsecond=0)
    from datetime import timedelta
    yday = known - timedelta(days=1)
    db.x("INSERT INTO events (event_id, type, items_missing, severity, camera_id, zone_id, zone_name, started_at, received_at, "
         "status, worker_id, worker_name, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
         ("evt_known", "ppe_missing", '["helmet"]', "High", "cam_01", "welding_bay", "Welding bay", yday.timestamp(),
          yday.timestamp(), "resolved", "W-003", "Bibhu", yday.timestamp()))
    db.x("INSERT INTO events (event_id, type, items_missing, severity, camera_id, zone_id, zone_name, started_at, received_at, "
         "status, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
         ("evt_other", "smoke", "[]", "High", "cam_02", "warehouse", "Warehouse", (yday - timedelta(hours=5)).timestamp(),
          (yday - timedelta(hours=5)).timestamp(), "resolved", yday.timestamp()))
    workers = [{"worker_id": "W-003", "name": "Bibhu"}, {"worker_id": "W-001", "name": "Chinmaya"}]
    r = assistant.answer("What happened yesterday at 4:00 PM?", workers=workers, zones=m.ZONES, cameras=m.CAMERAS)
    ids = [c["event_id"] for c in r["records"]]
    check("10", "'What happened yesterday at 4:00 PM?' -> the saved record at 16:02 yesterday, nothing else",
          ids == ["evt_known"] and "Missing helmet" in r["details"][0], r["answer"])
    r2 = assistant.answer("what happened yesterday at 3 am", workers=workers, zones=m.ZONES, cameras=m.CAMERAS)
    check("10", "no record in the window -> says 'No matching record was found' (no invented events)",
          r2["count"] == 0 and r2["answer"].startswith("No matching record"))
    r3 = assistant.answer("any smoke yesterday", workers=workers, zones=m.ZONES, cameras=m.CAMERAS)
    check("10", "type filter (smoke) + whole day", [c["event_id"] for c in r3["records"]] == ["evt_other"])
    r4 = assistant.answer("did bibhu have violations yesterday", workers=workers, zones=m.ZONES, cameras=m.CAMERAS,
                          role="viewer")
    check("10", "viewer role: worker names hidden in the answer", r4["records"] and "Bibhu" not in json.dumps(r4["records"]))
    check("10", "every question logged", db.q("SELECT COUNT(*) AS n FROM assistant_log")[0]["n"] >= 4)
    # ---------------- 12 latency numbers exist
    s = perf.summary()
    check("12", "latency measured as median + 95th percentile (event e2e, time to alert, notification delivery)",
          "event_e2e_ms" in s and "p95" in s["event_e2e_ms"] and "notify_telegram_ms" in s, json.dumps(s.get("event_e2e_ms")))
    # ---------------- 14 location
    loc = location.current("welding_bay", "cam_01")
    site = (location.cfg().get("site") or {})
    if site.get("lat") is None:
        check("14", "no GPS + no configured site -> 'Location unavailable' + zone label (never invented)",
              not loc["valid"] and loc["note"] == "Location unavailable" and loc["zone_label"])
    ok, _ = location.record("device", 20.2961, 85.8245, 25, time.time())
    loc = location.current("welding_bay", "cam_01")
    check("14", "valid device GPS -> shown with source, accuracy and map link",
          loc["valid"] and loc["source"] == "device" and "maps.google" in loc["map_url"])
    location.record("device", 20.3, 85.8, 20, time.time() - 3600)
    db.x("DELETE FROM locations WHERE at > ?", (time.time() - 60,))
    loc = location.current("welding_bay", "cam_01")
    check("14", "1-hour-old reading -> marked STALE and not used", any("stale" in w for w in (loc.get("warnings") or []))
          and (not loc["valid"] or loc["source"] == "configured"))
    bad, _ = location.record("hardware", 0, 0, None, time.time())
    check("14", "0,0 / invalid coordinates rejected", bad is False)
    # ---------------- every Python file compiles (2 Nov: a damaged tool went unnoticed because no test imported it)
    import compileall
    check("BUILD", "backend, edge and tools all compile", all(compileall.compile_dir(d, quiet=2, force=False) for d in ("backend", "edge", "tools")))
    from tools import find_phones
    y = "cameras:\n  cam_02:\n    name: A\n    source: http://192.0.0.4:8080/video\n  cam_03:\n    name: B\n    enabled: false   # no phone\n    source: http://1.1.1.1:8080/video\n    rotate: 90\n"
    n3 = find_phones.set_source(y, "cam_03", "172.24.255.50")
    check("BUILD", "find_phones: sets the 2nd phone and switches its camera on, the first phone is untouched",
          "http://172.24.255.50:8080/video" in n3 and "enabled" not in n3 and "http://192.0.0.4:8080/video" in n3 and "rotate: 90" in n3)
    # ---------------- entrance: WHO + WHAT (guide's request, 2 Nov): face / RFID identity with the PPE check
    def look(ec, st, t0, n=8, dt=0.15, **who):
        acts = []
        for i in range(n):
            acts += ec.update([dict(persons((100, 40, 300, 700))[0], **who)], {1: st}, 1280, 720, t0 + i * dt)
        return acts
    full = {"helmet": ("compliant", .9), "vest": ("compliant", .9), "boots": ("compliant", .8)}
    novest = dict(full, vest=("missing", .9))
    ec = EntranceChecker("cam_04", "main_entrance", ["helmet", "vest", "boots"], {})
    a = look(ec, novest, 1000, n=200)                    # stands at the door without a vest for 30 s
    check("ENT", "denied person keeps standing there -> ONE decision (one popup / call / message), not one every 3 s",
          len(a) == 1 and a[0]["result"] == "denied", f"{len(a)} decisions in 30 s")
    a = look(ec, full, 1030, n=60)
    check("ENT", "... puts the vest on -> a NEW decision: approved", len(a) == 1 and a[0]["result"] == "approved")
    ec = EntranceChecker("cam_04", "main_entrance", ["helmet", "vest", "boots"], {})
    a = look(ec, full, 1000, n=8, identity=None)
    check("ENT", "face still being matched -> waits for the name instead of saying 'unregistered'", not a)
    a = look(ec, full, 1001.2, n=4, identity="worker", worker_id="W-077", worker_name="Chinmaya")
    check("ENT", "... registered face found -> decision carries W-077 Chinmaya",
          len(a) == 1 and a[0]["worker_id"] == "W-077" and a[0]["identity"] == "worker")
    ec = EntranceChecker("cam_04", "main_entrance", ["helmet", "vest", "boots"], {})
    a = look(ec, full, 1000, n=30, identity="visitor", worker_id="V-009", worker_name=None)
    check("ENT", "visitor id is not a worker -> identity 'unregistered'", a and a[0]["identity"] == "unregistered" and not a[0]["worker_id"])
    from backend import hw_api, hardware as hw
    hw_api.setup(broadcast=m.broadcast, respond=m.respond, notify_async=m.notify_async, announce=m.announce, ALARM=m.ALARM,
                 ZONES=m.ZONES, list_workers=m.list_workers, entrance_scan=m.entrance_scan)
    for wid, nm, uid in (("W-077", "Chinmaya", "AA000077"), ("W-078", "Bibhu", "AA000078")):
        db.x("INSERT OR REPLACE INTO workers (worker_id, name, embeddings, photos, created_at, badge_uid, active) VALUES (?,?,?,?,?,?,1)",
             (wid, nm, "[]", 0, 0, uid))
    hw_api.CFG.setdefault("attendance", {})["readers"] = {"esp32-rfid-01": "main_entrance"}

    async def decide(eid, res, wid=None, name=None, missing=(), track=500):
        await m.ingest_event({"event_id": eid, "action": "open", "type": "entrance_check", "result": res,
                              "items_missing": list(missing), "items_ok": [], "items_uncertain": [], "required": ["helmet", "vest", "boots"],
                              "severity": "Low", "camera_id": "cam_04", "zone_id": "main_entrance", "zone_name": "Main entrance",
                              "track_id": track, "worker_id": wid, "worker_name": name, "identity": "worker" if wid else "unregistered",
                              "confidence": .9, "bbox": [1, 2, 3, 4], "decision_ms": 1200, "gate": "closed",
                              "captured_at": timeutil.local(time.time()).isoformat(), "started_at": timeutil.local(time.time()).isoformat()})
        return db.get_event(eid), m.entrance_payload(db.get_event(eid))
    said = lambda eid: (db.q("SELECT text FROM announcements WHERE event_id=? ORDER BY id DESC LIMIT 1", (eid,)) or [{"text": ""}])[0]["text"]
    m.ENTRANCE_CARD.clear()
    e, g = await decide("ent_w1", "approved", "W-077", "Chinmaya")
    check("ENT", "registered + all PPE -> 'W-077 Chinmaya verified - all required PPE worn. You may enter.' (popup + voice)",
          g["verdict"].startswith("W-077 Chinmaya verified") and "You may enter" in g["verdict"] and g["result"] == "approved"
          and g["identity"]["by"] == "face" and "Chinmaya" in said("ent_w1") and "may enter" in said("ent_w1"), g["verdict"])
    e, g = await decide("ent_w2", "denied", "W-077", "Chinmaya", ["vest"])
    check("ENT", "registered + vest missing -> names the worker and the item, 'Entry not permitted'",
          "W-077 Chinmaya" in g["verdict"] and "missing" in g["verdict"] and "Entry not permitted" in g["verdict"], g["verdict"])
    e, g = await decide("ent_u1", "approved")
    check("ENT", "unregistered + all PPE -> 'Unregistered worker - PPE verified. You may enter.'",
          g["verdict"] == "Unregistered worker - PPE verified. You may enter." and g["identity"]["kind"] == "unregistered", g["verdict"])
    e, g = await decide("ent_u2", "denied", missing=["helmet"])
    check("ENT", "unregistered + helmet missing -> 'Unregistered worker - ... Entry not permitted.'",
          g["verdict"].startswith("Unregistered worker") and "Entry not permitted" in g["verdict"], g["verdict"])
    check("ENT", "viewer copy of the verdict hides the worker's name (event + entrance payload)",
          "Chinmaya" not in m._for_viewer("entrance", m.entrance_payload(db.get_event("ent_w1")))["verdict"]
          and "Chinmaya" not in auth.mask_event(db.get_event("ent_w1"))["details"]["verdict"])
    t = time.time()
    r = await hw_api.handle_rfid({"uid": "AA000077", "device_id": "esp32-rfid-01"}, t)
    u2_before = m.entrance_payload(db.get_event("ent_w2"))["verdict"]
    e, g = await decide("ent_c1", "approved", track=501)
    check("ENT", "a card tapped right after ANOTHER person's face-checked decision does not rename that person",
          m.entrance_payload(db.get_event("ent_w2"))["verdict"] == u2_before)
    check("ENT", "card tapped, face not recognised -> identified by RFID", g["identity"]["by"] == "rfid" and g["identity"]["worker_id"] == "W-077"
          and "W-077 Chinmaya verified" in g["verdict"], g["verdict"])
    check("ENT", "tap + approved -> counted inside the site", any(p["worker_id"] == "W-077" for p in hw.present("main_entrance")))
    await hw_api.handle_rfid({"uid": "AA000077", "device_id": "esp32-rfid-01"}, t + 5)     # tap out
    check("ENT", "tap out -> no longer inside; names nobody at the check", not hw.present("main_entrance") and not m.ENTRANCE_CARD)
    await hw_api.handle_rfid({"uid": "AA000077", "device_id": "esp32-rfid-01"}, t + 10)
    e, g = await decide("ent_c2", "denied", missing=["vest"], track=502)
    check("ENT", "tap + PPE missing -> entry refused AND not counted as inside", g["identity"]["worker_id"] == "W-077" and not hw.present("main_entrance"))
    e, g = await decide("ent_c3", "approved", track=502)
    check("ENT", "same person puts the vest on -> approved with the same card, counted inside again",
          g["identity"]["worker_id"] == "W-077" and any(p["worker_id"] == "W-077" for p in hw.present("main_entrance")))
    await hw_api.handle_rfid({"uid": "AA000077", "device_id": "esp32-rfid-01"}, t + 20)    # tap out
    m.ENTRANCE_CARD.clear()
    e, g = await decide("ent_c4", "approved", track=503)
    db.x("UPDATE events SET received_at=? WHERE event_id='ent_c4'", (t + 24,))    # 1 s before the tap below
    await hw_api.handle_rfid({"uid": "AA000077", "device_id": "esp32-rfid-01"}, t + 25)
    g = m.entrance_payload(db.get_event("ent_c4"))
    check("ENT", "camera faster than the hand: card tapped just AFTER the decision -> 'Unregistered' corrected to the card's owner",
          g["identity"]["worker_id"] == "W-077" and g["verdict"].startswith("W-077 Chinmaya verified"), g["verdict"])
    await hw_api.handle_rfid({"uid": "AA000077", "device_id": "esp32-rfid-01"}, t + 30)
    await hw_api.handle_rfid({"uid": "AA000078", "device_id": "esp32-rfid-01"}, t + 12)
    e, g = await decide("ent_m1", "approved", "W-077", "Chinmaya", track=504)
    check("ENT", "somebody else's card (face W-077, card W-078) -> NEVER auto-approved: manual check with the reason",
          g["result"] == "manual" and e["status"] == "open" and "W-078" in (g["reason"] or "") and "W-077" in (g["reason"] or ""), g["reason"])
    m.ENTRANCE_CARD.clear()
    # ---------------- login, roles, masking (section 10)
    from fastapi.testclient import TestClient
    os.environ["SENTINELEYE_AUTH"] = "1"
    auth.USERS_FILE = os.path.join(TMP, "users.local.yaml")
    auth.set_user("sup", "supervisor-pass", "supervisor")
    auth.set_user("view", "viewer-pass1", "viewer")
    with TestClient(m.app) as c:
        check("AUTH", "no login -> API refuses (401) and the page redirects to /login",
              c.get("/api/events").status_code == 401 and c.get("/", follow_redirects=False).status_code == 303)
        check("AUTH", "edge / ESP32 posts work without login (devices)",
              c.post("/api/edge/health", json={"cameras": []}).status_code == 200)
        check("AUTH", "but actions on events need a login (POST /api/events/<id>/ack -> 401)",
              c.post("/api/events/evt_t09/ack", json={}).status_code == 401)
        r = c.post("/api/login", json={"username": "view", "password": "viewer-pass1"})
        ev = c.get("/api/events?limit=50").json()
        check("AUTH", "viewer: worker names and snapshots hidden", r.status_code == 200 and
              all(e.get("worker_name") is None and not e.get("snapshot") for e in ev))
        rr = c.post("/api/events/evt_t09/ack", json={})
        check("AUTH", "viewer cannot acknowledge (403) and the refusal is audited",
              rr.status_code == 403 and db.q("SELECT * FROM audit_log WHERE action='denied'"), f"{rr.status_code} {rr.text[:80]}")
        c.post("/api/logout")
        for _ in range(5):
            c.post("/api/login", json={"username": "sup", "password": "wrong"})
        check("AUTH", "5 wrong passwords -> locked for a minute",
              c.post("/api/login", json={"username": "sup", "password": "supervisor-pass"}).status_code == 401)
        auth._fails.clear()
        c.post("/api/login", json={"username": "sup", "password": "supervisor-pass"})
        r = c.post("/api/events/evt_t09/ack", json={})
        check("AUTH", "supervisor acknowledges; audit log names the user", r.status_code == 200 and
              db.q("SELECT * FROM audit_log WHERE action='ack' AND user='sup'"))
        check("AUTH", "supervisor cannot register workers (admin only)", c.post("/api/workers", json={"name": "x"}).status_code == 403)
        r = c.post("/api/assistant", json={"question": "What happened yesterday at 4 PM?"})
        check("AUTH", "assistant over HTTP returns the record with a link", r.status_code == 200
              and r.json()["records"] and r.json()["records"][0]["link"] == "/#event=evt_known")
        r = c.post("/api/entrance/ent_t03/decide", json={"decision": "approved", "note": "checked by hand"})
        e3 = db.get_event("ent_t03")
        check("03", "manual check result recorded (who, decision) and audited", r.status_code == 200
              and e3["details"]["override"]["decision"] == "approved" and e3["details"]["override"]["by"] == "sup"
              and db.q("SELECT * FROM audit_log WHERE action='entrance_manual_approved'"))
        check("AUTH", "health endpoint lists components", c.get("/api/health").json()["components"])
        # ---------------- video clip per incident (sections 3, 5, 8: "snapshot or clip when available")
        from edge.clips import ClipRecorder
        import numpy as np
        cdir = os.path.join(TMP, "clips_edge")
        rec = ClipRecorder({"pre_s": 4, "post_s": 4, "fps": 6, "width": 640}, "", cdir)
        t0 = 1000.0
        for i in range(int(12 * 6)):                       # 12 s of a 1280x720 camera, alert at 5 s
            ts = t0 + i / 6
            img = np.full((720, 1280, 3), (i * 3) % 255, np.uint8)
            if rec.due("cam_01", ts):
                rec.add("cam_01", img, ts, [{"bbox": [100, 100, 400, 700]}])
            if abs(ts - (t0 + 5)) < 1e-6 + 1 / 12:
                rec.start("cam_01", "evt_t09", ts, "ppe_missing")
        f = os.path.join(cdir, "evt_t09.mp4")
        last = -1
        for _ in range(200):                           # wait until the encoder has finished writing the file
            size = os.path.getsize(f) if os.path.exists(f) else 0
            if size > 0 and size == last and not rec.jobs and rec.todo.empty():
                break
            last = size
            time.sleep(0.25)
        try:
            import av
            with av.open(f) as vf:
                vs = vf.streams.video[0]
                nfr, wh, codec = sum(1 for _ in vf.decode(video=0)), (vs.width, vs.height), vs.codec_context.name
        except Exception as x:                            # noqa: BLE001
            nfr, wh, codec = 0, None, repr(x)
        check("CLIP", "incident clip = ~4 s before + ~4 s after the alert, H.264 MP4 (plays in every browser), 640 px",
              os.path.exists(f) and 40 <= nfr <= 56 and wh == (640, 360) and codec == "h264", f"{nfr} frames {wh} {codec}")
        c.post("/api/logout")
        r = c.post("/api/events/evt_t09/clip", content=open(f, "rb").read(), headers={"Content-Type": "video/mp4"})
        check("CLIP", "edge uploads the clip without login (device path) -> linked to the SAME event id",
              r.status_code == 200 and db.get_event("evt_t09")["clip"] == "/clips/evt_t09.mp4"
              and any(a["action"] == "clip_saved" for a in db.get_event("evt_t09")["actions"]), r.text[:80])
        check("CLIP", "no login -> clip file refused", c.get("/clips/evt_t09.mp4").status_code == 401)
        c.post("/api/login", json={"username": "view", "password": "viewer-pass1"})
        ev = c.get("/api/events/evt_t09").json()
        check("CLIP", "viewer role: clip hidden (no link, file 403)", ev.get("clip") is None
              and c.get("/clips/evt_t09.mp4").status_code == 403)
        c.post("/api/logout")
        c.post("/api/login", json={"username": "sup", "password": "supervisor-pass"})
        r = c.get("/clips/evt_t09.mp4")
        check("CLIP", "supervisor: dashboard record has the clip and it downloads (video/mp4)",
              c.get("/api/events/evt_t09").json().get("clip") == "/clips/evt_t09.mp4" and r.status_code == 200
              and r.headers.get("content-type", "").startswith("video/mp4") and len(r.content) > 1000)
        from edge.retention import purge
        old = os.path.join(m.CLIP_DIR, "evt_t09.mp4")
        os.utime(old, (time.time() - 40 * 86400,) * 2)
        check("CLIP", "retention: clips older than snapshot_retention_days are deleted like snapshots",
              purge(m.CLIP_DIR, 30, None, ("*.mp4", "*.webm")) == ["evt_t09"] and not os.path.exists(old))
    os.environ["SENTINELEYE_AUTH"] = "0"
    # ---------------- 4 Nov: live video pushed over one WebSocket (newest picture per camera, only the cameras asked for)
    with TestClient(m.app) as c:
        m.FRAMES["cam_01"] = (b"\xff\xd8jpeg-one", time.time(), time.time() - 0.05)
        m.FRAMES["cam_02"] = (b"\xff\xd8jpeg-two", time.time(), time.time() - 0.05)
        with c.websocket_connect("/ws/video") as v:
            v.send_text(json.dumps({"cams": ["cam_02"]}))
            first = v.receive_bytes()
            m.FRAMES["cam_01"] = (b"\xff\xd8jpeg-one-b", time.time(), time.time() - 0.04)
            m.FRAMES["cam_02"] = (b"\xff\xd8jpeg-two-b", time.time(), time.time() - 0.04)
            second = v.receive_bytes()
        heads = [x.split(b"\n", 1) for x in (first, second)]
        check("12", "live video: pictures are pushed over one WebSocket with camera + capture time; a viewer gets only its cameras",
              all(h[0].startswith(b"cam_02|") and h[0].count(b"|") == 2 for h in heads) and heads[1][1] == b"\xff\xd8jpeg-two-b",
              f"{[h[0].decode() for h in heads]}")
    # ---------------- 5 Nov: admin report generator (any time range + filters -> preview / PDF / CSV)
    with TestClient(m.app) as c:
        t1 = time.time() + 60
        r = c.get(f"/api/reports/custom?start={t1 - 7 * 86400}&end={t1}&format=json").json()
        rz = c.get(f"/api/reports/custom?start={t1 - 7 * 86400}&end={t1}&format=json&type=ppe_missing&zone=welding_bay").json()
        pdf = c.get(f"/api/reports/custom?start={t1 - 7 * 86400}&end={t1}&format=pdf&title=Shift%20report")
        csvr = c.get(f"/api/reports/custom?start={t1 - 7 * 86400}&end={t1}&format=csv&type=fire,smoke")
        bad = c.get(f"/api/reports/custom?start={t1}&end={t1 - 10}")
    check("REP", "report generator: time range -> summary (totals, most missed PPE, by zone / camera / worker) + records",
          r["summary"]["total"] > 0 and r["summary"]["by_zone"] and r["rows"] and r["summary"]["lines"][0].startswith("Period:"),
          r["summary"]["lines"][:2])
    check("REP", "filters work (PPE violations in the welding bay only)", rz["summary"]["total"] > 0 and
          all(x["type"] == "ppe_missing" and x["zone_name"] == "Welding bay" for x in rz["rows"]))
    check("REP", "PDF and CSV downloads (named by the time range); end before start refused",
          pdf.status_code == 200 and pdf.content[:4] == b"%PDF" and "netrax_report_" in pdf.headers.get("content-disposition", "")
          and csvr.status_code == 200 and csvr.text.startswith("event_id") and bad.status_code == 400)
    from edge.stream import StreamReader
    sr = StreamReader("cam_x", 0, 12)
    sr.frame_ts, sr.active_since, sr.fps = time.time(), 0, 30.0
    check("12", "camera health: a fresh 30 fps camera is ONLINE although the shared AI checks it only 3.5x/s (live video is separate)",
          sr.health(3.5) == "ONLINE" and sr.health(0.4) == "DEGRADED")
    # ---------------- shared engine: batching, priority, mapping back
    class FakeDet:
        device = "cpu"

        def __init__(self):
            self.batches = []

        def detect_batch(self, frames, flags):
            self.batches.append(len(frames))
            time.sleep(0.03)
            return [{"persons": [{"cam": f}], "items": [], "parts": [], "fire": [] if fl else None,
                     "timings": {"total_ms": 30}} for f, fl in zip(frames, flags)]
    det = FakeDet()
    eng = InferenceEngine(det, max_batch=4, gather_ms=5)
    eng.start()
    res = {}

    def cam(i):
        res[i] = eng.infer(f"cam_{i}", f"frame_{i}", i == 2, rank=1)
    ths = [threading.Thread(target=cam, args=(i,)) for i in range(3)]
    t0 = time.time()
    [t.start() for t in ths]
    [t.join() for t in ths]
    took = (time.time() - t0) * 1000
    check("12", "3 cameras at once -> ONE batched GPU call, each camera gets its own result back",
          det.batches[0] == 3 and all(res[i]["persons"][0]["cam"] == f"frame_{i}" for i in range(3))
          and res[2]["fire"] == [] and res[0]["fire"] is None, f"batches {det.batches}, {took:.0f} ms for 3 cameras")
    # ---------------- 4 Nov: models in their own process (shared memory frames, results back, mixed frame sizes)
    from edge.engine_proc import ProcessEngine
    import numpy as _np
    pe = ProcessEngine({}, max_batch=4, gather_ms=5, factory="echo", start_timeout=60)
    got = {}

    def _cam(i, shape):
        got[i] = pe.infer(f"cam_{i}", _np.full(shape, i * 10, _np.uint8), i == 1, rank=1)
    ths = [threading.Thread(target=_cam, args=(i, sh)) for i, sh in enumerate([(48, 64, 3), (64, 48, 3), (48, 64, 3)])]
    [t.start() for t in ths]
    [t.join() for t in ths]
    big = pe.infer("cam_0", _np.full((96, 128, 3), 77, _np.uint8), False)       # larger frame -> new buffer
    check("12", "AI process: every camera gets ITS OWN frame's result back (portrait + landscape + a resized camera)",
          all(abs(got[i]["persons"][0]["mean"] - i * 10) < 1e-6 for i in range(3)) and got[1]["fire"] == []
          and got[0]["fire"] is None and abs(big["persons"][0]["mean"] - 77) < 1e-6 and pe.det.supported == {"helmet", "vest"},
          f"stats {pe.stats()}")
    pe.close()
    # ---------------- whole edge pipeline with fake models (worker -> shared engine -> entrance / state machine)
    pipeline_test()
    # ---------------- report
    os.makedirs("docs/eval", exist_ok=True)
    with open("docs/eval/final_selftest.md", "w", encoding="utf-8") as f:
        f.write(f"# NETRAX final acceptance self-test\n\nRun: {timeutil.fmt(time.time())} - `python -m tools.final_selftest`\n\n"
                "Providers (Telegram / Twilio / SMTP) are replaced by fakes that answer like the real services; "
                "the live-provider and camera tests are in docs/NETRAX_Final_Test_Guide.pdf.\n\n"
                "| Test | Check | Result | Evidence |\n|---|---|---|---|\n")
        for tid, name, res_, evd in RESULTS:
            f.write(f"| {tid} | {name} | {res_} | {str(evd).replace('|', '/')} |\n")
    print("\nALL FINAL TESTS PASSED" if ok_all else "\nSOME FINAL TESTS FAILED")
    return ok_all


if __name__ == "__main__":
    good = asyncio.run(main())
    sys.exit(0 if good else 1)
