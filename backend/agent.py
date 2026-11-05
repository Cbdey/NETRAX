"""Zentrix - the NETRAX command agent (5 Nov; teacher: "the portal should be run by the AI assistant when the
supervisor or admin is absent, but only on their command").

What it does
  - Understands typed or spoken commands from a supervisor / admin ("hey Zentrix, analyse the recent alerts and
    acknowledge them", "open the admin panel", "enlarge the welding bay camera", "resolve the last alert",
    "allow the person at the entrance", "generate today's report as PDF") - spelling mistakes are tolerated.
  - ANALYSES alerts against live evidence before anyone acts: is the camera still seeing this violation right now,
    did the safety station confirm the fire, is the camera offline (cannot verify), is the detection weak, has an
    acknowledged alert been left unfixed. Each alert gets a verdict and a recommended action.
  - ACTS ONLY ON COMMAND. Nothing here runs by itself: every change is a normal supervisor action (same API,
    same audit log, user = the person who gave the command, note "via Zentrix: <command>").
  - Asks for confirmation before risky bulk changes (resolving / dismissing several alerts, anything that closes
    a fire or smoke alert, escalations that send messages, overriding an entrance refusal).
  - Viewers may ask questions and move around the dashboards, but cannot change records.
  - Everything else (questions about the past) goes to the grounded history assistant (backend/assistant.py).

Pure planning: plan() returns what to say, UI operations for the browser and record actions; backend/main.py
executes the record actions through the normal _act() path. Tested in tools/final_selftest.py [ZX].
"""
import difflib
import re
import time

from . import assistant, db, timeutil

NAME = "Zentrix"
WAKE = re.compile(r"^\s*(?:(?:hey|hi|hello|ok|okay|yo)\s+)?(?:zentrix|zen\s*trix|zentrex|zentricks|zen\s*tricks|sentrix|"
                  r"centrix|sentry\s*x|zendrix|xentrix|zentrics)\b[\s,.:!-]*", re.I)

# words the parser looks for; misspelt words within edit distance are mapped onto them ("acknowlege" -> acknowledge)
VOCAB = """acknowledge acknowledged resolve resolved dismiss escalate analyse analyze review verify check summarise
alert alerts alarm alarms incident incidents violation violations recent latest active pending open close
enlarge camera cameras focus fullscreen minimise maximise control panel admin hardware entrance dashboard
report reports generate download workers system health acceptance overview channels zones users audit
refuse allow approve reject accept welding warehouse assembly helmet gloves goggles everything recommendations
apply confirm cancel mute unmute today yesterday please""".split()

STATUS_OF = {"ack": "acknowledged", "resolve": "resolved", "dismiss": "dismissed", "escalate": "escalated"}
APPLIES = {"ack": ("open", "escalated", "verifying"), "escalate": ("open", "acknowledged"),
           "resolve": ("open", "escalated", "verifying", "acknowledged"),
           "dismiss": ("open", "escalated", "verifying", "acknowledged")}
ACTIVE = ("open", "escalated", "verifying", "acknowledged")
VERB_TEXT = {"ack": "acknowledge", "resolve": "resolve", "dismiss": "mark as false alarm", "escalate": "escalate"}
DONE_TEXT = {"ack": "Acknowledged", "resolve": "Resolved", "dismiss": "Marked as false alarm", "escalate": "Escalated"}
ITEM = {"helmet": "helmet", "vest": "safety vest", "gloves": "gloves", "boots": "safety shoes", "goggles": "goggles",
        "mask": "mask"}

PAGES = [  # (regex, url, spoken name)
    (r"\bcontrol\s*(room|panel|pannel|center|centre)\b|\bmain (dashboard|screen|page)\b|\bhome( page)?\b", "/",
     "the control room"),
    (r"\badmin\b|\badministration\b", "/admin", "the admin panel"),
    (r"\bhardware\b|\bsafety station\b|\bsensors?\b|\brfid (page|panel)\b", "/hardware", "the hardware page"),
    (r"\bentrance\b|\bgate\b|\bentry (page|panel|dashboard|screen)\b", "/entrance", "the entrance dashboard"),
]
SECTIONS = [  # admin sections (admin.html hash routes)
    (r"\breport(s)?( generator)?\b", "reports", "the report generator"),
    (r"\bincident(s)?( list| history| search)?\b|\bhistory\b", "incidents", "the incident list"),
    (r"\bworkers?\b|\bregistration\b|\bregister\b", "workers", "the workers page"),
    (r"\b(alerts? (and|&) channels?|channels?|notifications?|telegram|e-?mail settings)\b", "alerts",
     "alerts and channels"),
    (r"\bcameras? (and|&) zones?\b|\bzones?\b", "cameras", "cameras and zones"),
    (r"\busers?\b|\baudit( log)?\b", "users", "users and audit log"),
    (r"\bsystem health\b|\bhealth\b", "system", "system health"),
    (r"\bacceptance( tests?)?\b|\bchecklist\b", "acceptance", "the acceptance checklist"),
    (r"\boverview\b", "overview", "the admin overview"),
]
NAV_VERB = r"\b(open|go( back)? to|goto|take me to|navigate( to)?|switch to|bring up|launch|move to|visit|jump to|load)\b"


