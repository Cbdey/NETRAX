"""Live acceptance checklist, brief section 11 (tests 01-14): result, note and screenshot per test, stored in the database.

The automatic tests (tools.final_selftest) use fake providers. This page is for the REAL end-to-end run the brief asks
for: the person doing the test records PASS / FAIL, and each row shows what the database saw, so the evidence is real
records and never an invented number. Nothing here marks a test as passed by itself."""
import json
import os
import time

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import PlainTextResponse

from . import auth, db, timeutil

router = APIRouter()
PHOTO_DIR = None        # set by setup(): data/acceptance

TESTS = [
    ("01", "Complete PPE detected", "Stand at the entrance camera wearing all required PPE for 2 s.", "Green popup VERIFIED; recorded in the entrance log."),
    ("02", "Mandatory PPE item missing", "Same person without the vest.", "Red popup DO NOT ENTER naming the missing item."),
    ("03", "Uncertain / obstructed view", "Hide your feet, stand too close, or two people together.", "Amber MANUAL CHECK; never an automatic approval."),
    ("04", "Serious safety violation", "Remove the helmet in the Welding bay.", "Alert popup + dashboard-wide red state; Acknowledge, then Resolve; record kept."),
    ("05", "Fire and smoke detection", "Lighter or candle in front of a camera (safely).", "Large panel: type, time, camera, zone, status."),
    ("06", "Severity escalation", "Keep it burning or bring it closer.", "Level 1 warning, Level 2 critical, Level 3 evacuate; never goes down."),
    ("07", "Telegram delivery", "Any High alert.", "Message + snapshot in the group; delivery row says DELIVERED."),
    ("08", "Phone alert", "Critical alert.", "Team decision (29 Oct): phone call deferred to future work; mark N/A with this note."),
    ("09", "SMS and e-mail", "Same alert.", "E-mail with event details, type, severity, zone, time and action. SMS deferred with the phone call."),
    ("10", "AI historical query", "Ask: What happened today at <time of test 04>?", "The helmet record with snapshot and an open-record link."),
    ("11", "Multiple simultaneous events", "Two people without PPE on two cameras at once.", "Two separate incidents; acknowledging one keeps the other."),
    ("12", "Latency and load", "python -m tools.latency_report with one camera, then all cameras.", "Median + 95th percentile table in docs/eval/latency_report.md."),
    ("13", "Network / provider failure", "Turn the internet off, raise an alert.", "Dashboard still shows it at once; Telegram RETRYING then FAILED; health strip red."),
    ("14", "Location data", "No location, then Use this device, then wait.", "Location unavailable, then coordinates with source, then marked stale."),
]


def setup(data_dir):
    global PHOTO_DIR
    PHOTO_DIR = os.path.join(data_dir, "acceptance")
    os.makedirs(PHOTO_DIR, exist_ok=True)
    db.x("""CREATE TABLE IF NOT EXISTS acceptance (test_id TEXT PRIMARY KEY, result TEXT, note TEXT, by_user TEXT, at REAL, photo INTEGER)""")
    return PHOTO_DIR


def _latest(sql, args=()):
    rows = db.q(f"SELECT event_id, started_at FROM events WHERE {sql} ORDER BY started_at DESC LIMIT 1", args)
    return rows[0] if rows else None


def _count(sql, args=()):
    return db.q(f"SELECT COUNT(*) AS n FROM events WHERE {sql}", args)[0]["n"]


def evidence(test_id):
    """What the database actually recorded for this test: {text, event_id, at}."""
    def ev(text, row=None):
        return {"text": text, "event_id": row["event_id"] if row else None, "at": row["started_at"] if row else None}

    def entrance(res, word):
        q = "type='entrance_check' AND details LIKE ?"
        arg = (f'%"result": "{res}"%',)
        n = _count(q, arg)
        return ev(f"{n} {word} decision(s) recorded" if n else f"no {word} decision recorded yet", _latest(q, arg))

    def channel(ch, word):
        rows = db.q("SELECT status, COUNT(*) AS n FROM notifications WHERE channel=? AND event_id NOT LIKE 'test%' GROUP BY status", (ch,))
        got = ", ".join(f"{r['n']} {r['status']}" for r in rows)
        return ev(f"{word}: {got}" if got else f"no {word} attempt recorded yet")

    if test_id == "01":
        return entrance("approved", "approved")
    if test_id == "02":
        return entrance("denied", "denied")
    if test_id == "03":
        return entrance("manual", "manual-check")
    if test_id == "04":
        q = "type='ppe_missing' AND severity IN ('High','Critical') AND acked_at IS NOT NULL"
        n = _count(q)
        return ev(f"{n} serious violation(s) acknowledged by a person" if n else "no acknowledged serious violation yet", _latest(q))
    if test_id == "05":
        q = "type IN ('fire','smoke') AND status != 'suppressed'"
        return ev(f"{_count(q)} fire / smoke incident(s) recorded", _latest(q))
    if test_id == "06":
        rows = db.q("SELECT fire_level, COUNT(*) AS n FROM events WHERE type IN ('fire','smoke') AND fire_level IS NOT NULL GROUP BY fire_level ORDER BY fire_level")
        return ev("levels recorded: " + ", ".join(f"L{r['fire_level']} x{r['n']}" for r in rows) if rows else "no fire level recorded yet")
    if test_id == "07":
        return channel("telegram", "Telegram")
    if test_id == "08":
        return channel("call", "Phone call")
    if test_id == "09":
        return channel("email", "E-mail")
    if test_id == "10":
        rows = db.q("SELECT question, at FROM assistant_log ORDER BY at DESC LIMIT 1")
        n = db.q("SELECT COUNT(*) AS n FROM assistant_log")[0]["n"]
        return {"text": f"{n} question(s) logged" + (f"; latest: {rows[0]['question'][:70]}" if rows else ""), "event_id": None, "at": rows[0]["at"] if rows else None}
    if test_id == "11":
        n = db.q("""SELECT COUNT(*) AS n FROM events a JOIN events b ON a.event_id < b.event_id AND a.camera_id != b.camera_id
                    AND a.type='ppe_missing' AND b.type='ppe_missing' AND a.started_at <= b.started_at
                    AND b.started_at <= COALESCE(a.ended_at, a.started_at + 60)""")[0]["n"]
        return ev(f"{n} pair(s) of PPE incidents overlapped on different cameras")
    if test_id == "12":
        p = os.path.join("docs", "eval", "latency_report.md")
        if os.path.exists(p):
            return {"text": "docs/eval/latency_report.md exists", "event_id": None, "at": os.path.getmtime(p)}
        return ev("latency report not measured yet")
    if test_id == "13":
        n = db.q("SELECT COUNT(*) AS n FROM notifications WHERE status='failed' AND event_id NOT LIKE 'test%'")[0]["n"]
        return ev(f"{n} failed delivery attempt(s) recorded and shown")
    if test_id == "14":
        rows = db.q("SELECT source, COUNT(*) AS n FROM locations GROUP BY source")
        return ev("location readings: " + ", ".join(f"{r['n']} {r['source']}" for r in rows) if rows else "no location reading recorded yet")
    return ev("")


