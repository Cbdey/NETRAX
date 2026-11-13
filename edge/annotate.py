"""Draws the AI result on the live preview, clips and snapshots.

13 Nov (jury: "too crowded with many people - a box on every person and every item, writing everywhere"):
NO boxes and NO text inside the camera picture any more, except each person's NAME TAG. Per person:
    [ Ritika · W-004 ] [ UNAUTHORISED ]        name tag above the head (amber tag only when not allowed in that zone)
    [helmet][vest][gloves]                     colour PPE icons of that zone's required items:
                                               green tick = worn, red prohibition sign = missing, faded grey ? = cannot see
Unregistered people: "Unregistered person" (+ UNAUTHORISED in a restricted zone). Fire / smoke = a flame / smoke icon
on the spot. Icons: dashboard/icons/ppe/*.png (tools/make_ppe_icons.py) - the same pictures as on the dashboards.
Snapshots mark the alerted person with red CORNER marks (not a full box). Display only: no decision changes here."""
import os

import cv2
import numpy as np

from .config import path

GREEN, RED, GREY, AMBER, ORANGE = (80, 170, 60), (40, 40, 220), (150, 150, 150), (30, 160, 230), (0, 120, 255)

# One colour per class (BGR) - kept for the dashboard legend / tools; the live picture no longer draws item boxes.
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
ORDER = ("helmet", "vest", "gloves", "goggles", "mask", "boots")       # same order as dashboard/ppe_icons.js
STATE = {"compliant": "ok", "missing": "miss"}                          # anything else = cannot see
ICON_DIR = path("dashboard", "icons", "ppe")
TAG_BG = (52, 40, 33)            # BGR dark slate name tag
TAG_UNAUTH = (11, 158, 245)      # BGR amber


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


# ------------------------------------------------------------------ cached BGRA sprites
_ICONS, _TAGS, _SRC = {}, {}, {}


def _icon(name, px, grey=False):
    key = (name, px, grey)
    if key not in _ICONS:
        if name not in _SRC:
            _SRC[name] = cv2.imread(os.path.join(ICON_DIR, name + ".png"), cv2.IMREAD_UNCHANGED)
        src = _SRC[name]
        if src is None or src.ndim != 3 or src.shape[2] != 4:
            _ICONS[key] = None
        else:
            im = cv2.resize(src, (px, px), interpolation=cv2.INTER_AREA)
            if grey:
                g = cv2.cvtColor(im[:, :, :3], cv2.COLOR_BGR2GRAY)
                im = np.dstack([g, g, g, (im[:, :, 3] * 0.7).astype(np.uint8)])
            _ICONS[key] = im
        if len(_ICONS) > 600:
            _ICONS.clear()
    return _ICONS[key]


