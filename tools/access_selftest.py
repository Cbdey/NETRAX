"""Restricted zone + RFID reader + voice priority (no ESP32, scratch database).   python -m tools.access_selftest"""
import asyncio
import os
import tempfile
import time

os.environ.setdefault("SENTINELEYE_MUTE", "1")
os.environ["SENTINELEYE_DB"] = os.path.join(tempfile.mkdtemp(), "access_test.db")
os.environ["SENTINELEYE_NO_NOTIFY"] = "1"                    # never send real Telegram / e-mail from a test
os.environ["SENTINELEYE_SECRETS"] = os.path.join(tempfile.mkdtemp(), "none.yaml")
import backend.main as m            # noqa: E402
from backend import actuators, db, hw_api   # noqa: E402

ok_all = True


def check(name, cond):
    global ok_all
    ok_all &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name)


hw_api.setup(broadcast=m.broadcast, respond=m.respond, notify_async=m.notify_async, announce=m.announce,
             ALARM=m.ALARM, ZONES=m.ZONES, list_workers=m.list_workers)
hw_api._sheets_mirror = lambda r: SENT.append(r["uid"])
hw_api.CFG.setdefault("attendance", {})["readers"] = {"esp32-rfid-01": "welding_bay"}   # restricted-zone reader tests
SENT = []
allowed = m.ZONES["welding_bay"].get("allowed_workers") or []
for wid, name, uid in [(allowed[0] if allowed else "W-001", "Allowed", "11110001"), ("W-090", "Other", "22220002")]:
    db.x("INSERT OR REPLACE INTO workers (worker_id, name, embeddings, photos, created_at, badge_uid, active) "
         "VALUES (?,?,?,?,?,?,1)", (wid, name, "[]", 0, 0, uid))


def cam_evt(eid, wid, action="open"):
    return {"event_id": eid, "action": action, "type": "unauthorized_entry", "items_missing": [], "severity": "High",
            "camera_id": "cam_01", "zone_id": "welding_bay", "zone_name": "Welding bay", "worker_id": wid,
            "worker_name": "Other", "confidence": 0.9, "bbox": [1, 2, 3, 4], "track_id": 1}


