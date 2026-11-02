"""Draws boxes and labels for the live preview and snapshots."""
import cv2

GREEN, RED, GREY, AMBER, ORANGE = (80, 170, 60), (40, 40, 220), (150, 150, 150), (30, 160, 230), (0, 120, 255)

# One colour per class (BGR), so every PPE item, body part, fire and smoke is easy to tell apart.
# The same colours are shown as a legend on the dashboard (dashboard/index.html, CLASS_LEGEND).
CLASS_COLOURS = {
    "helmet": (0, 215, 255),     # yellow
    "vest": (0, 140, 255),       # orange
    "gloves": (60, 200, 60),     # green
    "boots": (40, 95, 160),      # brown (safety shoes)
    "foot": (40, 95, 160),       # brown (footwear region, v3)
    "goggles": (230, 200, 0),    # cyan
    "mask": (200, 70, 160),      # purple
    "head": (230, 170, 90),      # light blue (body part, v3)
    "face": (170, 170, 170),     # grey (body part, v3)
    "hand": (180, 120, 255),     # pink (body part, v3)
    "Fire": (30, 30, 230),       # red
    "Smoke": (235, 235, 235),    # white
}
BODY_PARTS = {"head", "face", "hand", "foot"}


def colour_of(cls):
    return CLASS_COLOURS.get(cls, GREEN)


def _text_colour(bg):
    b, g, r = bg
    return (20, 20, 20) if (0.299 * r + 0.587 * g + 0.114 * b) > 160 else (255, 255, 255)


def _label(img, text, x, y, color, scale=0.5):
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    y = max(th + 4, int(y))
    cv2.rectangle(img, (int(x), y - th - 6), (int(x) + tw + 6, y), color, -1)
    cv2.putText(img, text, (int(x) + 3, y - 4), cv2.FONT_HERSHEY_SIMPLEX, scale, _text_colour(color), 1, cv2.LINE_AA)


def draw(frame, persons, statuses, items, fire_boxes, zone_polys, rejected=(), parts=()):
    img = frame.copy()
    h, w = img.shape[:2]
    for zone_id, poly in zone_polys:
        pts = [(int(x * w), int(y * h)) for x, y in poly]
        for i in range(len(pts)):
            cv2.line(img, pts[i], pts[(i + 1) % len(pts)], AMBER, 1)
        _label(img, zone_id, pts[0][0], pts[0][1] + 16, AMBER)
    for it in parts:                      # body parts (v3): thin outline, no label
        x1, y1, x2, y2 = map(int, it["bbox"])
        cv2.rectangle(img, (x1, y1), (x2, y2), colour_of(it["cls"]), 1)
    for it in items:                      # PPE items: own colour + short label
        x1, y1, x2, y2 = map(int, it["bbox"])
        c = colour_of(it["cls"])
        cv2.rectangle(img, (x1, y1), (x2, y2), c, 2)
        _label(img, f"{it['cls']} {it['conf']:.2f}", x1, y1 + 14, c, scale=0.4)   # inside the box top
    for p in persons:
        st = statuses.get(p["tid"], {})
        missing = [k for k, (s, _) in st.items() if s == "missing"]
        unver = [k for k, (s, _) in st.items() if s == "unverifiable"]
        color = RED if missing else (GREY if unver and len(unver) == len(st) else GREEN)
        x1, y1, x2, y2 = map(int, p["bbox"])
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
        who = p.get("worker_id") or f"#{p.get('track_id', p['tid'])}"
        if p.get("worker_name"):
            who += f" {p['worker_name']}"
        text = f"{who} {p['conf']:.2f}"
        if missing:
            text += " NO " + ",".join(missing).upper()
        if unver:
            text += " ?" + ",".join(unver)        # cannot see that body part clearly
        if not missing and not unver and st:
            text += " PPE OK"
        _label(img, text, x1, y1, color)
        _item_column(img, st, x1, y1, x2, y2)
    for cls, bbox, status, conf in fire_boxes:
        x1, y1, x2, y2 = map(int, bbox)
        color = GREY if status == "suppressed" else colour_of(cls)
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 3 if status == "confirmed" else 1)
        _label(img, f"{cls} {conf:.2f} {status}", x1, y2 + 18, color)
    for cls, bbox, why in rejected:
        x1, y1, x2, y2 = map(int, bbox)
        cv2.rectangle(img, (x1, y1), (x2, y2), GREY, 1)
        _label(img, f"{cls}? rejected: {why}", x1, y2 + 18, GREY)
    return img


