"""Notifications + escalation (F16, final-round section 5).

Channels: Telegram (bot -> group), phone voice call, SMS (Twilio), e-mail (SMTP, e.g. Gmail).
11 Nov: a PHONE CALL for EVERY fire / smoke detection (any level; camera or safety station), never for PPE / access.
Providers are tried in the order of config/notify.yaml phone.provider (e.g. [android, callmebot]):
  callmebot (default) - free CallMeBot API: a real Telegram VOICE call that reads the alert aloud to each supervisor
                        (@username or +91 number; each person sends /start to @CallMeBot_txtbot once). Needs internet.
  android             - an Android phone on the laptop's USB (adb): a normal SIM call to each number. No internet needed;
                        the phone only rings (no spoken text) - the Telegram message carries the details.
  twilio              - the old Twilio voice call (kept, not recommended for India).
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
import re
import shutil
import smtplib
import subprocess
import threading
import time
from email.message import EmailMessage

import requests
import yaml

from . import db, timeutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ESCALATE_AFTER_S = {"Critical": 60, "High": 300}
RECIPIENT = {"Critical": "Safety officer", "High": "Zone supervisor"}
FINAL = {"delivered", "sent", "failed", "not_configured", "skipped", "expired", "in_digest"}
# 12 Nov: errors that mean "no internet right now" -> wait and resend, never counted as a failed try
NET_ERR = ("network:", "gaierror", "getaddrinfo", "ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout",
           "timed out", "NewConnectionError", "Max retries exceeded", "ConnectionRefused", "ConnectionReset",
           "Network is unreachable", "No route to host")


def is_network(err):
    return any(k in str(err or "") for k in NET_ERR)

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
    alt = os.environ.get("SENTINELEYE_SECRETS")          # tests: own secrets file, never the real one
    s = (yaml.safe_load(open(alt, encoding="utf-8")) or {}) if alt and os.path.exists(alt) else \
        ({} if alt else _load_yaml("secrets.local.yaml"))
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
        # 11 Nov: phone-call recipients for CallMeBot / Android ("@telegram_username" or "+9198xxxxxxxx")
        "call": {"to": [str(x).strip() for x in ((env("ALERT_CALL_TO") or "").split(",") if env("ALERT_CALL_TO")
                                                   else ((s.get("call") or {}).get("to") or [])) if str(x).strip()],
                 "adb_path": env("ADB_PATH") or (s.get("call") or {}).get("adb_path") or "",
                 "adb_device": env("ADB_DEVICE") or str((s.get("call") or {}).get("adb_device") or "")},
    }
    tw = out["twilio"]
    tw["from_number"] = _e164(tw["from_number"]) if tw["from_number"] else ""
    tw["to_numbers"] = [n for n in (_e164(x) for x in tw["to_numbers"]) if n]
    return out


def call_providers(policy=None):
    """Ordered list, e.g. ['android', 'callmebot']: a normal SIM call first, Telegram voice call if the phone is missing."""
    policy = policy if policy is not None else load_policy()
    p = os.environ.get("NETRAX_CALL_PROVIDER") or (policy.get("phone") or {}).get("provider") or "callmebot"
    p = p.split(",") if isinstance(p, str) else list(p)
    return [x.strip().lower() for x in p if str(x).strip()] or ["callmebot"]


def call_provider(policy=None):
    return call_providers(policy)[0]


def call_targets(sec, policy=None):
    """Who is called: CallMeBot -> Telegram @usernames / +numbers; Android -> phone numbers; Twilio -> twilio.to_numbers."""
    provs = call_providers(policy)
    if provs == ["twilio"]:
        return sec["twilio"]["to_numbers"]
    out = []
    for x in sec["call"]["to"]:
        out.append(x if x.startswith("@") else _e164(x))
    return [x for x in out if x and ("callmebot" in provs or not x.startswith("@"))]


def adb_exe(sec):
    p = sec["call"].get("adb_path") or shutil.which("adb") or ""
    if not p:
        for c in (os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk", "platform-tools", "adb.exe"),
                  os.path.join(ROOT, "tools", "platform-tools", "adb.exe")):
            if os.path.exists(c):
                return c
    return p


def configured(sec=None):
    sec = sec or load_secrets()
    tg, tw, em = sec["telegram"], sec["twilio"], sec["email"]
    twilio_ok = bool(tw["account_sid"] and tw["auth_token"] and tw["from_number"] and tw["to_numbers"])
    provs = call_providers()
    call_ok = twilio_ok if provs == ["twilio"] else bool(call_targets(sec)) and (provs != ["android"] or bool(adb_exe(sec)))
    return {"telegram": bool(tg["bot_token"] and tg["chat_id"]), "call": call_ok, "sms": twilio_ok,
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
    """12 Nov: the SAME identity rule as the dashboard and the voice - registered "Name · W-004",
    unknown "Unregistered person" (never a made-up name), access violation + "UNAUTHORISED"."""
    if not policy.get("include_worker_names", True):
        return None
    wid = e.get("worker_id") or ""
    if wid.startswith("W-"):
        who = f"{e['worker_name']} · {wid}" if e.get("worker_name") else wid
    elif e.get("type") in ("ppe_missing", "unauthorized_entry"):
        who = "Unregistered person"
    else:
        return None
    return who + (" · UNAUTHORISED" if e.get("type") == "unauthorized_entry" else "")


ITEM_LABEL = {"helmet": "Helmet", "vest": "Vest", "gloves": "Gloves", "goggles": "Goggles", "mask": "Mask", "boots": "Shoes"}
ITEM_ORDER = ["helmet", "vest", "gloves", "goggles", "mask", "boots"]


def ppe_line(e):
    """PPE status as text icons, same order as the dashboard icons: Helmet ❌ · Vest ✅ · Goggles ❔."""
    if e.get("type") != "ppe_missing":
        return None
    miss = set(e.get("items_missing") or [])
    ok = set(e.get("items_ok") or [])
    unk = set(e.get("items_uncertain") or [])
    req = set(e.get("items_required") or []) or miss | ok | unk
    parts = [f"{ITEM_LABEL.get(i, i)} {'❌' if i in miss else '✅' if i in ok else '❔'}" for i in ITEM_ORDER if i in req]
    parts += [f"{i} ❌" for i in sorted(miss - set(ITEM_ORDER))]
    return " · ".join(parts) or None


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
        lines.insert(2, f"Person: {who}")
    if ppe_line(e):
        lines.insert(3 if who else 2, f"PPE: {ppe_line(e)}")
    if e.get("verification"):
        lines.append("Sensor check: " + VERIF_TEXT.get(e["verification"], e["verification"]))
    lines += [f"Action: {action_text(e)}", f"Event ID: {e['event_id']}", f"Open: {url}"]
    long = "\n".join(lines)
    short = (f"NETRAX {e.get('severity') or ''}: {headline(e)} Zone {zone}, cam {cam}, {when}. "
             f"{action_text(e)} ID {e['event_id']}")
    spoken = (f"NETRAX safety alert. {headline(e)} Zone: {zone}. Camera {cam}. "
              f"{action_text(e)}")
    src = "camera and safety station" if e.get("verification") == "confirmed" else (
        "safety station sensors" if not e.get("camera_id") else f"camera {cam}")
    what = "Smoke" if e.get("type") == "smoke" else "Fire"
    # 12 Nov (prompt pack step 8): fixed call text, read twice by the provider (phone.repeat_message)
    call = (f"NETRAX fire alarm. {what} detected in {zone} by {src} at {timeutil.fmt(e.get('started_at') or time.time())}. "
            f"Open the dashboard and acknowledge.")
    t = e.get("type")
    head = (f"🔥 {('SMOKE' if t == 'smoke' else 'FIRE')}{lvl.upper()}" if t in ("fire", "smoke") else
            "⛔ UNAUTHORISED ENTRY" if t == "unauthorized_entry" else "🦺 PPE VIOLATION" if t == "ppe_missing" else _what(e).upper())
    if reason not in ("new alert", "new"):
        head += f" · {reason}"
    tg = [f"NETRAX · {head}"] + ([f"👷 {who}"] if who else []) + ([ppe_line(e)] if ppe_line(e) else []) + \
         [f"📍 {zone} · {cam} · {when}", f"➡ {action_text(e)}", url]
    return {"long": long, "short": short[:600], "spoken": spoken, "call": call[:250], "tg": "\n".join(tg)[:1024]}


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


def send_telegram(sec, text, photo=None, reply_to=None, buttons=None):
    tg = sec["telegram"]
    if not (tg["bot_token"] and tg["chat_id"]):
        return Result("not_configured", error="Telegram bot token / chat id not set")
    base = f"https://api.telegram.org/bot{tg['bot_token']}"
    extra = {"reply_to_message_id": int(reply_to), "allow_sending_without_reply": True} if str(reply_to or "").isdigit() else {}
    if buttons:                                   # 12 Nov: inline Acknowledge / Resolve buttons
        extra["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in buttons]]}
    try:
        if photo and os.path.exists(photo):
            with open(photo, "rb") as f:
                form = dict({"chat_id": tg["chat_id"], "caption": text[:1024]}, **extra)
                if "reply_markup" in form:
                    form["reply_markup"] = json.dumps(form["reply_markup"])      # multipart form: JSON as text
                r = requests.post(f"{base}/sendPhoto", data=form,
                                  files={"photo": f}, timeout=20)
            if r.status_code == 200 and len(text) > 1024:
                requests.post(f"{base}/sendMessage", json={"chat_id": tg["chat_id"], "text": text}, timeout=20)
        else:
            r = requests.post(f"{base}/sendMessage", json=dict({"chat_id": tg["chat_id"], "text": text}, **extra), timeout=20)
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


CALLMEBOT_URL = "https://api.callmebot.com/start.php"
CALLMEBOT_BAD = ("not authorized", "not authorised", "need to authorize", "authorize callmebot", "invalid", "not found",
                 "not allowed", "error", "blocked", "banned")
CALLMEBOT_WAIT = ("too many", "try again later", "rate limit", "please wait")


def send_call(sec, to, spoken, policy):
    """11 Nov: one entry point. Providers are tried in the order of phone.provider until one places the call;
    the result says which one did it (or why each failed)."""
    tried = []
    for prov in call_providers(policy):
        if prov == "callmebot":
            res = send_call_callmebot(to, spoken, policy)
        elif prov == "android":
            res = send_call_android(sec, to, policy)
        elif prov == "twilio":
            res = send_call_twilio(sec, to, spoken, policy)
        else:
            res = Result("not_configured", error=f"unknown phone provider {prov}")
        if res.status in ("sent", "sending", "delivered"):
            if tried:
                res.provider_status = (res.provider_status or "") + " | before: " + "; ".join(tried)
            return res
        tried.append(f"{prov}: {res.error or res.status}")
        last = res
    last.error = " | ".join(tried)[:300]
    return last


def _plain(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def send_call_callmebot(to, text, policy):
    """Free CallMeBot API: Telegram voice call that reads `text` (<= 256 chars) `rpt` times. 'sent' = CallMeBot accepted
    the call; it cannot tell us whether the person picked up, so it is never reported as 'delivered'."""
    if not to:
        return Result("not_configured", error="no call recipient (config/secrets.local.yaml call.to)")
    ph = policy.get("phone") or {}
    params = {"user": to, "text": (text or "NETRAX fire alarm")[:250], "lang": ph.get("lang", "en-IN-Standard-A"),
              "rpt": int(ph.get("repeat_message", 2)), "cc": ph.get("text_copy", "missed")}
    try:
        try:
            r = requests.get(CALLMEBOT_URL, params=params, timeout=int(ph.get("timeout_s", 90)))
        except requests.exceptions.SSLError:
            r = requests.get(CALLMEBOT_URL.replace("https://", "http://"), params=params, timeout=int(ph.get("timeout_s", 90)))
    except requests.RequestException as ex:
        return Result("failed", error=f"network: {ex.__class__.__name__}", recoverable=True)
    body = _plain(getattr(r, "text", ""))[:300]
    low = body.lower()
    if r.status_code in (429,) or r.status_code >= 500:
        return Result("failed", error=f"CallMeBot HTTP {r.status_code}: {body[:120]}", recoverable=True)
    if r.status_code != 200:
        return Result("failed", error=f"CallMeBot HTTP {r.status_code}: {body[:160]}")
    if any(w in low for w in CALLMEBOT_BAD):
        hint = " - the person must send /start to @CallMeBot_txtbot once" if "authori" in low else ""
        return Result("failed", provider_status=body[:160], error=f"CallMeBot refused: {body[:160]}{hint}")
    if any(w in low for w in CALLMEBOT_WAIT):
        return Result("failed", provider_status=body[:160], error=f"CallMeBot busy: {body[:120]}", recoverable=True)
    return Result("sent", provider_status=("CallMeBot call placed: " + body)[:200])


def send_call_android(sec, to, policy):
    """Android phone on USB (adb, USB debugging on, phone unlocked): starts a normal SIM call to `to` and hangs up
    after phone.ring_s seconds. 'sent' = the phone started dialling; adb cannot tell whether it was answered."""
    adb = adb_exe(sec)
    if not adb:
        return Result("not_configured", error="adb not found - install Android platform-tools or set call.adb_path")
    if not to or to.startswith("@"):
        return Result("not_configured", error="Android calls need a phone number (+91...)")
    ring = int((policy.get("phone") or {}).get("ring_s", 40))
    serial = (sec["call"].get("adb_device") or "").strip()     # optional: USB serial or Wi-Fi "192.168.x.y:5555"
    base = [adb] + (["-s", serial] if serial else [])
    try:
        if ":" in serial:
            subprocess.run([adb, "connect", serial], capture_output=True, text=True, timeout=10)
        dev = subprocess.run(base + ["get-state"], capture_output=True, text=True, timeout=10)
        if dev.returncode != 0 or "device" not in dev.stdout:
            return Result("failed", error="no Android phone connected (adb get-state: " + (dev.stderr or dev.stdout).strip()[:120] + ")",
                          recoverable=True)
        r = subprocess.run(base + ["shell", "am", "start", "-a", "android.intent.action.CALL", "-d", "tel:" + to],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as ex:
        return Result("failed", error=f"adb: {ex.__class__.__name__}", recoverable=True)
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0 or "Error" in out or "Exception" in out:
        return Result("failed", error=f"phone refused the call: {out[:160]}")
    threading.Timer(ring, lambda: subprocess.run(base + ["shell", "input", "keyevent", "KEYCODE_ENDCALL"],
                                                 capture_output=True, timeout=10)).start()
    return Result("sent", provider_status=f"Android phone dialling {to} (hangs up after {ring} s)")


def call_allowed(e):
    """11 Nov (user): a phone call for EVERY fire / smoke detection - any level, from a camera or the safety station alone.
    Not while a camera fire is still being checked with the station (verifying), not when the station said it is not a fire
    (suppressed), never for PPE / access / entrance."""
    if e.get("type") not in ("fire", "smoke"):
        return False
    return e.get("status") not in ("verifying", "suppressed", "resolved", "closed", "dismissed")


def send_call_twilio(sec, to, spoken, policy):
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


_SMTP = {"conn": None, "key": None, "lock": threading.Lock()}


def _smtp_conn(em, fresh=False):
    """12 Nov: ONE SMTP connection reused for every mail (Gmail counts logins too); 20 s timeout."""
    key = (em["smtp_host"], int(em["smtp_port"]), em["username"])
    c = _SMTP["conn"]
    if c is not None and not fresh and _SMTP["key"] == key:
        try:
            if c.noop()[0] == 250:
                return c
        except (smtplib.SMTPException, OSError):
            pass
    try:
        if c is not None:
            c.close()
    except Exception:                         # noqa: BLE001
        pass
    if int(em["smtp_port"]) == 465:
        c = smtplib.SMTP_SSL(em["smtp_host"], 465, timeout=20)
    else:
        c = smtplib.SMTP(em["smtp_host"], int(em["smtp_port"]), timeout=20)
        c.starttls()
    c.login(em["username"], em["app_password"])
    _SMTP["conn"], _SMTP["key"] = c, key
    return c


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
    with _SMTP["lock"]:
        for attempt in (1, 2):
            try:
                refused = _smtp_conn(em, fresh=attempt == 2).send_message(msg)
                break
            except smtplib.SMTPServerDisconnected:
                if attempt == 2:
                    return Result("failed", error="network: mail server disconnected twice", recoverable=True)
            except smtplib.SMTPAuthenticationError as ex:
                return Result("failed", error=f"login refused: {ex.smtp_code}", recoverable=False)
            except smtplib.SMTPResponseException as ex:
                text = f"{ex.smtp_code} {ex.smtp_error.decode(errors='replace') if isinstance(ex.smtp_error, bytes) else ex.smtp_error}"
                limit = "5.4.5" in text or "sending limit" in text.lower()
                return Result("failed", error=("Gmail daily sending limit reached: " if limit else "") + text[:160],
                              recoverable=not limit and ex.smtp_code >= 400 and ex.smtp_code < 500)
            except smtplib.SMTPException as ex:
                return Result("failed", error=f"{ex.__class__.__name__}: {str(ex)[:150]}", recoverable=True)
            except OSError as ex:                       # DNS / no route / timeout = no internet right now
                _SMTP["conn"] = None
                return Result("failed", error=f"network: {ex.__class__.__name__}: {str(ex)[:120]}", recoverable=True)
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
            first = _first_telegram(e.get("event_id"), job["row"])
            btn = None
            if not first and e.get("type") in ("ppe_missing", "unauthorized_entry", "fire", "smoke") and \
                    (pol.get("telegram") or {}).get("buttons", True):
                btn = [("✅ Acknowledge", f"ack:{e['event_id']}"), ("✔ Resolve", f"resolve:{e['event_id']}")]
            res = send_telegram(sec, msg.get("tg") or msg["long"], photo, reply_to=first, buttons=btn)
        elif ch == "call":
            if not self._call_still_wanted(e, job):
                self._update(job["row"], status="skipped", error="acknowledged / closed before the call was placed",
                             finished_at=time.time())
                return
            res = send_call(sec, job["to"], msg["call"], pol)
        elif ch == "sms":
            res = send_sms(sec, job["to"], msg["short"])
        elif ch == "email":
            res = send_email(sec, job.get("subject") or f"[NETRAX {e.get('severity') or ''}] {headline(e)[:90]} - {e['event_id']}",
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

    @staticmethod
    def _call_still_wanted(e, job):
        """Acknowledge / resolve stops the phone calls at once (a call still waiting in the queue is not placed);
        a Level 3 (evacuate) raise still rings."""
        if job.get("kind") == "test" or not e.get("event_id"):
            return True
        r = db.q("SELECT status FROM events WHERE event_id=?", (e["event_id"],))
        st = r[0]["status"] if r else None
        return st in (None, "open", "escalated", "verifying") or "|L3|" in str(job.get("kind"))

    def _finish(self, job, res, now):
        retry = job["policy"].get("retry") or {}
        maxa, back = int(retry.get("max_attempts", 3)), list(retry.get("backoff_s") or [2, 6, 15])
        if job["channel"] == "call" and (job["policy"].get("phone") or {}).get("repeat_until_ack", True) and job.get("kind") != "test":
            maxa = 1          # 12 Nov: the repeat loop calls again after 20 s (busy / no answer) with its own log row
        if res.status == "failed" and res.recoverable and is_network(res.error):
            # 12 Nov (prompt pack step 9): NO INTERNET is not a failed try. Keep the message and resend after 5 s, 15 s,
            # 30 s, then every 60 s, in order, for up to 2 h; older messages are marked expired with the reason.
            off = (job["policy"].get("offline") or {})
            if now - job["queued"] > float(off.get("keep_s", 7200)):
                self._update(job["row"], status="expired", error="no internet for 2 h - not sent: " + str(res.error)[:120], finished_at=now)
                return
            job["attempt"] -= 1
            job["net"] = job.get("net", 0) + 1
            waits = list(off.get("waits_s") or [5, 15, 30, 60])
            self._update(job["row"], status="waiting", attempts=job["attempt"], error=res.error, provider_status="waiting for internet")
            self.put(job, delay=waits[min(job["net"] - 1, len(waits) - 1)])
            return
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


def _first_telegram(event_id, row_id):
    """Telegram message id of this alert's first message: updates are sent as a REPLY to it (one thread per alert)."""
    if not event_id:
        return None
    r = db.q("SELECT provider_id FROM notifications WHERE event_id=? AND channel='telegram' AND status='delivered' "
             "AND provider_id IS NOT NULL AND id != ? ORDER BY id LIMIT 1", (event_id, row_id))
    return r[0]["provider_id"] if r else None


