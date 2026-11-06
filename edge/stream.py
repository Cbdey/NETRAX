"""Stream reader (F08): one thread per camera, keeps only the newest frame,
reconnects with backoff, and reports health ONLINE / DEGRADED / OFFLINE."""
import os
import threading
import time

import cv2

from .config import path


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

    def __init__(self, cam_id, source, target_fps, rotate=0, capture=None):
        super().__init__(daemon=True, name=f"reader-{cam_id}")
        # USB / laptop webcams: MJPG at a fixed size and frame rate, 1-frame driver buffer (no built-up lag).
        # cameras.yaml  capture: {width: 1280, height: 720, fps: 30, mjpg: true, exposure: null}
        self.capture = dict({"width": 1280, "height": 720, "fps": 30, "mjpg": True}, **(capture or {}))
        self.cam_id = cam_id
        self.rotate = self.ROTATE.get(int(rotate or 0))    # phone held upright -> picture sideways
        self.source = source
        self.target_fps = target_fps
        self.lock = threading.Lock()
        self.frame, self.frame_ts, self.frame_no = None, 0.0, 0
        self.running = True
        self.fps = 0.0
        self.black = False              # 5 Nov: black picture (covered lens / stuck USB camera)
        self.state = "OFFLINE"
        self.detail = "starting"
        self.is_file = isinstance(source, str) and not source.startswith(("http", "rtsp"))
        self.allowed = lambda: True        # camera pairs (edge/share.py): False = release the device, STANDBY
        self.standby = False
        self.active_since = 0.0            # when this camera last got its slot back (pair switch)

    def _open(self):
        src = self.source
        if isinstance(src, int) or (isinstance(src, str) and src.isdigit()):
            idx = int(src)
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
        backoff = 1.0
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
            if cap is None:
                time.sleep(backoff)
                local = isinstance(self.source, int) or (isinstance(self.source, str) and self.source.isdigit())
                backoff = min(backoff * 2, 30.0 if local else 10.0)
                continue
            backoff = 1.0
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
                    if fails > 30:
                        self.detail = "stream lost - reconnecting"
                        break
                    time.sleep(0.05)
                    continue
                fails = 0
                with self.lock:
                    if self.rotate is not None:
                        frame = cv2.rotate(frame, self.rotate)
                    self.frame, self.frame_ts = frame, time.time()
                    self.frame_no += 1
                n_count += 1
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
