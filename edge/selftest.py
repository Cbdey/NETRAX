import time
import os
"""Logic self-test - no camera or model needed.   Run:  python -m edge.selftest
Checks: per-item association, unverifiable state, grace period, one alert per
violation (no spam), resolve, zone-based severity, and the orange-vest fire case."""
import numpy as np

from .association import evaluate
from .config import load_all
from .fire_verify import FireVerifier
from .state_machine import PPEStateMachine

S, ZONES, _ = load_all()
S["fire"]["debug_log"] = None          # tests must not write to data/fire_debug.log
W, H = 1280, 720
risk = lambda z: ZONES[z]["risk"]
PERSON = {"tid": 7, "bbox": [500, 100, 700, 600], "conf": 0.9}
HELMET = {"cls": "helmet", "conf": 0.8, "bbox": [560, 90, 640, 150]}
VEST = {"cls": "vest", "conf": 0.8, "bbox": [520, 250, 680, 380]}
GLOVES = {"cls": "gloves", "conf": 0.6, "bbox": [500, 420, 540, 460]}
BOOTS = {"cls": "boots", "conf": 0.7, "bbox": [540, 560, 600, 605]}   # safety shoes
ok_all = True
from .zones import ZoneMap as _ZoneMap
# same as live with the v2 model only (goggles need the v3 aux model)
_JUDGED = _ZoneMap({"default_zone": "welding_bay"}, ZONES,
                   set(S["models"].get("trusted_items") or []) & {"helmet", "vest", "gloves", "mask"})


def check(name, cond):
    global ok_all
    ok_all &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name)


def simulate(items, zone, seconds, fps=10, sm=None, t0=1000.0):
    sm = sm or PPEStateMachine(S["state_machine"])
    req = {7: _JUDGED.required(zone)}     # same rule as the live pipeline (settings trusted_items)
    acts = []
    for i in range(int(seconds * fps)):
        st, _ = evaluate([PERSON], items, req, W, H, S["association"])
        acts += sm.update("cam_01", [PERSON], st, {7: zone}, risk, W, H, now=t0 + i / fps)
    return acts, sm, st


def alarms(acts):
    """open / update / resolve only - the one-off "PPE complete" info is not an alarm."""
    return [a for a in acts if a["action"] != "info"]


# 1. association
st = evaluate([PERSON], [HELMET, VEST], {7: ["helmet", "vest", "gloves"]}, W, H, S["association"])[0][7]
check("helmet+vest compliant, gloves missing", st["helmet"][0] == "compliant"
      and st["vest"][0] == "compliant" and st["gloves"][0] == "missing")
small = dict(PERSON, bbox=[500, 100, 530, 150])
st = evaluate([small], [], {7: ["helmet"]}, W, H, S["association"])[0][7]
check("tiny person -> unverifiable (no alert possible)", st["helmet"][0] == "unverifiable")
cut = dict(PERSON, bbox=[500, 300, 700, 720])
st = evaluate([cut], [HELMET], {7: ["boots"]}, W, H, S["association"])[0][7]
check("feet cut by frame edge -> boots unverifiable", st["boots"][0] == "unverifiable")

# 2. grace period + exactly one alert over 5 minutes of non-compliance
acts, _, _ = simulate([VEST, GLOVES, BOOTS], "welding_bay", 1.0)
check("no alert inside the first 1 s in a HIGH-risk zone (N-of-M + grace)", not acts)
acts, _, _ = simulate([GLOVES, BOOTS], "warehouse", 2.0)
check("no alert inside the first 2 s in a MEDIUM-risk zone (N-of-M + grace)", not acts)


def first_open(items, zone):
    a, _, _ = simulate(items, zone, 6)
    t = [x["started_at"] for x in a if x["action"] == "open"]
    return (t[0] - 1000.0) if t else None


t_hi, t_med = first_open([VEST, GLOVES, BOOTS], "welding_bay"), first_open([VEST, GLOVES, BOOTS], "warehouse")
check(f"zone priority: high-risk zone alerts first ({t_hi:.1f} s) before medium-risk ({t_med:.1f} s)",
      t_hi is not None and t_med is not None and t_hi < t_med and t_hi <= 2.5)   # one decision per look: ~2.2 s high, ~3.1 s medium
acts, _, _ = simulate([VEST, GLOVES, BOOTS], "welding_bay", 300)
opens = [a for a in acts if a["action"] == "open"]
check(f"5 min without helmet -> exactly 1 alert (got {len(opens)})", len(opens) == 1)
check("missing helmet in welding bay -> High", opens and opens[0]["severity"] == "High")

# 3. zone-dependent severity: gloves missing in warehouse vs welding bay
acts, _, _ = simulate([HELMET, VEST, BOOTS], "welding_bay", 10)
wb = [a for a in acts if a["action"] == "open"]
check("no gloves in welding bay -> High", wb and wb[0]["severity"] == "High")
acts, _, _ = simulate([HELMET, VEST, BOOTS], "warehouse", 10)
check("no gloves in warehouse -> no alert (not required there)", not alarms(acts))

# 4. two items missing -> one event listing both
acts, _, _ = simulate([GLOVES, BOOTS], "warehouse", 10)
o = [a for a in acts if a["action"] in ("open", "update")]
check("helmet+vest missing -> one event with both items",
      o and set(o[-1]["items_missing"]) == {"helmet", "vest"}
      and len([a for a in acts if a["action"] == "open"]) == 1)