async def run():
    t = time.time()
    r = await hw_api.handle_rfid({"uid": "11110001", "deviceId": "esp32-rfid-01", "name": "x"}, t)
    check("allowed worker taps in -> ENTERED, access GRANTED, reader text 'STUDENT ENTERED'",
          r["event"] == "ENTERED" and r["access"] == "GRANTED" and hw_api._reader_text(r).startswith("STUDENT ENTERED"))
    r = await hw_api.handle_rfid({"UID": "22220002"}, t + 5)
    ev = db.q("SELECT * FROM events WHERE type='unauthorized_entry' AND status='open'")
    check("other worker taps in -> ACCESS DENIED + one open HIGH alert",
          r["access"] == "DENIED" and hw_api._reader_text(r).startswith("ACCESS DENIED") and len(ev) == 1
          and ev[0]["severity"] == "High")
    r = await hw_api.handle_rfid({"uid": "22220002"}, t + 6)
    check("card held at the reader -> DUPLICATE, ignored", r["status"] == "DUPLICATE")
    r2 = await m.ingest_event(cam_evt("evt_test_cam", "W-090"))
    check("camera sees the same person -> merged into the RFID alert (one alert)", "merged_into" in r2)
    await hw_api.handle_rfid({"uid": "22220002"}, t + 20)
    ev = db.q("SELECT status FROM events WHERE type='unauthorized_entry'")
    check("other worker taps out -> alert resolved", len(ev) == 1 and ev[0]["status"] == "resolved")
    r = await hw_api.handle_rfid({"uid": "DEADBEEF"}, t + 30)
    check("unknown card -> logged as unregistered (as before)", r["status"] == "UNKNOWN_UID")
    check("every scan forwarded to the Google Sheet mirror", len(SENT) == 4)
    # the team's Google-Sheets sketch: POST /api/rfid {name, uid, event, timestamp} after the sheet answered
    check("POST + GET /api/rfid registered (sketch DASHBOARD_URL, no reflash)",
          {("/api/rfid", "POST"), ("/api/rfid", "GET")} <= {(r.path, mth) for r in hw_api.router.routes
                                                             for mth in getattr(r, "methods", ())})
    # friend's direct sketch toggles in RAM: after an ESP32 reboot it says ENTERED for someone already inside
    r = await hw_api.handle_rfid({"name": "Chinmaya Kumar Sahoo", "uid": "11110001", "event": "ENTERED",
                                  "timestamp": "2026-10-12 18:02:11"}, t + 40)
    check("default: laptop decides (inside -> EXITED) even if a rebooted reader says ENTERED; reply has result line",
          r["event"] == "EXITED" and r["result"].startswith("STUDENT EXITED") and len(SENT) == 5)
    hw_api.CFG.setdefault("attendance", {})["trust_reader_event"] = True
    r = await hw_api.handle_rfid({"name": "Chinmaya Kumar Sahoo", "uid": "11110001", "event": "EXITED"}, t + 50)
    check("trust_reader_event: true (Google-Sheets sketch) -> reader's EXITED kept, not mirrored to the sheet again",
          r["event"] == "EXITED" and len(SENT) == 5)
    hw_api.CFG["attendance"]["trust_reader_event"] = False
    r = await hw_api.handle_rfid({"name": "UNKNOWN", "uid": "22220002", "event": "UNKNOWN CARD"}, t + 60)
    check("card unknown to the sketch but registered here -> laptop decides ENTERED + DENIED alert + mirrored",
          r["event"] == "ENTERED" and r["access"] == "DENIED" and r["result"].startswith("ACCESS DENIED")
          and len(SENT) == 6)
    hw_api.READER_SEEN.clear()
    rd = {x["device_id"]: x for x in hw_api.readers(t)}
    check("reader that never called in -> shown as never seen (not online)",
          "esp32-rfid-01" in rd and not rd["esp32-rfid-01"]["online"] and rd["esp32-rfid-01"]["last_seen"] is None)
    await hw_api.handle_rfid({"uid": "11110001", "device_id": "esp32-rfid-01"}, t + 100, ip="172.20.163.50")
    rd = {x["device_id"]: x for x in hw_api.state(light=True)["readers"]}
    check("a scan marks the reader connected with its IP (dashboard chip)",
          rd["esp32-rfid-01"]["ip"] == "172.20.163.50" and rd["esp32-rfid-01"]["last_seen"] == t + 100)


asyncio.get_event_loop().run_until_complete(run())


# ONE ALARM PER DECISION (12 Oct): the speaker talks once per decided set of missing items
def said(eid=None, kind=None):
    q = "SELECT COUNT(*) AS n FROM announcements WHERE 1=1" + (" AND event_id=?" if eid else "") + (" AND kind=?" if kind else "")
    return db.q(q, tuple(x for x in (eid, kind) if x))[0]["n"]


def ppe_evt(eid, action, items, wid=None, **kw):
    from datetime import datetime, timezone
    return dict({"event_id": eid, "action": action, "type": "ppe_missing", "items_missing": items, "severity": "High",
                 "camera_id": "cam_01", "zone_id": "welding_bay", "zone_name": "Welding bay", "worker_id": wid,
                 "worker_name": "Allowed" if wid else None, "confidence": 0.9, "bbox": [1, 2, 3, 4], "track_id": 1,
                 "captured_at": datetime.now(timezone.utc).isoformat()}, **kw)


