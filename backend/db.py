"""SQLite storage (F09). Tables follow plan section 7.6."""
import json
import sqlite3
import threading
import time

_lock = threading.Lock()
_conn = None


def init(db_path):
    global _conn
    _conn = sqlite3.connect(db_path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY, type TEXT, items_missing TEXT, severity TEXT,
            camera_id TEXT, camera_name TEXT, zone_id TEXT, zone_name TEXT,
            track_id INTEGER, worker_id TEXT, confidence REAL, bbox TEXT,
            snapshot_path TEXT, captured_at TEXT, started_at REAL, received_at REAL,
            ended_at REAL, latency_ms REAL, status TEXT, end_reason TEXT, growth REAL,
            updated_at REAL);
        CREATE TABLE IF NOT EXISTS event_actions (
            action_id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, action TEXT,
            by_user TEXT, at REAL, note TEXT);
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, channel TEXT,
            recipient TEXT, message TEXT, delivered INTEGER, at REAL);
        CREATE INDEX IF NOT EXISTS ix_events_started ON events(started_at);
        CREATE TABLE IF NOT EXISTS workers (
            worker_id TEXT PRIMARY KEY, name TEXT, embeddings TEXT, photos INTEGER, created_at REAL);
        CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, zone_id TEXT, kind TEXT,
            text TEXT, channel TEXT, at REAL);
        """)
        # columns added after v1 (alert contract: add fields only)
        have = {r[1] for r in _conn.execute("PRAGMA table_info(events)")}
        for col, typ in (("onset_at", "REAL"), ("time_to_alert_ms", "REAL"), ("worker_name", "TEXT")):
            if col not in have:
                _conn.execute(f"ALTER TABLE events ADD COLUMN {col} {typ}")
        _conn.commit()


def q(sql, args=()):
    with _lock:
        return [dict(r) for r in _conn.execute(sql, args).fetchall()]


def x(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur


def row_to_event(r):
    if r is None:
        return None
    e = dict(r)
    e["items_missing"] = json.loads(e["items_missing"] or "[]")
    e["bbox"] = json.loads(e["bbox"] or "null")
    e["snapshot"] = f"/snapshots/{e['event_id']}.jpg" if e.get("snapshot_path") else None
    e["actions"] = q("SELECT action, by_user, at, note FROM event_actions WHERE event_id=? "
                     "ORDER BY at", (e["event_id"],))
    return e


def get_event(event_id):
    rows = q("SELECT * FROM events WHERE event_id=?", (event_id,))
    return row_to_event(rows[0]) if rows else None


def add_action(event_id, action, by_user, note=None):
    x("INSERT INTO event_actions (event_id, action, by_user, at, note) VALUES (?,?,?,?,?)",
      (event_id, action, by_user, time.time(), note))
