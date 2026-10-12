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
            text += " OK"
        _label(img, text, x1, y1, color)
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