def voice_sentences():
    """Fixed sentences, name first, items in icon order (13 Nov: PPE = '<name>. <items> violation.')."""
    reg = m.ppe_text({"type": "ppe_missing", "worker_id": "W-004", "worker_name": "Ritika", "zone_name": "Welding bay",
                      "items_missing": ["gloves", "helmet"]})
    unreg = m.ppe_text({"type": "ppe_missing", "worker_id": "V-003", "zone_name": "Welding bay", "items_missing": ["helmet"]})
    acc = m.access_text({"type": "unauthorized_entry", "worker_id": "W-002", "worker_name": "Sourav", "zone_name": "Control room"})
    nobody = m.ppe_text({"type": "ppe_missing", "worker_id": None, "worker_name": "Guess", "zone_name": "Store", "items_missing": ["vest"]})
    # 13 Nov (user): short - name + PPE gear violation only
    check(f"voice, registered: '{reg}'", reg == "Ritika, worker W 0 0 4. Helmet and gloves violation.")
    check(f"voice, unregistered: '{unreg}'", unreg == "Unregistered person. Helmet violation.")
    check(f"voice, unauthorised: '{acc}'", acc == "Attention Sourav, worker W 0 0 2. You are not authorised in the Control room. Please leave now.")
    check("voice never speaks a name we do not know (no worker ID = unregistered person)", "Guess" not in nobody)
    from backend.actuators import make_siren_wav
    import wave, tempfile as _tf2
    _w = os.path.join(_tf2.mkdtemp(), "siren.wav")
    _d = make_siren_wav(_w)
    with wave.open(_w) as _wf:
        _n = _wf.getnframes() / _wf.getframerate()
    check(f"fire: voice 'Fire detected.' / 'Smoke detected.' + a {_d:.1f} s industrial wail siren cycle",
          m.fire_words({"type": "fire"}) == "Fire detected." and m.fire_words({"type": "smoke"}) == "Smoke detected."
          and abs(_n - _d) < 0.05)


voice_sentences()


