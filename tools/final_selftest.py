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

try:
    sys.stdout.reconfigure(errors="replace")     # the Windows console cannot print every icon (❌ ✅ ❔)
except AttributeError:
    pass
TMP = tempfile.mkdtemp()
os.environ.setdefault("SENTINELEYE_MUTE", "1")
os.environ["SENTINELEYE_DB"] = os.path.join(TMP, "final_test.db")
os.environ["SENTINELEYE_DATA_DIR"] = TMP
os.environ["SENTINELEYE_AUTH"] = "0"           # HTTP auth is switched on per test below
os.environ["SENTINELEYE_POWER_FILE"] = os.path.join(TMP, "camera_power.json")   # never the real on/off file
os.environ["NETRAX_CHANNELS"] = "telegram,call,sms,email"   # test every channel even if the site disabled some
os.environ["SENTINELEYE_SECRETS"] = os.path.join(TMP, "no_secrets.yaml")   # never the real keys of this laptop
os.environ["NETRAX_CALL_PROVIDER"] = "twilio"  # the older acceptance tests use the fake Twilio; CallMeBot / Android: call_tests()
for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM",
          "ALERT_PHONE_TO", "SMTP_USER", "SMTP_PASSWORD", "ALERT_EMAIL_TO", "ALERT_CALL_TO", "ADB_PATH"):
    os.environ.pop(k, None)

import backend.main as m                       # noqa: E402
from backend import auth, assistant, db, fire_levels, location, notify, perf, timeutil   # noqa: E402
from edge.engine import InferenceEngine        # noqa: E402
from edge.entrance import EntranceChecker      # noqa: E402

# the older tests post several independent alerts for W-001 on cam_01; same-person merging (11 Nov) is tested
# on its own in merge_tests() with the switch on
m.SETTINGS.setdefault("incidents", {})["merge_same_person"] = False

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
    if NET.mode.get("email") == "down":            # 12 Nov: Wi-Fi off -> DNS lookup fails
        return notify.Result("failed", error="network: gaierror: [Errno 11001] getaddrinfo failed", recoverable=True)
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


def ppe_payload_w(eid, items, **kw):
    """Same as ppe_payload for a worker no other test uses (W-090), so the same-person merge test is independent."""
    p = ppe_payload(eid, items, **kw)
    p.update(worker_id="W-090", worker_name="Test worker")
    return p


def fire_payload(eid, bbox, growth=1.0, action="open", typ="fire", cam="cam_02", zone="warehouse", t=None):
    t = t or time.time()
    iso = timeutil.local(t).isoformat()
    return {"event_id": eid, "action": action, "type": typ, "items_missing": [], "severity": "Critical",
            "camera_id": cam, "camera_name": cam, "zone_id": zone, "zone_name": m.ZONES[zone]["name"], "track_id": None,
            "confidence": 0.8, "bbox": bbox, "growth": growth, "captured_at": iso, "started_at": iso, "onset_at": iso,
            "frame_w": 1280, "frame_h": 720}


def usb_order_test():
    """6 Nov live bug: the USB C270 ignored MJPG set BEFORE the size -> uncompressed YUY2 at 7.5 fps (MJPG last: 29.8 fps)."""
    import cv2 as _cv2
    import edge.stream as es
    calls = []

    class FakeCap:
        def __init__(self, *a): pass
        def isOpened(self): return True
        def set(self, prop, val): calls.append(prop); return True
        def get(self, prop): return _cv2.VideoWriter_fourcc(*"MJPG") if prop == _cv2.CAP_PROP_FOURCC else 0
    real = es.cv2.VideoCapture
    es.cv2.VideoCapture = FakeCap
    try:
        es.StreamReader("cam_x", 0, 10, 0, {"width": 1280, "height": 720, "fps": 30, "mjpg": True})._open()
    finally:
        es.cv2.VideoCapture = real
    i = {p: calls.index(p) for p in (_cv2.CAP_PROP_FOURCC, _cv2.CAP_PROP_FRAME_WIDTH, _cv2.CAP_PROP_FPS)}
    check("12", "USB camera: MJPG is requested AFTER size + frame rate (else the C270 stays uncompressed at 7.5 fps)",
          i[_cv2.CAP_PROP_FOURCC] > i[_cv2.CAP_PROP_FRAME_WIDTH] and i[_cv2.CAP_PROP_FOURCC] > i[_cv2.CAP_PROP_FPS], calls)


def motion_tests():
    """6 Nov: boxes / labels follow a walking person between AI results (display only)."""
    from edge.live import Motion, predict_overlay
    mo = Motion()

    def ov(ts, x):
        return {"ts": ts, "persons": [{"tid": 7, "bbox": [x, 100, x + 100, 400]}],
                "items": [{"cls": "helmet", "bbox": [x + 30, 100, x + 70, 140]}], "parts": []}
    o1, o2 = ov(10.0, 100), ov(10.5, 150)            # person walks right at 100 px/s
    mo.learn("cam_01", o1)
    mo.learn("cam_01", o2)
    moved = predict_overlay(o2, mo.shift("cam_01", o2, 10.8, 1280))
    far = predict_overlay(o2, mo.shift("cam_01", o2, 14.0, 1280))
    still = predict_overlay(o2, mo.shift("cam_01", o2, 10.5, 1280))
    check("12", "live view: boxes + their helmet label move WITH the walking person between AI results (100 px/s, 0.3 s -> +30 px)",
          abs(moved["persons"][0]["bbox"][0] - 180) < 1 and abs(moved["items"][0]["bbox"][0] - 210) < 1
          and o2["persons"][0]["bbox"][0] == 150, moved["persons"][0]["bbox"])
    check("12", "live view: prediction never runs ahead more than 0.5 s, and none for the AI's own frame",
          abs(far["persons"][0]["bbox"][0] - 200) < 1 and still["persons"][0]["bbox"][0] == 150, far["persons"][0]["bbox"])


def entrance_object_tests():
    """5 Nov live bug: a chair with clothes on it was checked as an 'unregistered worker' (conf 0.53-0.60, no head)."""
    ok_items = {"helmet": ("compliant", .9), "vest": ("compliant", .9), "boots": ("compliant", .9)}
    head = [[200, 80, .9], [190, 70, .9], [210, 70, .9], [180, 75, .8], [220, 75, .8], [160, 160, .9], [240, 160, .9]] + \
        [[0, 0, .1]] * 10
    legs = [[0, 0, .1]] * 13 + [[150, 600, .6], [250, 600, .6], [150, 690, .6], [250, 690, .6]]

    def run(person, n=60, dt=0.15, move=False):
        ec = EntranceChecker("cam_04", "main_entrance", ["helmet", "vest", "boots"], {})
        out = []
        for i in range(n):
            p = dict(person, tid=1, bbox=[100 + (i % 2) * 40 * move, 40, 300 + (i % 2) * 40 * move, 700])
            out += ec.update([p], {1: ok_items}, 1280, 720, 1000.0 + i * dt)
        return out
    clothes = run({"conf": 0.58, "kps": legs})
    clothes_conf = run({"conf": 0.72, "kps": legs})
    weak_still = run({"conf": 0.70, "kps": head}, n=120)
    real = run({"conf": 0.85, "kps": head})
    real_still = run({"conf": 0.85, "kps": head}, n=120)
    check("ENT", "a chair with clothes is not a person: weak detection or no head / shoulders -> no entrance decision at all",
          not clothes and not clothes_conf, f"{len(clothes)} / {len(clothes_conf)} decisions")
    check("ENT", "a real person (head + shoulders, confident) is still checked; standing still never turns them into background",
          len(real) == 1 and real[0]["result"] == "approved" and len(real_still) == 1, f"{len(real)} / {len(real_still)}")
    check("ENT", "a weak 'person' that never moves for 6 s is remembered as background (one decision at most, no repeats)",
          len(weak_still) <= 1, f"{len(weak_still)} decisions in 18 s")