def strip_wake(text):
    """'Hey Zentrix, open admin' -> ('open admin', True)."""
    m = WAKE.match(text or "")
    return (text[m.end():].strip(), True) if m else ((text or "").strip(), False)


def normalise(text, keep=()):
    """lower-case + fix misspelt command words (difflib, cutoff 0.8); names in `keep` are never changed."""
    t = (text or "").lower().replace("’", "'").replace("&", " and ")
    t = re.sub(r"[^\w\s:/'-]", " ", t)
    keep = {k.lower() for k in keep if k}
    out = []
    for w in t.split():
        if len(w) >= 5 and w not in VOCAB and w not in keep and not re.search(r"\d|_", w):
            m = difflib.get_close_matches(w, VOCAB, 1, 0.8)
            if m:
                w = m[0]
        out.append(w)
    t = " " + " ".join(out) + " "
    t = re.sub(r"\backnowledgement\b|\backnowled\w*\b|\backnow\w*\b|\back\b", "acknowledge", t)
    t = re.sub(r"\banaly[sz]\w*\b", "analyse", t)
    t = re.sub(r"\bpannel\b", "panel", t)
    nums = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
    t = re.sub(r"\b(cam|camera)\s+(one|two|three|four|five|six)\b", lambda m: f"{m[1]} {nums[m[2]]}", t)
    return t.strip()


def _action_of(seg):
    """-> ack | resolve | dismiss | escalate | None"""
    if re.search(r"\bfalse alarm\b|\bdismiss\w*\b|\breject\w*\b|\bignore\b|\bnot (a )?real\b", seg):
        return "dismiss"
    if re.search(r"\bescalat\w*\b", seg):
        return "escalate"
    if re.search(r"\bresolve\w*\b|\bclose\b|\bclose (it|them|all)\b|\bmark (it |them |all )?(as )?(fixed|done|solved|resolved)\b"
                 r"|\bclear (the |all )?(alert|alarm|incident)", seg):
        return "resolve"
    if re.search(r"\backnowledge\w*\b|\baccept (the |all |this |these )?(alert|alarm|incident|violation)", seg):
        return "ack"
    return None


def _actions_all(seg):
    """every action verb in the segment, e.g. 'acknowledge or mark resolve' -> ['ack', 'resolve']"""
    found = []
    for a, rx in (("ack", r"\backnowledge\w*\b"), ("resolve", r"\bresolve\w*\b|\bmark (it |them |all )?(as )?(fixed|done|solved)\b"),
                  ("dismiss", r"\bfalse alarm\b|\bdismiss\w*\b"), ("escalate", r"\bescalat\w*\b")):
        if re.search(rx, seg):
            found.append(a)
    return found or [_action_of(seg)]


def _entrance_decision(seg):
    entr = re.search(r"\b(entrance|gate|entry|door|waiting|visitor|at the (door|gate))\b", seg)
    if not entr:
        return None
    if re.search(r"\b(refuse|deny|denied|reject|block|stop|don't let|do not let|not allow|disallow)\b", seg):
        return "denied"
    if re.search(r"\b(allow|approve|let (him|her|them|the person|him in|her in|them in)|let \w+ in|accept|admit|permit|"
                 r"open the gate|grant)\b", seg):
        return "approved"
    return None