ALL_CHANNELS = ("telegram", "call", "sms", "email")


def enabled_channels(policy=None):
    """config/notify.yaml enabled_channels (default: all four). Disabled channels are never queued or shown."""
    policy = policy if policy is not None else load_policy()
    en = os.environ.get("NETRAX_CHANNELS")
    en = en.split(",") if en else policy.get("enabled_channels")
    return [c for c in ALL_CHANNELS if en is None or c in en]


UPDATE_REASONS = ("more PPE missing", "still missing", "still in restricted", "again", "reopened")


def channels_for(e, reason, policy):
    out = [c for c in _channels_for(e, reason, policy) if c in enabled_channels(policy)]
    if e.get("type") in ("ppe_missing", "unauthorized_entry") and str(reason).startswith(UPDATE_REASONS):
        # 12 Nov: an update of an alert that was already sent (one alert per violation) -> Telegram only, no new e-mail
        keep = (policy.get("one_alert") or {}).get("update_channels") or ["telegram"]
        out = [c for c in out if c in keep] or [c for c in keep if c in enabled_channels(policy)]
    ph = policy.get("phone") or {}
    if call_allowed(e):
        if "call" not in out and "call" in enabled_channels(policy) and ph.get("call_every_fire", True):
            out.insert(1 if out[:1] == ["telegram"] else 0, "call")      # every fire / smoke rings the supervisor
    elif "call" in out and ph.get("only_fire_smoke", ph.get("only_fire_critical", True)):
        out.remove("call")                                               # no calls for PPE / access / entrance
    return out


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