def ppe_accuracy_alert_tests():
    """11 Nov (final round tomorrow): vest seen on the entrance camera rejected as 'not hi-vis', helmet on a close /
    slim person rejected by size, head above the picture called 'NO HELMET', one person = many entrance popups and
    several open alerts for the same worker on one camera."""
    import numpy as _np
    from fastapi.testclient import TestClient
    from edge.validate import item_ok
    from edge.association import evaluate
    V = m.SETTINGS["validation"]
    person = [100, 50, 300, 700]
    vest = {"cls": "vest", "conf": 0.95, "bbox": [110, 200, 290, 450]}

    def frame(bgr):
        f = _np.full((720, 1280, 3), 60, _np.uint8)
        f[200:450, 110:290] = bgr
        return f
    washed = item_ok(frame((150, 225, 205)), person, vest, V)[0]       # HSV ~ (33, 85, 225): entrance USB vest
    strong = item_ok(frame((0, 255, 200)), person, vest, V)[0]
    blue = item_ok(frame((140, 70, 40)), person, vest, V)[0]
    beige = item_ok(frame((150, 180, 205)), person, vest, V)[0]        # skin / beige shirt (hue ~16)
    white = item_ok(frame((225, 230, 230)), person, vest, V)[0]
    check("PPE", "vest: washed-out fluorescent yellow (entrance USB camera) ACCEPTED; blue / beige / white shirt still rejected",
          washed and strong and not blue and not beige and not white, [washed, strong, blue, beige, white])
    slim = [100, 50, 200, 700]                                          # slim standing person, helmet 0.75 of the box
    helmet_ok = item_ok(frame((60, 60, 60)), slim, {"cls": "helmet", "conf": 0.9, "bbox": [112, 30, 187, 120]}, V)[0]
    shirt_as_helmet = item_ok(frame((60, 60, 60)), person, {"cls": "helmet", "conf": 0.9, "bbox": [120, 200, 280, 380]}, V)[0]
    check("PPE", "helmet on a slim person (0.75 of the box width) accepted; a shirt-sized box is still not a helmet",
          helmet_ok and not shirt_as_helmet, [helmet_ok, shirt_as_helmet])
    p = {"tid": 1, "bbox": [400, 12, 900, 720], "conf": 0.92}           # close-up: box starts 12 px below the top
    st, _ = evaluate([p], [], {1: ["helmet", "vest"]}, 1280, 720, m.SETTINGS["association"])
    check("PPE", "head above the picture (person close to the camera) -> helmet 'can't tell', never 'NO HELMET'",
          st[1]["helmet"][0] == "unverifiable" and st[1]["vest"][0] == "missing", st[1])

    # entrance: the same person under a NEW tracker id -> no second identical popup; a different verdict is announced
    ec = EntranceChecker("cam_04", "main_entrance", ["helmet", "vest", "boots"], {})
    miss_vest = {"helmet": ("compliant", .9), "vest": ("missing", .9), "boots": ("compliant", .9)}
    out, t = [], 1000.0
    for i in range(12):
        out += ec.update([{"tid": 1, "bbox": [100, 60, 300, 700], "conf": 0.9}], {1: miss_vest}, 1280, 720, t)
        t += 0.15
    t += 1.0                                                            # tracker loses them for a moment -> new id 2
    for i in range(12):
        out += ec.update([{"tid": 2, "bbox": [105, 60, 305, 700], "conf": 0.9}], {2: miss_vest}, 1280, 720, t)
        t += 0.15
    first = len(out)
    allok = {k: ("compliant", .9) for k in miss_vest}
    for i in range(40):                                                 # vest put on -> ONE approval
        out += ec.update([{"tid": 2, "bbox": [105, 60, 305, 700], "conf": 0.9}], {2: allok}, 1280, 720, t)
        t += 0.15
    check("ENT", "same person, new tracker id: no second 'vest missing' popup; putting the vest on -> one APPROVED",
          first == 1 and [o["result"] for o in out] == ["denied", "approved"], [o["result"] for o in out])
    ec = EntranceChecker("cam_04", "main_entrance", ["helmet", "vest", "boots"], {})
    unclear = {"helmet": ("unverifiable", .9), "vest": ("compliant", .9), "boots": ("compliant", .9)}
    out, t = [], 2000.0
    for i in range(40):
        out += ec.update([{"tid": 1, "bbox": [300, 5, 1000, 715], "conf": 0.9}], {1: unclear}, 1280, 720, t)
        t += 0.15
    check("ENT", "standing too close (head cut at the top) -> manual 'too close - step back', not 'denied'",
          out and out[0]["result"] == "manual" and "too close" in (out[0]["reason"] or ""),
          out and (out[0]["result"], out[0]["reason"]))

    # backend: one active PPE alert per person per camera
    m.SETTINGS["incidents"]["merge_same_person"] = True
    try:
        with TestClient(m.app) as c:
            now = time.time()
            c.post("/api/events", json=ppe_payload_w("evt_mg_a", ["helmet"], t=now - 30))
            c.post("/api/events", json=ppe_payload_w("evt_mg_b", ["helmet"], t=now - 20))        # edge restart / new track
            c.post("/api/events", json=ppe_payload_w("evt_mg_c", ["helmet", "vest"], t=now - 10))  # more missing
            a = db.get_event("evt_mg_a")
            dup = [db.get_event("evt_mg_b"), db.get_event("evt_mg_c")]
            other = ppe_payload("evt_mg_o", ["helmet"], t=now - 5)
            other.update(worker_id="W-002", worker_name="SUSHREE")
            c.post("/api/events", json=other)
            c.post("/api/events", json=ppe_payload_w("evt_mg_a", ["helmet"], action="resolve", t=now))   # first track gone
            still = db.get_event("evt_mg_a")["status"]
            c.post("/api/events", json=ppe_payload_w("evt_mg_b", ["helmet"], action="resolve", t=now))
            c.post("/api/events", json=ppe_payload_w("evt_mg_c", ["helmet"], action="resolve", t=now))
            end = db.get_event("evt_mg_a")["status"]
            # edge restart: the old run's alert is closed when the new edge reports in
            po = ppe_payload("evt_mg_old", ["gloves"], cam="cam_02", zone="warehouse", t=now - 60)
            po.update(worker_id="W-093", worker_name="Restart test")      # own worker: zone-wide merge (12 Nov)
            c.post("/api/events", json=po)
            db.x("UPDATE events SET updated_at=? WHERE event_id='evt_mg_old'", (now - 60,))
            c.post("/api/edge/health", json={"at": now, "edge_started": now - 1, "cameras": [{"camera_id": "cam_02"}]})
            old = db.get_event("evt_mg_old")
        check("PPE", "same worker, same camera: 3 edge alerts -> ONE alert (items updated to helmet + vest), another worker separate",
              dup == [None, None] and a["items_missing"] == ["helmet", "vest"] and db.get_event("evt_mg_o") is not None,
              [a["items_missing"], dup])
        check("PPE", "merged alert stays open until every merged camera track has ended",
              still == "open" and end == "resolved", [still, end])
        check("PPE", "edge restarted -> its old alerts that can no longer be tracked are closed (no orphan 'open' alerts)",
              old["status"] == "resolved" and old["end_reason"] == "edge_restarted", [old["status"], old["end_reason"]])
    finally:
        m.SETTINGS["incidents"]["merge_same_person"] = False


def cap_vs_helmet_test():
    """12 Nov (jury): a normal cap must not pass as a helmet. The main model calls caps 'helmet'; only a helmet the v3
    model also confirmed (per person, kept 30 s) counts. Far people are not judged this way (rarely confirmed)."""
    from edge.association import evaluate
    A = dict(m.SETTINGS["association"], helmet_confirm_wait_s=1.0, helmet_confirm_keep_s=30, helmet_confirm_min=0.3,
             helmet_confirm_min_height=0.35)
    near = {"tid": 1, "bbox": [400, 100, 700, 700], "conf": 0.92, "track_conf": 0.92}       # 83 % of a 720 px picture
    far = {"tid": 2, "bbox": [100, 400, 160, 560], "conf": 0.92, "track_conf": 0.92}        # 22 %
    cap = {"cls": "helmet", "conf": 0.8, "bbox": [500, 90, 600, 150], "confirm": 0.0}
    helm = dict(cap, confirm=0.85)

    def st(p, it, mem, t):
        return evaluate([p], [it], {p["tid"]: ["helmet"]}, 1280, 720, A, helmet_mem=mem, now=t)[0][p["tid"]]["helmet"][0]
    mem = {}
    seq_cap = [st(near, cap, mem, t) for t in (0.0, 0.5, 1.2, 2.0)]
    mem = {}
    seq_helm = [st(near, helm, mem, 0.0), st(near, cap, mem, 10.0), st(near, cap, mem, 45.0)]   # confirmation kept 30 s
    far_box = dict(cap, bbox=[115, 395, 145, 415])
    far_res = st(far, far_box, {}, 5.0)
    legacy = evaluate([near], [cap], {1: ["helmet"]}, 1280, 720, A)[0][1]["helmet"][0]      # no memory = old behaviour
    check("PPE", "cap (never confirmed by the 2nd model) -> 'checking' then NO HELMET; real helmet confirmed once stays OK 30 s; "
                 "far person / no memory = old behaviour",
          seq_cap == ["unverifiable", "unverifiable", "missing", "missing"] and seq_helm == ["compliant", "compliant", "missing"]
          and far_res == "compliant" and legacy == "compliant", [seq_cap, seq_helm, far_res, legacy])


