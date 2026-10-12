"""Autonomous response on this computer: speaker announcements + fire siren.

Speaker - Windows SAPI voice (SAPI.SpVoice) in its own thread. Every announcement is spoken in
          full, one after another. (pyttsx3 was dropped: its second runAndWait() returned at once
          without speaking, so only the first worker was ever called.)
Alarm   - siren pattern via winsound until acknowledged / resolved / silenced / timeout.
Without an audio device both fall back to a printed, clearly labelled MOCK."""
import heapq
import itertools
import threading
import time
from collections import deque


class Speaker:
    def __init__(self, cfg):
        self.cfg = cfg or {}
        self.enabled = self.cfg.get("enabled", True)
        self.local = self.cfg.get("local", True)
        self.log = deque(maxlen=30)
        self.mode = "starting" if self.enabled and self.local else "mock"
        # priority queue: (rank, seq, text). rank 0 = fire (clears the queue), 1 = new alert in a HIGH-risk
        # zone, 2 = medium, 3 = low, +3 for repeats, 6 = welcome / goodbye. Lowest rank is spoken first;
        # when the queue is full the LEAST important message is dropped, never a new high-risk alert.
        self.q, self.seq, self.cv = [], itertools.count(), threading.Condition()
        self.speaking = None
        if self.enabled and self.local:
            threading.Thread(target=self._run, daemon=True, name="speaker").start()

    def _run(self):
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            voice.Rate = self.cfg.get("rate", 0)            # -10 (slow) .. 10 (fast)
            voice.Volume = self.cfg.get("volume", 100)
            self.mode = "local"
        except Exception as e:                 # no Windows voice / audio device: labelled mock
            print(f"[speaker] Windows voice unavailable ({e!r}) - announcements are MOCK")
            self.mode = "mock"
            return
        while True:
            with self.cv:
                while not self.q:
                    self.cv.wait()
                _, _, text = heapq.heappop(self.q)
            self.speaking = text
            try:
                voice.Speak(text)              # synchronous: returns when the sentence is finished
            except Exception as e:
                print(f"[speaker] error: {e!r}")
            self.speaking = None

    RANKS = {"urgent": 0, "high": 1, "normal": 2, "low": 3, "repeat": 5, "info": 6}

    def rank(self, priority):
        return priority if isinstance(priority, int) else self.RANKS.get(priority, 2)

    def say(self, text, zone=None, event_id=None, priority="normal"):
        if not self.enabled:
            return None
        live = self.local and self.mode in ("local", "starting")
        rank = self.rank(priority)
        dropped = None
        if live:
            with self.cv:
                if rank == 0:                      # fire announcement: nothing else matters now
                    self.q.clear()
                heapq.heappush(self.q, (rank, next(self.seq), text))
                if len(self.q) > self.cfg.get("max_queue", 6):
                    worst = max(self.q)            # least important (highest rank, newest)
                    self.q.remove(worst)
                    heapq.heapify(self.q)
                    dropped = worst[2]
                self.cv.notify()
        entry = {"at": time.time(), "text": text, "zone": zone, "event_id": event_id,
                 "channel": "speaker" if live else "mock", "rank": rank}
        self.log.appendleft(entry)
        print(f"[speaker:{entry['channel']} p{rank}] {text}" + (f"   (queue full - dropped: {dropped})" if dropped else ""))
        return entry

    def pending(self):
        with self.cv:
            return [t for _, _, t in sorted(self.q)]


class Alarm:
    def __init__(self, cfg):
        self.cfg = cfg or {}
        self.enabled = self.cfg.get("enabled", True)
        self.local = self.cfg.get("local", True)
        self.active = None            # {event_id, zone, reason, since, events}
        self.lock = threading.Lock()
        self._stop = threading.Event()

    def start(self, event_id, zone, reason):
        if not self.enabled:
            return False
        with self.lock:
            if self.active:
                self.active["events"].add(event_id)
                return False
            self.active = {"event_id": event_id, "zone": zone, "reason": reason,
                           "since": time.time(), "events": {event_id}}
            self._stop.clear()
        if self.local:
            threading.Thread(target=self._siren, daemon=True, name="siren").start()
        print(f"[alarm] ON  - {reason}")
        return True

    def stop(self, why, event_id=None):
        with self.lock:
            if not self.active:
                return False
            if event_id is not None:
                self.active["events"].discard(event_id)
                if self.active["events"]:
                    return False           # another fire is still open
            self.active = None
            self._stop.set()
        print(f"[alarm] OFF - {why}")
        return True

    def state(self):
        a = self.active
        return None if not a else {k: (sorted(v) if isinstance(v, set) else v) for k, v in a.items()}

    def _siren(self):
        try:
            import winsound
        except ImportError:
            return
        limit = self.cfg.get("max_duration_s", 120)
        t0 = time.time()
        while not self._stop.is_set():
            if limit and time.time() - t0 > limit:
                break
            for f in (880, 1320):
                if self._stop.is_set():
                    break
                try:
                    winsound.Beep(f, 280)
                except RuntimeError:
                    return
            self._stop.wait(0.15)
