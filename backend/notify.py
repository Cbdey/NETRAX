"""Notifications + escalation (F16, final-round section 5).

Channels: Telegram (bot -> group), phone voice call + SMS (Twilio), e-mail (SMTP, e.g. Gmail).
- Asynchronous: a background thread delivers; detection and the dashboard never wait for a provider.
- Every message carries the event ID; one DB row per (event, channel, recipient) with its status.
- Bounded retries with back-off for recoverable failures (network, timeouts, 429/5xx, busy / no answer).
- Delivery is recorded only from the PROVIDER's answer: Telegram ok=true, Twilio call 'completed' /
  SMS 'delivered', SMTP accepted. A channel without credentials is 'not_configured' - never 'delivered'.
- The same continuous incident is never sent twice on a channel; a NEW or ESCALATING incident is.
  Fire / smoke are never delayed or merged by a cooldown.

Escalation timers (unchanged): Critical 60 s, High 300 s without acknowledgement.
"""
import heapq
import itertools
import json
import os
import smtplib
import threading
import time
from email.message import EmailMessage

import requests
import yaml

from . import db, timeutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ESCALATE_AFTER_S = {"Critical": 60, "High": 300}
RECIPIENT = {"Critical": "Safety officer", "High": "Zone supervisor"}
FINAL = {"delivered", "sent", "failed", "not_configured", "skipped"}

LEVEL_TITLE = {
    1: "WARNING - Small {what} detected. Immediate inspection required.",
    2: "CRITICAL ALERT - {What} detected. Immediate emergency response required.",
    3: "VERY CRITICAL - MAJOR {WHAT} DETECTED. EVACUATE THE AFFECTED AREA IMMEDIATELY.",
}
VERIF_TEXT = {"confirmed": "safety station CONFIRMS", "hardware_only": "safety station alarm (no camera)",
              "hardware_offline": "station offline - camera only, not cross-checked",
              "no_station": "no station in this zone - camera only, not cross-checked",
              "pending": "waiting for the safety station", "hardware_clear": "station clear"}
LEVEL_ACTION = {
    1: "Inspect the area now. Keep an extinguisher ready. Confirm on the dashboard.",
    2: "Start emergency response: alert the fire team, isolate power/gas if safe, use an extinguisher only if trained.",
    3: "Evacuate the affected area now. Call the fire brigade (101). Follow the site evacuation plan.",
}


def _load_yaml(name):
    p = os.path.join(ROOT, "config", name)
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_policy():
    return _load_yaml("notify.yaml")


def _e164(x):
    """Phone numbers in E.164 (+<country><number>). YAML turns 916372863454 (no quotes) into an int and Twilio
    rejects numbers without '+': accept both and add the '+'. 10-digit Indian numbers get +91."""
    s = "".join(ch for ch in str(x).strip() if ch.isdigit() or ch == "+")
    if not s:
        return ""
    if not s.startswith("+"):
        s = "+91" + s if len(s) == 10 else "+" + s.lstrip("0")
    return s


def load_secrets():
    """config/secrets.local.yaml (git-ignored), environment variables override."""
    s = _load_yaml("secrets.local.yaml")
    tg, tw, em = s.get("telegram") or {}, s.get("twilio") or {}, s.get("email") or {}
    env = os.environ.get
    out = {
        "telegram": {"bot_token": env("TELEGRAM_BOT_TOKEN") or tg.get("bot_token") or "",
                     "chat_id": str(env("TELEGRAM_CHAT_ID") or tg.get("chat_id") or "")},
        "twilio": {"account_sid": env("TWILIO_ACCOUNT_SID") or tw.get("account_sid") or "",
                   "auth_token": env("TWILIO_AUTH_TOKEN") or tw.get("auth_token") or "",
                   "from_number": env("TWILIO_FROM") or tw.get("from_number") or "",
                   "mode": env("TWILIO_MODE") or tw.get("mode") or "auto",
                   "to_numbers": [x for x in ((env("ALERT_PHONE_TO") or "").split(",") if env("ALERT_PHONE_TO")
                                              else (tw.get("to_numbers") or [])) if x]},
        "email": {"smtp_host": em.get("smtp_host") or "smtp.gmail.com", "smtp_port": int(em.get("smtp_port") or 465),
                  "username": env("SMTP_USER") or em.get("username") or "",
                  "app_password": env("SMTP_PASSWORD") or em.get("app_password") or "",
                  "to": [x for x in ((env("ALERT_EMAIL_TO") or "").split(",") if env("ALERT_EMAIL_TO")
                                     else (em.get("to") or [])) if x]},
        "assistant": s.get("assistant") or {},
    }
    tw = out["twilio"]
    tw["from_number"] = _e164(tw["from_number"]) if tw["from_number"] else ""
    tw["to_numbers"] = [n for n in (_e164(x) for x in tw["to_numbers"]) if n]
    return out