def eyewear_test():
    """12 Nov (jury): normal spectacles must not pass as safety goggles. Second-stage check on each goggles box
    (edge/eyewear.py): goggles -> compliant, unclear -> 'unverifiable' (no alarm), normal glasses + face seen -> NO GOGGLES."""
    from edge.association import evaluate
    A = m.SETTINGS["association"]
    p = {"tid": 1, "bbox": [400, 100, 700, 700], "conf": 0.92, "track_conf": 0.92}
    face = [{"cls": "face", "conf": 0.8, "bbox": [520, 120, 590, 200]}]
    g = {"cls": "goggles", "conf": 0.7, "bbox": [525, 140, 585, 160]}

    def st(ew):
        it = dict(g, eyewear=ew) if ew else dict(g)
        return evaluate([p], [it], {1: ["goggles"]}, 1280, 720, A, parts=face)[0][1]["goggles"][0]
    got = [st("goggles"), st("unclear"), st("normal_glasses"), st(None)]
    check("PPE", "eyewear check: safety goggles OK, unclear -> no alarm, NORMAL GLASSES (face seen) -> NO GOGGLES, "
                 "no check -> old behaviour", got == ["compliant", "unverifiable", "missing", "compliant"], got)
    import cv2, glob
    import numpy as np
    from edge.validate import item_ok
    frame = np.zeros((720, 1280, 3), np.uint8)
    ok = item_ok(frame, p["bbox"], dict(g, eyewear="normal_glasses"), m.SETTINGS["validation"])
    check("PPE", "a box rejected as normal glasses is labelled so (snapshot / live picture say WHY)",
          ok == (False, "normal glasses"), ok)
    # the real model, when its weights and our own frames are on this laptop
    from edge.eyewear import load, eye_crop
    mods = m.SETTINGS["models"]
    sample = sorted(glob.glob(os.path.join(m.ROOT, "datasets", "own", "ppe", "ppe_01_fullkit_day", "images", "*.jpg")))[:6]
    sun = sorted(glob.glob(os.path.join(m.ROOT, "datasets", "own", "ppe", "ppe_09_all_off_dim", "images", "*.jpg")))
    e = load(mods, 0) if sample and sun else None
    if e is None:
        check("PPE", "eyewear model test skipped (weights or own frames not on this machine)", True, "")
        return
    from ultralytics import YOLO
    aux = YOLO(os.path.join(m.ROOT, mods["ppe_aux"]))

    def boxes(paths):
        out = []
        for pth in paths:
            im = cv2.imread(pth)
            r = aux.predict(im, imgsz=640, conf=0.45, verbose=False)[0]
            out += [eye_crop(im, b) for b, k in zip(r.boxes.xyxy.tolist(), r.boxes.cls.tolist()) if aux.names[int(k)] == "goggles"]
        return out
    pg = e.probs(boxes(sample))
    ps = e.probs(boxes(sun))
    check("PPE", "eyewear model: team's safety goggles -> goggles; sunglasses the detector called 'goggles' -> not goggles",
          pg and min(pg) >= e.lo and sum(v >= e.hi for v in pg) >= len(pg) - 1 and ps and max(ps) < e.hi,
          [[round(v, 2) for v in pg], [round(v, 2) for v in ps]])


def polish_data_tests():
    """12 Nov polish (Prompt 5): every PPE alert carries what was worn / not seen / required (icons); old events still
    open; one identity rule (registered name + ID, 'Unregistered person', UNAUTHORISED) on screen, Telegram, e-mail."""
    from fastapi.testclient import TestClient
    from edge.state_machine import PPEStateMachine
    from backend import db as _db
    sm = PPEStateMachine(m.SETTINGS["state_machine"])
    person = {"tid": 5, "bbox": [400, 100, 700, 700], "conf": 0.9}
    acts = []
    for k in range(60):                                   # 7.5 s at 8 checks/s: helmet off, vest on, gloves hidden
        st = {5: {"helmet": ("missing", .9), "vest": ("compliant", .9), "gloves": ("unverifiable", .5)}}
        acts += sm.update("cam_t", [person], st, {5: "welding_bay"}, lambda z: "high", 1280, 720, now=1000.0 + k / 8)
    o = [a for a in acts if a["action"] == "open"]
    check("PPE", "an alert payload carries items_ok / items_uncertain / items_required (icons), decision unchanged",
          len(o) == 1 and o[0]["items_missing"] == ["helmet"] and o[0]["items_ok"] == ["vest"]
          and o[0]["items_uncertain"] == ["gloves"] and sorted(o[0]["items_required"]) == ["gloves", "helmet", "vest"],
          o[:1])
    _db.x("INSERT OR REPLACE INTO workers (worker_id, name, photos, created_at) VALUES ('W-091', 'Ritika', 0, ?)", (time.time(),))
    with TestClient(m.app) as c:
        p = ppe_payload("evt_pol_new", ["helmet"]); p.update(worker_id="W-091", worker_name=None, track_id=91,
                                                             items_ok=["vest"], items_uncertain=["gloves"], items_required=["helmet", "vest", "gloves"])
        c.post("/api/events", json=p)
        q = ppe_payload("evt_pol_old", ["vest"]); q.update(worker_id="V-091", worker_name=None, track_id=92)
        c.post("/api/events", json=q)
        new, old = c.get("/api/events/evt_pol_new"), c.get("/api/events/evt_pol_old")
    n, ol = new.json(), old.json()
    check("PPE", "backend stores + returns the 3 lists; an event without them still opens (lists = unknown)",
          new.status_code == 200 and n["items_ok"] == ["vest"] and n["items_uncertain"] == ["gloves"]
          and n["items_required"] == ["helmet", "vest", "gloves"] and old.status_code == 200 and ol["items_ok"] is None,
          [n.get("items_ok"), n.get("items_uncertain"), ol.get("items_ok")])
    check("PPE", "a registered worker ID always comes with the name (filled from the worker list)",
          n.get("worker_name") == "Ritika", n.get("worker_name"))
    pol = {"include_worker_names": True}
    w = [notify.who_text({"type": "ppe_missing", "worker_id": "W-091", "worker_name": "Ritika"}, pol),
         notify.who_text({"type": "ppe_missing", "worker_id": "V-003"}, pol),
         notify.who_text({"type": "unauthorized_entry", "worker_id": "W-002", "worker_name": "Sourav"}, pol)]
    line = notify.ppe_line(n)
    check("PPE", "messages: same identity rule + PPE line with icons (Helmet ❌ · Vest ✅ · Gloves ❔)",
          w == ["Ritika · W-091", "Unregistered person", "Sourav · W-002 · UNAUTHORISED"]
          and line == "Helmet ❌ · Vest ✅ · Gloves ❔", [w, line])
    # the dashboard helpers themselves (people.js, ppe_icons.js) in a real browser, when Edge is installed
    import html as _html, subprocess, tempfile as _tf
    edge = next((p for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                             r"C:\Program Files\Microsoft\Edge\Application\msedge.exe") if os.path.exists(p)), None)
    if not edge:
        check("PPE", "personLabel / icon row browser test skipped (Edge not installed)", True, "")
        return
    dd = os.path.join(m.ROOT, "dashboard").replace("\\", "/")
    page = os.path.join(TMP, "ui_test.html")
    open(page, "w", encoding="utf-8").write(f"""<!doctype html><meta charset="utf-8"><body><pre id="o"></pre>
<script src="file:///{dd}/ppe_icons.js"></script><script src="file:///{dd}/people.js"></script><script>
const r = {{reg: personLabel({{type:"ppe_missing", worker_id:"W-004", worker_name:"Ritika"}}),
  unreg: personLabel({{type:"ppe_missing", worker_id:"V-003", track_id:17}}),
  unauth: personLabel({{type:"unauthorized_entry", worker_id:"W-002", worker_name:"Sourav"}}),
  hidden: personLabel({{type:"ppe_missing", worker_id:"W-***"}}),
  icNew: ppeRowFor({{items_missing:["helmet"], items_ok:["vest"], items_uncertain:["gloves"], items_required:["helmet","vest","gloves"]}}),
  icOld: ppeRowFor({{items_missing:["helmet","vest"]}})}};
document.getElementById("o").textContent = JSON.stringify(r);</script>""")
    try:
        out = subprocess.run([edge, "--headless=new", "--disable-gpu", "--allow-file-access-from-files", f"--user-data-dir={_tf.mkdtemp()}",
                              "--dump-dom", "file:///" + page.replace("\\", "/")], capture_output=True, text=True, timeout=60, encoding="utf-8").stdout
        r = json.loads(_html.unescape(out.split('<pre id="o">')[1].split("</pre>")[0]))
    except Exception as ex:                                     # noqa: BLE001
        check("PPE", "personLabel / icon row in the browser", False, repr(ex)[:200])
        return
    cnt = lambda h, s: h.count(f"s-{s}")
    check("PPE", "dashboard identity: 'Ritika · W-004' · 'Unregistered person' (no fake name) · 'Sourav · W-002' + UNAUTHORISED",
          "Ritika · W-004" in r["reg"] and "UNAUTHORISED" not in r["reg"]
          and "Unregistered person" in r["unreg"] and "V-003" not in r["unreg"]
          and "Sourav · W-002" in r["unauth"] and "UNAUTHORISED" in r["unauth"] and "Registered worker" in r["hidden"],
          {k: _html.unescape(v)[:80] for k, v in r.items() if not k.startswith("ic")})
    check("PPE", "icon row: new alert = 1 missing, 1 worn, 1 cannot see; old alert (missing list only) still shows its icons",
          (cnt(r["icNew"], "miss"), cnt(r["icNew"], "ok"), cnt(r["icNew"], "unk")) == (1, 1, 1)
          and cnt(r["icOld"], "miss") == 2 and "NO HELMET" not in r["icNew"].upper().replace("NO-", ""),
          [cnt(r["icNew"], s) for s in ("miss", "ok", "unk")])