# 5. worker puts helmet on -> event resolves
acts, sm, _ = simulate([VEST, GLOVES, BOOTS], "welding_bay", 10)
acts2, _, _ = simulate([HELMET, VEST, GLOVES, BOOTS], "welding_bay", 5, sm=sm, t0=1010.0)
check("helmet put back on -> event resolved", any(a["action"] == "resolve" for a in acts2))

# 6. helmet added before grace ends -> no alert
acts, sm, _ = simulate([VEST, GLOVES, BOOTS], "welding_bay", 0.8)
acts2, _, _ = simulate([HELMET, VEST, GLOVES, BOOTS], "welding_bay", 10, sm=sm, t0=1000.8)
check("helmet added before grace ends -> no alert", not alarms(acts + acts2))

#    regression (live test): worker fixed the helmet, took it off again -> no new alert for 60 s
acts, sm, _ = simulate([VEST, GLOVES], "welding_bay", 4)                            # alert 1
acts += simulate([HELMET, VEST, GLOVES], "welding_bay", 8, sm=sm, t0=1004.0)[0]     # fixed, resolved
acts += simulate([VEST, GLOVES], "welding_bay", 4, sm=sm, t0=1012.0)[0]             # off again
opens = [a for a in acts if a["action"] == "open"]
check(f"same worker takes the helmet off again after fixing it -> new alert (opens={len(opens)})",
      len(opens) == 2)
acts, sm, _ = simulate([VEST, GLOVES], "welding_bay", 4)
acts += simulate([HELMET, VEST, GLOVES], "welding_bay", 2.2, sm=sm, t0=1004.0)[0]   # resolves at ~1006
acts += simulate([VEST, GLOVES], "welding_bay", 3, sm=sm, t0=1006.2)[0]             # flaps back < rearm_s
check("helmet flapping on/off within rearm_s -> no alert spam",
      len([a for a in acts if a["action"] == "open"]) == 1)

# 7. fire / smoke look-alikes and real fire
ORANGE = (0, 140, 255)          # BGR flame-ish orange


def run_fire(cam, make_frame, box_fn, persons, cls="Fire", n=60, conf=0.8):
    fv = FireVerifier(S["fire"])
    acts = []
    for i in range(n):
        a, _ = fv.update(cam, make_frame(i), [{"cls": cls, "conf": conf, "bbox": box_fn(i)}], persons, 5000 + i / 5)
        acts += a
    return acts


def painted(box, colour, noise=False, texture=False):
    def f(i):
        img = np.full((H, W, 3), 60, np.uint8)
        x1, y1, x2, y2 = map(int, box(i))
        img[y1:y2, x1:x2] = colour
        rng = np.random.default_rng(i)
        if noise:
            img[y1:y2, x1:x2, 1] = rng.integers(60, 200, (y2 - y1, x2 - x1))   # flicker
        if texture:
            # brightness texture only: real smoke is grey, it varies in brightness, not in colour
            img[y1:y2, x1:x2] = np.clip(img[y1:y2, x1:x2].astype(np.int16)
                                        + rng.integers(-35, 35, (y2 - y1, x2 - x1, 1)), 0, 255).astype(np.uint8)
        return img
    return f


vest_box = lambda i: [540, 260, 660, 360]
acts = run_fire("c1", painted(vest_box, ORANGE, noise=True), vest_box, [PERSON["bbox"]])
check("orange vest / red clothes on a person -> NO fire alert", not acts)

flash_box = lambda i: [900, 100, 1000, 200]
acts = run_fire("c2", painted(flash_box, (255, 255, 255)), flash_box, [])
check("phone flash / white lamp -> rejected as fire (no flame colour)", not acts)
acts = run_fire("c3", painted(flash_box, (250, 250, 250)), flash_box, [], cls="Smoke")
check("phone flash / ceiling light -> rejected as smoke (overexposed)", not acts)

lamp_box = lambda i: [900, 300, 1000, 400]
acts = run_fire("c4", painted(lamp_box, ORANGE), lamp_box, [])
check("static orange machine / lamp (no flicker, no growth) -> NO fire alert", not acts)

grow_box = lambda i: [100, 100, 140 + i * 3, 140 + i * 3]
acts = run_fire("c5", painted(grow_box, ORANGE, noise=True), grow_box, [])
check("real flickering, growing fire -> confirmed alert", any(a["action"] == "open" for a in acts))
check("growing fire -> Critical", any(a["severity"] == "Critical" for a in acts))
check("one fire event only (no spam)", len([a for a in acts if a["action"] == "open"]) == 1)

smoke_box = lambda i: [300, 50, 340 + i * 3, 90 + i * 2]
acts = run_fire("c6", painted(smoke_box, (130, 130, 130), texture=True), smoke_box, [], cls="Smoke")
check("grey drifting smoke -> alert", any(a["action"] == "open" for a in acts))
acts = run_fire("c7", painted(smoke_box, (150, 150, 150)), smoke_box, [], cls="Smoke")
check("hand / cloth over the lens (blur, no texture) -> no smoke alert", not acts)

# 8. track ID switch that also drifts across a location-grid cell -> handover, not a duplicate
#    alert (regression: live webcam showed track 1 -> 3 with a fresh OPEN instead of a handover)
sm = PPEStateMachine(S["state_machine"])
P1 = {"tid": 7, "bbox": [500, 100, 700, 600], "conf": 0.9}
P2 = {"tid": 8, "bbox": [650, 100, 850, 600], "conf": 0.9}   # same person, box drifted right
acts = []
t0 = 2000.0
for i in range(45):
    st, _ = evaluate([P1], [], {7: ["helmet"]}, W, H, S["association"])
    acts += sm.update("cam_01", [P1], st, {7: "welding_bay"}, risk, W, H, now=t0 + i / 10)