def _split(text):
    """'analyse the recent alerts and acknowledge them, then open admin' -> 3 segments."""
    parts = re.split(r"\b(?:and then|then|after that|also|and also)\b|[;.]|,(?=\s*(?:and\s+)?[a-z])", text)
    out = []
    verbs = r"(?:acknowledge|resolve|dismiss|escalate|analyse|review|verify|check|open|go|show|enlarge|zoom|focus|" \
            r"generate|download|mute|unmute|close|mark|allow|refuse|deny|reject|approve|take|switch|make|create|" \
            r"minimise|maximise|exit|back|stop|let)"
    for p in parts:
        p = (p or "").strip()
        if not p:
            continue
        sub = re.split(rf"\s+and\s+(?={verbs}\b)", p)      # 'X and acknowledge them' -> two steps
        out += [s.strip() for s in sub if s.strip()]
    return out or [text]


# ---------------------------------------------------------------- record selection
def _parse(seg, ctx):
    return assistant.parse(seg, ctx.get("now"), ctx.get("workers") or (), ctx.get("zones"), ctx.get("cameras"))


def _events(statuses, ctx, seg, include_entrance=False):
    p = _parse(seg, ctx)
    f = p["filters"]
    explicit = not any("no time given" in n for n in p["notes"])
    sql = f"SELECT * FROM events WHERE status IN ({','.join('?' * len(statuses))}) AND type != 'test'"
    args = list(statuses)
    if not include_entrance:
        sql += " AND type != 'entrance_check'"
    if f.get("types"):
        sql += f" AND type IN ({','.join('?' * len(f['types']))})"
        args += f["types"]
    for col, key in (("zone_id", "zones"), ("camera_id", "cameras"), ("worker_id", "workers")):
        if f.get(key):
            sql += f" AND {col} IN ({','.join('?' * len(f[key]))})"
            args += f[key]
    if f.get("severity"):
        sql += " AND severity = ?"
        args.append(f["severity"])
    now = ctx.get("now") or time.time()
    if explicit:
        sql += " AND started_at >= ? AND started_at <= ?"
        args += list(p["window"])
    elif re.search(r"\b(recent|recently|latest|new|newest)\b", seg):
        sql += " AND COALESCE(updated_at, started_at) >= ?"
        args.append(now - 3 * 3600)
    sql += " ORDER BY started_at DESC LIMIT 200"
    rows = [db.row_to_event(r) for r in db.q(sql, args)]
    if f.get("items"):
        rows = [r for r in rows if r["type"] != "ppe_missing" or set(f["items"]) & set(r["items_missing"] or [])]
    single = re.search(r"\b(last|latest|newest|most recent|this|that|current)\s+(\w+\s+)?(alert|alarm|incident|violation|"
                       r"event|one)\b(?!s)", seg) or re.search(r"\b(it)\b", seg) and not re.search(r"\ball\b", seg)
    return rows[:1] if single else rows


def _by_ids(ids):
    return [e for e in (db.get_event(i) for i in ids) if e]