def pm_gps_test():
    """12 Nov: the team station sends the Sharp dust sensor VOLTAGE -> estimated PM2.5 on the dashboard (never an alarm);
    GPS from the station is the location of the zone's alerts."""
    from backend import hardware
    hub = hardware.Hub(m.hw_api.CFG)
    base = {"device_id": "esp32-ppe-01", "uptime_s": 300, "temperature_c": 29.0, "humidity_pct": 60.0, "flame_detected": False,
            "mq2_adc": 900, "mq135_adc": 800, "helmet_fsr_adc": 1500}
    t = 5000.0
    for i in range(40):                                   # clean air: sensor sits at 0.62 V
        hub.ingest("esp32-ppe-01", dict(base, pm_adc=500, pm_sensor_v=0.62), now=t + i)
    clean = hub.latest["esp32-ppe-01"]["data"]
    ev = hub.ingest("esp32-ppe-01", dict(base, pm_adc=1900, pm_sensor_v=2.2), now=t + 41)    # very dusty / smoky
    dusty = hub.latest["esp32-ppe-01"]["data"]
    check("HW", "Sharp dust sensor voltage -> PM2.5 shown (estimated, self-zeroed), clean air ~0, dust high",
          clean["pm_estimated"] and clean["pm2_5_ugm3"] == 0 and dusty["pm2_5_ugm3"] >= 300 and dusty["pm_sensor_v"] == 2.2,
          [clean["pm2_5_ugm3"], dusty["pm2_5_ugm3"], dusty["pm_zero_v"]])
    check("HW", "an uncalibrated PM estimate NEVER raises a warning or counts as smoke (station stays NORMAL)",
          ev["level"] == "NORMAL" and not ev["hazards"].get("smoke"), [ev["level"], ev["reasons"]])
    real = hub.ingest("esp32-ppe-02", dict(base, device_id="esp32-ppe-02", pm2_5_ugm3=200), now=t + 42)
    check("HW", "a real PM sensor (pm2_5_ugm3 sent) still alarms as before", real["hazards"].get("smoke") is True, real["reasons"])


def phone_follow_test():
    """12 Nov: the phone's address changes when IP Webcam is reopened -> the camera finds it again by itself."""
    import shutil
    from edge import phones
    phones._IN_USE.clear()
    phones._LAST_SCAN.clear()
    phones.register("cam_02", "http://10.0.0.99:8080/video")
    phones.register("cam_03", "http://10.0.0.50:8080/video")
    new = phones.find_moved("cam_02", "http://10.0.0.99:8080/video", scan=lambda: ["10.0.0.50", "10.0.0.173"])
    again = phones.find_moved("cam_02", new, scan=lambda: ["10.0.0.7"])          # within rescan_s: no second scan
    f = os.path.join(TMP, "cams.yaml")
    shutil.copy(m.path("config", "cameras.yaml") if hasattr(m, "path") else os.path.join("config", "cameras.yaml"), f)
    # 13 Nov: cam_03 is the only phone; cam_01 = Axis RTSP, cam_02 = laptop webcam -> save() must never rewrite those
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("cameras:\n  cam_01:\n    name: Welding bay\n    source: \"rtsp://{axis}@192.168.0.90/axis-media/media.amp\"\n"
                 "  cam_02:\n    name: Warehouse\n    source: 0\n"
                 "  cam_03:\n    name: Assembly line\n    source: http://10.0.0.99:8080/video\n    rotate: 90\n")
    saved = phones.save("cam_03", "http://10.0.0.173:8080/video", file=f)
    txt = open(f, encoding="utf-8").read()
    check("CAM", "phone camera changed its address -> found on the Wi-Fi (not the other phone's), switched + saved in cameras.yaml",
          new == "http://10.0.0.173:8080/video" and again is None and saved and "source: http://10.0.0.173:8080/video" in txt
          and txt.count("10.0.0.173") == 1, [new, again])
    refused = [phones.save(c, "http://10.0.0.5:8080/video", file=f) for c in ("cam_01", "cam_02")]
    txt2 = open(f, encoding="utf-8").read()
    check("CAM", "phone follow never overwrites the Axis CCTV (cam_01) or the laptop webcam (cam_02)",
          refused == [False, False] and txt2 == txt, refused)
    phones._IN_USE.clear()
    phones._LAST_SCAN.clear()


def camera_power_tests():
    """11 Nov: camera ON / OFF from the dashboard -> data/camera_power.json -> the edge releases the camera."""
    from fastapi.testclient import TestClient
    from backend import auth
    from edge.power import Power, read_off
    os.environ["SENTINELEYE_POWER_FILE"] = os.path.join(TMP, "camera_power.json")
    cam = next(c for c, v in m.CAMERAS.items() if v.get("enabled", True) is not False)
    clock = [0.0]
    pw = Power(every_s=0.5, clock=lambda: clock[0])
    with TestClient(m.app) as c:
        off = c.post(f"/api/cameras/{cam}/power", json={"on": False}).json()
        listed = next(x for x in c.get("/api/cameras").json()["cameras"] if x["camera_id"] == cam)
        clock[0] += 1
        edge_off = not pw.is_on(cam)
        on = c.post(f"/api/cameras/{cam}/power", json={"on": True}).json()
        clock[0] += 1
        edge_on = pw.is_on(cam)
        bad = c.post("/api/cameras/nope/power", json={"on": False}).status_code
        audit = [a["action"] for a in c.get("/api/audit?limit=5").json()]
    check("CAM", "camera turned OFF on the dashboard: shown OFF with no alarm badge, the edge releases it within 1 s; ON again brings it back",
          off["power"] == "off" and off["status"] == "OFF" and off["badge"] is None and listed["power"] == "off"
          and edge_off and on["power"] == "on" and edge_on and cam not in read_off() and bad == 404
          and "camera_off" in audit and "camera_on" in audit, [off["status"], edge_off, edge_on, audit[:2]])
    check("CAM", "only a supervisor / admin may switch cameras (viewer refused by the role check)",
          auth.classify("POST", f"/api/cameras/{cam}/power", "10.0.0.5") == "supervisor")
    os.environ["SENTINELEYE_POWER_FILE"] = os.path.join(TMP, "camera_power.json")


