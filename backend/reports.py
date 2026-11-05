"""Analytics (F14) and CSV / PDF report export (F15)."""
import csv
import io
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from . import db

SHIFTS = {"morning": (6, 14), "evening": (14, 22), "night": (22, 6)}


def shift_of(ts):
    h = datetime.fromtimestamp(ts).hour
    for name, (a, b) in SHIFTS.items():
        if (a <= h < b) if a < b else (h >= a or h < b):
            return name
    return "night"


def events_between(since, zone=None, shift=None):
    rows = db.q("SELECT * FROM events WHERE started_at >= ? ORDER BY started_at DESC", (since,))
    out = []
    for r in rows:
        e = db.row_to_event(r)
        if e["status"] in ("suppressed", "verifying"):        # camera-only fire the station did not confirm
            continue
        if zone and e["zone_id"] != zone:
            continue
        e["shift"] = shift_of(e["started_at"])
        if shift and e["shift"] != shift:
            continue
        out.append(e)
    return out


def summary(edge_state):
    now = time.time()
    week = events_between(now - 7 * 86400)
    days = [(datetime.now() - timedelta(days=i)).date() for i in range(6, -1, -1)]
    per_day = Counter(datetime.fromtimestamp(e["started_at"]).date() for e in week)
    by_zone, by_item, by_shift = Counter(), Counter(), Counter()
    zone_shift = defaultdict(Counter)
    for e in week:
        by_zone[e["zone_name"] or e["zone_id"]] += 1
        by_shift[e["shift"]] += 1
        zone_shift[e["zone_name"] or e["zone_id"]][e["shift"]] += 1
        for it in e["items_missing"] or [e["type"]]:
            by_item[it] += 1
    dec = Counter()
    for c in (edge_state or {}).get("cameras", []):
        dec.update(c.get("decisions") or {})
    total = sum(dec.values())
    mix = {k: round(100 * dec[k] / total, 1) if total else 0 for k in ("compliant", "unverifiable", "missing")}
    judged = dec["compliant"] + dec["missing"]
    lat = [e["latency_ms"] for e in week if e["latency_ms"] is not None and e["started_at"] > now - 86400]
    tta = [e["time_to_alert_ms"] for e in week
           if e.get("time_to_alert_ms") is not None and e["started_at"] > now - 86400]
    active = db.q("SELECT COUNT(*) AS n FROM events WHERE status IN ('open','escalated')")[0]["n"]
    insights = []
    for z, sc in zone_shift.items():
        if len(sc) >= 2:
            (s1, n1), (s2, n2) = sc.most_common(2)
            if n2 and n1 >= 2 * n2:
                insights.append(f"{z} has {n1 / n2:.1f}x more violations on the {s1} shift than the {s2} shift")
    return {
        "compliance_rate": round(100 * dec["compliant"] / judged, 1) if judged else None,
        "compliance_mix": mix,
        "active_alerts": active,
        "avg_latency_s": round(sum(lat) / len(lat) / 1000, 2) if lat else None,
        # violation first seen -> alert on the dashboard (includes the N-of-M + grace wait)
        # median, not mean: one alert that waited minutes (e.g. a worker half-visible) would skew it
        "avg_time_to_alert_s": round(sorted(tta)[len(tta) // 2] / 1000, 2) if tta else None,
        "violations_7d": [{"date": d.isoformat(), "label": d.strftime("%a"), "count": per_day.get(d, 0)}
                          for d in days],
        "by_zone": dict(by_zone), "by_item": dict(by_item), "by_shift": dict(by_shift),
        "insights": insights,
        "heatmap": heatmap(week),
        "shift_summary": shift_summary(now),
    }


COLUMNS = ["event_id", "started", "ended", "shift", "type", "items_missing", "severity", "status",
           "zone_name", "camera_id", "track_id", "confidence", "latency_ms", "end_reason",
           "worker_id", "worker_name", "time_to_alert_ms"]


def _flat(e):
    fmt = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else ""
    return [e["event_id"], fmt(e["started_at"]), fmt(e["ended_at"]), e["shift"], e["type"],
            " ".join(e["items_missing"]), e["severity"], e["status"], e["zone_name"], e["camera_id"],
            e["track_id"] if e["track_id"] is not None else "", e["confidence"],
            round(e["latency_ms"]) if e["latency_ms"] is not None else "", e["end_reason"] or "",
            e.get("worker_id") or "", e.get("worker_name") or "",
            round(e["time_to_alert_ms"]) if e.get("time_to_alert_ms") is not None else ""]


def to_csv(events):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLUMNS)
    for e in events:
        w.writerow(_flat(e))
    return buf.getvalue()


def zone_summary_csv(events):
    agg = defaultdict(Counter)
    for e in events:
        agg[e["zone_name"] or e["zone_id"]][e["severity"]] += 1
        agg[e["zone_name"] or e["zone_id"]]["total"] += 1
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["zone", "total", "Critical", "High", "Medium", "Low"])
    for z, c in sorted(agg.items()):
        w.writerow([z, c["total"], c["Critical"], c["High"], c["Medium"], c["Low"]])
    return buf.getvalue()


