"""SQLite storage (F09). Tables follow plan section 7.6."""
import json
import os
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
        for col, typ in (("onset_at", "REAL"), ("time_to_alert_ms", "REAL"), ("worker_name", "TEXT"),
                         # final-round requirements (29 Oct): fire level, location, frame size, workflow
                         ("fire_level", "INTEGER"), ("fire_reasons", "TEXT"),
                         ("frame_w", "INTEGER"), ("frame_h", "INTEGER"),
                         ("location", "TEXT"), ("acked_at", "REAL"), ("acked_by", "TEXT"),
                         ("closed_by", "TEXT"), ("details", "TEXT"), ("clip_path", "TEXT"),
                         # 12 Nov polish: PPE icons need what was seen worn / could not be seen / was required
                         ("items_ok", "TEXT"), ("items_uncertain", "TEXT"), ("items_required", "TEXT")):
            if col not in have:
                _conn.execute(f"ALTER TABLE events ADD COLUMN {col} {typ}")
        have = {r[1] for r in _conn.execute("PRAGMA table_info(notifications)")}
        for col, typ in (("status", "TEXT"), ("attempts", "INTEGER"), ("provider_id", "TEXT"),
                         ("provider_status", "TEXT"), ("error", "TEXT"), ("kind", "TEXT"),
                         ("queued_at", "REAL"), ("sent_at", "REAL"), ("finished_at", "REAL"),
                         ("latency_ms", "REAL")):
            if col not in have:
                _conn.execute(f"ALTER TABLE notifications ADD COLUMN {col} {typ}")
        _conn.executescript("""
        CREATE TABLE IF NOT EXISTS entrance_checks (
            check_id TEXT PRIMARY KEY, camera_id TEXT, zone_id TEXT, at REAL, result TEXT,
            required TEXT, items_ok TEXT, items_missing TEXT, items_uncertain TEXT, evidence TEXT,
            confidence REAL, worker_id TEXT, worker_name TEXT, gate TEXT, snapshot_path TEXT,
            decision_ms REAL, reason TEXT);
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, user TEXT, role TEXT, action TEXT,
            target TEXT, detail TEXT, ip TEXT);
        CREATE TABLE IF NOT EXISTS assistant_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, user TEXT, question TEXT,
            window_from REAL, window_to REAL, n_results INTEGER, ms REAL);
        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, device_id TEXT, lat REAL, lon REAL,
            accuracy_m REAL, at REAL, received_at REAL);
        CREATE INDEX IF NOT EXISTS ix_events_type ON events(type, started_at);
        CREATE INDEX IF NOT EXISTS ix_events_zone ON events(zone_id, started_at);
        CREATE INDEX IF NOT EXISTS ix_events_camera ON events(camera_id, started_at);
        CREATE INDEX IF NOT EXISTS ix_events_worker ON events(worker_id, started_at);
        CREATE INDEX IF NOT EXISTS ix_events_status ON events(status);
        CREATE INDEX IF NOT EXISTS ix_notif_event ON notifications(event_id);
        CREATE INDEX IF NOT EXISTS ix_entr_at ON entrance_checks(at);
        CREATE INDEX IF NOT EXISTS ix_audit_at ON audit_log(at);
        """)
        _conn.execute("PRAGMA journal_mode=WAL")      # readers never block the alert writer
        _conn.commit()


def health():
    """Database probe for the system-health panel: a real write + read."""
    try:
        t = time.time()
        with _lock:
            _conn.execute("CREATE TABLE IF NOT EXISTS _probe (at REAL)")
            _conn.execute("DELETE FROM _probe")
            _conn.execute("INSERT INTO _probe VALUES (?)", (t,))
            _conn.commit()
            ok = _conn.execute("SELECT at FROM _probe").fetchone()[0] == t
        return ok, f"write+read {1000 * (time.time() - t):.1f} ms"
    except Exception as e:                        # noqa: BLE001 - reported on the dashboard
        return False, f"database error: {e!r}"[:200]


def audit(user, role, action, target=None, detail=None, ip=None):
    try:
        x("INSERT INTO audit_log (at, user, role, action, target, detail, ip) VALUES (?,?,?,?,?,?,?)",
          (time.time(), user, role, action, target, detail, ip))
    except Exception:                             # noqa: BLE001 - audit must never break the request
        pass


def q(sql, args=()):
    with _lock:
        return [dict(r) for r in _conn.execute(sql, args).fetchall()]


def x(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur


_NAMES = {"at": 0.0, "map": {}}


def _worker_name(wid):
    """Registered worker id -> name (cached 30 s; workers rarely change)."""
    if time.time() - _NAMES["at"] > 30:
        try:
            _NAMES["map"] = {r["worker_id"]: r["name"] for r in q("SELECT worker_id, name FROM workers")}
        except Exception:                         # noqa: BLE001 - a missing name never breaks an event
            _NAMES["map"] = {}
        _NAMES["at"] = time.time()
    return _NAMES["map"].get(wid)


def row_to_event(r):
    if r is None:
        return None
    e = dict(r)
    e["items_missing"] = json.loads(e["items_missing"] or "[]")
    for k in ("items_ok", "items_uncertain", "items_required"):     # old events: None = unknown (shown as before)
        e[k] = json.loads(e[k]) if e.get(k) else None
    wid = e.get("worker_id")
    if wid and str(wid).startswith("W-") and not e.get("worker_name"):   # old rows: the id without its name
        e["worker_name"] = _worker_name(wid)
    e["bbox"] = json.loads(e["bbox"] or "null")
    e["snapshot"] = f"/snapshots/{e['event_id']}.jpg" if e.get("snapshot_path") else None
    e["clip"] = "/clips/" + os.path.basename(e["clip_path"]) if e.get("clip_path") else None
    for k in ("location", "fire_reasons", "details"):
        if isinstance(e.get(k), str):
            try:
                e[k] = json.loads(e[k])
            except ValueError:
                pass
    e["actions"] = q("SELECT action, by_user, at, note FROM event_actions WHERE event_id=? "
                     "ORDER BY at", (e["event_id"],))
    return e


def get_event(event_id):
    rows = q("SELECT * FROM events WHERE event_id=?", (event_id,))
    return row_to_event(rows[0]) if rows else None


def add_action(event_id, action, by_user, note=None):
    x("INSERT INTO event_actions (event_id, action, by_user, at, note) VALUES (?,?,?,?,?)",
      (event_id, action, by_user, time.time(), note))
