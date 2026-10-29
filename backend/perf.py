"""Latency measurements (final-round section 7): rolling samples -> median / p95 / max.

Metrics (all milliseconds):
  event_e2e_ms          frame captured on the edge -> event stored in the backend (edge clock = laptop clock)
  dashboard_event_ms    frame captured -> event shown in a dashboard (reported by the browser)
  preview_age_ms        age of the live picture when the browser displays it (reported by the browser)
  edge_infer_ms         model time per frame (reported by the edge)
  edge_frame_ms         frame captured -> frame fully processed on the edge
  time_to_alert_ms      hazard first seen -> alert raised (includes the deliberate confirmation window)
  notify_<channel>_ms   event queued -> provider confirmed (Telegram / call / sms / email)
  assistant_ms          AI-assistant question -> answer
"""
import threading
import time
from collections import defaultdict, deque

_LOCK = threading.Lock()
_S = defaultdict(lambda: deque(maxlen=2000))     # name -> (t, value)


def add(name, value):
    if value is None:
        return
    try:
        v = float(value)
    except (TypeError, ValueError):
        return
    if v < 0 or v != v:
        return
    with _LOCK:
        _S[name].append((time.time(), v))


def pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 1)


def summary(window_s=None):
    now = time.time()
    out = {}
    with _LOCK:
        items = {k: list(v) for k, v in _S.items()}
    for name, rows in sorted(items.items()):
        vals = [v for t, v in rows if window_s is None or now - t <= window_s]
        if not vals:
            continue
        out[name] = {"n": len(vals), "p50": pct(vals, 50), "p95": pct(vals, 95), "max": round(max(vals), 1),
                     "mean": round(sum(vals) / len(vals), 1)}
    return out


def reset():
    with _LOCK:
        _S.clear()