def intelligence_tests():
    """11 Nov: 'Industrial Safety Intelligence' page = one extra page that only READS the existing APIs."""
    import re
    from fastapi.testclient import TestClient
    from backend import agent
    src = open(os.path.join(m.ROOT, "dashboard", "intelligence.html"), encoding="utf-8").read()
    with TestClient(m.app) as c:
        r = c.get("/intelligence")
        apis = sorted(set(re.findall(r'"(/api/[a-z_/]+)', src)))
        ok_apis = {a: c.get(a + ("?days=1" if a.endswith("export") else "")).status_code for a in apis
                   if not a.endswith("/") and a not in ("/api/reports/custom", "/api/events/", "/api/hw/history/")}
    check("INT", "safety intelligence page is served, never cached, and has all nine sections",
          r.status_code == 200 and "no-cache" in r.headers.get("cache-control", "")
          and all(f'id="s-{x}"' in src for x in ("overview", "sensors", "ai", "workers", "alerts", "predict", "map", "devices", "reports")),
          r.headers.get("cache-control"))
    check("INT", "the page uses only existing endpoints (every one answers) and changes data only through the normal alert actions"
                 " and the camera on/off switch",
          ok_apis and all(v == 200 for v in ok_apis.values())
          and set(re.findall(r'method: ?"(\w+)"', src)) == {"POST"} and src.count('method: "POST"') == 2
          and "/${t.dataset.act}" in src and "/power`" in src, ok_apis)
    nav = lambda t: (agent.plan(t, "t", "admin", {"zones": m.ZONES, "cameras": m.CAMERAS})["ui"] or [None])[0]
    check("INT", "Zentrix opens it by name, and the older page commands still go where they did",
          nav("hey zentrix open the safety intelligence dashboard") == {"op": "navigate", "url": "/intelligence"}
          and nav("open the factory map") == {"op": "navigate", "url": "/intelligence#map"}
          and nav("go to hardware page") == {"op": "navigate", "url": "/hardware"}
          and nav("open the report generator") == {"op": "navigate", "url": "/admin#reports"},
          [nav("hey zentrix open the safety intelligence dashboard"), nav("open the factory map")])


def call_tests():
    """11 Nov: phone calls ONLY for critical fire / smoke, via CallMeBot (Telegram voice call) or an Android phone on USB.
    Fake network / fake adb only - nothing is ever really called."""
    import types
    pol = notify.load_policy()                                   # the real site policy (config/notify.yaml)
    now = time.time()
    fire2 = {"event_id": "evt_call_a", "type": "fire", "severity": "Critical", "fire_level": 2, "status": "open",
             "zone_id": "warehouse", "zone_name": "Warehouse", "camera_id": "cam_02", "started_at": now}
    hw = dict(fire2, event_id="hw_call_b", fire_level=None, camera_id=None, verification="hardware_only")
    ppe = {"event_id": "evt_call_c", "type": "ppe_missing", "severity": "Critical", "status": "open", "items_missing": ["helmet"]}
    smoke1 = dict(fire2, type="smoke", fire_level=1, severity="High")
    old_rules = {"telegram", "email"}                        # Telegram + e-mail exactly as before, the call is only ADDED
    check("CALL", "EVERY fire / smoke rings the supervisor (Level 2 camera fire, Level 1 smoke, station-only fire); never PPE,"
                  " never a fire still being checked with the station; Telegram + e-mail unchanged",
          "call" in notify.channels_for(fire2, "new alert", pol) and "call" in notify.channels_for(hw, "new alert", pol)
          and "call" in notify.channels_for(smoke1, "new alert", pol)
          and old_rules <= set(notify.channels_for(smoke1, "new alert", pol))
          and "call" not in notify.channels_for(ppe, "new alert", pol)
          and "call" not in notify.channels_for(dict(fire2, status="verifying"), "new alert", pol)
          and "call" not in notify.channels_for(ppe, "ESCALATED (unacknowledged)", pol)
          and "call" in notify.channels_for(fire2, "ESCALATED (unacknowledged)", pol),
          [notify.channels_for(smoke1, "new alert", pol), notify.channels_for(ppe, "new alert", pol)])
    # provider order: Android phone first; when it is not connected the CallMeBot Telegram call takes over
    order = []
    old_a, old_c = notify.send_call_android, notify.send_call_callmebot
    old_env = os.environ.pop("NETRAX_CALL_PROVIDER", None)          # the older tests force Twilio; this one tests the site order
    try:
        notify.send_call_android = lambda sec, to, p: (order.append("android"), notify.Result("failed", error="no Android phone connected", recoverable=True))[1]
        notify.send_call_callmebot = lambda to, txt, p: (order.append("callmebot"), notify.Result("sent", provider_status="CallMeBot call placed"))[1]
        r = notify.send_call(notify.load_secrets(), "+919800000000", "fire", dict(pol, phone=dict(pol.get("phone") or {}, provider=["android", "callmebot"])))
    finally:
        notify.send_call_android, notify.send_call_callmebot = old_a, old_c
        if old_env:
            os.environ["NETRAX_CALL_PROVIDER"] = old_env
    check("CALL", "normal SIM call (Android) is tried first; phone missing -> CallMeBot Telegram call, and the record says why",
          order == ["android", "callmebot"] and r.status == "sent" and "android: no Android phone" in r.provider_status, r.provider_status)
    msg = notify.build(hw, "new alert", pol)
    check("CALL", "the spoken call text fits CallMeBot (<= 256 characters) and says what, where and that it came from the station",
          len(msg["call"]) <= 250 and "Warehouse" in msg["call"] and "safety station" in msg["call"], msg["call"])
    # --- CallMeBot answers (fake HTTP)
    seen = []

    def fake_get(answer):
        def get(url, params=None, timeout=None, **kw):
            seen.append((url, dict(params or {})))
            code, text = answer
            return types.SimpleNamespace(status_code=code, text=text)
        return get
    old = notify.requests
    try:
        notify.requests = types.SimpleNamespace(get=fake_get((200, "<html><b>Call queued</b> to @boss</html>")),
                                                RequestException=old.RequestException, exceptions=_real_requests.exceptions)
        ok = notify.send_call_callmebot("@boss", "x" * 400, pol)
        notify.requests.get = fake_get((200, "You need to authorize CallMeBot to contact you"))
        auth = notify.send_call_callmebot("@boss", "fire", pol)
        notify.requests.get = fake_get((503, "Service Unavailable"))
        down = notify.send_call_callmebot("@boss", "fire", pol)
    finally:
        notify.requests = old
    url, p = seen[0]
    check("CALL", "CallMeBot: accepted -> 'sent' (never claimed 'delivered'); not authorised -> FAILED with the /start hint; "
                  "server down -> retried",
          ok.status == "sent" and "Call queued" in ok.provider_status and auth.status == "failed" and "CallMeBot_txtbot" in auth.error
          and not auth.recoverable and down.status == "failed" and down.recoverable
          and "callmebot.com/start.php" in url and p["user"] == "@boss" and len(p["text"]) <= 250 and p["rpt"] == 2,
          [ok.status, auth.error, down.status, p.get("lang")])
    # --- Android phone on USB (fake adb, fake timer: no real command ever runs)
    ran, timers = [], []

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        ran.append(cmd[1:])
        out = "device\n" if cmd[1] == "get-state" else "Starting: Intent { act=android.intent.action.CALL }"
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")
    old_sp, old_th = notify.subprocess, notify.threading
    try:
        notify.subprocess = types.SimpleNamespace(run=fake_run, TimeoutExpired=old_sp.TimeoutExpired)
        notify.threading = types.SimpleNamespace(Timer=lambda s, f: types.SimpleNamespace(start=lambda: timers.append(s)))
        sec = notify.load_secrets(); sec["call"]["adb_path"] = "fake-adb"
        a = notify.send_call_android(sec, "+919800000000", pol)
        bad = notify.send_call_android(sec, "@boss", pol)
    finally:
        notify.subprocess, notify.threading = old_sp, old_th
    check("CALL", "Android phone: dials tel:+91... over adb and hangs up after ring_s; a Telegram name is refused for a SIM call",
          a.status == "sent" and ["shell", "am", "start", "-a", "android.intent.action.CALL", "-d", "tel:+919800000000"] in ran
          and timers == [int((pol.get("phone") or {}).get("ring_s", 40))] and bad.status == "not_configured", [a.provider_status, ran])
    # --- one call per fire: a level raise to 2 a moment later does not ring again; Level 3 and an escalation do
    n = lambda: len(db.q("SELECT id FROM notifications WHERE event_id='evt_call_a' AND channel='call'"))
    notify.enqueue(dict(fire2), "new alert", channels=["call"]); n1 = n()
    notify.enqueue(dict(fire2), "LEVEL RAISED to 2 (CRITICAL)", channels=["call"]); n2 = n()
    notify.enqueue(dict(fire2, fire_level=3), "LEVEL RAISED to 3 (EVACUATE)", channels=["call"]); n3 = n()
    check("CALL", "one call per fire: a quick level change does not ring again; Level 3 (evacuate) calls again",
          n1 >= 1 and n2 == n1 and n3 > n2, [n1, n2, n3])