# ---------- 5 Nov: report generator (admin): any time range + filters -> summary, breakdowns, PDF / CSV ----------
ITEM_LABEL = {"helmet": "Helmet", "vest": "Safety vest", "gloves": "Gloves", "boots": "Safety shoes", "goggles": "Goggles",
              "mask": "Mask"}
TYPE_LABEL = {"ppe_missing": "PPE violation", "unauthorized_entry": "Restricted zone", "fire": "Fire", "smoke": "Smoke",
              "entrance_check": "Entrance check"}


def custom_events(start, end, types=None, zones=None, cameras=None, workers=None, severities=None, statuses=None):
    sql, args = "SELECT * FROM events WHERE started_at >= ? AND started_at <= ? AND type != 'test'", [start, end]
    for col, vals in (("type", types), ("zone_id", zones), ("camera_id", cameras), ("worker_id", workers),
                      ("severity", severities), ("status", statuses)):
        if vals:
            sql += f" AND {col} IN ({','.join('?' * len(vals))})"
            args += list(vals)
    out = []
    for r in db.q(sql + " ORDER BY started_at", args):
        e = db.row_to_event(r)
        e["shift"] = shift_of(e["started_at"])
        out.append(e)
    return out


def _median(v):
    v = sorted(x for x in v if x is not None)
    return v[len(v) // 2] if v else None


def custom_summary(events, start, end):
    """Numbers only from the records (never estimated)."""
    fmt = lambda t: datetime.fromtimestamp(t).strftime("%d %b %Y %H:%M")
    by_type = Counter(TYPE_LABEL.get(e["type"], e["type"]) for e in events)
    by_sev = Counter(e["severity"] or "-" for e in events)
    by_zone = Counter(e["zone_name"] or e["zone_id"] or "-" for e in events)
    by_cam = Counter(e["camera_id"] or "safety station" for e in events)
    items = Counter(i for e in events if e["type"] == "ppe_missing" for i in e["items_missing"])
    workers = Counter((f"{e['worker_id']} {e.get('worker_name') or ''}".strip() if str(e.get("worker_id") or "").startswith("W-")
                       else "Unregistered / not identified") for e in events if e["type"] in ("ppe_missing", "unauthorized_entry"))
    gate = Counter((e.get("details") or {}).get("result") for e in events if e["type"] == "entrance_check")
    acked = [e for e in events if e.get("acked_at")]
    ack_s = _median([e["acked_at"] - e["started_at"] for e in acked])
    tta = _median([e.get("time_to_alert_ms") for e in events if e["type"] in ("ppe_missing", "fire", "smoke")])
    closed = sum(1 for e in events if e["status"] in ("resolved", "closed", "dismissed"))
    still = sum(1 for e in events if e["status"] in ("open", "escalated", "acknowledged", "verifying"))
    max_fire = max([e.get("fire_level") or 0 for e in events if e["type"] in ("fire", "smoke")] or [0])
    lines = [f"Period: {fmt(start)} to {fmt(end)} (IST) - {len(events)} record(s).",
             "By type: " + (", ".join(f"{k} {v}" for k, v in by_type.most_common()) or "none") + ".",
             f"Closed {closed}, still active {still}."]
    if items:
        lines.append("Most missed PPE: " + ", ".join(f"{ITEM_LABEL.get(k, k)} {v}" for k, v in items.most_common(5)) + ".")
    if by_zone:
        z, n = by_zone.most_common(1)[0]
        lines.append(f"Zone with most records: {z} ({n}).")
    if gate:
        lines.append(f"Entrance: {gate.get('approved', 0)} verified, {gate.get('denied', 0)} refused, {gate.get('manual', 0)} manual checks.")
    if max_fire:
        lines.append(f"Highest fire level reached: {max_fire} of 3.")
    if tta is not None:
        lines.append(f"Median time from hazard seen to alert: {tta / 1000:.1f} s (includes the deliberate confirmation).")
    if ack_s is not None:
        lines.append(f"Median time until a supervisor acknowledged: {ack_s:.0f} s ({len(acked)} acknowledged).")
    return {"title": "Summary", "lines": lines, "total": len(events), "closed": closed, "active": still,
            "by_type": by_type.most_common(), "by_severity": by_sev.most_common(), "by_zone": by_zone.most_common(),
            "by_camera": by_cam.most_common(), "items": [(ITEM_LABEL.get(k, k), v) for k, v in items.most_common()],
            "workers": workers.most_common(15), "entrance": {k: v for k, v in gate.items() if k},
            "median_time_to_alert_s": None if tta is None else round(tta / 1000, 2),
            "median_time_to_ack_s": None if ack_s is None else round(ack_s, 1), "max_fire_level": max_fire}


def to_pdf(events, title, summary=None):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=28, rightMargin=28,
                            topMargin=28, bottomMargin=28)
    st = getSampleStyleSheet()
    sev = Counter(e["severity"] for e in events)
    story = [Paragraph(title, st["Title"]),
             Paragraph(f"Generated {datetime.now():%Y-%m-%d %H:%M} by NETRAX. "
                       f"{len(events)} events: " + ", ".join(f"{k} {v}" for k, v in sev.items()),
                       st["Normal"]), Spacer(1, 10)]
    if summary:
        story.append(Paragraph(summary["title"], st["Heading3"]))
        story += [Paragraph("- " + ln, st["Normal"]) for ln in summary["lines"]] + [Spacer(1, 10)]
        blocks = [(h, summary.get(k)) for h, k in (("By zone", "by_zone"), ("By camera", "by_camera"), ("Most missed PPE", "items"),
                                                    ("By worker", "workers"), ("By severity", "by_severity"))] if "by_zone" in summary else []
        cells = []
        for h, rows in blocks:
            if rows:
                t = Table([[h, "n"]] + [[str(k), str(v)] for k, v in rows[:10]], colWidths=[150, 34])
                t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f766e")),
                                       ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONTSIZE", (0, 0), (-1, -1), 8),
                                       ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c9d1dd")), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
                cells.append(t)
        if cells:
            grid = Table([cells], hAlign="LEFT")
            grid.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                      ("RIGHTPADDING", (0, 0), (-1, -1), 8)]))
            story += [grid, Spacer(1, 12)]
    cols = ["Started", "Shift", "Event", "Severity", "Status", "Zone", "Camera", "Worker", "Conf."]
    data = [cols] + [[_flat(e)[1], e["shift"],
                      ("No " + ", ".join(e["items_missing"])) if e["items_missing"] else e["type"].title(),
                      e["severity"], e["status"], e["zone_name"], e["camera_id"] or "-",
                      e.get("worker_id") or "", e["confidence"] if e["confidence"] is not None else "-"]
                     for e in events[:500]]
    t = Table(data, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#15213a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c9d1dd")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f5f8")]),
    ]))
    story.append(t)
    doc.build(story)
    return buf.getvalue()