def configured(sec=None):
    sec = sec or load_secrets()
    tg, tw, em = sec["telegram"], sec["twilio"], sec["email"]
    twilio_ok = bool(tw["account_sid"] and tw["auth_token"] and tw["from_number"] and tw["to_numbers"])
    return {"telegram": bool(tg["bot_token"] and tg["chat_id"]), "call": twilio_ok, "sms": twilio_ok,
            "email": bool(em["username"] and em["app_password"] and em["to"])}


# ------------------------------------------------------------------ message text
def _what(e):
    t = e.get("type") or "event"
    if t == "ppe_missing":
        return "Missing PPE: " + ", ".join(i.upper() for i in (e.get("items_missing") or [])) or "PPE violation"
    if t == "unauthorized_entry":
        return "Unauthorised entry into restricted zone"
    if t == "entrance_check":
        d = e.get("details") or {}
        return f"Entrance {d.get('result', '').upper() or 'check'}" + \
            (f": missing {', '.join(i.upper() for i in e.get('items_missing') or [])}" if e.get("items_missing") else "")
    return t.replace("_", " ").title()


def headline(e):
    t = e.get("type")
    lvl = e.get("fire_level")
    if t in ("fire", "smoke") and lvl:
        w = "fire" if t == "fire" else "smoke"
        return LEVEL_TITLE[int(lvl)].format(what=w, What=w.title(), WHAT=w.upper())
    return f"{(e.get('severity') or '').upper()} - {_what(e)}"


def action_text(e):
    t = e.get("type")
    if t in ("fire", "smoke"):
        return LEVEL_ACTION.get(int(e.get("fire_level") or 2), LEVEL_ACTION[2])
    if t == "ppe_missing":
        return f"Stop the work and make the worker wear: {', '.join(e.get('items_missing') or [])}."
    if t == "unauthorized_entry":
        return "Escort the person out of the restricted zone and check their authorisation."
    if t == "entrance_check":
        return "Do not allow entry until all required PPE is worn (manual check if unclear)."
    return "Check the dashboard and respond."


def location_text(e):
    loc = e.get("location")
    if isinstance(loc, str):
        try:
            loc = json.loads(loc)
        except ValueError:
            loc = None
    if not loc or not loc.get("valid"):
        return f"Location unavailable - zone {e.get('zone_name') or e.get('zone_id') or '-'}"
    s = f"{loc['lat']:.5f}, {loc['lon']:.5f} ({loc.get('source_label') or loc.get('source')})"
    if loc.get("map_url"):
        s += f" {loc['map_url']}"
    return s


def who_text(e, policy):
    if not policy.get("include_worker_names", True):
        return None
    wid = e.get("worker_id") or ""
    if wid.startswith("W-"):
        return f"{e.get('worker_name') or ''} ({wid})".strip()
    return None


def describe(e):
    """One line (kept for old callers, reports and tests)."""
    what = e["type"].upper() if e["type"] != "ppe_missing" else \
        "NO " + " + ".join(i.upper() for i in e["items_missing"])
    who = f" - worker {e['worker_id']}" if e.get("worker_id") else ""
    conf = f", confidence {e['confidence']:.2f}" if e.get("confidence") is not None else ""
    return (f"[{e['severity']}] {what} - {e.get('zone_name') or e.get('zone_id')} "
            f"({e.get('camera_id') or '-'}){who}{conf}")