def reliable_delivery_tests():
    """12 Nov (prompt pack step 9): Wi-Fi off -> messages wait (never counted as failed) and arrive once after
    reconnecting; Telegram = photo + short caption with icons, updates as a reply; low-priority e-mails in a digest;
    daily e-mail cap; rows left 'sending' by a crash are sent after a restart."""
    from fastapi.testclient import TestClient
    set_creds(True)
    NET.mode["telegram"], NET.mode["email"] = "down", "down"
    rows = lambda eid: db.q("SELECT channel, status, attempts, error FROM notifications WHERE event_id=? ORDER BY id", (eid,))
    # earlier tests' messages are > 60 s old (the same-zone flood control would otherwise send Telegram only)
    db.x("UPDATE notifications SET queued_at = queued_at - 120 WHERE queued_at > ?", (time.time() - 60,))
    try:
        with TestClient(m.app) as c:
            p = ppe_payload("evt_net1", ["helmet"], cam="cam_03", zone="assembly_line"); p.update(worker_id="W-096", worker_name="Net test", items_ok=["vest"],
                                                             items_uncertain=["gloves"], items_required=["helmet", "vest", "gloves"])
            c.post("/api/events", json=p)
            time.sleep(1.5)
            off = rows("evt_net1")
            NET.mode["telegram"], NET.mode["email"] = "ok", None          # Wi-Fi back
            t_end = time.time() + 12
            while time.time() < t_end and any(r["status"] not in notify.FINAL for r in rows("evt_net1")):
                time.sleep(0.5)
            back = rows("evt_net1")
            cap = notify.build(db.get_event("evt_net1"), "new alert", notify.load_policy())["tg"]
            n_tg = len([x for x in NET.calls if "api.telegram.org" in x[1]])
            c.post("/api/events", json=dict(p, action="update", items_missing=["helmet", "vest"]))   # 'more PPE missing'
            t_end = time.time() + 6
            while time.time() < t_end and len([x for x in NET.calls if "api.telegram.org" in x[1]]) == n_tg:
                time.sleep(0.3)
            reply = [x for x in NET.calls if "api.telegram.org" in x[1]][-1][2] or {}
    finally:
        NET.mode["telegram"], NET.mode["email"] = "ok", None
    check("NOTIFY", "Wi-Fi off: Telegram + e-mail WAIT for the internet (not counted as failed tries)",
          off and all(r["status"] in ("waiting", "queued", "sending") for r in off) and all(int(r["attempts"] or 0) == 0 for r in off if r["status"] == "waiting")
          and any(r["status"] == "waiting" for r in off), [(r["channel"], r["status"], r["attempts"]) for r in off])
    check("NOTIFY", "Wi-Fi back: every message arrives exactly once (no loss, no duplicate)",
          sorted((r["channel"], r["status"]) for r in back) == [("email", "sent"), ("telegram", "delivered")], [(r["channel"], r["status"]) for r in back])
    check("NOTIFY", "Telegram caption: person + PPE icons (Helmet ❌ · Vest ✅ · Gloves ❔); an update is a REPLY to the first message",
          "Net test · W-096" in cap and "Helmet ❌ · Vest ✅ · Gloves ❔" in cap and str(reply.get("reply_to_message_id")) == "42",
          [cap.splitlines()[:3], reply.get("reply_to_message_id")])
    # low-priority -> digest; daily cap
    pol = notify.load_policy()
    den = {"event_id": "evt_dig1", "type": "entrance_check", "severity": "Low", "status": "open", "zone_id": "main_entrance",
           "zone_name": "Main entrance", "details": {"result": "denied"}, "started_at": time.time()}
    db.x("INSERT OR REPLACE INTO events (event_id, type, items_missing, severity, zone_id, zone_name, started_at, status, details) "
         "VALUES (?,?,?,?,?,?,?,?,?)", ("evt_dig1", "entrance_check", "[]", "Low", "main_entrance", "Main entrance", time.time(), "open",
                                        json.dumps({"result": "denied"})))
    notify.enqueue(den, "entrance refused", channels=["email"])
    d_rows = rows("evt_dig1")
    did = notify.send_digest(force=True)
    after = rows("evt_dig1")
    check("NOTIFY", "low-priority e-mail (entrance refused) -> collected into ONE digest e-mail (every 15 min)",
          d_rows and d_rows[0]["status"] == "digest" and did and after[0]["status"] == "in_digest", [d_rows, did])
    old_count = notify.emails_today
    try:
        notify.emails_today = lambda: int((pol.get("email") or {}).get("daily_max", 300))
        q1 = notify.enqueue(dict(db.get_event("evt_net1"), event_id="evt_net1"), "limit test", channels=["email"], kind="limit-ppe")
        fire = {"event_id": "evt_net_fire", "type": "fire", "severity": "Critical", "fire_level": 2, "status": "open", "zone_id": "warehouse",
                "zone_name": "Warehouse", "started_at": time.time()}
        q2 = notify.enqueue(fire, "new alert", channels=["email"])
    finally:
        notify.emails_today = old_count
    check("NOTIFY", "daily e-mail cap reached -> PPE e-mail skipped (reason shown), fire e-mail still sent",
          q1 and q1[0]["status"] == "skipped" and q2 and q2[0]["status"] == "queued", [q1 and q1[0]["status"], q2 and q2[0]["status"]])
    # crash: a row left 'sending' is sent after a restart
    now = time.time()
    cur = db.x("INSERT INTO notifications (event_id, channel, recipient, message, delivered, at, status, attempts, kind, queued_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?)", ("evt_net1", "telegram", "group", "x", 0, now, "sending", 1, "crash test|L0|High", now))
    n = notify.requeue_unfinished()
    t_end = time.time() + 6
    while time.time() < t_end and db.q("SELECT status FROM notifications WHERE id=?", (cur.lastrowid,))[0]["status"] not in notify.FINAL:
        time.sleep(0.3)
    st = db.q("SELECT status FROM notifications WHERE id=?", (cur.lastrowid,))[0]["status"]
    check("NOTIFY", "after a crash / restart, a message left 'sending' is picked up again and delivered",
          n >= 1 and st == "delivered", [n, st])
    # 12 Nov step 10: Telegram Acknowledge / Resolve buttons
    first_tg = [x for x in NET.calls if "api.telegram.org" in x[1] and "sendMessage" in x[1]]
    kb = [x[2].get("reply_markup") for x in first_tg if isinstance(x[2], dict) and x[2].get("reply_markup")]
    with TestClient(m.app):
        wrong = notify.handle_tg_callback({"data": "ack:evt_net1", "message": {"chat": {"id": -999}}, "from": {"username": "x"}})
        ok = notify.handle_tg_callback({"data": "ack:evt_net1", "message": {"chat": {"id": -100200}}, "from": {"username": "boss"}})
        again = notify.handle_tg_callback({"data": "ack:evt_net1", "message": {"chat": {"id": -100200}}, "from": {"username": "boss"}})
    ev = db.get_event("evt_net1")
    aud = db.q("SELECT user, action FROM audit_log WHERE target='evt_net1' ORDER BY id DESC LIMIT 1")
    check("NOTIFY", "Telegram: first message has Acknowledge / Resolve buttons; a press from the group acknowledges (audited), "
                    "another chat is refused, a second press changes nothing",
          kb and "ack:" in json.dumps(kb[0]) and "resolve:" in json.dumps(kb[0]) and wrong.startswith("Not allowed")
          and ok.startswith("Acknowledged") and again.startswith("Already") and ev["status"] == "acknowledged"
          and ev["acked_by"] == "telegram:boss" and aud and aud[0]["user"] == "telegram:boss", [wrong, ok, again, ev["status"], aud])
    set_creds(False)


