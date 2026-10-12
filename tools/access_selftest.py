"""Restricted zone + RFID reader + voice priority (no ESP32, scratch database).   python -m tools.access_selftest"""
import asyncio
import os
import tempfile
import time

os.environ.setdefault("SENTINELEYE_MUTE", "1")
os.environ["SENTINELEYE_DB"] = os.path.join(tempfile.mkdtemp(), "access_test.db")
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


async def run_voice():
    await m.ingest_event(ppe_evt("evt_voice1", "open", ["helmet", "vest"], "W-001"))
    check("PPE alarm -> spoken once", said("evt_voice1", "ppe") == 1)
    await m.ingest_event(ppe_evt("evt_voice1", "update", ["helmet", "vest"], "W-001", track_id=5))
    check("update with the SAME items (handover / new snapshot / name) -> no second call", said("evt_voice1") == 1)
    await m.ingest_event(ppe_evt("evt_voice1", "update", ["helmet", "vest", "goggles"], "W-001"))
    check("goggles confirmed 3 s later (same person) -> dashboard updated, NO second call within 20 s",
          said("evt_voice1", "ppe") == 1 and db.get_event("evt_voice1")["items_missing"] == ["helmet", "vest", "goggles"])
    await m.ingest_event(ppe_evt("evt_voice2", "open", ["helmet"], "V-002", worker_name=None))
    await m.ingest_event(ppe_evt("evt_voice3", "open", ["helmet"], "V-003", worker_name=None))
    check("tracker slip: same unregistered face opens 2 records (V-002, V-003) -> called once, not twice",
          said("evt_voice2", "ppe") + said("evt_voice3", "ppe") == 1)
    m.SPOKEN.clear()                                   # 20 s later
    await m.ingest_event(ppe_evt("evt_voice1", "update", ["helmet"], "W-001"))
    check("decided set changed later (vest + goggles back on, helmet still off) -> one new call",
          said("evt_voice1", "ppe") == 2)
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


m.SPK.update(repeat_after_s=60, max_repeats=1)
asyncio.get_event_loop().run_until_complete(run_voice())
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
