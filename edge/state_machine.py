"""Per-worker violation state machine (F05, plan section 7.2).

Per (camera, track, item):
  OK --(missing in >= N of last M frames)--> PENDING (grace timer)
  PENDING --(item seen again)--> OK            (no alert)
  PENDING --(grace elapsed)--> ALERTED          (one alert)
  ALERTED --(compliant for resolve_s)--> OK     (event resolved)
Cooldown is keyed on camera + zone + item + coarse location, so a tracker ID
switch does not create a duplicate alert - the open event is handed over.

ONE DECISION PER LOOK (12 Oct): the item states above are only evidence. What the worker is told is
decided per worker, all items at once:
  - a person who appears is watched for first_look_s, then ONE decision: one alert listing every missing
    item, or (all required items seen and worn) one 'ppe_ok' info ("PPE complete");
  - when the set of confirmed-missing items changes (PPE taken off / put on), the camera watches again for
    settle_s and then sends ONE new decision: open / update (new full list) / resolve;
  - if the person's face is being identified, the decision waits up to id_wait_s for the name, so the one
    alarm already says who it is.
No alert per item, no update per extra item.
"""
import time
import uuid
from collections import deque

from .severity import ppe_severity


class _Item:
    __slots__ = ("hist", "state", "pending_since", "ok_since", "onset")

    def __init__(self, m):
        self.hist = deque(maxlen=m)
        self.state = "OK"
        self.pending_since = None
        self.ok_since = None
        self.onset = None       # first frame of the current missing streak (time-to-alert metric)


class _Worker:
    __slots__ = ("tid", "last_seen", "bbox", "conf", "zone", "event_id", "open_items",
                 "opened_at", "severity", "seen_tick", "onset", "worker_id", "worker_name",
                 "first_seen", "change_since", "looked", "id_pending")

    def __init__(self, tid):
        self.tid = tid
        self.last_seen = 0.0
        self.bbox = None
        self.conf = 0.0
        self.zone = None
        self.event_id = None
        self.open_items = set()
        self.opened_at = None
        self.severity = None
        self.seen_tick = -1
        self.onset = None
        self.worker_id = None       # registered worker (W-001) or visitor (V-001), from identity
        self.worker_name = None
        self.first_seen = None      # first frame of this look (decision due first_look_s later)
        self.change_since = None    # confirmed-missing set differs from what was announced since then
        self.looked = False         # first decision for this person made
        self.id_pending = False     # face seen, name not decided yet