def build(e, reason, policy):
    when = timeutil.fmt(e.get("started_at") or e.get("received_at") or time.time())
    cam = e.get("camera_id") or e.get("device_id") or "-"
    cam_name = e.get("camera_name") or ""
    zone = e.get("zone_name") or e.get("zone_id") or "-"
    who = who_text(e, policy)
    lvl = f" (Level {e['fire_level']})" if e.get("fire_level") and e.get("type") in ("fire", "smoke") else ""
    url = f"{(policy.get('dashboard_url') or 'http://127.0.0.1:8000').rstrip('/')}/#event={e['event_id']}"
    lines = [f"NETRAX {('- ' + reason.upper()) if reason not in ('new alert', 'new') else 'ALERT'}",
             headline(e),
             f"Type: {_what(e)}{lvl} | Severity: {e.get('severity') or '-'} | Status: {e.get('status') or '-'}",
             f"Zone: {zone} | Camera: {cam}{(' (' + cam_name + ')') if cam_name and cam_name not in (zone, cam) else ''}",
             f"Time: {when}",
             f"Location: {location_text(e)}"]
    if who:
        lines.append(f"Worker: {who}")
    if e.get("verification"):
        lines.append("Sensor check: " + VERIF_TEXT.get(e["verification"], e["verification"]))
    lines += [f"Action: {action_text(e)}", f"Event ID: {e['event_id']}", f"Open: {url}"]
    long = "\n".join(lines)
    short = (f"NETRAX {e.get('severity') or ''}: {headline(e)} Zone {zone}, cam {cam}, {when}. "
             f"{action_text(e)} ID {e['event_id']}")
    spoken = (f"NETRAX safety alert. {headline(e)} Zone: {zone}. Camera {cam}. "
              f"{action_text(e)}")
    return {"long": long, "short": short[:600], "spoken": spoken}


# ------------------------------------------------------------------ providers
class Result:
    def __init__(self, status, provider_id=None, provider_status=None, error=None, recoverable=False):
        self.status, self.provider_id, self.provider_status = status, provider_id, provider_status
        self.error, self.recoverable = error, recoverable


def _http_fail(r):
    rec = r.status_code == 429 or r.status_code >= 500
    try:
        msg = r.json()
        msg = msg.get("description") or msg.get("message") or str(msg)
    except ValueError:
        msg = r.text[:200]
    return Result("failed", provider_status=f"HTTP {r.status_code}", error=str(msg)[:300], recoverable=rec)


def send_telegram(sec, text, photo=None):
    tg = sec["telegram"]
    if not (tg["bot_token"] and tg["chat_id"]):
        return Result("not_configured", error="Telegram bot token / chat id not set")
    base = f"https://api.telegram.org/bot{tg['bot_token']}"
    try:
        if photo and os.path.exists(photo):
            with open(photo, "rb") as f:
                r = requests.post(f"{base}/sendPhoto", data={"chat_id": tg["chat_id"], "caption": text[:1024]},
                                  files={"photo": f}, timeout=10)
            if r.status_code == 200 and len(text) > 1024:
                requests.post(f"{base}/sendMessage", json={"chat_id": tg["chat_id"], "text": text}, timeout=10)
        else:
            r = requests.post(f"{base}/sendMessage", json={"chat_id": tg["chat_id"], "text": text}, timeout=10)
    except requests.RequestException as ex:
        return Result("failed", error=f"network: {ex.__class__.__name__}", recoverable=True)
    if r.status_code == 200 and r.json().get("ok"):
        mid = (r.json().get("result") or {}).get("message_id")
        return Result("delivered", provider_id=str(mid), provider_status="ok (accepted by Telegram)")
    return _http_fail(r)


def _twilio(sec, path, data=None):
    tw = sec["twilio"]
    url = f"https://api.twilio.com/2010-04-01/Accounts/{tw['account_sid']}/{path}"
    auth = (tw["account_sid"], tw["auth_token"])
    if data is None:
        return requests.get(url, auth=auth, timeout=10)
    return requests.post(url, data=data, auth=auth, timeout=10)


# Twilio TRIAL accounts (2026 rules): a call may only use one of Twilio's template URLs and an SMS body must be a
# template NAME - no custom text, no From. docs: twilio.com/docs/usage/trials/try-out-voice , .../try-out-sms
TRIAL_CALL_URL = "https://webhooks.twilio.com/v1/Voice/Template/voice_text_to_speech"
TRIAL_SMS_TEMPLATE = "sms_internal_alerts"
_TRIAL = {"on": None}          # None = not known yet (mode auto), True after Twilio refused custom content


