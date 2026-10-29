"""Login, roles and access control (final-round section 10; assistant/footage rules of section 3).

Roles
  viewer      sees the live dashboard; worker names hidden, no snapshots / worker photos, cannot act
  supervisor  + acknowledge / resolve / escalate, AI assistant with names, snapshots, location
  admin       + register / delete workers, RFID badges, notification tests, camera switch
Devices (edge PC, ESP32 stations, RFID readers, phones) post without a login on the plant network:
  POST /api/events, /api/edge/health, /api/cameras/*/frame|raw, /api/hw/*, /api/sensors, /api/rfid,
  /api/location/hardware. The face gallery (/api/workers/gallery) is served to THIS computer only.

Users live in config/users.local.yaml (PBKDF2-SHA256 hashes, git-ignored). On the first start three
accounts are created with random passwords, written ONCE to data/initial_passwords.txt - change them with
  python -m tools.users set <name> <new password> [role]
Sessions: signed cookie (HMAC-SHA256, key in data/session.key), 12 h. Failed logins are rate-limited.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USERS_FILE = os.path.join(ROOT, "config", "users.local.yaml")
COOKIE = "netrax_session"
TTL_S = 12 * 3600
ROLES = {"viewer": 0, "supervisor": 1, "admin": 2}
_lock = threading.Lock()
_fails = {}


def enabled():
    if os.environ.get("SENTINELEYE_AUTH") == "0":
        return False
    try:
        with open(os.path.join(ROOT, "config", "settings.yaml"), encoding="utf-8") as f:
            return bool(((yaml.safe_load(f) or {}).get("auth") or {}).get("enabled", True))
    except OSError:
        return True


def _data_dir():
    return os.environ.get("SENTINELEYE_DATA_DIR") or os.path.join(ROOT, "data")


def _key():
    p = os.path.join(_data_dir(), "session.key")
    with _lock:
        if not os.path.exists(p):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(secrets.token_bytes(32))
        with open(p, "rb") as f:
            return f.read()


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
    return salt, h


def load_users():
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("users") or {}


def save_users(users):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        f.write("# NETRAX dashboard users - PBKDF2 hashes only. Never push this file.\n")
        yaml.safe_dump({"users": users}, f, sort_keys=True)


def set_user(name, password, role=None):
    users = load_users()
    role = role or (users.get(name) or {}).get("role") or "viewer"
    if role not in ROLES:
        raise ValueError(f"role must be one of {list(ROLES)}")
    if len(password) < 8:
        raise ValueError("password must have at least 8 characters")
    salt, h = hash_pw(password)
    users[name] = {"role": role, "salt": salt, "hash": h, "changed_at": int(time.time())}
    save_users(users)


def ensure_users():
    """First start: create admin / supervisor / viewer with random passwords (printed + saved once)."""
    if load_users():
        return None
    words = {}
    for name in ("admin", "supervisor", "viewer"):
        words[name] = secrets.token_urlsafe(9)
        set_user(name, words[name], name)
    path = os.path.join(_data_dir(), "initial_passwords.txt")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("NETRAX first-start passwords (change them: python -m tools.users set <name> <password>)\n")
        for n, w in words.items():
            f.write(f"{n:<11} {w}\n")
    print(f"[auth] created users admin / supervisor / viewer - passwords in {path}")
    return path


def check_password(name, password, ip="?"):
    now = time.time()
    k = ip or "?"
    hist = [t for t in _fails.get(k, []) if now - t < 60]
    if len(hist) >= 5:
        return None, "too many failed logins - wait one minute"
    u = load_users().get(name)
    ok = False
    if u:
        _, h = hash_pw(password, u["salt"])
        ok = hmac.compare_digest(h, u["hash"])
    if not ok:
        hist.append(now)
        _fails[k] = hist
        return None, "wrong user name or password"
    _fails.pop(k, None)
    return {"user": name, "role": u["role"]}, None


def make_token(user, role, ttl=TTL_S):
    body = base64.urlsafe_b64encode(json.dumps({"u": user, "r": role, "exp": int(time.time() + ttl)}).encode()).decode()
    sig = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_token(tok):
    if not tok or "." not in tok:
        return None
    body, sig = tok.rsplit(".", 1)
    if not hmac.compare_digest(sig, hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()):
        return None
    try:
        d = json.loads(base64.urlsafe_b64decode(body.encode()))
    except ValueError:
        return None
    if d.get("exp", 0) < time.time():
        return None
    u = load_users().get(d.get("u"))
    if not u:                                  # user deleted -> session dead
        return None
    return {"user": d["u"], "role": u["role"]}


# ------------------------------------------------------------------ path rules
DEVICE_EXACT = {"/api/events", "/api/edge/health", "/api/sensors", "/api/rfid", "/api/location/hardware",
                "/api/hw/telemetry", "/api/hw/rfid"}
PUBLIC = ("/login", "/api/login", "/api/logout", "/static/theme.css", "/static/theme.js", "/static/login.js",
          "/favicon.ico")
LOCAL_ONLY = ("/api/workers/gallery",)
ADMIN_PREFIX = ("/api/workers", "/api/notify/test", "/api/users", "/api/cameras/")
VIEWER_POST_OK = ("/api/assistant", "/api/perf/client", "/api/logout")


def is_local(ip):
    return ip in ("127.0.0.1", "::1", "localhost", "testclient")


def classify(method, path, ip):
    """-> 'public' | 'device' | 'local' | (min_role) """
    if path.startswith(PUBLIC) or path == "/login":
        return "public"
    if method == "POST" and (path.rstrip("/") in DEVICE_EXACT or
                             (path.startswith("/api/cameras/") and path.endswith(("/frame", "/raw"))
                              and path.count("/") == 4)):
        return "device"
    if method == "GET" and path.startswith(("/api/rfid", "/api/hw/rfid")):
        return "device"                                     # RFID readers that use GET
    if method == "GET" and path in ("/api/perf", "/api/health") and is_local(ip):
        return "device"                                     # tools/latency_report.py on the edge computer
    if path.startswith(LOCAL_ONLY):
        return "local"
    if path.startswith(("/snapshots", "/workers/")):
        return "supervisor"
    if path.startswith(("/api/users", "/api/audit")):
        return "admin"
    if method in ("POST", "DELETE", "PUT", "PATCH"):
        if path.startswith(VIEWER_POST_OK):
            return "viewer"
        if path.startswith(ADMIN_PREFIX):
            return "admin"
        return "supervisor"
    return "viewer"


def allowed(role, need):
    return ROLES.get(role, -1) >= ROLES.get(need, 99)


def mask_event(e):
    """Viewer copy of an event: no worker identity, no snapshot."""
    if not isinstance(e, dict):
        return e
    e = dict(e)
    if e.get("worker_id"):
        e["worker_id"] = "W-***" if str(e["worker_id"]).startswith("W-") else e["worker_id"]
    e["worker_name"] = None
    e["snapshot"] = None
    return e
