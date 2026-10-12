"""Offline-first alert buffer (F17). Every event is written to a local SQLite queue
first; a background thread sends it to the backend and marks it sent. If the network
or backend is down, events wait and are synced automatically when it comes back.

Latency: put() wakes the sender immediately (no polling delay) and the sender keeps one
HTTP connection open, so a new alert leaves the edge within a few milliseconds."""
import json
import sqlite3
import threading
import time

import requests


class Outbox:
    def __init__(self, db_path, backend_url):
        self.db_path = db_path
        self.url = backend_url.rstrip("/") + "/api/events"
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.online = False
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")       # cheaper commits than the default journal
            c.execute("""CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0)""")
        threading.Thread(target=self._sender, daemon=True, name="outbox").start()

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=10)
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    def put(self, payload):
        with self.lock, self._conn() as c:
            c.execute("INSERT INTO outbox (payload_json, created_at) VALUES (?, ?)",
                      (json.dumps(payload), time.time()))
        self.wake.set()

    def pending(self):
        with self.lock, self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM outbox WHERE sent = 0").fetchone()[0]

    def _sender(self):
        session = requests.Session()
        while True:
            with self.lock, self._conn() as c:
                rows = c.execute("SELECT id, payload_json FROM outbox WHERE sent = 0 "
                                 "ORDER BY id LIMIT 20").fetchall()
            if not rows:
                self.wake.wait(timeout=1.0)
                self.wake.clear()
                continue
            for row_id, payload in rows:
                try:
                    r = session.post(self.url, data=payload, timeout=3,
                                     headers={"Content-Type": "application/json"})
                    ok = r.status_code < 300
                except requests.RequestException:
                    ok = False
                self.online = ok
                if not ok:
                    time.sleep(2.0)       # backend unreachable - keep buffering
                    break
                with self.lock, self._conn() as c:
                    c.execute("UPDATE outbox SET sent = 1 WHERE id = ?", (row_id,))