def email_mode(e, reason, policy):
    """12 Nov (prompt pack step 9): 'now' = its own e-mail (fire / smoke, Critical or High PPE, unauthorised entry,
    escalations); 'digest' = collected into one e-mail every 15 min; 'limit' = the daily e-mail cap is reached
    (only fire / smoke e-mails go out until midnight)."""
    em = policy.get("email") or {}
    fire = e.get("type") in ("fire", "smoke")
    now_types = em.get("send_now_for") or ["fire", "smoke", "ppe_missing_Critical", "ppe_missing_High", "unauthorized_entry"]
    important = fire or str(reason).startswith("ESCALATED") or e.get("type") in now_types or \
        f"{e.get('type')}_{e.get('severity')}" in now_types or e.get("type") == "test"
    if not fire and emails_today() >= int(em.get("daily_max", 300)):
        return "limit"
    return "now" if important else "digest"


def emails_today():
    day0 = timeutil.local(time.time()).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return db.q("SELECT COUNT(*) n FROM notifications WHERE channel='email' AND queued_at >= ? AND status IN "
                "('queued','sending','retrying','waiting','sent','delivered')", (day0,))[0]["n"]


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
        if (ch == "call" and kind != "test" and not str(kind).startswith("call #") and not reason.startswith("ESCALATED")
                and int(e.get("fire_level") or 0) < 3
                and db.q("SELECT id FROM notifications WHERE event_id=? AND channel='call' AND queued_at > ?",
                         (e["event_id"], now - float((policy.get("phone") or {}).get("recall_after_s", 120))))):
            continue                                  # one call per fire; again only for Level 3 or an escalation
        targets = call_targets(sec, policy) if ch == "call" else sec["twilio"]["to_numbers"] if ch == "sms" else [None]
        for i, to in enumerate(targets or [None]):
            label = to or (", ".join(sec["email"]["to"]) if ch == "email" else
                           (f"group {sec['telegram']['chat_id']}" if ch == "telegram" and sec["telegram"]["chat_id"]
                            else recip))
            text = msg["call"] if ch == "call" else \
                msg["short"] if ch == "sms" else msg["long"]
            mode = email_mode(e, reason, policy) if ch == "email" and kind != "test" else "now"
            cur = db.x("INSERT INTO notifications (event_id, channel, recipient, message, delivered, at, status, attempts, "
                       "kind, queued_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (e["event_id"], ch, label, text, 0, now, {"now": "queued", "digest": "digest", "limit": "skipped"}[mode],
                        0, kind, now))
            if mode != "now":
                if mode == "limit":
                    DISPATCHER._update(cur.lastrowid, error=f"daily e-mail limit reached ({int((policy.get('email') or {}).get('daily_max', 300))}) - "
                                       "only fire / smoke e-mails until midnight", finished_at=now)
                out.append({"id": cur.lastrowid, "event_id": e["event_id"], "channel": ch, "recipient": label,
                            "message": text, "status": "digest" if mode == "digest" else "skipped", "at": now})
                continue
            job = {"event": dict(e), "channel": ch, "to": to, "row": cur.lastrowid, "attempt": 0, "msg": msg,
                   "policy": policy, "queued": now, "kind": kind}
            if ch in ("call", "sms") and not to:
                DISPATCHER._update(cur.lastrowid, status="not_configured", error="nobody to call - add call.to in "
                                   "config/secrets.local.yaml" if ch == "call" else "no phone numbers configured",
                                   finished_at=now)
            else:   # an Android phone can make one call at a time: the next number waits until the last one hung up
                DISPATCHER.put(job, delay=i * (int((policy.get("phone") or {}).get("ring_s", 40)) + 5)
                               if ch == "call" and "android" in call_providers(policy) else 0.0)
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


