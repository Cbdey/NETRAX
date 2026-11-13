"""Stream reader (F08): one thread per camera, keeps only the newest frame,
reconnects with backoff, and reports health ONLINE / DEGRADED / OFFLINE."""
import os
import re
import threading
import time
from urllib.parse import quote, urlparse

# 13 Nov (Axis P1447-LE): RTSP over TCP (UDP loses packets on a busy network -> grey / broken pictures) and a 5 s socket
# timeout. OpenCV reads this variable when a stream is OPENED. 'stimeout' = FFmpeg <= 4, 'timeout' = FFmpeg 5+ (this
# OpenCV ships avformat 61 = FFmpeg 7); an option the library does not know is ignored.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;5000000|timeout;5000000")

import cv2                       # noqa: E402

from .config import path         # noqa: E402

# ---------------------------------------------------------------- CCTV login from secrets (13 Nov)
# cameras.yaml:   source: "rtsp://{axis}@192.168.0.90/axis-media/media.amp?..."
# secrets.local.yaml (git-ignored):   cameras: {axis: {user: netrax, password: "..."}}
# {axis} is replaced by the URL-encoded "user:password" only in memory, when the stream is opened.
_PLACE = re.compile(r"\{(\w+)\}@")
_KNOWN = set()                   # passwords / encoded logins seen -> masked in any text


def camera_secrets():
    """cameras: block of config/secrets.local.yaml (tests: SENTINELEYE_SECRETS = own file, never the real one)."""
    import yaml
    alt = os.environ.get("SENTINELEYE_SECRETS")
    p = alt if alt else path("config", "secrets.local.yaml")
    try:
        with open(p, encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("cameras") or {}
    except (OSError, ValueError):
        return {}


def fill_source(src, creds=None):
    """(real url, None) or (None, friendly error). Sources without a {name}@ placeholder are returned unchanged."""
    if not isinstance(src, str):
        return src, None
    m = _PLACE.search(src)
    if not m:
        return src, None
    name = m.group(1)
    c = (camera_secrets() if creds is None else creds).get(name) or {}
    user, pw = str(c.get("user") or ""), str(c.get("password") or "")
    if not user or not pw:
        return None, f"{name.capitalize()} password not set in secrets.local.yaml (cameras: {name}: user / password)"
    enc = quote(user, safe="") + ":" + quote(pw, safe="")
    _KNOWN.update({pw, quote(pw, safe=""), enc})
    return src.replace(m.group(0), enc + "@", 1), None


def mask(text):
    """Any login inside a URL -> ***, and every known camera password -> ***."""
    if text is None:
        return text
    s = re.sub(r"(\w+://)[^/@\s]+@", r"\1***@", str(text))
    for k in sorted(_KNOWN, key=len, reverse=True):
        if k:
            s = s.replace(k, "***")
    return s


def cctv_label(src):
    return "Axis camera" if isinstance(src, str) and "axis" in src.lower() else "IP camera"


def rtsp_unreachable_text(src, url):
    u = urlparse(url)
    port = u.port or 554
    net = ".".join((u.hostname or "?").split(".")[:3])
    return f"{cctv_label(src)} not reachable at {u.hostname}:{port} - Ethernet cable / laptop IP {net}.x?"


def resolve_rtsp(src, url, relay=None):
    """(url to open, None) or (None, plain-word error). Direct address first; if it does not answer and the camera has
    `relay:` in cameras.yaml, the relay laptop on the Wi-Fi (edge/relay.py)."""
    if reachable(url):
        return url, None
    if relay is not None:
        from .relay import with_host
        host = relay.find()
        if host:
            return with_host(url, host, relay.port), None
        u = urlparse(url)
        return None, (f"{cctv_label(src)} not reachable at {u.hostname}:{u.port or 554} and no relay found on the Wi-Fi "
                      f"(port {relay.port}) - run AXIS_RELAY_FRIEND.bat on the laptop the camera is plugged into")
    return None, rtsp_unreachable_text(src, url)


def open_rtsp(url, open_ms=8000, read_ms=5000):
    """FFmpeg backend explicitly (CAP_ANY could fall through to other backends that print the URL)."""
    try:
        return cv2.VideoCapture(url, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, open_ms,
                                                      cv2.CAP_PROP_READ_TIMEOUT_MSEC, read_ms])
    except (cv2.error, TypeError):           # older OpenCV without the params form
        return cv2.VideoCapture(url, cv2.CAP_FFMPEG)