def _twilio_mode(sec):
    m = str(sec["twilio"].get("mode") or "auto").lower()
    if m == "trial":
        return True
    if m == "full":
        return False
    return bool(_TRIAL["on"])


def _is_trial_refusal(r):
    try:
        msg = (r.json().get("message") or "").lower()
    except ValueError:
        msg = r.text.lower()
    return r.status_code == 400 and "trial" in msg


def send_call(sec, to, spoken, policy):
    tw = sec["twilio"]
    if not (tw["account_sid"] and tw["auth_token"] and tw["from_number"]):
        return Result("not_configured", error="Twilio account SID / token / number not set")
    voice = (policy.get("phone") or {}).get("voice", "alice")
    rep = int((policy.get("phone") or {}).get("repeat_message", 2))
    safe = spoken.replace("&", "and").replace("<", " ").replace(">", " ")
    twiml = "<Response>" + "<Pause length=\"1\"/>".join(
        f"<Say voice=\"{voice}\">{safe}</Say>" for _ in range(max(1, rep))) + "</Response>"
    try:
        if not _twilio_mode(sec):
            r = _twilio(sec, "Calls.json", {"To": to, "From": tw["from_number"], "Twiml": twiml})
            if _is_trial_refusal(r) and str(tw.get("mode") or "auto").lower() == "auto":
                _TRIAL["on"] = True                     # remember: this is a trial account
            else:
                return _call_result(r, False)
        r = _twilio(sec, "Calls.json", {"To": to, "Url": TRIAL_CALL_URL})
        return _call_result(r, True)
    except requests.RequestException as ex:
        return Result("failed", error=f"network: {ex.__class__.__name__}", recoverable=True)


def _call_result(r, trial):
    if r.status_code in (200, 201):
        j = r.json()
        return Result("sending", provider_id=j.get("sid"),
                      provider_status=(j.get("status") or "") + (" | TRIAL: Twilio template voice, not the alert text"
                                                                if trial else ""))
    return _http_fail(r)


def send_sms(sec, to, text):
    tw = sec["twilio"]
    if not (tw["account_sid"] and tw["auth_token"] and tw["from_number"]):
        return Result("not_configured", error="Twilio account SID / token / number not set")
    try:
        if not _twilio_mode(sec):
            r = _twilio(sec, "Messages.json", {"To": to, "From": tw["from_number"], "Body": text})
            if _is_trial_refusal(r) and str(tw.get("mode") or "auto").lower() == "auto":
                _TRIAL["on"] = True
            else:
                return _sms_result(r, False)
        r = _twilio(sec, "Messages.json", {"To": to, "Body": TRIAL_SMS_TEMPLATE})
        return _sms_result(r, True)
    except requests.RequestException as ex:
        return Result("failed", error=f"network: {ex.__class__.__name__}", recoverable=True)


def _sms_result(r, trial):
    if r.status_code in (200, 201):
        j = r.json()
        return Result("sending", provider_id=j.get("sid"),
                      provider_status=(j.get("status") or "") + (" | TRIAL: Twilio template text, not the alert text"
                                                                if trial else ""))
    return _http_fail(r)


CALL_FINAL = {"completed": "delivered", "busy": "failed", "failed": "failed", "no-answer": "failed",
              "canceled": "failed"}
SMS_FINAL = {"delivered": "delivered", "sent": "sent", "undelivered": "failed", "failed": "failed"}


def twilio_status(sec, channel, sid):
    path = f"Calls/{sid}.json" if channel == "call" else f"Messages/{sid}.json"
    try:
        r = _twilio(sec, path)
        if r.status_code != 200:
            return None, None
        j = r.json()
        st = j.get("status")
        final = (CALL_FINAL if channel == "call" else SMS_FINAL).get(st)
        err = j.get("error_message") or (f"Twilio {st}" if final == "failed" else None)
        return st, (final, err)
    except requests.RequestException:
        return None, None