# ------------------------------------------------------------------ 12 Nov: e-mail digest + restart pick-up
def send_digest(now=None, force=False):
    """Everything that is not important enough for its own e-mail is collected and sent as ONE e-mail every
    email.digest_every_s (15 min). Returns the digest row id or None."""
    now = now or time.time()
    policy = load_policy()
    every = float((policy.get("email") or {}).get("digest_every_s", 900))
    rows = db.q("SELECT n.id, n.event_id, n.queued_at, n.kind FROM notifications n WHERE n.channel='email' AND n.status='digest' "
                "ORDER BY n.id")
    if not rows or (not force and now - rows[0]["queued_at"] < every):
        return None
    lines = []
    for r in rows:
        e = db.get_event(r["event_id"]) or {"event_id": r["event_id"], "type": "?"}
        lines.append(f"- {timeutil.fmt(e.get('started_at') or r['queued_at'])} | {_what(e)} | "
                     f"{e.get('zone_name') or e.get('zone_id') or '-'} | {who_text(e, policy) or '-'} | {e['event_id']}")
    did = f"digest_{int(now)}"
    body = f"NETRAX digest - {len(rows)} lower-priority alert(s) since {timeutil.fmt(rows[0]['queued_at'])}\n\n" + "\n".join(lines)
    sec = load_secrets()
    cur = db.x("INSERT INTO notifications (event_id, channel, recipient, message, delivered, at, status, attempts, kind, queued_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?)", (did, "email", ", ".join(sec["email"]["to"]), body, 0, now, "queued", 0, "digest", now))
    for r in rows:
        db.x("UPDATE notifications SET status='in_digest', provider_status=?, finished_at=? WHERE id=?", (did, now, r["id"]))
    job = {"event": {"event_id": did, "type": "digest", "severity": "Low"}, "channel": "email", "to": None, "row": cur.lastrowid,
           "attempt": 0, "msg": {"long": body}, "policy": policy, "queued": now, "kind": "digest",
           "subject": f"[NETRAX digest] {len(rows)} alert(s)"}
    DISPATCHER.put(job)
    DISPATCHER.start()
    return cur.lastrowid