ITEM_SHORT = {"helmet": "HELMET", "vest": "VEST", "gloves": "GLOVES", "boots": "SHOES", "goggles": "GOGGLES",
              "mask": "MASK"}


def _item_column(img, st, x1, y1, x2, y2):
    """One clear line per required item inside the worker's box: green 'HELMET OK', red 'NO GLOVES',
    grey 'GOGGLES ?' (body part not visible - never counted as missing)."""
    if not st:
        return
    h = img.shape[0]
    scale = max(0.45, min(0.75, (y2 - y1) / 700))
    line = int(26 * scale / 0.55)
    y = int(y1) + line + 6
    for item in ("helmet", "goggles", "mask", "vest", "gloves", "boots"):
        if item not in st:
            continue
        s = st[item][0]
        name = ITEM_SHORT.get(item, item.upper())
        text, col = ((f"{name} OK", GREEN) if s == "compliant" else (f"NO {name}", RED) if s == "missing"
                     else (f"{name} ?", GREY))
        if y > min(h - 4, y2 - 4):
            break
        _label(img, text, x1 + 4, y, col, scale=scale)
        y += line


def highlight(img, bbox, color=RED):
    out = img.copy()
    x1, y1, x2, y2 = map(int, bbox)
    cv2.rectangle(out, (x1, y1), (x2, y2), color, 4)
    return out


def blur_regions(img, boxes):
    """Privacy (F22): pixelate faces / heads in STORED snapshots. The live view is not blurred."""
    out = img.copy()
    h, w = out.shape[:2]
    for x1, y1, x2, y2 in boxes:
        x1, y1 = max(0, int(x1)), max(0, int(y1))
        x2, y2 = min(w, int(x2)), min(h, int(y2))
        if x2 - x1 < 4 or y2 - y1 < 4:
            continue
        roi = out[y1:y2, x1:x2]
        small = cv2.resize(roi, (max(1, (x2 - x1) // 12), max(1, (y2 - y1) // 12)))
        out[y1:y2, x1:x2] = cv2.resize(small, (x2 - x1, y2 - y1), interpolation=cv2.INTER_NEAREST)
    return out


def to_jpeg(img, width=640, quality=80):
    h, w = img.shape[:2]
    if w > width:
        img = cv2.resize(img, (width, int(h * width / w)))
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else None


def entrance_banner(img, checker):
    """Big result bar on the entrance camera picture (ACCESS APPROVED / DENIED / MANUAL CHECK)."""
    s = checker.s
    h, w = img.shape[:2]
    if not s:
        text, col = "ENTRANCE PPE CHECK - step in front of the camera", (90, 90, 90)
    elif not (s.get("decided") or s.get("shown")):
        text, col = f"CHECKING PPE... {', '.join(checker.required)}", (30, 160, 230)
    else:
        who = f"{s['worker_id']} {s.get('worker_name') or ''}".strip() + ": " if s.get("worker_id") else ""
        text, col = {"approved": (who + "ACCESS APPROVED - all required PPE detected", (60, 160, 60)),
                     "denied": (who + "ACCESS DENIED - required PPE incomplete", (40, 40, 220)),
                     "manual": (who + "VERIFICATION FAILED - manual check required", (0, 140, 255))}[s.get("decided") or s["shown"]]
    bar = max(28, h // 14)
    cv2.rectangle(img, (0, h - bar), (w, h), col, -1)
    scale = max(0.5, bar / 48)
    cv2.putText(img, text, (10, h - bar // 3), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 2, cv2.LINE_AA)
    return img