for i in range(40):
    st, _ = evaluate([P2], [], {8: ["helmet"]}, W, H, S["association"])
    acts += sm.update("cam_01", [P2], st, {8: "welding_bay"}, risk, W, H, now=t0 + 4.5 + i / 10)
opens = [a for a in acts if a["action"] == "open"]
check(f"track ID switch across a grid cell -> handover, not a duplicate alert (got {len(opens)} opens)",
      len(opens) == 1)

# 9. PPE item validation: plain shirt is not a vest, cap-shaped box is not accepted as a helmet
from .validate import item_ok, size_ok
VCFG = S.get("validation", {})
shirt = {"cls": "vest", "conf": 0.7, "bbox": [520, 250, 680, 380]}
blue_shirt = np.full((H, W, 3), 60, np.uint8); blue_shirt[250:380, 520:680] = (140, 70, 40)
ok, why = item_ok(blue_shirt, PERSON["bbox"], shirt, VCFG)
check(f"plain blue shirt rejected as a vest ({why})", not ok)
hivis = np.full((H, W, 3), 60, np.uint8); hivis[250:380, 520:680] = (40, 190, 250)
ok, _ = item_ok(hivis, PERSON["bbox"], shirt, VCFG)
check("real hi-vis vest accepted", ok)
check("shirt-sized box rejected as a helmet",
      not size_ok(PERSON["bbox"], [520, 250, 680, 380], "helmet"))
check("helmet-sized box on the head accepted", size_ok(PERSON["bbox"], [560, 95, 640, 150], "helmet"))


# 10. close-up worker (person box cut by the bottom edge) wearing a helmet -> helmet accepted
#     (regression: bike helmet close to the webcam was rejected by the height ratio -> false alert)
close = [400, 300, 900, 720]
blank = np.full((H, W, 3), 60, np.uint8)
ok, why = item_ok(blank, close, {"cls": "helmet", "conf": 0.8, "bbox": [560, 280, 740, 400]}, VCFG)
check(f"close-up helmet (body cut by frame edge) accepted ({why})", ok)
ok, _ = item_ok(blank, close, {"cls": "helmet", "conf": 0.8, "bbox": [420, 450, 880, 720]}, VCFG)
check("close-up shirt-sized box still rejected as a helmet", not ok)

# 11. static look-alike (clothes on a hanger) detected as a weak 'person' -> never an alert
#     (regression: hanging shirt at conf ~0.57 raised NO HELMET)
from .tracker import TrackConfidence
tc, sm, acts = TrackConfidence(window=20), PPEStateMachine(S["state_machine"]), []
for i in range(100):
    cloth = tc.update([dict(PERSON, conf=0.57 + 0.04 * (i % 3 == 0))])
    st, _ = evaluate(cloth, [], {7: ["helmet"]}, W, H, S["association"])
    acts += sm.update("cam_01", cloth, st, {7: "welding_bay"}, risk, W, H, now=3000 + i / 10)
check("hanging clothes (weak person conf) -> unverifiable, no alert", not acts)
tc = TrackConfidence(window=20)
real = tc.update([dict(PERSON, conf=0.9)])
st, _ = evaluate(real, [], {7: ["helmet"]}, W, H, S["association"])
check("real person (strong conf) without helmet -> missing", st[7]["helmet"][0] == "missing")

# 12. response time: violation first seen -> alert decided (settings: N-of-M + grace)
acts, _, _ = simulate([VEST, GLOVES, BOOTS], "welding_bay", 5)
o = [a for a in acts if a["action"] == "open"][0]
tta = o["at"] - o["onset_at"]
check(f"time-to-alert {tta:.1f} s (<= 2.5 s; was ~4 s before tuning)", o["onset_at"] and tta <= 2.5)

# 13. safety shoes (logic kept for later; boots are not required in any zone right now)
#     regression: close-up upper body (webcam, legs off-screen) ended 12 px above the frame edge
#     and raised 'no boots' - real COCO box from data/snapshots/evt_68e2cd8e05.jpg (640x480)
closeup = dict(PERSON, bbox=[143, 51, 609, 468])
st = evaluate([closeup], [], {7: ["helmet", "boots"]}, 640, 480, S["association"])[0][7]
check("close-up upper body (feet off-screen) -> boots unverifiable, helmet still missing",
      st["boots"][0] == "unverifiable" and st["helmet"][0] == "missing")