def send_email(sec, subject, body, photo=None):
    em = sec["email"]
    if not (em["username"] and em["app_password"] and em["to"]):
        return Result("not_configured", error="SMTP user / app password / recipients not set")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, em["username"], ", ".join(em["to"])
    msg.set_content(body)
    if photo and os.path.exists(photo):
        with open(photo, "rb") as f:
            msg.add_attachment(f.read(), maintype="image", subtype="jpeg", filename=os.path.basename(photo))
    try:
        if int(em["smtp_port"]) == 465:
            with smtplib.SMTP_SSL(em["smtp_host"], 465, timeout=15) as s:
                s.login(em["username"], em["app_password"])
                refused = s.send_message(msg)
        else:
            with smtplib.SMTP(em["smtp_host"], int(em["smtp_port"]), timeout=15) as s:
                s.starttls()
                s.login(em["username"], em["app_password"])
                refused = s.send_message(msg)
    except smtplib.SMTPAuthenticationError as ex:
        return Result("failed", error=f"login refused: {ex.smtp_code}", recoverable=False)
    except (smtplib.SMTPException, OSError) as ex:
        return Result("failed", error=f"{ex.__class__.__name__}: {str(ex)[:150]}", recoverable=True)
    if refused:
        return Result("failed", error=f"refused recipients: {list(refused)}", recoverable=False)
    return Result("sent", provider_status="accepted by mail server")