def rows():
    saved = {r["test_id"]: r for r in db.q("SELECT * FROM acceptance")}
    out = []
    for tid, name, how, expect in TESTS:
        s = saved.get(tid) or {}
        try:
            evd = evidence(tid)
        except Exception as x:                       # noqa: BLE001 - evidence must never break the page
            evd = {"text": f"could not read evidence: {x}", "event_id": None, "at": None}
        out.append({"id": tid, "name": name, "how": how, "expect": expect, "evidence": evd, "result": s.get("result"),
                    "note": s.get("note"), "by": s.get("by_user"), "at": s.get("at"),
                    "photo": f"/acceptance_photos/{tid}.jpg?v={int(s.get('at') or 0)}" if s.get("photo") else None})
    return out


@router.get("/api/acceptance")
def get_all():
    return rows()


@router.post("/api/acceptance/{test_id}")
def save(test_id: str, request: Request, body: dict = Body(...)):
    if test_id not in {t[0] for t in TESTS}:
        raise HTTPException(404, "unknown test")
    res = (body or {}).get("result")
    if res not in ("pass", "fail", "na", None):
        raise HTTPException(400, "result must be pass, fail or na")
    u = getattr(request.state, "user", None) or {}
    now = time.time()
    old = db.q("SELECT photo FROM acceptance WHERE test_id=?", (test_id,))
    db.x("INSERT OR REPLACE INTO acceptance (test_id, result, note, by_user, at, photo) VALUES (?,?,?,?,?,?)",
         (test_id, res, ((body or {}).get("note") or "")[:500], u.get("user") or "local", now, old[0]["photo"] if old else 0))
    db.audit(u.get("user"), u.get("role"), "acceptance", test_id, json.dumps({"result": res}))
    return next(r for r in rows() if r["id"] == test_id)


@router.post("/api/acceptance/{test_id}/photo")
async def photo(test_id: str, request: Request):
    if test_id not in {t[0] for t in TESTS}:
        raise HTTPException(404, "unknown test")
    data = await request.body()
    if not data or len(data) > 15 * 1024 * 1024:
        raise HTTPException(400, "empty or too large")
    with open(os.path.join(PHOTO_DIR, f"{test_id}.jpg"), "wb") as f:
        f.write(data)
    now = time.time()
    if db.q("SELECT 1 FROM acceptance WHERE test_id=?", (test_id,)):
        db.x("UPDATE acceptance SET photo=1, at=? WHERE test_id=?", (now, test_id))
    else:
        db.x("INSERT INTO acceptance (test_id, photo, at) VALUES (?,1,?)", (test_id, now))
    return next(r for r in rows() if r["id"] == test_id)


@router.get("/api/acceptance/report.md")
def report():
    lines = [f"# NETRAX live acceptance results\n\nExported {timeutil.fmt(time.time())}. Results entered by the person who ran each test; "
             "evidence column read from the database.\n", "| Test | Name | Result | By | Note | Database evidence |", "|---|---|---|---|---|---|"]
    word = {"pass": "PASS", "fail": "FAIL", "na": "N/A", None: "not run"}
    for r in rows():
        cell = lambda v: str(v or "").replace("|", "/").replace("\n", " ")
        lines.append(f"| {r['id']} | {r['name']} | {word[r['result']]} | {cell(r['by'])} | {cell(r['note'])} | {cell(r['evidence']['text'])} |")
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/markdown",
                             headers={"Content-Disposition": "attachment; filename=netrax_acceptance_results.md"})


_ = auth    # roles are enforced by the middleware in main.py (POST = supervisor, photos = supervisor)
