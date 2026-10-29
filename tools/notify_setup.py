"""Set up and test the alert channels (Telegram, phone call + SMS via Twilio, e-mail).
    python -m tools.notify_setup status            which channels have credentials (config/secrets.local.yaml)
    python -m tools.notify_setup telegram          find the group chat id (send any message in the group first)
    python -m tools.notify_setup test [channels]   send a TEST message, e.g.  test telegram sms
Works without the backend running; prints exactly what each provider answered."""
import os
import sys
import tempfile
import time

import requests

os.environ.setdefault("SENTINELEYE_DB", os.path.join(tempfile.mkdtemp(), "notify_test.db"))
from backend import db, notify   # noqa: E402

db.init(os.environ["SENTINELEYE_DB"])


def status():
    sec = notify.load_secrets()
    for ch, ok in notify.configured(sec).items():
        print(f"{ch:<9} {'READY' if ok else 'NOT CONFIGURED'}")
    if not any(notify.configured(sec).values()):
        print("\nCopy config/secrets.example.yaml to config/secrets.local.yaml and fill it in.")


def telegram():
    tok = notify.load_secrets()["telegram"]["bot_token"]
    if not tok:
        print("Put telegram.bot_token (from @BotFather) into config/secrets.local.yaml first.")
        return 1
    r = requests.get(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=10).json()
    if not r.get("ok"):
        print("Telegram refused the token:", r.get("description"))
        return 1
    chats = {}
    for u in r.get("result", []):
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        c = msg.get("chat") or {}
        if c:
            chats[c["id"]] = f"{c.get('type')} '{c.get('title') or c.get('username') or c.get('first_name')}'"
    if not chats:
        print("No messages seen yet. Add the bot to your group, send any message in the group, run this again.")
        print("(If the group still does not show: in @BotFather -> /setprivacy -> Disable, then send a message again.)")
        return 1
    for cid, name in chats.items():
        print(f"chat_id {cid:<16} {name}")
    print("\nCopy the group's chat_id (negative number) into config/secrets.local.yaml -> telegram.chat_id")
    return 0


def test(chans):
    now = time.time()
    db.x("INSERT INTO events (event_id, type, items_missing, severity, zone_name, started_at, received_at, status) "
         "VALUES (?,?,?,?,?,?,?,?)", (f"test_{int(now)}", "test", "[]", "Low", "TEST - no incident", now, now, "closed"))
    e = db.get_event(f"test_{int(now)}")
    rows = notify.enqueue(e, "TEST MESSAGE - please ignore", kind="test", channels=chans)
    print(f"queued {len(rows)} message(s); waiting for the providers (up to 100 s for a phone call)...")
    t = time.time()
    while time.time() - t < 100:
        st = db.q("SELECT status FROM notifications")
        if st and all(r["status"] in notify.FINAL for r in st):
            break
        time.sleep(1)
    for r in db.q("SELECT channel, recipient, status, attempts, provider_status, error, latency_ms FROM notifications"):
        print(f"{r['channel']:<9} {r['status']:<15} to {r['recipient']} | tries {r['attempts']} | "
              f"{r['provider_status'] or ''} {r['error'] or ''} | {round(r['latency_ms'] or 0)} ms")
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] == "status":
        status()
    elif a[0] == "telegram":
        sys.exit(telegram())
    elif a[0] == "test":
        sys.exit(test(a[1:] or ["telegram", "call", "sms", "email"]))
    else:
        print(__doc__)