# ---------------------------------------------------------------- live verification ("AI-verified")
def assess(e, live, now=None, mask=False):
    """Verdict + recommended action for one alert from the evidence available right now.
    live = cameras_payload() of the backend (edge online, per-camera status + tile badge)."""
    now = now or time.time()
    cams = {c["camera_id"]: c for c in (live or {}).get("cameras", [])}
    edge = bool((live or {}).get("edge_online"))
    cam = cams.get(e.get("camera_id")) or {}
    cam_live = edge and cam.get("status") in ("ONLINE", "DEGRADED")
    badge = (cam.get("badge") or "").upper()
    age_min = (now - (e.get("started_at") or now)) / 60
    acked_min = (now - e["acked_at"]) / 60 if e.get("acked_at") else None
    conf = e.get("confidence")
    t, st = e["type"], e["status"]
    why, rec, verdict = [], None, ""
    if t in ("fire", "smoke"):
        lvl = e.get("fire_level") or 2
        seen = cam_live and (t.upper() in badge)
        ver = e.get("verification") or ""
        if st == "verifying":
            verdict, rec = "Waiting for the safety station to confirm", None
            why.append("the camera saw something fire-like; the station sensors are being checked")
        elif ver == "confirmed" or seen:
            verdict = f"REAL {t.upper()} - level {lvl}"
            if ver == "confirmed":
                why.append("camera and safety-station sensors agree")
            if seen:
                why.append("the camera still sees it now")
            rec = "ack" if st in ("open", "escalated") else ("escalate" if lvl >= 3 and st == "acknowledged" else None)
            if rec is None:
                why.append("keep it active until the fire is out, then resolve it")
        elif cam_live:
            verdict, rec = f"{t.title()} no longer seen by the camera", "resolve"
            why.append("check the area on site before closing it")
        else:
            verdict, rec = "Cannot verify - camera offline", "ack" if st in ("open", "escalated") else None
            why.append("no live picture from this camera; treat it as real")
    elif t in ("ppe_missing", "unauthorized_entry"):
        who = "a registered worker" if mask and e.get("worker_id") else             e.get("worker_name") or (e.get("worker_id") if str(e.get("worker_id") or "").startswith("W-")
                                     else "an unregistered person")
        items = ", ".join(ITEM.get(i, i) for i in e.get("items_missing") or [])
        what = f"{who} without {items}" if t == "ppe_missing" else f"{who} in a restricted zone"
        still = cam_live and (("NO " in badge and t == "ppe_missing" and
                               any(i.upper() in badge for i in e.get("items_missing") or []))
                              or ("NOT AUTHORISED" in badge and t == "unauthorized_entry"))
        if not edge:
            verdict, rec = "Cannot verify - edge computer offline", None
            why.append("the AI is not running, so it cannot be checked live")
        elif not cam_live:
            verdict, rec = "Cannot verify - camera offline", None
            why.append(f"camera {e.get('camera_id')} has no live picture")
        elif still:
            verdict = f"STILL HAPPENING: {what}"
            why.append("the camera shows this violation right now")
            if st in ("open", "escalated"):
                rec = "ack"
                why.append("acknowledge it and send someone")
            elif acked_min is not None and acked_min >= 5:
                rec = "escalate"
                why.append(f"acknowledged {acked_min:.0f} min ago but still not fixed")
        elif age_min >= 1:
            verdict, rec = "No longer seen - looks finished", "resolve"
            why.append("the camera does not show this violation any more (fixed, person left, or the edge restarted)")
            if t == "ppe_missing":
                why.append("if the person is still there without PPE the camera re-checks and alerts again")
        else:
            verdict, rec = f"New: {what}", "ack" if st in ("open", "escalated") else None
            why.append("just raised; the camera is still looking")
        if conf is not None and conf < 0.45 and rec != "resolve":
            why.append(f"weak detection (confidence {conf:.2f}) - look at the snapshot; it may be a false alarm")
    else:
        verdict = "Information"
    return {"event_id": e["event_id"], "title": _title(e, mask), "status": st, "verdict": verdict, "recommend": rec,
            "why": why, "time": timeutil.fmt(e.get("started_at"), with_date=False) if e.get("started_at") else "",
            "zone": e.get("zone_name") or e.get("zone_id"), "camera": e.get("camera_id"), "snapshot": e.get("snapshot")}


def _title(e, mask=False):
    if mask:
        e = dict(e, worker_name=None)
    t = e["type"]
    if t == "ppe_missing":
        return "Missing " + ", ".join(ITEM.get(i, i) for i in e.get("items_missing") or []) + \
            (f" - {e['worker_name']}" if e.get("worker_name") else "")
    if t == "unauthorized_entry":
        return "Unauthorised entry" + (f" - {e['worker_name']}" if e.get("worker_name") else "")
    if t in ("fire", "smoke"):
        return f"{t.title()} level {e.get('fire_level') or '?'}"
    if t == "entrance_check":
        return (e.get("details") or {}).get("verdict") or "Entrance check"
    return t.replace("_", " ")


# ---------------------------------------------------------------- planning
HELP = ("I can: analyse and verify alerts; acknowledge, resolve, escalate or mark them as false alarms; allow or refuse "
        "the person at the entrance; open the control room, admin, hardware or entrance pages and admin sections; "
        "enlarge a camera or go back to all cameras; generate reports; mute the alert sound; and answer questions "
        "about past events. I act only when you tell me to.")


