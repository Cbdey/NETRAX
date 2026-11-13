"""Follow a phone camera whose address changed (12 Nov, user: "the mobile IP changes whenever we open IP Webcam").

When a phone camera (http://<ip>:8080/...) stops answering, the stream reader asks find_moved(): the laptop's Wi-Fi
subnet is scanned for IP Webcam (http://<ip>:8080/shot.jpg must return a JPEG, ~3-6 s), an address that no other
camera uses is taken, the camera switches to it and config/cameras.yaml is updated (so a restart uses it too).
One scan at a time for all cameras; at most one scan per camera every rescan_s.
"""
import concurrent.futures as cf
import re
import socket
import threading
import time

import requests

from .config import path

_LOCK = threading.Lock()
_IN_USE = {}                 # cam_id -> current source url (all phone cameras register here)
_LAST_SCAN = {}


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))            # no packet is sent; picks the Wi-Fi interface's address
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def is_phone(ip, port=8080):
    try:
        with socket.create_connection((ip, port), timeout=0.6):
            pass
        r = requests.get(f"http://{ip}:{port}/shot.jpg", timeout=3)
        return r.ok and r.headers.get("Content-Type", "").startswith("image/jpeg")
    except (OSError, requests.RequestException):
        return False


def host_of(url):
    m = re.match(r"^\w+://([^/:]+)", url or "")
    return m.group(1) if m else None


def register(cam_id, url):
    with _LOCK:
        _IN_USE[cam_id] = url


def find_moved(cam_id, url, rescan_s=20.0, scan=None):
    """New url for this phone camera, or None. scan(): list of phone IPs (tests pass a fake)."""
    now = time.time()
    if now - _LAST_SCAN.get(cam_id, 0) < rescan_s:
        return None
    _LAST_SCAN[cam_id] = now
    m = re.match(r"^(\w+://)([^/:]+)(:\d+)?(/.*)?$", url or "")
    if not m:
        return None
    port = int((m.group(3) or ":8080")[1:])
    with _LOCK:                                      # one scan at a time
        taken = {host_of(u) for c, u in _IN_USE.items() if c != cam_id}
        found = scan() if scan else _scan(port)
        free = [ip for ip in found if ip not in taken and ip != m.group(2)]
        if not free:
            return None
        new = f"{m.group(1)}{free[0]}{m.group(3) or ''}{m.group(4) or ''}"
        _IN_USE[cam_id] = new
    return new


def _scan(port):
    me = local_ip()
    if not me:
        return []
    base = me.rsplit(".", 1)[0]
    with cf.ThreadPoolExecutor(128) as ex:
        ips = [f"{base}.{i}" for i in range(1, 255) if f"{base}.{i}" != me]
        return [ip for ip, ok in zip(ips, ex.map(lambda ip: is_phone(ip, port), ips)) if ok]


def save(cam_id, url, file=None):
    """Write the new address into config/cameras.yaml (only that camera's 'source:' line)."""
    p = file or path("config", "cameras.yaml")
    with _LOCK:
        txt = open(p, encoding="utf-8").read()
        # 13 Nov: only a camera that IS a phone (http://...:8080) may be rewritten - never the Axis CCTV or a webcam index
        new = re.sub(rf"(^  {cam_id}:\n(?:    .*\n)*?    source:\s*)http://[^\s/]+:8080\S*", lambda mm: mm.group(1) + url, txt,
                     count=1, flags=re.M)
        if new != txt:
            with open(p, "w", encoding="utf-8") as f:
                f.write(new)
            return True
    return False