async def run_voice():
    await m.ingest_event(ppe_evt("evt_voice1", "open", ["helmet", "vest"], "W-001"))
    check("PPE alarm -> spoken once", said("evt_voice1", "ppe") == 1)
    await m.ingest_event(ppe_evt("evt_voice1", "update", ["helmet", "vest"], "W-001", track_id=5))
    check("update with the SAME items (handover / new snapshot / name) -> no second call", said("evt_voice1") == 1)
    await m.ingest_event(ppe_evt("evt_voice1", "update", ["helmet", "vest", "goggles"], "W-001"))
    last = db.q("SELECT text FROM announcements WHERE event_id='evt_voice1' ORDER BY id DESC LIMIT 1")[0]["text"]
    check(f"29 Oct / 12 Nov: goggles found missing later on the same person -> NEW call '{last}'",
          said("evt_voice1", "ppe") == 2 and last == "Allowed, worker W 0 0 1. Safety goggles violation."
          and db.get_event("evt_voice1")["items_missing"] == ["helmet", "vest", "goggles"])
    kinds_ = [r["kind"] for r in db.q("SELECT kind FROM notifications WHERE event_id='evt_voice1'")]
    check("29 Oct: ... and ONE Telegram/e-mail 'more PPE missing: GOGGLES' per channel",
          any(k.startswith("more PPE missing: GOGGLES") for k in kinds_))
    await m.ingest_event(ppe_evt("evt_voice1", "update", ["helmet", "vest", "goggles"], "W-001", track_id=9))
    check("same list again (tracker handover) -> no extra call, no extra message",
          said("evt_voice1", "ppe") == 2 and len(db.q("SELECT kind FROM notifications WHERE event_id='evt_voice1'")) == len(kinds_))
    m.SPK["unregistered_wait_s"] = 0.3                 # 13 Nov: unnamed alerts wait for face ID before the voice
    await m.ingest_event(ppe_evt("evt_voice2", "open", ["helmet"], "V-002", worker_name=None))
    await m.ingest_event(ppe_evt("evt_voice3", "open", ["helmet"], "V-003", worker_name=None))
    early = said("evt_voice2", "ppe") + said("evt_voice3", "ppe")
    await asyncio.sleep(0.6)
    check(f"tracker slip: same unregistered face opens 2 records (V-002, V-003) -> called once, not twice "
          f"(and not before the {m.SPK['unregistered_wait_s']} s wait for a name: {early})",
          early == 0 and said("evt_voice2", "ppe") + said("evt_voice3", "ppe") == 1)
    # 13 Nov: name arrives DURING the wait -> spoken once, with the name (never 'unregistered')
    m.SPOKEN.clear()
    await m.ingest_event(ppe_evt("evt_nm1", "open", ["helmet"], None, worker_name=None, camera_id="cam_02",
                                 zone_id="warehouse", zone_name="Warehouse"))
    await m.ingest_event(ppe_evt("evt_nm1", "update", ["helmet"], "W-003", worker_name="Bibhu", camera_id="cam_02",
                                 zone_id="warehouse", zone_name="Warehouse"))
    await asyncio.sleep(0.6)
    t = [r["text"] for r in db.q("SELECT text FROM announcements WHERE event_id='evt_nm1'")]
    check(f"13 Nov: face recognised during the wait -> ONE call with the name: {t}",
          t == ["Bibhu, worker W 0 0 3. Helmet violation."])
    # 13 Nov: spoken as 'Unregistered person' (no name in time), recognised later -> said again ONCE with the name
    m.SPOKEN.clear()
    await m.ingest_event(ppe_evt("evt_nm2", "open", ["vest"], None, worker_name=None, camera_id="cam_03",
                                 zone_id="assembly_line", zone_name="Assembly line"))
    await asyncio.sleep(0.6)
    await m.ingest_event(ppe_evt("evt_nm2", "update", ["vest"], "W-004", worker_name="Ritika", camera_id="cam_03",
                                 zone_id="assembly_line", zone_name="Assembly line"))
    await m.ingest_event(ppe_evt("evt_nm2", "update", ["vest"], "W-004", worker_name="Ritika", camera_id="cam_03",
                                 zone_id="assembly_line", zone_name="Assembly line", track_id=8))
    t = [r["text"] for r in db.q("SELECT text FROM announcements WHERE event_id='evt_nm2' ORDER BY id")]
    check(f"13 Nov: said 'Unregistered person', name found later -> corrected once with name + ID: {t}",
          t == ["Unregistered person. Safety vest violation.", "Ritika, worker W 0 0 4. Safety vest violation."])
    m.SPOKEN.clear()                                   # 20 s later
    await m.ingest_event(ppe_evt("evt_voice1", "update", ["helmet"], "W-001"))
    check("vest + goggles put back on (helmet still off) -> dashboard updated, no new alarm (nothing NEW missing)",
          said("evt_voice1", "ppe") == 2 and db.get_event("evt_voice1")["items_missing"] == ["helmet"])
    m.CALLS.pop("evt_voice2", None); m.CALLS.pop("evt_voice3", None)
    t = time.time()
    await m.repeat_calls(t + 61)
    await m.repeat_calls(t + 125)
    await m.repeat_calls(t + 190)
    check(f"still not fixed -> exactly ONE reminder after 60 s, then silence (settings max_repeats 1)",
          said("evt_voice1", "ppe_repeat") == 1)
    await m.ingest_event(ppe_evt("evt_voice1", "resolve", ["helmet"], "W-001", reason="compliant"))
    check("worker puts it on -> one 'PPE complete, thank you'", said("evt_voice1", "ppe_ok") == 1)
    n0 = said(kind="ppe_ok")
    await m.ingest_event(ppe_evt("info_cam_01_7_1", "info", [], "W-001", type="ppe_ok"))
    check("camera says compliant on arrival -> one 'PPE complete' call, no alert record",
          said(kind="ppe_ok") == n0 + 1 and db.get_event("info_cam_01_7_1") is None)
    await m.ingest_event(ppe_evt("info_cam_01_7_2", "info", [], "W-001", type="ppe_ok",
                                 captured_at="2026-01-01T00:00:00Z"))
    check("old 'PPE complete' replayed from the offline buffer -> not spoken", said(kind="ppe_ok") == n0 + 1)
    # 29 Oct bug: supervisor pressed Resolve, the worker still wears nothing -> the camera is told, looks again,
    # and the NEW alert gets the full alarm + messages (no 'same person' silence, no flood-control hold)
    m.SPOKEN.clear()
    await m.ingest_event(ppe_evt("evt_rs1", "open", ["helmet", "vest", "goggles"], "W-095"))
    await m.ingest_event(ppe_evt("evt_rs1", "update", ["helmet", "vest", "goggles", "gloves"], "W-095"))
    await m._act("evt_rs1", "ack", "acknowledged", {})
    await m._act("evt_rs1", "resolve", "resolved", {})

    class _Req:
        headers = {}

        async def body(self):
            return b"jpg"
    reply = await m.camera_frame("cam_01", _Req())
    reply2 = await m.camera_frame("cam_02", _Req())
    check("29 Oct: Resolve -> the camera's next frame reply tells it (closed: evt_rs1); other cameras not told",
          "evt_rs1" in (reply.get("closed") or []) and "closed" not in reply2)
    n1 = said("evt_rs1", "ppe")
    await m.ingest_event(ppe_evt("evt_rs2", "open", ["helmet", "vest", "goggles", "gloves"], "W-095", reopen_of="evt_rs1"))
    k1 = [(r["kind"], r["channel"]) for r in db.q("SELECT kind, channel FROM notifications WHERE event_id='evt_rs1'")]
    still = [c for k, c in k1 if k.startswith("still missing after resolve")]
    # 12 Nov (prompt pack step 6, one alert per violation): within regrace_s the SAME alert comes back (not a second
    # alert): spoken again once + ONE Telegram update 'still missing after resolve' (no new e-mail)
    check(f"12 Nov: still no PPE right after Resolve -> the SAME alert re-opens, spoken again, ONE Telegram update "
          f"({db.get_event('evt_rs1')['status']}, {still})",
          db.get_event("evt_rs2") is None and db.get_event("evt_rs1")["status"] == "open"
          and said("evt_rs1", "ppe") == n1 + 1 and still == ["telegram"])
    await m._act("evt_rs1", "resolve", "resolved", {})
    db.x("UPDATE events SET ended_at=? WHERE event_id='evt_rs1'", (time.time() - 200,))     # resolved > regrace_s ago
    await m.ingest_event(ppe_evt("evt_rs3", "open", ["helmet"], "W-095"))
    check("12 Nov: seen violating again after the grace time -> a NEW alert", db.get_event("evt_rs3") is not None
          and db.get_event("evt_rs3")["status"] == "open")