def plan(text, user="?", role="viewer", ctx=None, pending=None):
    """-> dict(reply, speak, ui[], execute[], confirm, analysis[], ask_history, intents[])"""
    ctx = dict(ctx or {})
    ctx.setdefault("now", time.time())
    raw, woke = strip_wake(text)
    keep = [w.get("name", "").split()[0] for w in ctx.get("workers") or [] if w.get("name")]
    t = normalise(raw, keep)
    out = {"reply": "", "speak": "", "ui": [], "execute": [], "confirm": None, "analysis": [], "ask_history": None,
           "intents": [], "heard": raw}
    can_act = role in ("supervisor", "admin")
    say = []
    if not t:
        out["reply"] = out["speak"] = "Yes? I am listening."
        return out
    # ---- confirm / cancel a pending plan
    core = re.sub(r"\b(please|now|it|them|all|the|recommendations?|suggestions?|zentrix|that|this|thanks?)\b", " ", t)
    core = re.sub(r"\s+", " ", core).strip()
    if pending and core and re.fullmatch(r"((yes|yeah|yep|ok|okay|sure|fine|right)\s*)*(confirm(ed)?|do|go ahead|proceed|"
                                         r"apply|carry on|execute|accept)?", core):
        out["intents"].append("confirm")
        out["execute"] = list(pending.get("execute") or [])
        out["reply"] = "Confirmed."
        return out
    if pending and re.fullmatch(r"(no|nope|cancel|stop|don't|do not|abort|never mind|leave it|wait)( it| that| please)?", t):
        out["intents"].append("cancel")
        out["reply"] = out["speak"] = "Cancelled. Nothing was changed."
        out["cancel"] = True
        return out
    if re.search(r"\b(help|what can you do|commands)\b", t):
        out["intents"].append("help")
        out["reply"] = HELP
        out["speak"] = "I can analyse and handle alerts, open any page, enlarge cameras and make reports. I act only on your command."
        return out
    global_act = _action_of(t) or _entrance_decision(t)
    last = []                 # records the previous step talked about ('analyse ... and acknowledge THEM')
    confirm_exec, confirm_lines = [], []
    for seg in _split(t):
        intent = None
        # ---- entrance manual decision
        dec = _entrance_decision(seg)
        act = _action_of(seg)
        if dec and not re.search(r"\b(alert|alarm|incident)s?\b", seg):
            intent = "entrance"
            if not can_act:
                say.append(f"Your role ({role}) cannot decide entrance checks.")
            else:
                rows = db.q("SELECT * FROM events WHERE type='entrance_check' AND started_at >= ? ORDER BY started_at DESC "
                            "LIMIT 20", (ctx["now"] - 1800,))
                evs = [db.row_to_event(r) for r in rows]
                pend = [e for e in evs if (e.get("details") or {}).get("result") == "manual"
                        and not (e.get("details") or {}).get("override")]
                g = pend[0] if pend else (evs[0] if evs else None)
                if g is None:
                    say.append("There is no entrance check in the last 30 minutes.")
                else:
                    d = g.get("details") or {}
                    who = g.get("worker_name") or ("unregistered person" if not g.get("worker_id") else g["worker_id"])
                    step = {"kind": "entrance", "event_id": g["event_id"], "decision": dec}
                    if dec == "approved" and d.get("result") == "denied" and not d.get("override"):
                        confirm_exec.append(step)
                        confirm_lines.append(f"Allow {who} in although the camera found missing PPE "
                                             f"({', '.join(d.get('items_missing') or []) or 'see check'})")
                    else:
                        out["execute"].append(step)
                        say.append(f"{'Allowing' if dec == 'approved' else 'Refusing'} {who} at the entrance.")
            out["intents"].append(intent)
            continue
        # ---- analyse / verify
        if re.search(r"\b(analyse|review|verify|check|inspect|examine|assess|evaluate|summarise|summarize|"
                     r"look at|go through|status of)\b", seg) and \
                re.search(r"\b(alerts?|alarms?|incidents?|violations?|events?|everything|situation|recent|active|"
                          r"open|pending|them|fire|smoke)\b", seg) and not re.search(NAV_VERB + r".*\b(page|panel)\b", seg):
            intent = "analyse"
            ids = re.findall(r"evt_[0-9a-f]{6,}", seg)
            rows = _by_ids(ids) if ids else _events(ACTIVE, ctx, seg)
            res = [assess(e, ctx.get("live"), ctx["now"], ctx.get("mask")) for e in rows[:25]]
            out["analysis"] += res
            last = [e["event_id"] for e in rows]
            if not rows:
                say.append("There are no active alerts matching that. Everything is handled.")
            else:
                still = sum(1 for r in res if r["verdict"].startswith(("STILL", "REAL", "New")))
                done = sum(1 for r in res if r["recommend"] == "resolve")
                unk = sum(1 for r in res if r["verdict"].startswith("Cannot"))
                parts = [f"I checked {len(rows)} active alert{'s' if len(rows) != 1 else ''}"
                         + (f" (showing the first 25)" if len(rows) > 25 else "") + "."]
                if still:
                    parts.append(f"{still} still need attention")
                if done:
                    parts.append(f"{done} look finished and can be resolved")
                if unk:
                    parts.append(f"{unk} cannot be verified because the camera or edge is offline")
                say.append(parts[0] + (" " + ", ".join(parts[1:]) + "." if len(parts) > 1 else ""))
                recs = [(r["event_id"], r["recommend"]) for r in res if r["recommend"]]
                if recs and not global_act and can_act:
                    rec_exec = [{"kind": "event", "event_id": i, "action": a} for i, a in recs]
                    n = {a: sum(1 for _, x in recs if x == a) for a in {a for _, a in recs}}
                    lines = [f"{VERB_TEXT[a]} {k} alert{'s' if k != 1 else ''}" for a, k in n.items()]
                    out["suggest"] = {"execute": rec_exec, "summary": "My recommendation: " + "; ".join(lines) + "."}
                    say.append(out["suggest"]["summary"] + " Say 'apply' to do it, or tell me what to do.")
            out["intents"].append(intent)
            continue
        # ---- record actions
        if act and (len(seg.split()) <= 2 or last or re.search(
                r"\b(alerts?|alarms?|incidents?|violations?|events?|them|it|this|that|all|everything|"
                r"these|those|fire|smoke|helmet|vest|gloves|goggles|mask|ppe|evt_\w+|one|recent|last|latest)\b", seg)):
            intent = "action"
            acts = _actions_all(seg)
            choose = len(acts) > 1        # "acknowledge OR resolve": per alert, whichever the verification recommends
            if not can_act:
                say.append(f"Your role ({role}) cannot {VERB_TEXT[act]} alerts - ask a supervisor or admin.")
                out["intents"].append(intent)
                continue
            statuses = tuple(sorted({s for a in acts for s in APPLIES[a]}))
            ids = re.findall(r"evt_[0-9a-f]{6,}", seg)
            if ids:
                rows = _by_ids(ids)
            elif last and (re.search(r"\b(them|these|those|it|all of them|the same)\b", seg) or choose
                           or not re.search(r"\b(alerts?|alarms?|incidents?|violations?|all)\b", seg)):
                rows = _by_ids(last)
            else:
                rows = _events(statuses, ctx, seg)
            if choose:
                pick = []
                for e in rows:
                    r = assess(e, ctx.get("live"), ctx["now"])["recommend"]
                    if r in acts and e["status"] in APPLIES[r]:
                        pick.append((e, r))
                left = len([e for e in rows if e["status"] in statuses]) - len(pick)
                if not pick:
                    say.append("None of those alerts can be " + " or ".join(DONE_TEXT[a].lower() for a in acts)
                               + " on the evidence I have - tell me exactly what to do.")
                    out["intents"].append(intent)
                    continue
                rows = [e for e, _ in pick]
                steps = [{"kind": "event", "event_id": e["event_id"], "action": r} for e, r in pick]
                if left > 0:
                    say.append(f"{left} alert{'s' if left != 1 else ''} left for you to decide (cannot be verified).")
            else:
                rows = [e for e in rows if e["status"] in APPLIES[act]]
            if not rows:
                say.append(f"There is no alert to {VERB_TEXT[act]}" + (" among those." if last else " right now."))
                out["intents"].append(intent)
                continue
            if not choose:
                steps = [{"kind": "event", "event_id": e["event_id"], "action": act} for e in rows]
            if choose:
                n = {}
                for s_ in steps:
                    n[s_["action"]] = n.get(s_["action"], 0) + 1
                if sum(1 for s_ in steps if s_["action"] in ("resolve", "dismiss")) > 1:
                    confirm_exec += steps
                    confirm_lines.append("; ".join(f"{VERB_TEXT[a]} {k}" for a, k in n.items()) + " (as verified)")
                else:
                    out["execute"] += steps
                    say.append("Done as verified: " + ", ".join(f"{DONE_TEXT[a].lower()} {k}" for a, k in n.items()) + ".")
                last = [e["event_id"] for e in rows]
                out["intents"].append(intent)
                continue
            risky = (act in ("resolve", "dismiss") and (len(rows) > 1 or any(e["type"] in ("fire", "smoke") for e in rows))) \
                or (act == "escalate" and len(rows) > 1) or len(rows) > 15
            if risky:
                confirm_exec += steps
                still = [assess(e, ctx.get("live"), ctx["now"]) for e in rows]
                warn = [s for s in still if s["verdict"].startswith(("STILL", "REAL"))]
                confirm_lines.append(f"{VERB_TEXT[act].capitalize()} {len(rows)} alert{'s' if len(rows) != 1 else ''}: "
                                     + "; ".join(_title(e) for e in rows[:6]) + (" ..." if len(rows) > 6 else ""))
                if warn:
                    confirm_lines.append(f"Warning: {len(warn)} of them are still happening on camera "
                                         f"({'; '.join(w['title'] for w in warn[:3])})")
            else:
                out["execute"] += steps
                say.append(f"{DONE_TEXT[act]}: " + "; ".join(_title(e) for e in rows[:5])
                           + (f" and {len(rows) - 5} more" if len(rows) > 5 else "") + ".")
            last = [e["event_id"] for e in rows]
            out["intents"].append(intent)
            continue
        # ---- reports
        if re.search(r"\b(generate|make|create|prepare|download|export|give me|build)\b.*\breports?\b|\breports? (for|of)\b", seg):
            intent = "report"
            p = _parse(seg, ctx)
            f = p["filters"]
            explicit = not any("no time given" in n for n in p["notes"])
            t0, t1 = p["window"]
            if not explicit:
                t0 = timeutil.local(ctx["now"]).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                t1 = ctx["now"]
            fmt = "csv" if re.search(r"\b(csv|excel|spreadsheet)\b", seg) else "pdf"
            op = {"op": "report", "start": t0, "end": t1, "format": fmt,
                  "type": ",".join(f.get("types") or []), "zone": ",".join(f.get("zones") or []),
                  "camera": ",".join(f.get("cameras") or []), "worker": ",".join(f.get("workers") or [])}
            out["ui"].append(op)
            say.append(f"Report from {timeutil.fmt(t0)} to {timeutil.fmt(t1)} is ready in the report generator"
                       + (f" ({fmt.upper()} download started)." if re.search(r"\b(download|export|pdf|csv)\b", seg) else "."))
            if not re.search(r"\b(download|export|pdf|csv)\b", seg):
                op["format"] = None
            out["intents"].append(intent)
            continue
        # ---- cameras
        cam_op = _camera_op(seg, ctx)
        if cam_op:
            intent = "camera"
            out["ui"].append(cam_op[0])
            say.append(cam_op[1])
            out["intents"].append(intent)
            continue
        # ---- open one alert's details
        if re.search(r"\b(open|show|display|view)\b.*\b(details?|snapshot|clip|video)\b", seg) or \
                re.search(r"\b(open|show)\b.*\b(last|latest|newest)\s+(alert|alarm|incident)\b(?!s)", seg) or \
                re.search(r"\b(open|show)\b.*\bevt_[0-9a-f]{6,}", seg):
            ids = re.findall(r"evt_[0-9a-f]{6,}", seg)
            rows = _by_ids(ids) if ids else _events(ACTIVE + ("resolved", "closed", "dismissed"), ctx,
                                                    seg if re.search(r"\b(last|latest)\b", seg) else seg + " latest alert")
            if rows:
                out["ui"].append({"op": "event", "id": rows[0]["event_id"]})
                say.append(f"Opening {_title(rows[0])}.")
                out["intents"].append("details")
                continue
        # ---- navigation
        nav = _nav(seg)
        if nav:
            out["ui"].append(nav[0])
            say.append(f"Opening {nav[1]}.")
            out["intents"].append("navigate")
            continue
        # ---- sound / theme
        if re.search(r"\b(unmute|sound on|turn on (the )?(sound|beep|alarm sound)|enable (the )?sound)\b", seg):
            out["ui"].append({"op": "mute", "on": False})
            say.append("Alert sound is on.")
            out["intents"].append("sound")
            continue
        if re.search(r"\b(mute|silence|quiet|sound off|turn off (the )?(sound|beep|alarm sound)|stop (the )?(beep|sound))\b", seg):
            out["ui"].append({"op": "mute", "on": True})
            say.append("Alert sound on this screen is muted. Site sirens stop when the alert is acknowledged.")
            out["intents"].append("sound")
            continue
        if re.search(r"\b(dark|night) (mode|theme)\b", seg) or re.search(r"\b(light|day) (mode|theme)\b", seg):
            out["ui"].append({"op": "theme", "mode": "dark" if re.search(r"\b(dark|night)\b", seg) else "light"})
            say.append("Theme changed.")
            out["intents"].append("theme")
            continue
        if re.search(r"\b(refresh|reload)\b", seg):
            out["ui"].append({"op": "reload"})
            say.append("Reloading the page.")
            out["intents"].append("reload")
            continue
        # ---- anything else: a question about the history (grounded assistant)
        out["ask_history"] = raw if len(_split(t)) == 1 else seg
        out["intents"].append("question")
    if confirm_exec:
        out["confirm"] = {"execute": confirm_exec, "lines": confirm_lines}
        say.append("Please confirm: " + " | ".join(confirm_lines) + ". Say 'confirm' or 'cancel'.")
    out["reply"] = " ".join(say)
    out["speak"] = out["reply"][:400]
    return out


