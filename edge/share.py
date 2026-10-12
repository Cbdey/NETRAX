"""Camera pairs (one device slot, several cameras): e.g. the laptop webcam and the USB webcam are never
used at the same time. Cameras with the same `share:` name in cameras.yaml form a group:

  * only ONE camera of a group is opened at a time (the other is released, status STANDBY, no alarm);
  * which one: the last choice from the dashboard ("Use this camera" -> data/camera_share.json),
    else the first one in cameras.yaml;
  * failover: if the chosen camera gives no picture for `failover_s` seconds, the next one is tried,
    and so on in a loop, so a dead or unplugged webcam never leaves the slot empty.
"""
import json
import os
import time

from .config import path


def share_file():
    return os.environ.get("SENTINELEYE_SHARE_FILE") or path("data", "camera_share.json")


def read_choice():
    try:
        with open(share_file(), "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def write_choice(group, cam_id):
    d = read_choice()
    d[group] = cam_id
    os.makedirs(os.path.dirname(share_file()), exist_ok=True)
    tmp = share_file() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, share_file())


class ShareGroups:
    def __init__(self, cameras, failover_s=15.0, clock=time.time):
        self.clock = clock
        self.failover_s = failover_s
        self.groups = {}
        for cam_id, cam in cameras.items():
            g = cam.get("share")
            if g:
                self.groups.setdefault(str(g), []).append(cam_id)
        self.groups = {g: m for g, m in self.groups.items() if len(m) > 1}
        self.group_of = {c: g for g, m in self.groups.items() for c in m}
        self.choice = {}                 # group -> camera the user asked for
        self.active = {}                 # group -> camera holding the device now
        self.since = {}                  # group -> when that camera got the slot
        self.note = {}                   # group -> why (for the tile text)
        self.step({})

    def allowed(self, cam_id):
        g = self.group_of.get(cam_id)
        return g is None or self.active.get(g) == cam_id

    def standby_detail(self, cam_id):
        g = self.group_of.get(cam_id)
        return f"standby - {self.active.get(g)} is using this slot" + (f" ({self.note[g]})" if self.note.get(g) else "")

    def step(self, last_frame_at):
        """last_frame_at: cam_id -> time of its newest frame (0 if none). Returns messages for the log."""
        msgs, now, saved = [], self.clock(), read_choice()
        for g, members in self.groups.items():
            want = saved.get(g) if saved.get(g) in members else members[0]
            if self.choice.get(g) != want:                     # first start, or a new choice on the dashboard
                self.choice[g] = want
                self._give(g, want, now, "chosen on the dashboard" if g in saved else "")
                msgs.append(f"{g}: {want} uses the slot")
                continue
            cur = self.active[g]
            seen = last_frame_at.get(cur) or 0
            if seen < self.since[g] and now - self.since[g] >= self.failover_s:   # no picture since it got the slot
                nxt = members[(members.index(cur) + 1) % len(members)]
                self._give(g, nxt, now, f"{cur} gave no picture")
                msgs.append(f"{g}: {cur} gave no picture for {self.failover_s:.0f} s - trying {nxt}")
        return msgs

    def _give(self, g, cam_id, now, why):
        self.active[g], self.since[g], self.note[g] = cam_id, now, why