async def run_missed():
    """13 Nov (user: "violations missed in the PPE violation panel"): two ways a real violation never showed as Active."""
    m.EDGE["started"] = time.time() - 30
    # 1. an alert left escalated by an EARLIER edge run swallowed every new violation of that person
    await m.ingest_event(ppe_evt("evt_old1", "open", ["helmet"], "W-097"))
    db.x("UPDATE events SET status='escalated', updated_at=?, started_at=? WHERE event_id='evt_old1'",
         (time.time() - 3600, time.time() - 3600))
    await m.ingest_event(ppe_evt("evt_new1", "open", ["helmet"], "W-097"))
    check("13 Nov: alert left open by an earlier AI run does NOT swallow the new violation -> new Active alert",
          db.get_event("evt_new1") is not None and db.get_event("evt_new1")["status"] == "open")
    # 2. acknowledged alert: the person left and came back (new edge alert) -> back to Active, spoken again
    await m.ingest_event(ppe_evt("evt_ack1", "open", ["vest"], "W-098"))
    await m._act("evt_ack1", "ack", "acknowledged", {})
    m.PPE_SOURCES.pop("evt_ack1", None)                   # the edge alert behind it has ended (person left)
    n0 = said("evt_ack1", "ppe")
    await m.ingest_event(ppe_evt("evt_ack2", "open", ["vest"], "W-098"))
    check(f"13 Nov: violation again after Acknowledge -> the card is back under Active and spoken "
          f"({db.get_event('evt_ack1')['status']}, calls {n0}->{said('evt_ack1', 'ppe')})",
          db.get_event("evt_ack1")["status"] == "open" and said("evt_ack1", "ppe") == n0 + 1)
    # 3. acknowledged alert, more PPE missing -> back under Active
    await m._act("evt_ack1", "ack", "acknowledged", {})
    await m.ingest_event(ppe_evt("evt_ack1", "update", ["vest", "helmet"], "W-098"))
    check("13 Nov: more PPE missing after Acknowledge -> back under Active",
          db.get_event("evt_ack1")["status"] == "open")


