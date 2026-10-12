"""Notifications + escalation (F16).

Critical -> safety officer immediately.  High -> zone supervisor.
Medium -> periodic digest only.  Low -> dashboard log only.
Unacknowledged Critical/High alerts auto-escalate after a timer.
Real Telegram delivery if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set,
otherwise the message is recorded as a clearly labelled MOCK notification."""
import os
import time

import requests

from . import db

ESCALATE_AFTER_S = {"Critical": 60, "High": 300}
RECIPIENT = {"Critical": "Safety officer", "High": "Zone supervisor"}


def _telegram(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text}, timeout=5)
        return r.status_code == 200
    except requests.RequestException:
        return False


def describe(e):
    what = e["type"].upper() if e["type"] != "ppe_missing" else \
        "NO " + " + ".join(i.upper() for i in e["items_missing"])
    who = f" - worker {e['worker_id']}" if e.get("worker_id") else ""
    conf = f", confidence {e['confidence']:.2f}" if e.get("confidence") is not None else ""
    return (f"[{e['severity']}] {what} - {e.get('zone_name') or e.get('zone_id')} "
            f"({e.get('camera_id') or '-'}){who}{conf}")


def send(e, reason="new alert", recipient=None):
    recipient = recipient or RECIPIENT.get(e["severity"])
    if not recipient:
        return None
    text = f"NETRAX {reason}: {describe(e)}"
    delivered = _telegram(text)
    channel = "telegram" if delivered else "mock"
    db.x("INSERT INTO notifications (event_id, channel, recipient, message, delivered, at) "
         "VALUES (?,?,?,?,?,?)", (e["event_id"], channel, recipient, text, int(delivered), time.time()))
    return {"event_id": e["event_id"], "channel": channel, "recipient": recipient,
            "message": text, "at": time.time()}


def due_for_escalation(now):
    out = []
    for e in db.q("SELECT event_id, severity, received_at FROM events WHERE status='open'"):
        limit = ESCALATE_AFTER_S.get(e["severity"])
        if limit and now - (e["received_at"] or now) >= limit:
            out.append(e["event_id"])
    return out
