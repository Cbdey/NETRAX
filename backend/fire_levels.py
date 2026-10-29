"""Fire / smoke severity level (final-round section 9).

Level 1  WARNING        small fire / smoke - inspection
Level 2  CRITICAL       emergency response - Telegram + phone immediately
Level 3  VERY CRITICAL  evacuate - full-screen red, every channel

The level is a RISK level from several pieces of evidence, not a measured fire size: visible flame area
depends on distance and angle, so area is only one input. Rules:
  * the level of an incident only goes UP while it is active (never downgraded because the flame looks
    smaller in one frame);
  * strong evidence of immediate danger (station flame + heat, several sources, fast growth) raises the
    level even when the visible flame is small.
Thresholds are prototype values in config/settings.yaml -> fire_levels; validate them on site.
"""
import json
import time

DEFAULT = {
    "l2_area_frac": 0.03,       # flame box >= 3 % of the picture
    "l3_area_frac": 0.15,       # >= 15 %
    "l2_smoke_area_frac": 0.10,
    "l3_smoke_area_frac": 0.35,
    "l2_growth": 1.3,           # box area grew 30 % within the verifier window
    "l3_growth": 2.0,
    "l2_duration_s": 15,        # still burning after 15 s
    "l3_duration_s": 60,        # still burning after 60 s
    "l3_sources": 2,            # fire seen by 2+ independent sources in the same zone (cameras / station)
    "l3_temp_c": 60,            # station temperature at/above this together with a flame
}

NAMES = {1: "WARNING", 2: "CRITICAL", 3: "VERY CRITICAL - EVACUATE"}
SEVERITY = {1: "High", 2: "Critical", 3: "Critical"}


def _area_frac(e):
    try:
        b = e.get("bbox")
        if isinstance(b, str):
            b = json.loads(b)
        w, h = float(e.get("frame_w") or 0), float(e.get("frame_h") or 0)
        if not b or w <= 0 or h <= 0:
            return None
        return max(0.0, (b[2] - b[0]) * (b[3] - b[1])) / (w * h)
    except (TypeError, ValueError, IndexError):
        return None


def _sensors(e):
    s = e.get("sensors")
    if isinstance(s, str):
        try:
            s = json.loads(s)
        except ValueError:
            s = None
    return s or {}


def classify(e, now=None, cfg=None, other_sources=0):
    """Returns (level 1..3, [reasons]). other_sources = other active fire/smoke incidents in the same zone."""
    c = dict(DEFAULT, **(cfg or {}))
    now = now or time.time()
    fire = e.get("type") == "fire"
    lvl, why = 1, []
    area = _area_frac(e)
    if area is not None:
        a2, a3 = (c["l2_area_frac"], c["l3_area_frac"]) if fire else (c["l2_smoke_area_frac"], c["l3_smoke_area_frac"])
        why.append(f"{'flame' if fire else 'smoke'} covers {area * 100:.1f}% of the picture")
        if area >= a3:
            lvl = 3
        elif area >= a2:
            lvl = max(lvl, 2)
    g = e.get("growth")
    if g:
        if g >= c["l3_growth"]:
            lvl = 3
            why.append(f"growing fast (x{g:.1f})")
        elif g >= c["l2_growth"]:
            lvl = max(lvl, 2)
            why.append(f"growing (x{g:.1f})")
    onset = e.get("onset_at") or e.get("started_at")
    if onset and e.get("status") in ("open", "acknowledged", "escalated", "verifying", None):
        dur = now - float(onset)
        if fire and dur >= c["l3_duration_s"]:
            lvl = 3
            why.append(f"burning for {dur:.0f} s")
        elif dur >= c["l2_duration_s"]:
            lvl = max(lvl, 2)
            why.append(f"active for {dur:.0f} s")
    s = _sensors(e)
    data, reasons = s.get("data") or {}, s.get("reasons") or []
    flame = bool(data.get("flame_detected")) or any("flame" in r for r in reasons)
    heat = any("temperature" in r for r in reasons)
    smoke_hw = any(k in r for r in reasons for k in ("MQ-2", "MQ-135", "PM2.5"))
    if e.get("verification") == "confirmed" or (s.get("level") == "CRITICAL"):
        lvl = max(lvl, 2)
        why.append("safety station confirms: " + ("; ".join(reasons) if reasons else "hazard"))
    temp = data.get("temperature_c")
    if flame and ((temp is not None and temp >= c["l3_temp_c"]) or (heat and smoke_hw)):
        lvl = 3
        why.append("station: flame with high heat / heavy smoke")
    if other_sources + 1 >= c["l3_sources"] and fire:
        lvl = 3
        why.append(f"seen by {other_sources + 1} sources in this zone")
    if not why:
        why.append("detected; no size / growth / sensor evidence yet")
    return lvl, why


def update_level(e, now=None, cfg=None, other_sources=0):
    """New level = max(old, computed). Returns (level, reasons, raised: bool)."""
    old = int(e.get("fire_level") or 0)
    lvl, why = classify(e, now, cfg, other_sources)
    new = max(old, lvl)
    return new, why, new > old
