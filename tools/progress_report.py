"""Builds docs/NETRAX_Progress_Report.pdf - what has been built and what it outputs.

    python -m tools.progress_report [--images <folder with dashboard.png, roi.png>]

Numbers come from docs/benchmark.json, the event database and the measurements recorded in
docs/model_card.md. Screenshots: dashboard.png / roi.png in --images (optional)."""
import argparse
import json
import os
import sqlite3
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
                                Table, TableStyle)

from edge.config import path

NAVY, GREEN, AMBER, RED, GREY = (colors.HexColor(c) for c in ("#15213a", "#2e7d5b", "#c98a1b", "#c0392b", "#667085"))
LIGHT, LINE = colors.HexColor("#f3f5f8"), colors.HexColor("#d9dee7")

pdfmetrics.registerFont(TTFont("Segoe", r"C:\Windows\Fonts\segoeui.ttf"))
pdfmetrics.registerFont(TTFont("Segoe-B", r"C:\Windows\Fonts\segoeuib.ttf"))
from reportlab.lib.fonts import addMapping  # noqa: E402
addMapping("Segoe", 0, 0, "Segoe"); addMapping("Segoe", 1, 0, "Segoe-B")
addMapping("Segoe", 0, 1, "Segoe"); addMapping("Segoe", 1, 1, "Segoe-B")

S = {
    "title": ParagraphStyle("t", fontName="Segoe-B", fontSize=26, leading=32, textColor=NAVY),
    "sub": ParagraphStyle("s", fontName="Segoe", fontSize=13, leading=18, textColor=GREY),
    "h1": ParagraphStyle("h1", fontName="Segoe-B", fontSize=16, leading=21, textColor=NAVY, spaceBefore=4, spaceAfter=8),
    "h2": ParagraphStyle("h2", fontName="Segoe-B", fontSize=12, leading=16, textColor=NAVY, spaceBefore=8, spaceAfter=4),
    "p": ParagraphStyle("p", fontName="Segoe", fontSize=9.5, leading=13.5, spaceAfter=5),
    "small": ParagraphStyle("sm", fontName="Segoe", fontSize=8, leading=10.5, textColor=GREY),
    "cell": ParagraphStyle("c", fontName="Segoe", fontSize=8.3, leading=10.8),
    "cellb": ParagraphStyle("cb", fontName="Segoe-B", fontSize=8.3, leading=10.8),
    "kpi": ParagraphStyle("k", fontName="Segoe-B", fontSize=17, leading=20, textColor=NAVY, alignment=TA_CENTER),
    "kpil": ParagraphStyle("kl", fontName="Segoe", fontSize=7.8, leading=10, textColor=GREY, alignment=TA_CENTER),
    "cap": ParagraphStyle("cap", fontName="Segoe", fontSize=8, leading=10.5, textColor=GREY, alignment=TA_CENTER),
}
STATUS = {"Done": GREEN, "Partial": AMBER, "Not started": RED, "Open": RED, "Fixed": GREEN}


def P(t, st="p"):
    return Paragraph(t, S[st])


def table(rows, widths, head=True, status_col=None):
    data = [[P(str(c), "cellb" if (head and i == 0) else "cell") for c in r] for i, r in enumerate(rows)]
    style = [("GRID", (0, 0), (-1, -1), 0.4, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
             ("ROWBACKGROUNDS", (0, 1 if head else 0), (-1, -1), [colors.white, LIGHT])]
    if head:
        style += [("BACKGROUND", (0, 0), (-1, 0), NAVY), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white)]
        for c in range(len(rows[0])):
            data[0][c] = Paragraph(str(rows[0][c]), ParagraphStyle("hd", parent=S["cellb"], textColor=colors.white))
    if status_col is not None:              # colour the status word: Done green, Partial amber, Open red
        for i, r in enumerate(rows[1:], 1):
            word = str(r[status_col])
            col = STATUS.get(word) or STATUS.get(word.split(" (")[0])
            if col:
                data[i][status_col] = Paragraph(f"<b>{word}</b>", ParagraphStyle("st", parent=S["cell"], textColor=col))
    t = Table(data, colWidths=widths, repeatRows=1 if head else 0)   # after the cells are final
    t.setStyle(TableStyle(style))
    return t


def img(p, width, max_h=None):
    if not p or not os.path.exists(p):
        return P(f"(image missing: {os.path.basename(str(p))})", "small")
    from reportlab.lib.utils import ImageReader
    w, h = ImageReader(p).getSize()
    hh = width * h / w
    if max_h and hh > max_h:
        width, hh = max_h * w / h, max_h
    return Image(p, width=width, height=hh)


