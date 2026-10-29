"""Measure latency on the RUNNING system and append a table to docs/eval/latency_report.md (section 7 / test 12).
Run on the edge laptop while the backend, the edge and the dashboard are running:
    python -m tools.latency_report --label "1 camera (cam_01)" --seconds 120
    python -m tools.latency_report --label "4 cameras + 2 dashboards" --seconds 180
Reports median and 95th percentile (never one-off numbers). Keep the dashboard open: it reports what the
operator actually sees (picture age, event arrival)."""
import argparse
import os
import time
from datetime import datetime

import requests


def fmt(v):
    if v is None:
        return "-"
    return f"{v:.0f} ms" if v < 1000 else f"{v / 1000:.2f} s"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="run")
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    a = ap.parse_args()
    t0 = time.time()
    print(f"measuring '{a.label}' for {a.seconds} s - use the cameras normally (walk, raise a test alert) ...")
    cams = {}
    last = None
    while time.time() - t0 < a.seconds:
        try:
            last = requests.get(f"{a.url}/api/perf", params={"window_s": a.seconds}, timeout=5).json()
            for cid, c in ((last.get("edge") or {}).get("cameras") or {}).items():
                cams.setdefault(cid, []).append(c)
        except requests.RequestException as e:
            print("backend not reachable:", e)
        time.sleep(5)
    if not last:
        print("no data")
        return 1
    m = last.get("metrics") or {}
    lines = [f"\n## {a.label} - {datetime.now().strftime('%d %b %Y %H:%M')} ({a.seconds} s)\n",
             f"Runtime: {(last.get('edge') or {}).get('runtime') or '-'}; GPU engine: {(last.get('edge') or {}).get('engine')}\n",
             "| Camera | status | processed fps (median) | capture -> processed p50 | p95 | model p50 | wait for GPU p50 | stale frames skipped |",
             "|---|---|---|---|---|---|---|---|"]
    for cid, rows in sorted(cams.items()):
        r = rows[-1]
        fps = sorted(x.get("fps") or 0 for x in rows)[len(rows) // 2]
        lines.append(f"| {cid} | {r.get('status')} | {fps} | {fmt(r.get('frame_p50'))} | {fmt(r.get('frame_p95'))} | "
                     f"{fmt(r.get('infer_p50'))} | {fmt(r.get('wait_p50'))} | {r.get('dropped_stale', 0)} |")
    names = {"event_e2e_ms": "frame captured -> event stored (backend)",
             "dashboard_event_ms": "frame captured -> event on the dashboard",
             "preview_age_ms": "live picture age on the dashboard",
             "preview_edge_to_backend_ms": "live frame edge -> backend",
             "time_to_alert_ms": "hazard first seen -> alert (includes the deliberate confirmation window)",
             "entrance_decision_ms": "entrance: person seen -> APPROVED/DENIED/MANUAL",
             "notify_telegram_ms": "Telegram delivered", "notify_call_ms": "phone call answered (completed)",
             "notify_sms_ms": "SMS delivered", "notify_email_ms": "e-mail accepted by server",
             "assistant_ms": "AI assistant answer"}
    lines += ["", "| Measurement | n | median | 95th percentile | max |", "|---|---|---|---|---|"]
    for k, label in names.items():
        if k in m:
            lines.append(f"| {label} | {m[k]['n']} | {fmt(m[k]['p50'])} | {fmt(m[k]['p95'])} | {fmt(m[k]['max'])} |")
    text = "\n".join(lines) + "\n"
    print(text)
    os.makedirs("docs/eval", exist_ok=True)
    p = "docs/eval/latency_report.md"
    new = not os.path.exists(p)
    with open(p, "a", encoding="utf-8") as f:
        if new:
            f.write("# NETRAX measured latency (tools/latency_report.py)\n\nAll values measured on the running system; "
                    "median and 95th percentile.\n")
        f.write(text)
    print(f"appended to {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
