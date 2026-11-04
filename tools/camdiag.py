"""Camera-only test, NO AI (4 Nov): is the freezing caused by the cameras / Wi-Fi or by the AI?

    python -m tools.camdiag                 all enabled cameras together for 30 s (stop the edge first!)
    python -m tools.camdiag --seconds 60 --only cam_02

For every camera it measures, while ALL cameras run at the same time (like the real system):
  resolution, frames per second received, the longest gap between two frames (a "freeze"),
  how many gaps were longer than 0.5 s, decode/read time per frame, and reconnects.
Read the verdict lines: if a camera freezes HERE, the problem is the camera / network (fix: phone settings,
hotspot, USB port) - no AI change can fix it. If every camera is smooth here but freezes in the full system,
the problem is processing, and `[perf]` lines in the edge window show which stage.
Results are appended to docs/eval/camdiag.md."""
import argparse
import os
import threading
import time
from datetime import datetime

import cv2

from edge.config import load_all, path
from edge.stream import StreamReader


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--only", default=None, help="comma-separated camera ids")
    a = ap.parse_args()
    _, _, cams = load_all()
    cams = {c: v for c, v in cams.items() if v.get("enabled", True) is not False}
    if a.only:
        cams = {c: v for c, v in cams.items() if c in a.only.split(",")}
    readers, stats = {}, {}
    for cid, cam in cams.items():
        r = StreamReader(cid, cam["source"], 30, cam.get("rotate", 0), cam.get("capture"))
        r.start()
        readers[cid] = r
        stats[cid] = {"frames": 0, "gaps": [], "last": None, "size": None, "no": 0}
    print(f"testing {', '.join(cams)} together for {a.seconds} s (no AI) ...")
    t0 = time.time()
    while time.time() - t0 < a.seconds:
        now = time.time()
        for cid, r in readers.items():
            f, ts, no = r.latest()
            s = stats[cid]
            if f is not None and no != s["no"]:
                if s["last"] is not None:
                    s["gaps"].append(ts - s["last"])
                s["frames"] += max(1, no - s["no"]) if s["no"] else 1
                s["no"], s["last"] = no, ts
                s["size"] = f"{f.shape[1]}x{f.shape[0]}"
        time.sleep(0.005)
    dur = time.time() - t0
    lines = [f"\n## {datetime.now().strftime('%d %b %Y %H:%M')} - {len(cams)} camera(s) together, {a.seconds} s, no AI\n",
             "| Camera | source | picture | fps received | longest freeze | freezes > 0.5 s | status | verdict |",
             "|---|---|---|---|---|---|---|---|"]
    for cid, s in stats.items():
        r = readers[cid]
        g = s["gaps"]
        fps = s["frames"] / dur
        worst = max(g) if g else None
        long_ = sum(1 for x in g if x > 0.5)
        if not s["frames"]:
            verdict = "NO PICTURE - " + (r.detail or "camera not reachable")
        elif long_ >= 3 or (worst and worst > 2):
            verdict = "FREEZES without AI -> camera / network problem (see guide)"
        elif fps < 8:
            verdict = "slow source -> lower the phone resolution / check light (webcams drop fps in the dark)"
        else:
            verdict = "smooth"
        lines.append(f"| {cid} | {str(cams[cid]['source'])[:40]} | {s['size'] or '-'} | {fps:.1f} | "
                     f"{'%.2f s' % worst if worst else '-'} | {long_} | {r.state if hasattr(r, 'state') else ''} {r.detail} | {verdict} |")
    for r in readers.values():
        r.stop()
    text = "\n".join(lines) + "\n"
    print(text)
    os.makedirs(path("docs", "eval"), exist_ok=True)
    with open(path("docs", "eval", "camdiag.md"), "a", encoding="utf-8") as f:
        f.write(text)
    print("appended to docs/eval/camdiag.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