# ------------------------------------------------------------------ dispatcher
class Dispatcher:
    """Background delivery queue: fire first, then by severity; retries never block new alerts."""
    PRIO = {"fire": 0, "smoke": 0}

    def __init__(self):
        self.cv = threading.Condition()
        self.heap = []                       # [not_before, prio, seq, job]
        self.seq = itertools.count()
        self.watch = []                      # twilio sids being followed
        self.on_update = None                # callback(row dict) -> dashboard broadcast
        self.started = False
        self.stats = {"sent": 0, "failed": 0, "retries": 0, "last_ok": {}, "last_error": {}}
        self.offline = bool(os.environ.get("SENTINELEYE_NO_NOTIFY"))   # tests: never touch the network

    def start(self):
        if not self.started:
            self.started = True
            threading.Thread(target=self._loop, daemon=True, name="notify").start()
            threading.Thread(target=self._follow, daemon=True, name="notify-status").start()

    def pending(self):
        with self.cv:
            return len(self.heap) + len(self.watch)

    def put(self, job, delay=0.0):
        prio = self.PRIO.get(job["event"].get("type"), {"Critical": 1, "High": 2}.get(job["event"].get("severity"), 3))
        with self.cv:
            heapq.heappush(self.heap, [time.time() + delay, prio, next(self.seq), job])
            self.cv.notify()

    def _update(self, row_id, **kw):
        if not kw:
            return
        sets = ", ".join(f"{k}=?" for k in kw)
        db.x(f"UPDATE notifications SET {sets} WHERE id=?", (*kw.values(), row_id))
        if self.on_update:
            try:
                self.on_update(db.q("SELECT * FROM notifications WHERE id=?", (row_id,))[0])
            except Exception:              # noqa: BLE001
                pass

    def _loop(self):
        while True:
            with self.cv:
                while not self.heap or self.heap[0][0] > time.time():
                    wait = (self.heap[0][0] - time.time()) if self.heap else None
                    self.cv.wait(timeout=wait if wait is None or wait > 0 else 0.01)
                job = heapq.heappop(self.heap)[3]
            try:
                self._deliver(job)
            except Exception as ex:          # noqa: BLE001 - a provider bug must not stop the queue
                self._update(job["row"], status="failed", error=f"internal: {ex!r}"[:300], finished_at=time.time())

    def _deliver(self, job):
        e, ch, sec, pol = job["event"], job["channel"], load_secrets(), job["policy"]
        job["attempt"] += 1
        self._update(job["row"], status="sending", attempts=job["attempt"], sent_at=time.time())
        msg = job["msg"]
        photo = e.get("snapshot_path")
        if self.offline:
            res = Result("not_configured", error="notifications disabled (SENTINELEYE_NO_NOTIFY)")
        elif ch == "telegram":
            res = send_telegram(sec, msg["long"], photo)
        elif ch == "call":
            res = send_call(sec, job["to"], msg["spoken"], pol)
        elif ch == "sms":
            res = send_sms(sec, job["to"], msg["short"])
        elif ch == "email":
            res = send_email(sec, f"[NETRAX {e.get('severity') or ''}] {headline(e)[:90]} - {e['event_id']}",
                             msg["long"], photo)
        else:
            res = Result("failed", error=f"unknown channel {ch}")
        now = time.time()
        if res.status == "sending":                       # Twilio accepted the request: follow it
            self._update(job["row"], status="sending", provider_id=res.provider_id, provider_status=res.provider_status)
            with self.cv:
                self.watch.append({"job": job, "sid": res.provider_id, "until": now + pol.get("retry", {}).get(
                    "call_status_poll_s", 90), "note": " | TRIAL template" if "TRIAL" in (res.provider_status or "") else ""})
            return
        self._finish(job, res, now)

    def _finish(self, job, res, now):
        retry = job["policy"].get("retry") or {}
        maxa, back = int(retry.get("max_attempts", 3)), list(retry.get("backoff_s") or [2, 6, 15])
        if res.status == "failed" and res.recoverable and job["attempt"] < maxa:
            self.stats["retries"] += 1
            self._update(job["row"], status="retrying", error=res.error, provider_status=res.provider_status)
            self.put(job, delay=back[min(job["attempt"] - 1, len(back) - 1)])
            return
        lat = (now - job["queued"]) * 1000
        self._update(job["row"], status=res.status, error=res.error, provider_status=res.provider_status,
                     provider_id=res.provider_id or None, finished_at=now,
                     delivered=1 if res.status == "delivered" else 0,
                     latency_ms=lat if res.status in ("delivered", "sent") else None)
        if res.status in ("delivered", "sent"):
            self.stats["sent"] += 1
            self.stats["last_ok"][job["channel"]] = now
            from . import perf
            perf.add(f"notify_{job['channel']}_ms", lat)
        elif res.status == "failed":
            self.stats["failed"] += 1
            self.stats["last_error"][job["channel"]] = (now, res.error)

    def _follow(self):
        while True:
            time.sleep(3)
            with self.cv:
                items = list(self.watch)
            if not items:
                continue
            sec = load_secrets()
            for w in items:
                job = w["job"]
                st, fin = twilio_status(sec, job["channel"], w["sid"])
                now = time.time()
                if st:
                    st = st + w.get("note", "")
                    self._update(job["row"], provider_status=st)
                done = fin and fin[0]
                if done or now > w["until"]:
                    with self.cv:
                        if w in self.watch:
                            self.watch.remove(w)
                    if done:
                        busy = st.split(" |")[0] in ("busy", "no-answer")
                        self._finish(job, Result(fin[0], provider_id=w["sid"], provider_status=st, error=fin[1],
                                                 recoverable=busy), now)
                    else:
                        # provider never reported a final state: say so, do not claim delivery
                        self._finish(job, Result("sent", provider_id=w["sid"], provider_status=f"{st or '?'} "
                                                 "(no final status from Twilio yet)"), now)


DISPATCHER = Dispatcher()


ALL_CHANNELS = ("telegram", "call", "sms", "email")


def enabled_channels(policy=None):
    """config/notify.yaml enabled_channels (default: all four). Disabled channels are never queued or shown."""
    policy = policy if policy is not None else load_policy()
    en = os.environ.get("NETRAX_CHANNELS")
    en = en.split(",") if en else policy.get("enabled_channels")
    return [c for c in ALL_CHANNELS if en is None or c in en]


def channels_for(e, reason, policy):
    return [c for c in _channels_for(e, reason, policy) if c in enabled_channels(policy)]


def _channels_for(e, reason, policy):
    rules = policy.get("rules") or {}
    if reason.startswith("ESCALATED"):
        return list(rules.get("escalation") or [])
    t, sev = e.get("type"), e.get("severity")
    if t in ("fire", "smoke") and e.get("fire_level"):
        key = f"fire_level_{int(e['fire_level'])}"
        if key in rules:
            return list(rules[key])
    if t == "entrance_check":
        res = (e.get("details") or {}).get("result")
        return list(rules.get(f"entrance_{res}") or []) if res else []
    for key in (f"{t}_{sev}", t, sev):
        if key in rules:
            return list(rules[key] or [])
    return []