# ---------- F14 heatmap + F20 (Plan B) templated shift summary ----------
ITEM_WORDS = {"helmet": "helmet", "vest": "safety vest", "gloves": "gloves", "boots": "safety shoes", "mask": "mask"}


def heatmap(events):
    """Violations per zone x hour of day (plan F14)."""
    zones = sorted({e["zone_name"] or e["zone_id"] or "-" for e in events})
    grid = {z: [0] * 24 for z in zones}
    for e in events:
        grid[e["zone_name"] or e["zone_id"] or "-"][datetime.fromtimestamp(e["started_at"]).hour] += 1
    return {"zones": zones, "counts": [grid[z] for z in zones]}


def shift_start(ts):
    """Start time of the shift that ts falls in (night shift runs 22:00-06:00)."""
    d = datetime.fromtimestamp(ts)
    name = shift_of(ts)
    h = SHIFTS[name][0]
    start = d.replace(hour=h, minute=0, second=0, microsecond=0)
    if start > d:                       # e.g. 02:00 belongs to the night shift that began yesterday 22:00
        start -= timedelta(days=1)
    return name, start.timestamp()


def shift_summary(now=None):
    """Plain-language summary of the current shift, every number counted from the database
    (no LLM needed - plan F20 Plan B; an LLM can later rephrase exactly these facts)."""
    now = now or time.time()
    name, start = shift_start(now)
    evs = [e for e in events_between(start) if e["status"] != "dismissed"]
    a, b = SHIFTS[name]
    head = f"{name.title()} shift ({a:02d}:00-{b:02d}:00), since {datetime.fromtimestamp(start):%H:%M}"
    if not evs:
        return {"shift": name, "since": start, "title": head,
                "lines": ["No violations or fire/smoke alerts so far this shift."]}
    ppe = [e for e in evs if e["type"] == "ppe_missing"]
    fire = [e for e in evs if e["type"] in ("fire", "smoke")]
    lines = [f"{len(ppe)} PPE alert{'s' * (len(ppe) != 1)} and {len(fire)} fire/smoke alert"
             f"{'s' * (len(fire) != 1)}."]
    if fire:
        z = Counter(e["zone_name"] or e["zone_id"] for e in fire).most_common(1)[0]
        lines.append(f"Fire/smoke: {len(fire)} alert(s), most in {z[0]}; "
                     f"{sum(e['severity'] == 'Critical' for e in fire)} Critical.")
    if ppe:
        items = Counter(i for e in ppe for i in e["items_missing"])
        top_i = ", ".join(f"{ITEM_WORDS.get(i, i)} ({n})" for i, n in items.most_common(3))
        lines.append(f"Most often missing: {top_i}.")
        z = Counter(e["zone_name"] or e["zone_id"] for e in ppe).most_common(1)[0]
        lines.append(f"Zone with most PPE alerts: {z[0]} ({z[1]}).")
        workers = Counter((e["worker_id"], e.get("worker_name")) for e in ppe
                          if (e.get("worker_id") or "").startswith("W-"))
        if workers:
            (wid, wname), n = workers.most_common(1)[0]
            lines.append(f"Worker with most alerts: {wid} {wname or ''} ({n}).".replace("  ", " "))
        unknown = sum(1 for e in ppe if not (e.get("worker_id") or "").startswith("W-"))
        if unknown:
            lines.append(f"{unknown} alert(s) were for unregistered people.")
    open_ = [e for e in evs if e["status"] in ("open", "escalated")]
    if open_:
        lines.append(f"{len(open_)} still open (not acknowledged); "
                     f"{sum(e['status'] == 'escalated' for e in open_)} escalated.")
    tta = sorted(e["time_to_alert_ms"] for e in evs if e.get("time_to_alert_ms") is not None)
    if tta:
        lines.append(f"Median time from violation to alert: {tta[len(tta) // 2] / 1000:.1f} s.")
    return {"shift": name, "since": start, "title": head, "lines": lines}