def call_lanes():
    """13 Nov (user: "calling failed during the fire check"): MEASURED - the fire's call waited 90 s behind PPE Telegram /
    e-mail messages in ONE queue, then was skipped because the short fire had already ended by itself."""
    from backend import notify
    d = notify.Dispatcher()
    for k in range(5):
        d.put({"event": {"type": "ppe_missing", "severity": "High"}, "channel": "telegram", "row": k})
    d.put({"event": {"type": "fire", "severity": "Critical"}, "channel": "telegram", "row": "F"})
    d.put({"event": {"type": "fire", "severity": "Critical"}, "channel": "call", "row": "C"})
    first_tg, _ = d._next("telegram")
    first_call, _ = d._next("call")
    check("13 Nov: fire Telegram jumps 5 waiting PPE messages; the phone call has its own lane (never waits for Telegram / e-mail)",
          first_tg["row"] == "F" and first_call["row"] == "C" and len(d.lanes["telegram"]) == 5)
    now = time.time()
    for eid, st, by in (("evt_fc1", "resolved", None), ("evt_fc2", "acknowledged", "sup"), ("evt_fc3", "resolved", "sup")):
        db.x("INSERT INTO events (event_id, type, severity, status, zone_id, received_at, started_at, closed_by, acked_by) "
             "VALUES (?,?,?,?,?,?,?,?,?)", (eid, "fire", "Critical", st, "welding_bay", now, now, by if st == "resolved" else None,
                                            by if st == "acknowledged" else None))
    want = [notify.Dispatcher._call_still_wanted({"event_id": e}, {"kind": "new alert|L0|Critical"})
            for e in ("evt_fc1", "evt_fc2", "evt_fc3")]
    check(f"13 Nov: short fire that ended BY ITSELF still gets its first call; acknowledged / resolved by a person -> no call {want}",
          want == [True, False, False])


call_lanes()
m.SPK.update(repeat_after_s=60, max_repeats=1)
asyncio.get_event_loop().run_until_complete(run_voice())
asyncio.get_event_loop().run_until_complete(run_missed())
check("voice rank: 'PPE complete' (5) after every alarm, before welcome (6)",
      m.voice_rank("welding_bay", "ppe_repeat") < m.voice_rank("welding_bay", "ppe_ok") < m.voice_rank("x", "attendance"))
check("voice rank: high-risk alert 1 < medium 2 < repeat 4 < welcome 6; fire 0",
      (m.voice_rank("welding_bay", "ppe"), m.voice_rank("warehouse", "ppe"), m.voice_rank("welding_bay", "ppe_repeat"),
       m.voice_rank("welding_bay", "attendance"), m.voice_rank("x", "alarm")) == (1, 2, 4, 6, 0))
sp = actuators.Speaker({"enabled": True, "local": False, "max_queue": 4})
sp.local, sp.mode = True, "local"                  # drive the queue without an audio device
for text, pr in [("welcome", 6), ("reminder", 4), ("warehouse", 2), ("welding", 1), ("assembly-low", 3)]:
    sp.say(text, priority=pr)
check("speaker queue: high-risk first, full queue drops the least important (welcome)",
      sp.pending() == ["welding", "warehouse", "assembly-low", "reminder"])
sp.say("FIRE", priority="urgent")
check("fire announcement clears the queue", sp.pending() == ["FIRE"])
print("\nALL ACCESS TESTS PASSED" if ok_all else "\nSOME ACCESS TESTS FAILED")