class PPEStateMachine:
    def __init__(self, cfg):
        self.m = cfg["window_m"]
        self.n = cfg["confirm_n"]
        self.grace = cfg["grace_s"]
        self.cooldown = cfg["cooldown_s"]
        self.resolve = cfg["resolve_s"]
        self.lost = cfg["lost_track_s"]
        self.repeat_window = cfg["repeat_window_s"]
        self.grid = cfg["location_grid"]
        # zone-wise priority: a high-risk zone confirms with fewer frames and a shorter grace, so its
        # alert comes first; settings.yaml state_machine.by_risk overrides confirm_n / grace_s per risk.
        self.by_risk = cfg.get("by_risk") or {}
        self.items = {}        # (cam, tid, item) -> _Item
        self.workers = {}      # (cam, tid) -> _Worker
        self.cooldowns = {}    # (cam, zone, item, cell) -> (until, worker_key)
        self.alert_times = {}  # (cam, tid) -> last alert time
        self.resolved_at = {}  # ((cam, tid), item) -> when that worker last put the item back on
        self.rearm = cfg.get("rearm_s", 5.0)
        self.first_look = cfg.get("first_look_s", 2.5)   # watch a new person this long, then decide once
        self.settle = cfg.get("settle_s", 1.0)           # after a change, watch this long, then decide once
        self.id_wait = cfg.get("id_wait_s", 1.5)         # extra wait for the name while the face is matched
        self.ok_info = cfg.get("announce_compliant", True)
        self.ok_repeat = cfg.get("ok_repeat_s", 600)     # "PPE complete" at most once per person per 10 min
        self.ok_said = {}                                # worker id / track -> when "PPE complete" was sent
        self.tick = 0

    def _cell(self, bbox, fw, fh):
        fx = ((bbox[0] + bbox[2]) / 2.0) / fw
        fy = bbox[3] / fh
        g = self.grid
        return (min(int(fx * g), g - 1), min(int(fy * g), g - 1))

    def update(self, cam, persons, statuses, zone_of, risk_of, fw, fh, now=None):
        """persons: [{'tid','bbox','conf'}]; statuses: {tid: {item: (status, conf)}}
        zone_of: {tid: zone_id}; risk_of: callable(zone_id) -> 'high'|'medium'|'low'
        Returns a list of actions: dicts with action in {'open','update','resolve'}."""
        now = time.time() if now is None else now
        self.tick += 1
        actions = []
        for p in persons:
            tid = p["tid"]
            wkey = (cam, tid)
            w = self.workers.get(wkey)
            if w is None:
                w = self.workers[wkey] = _Worker(tid)
            w.last_seen, w.bbox, w.conf, w.seen_tick = now, p["bbox"], p["conf"], self.tick
            if w.first_seen is None:
                w.first_seen = now
            w.zone = zone_of[tid]
            w.id_pending = "identity" in p and p.get("identity") is None and not p.get("worker_id")
            rcfg = self.by_risk.get(risk_of(w.zone)) or {}
            need_n, grace = rcfg.get("confirm_n", self.n), rcfg.get("grace_s", self.grace)
            if p.get("worker_id") and p.get("worker_id") != w.worker_id:
                w.worker_id, w.worker_name = p["worker_id"], p.get("worker_name")
                if w.event_id:                       # identified after the alert opened
                    actions.append(self._action("update", cam, w, now))
            cell = self._cell(p["bbox"], fw, fh)
            newly, cleared = [], []
            for item, (status, _conf) in statuses.get(tid, {}).items():
                st = self.items.get((cam, tid, item))
                if st is None:
                    st = self.items[(cam, tid, item)] = _Item(self.m)
                if status == "unverifiable":
                    continue            # neither evidence for nor against
                missing = status == "missing"
                st.hist.append(missing)
                if st.state == "OK":
                    if not any(st.hist):
                        st.onset = None
                    elif missing and st.onset is None:
                        st.onset = now
                    if sum(st.hist) >= need_n:
                        st.state, st.pending_since = "PENDING", now
                elif st.state == "PENDING":
                    recent = list(st.hist)[-3:]
                    if len(recent) == 3 and not any(recent):
                        st.state, st.pending_since = "OK", None
                    elif now - st.pending_since >= grace:
                        ckey = (cam, w.zone, item, cell)
                        owner = self._cooldown_owner(ckey, wkey, item, now)
                        if owner is None:
                            old_key = self._nearby_handover(cam, w, item)
                        else:
                            old_key = None if owner == "self" else owner
                        if owner == "self":
                            st.state = "SUPPRESSED"      # same worker, fixed it < rearm_s ago
                        elif old_key is not None:
                            self._handover(w, old_key, item, actions, risk_of, now)
                            st.state = "ALERTED" if w.event_id else "SUPPRESSED"
                        else:
                            st.state = "ALERTED"
                            newly.append(item)
                            self.cooldowns[ckey] = (now + self.cooldown, wkey)
                            if st.onset is not None:
                                w.onset = st.onset if w.onset is None else min(w.onset, st.onset)
                elif st.state in ("ALERTED", "SUPPRESSED"):
                    if missing:
                        st.ok_since = None
                        if st.state == "SUPPRESSED":
                            ckey = (cam, w.zone, item, cell)
                            if self._cooldown_owner(ckey, wkey, item, now) is None:
                                st.state = "ALERTED"
                                newly.append(item)
                                self.cooldowns[ckey] = (now + self.cooldown, wkey)
                    else:
                        if st.ok_since is None:
                            st.ok_since = now
                        elif now - st.ok_since >= self.resolve:
                            if st.state == "ALERTED":
                                cleared.append(item)
                                self.resolved_at[(wkey, item)] = now
                            st.state, st.ok_since = "OK", None
                            st.hist.clear()
            self._decide(cam, w, wkey, statuses.get(tid, {}), rcfg, risk_of, now, actions)
        actions += self._expire(cam, now)
        return actions

    def _decide(self, cam, w, wkey, req, rcfg, risk_of, now, actions):
        """One decision per look: compare the confirmed-missing items with what was announced and,
        when they differ for settle_s (or a new person's first_look_s is over), send ONE action."""
        confirmed = set()
        for i in set(req) | w.open_items:
            st = self.items.get((cam, w.tid, i))
            if st is not None and st.state == "ALERTED":
                confirmed.add(i)
        if confirmed != w.open_items:
            if w.change_since is None:
                w.change_since = now
        else:
            w.change_since = None
        first_due = w.first_seen + rcfg.get("first_look_s", self.first_look)
        if w.change_since is not None:
            due = max(w.change_since + rcfg.get("settle_s", self.settle), first_due)
        elif not w.looked:
            due = first_due
        else:
            return
        if now < due or (w.id_pending and now < due + self.id_wait):
            return
        w.looked, w.change_since = True, None
        if confirmed == w.open_items:
            if not confirmed and self.ok_info and self._all_worn(cam, w, req):
                key = w.worker_id or wkey
                if now - self.ok_said.get(key, -1e9) >= self.ok_repeat:
                    self.ok_said[key] = now
                    actions.append(self._action("info", cam, w, now, reason="ppe_complete", type_="ppe_ok"))
            return
        if w.event_id is None:                    # new violation: ONE alert with every missing item
            last = self.alert_times.get(wkey)
            repeated = last is not None and now - last <= self.repeat_window
            self.alert_times[wkey] = now
            w.open_items = set(confirmed)
            w.event_id = "evt_" + uuid.uuid4().hex[:10]
            w.opened_at = now
            w.onset = w.onset or now
            w.severity = ppe_severity(sorted(w.open_items), risk_of(w.zone), repeated)
            actions.append(self._action("open", cam, w, now))
        elif not confirmed:                       # everything back on: ONE resolve
            w.open_items = set()
            actions.append(self._action("resolve", cam, w, now, reason="compliant"))
            w.event_id, w.severity, w.onset = None, None, None
        else:                                     # different set: ONE update with the full new list
            w.open_items = set(confirmed)
            w.severity = ppe_severity(sorted(w.open_items), risk_of(w.zone))
            actions.append(self._action("update", cam, w, now))

    def _all_worn(self, cam, w, req):
        """'PPE complete' only when every required item was actually SEEN worn (not merely unverifiable)."""
        if not req:
            return False
        for i in req:
            st = self.items.get((cam, w.tid, i))
            if st is None or len(st.hist) < 3 or sum(st.hist) > len(st.hist) * 0.2:
                return False
        return True

    def _cooldown_owner(self, ckey, wkey, item, now):
        """Who holds the cooldown for this zone+item+place?  None = free to alert,
        "self" = this same worker fixed it less than rearm_s ago, else the other worker's key.
        The cooldown exists to absorb tracker ID switches (a DIFFERENT key for the same person);
        the same worker taking the item off again later is a NEW violation and must alert again."""
        cd = self.cooldowns.get(ckey)
        if not cd or cd[0] <= now:
            return None
        if cd[1] == wkey:
            return None if now - self.resolved_at.get((wkey, item), -1e9) >= self.rearm else "self"
        return cd[1]

    def _nearby_handover(self, cam, w, item):
        """Fallback for _handover's cooldown-cell lookup: a tracker ID switch can shift the
        worker's box across a grid-cell boundary (the coarse location_grid misses it), so also
        look for a just-lost worker with an open event for this item close to w's current box."""
        if w.bbox is None:
            return None
        wx, wy = (w.bbox[0] + w.bbox[2]) / 2.0, (w.bbox[1] + w.bbox[3]) / 2.0
        wdiag = max(1.0, ((w.bbox[2] - w.bbox[0]) ** 2 + (w.bbox[3] - w.bbox[1]) ** 2) ** 0.5)
        best, best_d = None, None
        for wkey, old in self.workers.items():
            if wkey[0] != cam or old is w or old.event_id is None or old.seen_tick == self.tick:
                continue
            if item not in old.open_items or old.bbox is None:
                continue
            ox, oy = (old.bbox[0] + old.bbox[2]) / 2.0, (old.bbox[1] + old.bbox[3]) / 2.0
            d = ((wx - ox) ** 2 + (wy - oy) ** 2) ** 0.5
            if d < wdiag * 0.75 and (best_d is None or d < best_d):
                best, best_d = wkey, d
        return best

    def _handover(self, w, old_key, item, actions, risk_of, now):
        """Tracker ID switch: move the old worker's open event to the new track."""
        old = self.workers.get(old_key)
        if old is None or old is w or old.event_id is None or old.seen_tick == self.tick:
            return
        if w.event_id is None:
            w.event_id, w.opened_at, w.severity = old.event_id, old.opened_at, old.severity
        w.open_items |= old.open_items
        old.event_id, old.open_items = None, set()
        actions.append(self._action("update", old_key[0], w, now))

    def rename(self, cam, old_tid, new_tid):
        """The identity layer recognised track old_tid as a known person (stable id new_tid).
        Move its state over; if that person's state already exists (re-entered the view within
        lost_track_s) keep the existing one, so their open alert simply continues."""
        old = self.workers.pop((cam, old_tid), None)
        old_items = {k: self.items.pop(k) for k in [k for k in self.items if k[0] == cam and k[1] == old_tid]}
        if old is None or (cam, new_tid) in self.workers:
            if old is not None and old.event_id:
                self.workers[(cam, old_tid)] = old       # never drop an open event silently
                self.items.update(old_items)
            return
        old.tid = new_tid
        self.workers[(cam, new_tid)] = old
        for (c, _, item), st in old_items.items():
            self.items[(c, new_tid, item)] = st
        if (cam, old_tid) in self.alert_times:
            self.alert_times[(cam, new_tid)] = self.alert_times.pop((cam, old_tid))
        for k, (until, wkey) in list(self.cooldowns.items()):
            if wkey == (cam, old_tid):
                self.cooldowns[k] = (until, (cam, new_tid))
        for (wkey, item) in [k for k in self.resolved_at if k[0] == (cam, old_tid)]:
            self.resolved_at[((cam, new_tid), item)] = self.resolved_at.pop((wkey, item))

    def _expire(self, cam, now):
        out = []
        for wkey in [k for k in self.workers if k[0] == cam]:
            w = self.workers[wkey]
            if now - w.last_seen > self.lost:
                if w.event_id:
                    out.append(self._action("resolve", cam, w, now, reason="left_view"))
                for k in [k for k in self.items if k[0] == cam and k[1] == w.tid]:
                    del self.items[k]
                del self.workers[wkey]
        for k in [k for k, v in self.cooldowns.items() if v[0] <= now]:
            del self.cooldowns[k]
        return out

    @staticmethod
    def _action(kind, cam, w, now, reason=None, type_="ppe_missing"):
        return {
            "action": kind,
            "event_id": w.event_id or f"info_{cam}_{w.tid}_{int(now * 1000)}",
            "type": type_,
            "items_missing": sorted(w.open_items),
            "severity": w.severity,
            "camera_id": cam,
            "zone_id": w.zone,
            "track_id": int(w.tid),
            "worker_id": w.worker_id,
            "worker_name": w.worker_name,
            "confidence": round(float(w.conf), 3),
            "bbox": [int(v) for v in w.bbox],
            "started_at": w.opened_at,
            "onset_at": w.onset,          # first frame the item was seen missing
            "at": now,
            "reason": reason,
        }
