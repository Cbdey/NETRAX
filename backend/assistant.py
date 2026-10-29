"""NETRAX AI assistant over the event history (final-round section 3).

Grounded by design: the question is turned into a database query (time window + filters); the answer is
written ONLY from the rows that come back. If nothing matches, it says so. Every record links to the real
event. Optional: with an Anthropic API key (config/secrets.local.yaml -> assistant.anthropic_api_key) an LLM
re-words the same results; it gets only the rows, and the rows are always shown under its text.

Understands (English, case-insensitive):
  time   today, yesterday, day before yesterday, last night, this morning/afternoon/evening, monday..sunday,
         12 oct / oct 12 / 12/10 / 2026-10-12, last 2 hours / 30 minutes / 3 days, this week, last week,
         at 4 pm / 4:30 pm / 16:00 / around 4, between 3 and 5 pm
  what   fire, smoke, ppe / violation / helmet / vest / gloves / goggles / mask / shoes, unauthorised /
         restricted, entrance / gate / approved / denied / manual, attendance / rfid / entered
  where  zone names or ids, camera ids (cam_01, camera 1) or names
  who    registered worker names or W-ids
  state  open / active / unresolved, acknowledged, resolved / closed, critical / high / medium / low
"""
import json
import re
import time
from datetime import datetime, timedelta

from . import db, timeutil

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                          "nov", "dec"])}
ITEMS = {"helmet": "helmet", "hard hat": "helmet", "vest": "vest", "jacket": "vest", "glove": "gloves",
         "goggle": "goggles", "glasses": "goggles", "mask": "mask", "shoe": "boots", "boot": "boots"}
PARTS = {"morning": (6, 12), "afternoon": (12, 17), "evening": (17, 21), "night": (21, 30)}
TYPE_LABEL = {"ppe_missing": "PPE violation", "fire": "Fire", "smoke": "Smoke", "unauthorized_entry":
              "Unauthorised entry", "entrance_check": "Entrance PPE check", "gas": "Gas", "heat": "Heat"}


def _day(d):
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def _hour(h, ampm):
    h = int(h)
    if ampm == "pm" and h < 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    return h