def requeue_unfinished(now=None):
    """After a crash / restart: messages left 'queued' / 'sending' / 'retrying' / 'waiting' are sent again (not
    forgotten). A call or SMS already handed to Twilio cannot be followed any more -> 'sent (status unknown)'.
    Calls older than 10 min are not placed any more (expired). Returns how many were queued again."""
    now = now or time.time()
    policy = load_policy()
    n = 0
    for r in db.q("SELECT * FROM notifications WHERE status IN ('queued','sending','retrying','waiting') ORDER BY id"):
        if r["channel"] in ("call", "sms") and r.get("provider_id"):
            DISPATCHER._update(r["id"], status="sent", provider_status="(status unknown after a restart)", finished_at=now)
            continue
        if r["channel"] == "call" and now - (r["queued_at"] or now) > 600:
            DISPATCHER._update(r["id"], status="expired", error="not placed before a restart; too old to call now", finished_at=now)
            continue
        if now - (r["queued_at"] or now) > float((policy.get("offline") or {}).get("keep_s", 7200)):
            DISPATCHER._update(r["id"], status="expired", error="older than 2 h when the system restarted", finished_at=now)
            continue
        if str(r["event_id"]).startswith("digest_"):
            job = {"event": {"event_id": r["event_id"], "type": "digest", "severity": "Low"}, "channel": "email", "to": None,
                   "row": r["id"], "attempt": 0, "msg": {"long": r["message"]}, "policy": policy, "queued": r["queued_at"] or now,
                   "kind": "digest", "subject": "[NETRAX digest] (resent after a restart)"}
        else:
            e = db.get_event(r["event_id"])
            if not e:
                DISPATCHER._update(r["id"], status="expired", error="alert record not found after a restart", finished_at=now)
                continue
            reason = str(r.get("kind") or "new alert").split("|")[0]
            job = {"event": e, "channel": r["channel"], "to": r["recipient"] if r["channel"] in ("call", "sms") else None,
                   "row": r["id"], "attempt": int(r.get("attempts") or 0), "msg": build(e, reason, policy), "policy": policy,
                   "queued": r["queued_at"] or now, "kind": r.get("kind")}
        DISPATCHER._update(r["id"], status="queued", provider_status="queued again after a restart")
        DISPATCHER.put(job)
        n += 1
    DISPATCHER.start()
    return n