# 14. fire / smoke on real inputs (regressions from live tests on 2026-09-23)
def flame_frame(box, core=0.45, rim=ORANGE):
    """Orange flickering flame with a white-hot core (what a webcam sees of a match / lighter)."""
    def f(i):
        img = np.full((H, W, 3), 60, np.uint8)
        x1, y1, x2, y2 = map(int, box(i))
        img[y1:y2, x1:x2] = rim
        rng = np.random.default_rng(i)
        img[y1:y2, x1:x2, 1] = rng.integers(60, 200, (y2 - y1, x2 - x1))
        cw, ch = int((x2 - x1) * core ** 0.5), int((y2 - y1) * core ** 0.5)
        cx, cy = (x1 + x2) // 2 + int(rng.integers(-3, 4)), (y1 + y2) // 2 + int(rng.integers(-3, 4))
        img[cy - ch // 2:cy + ch // 2, cx - cw // 2:cx + cw // 2] = (255, 255, 255)
        return img
    return f


hand_box = lambda i: [560, 300, 610, 370]            # held in the hand = inside the person box
acts = run_fire("c8", flame_frame(hand_box), hand_box, [PERSON["bbox"]])
check("flame held in the hand (on a person, glowing + flickering) -> fire alert",
      any(a["action"] == "open" for a in acts))
match_box = lambda i: [900, 400, 924, 436]           # tiny match flame, mostly white core
acts = run_fire("c9", flame_frame(match_box, core=0.6), match_box, [])
check("small match flame (white core, thin orange rim) -> fire alert", any(a["action"] == "open" for a in acts))
fv = FireVerifier(S["fire"])
d = {"cls": "Fire", "conf": 0.8, "bbox": [900, 100, 1000, 200]}
check("phone flash (white, no orange) still rejected as fire",
      not fv.colour_ok(painted(flash_box, (255, 255, 255))(0), d)[0])

plume = lambda i: [520 + (i % 2) * 70, 150, 600 + (i % 2) * 70, 260]   # wavering thin smoke
acts = run_fire("c10", painted(plume, (175, 175, 175), texture=True), plume, [PERSON["bbox"]], cls="Smoke")
check("thin agarbatti smoke wavering past a person -> stays ONE region, smoke alert",
      len([a for a in acts if a["action"] == "open"]) == 1)
hair = lambda i: [540, 100, 660, 180]
acts = run_fire("c11", painted(hair, (35, 35, 40), texture=True), hair, [PERSON["bbox"]], cls="Smoke")
check("dark hair detected as 'smoke' -> rejected (too dark)", not acts)

#   regression: all 25 'smoke' alerts from the live tests were faces / hair / phone flash.
#   Replay them from data/snapshots (skipped if the snapshots are not on this machine).
import json as _json, os as _os, sqlite3 as _sq
import cv2 as _cv2
from .config import path as _path
_dbp = _path("data", "sentineleye.db")
if _os.path.exists(_dbp):
    _fv, _n, _rej = FireVerifier(S["fire"]), 0, 0
    for _eid, _b in _sq.connect(_dbp).execute("SELECT event_id, bbox FROM events WHERE type='smoke'"):
        _p = _path("data", "snapshots", f"{_eid}.jpg")
        if _b and _os.path.exists(_p):
            # full verifier, 40 frames of the stored snapshot (persons unknown here, so the
            # person-box rule is not used: colour, skin and the static-smoke rule must do it)
            _img, _box = _cv2.imread(_p), _json.loads(_b)
            _v, _opened = FireVerifier(S["fire"]), False
            for _i in range(40):
                _a, _ = _v.update("replay", _img, [{"cls": "Smoke", "conf": 0.5, "bbox": _box}], [], 9000 + _i / 5)
                _opened |= any(x["action"] == "open" for x in _a)
            _n += 1
            _rej += not _opened
    if _n:
        check(f"live smoke false alarms (faces/hair/flash/bedsheet) rejected: {_rej}/{_n}", _rej == _n)

#   regression (4-camera soak test): the flame core of a fire was ALSO confirmed as smoke
fv, acts = FireVerifier(S["fire"]), []
core = [560, 300, 620, 360]
for i in range(60):
    img = flame_frame(lambda k: [540, 280, 640, 380])(i)
    img[300:360, 560:620] = 185                           # grey-white core, looks like smoke
    a, _ = fv.update("c12", img, [{"cls": "Fire", "conf": 0.8, "bbox": [540, 280, 640, 380]},
                                  {"cls": "Smoke", "conf": 0.7, "bbox": core}], [], 5000 + i / 5)
    acts += a
check("smoke box inside a fire box -> part of the fire, no separate smoke alert",
      any(x["type"] == "fire" for x in acts) and not any(x["type"] == "smoke" for x in acts))

# 17. privacy: snapshot retention deletes only images older than the limit
import tempfile as _tf
import time as _time
from .retention import purge
_d = _tf.mkdtemp()
for _n, _age in (("old_evt", 40), ("new_evt", 1)):
    _f = _os.path.join(_d, _n + ".jpg")
    open(_f, "wb").close()
    _t = _time.time() - _age * 86400
    _os.utime(_f, (_t, _t))
check("snapshot retention (30 days): old image deleted, recent one kept",
      purge(_d, 30) == ["old_evt"] and _os.path.exists(_os.path.join(_d, "new_evt.jpg")))

#   regression: unreachable phone camera hung cv2.VideoCapture > 60 s and slowed the webcam to 2.5 fps
from .stream import reachable
_t = _time.time()
check("unreachable network camera is detected quickly (no minute-long hang)",
      not reachable("http://192.0.2.1:8080/video", timeout=1.0) and _time.time() - _t < 3)

# 16. load-aware processing (plan 7.5): overloaded GPU -> low-risk zone slowed first, restored later
from .load import LoadManager


class _R:
    frame_ts, fps, target_fps = 1.0, 30.0, 10


class _W:
    def __init__(self, got): self.fps_target, self.got, self.reader = 10, got, _R()
    def processed_fps(self): return min(self.got, self.fps_target)
    def set_target(self, f): self.fps_target = f


wk = {"weld": _W(6), "ware": _W(6)}
lm, rk = LoadManager({}, 10), {"weld": "high", "ware": "medium"}.get
for _ in range(9):
    lm.step(wk, rk)
check(f"GPU overloaded -> warehouse (medium) slowed first, welding bay (high) kept "
      f"(ware={wk['ware'].fps_target}, weld={wk['weld'].fps_target})",
      wk["ware"].fps_target < 10 and wk["weld"].fps_target == 10)
wk["weld"].got = wk["ware"].got = 30
for _ in range(60):
    lm.step(wk, rk)
check("load drops -> FPS given back", wk["ware"].fps_target == 10)
slow_file = {"clip": _W(5)}
slow_file["clip"].reader.fps = 5.0                  # a 5 fps video is not overload
LoadManager({}, 10).step(slow_file, lambda c: "low")
check("5 fps video file is not treated as overload", slow_file["clip"].fps_target == 10)

# 15. one person = one ID (identity layer + state machine hand-over)
sm = PPEStateMachine(S["state_machine"])
acts = []
def run_p(tid, secs, t0):
    for i in range(int(secs * 10)):
        p = dict(PERSON, tid=tid, worker_id="W-001", worker_name="Ravi")
        st, _ = evaluate([p], [VEST, GLOVES], {tid: ["helmet", "vest", "gloves"]}, W, H, S["association"])
        acts.extend(sm.update("cam_01", [p], st, {tid: "welding_bay"}, risk, W, H, now=t0 + i / 10))
run_p(5, 3, 7000)                                   # tracker id 5, alert opens
sm.rename("cam_01", 5, 100001); run_p(100001, 2, 7003)   # recognised as W-001
run_p(9, 0.4, 7006)                                 # left for 1 s, came back as tracker id 9
sm.rename("cam_01", 9, 100001); run_p(100001, 3, 7006.4) # recognised again
opens = [a for a in acts if a["action"] == "open"]
check(f"same worker re-enters with a new tracker id -> same alert, no duplicate (opens={len(opens)})",
      len(opens) == 1 and opens[0]["worker_id"] == "W-001")

from .identity import IdentityService
rng = np.random.default_rng(1)
unit = lambda v: (v / np.linalg.norm(v)).astype(np.float32)
E1, E2, E3 = unit(rng.normal(size=128)), unit(rng.normal(size=128)), unit(rng.normal(size=128))


class FakeFaces:
    """Stands in for YuNet + SFace: one face at the person's head with a chosen embedding."""
    emb = E1
    def faces(self, img):
        return [np.array([570, 120, 60, 60] + [0] * 10 + [0.95], np.float32)]
    def embed(self, img, face):
        return unit(self.emb + rng.normal(scale=0.03, size=128))


ff = FakeFaces()
ids = IdentityService({"enabled": True}, backend_url=None, engine=ff)
ids.set_gallery([{"worker_id": "W-001", "name": "Ravi", "embeddings": [E1.tolist()]},
                 {"worker_id": "W-002", "name": "Priya", "embeddings": [E2.tolist()]}])
frame = np.zeros((H, W, 3), np.uint8)
def seen_as(tid, n=8):
    for k in range(n):
        ps = ids.update("cam_01", frame, [dict(PERSON, tid=tid)], k)
    return ps[0]["worker_id"]
check("registered worker recognised -> W-001", seen_as(11) == "W-001")
check("same worker, new tracker id -> still W-001 (one person, one ID)", seen_as(12) == "W-001")
ff.emb = E2
check("second registered worker -> W-002", seen_as(13) == "W-002")
ff.emb = E3
v1 = seen_as(14)
check(f"unregistered person -> visitor id ({v1})", v1 and v1.startswith("V-"))
check("same unregistered person on a new track -> same visitor id", seen_as(15) == v1)
#   regression: webcam close-up - face low in a body-cut person box was never attached to the person
low_face = np.array([300, 300, 146, 146] + [0] * 11, np.float32)
check("close-up: face low in the person box still belongs to that person",
      IdentityService._owner(low_face, [dict(PERSON, bbox=[143, 51, 609, 480])]) is not None)
ids.set_gallery([{"worker_id": "W-002", "name": "Priya", "embeddings": [E2.tolist()]}])
ff.emb = E1
check("deleted worker is no longer recognised as W-001", seen_as(16) != "W-001")

# --- step 1 (v3 prep): per-class colours, untrusted items never 'missing'
from .annotate import CLASS_COLOURS, draw as _draw
from .zones import ZoneMap as _ZM
_ppe = ["helmet", "vest", "gloves", "boots", "goggles", "mask", "Fire", "Smoke"]
check("every PPE item, fire and smoke has its own box colour",
      len({CLASS_COLOURS[c] for c in _ppe}) == len(_ppe))
_zm = _ZM({"default_zone": "welding_bay"}, ZONES, ["helmet", "vest", "gloves", "mask"])
_req = _zm.required("welding_bay")
check(f"goggles / safety shoes not judged by the v2 model -> skipped, never 'missing' ({_req})",
      "goggles" not in _req and "boots" not in _req and "helmet" in _req)
_img = _draw(np.zeros((240, 320, 3), np.uint8), [], {}, [{"cls": "helmet", "conf": 0.9, "bbox": [10, 10, 60, 40]}],
             [("Smoke", [100, 100, 200, 200], "confirmed", 0.6)], [], parts=[{"cls": "hand", "conf": 0.5, "bbox": [5, 100, 30, 130]}])
check("helmet box drawn in yellow, smoke in white", tuple(_img[10, 30]) == CLASS_COLOURS["helmet"]
      and tuple(_img[150, 100]) == CLASS_COLOURS["Smoke"])

# --- v3 hybrid: evidence rule (bare hand / face seen) + dark-scene smoke gate
_req = {7: ["helmet", "vest", "gloves", "goggles"]}
_HAND = {"cls": "hand", "conf": 0.7, "bbox": [505, 425, 535, 455]}
_FACE = {"cls": "face", "conf": 0.8, "bbox": [570, 110, 630, 170]}
_GOG = {"cls": "goggles", "conf": 0.7, "bbox": [575, 120, 625, 135]}
st, _ = evaluate([PERSON], [HELMET, VEST], _req, W, H, S["association"], parts=[_HAND, _FACE])
check("v3: bare hand seen, no glove -> gloves MISSING; face seen, no goggles -> goggles MISSING",
      st[7]["gloves"][0] == "missing" and st[7]["goggles"][0] == "missing")
st, _ = evaluate([PERSON], [HELMET, VEST], _req, W, H, S["association"], parts=[])
check("v3: hands in pockets / face turned away -> gloves + goggles UNVERIFIABLE (no false alarm)",
      st[7]["gloves"][0] == "unverifiable" and st[7]["goggles"][0] == "unverifiable")
st, _ = evaluate([PERSON], [HELMET, VEST, GLOVES, _GOG], _req, W, H, S["association"], parts=[_FACE])
check("v3: gloves and goggles worn -> compliant", st[7]["gloves"][0] == "compliant" and st[7]["goggles"][0] == "compliant")
_P2 = {"tid": 8, "bbox": [520, 140, 720, 640], "conf": 0.9}      # second worker standing just behind
st, _ = evaluate([PERSON, _P2], [HELMET, VEST, GLOVES], {7: ["gloves"], 8: ["gloves"]}, W, H, S["association"],
                 parts=[{"cls": "hand", "conf": 0.7, "bbox": [690, 470, 715, 500]}])
check("two workers in a line: worker 1's glove is not given to worker 2 (bare hand -> worker 2 missing)",
      st[7]["gloves"][0] == "compliant" and st[8]["gloves"][0] == "missing")
_fv = FireVerifier(S["fire"])
_dark = np.full((H, W, 3), 25, np.uint8)
_a = []
for i in range(60):
    a, _ = _fv.update("dk", _dark, [{"cls": "Smoke", "conf": 0.45, "bbox": [300, 200, 500, 400]}], [], 7000 + i / 5)
    _a += a
check("dark room: weak 'smoke' (conf 0.45, sensor noise) -> NO alarm", not _a)

# --- safety shoes: foot seen + classifier says normal shoe -> missing; safety shoe -> compliant; no foot -> can't tell
_FOOT = {"cls": "foot", "conf": 0.7, "bbox": [545, 560, 600, 600]}
_SAFE = {"cls": "boots", "conf": 0.8, "bbox": [545, 560, 600, 600]}
st, _ = evaluate([PERSON], [], {7: ["boots"]}, W, H, S["association"], parts=[_FOOT])
check("shoes: foot seen, normal shoe -> boots MISSING", st[7]["boots"][0] == "missing")
st, _ = evaluate([PERSON], [_SAFE], {7: ["boots"]}, W, H, S["association"], parts=[_FOOT])
check("shoes: safety shoe -> compliant", st[7]["boots"][0] == "compliant")
st, _ = evaluate([PERSON], [], {7: ["boots"]}, W, H, S["association"], parts=[])
check("shoes: feet not seen (desk, cut off) -> UNVERIFIABLE, no alert", st[7]["boots"][0] == "unverifiable")

# --- pose keypoints: right owner when two workers stand in a line; hidden body part -> can't tell
def _kps(x0, y0, h, hide=()):
    """17 COCO keypoints for an upright person whose box top-left is (x0, y0) and height h."""
    pts = {0: (0.0, 0.08), 1: (-0.02, 0.07), 2: (0.02, 0.07), 3: (-0.04, 0.08), 4: (0.04, 0.08),
           5: (-0.12, 0.20), 6: (0.12, 0.20), 7: (-0.15, 0.35), 8: (0.15, 0.35), 9: (-0.16, 0.50),
           10: (0.16, 0.50), 11: (-0.08, 0.52), 12: (0.08, 0.52), 13: (-0.08, 0.75), 14: (0.08, 0.75),
           15: (-0.07, 0.97), 16: (0.07, 0.97)}
    cx = x0 + 0.5 * h * 0.4
    return [[cx + dx * h, y0 + dy * h, 0.0 if k in hide else 0.9] for k, (dx, dy) in sorted(pts.items())]
_A = {"tid": 21, "bbox": [500, 100, 700, 600], "conf": 0.9, "kps": _kps(500, 100, 500)}
_B = {"tid": 22, "bbox": [560, 120, 760, 620], "conf": 0.9, "kps": _kps(560, 120, 500)}     # overlapping, just behind
_ga = _A["kps"][9]; _gb = _B["kps"][10]
_GA = {"cls": "gloves", "conf": 0.7, "bbox": [_ga[0] - 12, _ga[1] - 12, _ga[0] + 12, _ga[1] + 12]}
_HB = {"cls": "hand", "conf": 0.7, "bbox": [_gb[0] - 12, _gb[1] - 12, _gb[0] + 12, _gb[1] + 12]}
st, _ = evaluate([_A, _B], [_GA], {21: ["gloves"], 22: ["gloves"]}, W, H, S["association"], parts=[_HB])
check("pose: two workers in a line - glove goes to worker A (wrist), bare hand to worker B -> A ok, B missing",
      st[21]["gloves"][0] == "compliant" and st[22]["gloves"][0] == "missing")
_Bh = dict(_B, kps=_kps(560, 120, 500, hide=(9, 10)))
st, _ = evaluate([_A, _Bh], [_GA], {21: ["gloves"], 22: ["gloves"]}, W, H, S["association"], parts=[])
check("pose: worker B's hands hidden behind worker A -> B gloves UNVERIFIABLE (no false alarm)",
      st[22]["gloves"][0] == "unverifiable")
_back = dict(_A, kps=_kps(500, 100, 500, hide=(0, 1, 2, 3, 4)))      # seen from behind: no face points
st, _ = evaluate([_back], [], {21: ["helmet", "goggles"]}, W, H, S["association"], parts=[])
check("pose: worker seen from behind -> helmet still judged (missing), goggles can't be judged",
      st[21]["helmet"][0] == "missing" and st[21]["goggles"][0] == "unverifiable")
# 12 Oct: live false alarms 10 Oct - a 'smoke' box that is exactly the seated worker (white T-shirt)
shirt_p = [130, 90, 520, 470]
acts = run_fire("c20", painted(lambda i: shirt_p, (200, 200, 200), texture=True), lambda i: shirt_p, [shirt_p], cls="Smoke")
check("smoke box = the person box (white T-shirt) -> NO smoke alarm", not acts)
bed = [60, 390, 470, 630]
acts = run_fire("c21", lambda i: painted(lambda k: bed, (150, 160, 150), texture=True)(0), lambda i: bed, [], cls="Smoke")
check("static grey bedsheet seen as 'smoke' (never moves) -> NO smoke alarm", not acts)

# 12 Oct: camera pair - laptop webcam (zone A) OR USB webcam (zone C), never both at once
import tempfile as _tf
from edge.share import ShareGroups, write_choice
from edge.stream import StreamReader
os.environ["SENTINELEYE_SHARE_FILE"] = os.path.join(_tf.mkdtemp(), "share.json")
_now = [1000.0]
_cams = {"cam_01": {"share": "webcams"}, "cam_02": {}, "cam_04": {"share": "webcams"}}
sg = ShareGroups(_cams, failover_s=15, clock=lambda: _now[0])
check("pair: only the first webcam holds the slot, the other is on standby, the phone is free",
      sg.allowed("cam_01") and not sg.allowed("cam_04") and sg.allowed("cam_02"))
_now[0] += 3; sg.step({"cam_01": _now[0]})
write_choice("webcams", "cam_04"); _now[0] += 1; sg.step({"cam_01": _now[0]})
check("pair: 'Use this camera' on the USB tile -> USB gets the slot, laptop webcam released",
      sg.allowed("cam_04") and not sg.allowed("cam_01"))
_now[0] += 16; m = sg.step({"cam_01": 0, "cam_04": 0})
check("pair: USB webcam unplugged (no picture 15 s) -> laptop webcam takes over automatically",
      sg.allowed("cam_01") and not sg.allowed("cam_04") and m)
_now[0] += 5; sg.step({"cam_01": _now[0] - 1})
check("pair: working camera is never switched away", sg.allowed("cam_01"))
_r = StreamReader("cam_04", 1, 10)
_r.allowed = lambda: False
check("pair: standby camera reports STANDBY (grey tile, no OFFLINE alarm)", _r.health(10) == "STANDBY")
del os.environ["SENTINELEYE_SHARE_FILE"]

# 12 Oct: restricted zone (zones.yaml allowed_workers) - camera side
from edge.access import AccessGuard
_zones = {"welding_bay": {"risk": "high", "allowed_workers": ["W-001", "W-003"]}, "warehouse": {"risk": "medium"}}
ag = AccessGuard(_zones)
def _p(tid, wid, kind="worker", name=None):
    return {"tid": tid, "bbox": [100, 100, 300, 600], "conf": 0.9, "worker_id": wid, "worker_name": name, "identity": kind}
acts = []
for i in range(20):
    acts += ag.update("cam_01", [_p(1, "W-001", name="Chinmaya"), _p(2, "V-001", "visitor")], {1: "welding_bay", 2: "welding_bay"}, 2000 + i * 0.1)
check("restricted zone: allowed worker (Chinmaya) + unregistered visitor -> no access alert", not acts)
acts = []
for i in range(20):
    acts += ag.update("cam_01", [_p(3, "W-004", name="Anuska")], {3: "welding_bay"}, 2010 + i * 0.1)
opens = [a for a in acts if a["action"] == "open"]
check("restricted zone: other registered worker (Anuska) -> exactly 1 HIGH 'unauthorized_entry' alert",
      len(opens) == 1 and opens[0]["type"] == "unauthorized_entry" and opens[0]["severity"] == "High"
      and opens[0]["worker_id"] == "W-004")
acts = ag.update("cam_01", [], {}, 2018)
check("restricted zone: worker leaves (5 s unseen) -> alert resolved", any(a["action"] == "resolve" for a in acts))
acts = ag.update("cam_01", [_p(4, "W-004")], {4: "warehouse"}, 2020)
check("same worker in an open zone -> no access alert", not acts)

# 12 Oct: GPU priority gate - high-risk camera served before a waiting medium/low one
import threading as _th
from edge.priority import PriorityGate
g, order = PriorityGate(max_wait_s=5), []
def _run(rank, name):
    with g.slot(rank):
        order.append(name)
with g.slot(1):                                  # GPU busy (a medium camera)
    ts = [_th.Thread(target=_run, args=(2, "low")), _th.Thread(target=_run, args=(1, "medium")),
          _th.Thread(target=_run, args=(0, "high"))]
    for t in ts:
        t.start(); time.sleep(0.05)
for t in ts:
    t.join(2)
check(f"GPU priority: waiting cameras served high -> medium -> low ({' -> '.join(order)})", order == ["high", "medium", "low"])
g2, order2 = PriorityGate(max_wait_s=0.1), []
with g2.slot(0):
    t_low = _th.Thread(target=lambda: (g2.slot(2).__enter__(), order2.append("low")), daemon=True)
    t_low.start(); time.sleep(0.3)
    t_hi = _th.Thread(target=lambda: (g2.slot(0).__enter__(), order2.append("high")), daemon=True)
    t_hi.start(); time.sleep(0.05)
time.sleep(0.2)
check("GPU priority: a low-risk camera that waited too long is not starved", order2[:1] == ["low"])

# 20. ONE DECISION PER LOOK (12 Oct, user: "analyse what the worker wears, then ONE alarm, not one per item")
kinds = lambda acts, k: [a for a in acts if a["action"] == k]
acts, _, _ = simulate([BOOTS], "welding_bay", 30)                  # no helmet, no vest, no gloves
o = kinds(acts, "open")
check("worker walks in without helmet, vest, gloves -> ONE alarm listing all three, no follow-up updates",
      len(o) == 1 and set(o[0]["items_missing"]) == {"helmet", "vest", "gloves"} and not kinds(acts, "update"))
acts, _, _ = simulate([HELMET, VEST, GLOVES, BOOTS], "welding_bay", 60)
check("fully equipped worker -> ONE 'PPE complete' info in 60 s, no alarm",
      len(kinds(acts, "info")) == 1 and kinds(acts, "info")[0]["type"] == "ppe_ok" and not alarms(acts))
acts, sm, _ = simulate([HELMET, VEST, GLOVES, BOOTS], "welding_bay", 5)       # complete first
acts2, _, _ = simulate([GLOVES, BOOTS], "welding_bay", 10, sm=sm, t0=1005.0)  # then takes helmet + vest off
o = kinds(acts2, "open")
check("same worker then takes helmet AND vest off -> ONE new alarm with both (camera re-analyses first)",
      len(o) == 1 and set(o[0]["items_missing"]) == {"helmet", "vest"} and not kinds(acts2, "update"))
acts, sm, _ = simulate([HELMET, VEST, GLOVES, BOOTS], "welding_bay", 5)
acts2, sm, _ = simulate([VEST, GLOVES, BOOTS], "welding_bay", 0.3, sm=sm, t0=1005.0)   # helmet off ...
acts2 += simulate([GLOVES, BOOTS], "welding_bay", 10, sm=sm, t0=1005.3)[0]           # ... vest 0.3 s later
o = kinds(acts2, "open")
check("helmet and vest taken off 0.3 s apart -> still ONE alarm with both, not two alarms",
      len(o) == 1 and set(o[0]["items_missing"]) == {"helmet", "vest"} and not kinds(acts2, "update"))
acts3, _, _ = simulate([HELMET, VEST, GLOVES, BOOTS], "welding_bay", 8, sm=sm, t0=1015.3)
check("puts everything back on -> ONE resolve, no extra alarm",
      len(kinds(acts3, "resolve")) == 1 and not kinds(acts3, "open") + kinds(acts3, "update"))
#    the name: while the face is still being matched, the alarm waits (max id_wait_s) so it says WHO it is
sm, acts = PPEStateMachine(S["state_machine"]), []
for i in range(60):
    p = dict(PERSON, identity=None) if i < 26 else dict(PERSON, identity="worker", worker_id="W-001", worker_name="Ravi")
    st, _ = evaluate([p], [VEST, GLOVES], {7: ["helmet", "vest", "gloves"]}, W, H, S["association"])
    acts += sm.update("cam_01", [p], st, {7: "welding_bay"}, risk, W, H, now=1000 + i / 10)
o = kinds(acts, "open")
check("face still being matched -> the ONE alarm waits for the name (W-001), not 'unregistered' then a 2nd call",
      len(o) == 1 and o[0]["worker_id"] == "W-001" and o[0]["at"] - 1000 <= 3.8)
#    face ID mix-ups: weak look-alike is not named; one odd frame never renames a worker
ids = IdentityService(S["identity"] | {"debug_log": None}, backend_url=None, engine=ff)
ids.set_gallery([{"worker_id": "W-001", "name": "Ravi", "embeddings": [E1.tolist()]},
                 {"worker_id": "W-002", "name": "Priya", "embeddings": [E2.tolist()]}])
ff.emb = unit(0.42 * E1 + np.sqrt(1 - 0.42 ** 2) * E3)                     # ~0.4 alike, like two different people
check("look-alike face (similarity ~0.4, below 0.45) is NOT named W-001", seen_as(21) != "W-001")
ff.emb = E1
named = seen_as(22)
ff.emb = E2
ids.update("cam_01", frame, [dict(PERSON, tid=22)], 0)                      # one frame looks like W-002
ids.update("cam_01", frame, [dict(PERSON, tid=22)], 2)
ff.emb = E1
check("one or two odd frames do not rename W-001 to W-002", named == "W-001" and seen_as(22, 4) == "W-001")


print("\nALL TESTS PASSED" if ok_all else "\nSOME TESTS FAILED")
