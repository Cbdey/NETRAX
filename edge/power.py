"""Camera ON / OFF from the dashboard (11 Nov, user: "an on/off button for each camera on the dashboard, not only on the server").

data/camera_power.json = {"cam_02": false, ...}. A camera listed as false is released by the edge (the reader goes STANDBY, the
device / phone stream is closed), so it gets no AI checks and raises no alarms. It stays off after a restart until it is
turned on again from the dashboard. Cameras with `enabled: false` in cameras.yaml are not started at all and cannot be
switched on from here (that is a configuration decision).
"""
import json
import os
import time

from .config import path


def power_file():
    return os.environ.get("SENTINELEYE_POWER_FILE") or path("data", "camera_power.json")


def read_off():
    """Set of camera ids switched off from the dashboard."""
    try:
        with open(power_file(), "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        return {c for c, on in d.items() if on is False}
    except (OSError, ValueError, AttributeError):
        return set()


def write_power(cam_id, on):
    try:
        with open(power_file(), "r", encoding="utf-8") as f:
            d = json.load(f) or {}
    except (OSError, ValueError):
        d = {}
    if on:
        d.pop(cam_id, None)
    else:
        d[cam_id] = False
    os.makedirs(os.path.dirname(power_file()), exist_ok=True)
    tmp = power_file() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, power_file())
    return read_off()


class Power:
    """Edge side: re-reads the file at most every `every_s` seconds (the readers ask many times a second)."""

    def __init__(self, every_s=0.5, clock=time.time):
        self.every_s, self.clock = every_s, clock
        self.off, self.at = read_off(), clock()

    def is_on(self, cam_id):
        now = self.clock()
        if now - self.at >= self.every_s:
            self.off, self.at = read_off(), now
        return cam_id not in self.off
