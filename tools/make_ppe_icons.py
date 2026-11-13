"""Draws NETRAX's colour PPE icons (13 Nov; v2 "realistic": jury / user - "like the real items, colourful, beautiful").

    python -m tools.make_ppe_icons       -> dashboard/icons/ppe/<item>_<state>.png  (+ fire.png, smoke.png)

Original drawings (no third-party art), shaded like real objects: colour gradients, 3D bevel (light top-left, shade
bottom-right), glossy highlights, soft drop shadow, details (helmet ridge + brim, hi-vis vest with silver reflective tape
and mesh, leather work gloves with stitching, goggles with glass lenses and strap, N95 respirator with valve, leather
safety boot with steel toe and tread). Items: helmet vest gloves goggles mask boots.
States: ok   = item on a white tile, green border + green tick badge
        miss = item on a white tile, red border + red prohibition sign over it
        unk  = faded grey item, grey border + grey "?" badge (cannot see that body part - never an alarm)
The same PNGs are used by the live camera overlay (edge/annotate.py) and the dashboards (dashboard/ppe_icons.js)."""
import os
import random

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from edge.config import path

S = 512                    # drawing size (supersampled), saved at OUT
OUT = 128
ITEMS = ("helmet", "vest", "gloves", "goggles", "mask", "boots")
GREEN, RED, GREY = (34, 160, 70), (214, 40, 40), (128, 136, 148)


def C(h, a=255):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) + (a,)


def _font(px):
    for f in ("C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(f, px)
        except OSError:
            continue
    return ImageFont.load_default()


# ------------------------------------------------------------------ shading helpers
class _Solid:
    """ImageDraw proxy for masks: shapes are FILLED (255) unless the call says otherwise."""
    SHAPES = ("ellipse", "pieslice", "polygon", "rectangle", "rounded_rectangle", "chord")

    def __init__(self, d):
        self.d = d

    def __getattr__(self, name):
        f = getattr(self.d, name)
        if name in self.SHAPES:
            def g(*a, **k):
                if "outline" not in k:
                    k.setdefault("fill", 255)
                return f(*a, **k)
            return g
        return f


def mask_of(fn):
    m = Image.new("L", (S, S), 0)
    fn(_Solid(ImageDraw.Draw(m)))
    return m


def gradient(c1, c2, vertical=True, box=None):
    """Linear gradient c1 -> c2 over the box (default whole canvas)."""
    x0, y0, x1, y1 = box or (0, 0, S, S)
    g = Image.new("RGBA", (S, S), c1)
    d = ImageDraw.Draw(g)
    n = (y1 - y0) if vertical else (x1 - x0)
    for k in range(max(1, n)):
        t = k / max(1, n - 1)
        col = tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(4))
        if vertical:
            d.line([(0, y0 + k), (S, y0 + k)], fill=col)
        else:
            d.line([(x0 + k, 0), (x0 + k, S)], fill=col)
    if vertical:
        d.rectangle((0, y1, S, S), fill=c2)
    else:
        d.rectangle((x1, 0, S, S), fill=c2)
    return g


def bbox_of(m):
    return m.getbbox() or (0, 0, S, S)


def paint(canvas, m, c1, c2, vertical=True, bevel=0.55, outline=None, ow=6, gloss=None):
    """Fill mask m with a gradient, add a 3D bevel (light top-left / shade bottom-right), outline and gloss."""
    b = bbox_of(m)
    layer = gradient(c1, c2, vertical, b)
    layer.putalpha(m)
    canvas.alpha_composite(layer)
    if bevel:
        k = 10
        sh = ImageChops.subtract(m, ImageChops.offset(m, -k, -k)).filter(ImageFilter.GaussianBlur(9))   # bottom-right rim
        hi = ImageChops.subtract(m, ImageChops.offset(m, k, k)).filter(ImageFilter.GaussianBlur(9))     # top-left rim
        sh = ImageChops.multiply(sh, m).point(lambda v: int(v * bevel))
        hi = ImageChops.multiply(hi, m).point(lambda v: int(v * bevel * 0.9))
        dark = Image.new("RGBA", (S, S), (0, 0, 0, 255))
        dark.putalpha(sh)
        canvas.alpha_composite(dark)
        light = Image.new("RGBA", (S, S), (255, 255, 255, 255))
        light.putalpha(hi)
        canvas.alpha_composite(light)
    if outline:
        ring = ImageChops.subtract(m.filter(ImageFilter.MaxFilter(ow * 2 + 1)), m)
        o = Image.new("RGBA", (S, S), outline)
        o.putalpha(ring.point(lambda v: int(v * outline[3] / 255)))
        canvas.alpha_composite(o)
    if gloss:                                     # glossy highlight ellipse (x0, y0, x1, y1, alpha), clipped to m
        g = Image.new("L", (S, S), 0)
        ImageDraw.Draw(g).ellipse(gloss[:4], fill=gloss[4])
        g = ImageChops.multiply(g.filter(ImageFilter.GaussianBlur(14)), m)
        w = Image.new("RGBA", (S, S), (255, 255, 255, 255))
        w.putalpha(g)
        canvas.alpha_composite(w)