def _font(px):
    from PIL import ImageFont
    for f in ("C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(f, px)
        except OSError:
            continue
    return ImageFont.load_default()


def _tag(text, px, bg):
    """Rounded name tag (BGRA), white bold text. Cached per (text, size, colour)."""
    key = (text, px, bg)
    if key not in _TAGS:
        from PIL import Image, ImageDraw
        f = _font(px)
        x0, y0, x1, y1 = f.getbbox(text)
        pad_x, pad_y = int(px * 0.55), int(px * 0.30)
        w, h = (x1 - x0) + 2 * pad_x, (y1 - y0) + 2 * pad_y
        im = Image.new("RGBA", (w, h))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=(bg[2], bg[1], bg[0], 225),
                            outline=(255, 255, 255, 120), width=max(1, px // 12))
        d.text((pad_x - x0, pad_y - y0), text, font=f, fill=(255, 255, 255, 255))
        a = np.array(im)
        _TAGS[key] = a[:, :, [2, 1, 0, 3]].copy()
        if len(_TAGS) > 400:
            _TAGS.clear()
    return _TAGS[key]


def _blend(img, sprite, x, y):
    """Alpha-blend a BGRA sprite onto img with its top-left at (x, y); clipped at the picture edges."""
    if sprite is None:
        return
    H, W = img.shape[:2]
    h, w = sprite.shape[:2]
    x, y = int(x), int(y)
    sx0, sy0 = max(0, -x), max(0, -y)
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return
    sp = sprite[sy0:sy0 + (y1 - y0), sx0:sx0 + (x1 - x0)]
    a = sp[:, :, 3:4].astype(np.float32) / 255.0
    roi = img[y0:y1, x0:x1]
    roi[:] = (sp[:, :, :3] * a + roi * (1.0 - a)).astype(np.uint8)


def person_label(p):
    """Same identity rule as the dashboards (dashboard/people.js): 'Name · W-004' / 'W-004' / 'Unregistered person'."""
    wid = p.get("worker_id")
    if wid and (p.get("identity") in (None, "worker")) and str(wid).startswith("W-"):
        name = p.get("worker_name")
        return f"{name} · {wid}" if name else wid
    return "Unregistered person"


def _sizes(img_w, person_h):
    icon = int(max(img_w * 0.03, min(img_w * 0.05, person_h * 0.12)))
    return icon, max(11, int(icon * 0.50))


def draw(frame, persons, statuses, items, fire_boxes, zone_polys, rejected=(), parts=(), unauth=()):
    """Live / clip / snapshot picture: name tag + PPE icons per person, fire / smoke icons. items, parts, zone_polys and
    rejected are accepted for compatibility but no longer drawn (they were the crowded boxes)."""
    img = frame.copy()
    H, W = img.shape[:2]
    for p in sorted(persons, key=lambda q: q["bbox"][3]):          # nearer (lower) people drawn last = on top
        x1, y1, x2, y2 = map(float, p["bbox"])
        st = statuses.get(p["tid"], {})
        icon, font_px = _sizes(W, y2 - y1)
        gap = max(2, icon // 8)
        tags = [_tag(person_label(p), font_px, TAG_BG)]
        if p["tid"] in unauth:
            tags.append(_tag("UNAUTHORISED", font_px, TAG_UNAUTH))
        names = [f"{it}_{STATE.get(st[it][0], 'unk')}" for it in ORDER if it in st]
        tag_w = sum(t.shape[1] for t in tags) + gap * (len(tags) - 1)
        tag_h = max(t.shape[0] for t in tags)
        row_w = len(names) * icon + gap * max(0, len(names) - 1)
        block_h = tag_h + (gap + icon if names else 0)
        cx = (x1 + x2) / 2
        top = y1 - tag_h - gap * 2                                  # tag above the head ...
        if top < 2:
            top = max(2.0, y1 + gap)                                # ... or just inside when the head is at the edge
        top = min(top, H - block_h - 2)
        x = min(max(2.0, cx - tag_w / 2), W - tag_w - 2)
        for t in tags:
            _blend(img, t, x, top + (tag_h - t.shape[0]) / 2)
            x += t.shape[1] + gap
        if names:
            x = min(max(2.0, cx - row_w / 2), W - row_w - 2)
            y = top + tag_h + gap
            for n in names:
                _blend(img, _icon(n, icon), x, y)
                x += icon + gap
    for cls, bbox, status, conf in fire_boxes:                      # flame / smoke icon on the spot, no box
        bx1, by1, bx2, by2 = map(float, bbox)
        px = int(min(W * 0.10, max(W * 0.06, max(bx2 - bx1, by2 - by1) * 0.5)))
        if status == "confirmed":
            px = int(px * 1.25)
        _blend(img, _icon("fire" if cls == "Fire" else "smoke", px, grey=(status == "suppressed")),
               (bx1 + bx2) / 2 - px / 2, max(0.0, (by1 + by2) / 2 - px / 2))
    return img


def highlight(img, bbox, color=RED):
    """Snapshot: mark the alerted person with corner marks (13 Nov: no full boxes in pictures)."""
    out = img.copy()
    x1, y1, x2, y2 = map(int, bbox)
    L = max(10, int(min(x2 - x1, y2 - y1) * 0.18))
    t = max(3, out.shape[1] // 320)
    for (x, y, dx, dy) in ((x1, y1, 1, 1), (x2, y1, -1, 1), (x1, y2, 1, -1), (x2, y2, -1, -1)):
        cv2.line(out, (x, y), (x + dx * L, y), color, t, cv2.LINE_AA)
        cv2.line(out, (x, y), (x, y + dy * L), color, t, cv2.LINE_AA)
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
    """Entrance camera: a coloured strip with a symbol, no writing (13 Nov). Green tick = approved, red cross = denied,
    amber ? = manual check, thin blue strip = checking. The entrance page / cards say it in words."""
    s = checker.s
    h, w = img.shape[:2]
    if not s:
        return img
    res = s.get("decided") or s.get("shown")
    if not res:
        cv2.rectangle(img, (0, h - max(6, h // 80)), (w, h), (230, 150, 30), -1)
        return img
    col = {"approved": (60, 160, 60), "denied": (40, 40, 220), "manual": (0, 140, 255)}[res]
    bar = max(28, h // 12)
    over = img.copy()
    cv2.rectangle(over, (0, h - bar), (w, h), col, -1)
    cv2.addWeighted(over, 0.85, img, 0.15, 0, img)
    c, r, t = (w // 2, h - bar // 2), int(bar * 0.36), max(3, bar // 9)
    if res == "approved":
        cv2.line(img, (c[0] - r, c[1]), (c[0] - r // 3, c[1] + r * 2 // 3), (255, 255, 255), t, cv2.LINE_AA)
        cv2.line(img, (c[0] - r // 3, c[1] + r * 2 // 3), (c[0] + r, c[1] - r * 2 // 3), (255, 255, 255), t, cv2.LINE_AA)
    elif res == "denied":
        cv2.line(img, (c[0] - r, c[1] - r), (c[0] + r, c[1] + r), (255, 255, 255), t, cv2.LINE_AA)
        cv2.line(img, (c[0] + r, c[1] - r), (c[0] - r, c[1] + r), (255, 255, 255), t, cv2.LINE_AA)
    else:
        q = _tag("?", max(12, int(bar * 0.55)), col)
        _blend(img, q, c[0] - q.shape[1] / 2, c[1] - q.shape[0] / 2)
    return img