def parse(question, now=None, workers=(), zones=None, cameras=None):
    """-> dict(window=(t0, t1), filters, notes, intent). Pure function (tested in tools/final_selftest)."""
    now_dt = timeutil.local(now or time.time())
    q = " " + re.sub(r"[?!,]", " ", question.lower()) + " "
    q = q.replace("’", "'")
    notes, f = [], {}
    day = None
    # ---- day
    if "day before yesterday" in q:
        day = _day(now_dt) - timedelta(days=2)
    elif "yesterday" in q or "last night" in q:
        day = _day(now_dt) - timedelta(days=1)
    elif "today" in q or "this morning" in q or "this afternoon" in q or "this evening" in q or "tonight" in q:
        day = _day(now_dt)
    else:
        m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", q)
        m2 = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", q)
        m3 = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?\b", q)
        m4 = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", q)
        try:
            if m:
                day = now_dt.replace(year=int(m[1]), month=int(m[2]), day=int(m[3]))
            elif m2:
                day = now_dt.replace(month=MONTHS[m2[2]], day=int(m2[1]))
            elif m3:
                day = now_dt.replace(month=MONTHS[m3[1]], day=int(m3[2]))
            elif m4:
                yr = int(m4[3]) if m4[3] else now_dt.year
                yr = yr + 2000 if yr < 100 else yr
                day = now_dt.replace(year=yr, month=int(m4[2]), day=int(m4[1]))      # Indian order: dd/mm
                notes.append(f"read {m4[0]} as day/month ({day.strftime('%d %b %Y')})")
            if day is not None:
                day = _day(day)
                if day > now_dt and not m and not (m4 and m4[3]):
                    day = day.replace(year=day.year - 1)        # "12 dec" asked in january = last december
        except ValueError:
            notes.append("that date does not exist - using the last 24 hours")
            day = None
        if day is None:
            for i, wd in enumerate(WEEKDAYS):
                if re.search(rf"\b(on |last )?{wd}\b", q):
                    back = (now_dt.weekday() - i) % 7 or (7 if f" last {wd}" in q else 0)
                    day = _day(now_dt) - timedelta(days=back)
                    break
    t0 = t1 = None
    # ---- relative windows
    m = re.search(r"\b(?:last|past|previous)\s+(\d+)?\s*(minute|min|hour|hr|day)s?\b", q)
    if m and day is None:
        n = int(m[1] or 1)
        unit = {"minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400}[m[2]]
        t1, t0 = now_dt, now_dt - timedelta(seconds=n * unit)
    elif "this week" in q and day is None:
        t0, t1 = _day(now_dt) - timedelta(days=now_dt.weekday()), now_dt
    elif "last week" in q and day is None:
        start = _day(now_dt) - timedelta(days=now_dt.weekday() + 7)
        t0, t1 = start, start + timedelta(days=7)
    # ---- clock time on that day
    base = day or _day(now_dt)
    between = re.search(r"between\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s+(?:and|to|-)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", q)
    at = re.search(r"(?:\bat|\baround|\babout|\bnear|@)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|o'clock)?\b", q) or \
        re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", q) or re.search(r"\b(\d{1,2}):(\d{2})()\b", q)
    part = next((p for p in PARTS if p in q), None)
    if t0 is None and between:
        ap2 = between[6] or between[3]
        ap1 = between[3] or (ap2 if int(between[1]) <= int(between[4]) else None)
        h1, h2 = _hour(between[1], ap1), _hour(between[4], ap2)
        t0 = base.replace(hour=h1 % 24, minute=int(between[2] or 0))
        t1 = base.replace(hour=h2 % 24, minute=int(between[5] or 0))
        if not (between[3] or between[6]):
            notes.append("no AM/PM given - read as 24-hour clock")
    elif t0 is None and at:
        ampm = (at[3] or "").replace("o'clock", "")
        h = int(at[1])
        if not ampm and h <= 12 and not (at[2] and h >= 13):
            if h <= 7 or (part in ("afternoon", "evening", "night")):
                ampm = "pm"
                notes.append(f"'{at[1]}{':' + at[2] if at[2] else ''}' has no AM/PM - read as "
                             f"{h}:{at[2] or '00'} PM. Add AM/PM for the morning")
            else:
                ampm = "am" if h < 12 else ""
        hh = _hour(h, ampm or None) if ampm else h
        if hh < 24:
            center = base.replace(hour=hh, minute=int(at[2] or 0))
            span = 30
            t0, t1 = center - timedelta(minutes=span), center + timedelta(minutes=span)
            notes.append(f"looking {span} min either side of {center.strftime('%H:%M')}")
    elif t0 is None and part:
        a, b = PARTS[part]
        t0 = base.replace(hour=a)
        t1 = base + timedelta(hours=b)
    elif t0 is None and day is not None:
        t0, t1 = day, day + timedelta(days=1)
    if t0 is None:
        t0, t1 = now_dt - timedelta(hours=24), now_dt
        notes.append("no time given - showing the last 24 hours")
    if t1 > now_dt:
        t1 = now_dt
    # ---- what
    types = set()
    if re.search(r"\bfire|flame|burn", q):
        types.add("fire")
    if "smoke" in q:
        types.add("smoke")
    if re.search(r"\bfire alarm|\bhazard", q):
        types |= {"fire", "smoke", "heat", "gas"}
    items = sorted({v for k, v in ITEMS.items() if k in q})
    if items or re.search(r"\bppe\b|violation|safety gear|without", q):
        types.add("ppe_missing")
    if re.search(r"unauthori[sz]|restricted|intru|not allowed|trespass", q):
        types.add("unauthorized_entry")
    entr = re.search(r"entrance|gate|entry check|approved|denied|rejected|manual check", q)
    if entr:
        types.add("entrance_check")
    att = bool(re.search(r"attendance|rfid|card|tapped|checked in|clock(ed)? in|\benter(ed)?\b|who came|came in|arriv", q))
    if types:
        f["types"] = sorted(types)
    if items:
        f["items"] = items
    if "denied" in q or "rejected" in q:
        f["entrance_result"] = "denied"
    elif "approved" in q:
        f["entrance_result"] = "approved"
    elif "manual" in q:
        f["entrance_result"] = "manual"
    for s in ("critical", "high", "medium", "low"):
        if re.search(rf"\b{s}\b", q):
            f["severity"] = s.title()
    if re.search(r"\b(open|active|unresolved|ongoing|pending)\b", q):
        f["status"] = ["open", "escalated", "verifying", "acknowledged"]
    elif re.search(r"\backnowledged\b", q):
        f["status"] = ["acknowledged"]
    elif re.search(r"\b(resolved|closed|fixed)\b", q):
        f["status"] = ["resolved", "closed"]
    for zid, z in (zones or {}).items():
        names = {zid.replace("_", " "), (z.get("name") or "").lower()}
        if any(n and n in q for n in names) or (zid == "welding_bay" and "welding" in q) or \
                (zid == "assembly_line" and "assembly" in q):
            f.setdefault("zones", []).append(zid)
    for cid, c in (cameras or {}).items():
        num = int(re.sub(r"\D", "", cid) or 0)
        if cid in q or re.search(rf"\b(cam|camera)\s*0?{num}\b", q):
            f.setdefault("cameras", []).append(cid)
    for w in workers:
        nm = (w.get("name") or "").lower().strip()
        first = nm.split()[0] if nm else ""
        if w["worker_id"].lower() in q or (first and len(first) >= 3 and re.search(rf"\b{re.escape(first)}", q)):
            f.setdefault("workers", []).append(w["worker_id"])
    intent = "count" if re.search(r"how many|count|number of", q) else "summary"
    return {"window": (t0.timestamp(), t1.timestamp()), "filters": f, "notes": notes, "intent": intent,
            "attendance": att}


def query(parsed, limit=50):
    t0, t1 = parsed["window"]
    f = parsed["filters"]
    sql = "SELECT * FROM events WHERE started_at >= ? AND started_at <= ?"
    args = [t0, t1]
    if f.get("types"):
        sql += f" AND type IN ({','.join('?' * len(f['types']))})"
        args += f["types"]
    for col, key in (("zone_id", "zones"), ("camera_id", "cameras"), ("worker_id", "workers"), ("status", "status")):
        if f.get(key):
            sql += f" AND {col} IN ({','.join('?' * len(f[key]))})"
            args += f[key]
    if f.get("severity"):
        sql += " AND severity = ?"
        args.append(f["severity"])
    sql += " ORDER BY started_at ASC LIMIT 2000"
    rows = [db.row_to_event(r) for r in db.q(sql, args)]
    if f.get("items"):
        rows = [r for r in rows if r["type"] != "ppe_missing" or set(f["items"]) & set(r["items_missing"] or [])]
    if f.get("entrance_result"):
        rows = [r for r in rows if r["type"] != "entrance_check" or
                (r.get("details") or {}).get("result") == f["entrance_result"]]
    att = []
    if parsed.get("attendance"):
        try:
            asql = "SELECT * FROM attendance WHERE at >= ? AND at <= ?"
            aargs = [t0, t1]
            if f.get("workers"):
                asql += f" AND worker_id IN ({','.join('?' * len(f['workers']))})"
                aargs += f["workers"]
            att = db.q(asql + " ORDER BY at ASC LIMIT 200", aargs)
        except Exception:                 # noqa: BLE001 - attendance table only exists with hardware module
            att = []
    return rows, att


def _what(r):
    t = r["type"]
    if t == "ppe_missing":
        return "Missing " + ", ".join(r.get("items_missing") or []) if r.get("items_missing") else "PPE violation"
    if t == "entrance_check":
        d = r.get("details") or {}
        res = (d.get("result") or "").upper()
        return f"Entrance {res}" + (f" (missing {', '.join(r.get('items_missing') or [])})" if r.get("items_missing") else "")
    if t in ("fire", "smoke") and r.get("fire_level"):
        return f"{TYPE_LABEL[t]} - level {r['fire_level']}"
    return TYPE_LABEL.get(t, t.replace("_", " ").title())


def card(r, mask_names=False):
    who = None
    if r.get("worker_id") and not mask_names:
        who = f"{r.get('worker_name') or ''} ({r['worker_id']})".strip()
    elif r.get("worker_id"):
        who = "registered worker (name hidden for your role)"
    return {"event_id": r["event_id"], "type": r["type"], "what": _what(r), "time": timeutil.fmt(r["started_at"]),
            "at": r["started_at"], "zone": r.get("zone_name") or r.get("zone_id"), "camera": r.get("camera_id"),
            "severity": r.get("severity"), "status": r.get("status"), "worker": who,
            "snapshot": None if mask_names else r.get("snapshot"),
            "clip": None if mask_names else r.get("clip"), "link": f"/#event={r['event_id']}",
            "location": (r.get("location") or {}).get("note") if isinstance(r.get("location"), dict) else None}


def answer(question, user="supervisor", role="supervisor", workers=(), zones=None, cameras=None, now=None,
           llm_key=None, llm_model=None):
    t_start = time.time()
    p = parse(question, now, workers, zones, cameras)
    rows, att = query(p)
    t0, t1 = p["window"]
    win = f"{timeutil.fmt(t0)} to {timeutil.fmt(t1, with_date=timeutil.local(t0).date() != timeutil.local(t1).date())}"
    mask = role == "viewer"
    cards = [card(r, mask) for r in rows]
    lines = []
    if not rows and not att:
        text = f"No matching record was found between {win}."
        if p["filters"]:
            text += " Filters: " + _filters_text(p["filters"]) + "."
    else:
        by = {}
        for r in rows:
            by.setdefault(_what_type(r), []).append(r)
        parts = [f"{len(v)} {k}" for k, v in sorted(by.items(), key=lambda kv: -len(kv[1]))]
        text = f"Between {win}: {len(rows)} incident{'s' if len(rows) != 1 else ''}"
        text += (" - " + ", ".join(parts) + "." if parts else ".")
        for r in rows[:8]:
            c = card(r, mask)
            lines.append(f"{timeutil.fmt(r['started_at'], with_date=False)} - {c['what']} in {c['zone']}"
                         f"{' (' + c['camera'] + ')' if c['camera'] else ''}"
                         f"{', ' + c['worker'] if c['worker'] else ''}; status {c['status']}")
        if len(rows) > 8:
            lines.append(f"... and {len(rows) - 8} more (all listed below).")
        open_n = sum(1 for r in rows if r["status"] in ("open", "escalated", "verifying"))
        if open_n:
            lines.append(f"{open_n} of them still need action (open).")
        if att:
            names = sorted({a.get("name") or a.get("worker_id") or "unknown card" for a in att})
            if mask:
                names = [f"{len(names)} card holder(s)"]
            lines.append(f"RFID attendance: {len(att)} scan(s) - {', '.join(names)}.")
    missing_info = []
    if rows and any(not r.get("snapshot") for r in rows):
        missing_info.append("some records have no saved snapshot (privacy retention or hardware-only events)")
    llm_used = False
    if llm_key and (rows or att):
        try:
            text2 = _llm(llm_key, llm_model, question, text, lines, p)
            if text2:
                text, lines, llm_used = text2, [], True
        except Exception:                 # noqa: BLE001 - always fall back to the grounded template
            pass
    ms = (time.time() - t_start) * 1000
    db.x("INSERT INTO assistant_log (at, user, question, window_from, window_to, n_results, ms) VALUES (?,?,?,?,?,?,?)",
         (time.time(), user, question[:500], t0, t1, len(rows), ms))
    return {"question": question, "answer": text, "details": lines, "notes": p["notes"],
            "interpretation": {"from": timeutil.fmt(t0), "to": timeutil.fmt(t1), "filters": p["filters"],
                               "timezone": timeutil.tz_label()},
            "count": len(rows), "records": cards[-50:],
            "attendance": [] if mask else [{"time": timeutil.fmt(a["at"]), "name": a.get("name"),
                                            "worker_id": a.get("worker_id"), "event": a.get("event"),
                                            "zone": a.get("zone_id")} for a in att[-50:]],
            "unavailable": missing_info, "grounded": True, "llm": llm_used, "ms": round(ms, 1)}


def _what_type(r):
    if r["type"] == "entrance_check":
        return "entrance " + ((r.get("details") or {}).get("result") or "check")
    if r["type"] in ("fire", "smoke") and r.get("fire_level"):
        return f"{r['type']} (level {r['fire_level']})"
    return TYPE_LABEL.get(r["type"], r["type"]).lower()


def _filters_text(f):
    out = []
    for k, v in f.items():
        out.append(f"{k}={','.join(v) if isinstance(v, list) else v}")
    return "; ".join(out)


def _llm(key, model, question, text, lines, parsed):
    import requests
    facts = text + "\n" + "\n".join(lines)
    body = {"model": model or "claude-haiku-5-5", "max_tokens": 300,
            "system": "You summarise safety-incident database results for a plant supervisor. Use ONLY the facts "
                      "given. Never add events, times, names or causes that are not in the facts. If the facts say "
                      "no record was found, say exactly that. Two to four short sentences.",
            "messages": [{"role": "user", "content": f"Question: {question}\nDatabase facts:\n{facts}"}]}
    r = requests.post("https://api.anthropic.com/v1/messages", json=body, timeout=12,
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    if r.status_code != 200:
        return None
    return "".join(c.get("text", "") for c in r.json().get("content", [])).strip() or None