def fire_call_repeat_tests():
    """12 Nov (prompt pack step 8): fire call at once, repeated every 60 s until Acknowledge / Resolve, a failed try again
    after 20 s, every attempt its own row. Simulated clock; nothing is really called."""
    from fastapi.testclient import TestClient
    set_creds(False)                                # calls end at once as 'not configured' (no provider is followed)
    t0 = time.time()
    with TestClient(m.app) as c:
        c.post("/api/events", json=fire_payload("evt_frep", [100, 100, 300, 300], t=t0))
    e = db.get_event("evt_frep")
    calls = lambda: db.q("SELECT id, status, queued_at, kind FROM notifications WHERE event_id='evt_frep' AND channel='call' ORDER BY id")
    t_w = time.time()
    while time.time() - t_w < 5 and any(r["status"] not in notify.FINAL for r in calls()):
        time.sleep(0.1)
    first = calls()
    check("CALL", "fire detected -> the phone call is queued within 5 s (no waiting for a level or an escalation timer)",
          e["status"] in ("open", "escalated") and first and first[0]["queued_at"] - t0 <= 5.0,
          [e["status"], [(r["status"], round(r["queued_at"] - t0, 2)) for r in first]])
    def run(now):                                   # the test runner already has an event loop: own thread
        th = threading.Thread(target=lambda: asyncio.run(m.fire_call_tick(now)))
        th.start(); th.join()
    db.x("UPDATE notifications SET status='sent', queued_at=? WHERE id=?", (t0, first[-1]["id"]))
    run(t0 + 30); n30 = len(calls())
    run(t0 + 61); n61 = len(calls())
    last = calls()[-1]
    db.x("UPDATE notifications SET status='failed', finished_at=? WHERE id=?", (t0 + 62, last["id"]))
    run(t0 + 70); n70 = len(calls())
    run(t0 + 83); n83 = len(calls())
    check("CALL", "not acknowledged -> called again every 60 s; busy / no answer / failed -> next try after 20 s (own log row)",
          n30 == len(first) and n61 == len(first) + 1 and n70 == n61 and n83 == n61 + 1
          and calls()[-1]["kind"].startswith("call #"), [len(first), n30, n61, n70, n83])
    db.x("UPDATE notifications SET status='sent' WHERE event_id='evt_frep' AND channel='call'")
    db.x("UPDATE events SET status='acknowledged' WHERE event_id='evt_frep'")
    run(t0 + 1000); n_ack = len(calls())
    wanted = notify.Dispatcher._call_still_wanted({"event_id": "evt_frep"}, {"kind": "call #9"})
    check("CALL", "Acknowledge stops the calls at once (no new call, a call still in the queue is not placed)",
          n_ack == n83 and wanted is False, [n_ack, wanted])
    msg = notify.build(e, "new alert", notify.load_policy())["call"]
    check("CALL", "call text: 'NETRAX fire alarm. Fire detected in <zone> by <camera> at <time>. Open the dashboard and acknowledge.'",
          msg.startswith("NETRAX fire alarm. Fire detected in Warehouse by camera cam_02 at ") and msg.endswith("Open the dashboard and acknowledge."), msg)


def zentrix_more_tests():
    """11 Nov (user): Zentrix opens every page / section / box and does every manual action - only on command."""
    from fastapi.testclient import TestClient
    from backend import agent
    ctx = {"zones": m.ZONES, "cameras": m.CAMERAS, "live": {"cameras": []}}
    ui = lambda t, role="admin": agent.plan(t, "t", role, ctx)["ui"]
    check("ZX", "opens Safety Intelligence sections by name (live sensors, factory map, alerts centre, device health, predictive)",
          ui("open live sensors") == [{"op": "navigate", "url": "/intelligence#sensors"}]
          and ui("hey zentrix open the factory map") == [{"op": "navigate", "url": "/intelligence#map"}]
          and ui("open the alerts centre") == [{"op": "navigate", "url": "/intelligence#alerts"}]
          and ui("go to device health") == [{"op": "navigate", "url": "/intelligence#devices"}]
          and ui("show predictive analytics") == [{"op": "navigate", "url": "/intelligence#predict"}]
          and ui("open worker management") == [{"op": "navigate", "url": "/intelligence#workers"}]
          and ui("open the report generator") == [{"op": "navigate", "url": "/admin#reports"}],
          [ui("open live sensors"), ui("open worker management")])
    p = agent.plan("show acknowledged alerts", "t", "admin", ctx)
    check("ZX", "control-room boxes + tabs ('show acknowledged fire alerts', 'show ppe history', 'show zones and people'); "
                "'show ACKNOWLEDGED alerts' only opens the list - it never acknowledges anything",
          ui("show acknowledged fire alerts") == [{"op": "box", "box": "fire", "tab": "ack"}]
          and ui("show ppe history") == [{"op": "box", "box": "ppe", "tab": "done"}]
          and ui("show zones and people") == [{"op": "box", "box": "zone", "tab": None}]
          and p["execute"] == [] and p["confirm"] is None and len(p["ui"]) == 2, p["ui"])
    os.environ["SENTINELEYE_POWER_FILE"] = os.path.join(TMP, "zx_power.json")
    cam = next(c for c, v in m.CAMERAS.items() if v.get("enabled", True) is not False)
    num = int("".join(ch for ch in cam if ch.isdigit()))
    with TestClient(m.app) as c:
        z = lambda t: c.post("/api/zentrix", json={"text": t}).json()
        a = z(f"turn off camera {num}")
        before = next(x for x in c.get("/api/cameras").json()["cameras"] if x["camera_id"] == cam)["power"]
        b = z("confirm")
        after = next(x for x in c.get("/api/cameras").json()["cameras"] if x["camera_id"] == cam)["power"]
        on = z(f"switch on camera {num}")
        back = next(x for x in c.get("/api/cameras").json()["cameras"] if x["camera_id"] == cam)["power"]
        sil = z("silence the siren")
        test = z("test the phone call")
    os.environ["SENTINELEYE_POWER_FILE"] = os.path.join(TMP, "camera_power.json")
    check("ZX", "camera OFF by command asks first and switches only after 'confirm'; ON runs at once (same path + audit as the button)",
          a["confirm"] and before == "on" and b["done"] == 1 and after == "off" and on["done"] == 1 and back == "on",
          [a["reply"][:60], before, after, back])
    check("ZX", "siren silenced by command; a real test call needs confirmation first",
          sil["done"] == 1 and "siren" in sil["reply"].lower() and test["confirm"] and test["done"] == 0, [sil["reply"], test["reply"][:60]])


