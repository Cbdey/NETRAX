"""Autonomous response on this computer: speaker announcements + fire siren.

Speaker - Windows SAPI voice (SAPI.SpVoice) in its own thread. Every announcement is spoken in
          full, one after another. (pyttsx3 was dropped: its second runAndWait() returned at once
          without speaking, so only the first worker was ever called.)
Alarm   - industrial wail siren (WAV via winsound, 13 Nov) + "Fire detected." between cycles, until acknowledged /
          resolved / silenced / timeout.
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


def make_siren_wav(path, rate=22050, up_s=2.2, down_s=2.2, lo=520.0, hi=1180.0):
    """13 Nov (user: "the fire siren used in real industries"): one cycle of an industrial WAIL siren - the pitch rises
    from lo to hi and falls back (motor-siren sweep), rich in harmonics so it carries over machine noise. Written once."""
    import math
    import struct
    import wave
    n_up, n_dn = int(up_s * rate), int(down_s * rate)
    freqs = [lo + (hi - lo) * (1 - math.cos(math.pi * k / n_up)) / 2 for k in range(n_up)] +             [hi - (hi - lo) * (1 - math.cos(math.pi * k / n_dn)) / 2 for k in range(n_dn)]
    ph, frames = 0.0, bytearray()
    fade = int(0.02 * rate)
    for k, f in enumerate(freqs):
        ph += 2 * math.pi * f / rate
        v = 0.62 * math.sin(ph) + 0.22 * math.sin(2 * ph) + 0.12 * math.sin(3 * ph) + 0.06 * math.sin(5 * ph)
        v = max(-1.0, min(1.0, v * 1.15))                                   # slight clipping = horn-like edge
        env = min(1.0, k / fade, (len(freqs) - k) / fade)
        frames += struct.pack("<h", int(v * env * 30000))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return up_s + down_s


class Alarm:
    def __init__(self, cfg):
        self.cfg = cfg or {}
        self.enabled = self.cfg.get("enabled", True)
        self.local = self.cfg.get("local", True)
        self.active = None            # {event_id, zone, reason, since, events}
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self.say = None               # 13 Nov: main sets this to the speaker -> "Fire detected." between siren cycles
        self.words = "Fire detected."

    def start(self, event_id, zone, reason, words=None):
        if not self.enabled:
            return False
        with self.lock:
            if self.active:
                self.active["events"].add(event_id)
                return False
            self.active = {"event_id": event_id, "zone": zone, "reason": reason,
                           "since": time.time(), "events": {event_id}}
            self.words = words or "Fire detected."
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
        """Industrial wail siren (WAV, loops) with a short pause every `voice_every` cycles in which the voice repeats
        "Fire detected." - until stopped (Acknowledge / Resolve / silence) or max_duration_s. Falls back to beeps."""
        try:
            import winsound
        except ImportError:
            return
        limit = self.cfg.get("max_duration_s", 120)
        every = int(self.cfg.get("voice_every", 3))
        t0 = time.time()
        wav = None
        try:
            import os
            wav = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "fire_siren_wail.wav")
            os.makedirs(os.path.dirname(wav), exist_ok=True)
            cycle = make_siren_wav(wav) if not os.path.exists(wav) else 4.4
        except Exception as e:                 # noqa: BLE001
            print(f"[alarm] siren sound file failed ({e!r}) - using beeps")
            wav = None
        self._stop.wait(2.0)                   # the first "Fire detected." is spoken before the siren starts
        n = 0
        while not self._stop.is_set():
            if limit and time.time() - t0 > limit:
                break
            if wav:
                try:
                    winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
                except RuntimeError:
                    wav = None
                    continue
                if self._stop.wait(cycle):
                    break
                n += 1
                if every and n % every == 0 and self.say:          # pause, say the words again, then wail on
                    try:
                        self.say(self.words)
                    except Exception:          # noqa: BLE001
                        pass
                    self._stop.wait(2.2)
                continue
            for f in (880, 1320):              # fallback: two-tone beeps
                if self._stop.is_set():
                    break
                try:
                    winsound.Beep(f, 280)
                except RuntimeError:
                    return
            self._stop.wait(0.15)
        try:
            winsound.PlaySound(None, 0)        # cut the siren at once
        except Exception:                      # noqa: BLE001
            pass
