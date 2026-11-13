"""Reach an RTSP CCTV camera that is plugged into ANOTHER laptop (13 Nov, Axis P1447-LE).

The Axis camera sits on a team member's laptop Ethernet (192.168.0.90). That laptop runs
tools/axis_relay/AXIS_RELAY_FRIEND.bat, which forwards <its Wi-Fi IP>:8554 -> 192.168.0.90:554 (Windows portproxy).
cameras.yaml keeps the DIRECT address and adds `relay: 8554`:
  - camera reachable directly (cable in this laptop) -> used directly;
  - otherwise the Wi-Fi is scanned for a host answering RTSP on port 8554 and the stream goes through it
    (remembered; scanned again at most every rescan_s when it stops answering - the friend's IP may change).
RTSP runs over ONE TCP connection (rtsp_transport tcp), so a plain TCP forward carries video and control.
"""
import concurrent.futures as cf
import socket
import time
from urllib.parse import urlsplit, urlunsplit


def probe_rtsp(ip, port, timeout=0.6):
    """True if ip:port answers an RTSP OPTIONS request (no login needed for OPTIONS)."""
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.settimeout(2.0)
            s.sendall(f"OPTIONS rtsp://{ip}:{port}/ RTSP/1.0\r\nCSeq: 1\r\nUser-Agent: NETRAX\r\n\r\n".encode())
            return s.recv(64).startswith(b"RTSP/1.0")
    except OSError:
        return False


def scan(port, ips=None):
    """Hosts on this laptop's Wi-Fi /24 answering RTSP on `port` (ips: test override)."""
    if ips is None:
        from .phones import local_ip
        me = local_ip()
        if not me:
            return []
        base = me.rsplit(".", 1)[0]
        ips = [f"{base}.{i}" for i in range(1, 255) if f"{base}.{i}" != me]
    with cf.ThreadPoolExecutor(128) as ex:
        return [ip for ip, ok in zip(ips, ex.map(lambda ip: probe_rtsp(ip, port), ips)) if ok]


def with_host(url, host, port):
    """Same URL (login, path, query) with another host:port."""
    u = urlsplit(url)
    login = u.netloc.rsplit("@", 1)[0] + "@" if "@" in u.netloc else ""
    return urlunsplit((u.scheme, f"{login}{host}:{port}", u.path, u.query, u.fragment))


class Relay:
    """Per camera: remembers the relay host that worked."""

    def __init__(self, port, rescan_s=20.0, scan_fn=None, reach_fn=None):
        self.port = int(port)
        self.rescan_s = rescan_s
        self.host = None
        self.last_scan = 0.0
        self._scan = scan_fn or (lambda: scan(self.port))
        self._reach = reach_fn or (lambda h: probe_rtsp(h, self.port))

    def find(self):
        """Relay host or None. Keeps a working one; scans the Wi-Fi at most every rescan_s."""
        if self.host and self._reach(self.host):
            return self.host
        if time.time() - self.last_scan < self.rescan_s:
            return None
        self.last_scan = time.time()
        found = self._scan()
        self.host = found[0] if found else None
        return self.host
