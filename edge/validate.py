"""Item sanity checks (plan section 7.1 - reduce false 'compliant' decisions).

A detection is only accepted as real PPE if it also passes:
  1. size check    - the box is a plausible size relative to the person box
                     (a cap-sized box high on the head is fine; a shirt-sized box is not a helmet)
  2. colour check  - a safety vest must actually contain high-visibility colour
                     (saturated yellow / orange / lime). A plain shirt is rejected.
Both checks are configurable in settings.yaml -> validation.
"""
import cv2
import numpy as np

# item: (min_w, max_w, min_h, max_h) as fractions of the person box
SIZE = {
    # 11 Nov, measured on our own labelled frames: 31 of 260 REAL helmets were thrown away because a slim standing
    # person's box is narrow (helmet 0.60-0.86 of it) and 9 real vests because the vest covers 0.65-0.81 of the body
    # height. Max helmet width 0.60 -> 0.95 (height limit kept: a shirt-sized box is still not a helmet),
    # max vest height 0.62 -> 0.85.
    "helmet": (0.12, 0.95, 0.04, 0.22),
    "mask":   (0.06, 0.50, 0.03, 0.18),
    "goggles": (0.06, 0.55, 0.02, 0.12),
    "vest":   (0.22, 1.15, 0.12, 0.85),
    "gloves": (0.02, 0.38, 0.02, 0.32),
    "boots":  (0.04, 0.65, 0.02, 0.28),
}


EDGE_PX = 4
UPPER_BODY = {"helmet", "mask", "vest", "goggles"}


def size_ok(person, item_box, cls, cut_bottom=False):
    if cls not in SIZE:
        return True
    pw, ph = person[2] - person[0], person[3] - person[1]
    if pw <= 0 or ph <= 0:
        return False
    w, h = (item_box[2] - item_box[0]) / pw, (item_box[3] - item_box[1]) / ph
    a, b, c, d = SIZE[cls]
    if cut_bottom and cls in UPPER_BODY:
        # close-up: the person box is only head+torso, so its height is not the body height. Without a height check
        # the width must do the work: real close-up helmets measured 0.60-0.67 of the box, a shirt ~0.9 -> max 0.75
        if cls == "helmet":
            b = min(b, 0.75)
        return a <= w <= b and h >= c
    return a <= w <= b and c <= h <= d


PALE = {"s_min": 50, "v_min": 130}


def hivis_share(frame, box, pale=None):
    """Share of pixels that look like high-visibility fabric.
    11 Nov: the entrance USB camera (and dim / bright rooms) washes the fluorescent yellow-lime vest out to saturation
    74-85 - the old rule needed > 90 and accepted only 25 % of the REAL vests in our own labelled frames (entrance:
    'vest? rejected: not hi-vis' on a vest the model saw at 0.95). Also count PALE fluorescent yellow / lime
    (hue 24-48, saturation > s_min, bright > v_min): 98 % accepted; skin / beige / white / blue stay out (other hues
    or no saturation). settings validation.vest_pale_s_min / vest_pale_v_min."""
    pale = pale or PALE
    fh, fw = frame.shape[:2]
    x1, y1 = max(0, int(box[0])), max(0, int(box[1]))
    x2, y2 = min(fw, int(box[2])), min(fh, int(box[3]))
    if x2 - x1 < 4 or y2 - y1 < 4:
        return 0.0
    roi = frame[y1:y2, x1:x2]
    if roi.shape[0] * roi.shape[1] > 120 * 120:
        roi = cv2.resize(roi, (120, 120))
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0].astype(np.int16), hsv[:, :, 1], hsv[:, :, 2]
    hi = ((h >= 10) & (h <= 45)) | ((h >= 45) & (h <= 90) & (s > 120))   # orange/yellow, lime
    strong = hi & (s > 90) & (v > 90)
    washed = (h >= 24) & (h <= 48) & (s > int(pale["s_min"])) & (v > int(pale["v_min"]))
    return float((strong | washed).mean())


def item_ok(frame, person, det, cfg):
    """True if this detection may count as the worker actually wearing the item."""
    cls = det["cls"]
    cut_bottom = person[3] >= frame.shape[0] - EDGE_PX
    if not size_ok(person, det["bbox"], cls, cut_bottom):
        return False, "size"
    # 12 Nov: second-stage eyewear check (edge/eyewear.py) - spectacles / sunglasses are not safety goggles
    if cls == "goggles" and det.get("eyewear") == "normal_glasses":
        return False, "normal glasses"
    if cls == "goggles" and det.get("eyewear") == "unclear":
        return False, "unclear"
    if cls == "vest" and cfg.get("vest_hivis_min", 0) > 0:
        pale = {"s_min": cfg.get("vest_pale_s_min", PALE["s_min"]), "v_min": cfg.get("vest_pale_v_min", PALE["v_min"])}
        if hivis_share(frame, det["bbox"], pale) < cfg["vest_hivis_min"]:
            return False, "not hi-vis"
    return True, "ok"