def reachable(url, timeout=1.5):
    """Quick TCP check of a network camera (http://host:port/..., rtsp://host:port/...)."""
    import socket
    from urllib.parse import urlparse
    u = urlparse(url)
    port = u.port or (554 if u.scheme == "rtsp" else 80)
    try:
        with socket.create_connection((u.hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


class StreamReader(threading.Thread):
    ROTATE = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}

    def __init__(self, cam_id, source, target_fps, rotate=0, capture=None, device=None, relay=None):
        super().__init__(daemon=True, name=f"reader-{cam_id}")
        self.relay = None                  # 13 Nov: CCTV plugged into another laptop -> its Wi-Fi relay (edge/relay.py)
        if relay:
            from .relay import Relay
            self.relay = Relay(relay)
        self.via = None                    # "relay 10.248.34.50:8554" while the stream goes through the relay
        self.device = device               # 13 Nov: webcam chosen by NAME (edge/webcams.py); source index = fallback
        # USB / laptop webcams: MJPG at a fixed size and frame rate, 1-frame driver buffer (no built-up lag).
        # cameras.yaml  capture: {width: 1280, height: 720, fps: 30, mjpg: true, exposure: null}
        self.capture = dict({"width": 1280, "height": 720, "fps": 30, "mjpg": True}, **(capture or {}))
        self.cam_id = cam_id
        self.rotate = self.ROTATE.get(int(rotate or 0))    # phone held upright -> picture sideways
        self.source = source
        # 12 Nov: phone (IP Webcam) whose address changes -> follow it (edge/phones.py)
        self.follow_phone = isinstance(source, str) and source.startswith("http") and ":8080" in source
        self.unreach = 0
        if self.follow_phone:
            from . import phones
            phones.register(cam_id, source)
        self.target_fps = target_fps
        self.lock = threading.Lock()
        self.frame, self.frame_ts, self.frame_no = None, 0.0, 0
        self.running = True
        self.fps = 0.0
        self.black = False              # 5 Nov: black picture (covered lens / stuck USB camera)
        self.state = "OFFLINE"
        self.detail = "starting"
        self.is_file = isinstance(source, str) and not source.startswith(("http", "rtsp"))
        self.is_rtsp = isinstance(source, str) and source.lower().startswith("rtsp")
        self.size = None
        self.lag_ms = []                   # 13 Nov: RTSP delay above the best seen (frames waiting in the stream buffer)
        self._lag_base = None
        self._last_report = time.time()
        self.allowed = lambda: True        # camera pairs (edge/share.py): False = release the device, STANDBY
        self.standby = False
        self.active_since = 0.0            # when this camera last got its slot back (pair switch)

    def _open(self):
        src = self.source
        if isinstance(src, int) or (isinstance(src, str) and src.isdigit()):
            idx = int(src)
            if self.device and os.name == "nt":
                from .webcams import index_of
                idx = index_of(self.device, idx)
                if idx is None:
                    d = str(self.device)
                    self.detail = (f"USB webcam not connected (only '{d[1:].strip()}' found) - plug it in" if d.startswith("!")
                                   else f"webcam '{d}' not connected - plug it in (Device Manager > Cameras)")
                    return None
                self.index = idx
            cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(idx)
            if cap is not None and cap.isOpened():
                c = self.capture
                # 6 Nov: ORDER MATTERS on DirectShow - the Logi C270 silently ignored MJPG when it was set BEFORE the size
                # and stayed on uncompressed YUY2, which at 1280x720 is limited to 7.5 fps (measured: MJPG first -> YUY2
                # 7.5 fps; size, fps, THEN MJPG -> MJPG 29.2 fps). Size, fps, then MJPG.
                if c.get("width"):
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(c["width"]))
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(c["height"]))
                if c.get("fps"):
                    cap.set(cv2.CAP_PROP_FPS, int(c["fps"]))
                if c.get("mjpg"):
                    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
                    v = int(cap.get(cv2.CAP_PROP_FOURCC))
                    self.pixfmt = "".join(chr((v >> 8 * i) & 0xFF) for i in range(4))
                    if self.pixfmt != "MJPG":
                        print(f"[{self.cam_id}] camera refused MJPG (format {self.pixfmt!r}) - frame rate may be limited", flush=True)
                if c.get("exposure") is not None:          # fixed exposure = less motion blur when running
                    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)
                    cap.set(cv2.CAP_PROP_EXPOSURE, float(c["exposure"]))
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        elif self.is_file:
            full = src if os.path.isabs(src) else path(src)
            if not os.path.exists(full):
                self.detail = f"file not found: {src}"
                return None
            cap = cv2.VideoCapture(full)
        elif self.is_rtsp:
            url, err = fill_source(src)
            if err:
                self.detail = err
                return None
            url, err = resolve_rtsp(src, url, self.relay)
            if err:
                self.detail = err
                return None
            via = None
            if self.relay is not None and self.relay.host and f"@{self.relay.host}:{self.relay.port}/" in url:
                via = f"relay {self.relay.host}:{self.relay.port}"
            if via != self.via:
                print(f"[{self.cam_id}] {cctv_label(src)} " + (f"through the {via} (camera on another laptop)" if via
                                                               else "connected directly"), flush=True)
                self.via = via
            cap = open_rtsp(url)
            if not cap or not cap.isOpened():
                self.detail = (f"{cctv_label(src)} answered but the video did not open - user / password in "
                               "secrets.local.yaml or the URL path / resolution")
                return None
            self._lag_base = None
            return cap
        else:
            if not reachable(src):
                # an unreachable phone / IP camera made cv2.VideoCapture hang for over a minute
                # and stalled the other cameras - check the address quickly first
                self.detail = f"not reachable: {src.split('//')[-1].split('/')[0]} (same Wi-Fi? app running?)"
                return None
            cap = cv2.VideoCapture(src)
            if cap is not None and cap.isOpened():
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)          # keep only the newest network frame
        if not cap or not cap.isOpened():
            self.detail = "cannot open source"
            return None
        return cap

    def run(self):
        backoff, open_fails = 1.0, 0
        while self.running:
            if not self.allowed():
                self.standby = True
                time.sleep(0.5)
                continue
            if self.standby:
                self.active_since = time.time()
                self.detail = "switching to this camera..."
            self.standby = False
            cap = self._open()
            if cap is None and self.follow_phone and self.detail.startswith("not reachable"):
                self.unreach += 1
                if self.unreach >= 2 and self._follow_phone():
                    backoff, open_fails, self.unreach = 1.0, 0, 0
                    continue
            if cap is None:
                open_fails += 1
                time.sleep(backoff)
                local = isinstance(self.source, int) or (isinstance(self.source, str) and self.source.isdigit())
                # 11 Nov: a USB camera reopened after a black picture often fails the first 1-2 tries ("can't be used to
                # capture by index") - the doubling wait then reached 30 s and the tile stayed dead for ~1 min.
                # Retry every <= 3 s for the first 10 tries, only then slow down (a camera that is really unplugged).
                backoff = min(backoff * 2, 3.0 if open_fails < 10 else (30.0 if local else 10.0))
                continue
            backoff, open_fails, self.unreach = 1.0, 0, 0
            self.detail = "ok"
            period = 0.0
            if self.is_file:
                file_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
                file_fps = min(max(file_fps, 1.0), 60.0)
                period = 1.0 / file_fps               # play clips at real speed
            t_count, n_count = time.time(), 0
            fails = 0
            black_since = None
            while self.running:
                if not self.allowed():                 # the other camera of the pair got the slot
                    self.standby = True
                    with self.lock:
                        self.frame, self.frame_ts = None, 0.0
                    break
                t0 = time.time()
                ok, frame = cap.read()
                if not ok:
                    if self.is_file:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)      # loop the clip
                        fails += 1
                        if fails > 3:
                            break
                        continue
                    fails += 1
                    # an RTSP read already waits up to 5 s (read timeout): reconnect after 3 failed reads, not 30
                    if fails > (3 if self.is_rtsp else 30):
                        self.detail = "stream lost - reconnecting"
                        break
                    time.sleep(0.05)
                    continue
                fails = 0
                now = time.time()
                if self.is_rtsp:
                    self._note_lag(cap, now)
                with self.lock:
                    if self.rotate is not None:
                        frame = cv2.rotate(frame, self.rotate)
                    self.frame, self.frame_ts = frame, now
                    self.frame_no += 1
                    self.size = (frame.shape[1], frame.shape[0])
                n_count += 1
                if self.is_rtsp and now - self._last_report >= 60:
                    self._last_report = now
                    print(f"[{self.cam_id}] {self.report()}", flush=True)
                if time.time() - t_count >= 2.0:
                    self.fps = n_count / (time.time() - t_count)
                    t_count, n_count = time.time(), 0
                    # 5 Nov: a stuck / covered USB camera delivers BLACK pictures at ~1 fps - say so instead of 'DEGRADED'
                    self.black = black_picture(frame)
                    if self.black:
                        self.detail = "picture is black - lens covered or camera stuck: unplug and re-plug the camera"
                        black_since = black_since or time.time()
                        # a USB camera that stopped streaming (Windows USB power saving, loose cable) returns an empty
                        # frame once a second: release it and open it again (every ~10 s) instead of waiting forever
                        if not self.is_file and isinstance(self.source, int) and time.time() - black_since >= 8:
                            print(f"[{self.cam_id}] black picture for 8 s - reopening the camera", flush=True)
                            break
                    else:
                        black_since = None
                        if self.detail.startswith("picture is black"):
                            self.detail = "ok"
                if period:
                    time.sleep(max(0.0, period - (time.time() - t0)))
            cap.release()
            if self.running and not self.is_file:
                time.sleep(1.0)                        # let the camera driver let go before opening it again

    def _follow_phone(self):
        """The phone stopped answering at its address: look for it on the Wi-Fi and switch (saved in cameras.yaml)."""
        from . import phones
        self.detail = "phone not answering - searching the Wi-Fi for it..."
        new = phones.find_moved(self.cam_id, self.source)
        if not new:
            self.detail = f"not reachable: {phones.host_of(self.source)} - open IP Webcam + 'Start server' (same Wi-Fi)"
            return False
        print(f"[{self.cam_id}] phone moved: {self.source} -> {new} (saved in cameras.yaml)", flush=True)
        self.source = new
        try:
            phones.save(self.cam_id, new)
        except OSError as e:
            print(f"[{self.cam_id}] could not save the new address: {e}", flush=True)
        return True

    def _note_lag(self, cap, now):
        """Stream time (PTS) vs wall clock. The lowest difference seen = the camera's own delay; anything above it =
        frames that waited in the buffer (the reader fell behind). Reading continuously keeps this near 0."""
        pts = cap.get(cv2.CAP_PROP_POS_MSEC)
        if not pts or pts <= 0:
            return
        d = now * 1000.0 - pts
        self._lag_base = d if self._lag_base is None else min(self._lag_base, d)
        self.lag_ms.append(d - self._lag_base)
        del self.lag_ms[:-600]

    def report(self):
        lag = sorted(self.lag_ms)
        p50 = lag[len(lag) // 2] if lag else None
        size = f"{self.size[0]}x{self.size[1]}" if self.size else "-"
        return (f"camera {self.fps:.1f} fps, {size}, frame waiting in the stream buffer "
                + (f"p50 {p50:.0f} ms, max {lag[-1]:.0f} ms" if lag else "not measured"))

    def latest(self):
        with self.lock:
            return self.frame, self.frame_ts, self.frame_no

    def health(self, processed_fps):
        if self.standby or not self.allowed():
            self.state = "STANDBY"
            return self.state
        age = time.time() - self.frame_ts if self.frame_ts else 1e9
        if self.frame_ts < self.active_since and time.time() - self.active_since < 10:
            self.state = "DEGRADED"                     # just switched on, first picture on its way
            return self.state
        if age > 10:
            self.state = "OFFLINE"
        elif age > 2 or (self.fps and self.fps < 4) or processed_fps < 1.0 or getattr(self, "black", False):
            # 4 Nov: degraded = the CAMERA is slow / stale or the AI has (almost) stopped. The AI rate itself is
            # shared by all cameras on one GPU (3-6 checks/s each with 3 cameras); the live video runs separately.
            self.state = "DEGRADED"
        else:
            self.state = "ONLINE"
        return self.state

    def stop(self):
        self.running = False


def black_picture(frame, level=4.0):
    """True if the picture is (almost) completely black: covered lens or a stuck camera driver."""
    try:
        return frame is not None and float(frame[::16, ::16].mean()) < level
    except Exception:              # noqa: BLE001
        return False