def lines(canvas, pts_list, col, width, blur=0, clip=None):
    l = Image.new("RGBA", (S, S))
    d = ImageDraw.Draw(l)
    for pts in pts_list:
        d.line(pts, fill=col, width=width, joint="curve")
    if blur:
        l = l.filter(ImageFilter.GaussianBlur(blur))
    if clip is not None:
        l.putalpha(ImageChops.multiply(l.getchannel("A"), clip))
    canvas.alpha_composite(l)


def dashes(a, b, n=9, on=0.55):
    out = []
    for k in range(n):
        t0, t1 = k / n, (k + on) / n
        out.append([(a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0), (a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1)])
    return out


def texture(canvas, m, step=10, col=(0, 0, 0, 34), r=2):
    """Fine mesh / fabric dots clipped to m."""
    t = Image.new("RGBA", (S, S))
    d = ImageDraw.Draw(t)
    for y in range(0, S, step):
        for x in range((y // step % 2) * step // 2, S, step):
            d.ellipse((x - r, y - r, x + r, y + r), fill=col)
    t.putalpha(ImageChops.multiply(t.getchannel("A"), m))
    canvas.alpha_composite(t)


# ------------------------------------------------------------------ items
def helmet(cv):
    ol = C("6a4600", 230)
    dome = mask_of(lambda d: d.pieslice((88, 96, 424, 432), 180, 360))
    dome = ImageChops.subtract(dome, mask_of(lambda d: d.rectangle((0, 262, S, S))))
    paint(cv, dome, C("ffe866"), C("f0a400"), bevel=0.7, outline=ol, ow=5, gloss=(140, 120, 300, 230, 210))
    ridge = mask_of(lambda d: d.rounded_rectangle((228, 92, 284, 262), 26))
    paint(cv, ridge, C("fff1a6"), C("f5b800"), vertical=False, bevel=0.6, outline=C("8a6000", 160), ow=3)
    for x in (172, 340):                                                   # side ribs
        rib = mask_of(lambda d, x=x: d.rounded_rectangle((x - 9, 130 if x < 256 else 130, x + 9, 262), 9))
        paint(cv, ImageChops.multiply(rib, dome), C("ffe680"), C("e8a000"), bevel=0.4)
    brim = mask_of(lambda d: d.rounded_rectangle((44, 252, 468, 318), 33))
    paint(cv, brim, C("ffd23f"), C("c78400"), bevel=0.8, outline=ol, ow=5, gloss=(90, 256, 330, 282, 150))
    under = mask_of(lambda d: d.rounded_rectangle((110, 312, 402, 334), 11))
    paint(cv, under, C("5d4300"), C("2e2100"), bevel=0)                     # shadow under the brim (suspension)


def vest(cv):
    ol = C("6d2a00", 220)
    shape = [(152, 66), (214, 66), (256, 178), (298, 66), (360, 66), (436, 150), (436, 452), (296, 452),
             (296, 196), (216, 196), (216, 452), (76, 452), (76, 150)]
    body = mask_of(lambda d: d.polygon(shape))
    body = body.filter(ImageFilter.GaussianBlur(1.2))
    paint(cv, body, C("ff9a2e"), C("e85a00"), bevel=0.6, outline=ol, ow=5)
    texture(cv, body, step=12, col=(120, 40, 0, 40), r=2)                   # mesh fabric
    tape_c1, tape_c2 = C("f7f9fa"), C("a9b3ba")
    for (x0, x1) in ((76, 216), (296, 436)):
        band = ImageChops.multiply(mask_of(lambda d, x0=x0, x1=x1: d.rectangle((x0, 326, x1, 372))), body)
        paint(cv, band, tape_c1, tape_c2, bevel=0.35)
        lines(cv, [[(x0, 326), (x1, 326)], [(x0, 372), (x1, 372)]], C("d9ff3d"), 7, clip=body)       # lime trim
        lines(cv, [[(x, 330), (x + 18, 368)] for x in range(x0 - 20, x1, 26)], (255, 255, 255, 70), 4, clip=band)
    for xs in ((158, 198), (314, 354)):                                     # tape over the shoulders
        strap = ImageChops.multiply(mask_of(lambda d, xs=xs: d.rectangle((xs[0], 66, xs[1], 326))), body)
        paint(cv, strap, tape_c1, tape_c2, vertical=False, bevel=0.35)
        lines(cv, [[(xs[0], 66), (xs[0], 326)], [(xs[1], 66), (xs[1], 326)]], C("d9ff3d"), 6, clip=body)
    lines(cv, [[(256, 196), (256, 452)]], C("5a2400"), 4)                   # front opening
    lines(cv, dashes((248, 205), (248, 445), 16), C("3b2a20"), 5)           # zip teeth
    lines(cv, [[(140, 140), (120, 420)]], (255, 255, 255, 60), 22, blur=10, clip=body)   # fabric sheen


def gloves(cv):
    ol = C("5a3a10", 220)
    leather1, leather2 = C("f6cf6a"), C("c98b26")
    fingers = [(148, 92, 196, 290), (198, 66, 246, 280), (248, 72, 296, 282), (298, 104, 342, 290)]
    for f in fingers:
        m = mask_of(lambda d, f=f: d.rounded_rectangle(f, 24))
        paint(cv, m, leather1, leather2, vertical=False, bevel=0.6, outline=ol, ow=4,
              gloss=(f[0] + 8, f[1] + 10, f[0] + 26, f[1] + 90, 150))
    def _thumb(d):                                                         # capsule from the palm up and outwards
        a, b, r = (180, 330), (92, 196), 29
        d.line([a, b], fill=255, width=2 * r)
        d.ellipse((a[0] - r, a[1] - r, a[0] + r, a[1] + r))
        d.ellipse((b[0] - r, b[1] - r, b[0] + r, b[1] + r))
    thumb = mask_of(_thumb)
    paint(cv, thumb, leather1, leather2, bevel=0.6, outline=ol, ow=4, gloss=(80, 190, 120, 240, 140))
    lines(cv, dashes((150, 300), (100, 222), 6), C("8a5a18"), 3)
    palm = mask_of(lambda d: d.rounded_rectangle((140, 218, 352, 372), 48))
    paint(cv, palm, C("f3c75c"), C("c48424"), bevel=0.65, outline=ol, ow=4, gloss=(170, 236, 260, 300, 110))
    lines(cv, dashes((156, 250), (336, 250), 14), C("7a4e12"), 4)           # stitching
    for x in (196, 246, 296):
        lines(cv, dashes((x, 120), (x, 250), 7), C("8a5a18"), 3)
    cuff = mask_of(lambda d: d.rounded_rectangle((146, 362, 346, 456), 16))
    paint(cv, cuff, C("3d7fe0"), C("1d4fa8"), bevel=0.6, outline=C("0f2c66", 230), ow=4)
    lines(cv, [[(146, 386), (346, 386)], [(146, 432), (346, 432)]], C("8fb8ff"), 5)
    lines(cv, dashes((156, 409), (336, 409), 16), C("dce9ff"), 3)


def goggles(cv):
    strap = mask_of(lambda d: d.rounded_rectangle((14, 222, 498, 282), 24))
    paint(cv, strap, C("3a3f45"), C("15181b"), bevel=0.4)
    lines(cv, [[(x, 228), (x + 14, 276)] for x in range(20, 500, 22)], (255, 255, 255, 30), 4, clip=strap)
    frame = mask_of(lambda d: d.rounded_rectangle((66, 140, 446, 360), 96))
    paint(cv, frame, C("4f6b5c"), C("1f2d26"), bevel=0.7, outline=C("0d1410", 230), ow=4, gloss=(110, 146, 400, 180, 90))
    for x0 in (92, 266):
        lens = mask_of(lambda d, x0=x0: d.rounded_rectangle((x0, 166, x0 + 154, 334), 70))
        paint(cv, lens, C("bfefff", 245), C("2c9ccf", 245), bevel=0.5, outline=C("0d4e6e", 200), ow=3)
        lines(cv, [[(x0 + 30, 214), (x0 + 96, 186)]], (255, 255, 255, 230), 18, blur=2, clip=lens)   # reflections
        lines(cv, [[(x0 + 34, 252), (x0 + 60, 242)]], (255, 255, 255, 170), 10, blur=2, clip=lens)
        lines(cv, [[(x0 + 90, 316), (x0 + 140, 280)]], (255, 255, 255, 90), 8, blur=3, clip=lens)
    for cx in (86, 426):                                                   # vents
        for dy in (-26, 0, 26):
            m = mask_of(lambda d, cx=cx, dy=dy: d.ellipse((cx - 7, 250 + dy - 7, cx + 7, 250 + dy + 7)))
            paint(cv, m, C("0c0f0d"), C("0c0f0d"), bevel=0)


def mask(cv):
    for a, b in (((120, 200), (34, 140)), ((120, 330), (34, 392)), ((392, 200), (478, 140)), ((392, 330), (478, 392))):
        lines(cv, [[a, b]], C("2b6cb0"), 16)                               # elastic straps
        lines(cv, [[a, b]], (255, 255, 255, 80), 5)
    cup = mask_of(lambda d: d.ellipse((96, 120, 416, 420)))
    paint(cv, cup, C("ffffff"), C("cfd6dc"), bevel=0.55, outline=C("8a96a0", 220), ow=4, gloss=(140, 140, 300, 230, 180))
    texture(cv, cup, step=11, col=(90, 100, 110, 26), r=2)                  # non-woven fibre
    lines(cv, [[(256, 124), (256, 416)]], (120, 130, 140, 110), 5, clip=cup)                  # moulded seam
    lines(cv, [[(150, 160), (362, 160)]], C("aab4bc"), 14, clip=cup)                         # nose foam
    clip_ = mask_of(lambda d: d.rounded_rectangle((206, 130, 306, 158), 12))
    paint(cv, clip_, C("e6ebef"), C("8d98a2"), bevel=0.5)
    valve = mask_of(lambda d: d.ellipse((300, 250, 390, 340)))
    paint(cv, valve, C("ff8a3d"), C("d24e00"), bevel=0.7, outline=C("7a2a00", 230), ow=3, gloss=(312, 258, 350, 290, 170))
    lines(cv, [[(318, 278), (372, 278)], [(318, 296), (372, 296)], [(318, 314), (372, 314)]], C("7a2a00", 200), 5, clip=valve)


def boots(cv):
    ol = C("2a1608", 230)
    shape = [(150, 56), (292, 56), (300, 270), (392, 300), (446, 340), (458, 400), (150, 400)]
    upper = mask_of(lambda d: d.polygon(shape)).filter(ImageFilter.GaussianBlur(1.5))
    paint(cv, upper, C("a0683a"), C("5b3416"), vertical=False, bevel=0.65, outline=ol, ow=4, gloss=(170, 80, 230, 300, 90))
    toe = ImageChops.multiply(mask_of(lambda d: d.ellipse((320, 280, 500, 470))), upper)
    paint(cv, toe, C("6b4224"), C("3b2210"), bevel=0.5)
    lines(cv, [[(330, 300), (360, 290), (430, 312)]], (255, 255, 255, 60), 10, blur=4, clip=toe)
    collar = mask_of(lambda d: d.rounded_rectangle((144, 48, 298, 96), 18))
    paint(cv, collar, C("3a2a22"), C("1a120d"), bevel=0.5)
    tongue = mask_of(lambda d: d.rounded_rectangle((226, 70, 284, 270), 20))
    paint(cv, tongue, C("8a5a32"), C("5a3818"), bevel=0.4)
    for y in (118, 158, 198, 238):                                          # laces + eyelets
        lines(cv, [[(222, y), (292, y + 14)], [(222, y + 14), (292, y)]], C("f4c430"), 8)
        for x in (220, 294):
            e = mask_of(lambda d, x=x, y=y: d.ellipse((x - 7, y, x + 7, y + 14)))
            paint(cv, e, C("d7dde2"), C("7c868e"), bevel=0)
    lines(cv, dashes((160, 330), (440, 360), 18), C("f4c430"), 4)           # welt stitching
    sole = mask_of(lambda d: d.rounded_rectangle((136, 392, 474, 452), 20))
    paint(cv, sole, C("3a3a3a"), C("0e0e0e"), bevel=0.5, outline=C("000000", 200), ow=3)
    for x in range(160, 470, 34):                                           # tread lugs
        m = mask_of(lambda d, x=x: d.rectangle((x, 440, x + 18, 462)))
        paint(cv, m, C("1a1a1a"), C("000000"), bevel=0)
    lines(cv, [[(136, 404), (474, 404)]], C("f4c430"), 6)                   # yellow midsole line


DRAW = {"helmet": helmet, "vest": vest, "gloves": gloves, "goggles": goggles, "mask": mask, "boots": boots}


def fire(cv):
    glow = mask_of(lambda d: d.ellipse((70, 90, 442, 490))).filter(ImageFilter.GaussianBlur(40))
    g = Image.new("RGBA", (S, S), C("ff6a00"))
    g.putalpha(glow.point(lambda v: int(v * 0.55)))
    cv.alpha_composite(g)
    outer = mask_of(lambda d: d.polygon([(256, 24), (330, 120), (386, 168), (420, 280), (408, 380), (350, 460), (162, 460),
                                         (104, 380), (92, 280), (134, 190), (176, 236), (200, 130)])).filter(ImageFilter.GaussianBlur(6))
    paint(cv, outer, C("ff3d00"), C("b71c00"), bevel=0.3)
    mid = mask_of(lambda d: d.polygon([(262, 132), (328, 232), (350, 340), (316, 436), (198, 436), (164, 350), (184, 270),
                                       (222, 296)])).filter(ImageFilter.GaussianBlur(6))
    paint(cv, mid, C("ffb300"), C("ff6d00"), bevel=0)
    core = mask_of(lambda d: d.polygon([(258, 262), (304, 336), (302, 404), (256, 440), (210, 404), (212, 336)])).filter(ImageFilter.GaussianBlur(5))
    paint(cv, core, C("fffde7"), C("ffe24a"), bevel=0)


def smoke(cv):
    for box, c1, c2 in (((50, 220, 270, 420), "b0bcc4", "6b7780"), ((170, 130, 410, 370), "d5dde2", "8c98a0"),
                        ((290, 230, 480, 420), "9aa6ae", "5d6870"), ((130, 280, 370, 460), "e8eef1", "a2adb4")):
        m = mask_of(lambda d, box=box: d.ellipse(box)).filter(ImageFilter.GaussianBlur(4))
        paint(cv, m, C(c1, 240), C(c2, 240), bevel=0.45, gloss=(box[0] + 30, box[1] + 20, box[0] + 120, box[1] + 80, 120))


# ------------------------------------------------------------------ tiles + state marks
def _art(fn, scale=0.80):
    a = Image.new("RGBA", (S, S))
    fn(a)
    sh = a.getchannel("A").filter(ImageFilter.GaussianBlur(10))             # soft drop shadow
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 255))
    shadow.putalpha(sh.point(lambda v: int(v * 0.35)))
    base = Image.new("RGBA", (S, S))
    base.alpha_composite(ImageChops.offset(shadow, 8, 12))
    base.alpha_composite(a)
    k = int(S * scale)
    base = base.resize((k, k), Image.LANCZOS)
    out = Image.new("RGBA", (S, S))
    out.alpha_composite(base, ((S - k) // 2, (S - k) // 2))
    return out


def _tile(border, alpha=245):
    im = Image.new("RGBA", (S, S))
    t = mask_of(lambda d: d.rounded_rectangle((12, 12, S - 12, S - 12), 100))
    g = gradient(C("ffffff", alpha), C("e9edf1", alpha))
    g.putalpha(t.point(lambda v: int(v * alpha / 255)))
    im.alpha_composite(g)
    ring = ImageChops.subtract(t, mask_of(lambda d: d.rounded_rectangle((40, 40, S - 40, S - 40), 76)))
    r = Image.new("RGBA", (S, S), border + (255,))
    r.putalpha(ring)
    im.alpha_composite(r)
    return im


def _badge(im, colour, kind):
    c = (S - 118, S - 118)
    sh = mask_of(lambda d: d.ellipse((c[0] - 92, c[1] - 84, c[0] + 96, c[1] + 104))).filter(ImageFilter.GaussianBlur(8))
    s = Image.new("RGBA", (S, S), (0, 0, 0, 255))
    s.putalpha(sh.point(lambda v: v // 3))
    im.alpha_composite(s)
    d = ImageDraw.Draw(im)
    d.ellipse((c[0] - 90, c[1] - 90, c[0] + 90, c[1] + 90), fill=(255, 255, 255, 255))
    disc = mask_of(lambda dd: dd.ellipse((c[0] - 76, c[1] - 76, c[0] + 76, c[1] + 76)))
    lighter = tuple(min(255, int(v * 1.25 + 30)) for v in colour)
    paint(im, disc, lighter + (255,), colour + (255,), bevel=0.4)
    d = ImageDraw.Draw(im)
    if kind == "tick":
        d.line([(c[0] - 40, c[1] + 2), (c[0] - 10, c[1] + 34), (c[0] + 44, c[1] - 30)], fill="white", width=26, joint="curve")
    else:
        d.text(c, "?", font=_font(130), fill="white", anchor="mm")


def make(item, state):
    art = _art(DRAW[item])
    if state == "unk":
        g = art.convert("LA").convert("RGBA")
        g.putalpha(art.getchannel("A").point(lambda v: int(v * 0.5)))
        art = ImageEnhance.Brightness(g).enhance(1.2)
    im = _tile({"ok": GREEN, "miss": RED, "unk": GREY}[state], 245 if state != "unk" else 205)
    im.alpha_composite(art)
    if state == "ok":
        _badge(im, GREEN, "tick")
    elif state == "unk":
        _badge(im, GREY, "?")
    else:                                                       # prohibition sign over the item
        ring = mask_of(lambda d: (d.ellipse((60, 60, S - 60, S - 60), outline=255, width=42),
                                  d.line([(130, 130), (S - 130, S - 130)], fill=255, width=42)))
        sh = Image.new("RGBA", (S, S), (0, 0, 0, 255))
        sh.putalpha(ring.filter(ImageFilter.GaussianBlur(6)).point(lambda v: v // 3))
        im.alpha_composite(ImageChops.offset(sh, 4, 6))
        paint(im, ring, C("ff4b4b"), C("c41616"), bevel=0.35)
    return im.resize((OUT, OUT), Image.LANCZOS)


def make_hazard(fn):
    im = _art(fn, 0.9)
    return im.resize((OUT, OUT), Image.LANCZOS)


def main():
    random.seed(1)
    out = path("dashboard", "icons", "ppe")
    os.makedirs(out, exist_ok=True)
    n = 0
    for item in ITEMS:
        for st in ("ok", "miss", "unk"):
            make(item, st).save(os.path.join(out, f"{item}_{st}.png"), optimize=True)
            n += 1
    make_hazard(fire).save(os.path.join(out, "fire.png"), optimize=True)
    make_hazard(smoke).save(os.path.join(out, "smoke.png"), optimize=True)
    big = 160                                                   # contact sheet to look at
    sheet = Image.new("RGBA", (big * 6 + 70, big * 4 + 50), (40, 44, 52, 255))
    for i, item in enumerate(ITEMS):
        for j, st in enumerate(("ok", "miss", "unk")):
            im = Image.open(os.path.join(out, f"{item}_{st}.png")).resize((big, big), Image.LANCZOS)
            sheet.alpha_composite(im, (10 + i * (big + 10), 10 + j * (big + 10)))
    for i, n_ in enumerate(("fire", "smoke")):
        im = Image.open(os.path.join(out, f"{n_}.png")).resize((big, big), Image.LANCZOS)
        sheet.alpha_composite(im, (10 + i * (big + 10), 20 + 3 * (big + 10)))
    sheet.save(path("_to_delete", "ppe_icon_sheet.png"))
    print(f"{n + 2} icons -> dashboard/icons/ppe/  (preview: _to_delete/ppe_icon_sheet.png)")


if __name__ == "__main__":
    main()