def zentrix_tests():
    """5 Nov: Zentrix command agent - chat / voice commands of the supervisor; acts ONLY on command."""
    from fastapi.testclient import TestClient
    from backend import agent

    def live(badges):
        m.EDGE.update(at=time.time(), state={"cameras": [{"camera_id": c, "status": "ONLINE", "fps": 5, "badge": b}
                                                         for c, b in badges.items()]})

    def zx(c, text):
        return c.post("/api/zentrix", json={"text": text}).json()

    def nav(text):
        ui = agent.plan(text, "t", "admin", {"zones": m.ZONES, "cameras": m.CAMERAS})["ui"]
        return ui[0] if ui else None
    check("ZX", "commands open pages and sections (wake word + misspellings: 'hey zentrix open the admin pannel')",
          nav("hey zentrix open the admin pannel") == {"op": "navigate", "url": "/admin"}
          and nav("Zentrix, open the report generator") == {"op": "navigate", "url": "/admin#reports"}
          and nav("go to hardware page") == {"op": "navigate", "url": "/hardware"}
          and nav("take me to the control panel") == {"op": "navigate", "url": "/"}
          and nav("open entrance dashboard") == {"op": "navigate", "url": "/entrance"}
          and nav("entrance denied today") is None,
          [nav("hey zentrix open the admin pannel"), nav("entrance denied today")])
    check("ZX", "camera commands: enlarge by zone or number, back to all cameras; a PPE question is not a camera command",
          nav("enlarge the welding bay camera") == {"op": "focus", "cam": "cam_01"}
          and nav("zentrix enlarge camera two") == {"op": "focus", "cam": "cam_02"}
          and nav("show all cameras") == {"op": "unfocus"}
          and nav("show helmet violations in the welding bay") is None,
          [nav("enlarge the welding bay camera"), nav("zentrix enlarge camera two")])
    with TestClient(m.app) as c:
        now = time.time()
        c.post("/api/events", json=ppe_payload("evt_zx_live", ["helmet", "vest"], t=now - 20))
        c.post("/api/events", json=ppe_payload("evt_zx_gone", ["gloves"], cam="cam_02", zone="warehouse", t=now - 300))
        live({"cam_01": "NO HELMET / VEST", "cam_02": None})
        a = zx(c, "hey zentrix pls analyse the recent alerts")
        byid = {x["event_id"]: x for x in a["analysis"]}
        st0 = (db.get_event("evt_zx_live")["status"], db.get_event("evt_zx_gone")["status"])
        check("ZX", "analyse = live verification: violation still on camera -> acknowledge; no longer seen -> resolve; "
                    "NOTHING changes without a command",
              byid["evt_zx_live"]["verdict"].startswith("STILL HAPPENING") and byid["evt_zx_live"]["recommend"] == "ack"
              and byid["evt_zx_gone"]["recommend"] == "resolve" and a["suggest"] and st0 == ("open", "open"),
              f"{byid['evt_zx_live']['verdict']} / {byid['evt_zx_gone']['verdict']} / {a['reply'][:120]}")
        live({"cam_01": "NO HELMET / VEST", "cam_02": None})
        b = zx(c, "apply")
        e1, e2 = db.get_event("evt_zx_live"), db.get_event("evt_zx_gone")
        check("ZX", "'apply' carries out the recommendations through the normal actions (audited: user + 'via Zentrix')",
              e1["status"] == "acknowledged" and e2["status"] == "resolved" and b["reply"].startswith("Done")
              and any("via Zentrix" in (x.get("note") or "") for x in e1["actions"]), b["reply"])
        c.post("/api/events", json=ppe_payload("evt_zx_a", ["helmet"], t=now - 3))
        c.post("/api/events", json=ppe_payload("evt_zx_b", ["vest"], cam="cam_02", zone="warehouse", t=now - 1))
        r1 = zx(c, "acknowlege the latest alert")
        check("ZX", "a single clear command runs at once ('acknowlege the latest alert' -> newest alert acknowledged)",
              db.get_event("evt_zx_b")["status"] == "acknowledged" and db.get_event("evt_zx_a")["status"] == "open"
              and r1["done"] == 1, r1["reply"])
        r2 = zx(c, "resolve all the alerts")
        unchanged = db.get_event("evt_zx_a")["status"] == "open"
        r3 = zx(c, "cancel")
        r4 = zx(c, "resolve all the alerts")
        r5 = zx(c, "yes confirm")
        check("ZX", "bulk resolve asks for confirmation first; 'cancel' changes nothing; 'confirm' resolves them all",
              r2["confirm"] and unchanged and "Cancelled" in r3["reply"] and db.get_event("evt_zx_a")["status"] == "resolved"
              and db.get_event("evt_zx_b")["status"] == "resolved" and r5["reply"].startswith("Done"),
              f"{r2['reply'][:90]} | {r5['reply'][:80]}")
        c.post("/api/events", json=ppe_payload("evt_zx_c", ["helmet"], t=now - 5))
        r6 = zx(c, "analyse the recent alerts and acknowledge them")
        check("ZX", "'analyse ... and acknowledge them' = analysis + the commanded action on exactly those alerts",
              "analyse" in r6["intents"] and "action" in r6["intents"] and db.get_event("evt_zx_c")["status"] == "acknowledged",
              r6["reply"][:140])
        c.post("/api/events", json=ppe_payload("evt_zx_d", ["helmet", "vest"], t=now - 2))
        c.post("/api/events", json=ppe_payload("evt_zx_e", ["mask"], cam="cam_02", zone="warehouse", t=now - 240))
        live({"cam_01": "NO HELMET / VEST", "cam_02": None})
        r7 = zx(c, "hey zentrix pls analyse the recenmt alerts and acknowlege or mark resolve")
        check("ZX", "'analyse the recent alerts and acknowledge OR resolve' (user's words, misspelt) -> each alert gets "
                    "the verified choice: still on camera = acknowledged, no longer seen = resolved",
              db.get_event("evt_zx_d")["status"] == "acknowledged" and db.get_event("evt_zx_e")["status"] == "resolved",
              r7["reply"][:160])
        q = zx(c, "any fire today?")
        check("ZX", "questions about the past are answered from the records (grounded history assistant)",
              q["history"] and q["history"]["grounded"] and "question" in q["intents"], q["reply"][:100])
    vw = agent.plan("resolve all alerts", "v", "viewer", {"zones": m.ZONES, "cameras": m.CAMERAS})
    vn = agent.plan("open admin", "v", "viewer", {"zones": m.ZONES, "cameras": m.CAMERAS})
    check("ZX", "a viewer can ask and move around but cannot change records",
          not vw["execute"] and not vw["confirm"] and "cannot" in vw["reply"] and vn["ui"][0]["url"] == "/admin", vw["reply"])
    ent = [agent._entrance_decision(agent.normalise(x)) for x in
           ("allow the person at the entrance", "refuse entry at the gate", "acknowledge the alert")]
    check("ZX", "entrance commands: allow / refuse the person at the entrance", ent == ["approved", "denied", None], ent)
    m.EDGE.update(at=0.0, state=None)
    import numpy as _np
    from edge.stream import black_picture
    check("12", "camera health: a black picture (covered lens / stuck USB camera, seen live 5 Nov) is reported as such",
          black_picture(_np.zeros((480, 640, 3), _np.uint8)) and not black_picture(_np.full((480, 640, 3), 90, _np.uint8))
          and not black_picture(None))


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
    t_w = time.time()
    while time.time() - t_w < 15 and not db.q("SELECT id FROM notifications WHERE event_id='evt_t13' AND status='waiting'"):
        time.sleep(0.2)
    rows = db.q("SELECT * FROM notifications WHERE event_id='evt_t13'")
    tg = [r for r in rows if r["channel"] == "telegram"]
    # 12 Nov (prompt pack step 9): no internet is NOT a failed try - the message waits and is resent when it returns
    check("13", "internet down: Telegram message kept and WAITING for the internet (reason shown, not a failed try) - never 'delivered'",
          tg and tg[0]["status"] == "waiting" and int(tg[0]["attempts"] or 0) == 0 and "network" in (tg[0]["error"] or ""),
          tg and (tg[0]["status"], tg[0]["attempts"], tg[0]["error"]))
    h = m.system_health()
    check("13", "system health shows the channel waiting for the internet",
          any(c["name"] == "Alerts: telegram" and c["state"] == "warn" and "waiting for internet" in c["message"] for c in h["components"]))
    NET.mode["telegram"] = "ok"
    set_creds(False)
    await m.ingest_event(ppe_payload("evt_t13b", ["vest"], "High", "cam_01", "welding_bay"))
    rows = wait_notifs("evt_t13b", 1)
    check("13", "no credentials -> NOT CONFIGURED (clearly labelled), nothing claimed as sent",
          rows and all(r["status"] == "not_configured" and not r["delivered"] for r in rows), [(r["channel"], r["status"]) for r in rows])
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
    zentrix_tests()
    zentrix_more_tests()
    intelligence_tests()
    camera_power_tests()
    call_tests()
    fire_call_repeat_tests()
    reliable_delivery_tests()
    ppe_accuracy_alert_tests()
    phone_follow_test()
    pm_gps_test()
    cap_vs_helmet_test()
    eyewear_test()
    polish_data_tests()
    entrance_object_tests()
    motion_tests()
    usb_order_test()
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
    # 11 Nov: a timed-out camera must NOT queue more frames (startup pile-up = every camera fell behind / 'cameras closed')
    from edge.engine_proc import _Req
    stuck = pe.inflight["cam_slow"] = _Req()          # an answer that has not come back yet (deterministic: no timing)
    n0, errs = pe.n, 0
    for _ in range(3):
        try:
            pe.infer("cam_slow", _np.full((48, 64, 3), 5, _np.uint8), False, timeout=0.05)
        except TimeoutError:
            errs += 1
    queued = pe.n - n0
    stuck.done.set()                                   # the late answer arrives -> the camera goes on normally
    after = pe.infer("cam_slow", _np.full((48, 64, 3), 9, _np.uint8), False)
    queued += 0 if pe.n - n0 == 1 else 99
    check("12", "AI process: a camera whose request timed out waits for it - no pile-up of stale frames, next frame correct",
          queued == 0 and errs == 3 and abs(after["persons"][0]["mean"] - 9) < 1e-6, f"queued {queued}, timeouts {errs}")
    pe.close()
    from edge import run_edge as _re
    _env = os.environ.pop("SENTINELEYE_EDGE_MULTI", None)
    first = _re._single_instance(port=48799)
    hold, _re._ONE = _re._ONE, None
    second = _re._single_instance(port=48799)
    hold.close()
    if _env is not None:
        os.environ["SENTINELEYE_EDGE_MULTI"] = _env
    check("12", "edge: a second copy is refused (two copies took the cameras from each other)", first and not second,
          f"first {first}, second {second}")
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