def _nav(seg):
    has_verb = re.search(NAV_VERB, seg) or re.search(r"\b(show|display)\b.*\b(page|panel|dashboard|screen|section|tab)\b", seg)
    short = any(re.fullmatch(rf"(the )?({rx})( page| panel| dashboard| screen)?", seg) for rx, _, _ in PAGES)
    for rx, sec, name in SECTIONS:
        if re.search(rx, seg) and (has_verb or re.search(r"\b(page|section|tab)\b", seg)) and \
                not re.search(r"\b(control|hardware|entrance)\b", seg):
            if sec in ("cameras",) and re.search(r"\bcamera\b", seg) and not re.search(r"\bzones?\b", seg):
                continue
            return {"op": "navigate", "url": f"/admin#{sec}"}, name
    for rx, url, name in PAGES:
        if re.search(rx, seg) and (has_verb or short or re.search(r"\b(page|panel|dashboard|screen)\b", seg)):
            return {"op": "navigate", "url": url}, name
    return None


def _camera_op(seg, ctx):
    cams = ctx.get("cameras") or {}
    zones = ctx.get("zones") or {}
    if re.search(r"\b(full ?screen)\b", seg) and not re.search(r"\b(exit|leave|close|stop)\b", seg):
        return {"op": "fullscreen"}, "Full screen camera wall."
    if re.search(r"\b(show all (the )?cameras|all cameras|exit (full|focus|large|big)|back to (the )?(grid|all|normal)|grid view|"
                 r"normal view|minimi[sz]e|restore|shrink|smaller|exit full ?screen|close (the )?(camera|big view))\b", seg):
        return {"op": "unfocus"}, "Showing all cameras."
    if re.search(r"\b(focus view|big view|large view|alarm camera first)\b", seg):
        return {"op": "view", "mode": "big"}, "Focus view: the camera with an alarm leads."
    strong = re.search(r"\b(enlarge|zoom|maximi[sz]e|bigger|expand|magnify|full view|make (it )?big)\b", seg)
    weak = re.search(r"\b(focus( on)?|show|open|watch|look at|display|view)\b", seg)
    noun = re.search(r"\b(cam|camera|cctv|feed)\b|\bcam_?\d", seg)
    if not ((strong and (noun or re.search(r"\b(bay|warehouse|assembly|entrance|welding)\b", seg))) or (weak and noun)):
        return None
    target = None
    for cid, c in cams.items():
        num = int(re.sub(r"\D", "", cid) or 0)
        if cid in seg or re.search(rf"\b(cam|camera)\s*(no\.?|number)?\s*0?{num}\b", seg):
            target = cid
    if target is None:
        for cid, c in cams.items():
            z = zones.get(c.get("default_zone")) or {}
            words = {w for w in re.split(r"\W+", ((z.get("name") or "") + " " + (c.get("name") or "")).lower()) if len(w) >= 4}
            words |= set((c.get("default_zone") or "").split("_")) - {"main", "line", "bay", "zone"}
            if any(re.search(rf"\b{re.escape(w)}\b", seg) for w in words if len(w) >= 4):
                target = cid
                break
    if target is None:
        live = [c for c in (ctx.get("live") or {}).get("cameras", []) if c.get("badge") and "APPROVED" not in c["badge"]]
        if live:
            target = live[0]["camera_id"]
    if target is None:
        return {"op": "view", "mode": "big"}, "Which camera? Say for example 'enlarge camera 1' or 'enlarge the welding bay'."
    c = cams.get(target) or {}
    if c.get("role") == "entrance":
        return {"op": "navigate", "url": "/entrance"}, "The entrance camera is on the entrance dashboard - opening it."
    return {"op": "focus", "cam": target}, f"Enlarged {c.get('name') or target} ({target})."