# ------------------------------------------------------------------ 12 Nov: Telegram buttons -> acknowledge / resolve
TG_ACTION = {"fn": None}            # set by main: fn(event_id, action, user) -> (ok, text)
_TG = {"offset": None, "started": False}


def handle_tg_callback(cb, sec=None):
    """One button press. Only presses from the configured group chat count; the action runs through the same path as
    the dashboard button (audit log, stop calls). Returns the text shown to the person who pressed."""
    sec = sec or load_secrets()
    chat = str(((cb.get("message") or {}).get("chat") or {}).get("id", ""))
    if not sec["telegram"]["chat_id"] or chat != str(sec["telegram"]["chat_id"]):
        return "Not allowed from this chat."
    act, _, eid = str(cb.get("data") or "").partition(":")
    if act not in ("ack", "resolve") or not eid or not TG_ACTION["fn"]:
        return "Unknown button."
    who = cb.get("from") or {}
    user = "telegram:" + (who.get("username") or who.get("first_name") or str(who.get("id", "?")))
    ok, text = TG_ACTION["fn"](eid, act, user)
    return text


def _tg_poll():
    while True:
        sec = load_secrets()
        tg = sec["telegram"]
        if not (tg["bot_token"] and tg["chat_id"]) or os.environ.get("SENTINELEYE_NO_NOTIFY"):
            time.sleep(30)
            continue
        base = f"https://api.telegram.org/bot{tg['bot_token']}"
        try:
            params = {"timeout": 25, "allowed_updates": json.dumps(["callback_query"])}
            if _TG["offset"] is not None:
                params["offset"] = _TG["offset"]
            r = requests.get(f"{base}/getUpdates", params=params, timeout=35)
            if r.status_code != 200:
                time.sleep(10)
                continue
            for u in r.json().get("result") or []:
                _TG["offset"] = u["update_id"] + 1
                cb = u.get("callback_query")
                if not cb:
                    continue
                text = handle_tg_callback(cb, sec)
                try:
                    requests.post(f"{base}/answerCallbackQuery", json={"callback_query_id": cb["id"], "text": text[:180]}, timeout=10)
                except requests.RequestException:
                    pass
        except Exception:                             # noqa: BLE001 - no internet etc.: try again later
            time.sleep(5)


def start_telegram_buttons():
    if _TG["started"] or not (load_policy().get("telegram") or {}).get("buttons", True):
        return
    _TG["started"] = True
    threading.Thread(target=_tg_poll, daemon=True, name="telegram-buttons").start()