def _recent_same(e, policy):
    """A different event of the same zone+type was already sent a moment ago (flood control)."""
    cd = policy.get("cooldown") or {}
    if e.get("type") in (cd.get("never_for") or ["fire", "smoke"]) or e.get("severity") == "Critical":
        return False                      # fire / smoke and Critical incidents are never held back
    win = float(cd.get("same_zone_type_s", 60))
    r = db.q("SELECT n.id FROM notifications n JOIN events e ON e.event_id = n.event_id WHERE e.zone_id=? "
             "AND e.type=? AND n.event_id != ? AND n.channel != 'telegram' AND n.queued_at > ? LIMIT 1",
             (e.get("zone_id"), e.get("type"), e["event_id"], time.time() - win))
    return bool(r)


def enqueue(e, reason="new alert", recipient=None, kind=None, channels=None):
    """Queue an incident for every channel its policy names. Returns the queued rows (dashboard feed)."""
    policy = load_policy()
    timeutil.set_tz(policy.get("timezone"))
    chans = [c for c in (channels if channels is not None else channels_for(e, reason, policy))
             if c in enabled_channels(policy)]
    if not chans:
        return []
    kind = kind or f"{reason}|L{e.get('fire_level') or 0}|{e.get('severity')}"
    if reason in ("new alert", "new") and _recent_same(e, policy):
        chans = [c for c in chans if c == "telegram"]
    sec = load_secrets()
    msg = build(e, reason, policy)
    now = time.time()
    out = []
    recip = recipient or RECIPIENT.get(e.get("severity")) or "operator"
    for ch in chans:
        if db.q("SELECT id FROM notifications WHERE event_id=? AND channel=? AND kind=?", (e["event_id"], ch, kind)):
            continue                                  # same continuous incident: never twice on a channel
        targets = sec["twilio"]["to_numbers"] if ch in ("call", "sms") else [None]
        for to in (targets or [None]):
            label = to or (", ".join(sec["email"]["to"]) if ch == "email" else
                           (f"group {sec['telegram']['chat_id']}" if ch == "telegram" and sec["telegram"]["chat_id"]
                            else recip))
            text = msg["spoken"] if ch == "call" else msg["short"] if ch == "sms" else msg["long"]
            cur = db.x("INSERT INTO notifications (event_id, channel, recipient, message, delivered, at, status, attempts, "
                       "kind, queued_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (e["event_id"], ch, label, text, 0, now, "queued", 0, kind, now))
            job = {"event": dict(e), "channel": ch, "to": to, "row": cur.lastrowid, "attempt": 0, "msg": msg,
                   "policy": policy, "queued": now}
            if ch in ("call", "sms") and not to:
                DISPATCHER._update(cur.lastrowid, status="not_configured", error="no phone numbers configured",
                                   finished_at=now)
            else:
                DISPATCHER.put(job)
            out.append({"id": cur.lastrowid, "event_id": e["event_id"], "channel": ch, "recipient": label,
                        "message": text, "status": "queued", "at": now})
    DISPATCHER.start()
    return out


def send(e, reason="new alert", recipient=None):
    """Old synchronous entry point: now only queues (never blocks). Returns the first queued row or None."""
    rows = enqueue(e, reason, recipient)
    return rows[0] if rows else None


def status():
    """Provider overview for the system-health panel."""
    conf = configured()
    s = DISPATCHER.stats
    out = {}
    en = enabled_channels()
    for ch, ok in conf.items():
        if ch not in en:
            continue
        last = db.q("SELECT status, error, finished_at FROM notifications WHERE channel=? AND status IN "
                    "('delivered','sent','failed') ORDER BY id DESC LIMIT 1", (ch,))
        out[ch] = {"configured": ok, "last": last[0] if last else None}
    return {"channels": out, "queue": DISPATCHER.pending(), "retries": s["retries"],
            "failed": s["failed"], "sent": s["sent"]}


def due_for_escalation(now):
    out = []
    for e in db.q("SELECT event_id, severity, received_at FROM events WHERE status='open'"):
        limit = ESCALATE_AFTER_S.get(e["severity"])
        if limit and now - (e["received_at"] or now) >= limit:
            out.append(e["event_id"])
    return out