def kpis(items, total_w):
    w = total_w / len(items)
    cells = [[P(v, "kpi") for v, _ in items], [P(l, "kpil") for _, l in items]]
    t = Table(cells, colWidths=[w] * len(items))
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.6, LINE),
                           ("BACKGROUND", (0, 0), (-1, -1), LIGHT), ("TOPPADDING", (0, 0), (-1, -1), 6),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    return t


def footer(c, doc):
    c.saveState()
    c.setFont("Segoe", 7.5)
    c.setFillColor(GREY)
    c.drawString(18 * mm, 10 * mm, "NETRAX - PS6 PPE, Smoke & Fire Safety AI - progress report")
    c.drawRightString(A4[0] - 18 * mm, 10 * mm, f"page {doc.page}")
    c.restoreState()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", default=os.environ.get("TEMP", "") and os.path.join(os.environ["TEMP"], "report_img"))
    ap.add_argument("--out", default=path("docs", "NETRAX_Progress_Report.pdf"))
    a = ap.parse_args()
    W = A4[0] - 36 * mm
    db = sqlite3.connect(path("data", "sentineleye.db"))
    counts = dict(db.execute("SELECT type, COUNT(*) FROM events GROUP BY type").fetchall())
    workers = db.execute("SELECT COUNT(*) FROM workers").fetchone()[0]
    bench = json.load(open(path("docs", "benchmark.json")))
    gal = path("docs", "failure_gallery")
    def snap(e):
        """Event snapshot with faces pixelated (older snapshots predate the F22 blur)."""
        import cv2
        from edge import annotate
        from edge.identity import FaceEngine
        src = path("data", "snapshots", f"{e}.jpg")
        im = cv2.imread(src)
        if im is None:
            return src
        fs = [(f[0] - 0.25 * f[2], f[1] - 0.35 * f[3], f[0] + 1.25 * f[2], f[1] + 1.2 * f[3])
              for f in FaceEngine().faces(im)]
        out = os.path.join(a.images, f"{e}_blur.jpg")
        cv2.imwrite(out, annotate.blur_regions(im, fs))
        return out
    story = []

    # ---------- cover ----------
    story += [Spacer(1, 40 * mm), P("NETRAX", "title"),
              P("AI safety monitor for PPE compliance, smoke and fire detection", "sub"), Spacer(1, 4 * mm),
              P("BPUT Hackathon 2026 · Problem Statement 6 · Progress report: work done and outputs", "sub"),
              Spacer(1, 10 * mm),
              kpis([("20 / 22", "plan features done or done for now"),
                    ("~42 ms", "inference per frame, 3 models (GTX 1650)"),
                    ("98-175 ms", "edge to dashboard (live)"),
                    ("51 / 52", "self-tests pass (1 = open smoke issue)")], W),
              Spacer(1, 10 * mm),
              P(f"Report date: {datetime.now():%d %B %Y}. Every number in this report was measured on our own "
                "prototype unless it is marked <b>assumption</b>. Parts that are planned but not built are listed "
                "as such. Faces in all images are pixelated.", "p"),
              PageBreak()]

    # ---------- 1. summary ----------
    story += [P("1. Summary", "h1"),
              P("NETRAX watches CCTV, webcam, phone or recorded video <b>on a local PC (edge)</b>. For every person "
                "it checks each required PPE item for the zone they stand in (helmet, vest, gloves). It detects "
                "fire and smoke, and turns each event into one located, prioritised alert. The alert carries zone, "
                "camera, worker ID, missing items, severity, confidence and a snapshot."),
              P("It works <b>autonomously</b>: the laptop speaker calls the worker by their registered ID until they "
                "comply, and a siren plays for fire. The supervisor dashboard keeps a human in the loop "
                "(acknowledge / escalate / dismiss, reports, analytics). Only alerts and thumbnails leave the edge; raw "
                "video never does."),
              P("What exists today", "h2"),
              table([["Area", "What works now"],
                     ["Detection", "Person (COCO YOLOv8n) + PPE model (YOLOv8s: helmet, vest, gloves) + fire/smoke model (YOLOv8n), FP32 on GPU"],
                     ["Per-worker logic", "Each item judged per person: worn / missing / can't tell; zone rules; 7-of-10 frames + grace; one alert per violation; re-alert on a real repeat"],
                     ["Worker identity", "Registration with photos on the dashboard -> W-001 IDs; face match keeps one ID per person; visitors get V-001 (memory only)"],
                     ["Fire / smoke", "Colour, white-hot core, flicker, growth, on-person, skin/hair/glare checks; siren + spoken evacuation call"],
                     ["Autonomous response", "Windows voice: 'Attention worker W 0 0 1 ... please wear your helmet now', repeats every 20 s; siren for fire"],
                     ["Cameras", "Laptop webcam (Welding bay) + mobile phone over Wi-Fi (Warehouse, picture rotated upright); N cameras supported; load manager"],
                     ["Dashboard", "Live tiles, alert feed, zones, workers panel, heatmap, shift summary, CSV/PDF reports, ROI calculator"],
                     ["Robustness", "Offline alert buffer, camera health ONLINE/DEGRADED/OFFLINE, fast fail for unreachable cameras"],
                     ["Privacy", "Faces pixelated in stored snapshots; 30-day snapshot retention; consent-based registration"]],
                    [32 * mm, W - 32 * mm]),
              Spacer(1, 4 * mm),
              P(f"Data so far: {counts.get('ppe_missing', 0)} PPE alerts, {counts.get('fire', 0)} fire and "
                f"{counts.get('smoke', 0)} smoke alerts from our own live tests (22 Sep - 10 Oct 2026); "
                f"{workers} registered worker.", "small"),
              PageBreak()]

    # ---------- 2. plan status ----------
    feats = [
        ["F01", "PPE detection (helmet, vest, gloves, boots)", "Done", "Helmet/vest/gloves live; safety shoes paused until our own shoe data"],
        ["F02", "Missing item per person + 'can't tell' state", "Done", "Only 'missing' can alert; feet out of view = can't tell"],
        ["F03", "Fire and smoke detection", "Done", "Match, lighter and fire video confirmed live"],
        ["F04", "Multi-object tracking", "Done", "ByteTrack + face ID for one stable ID per person"],
        ["F05", "N-of-M, grace, dedup, cooldown", "Done", "7/10 frames + 1.5 s; re-arm after 5 s for real repeats"],
        ["F06", "Zone rules engine", "Done", "zones.yaml: required PPE + risk per zone"],
        ["F07", "Severity engine", "Done", "Critical / High / Medium / Low by event + zone risk"],
        ["F08", "Multi-stream + health", "Partial", "Webcam + phone live; 4-stream soak measured; recorded demo clips still to record"],
        ["F09", "Backend + DB + snapshots", "Done", "FastAPI + SQLite"],
        ["F10", "Supervisor dashboard", "Done", "Ack / escalate / dismiss, live tiles, feed"],
        ["F11", "Confidence display", "Done", "On boxes and alert cards"],
        ["F12", "Edge optimisation + measured FPS", "Done", "Benchmark table (PyTorch CUDA vs ONNX CPU), 4-stream table"],
        ["F13", "Fire verification + look-alike suppression", "Done", "Plus skin/hair/glare and flame-core rules"],
        ["F14", "Analytics + heatmap", "Done", "7-day chart, zone x hour heatmap, trend insights"],
        ["F15", "CSV / PDF reports", "Done", "Per shift / zone; PDF includes the shift summary"],
        ["F16", "Escalation + notifications", "Done", "Auto-escalation; Telegram if configured, otherwise MOCK"],
        ["F17", "Offline alert buffer", "Done", "SQLite outbox, sync on reconnect"],
        ["F18", "Failure-case gallery + hard-test metrics", "Partial", "Gallery v1 (7 real cases); metrics need our own labelled data"],
        ["F19", "Worker ID", "Done", "Photo registration (face); badge/RFID planned in hardware phase"],
        ["F20", "LLM / summary layer", "Done (Plan B)", "Templated shift summary (no LLM, works offline)"],
        ["F21", "Cost / ROI", "Done", "docs/cost_roi.md + calculator page"],
        ["F22", "Privacy: head blur", "Done", "Faces pixelated in stored snapshots + retention"],
    ]
    story += [P("2. Status against the project plan (F01-F22)", "h1"),
              table([["ID", "Feature (plan section 3)", "Status", "Evidence / note"]] + feats,
                    [12 * mm, 62 * mm, 22 * mm, W - 96 * mm], status_col=2),
              Spacer(1, 3 * mm),
              P("Also built beyond the feature list: load-aware processing (plan 7.5), snapshot retention (plan 14), "
                "autonomous speaker + siren, phone camera support with rotation.", "small"),
              PageBreak()]

    # ---------- 3. architecture ----------
    arch = [["Layer", "Component", "File"],
            ["Sources", "Webcam, phone (IP Webcam), RTSP, video files; one reader thread per camera, newest frame only", "edge/stream.py"],
            ["Edge: detection", "COCO person + PPE + fire/smoke YOLO models, FP32 on GPU, CUDA warm-up", "edge/detector.py"],
            ["Edge: people", "ByteTrack IDs + face identity (YuNet + SFace) -> W-001 / V-001", "edge/tracker.py, edge/identity.py"],
            ["Edge: PPE logic", "Item-to-person association, size + hi-vis checks, zone rules", "edge/association.py, validate.py"],
            ["Edge: alert logic", "N-of-M, grace, cooldown, re-arm, resolve, ID hand-over", "edge/state_machine.py"],
            ["Edge: fire logic", "Colour, white-hot core, flicker, growth, look-alike suppression", "edge/fire_verify.py"],
            ["Edge: system", "Severity, load manager, privacy blur, retention, offline outbox", "edge/severity.py, load.py, outbox.py"],
            ["Backend", "Events, actions, escalation, reports, analytics, workers, WebSocket", "backend/main.py, reports.py"],
            ["Response", "Windows voice (worker ID) + siren", "backend/actuators.py"],
            ["Dashboard", "Single-page supervisor UI + ROI calculator", "dashboard/index.html, roi.html"]]
    story += [P("3. Architecture", "h1"),
              P("Everything that touches video stays on the edge PC. Only small alert records (about 42 KB including a "
                "snapshot, measured) and status cross to the backend and dashboard."),
              table(arch, [30 * mm, W - 30 * mm - 52 * mm, 52 * mm]),
              PageBreak()]

    # ---------- 4. outputs ----------
    story += [P("4. Outputs", "h1"), P("4.1 Supervisor dashboard (real data from our tests)", "h2"),
              img(os.path.join(a.images, "dashboard.png"), W, max_h=205 * mm),
              P("Screenshot taken from the running backend; cameras are shown offline because the edge was stopped "
                "for the screenshot. Alert feed, workers panel, shift summary and heatmap use the real event log.", "cap"),
              PageBreak(),
              P("4.2 Live detections from our tests (faces pixelated)", "h2")]
    pairs = [(snap("evt_7e3870d426"), "10 Oct - Webcam, Welding bay: registered worker <b>W-001</b> recognised; NO HELMET, VEST, GLOVES -> High alert"),
             (snap("evt_e9a264847a"), "Phone camera, Warehouse (picture rotated upright): <b>same worker W-001</b> on a second camera; NO VEST"),
             (snap("evt_5f1c8457b3"), "Phone camera: unregistered person gets a temporary visitor ID <b>V-001</b> (never stored)"),
             (snap("evt_7231e446b9"), "Match / lighter flame held in the hand (24 Sep): <b>FIRE confirmed</b>, siren + spoken evacuation call")]
    rows = []
    for i in range(0, len(pairs), 2):
        rows.append([img(p, W / 2 - 4 * mm, max_h=62 * mm) for p, _ in pairs[i:i + 2]])
        rows.append([P(c, "cap") for _, c in pairs[i:i + 2]])
    t = Table(rows, colWidths=[W / 2] * 2)
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (0, 0), (-1, -1), "CENTER")]))
    story += [t, Spacer(1, 3 * mm),
              P("4.3 Autonomous response (what the laptop says)", "h2"),
              table([["Event", "Announcement (Windows voice)"],
                     ["Registered worker violates PPE", "'Attention worker W 0 0 1, Chinmaya. Please wear your helmet and safety vest now.' (every 20 s until compliant or acknowledged)"],
                     ["Unregistered person", "'Attention unregistered person in the Welding bay. Please wear your gloves now.'"],
                     ["Fire / smoke confirmed", "Siren + 'Fire alarm. Fire detected in the Welding Bay. Evacuate the Welding Bay now.'"]],
                    [45 * mm, W - 45 * mm]),
              PageBreak()]

    # ---------- 5. measured results ----------
    br = [["Model", "Runtime", "mean ms", "p95 ms"]] + [[r["model"], r["runtime"], r["mean_ms"], r["p95_ms"]] for r in bench["rows"]]
    story += [P("5. Measured results", "h1"),
              P(f"5.1 Inference speed ({bench['gpu']}, {bench['imgsz']} px, {bench['frames']} real frames, measured {bench['measured_at']})", "h2"),
              table(br, [55 * mm, 45 * mm, 25 * mm, 25 * mm]),
              P("PyTorch on the GPU is 4-6x faster than ONNX on the CPU on this laptop, so the pipeline runs on the GPU. "
                "ONNX CPU numbers show what a GPU-less mini-PC would get. TensorRT / Jetson were not tested.", "small"),
              P("5.2 Several cameras at once (4-stream soak, 24 Sep, synthetic clips)", "h2"),
              table([["Camera", "Zone risk", "Target fps", "Achieved fps"],
                     ["Welding bay", "high", "10", "7.0"], ["Assembly line", "high", "10", "6.9"],
                     ["Warehouse", "medium", "10 -> 4 (load manager)", "4.0"], ["Warehouse 2", "medium", "10 -> 4 (load manager)", "4.1"]],
                    [40 * mm, 30 * mm, 50 * mm, 30 * mm]),
              P("About 22 processed frames/s in total on one 4 GB GPU; lower-risk zones are slowed first, no stream stops.", "small"),
              P("5.3 Response time and other measurements", "h2"),
              table([["Measure", "Value", "How measured"],
                     ["Violation first seen -> alert (one camera)", "median 2.8 s", "event log, 24 Sep night shift"],
                     ["Violation first seen -> alert (webcam + phone)", "median 4.3 s", "event log, 10 Oct (two cameras share the GPU)"],
                     ["Edge -> dashboard transport", "98-175 ms live; 0.32 s avg 10 Oct", "captured_at vs received_at per alert"],
                     ["Alert size incl. snapshot", "41.7 KB", "329 alerts in the outbox"],
                     ["Single webcam processing rate", "about 9.5 fps (on charger)", "dashboard health; on battery it drops to about 2 fps"],
                     ["Live smoke false alarms rejected (replay)", "26 / 30: all 25 older cases (faces, hair, flash); 4 new white-T-shirt cases from 10 Oct NOT yet", "replayed from stored snapshots; rules were tuned on the older cases"],
                     ["Worker identity offline check", "W-001 on 6 / 6 snapshots; 3 others -> visitor IDs", "6 images, not a benchmark"],
                     ["Self-tests", "51 of 52 pass", "python -m edge.selftest; the failing one replays the 4 open white-T-shirt smoke cases"]],
                    [62 * mm, 45 * mm, W - 107 * mm]),
              P("Accuracy of the models on our own footage (hard test set) is <b>not measured yet</b>; it needs the team's "
                "labelled data (planned). Model validation scores from training: PPE mAP50 0.872, fire/smoke mAP50 0.769 "
                "(public validation sets, not our cameras).", "small"),
              PageBreak()]

    # ---------- 6. robustness ----------
    story += [P("6. Robustness: failure-case gallery (real cases from our tests)", "h1"),
              P("Each case shows the tricky input, what a naive detector does, and what NETRAX does now. The verdict "
                "under each image was computed by the current code (python -m tools.gallery).")]
    gfiles = ["01_face_as_smoke", "03_flash_as_smoke", "04_match_in_hand", "05_phone_screen_fire",
              "06_closeup_no_boots", "07_one_person_one_id"]
    rows = []
    for i in range(0, len(gfiles), 2):
        rows.append([img(os.path.join(gal, f + ".jpg"), W / 2 - 4 * mm, max_h=95 * mm) for f in gfiles[i:i + 2]])
    t = Table(rows, colWidths=[W / 2] * 2)
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story += [t, PageBreak()]

    # ---------- 7. fixes from live tests ----------
    fixes = [["Problem seen in live tests", "Cause", "Fix", "Status"],
             ["PPE alert took about 4 s", "10/15 frames + 3 s grace; outbox polled every 0.3 s", "7/10 frames + 1.5 s; instant outbox; CUDA warm-up", "Fixed"],
             ["Speaker called only the first worker", "TTS library spoke once then went silent", "Windows SAPI voice directly; queue per announcement", "Fixed"],
             ["Speaker stopped after 3 calls", "Max 2 repeats", "Repeat every 20 s until compliant / acknowledged", "Fixed"],
             ["No new alert when the same worker violated again", "60 s cooldown meant for tracker ID switches", "Re-arm: same worker, 5 s after fixing -> new alert", "Fixed"],
             ["New ID every time a person re-entered", "Tracker numbers restart", "Face registration -> one stable ID per person", "Fixed"],
             ["Match / fire close to camera ignored", "'Fire on a person = orange vest' rule", "Flame must glow + flicker; white-hot core counted", "Fixed"],
             ["Agarbatti smoke never confirmed", "Thin plume kept starting new regions; high threshold", "Region matching tolerates drift; lower threshold; every frame when busy", "Fixed (needs live re-check)"],
             ["Smoke alarms on faces / hair / flash", "Model confuses them with smoke", "Skin, dark-hair and glare checks", "Fixed (25/25 old cases)"],
             ["Fire core also reported as smoke", "White flame core looks like smoke", "Smoke inside a fire box = part of the fire", "Fixed"],
             ["False 'no safety shoes' in close-ups", "Feet out of frame", "Full-body check; shoes paused anyway", "Fixed"],
             ["Phone picture sideways", "Phone held upright", "rotate: 90 option per camera", "Fixed"],
             ["Unreachable phone slowed the webcam", "Stream open hung > 60 s", "1.5 s reachability check first", "Fixed"],
             ["Smoke false alarms on a person in a white T-shirt (10 Oct)", "Bright, low-colour shirt passes smoke checks", "To do: stronger on-person rule / own data", "Open"],
             ["Black gloves reported missing (10 Oct)", "Public glove data has few black gloves", "Own glove data + retraining", "Open"]]
    story += [P("7. Problems found in live tests and how they were fixed", "h1"),
              table(fixes, [45 * mm, 42 * mm, 58 * mm, W - 145 * mm], status_col=3),
              Spacer(1, 3 * mm),
              P("Every fix has an automated regression test in edge/selftest.py.", "small"),
              PageBreak()]

    # ---------- 8. cost ----------
    story += [P("8. Deployment, bandwidth and cost", "h1"),
              table([["Item", "Value", "Source"],
                     ["Cloud video, 16 cameras x 4 Mbps", "about 690 GB per day upload", "assumption (2-6 Mbps per camera is typical)"],
                     ["NETRAX, 16 cameras x 80 alerts x 41.7 KB", "about 53 MB per day", "alert size measured"],
                     ["Edge PC with GPU, per ~4 cameras", "Rs 70,000 (example)", "assumption"],
                     ["Payback", "one-time cost / (yearly benefit - running cost)", "formula; calculator on the dashboard"]],
                    [62 * mm, 55 * mm, W - 117 * mm]),
              Spacer(1, 4 * mm),
              img(os.path.join(a.images, "roi.png"), W, max_h=120 * mm),
              P("ROI calculator (dashboard -> footer link). All rupee values are placeholders to change; we do not claim "
                "incident reductions.", "cap"),
              PageBreak()]

    # ---------- 9. limitations + next ----------
    story += [P("9. Known limitations (honest list)", "h1"),
              table([["Limitation", "Plan"],
                     ["A phone screen playing a fire video is detected as fire", "IR flame sensor in the hardware phase (camera AND sensor)"],
                     ["Smoke false alarms on a white T-shirt; black gloves missed", "Own data + retraining; stronger on-person smoke rule"],
                     ["Safety shoes switched off", "Train on our own shoe photos, then re-enable"],
                     ["Face-based worker ID; judging checklist prefers badge/RFID", "Consent-based and switchable; RFID badge in hardware phase"],
                     ["Accuracy on our own footage not measured yet", "Hard test set from the team's data (tools ready)"],
                     ["Slower with several cameras / on battery", "Load manager; run on charger; ONNX/TensorRT not used on this GPU"],
                     ["Notifications are MOCK unless Telegram is configured", "Configure a bot token for real messages"]],
                    [W / 2, W / 2]),
              P("10. What comes next", "h1"),
              table([["#", "Work", "Needs"],
                     ["1", "Record own data (shoes, black gloves, white shirts, fire/smoke, negatives), label, build dataset, evaluate, retrain v3", "Team recordings (tools + guide ready)"],
                     ["2", "Record demo clips for the 3rd camera (fire video for Assembly line)", "Team recording"],
                     ["3", "Fix open issues from 10 Oct (white-shirt smoke, black gloves)", "Rules now, data for gloves"],
                     ["4", "Slides, backup demo video, rehearsal (docs/demo_script.md)", "-"],
                     ["5", "Hardware phase: IR flame + MQ-2 smoke sensors, RFID, ESP32 siren (docs/hardware_plan.md)", "Hardware parts"]],
                    [8 * mm, W - 60 * mm, 52 * mm])]

    doc = SimpleDocTemplate(a.out, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=18 * mm, title="NETRAX progress report", author="NETRAX team")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    print(a.out)


if __name__ == "__main__":
    main()
