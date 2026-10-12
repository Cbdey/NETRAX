"""PPE-to-person association (plan section 7.1).

For every tracked person and every required PPE item the result is one of:
  compliant    - item found in the expected body region
  missing      - person clearly visible, item absent  (only this can raise an alert)
  unverifiable - person too small, cut off by the frame edge, occluded, or low confidence
"""
from .geometry import area, intersection, center
from .validate import item_ok

# Expected body region per item, as fractions of the person box.
# (y_top, y_bottom, x_expand)
REGIONS = {
    "helmet": (-0.10, 0.30, 0.10),
    "mask":   (0.00, 0.35, 0.05),
    "goggles": (0.00, 0.22, 0.05),   # eye band (v3; pose keypoints will replace these fractions)
    "vest":   (0.15, 0.70, 0.10),
    "gloves": (0.25, 1.00, 0.20),
    "boots":  (0.75, 1.10, 0.10),
}
HEAD_ITEMS = {"helmet", "mask", "goggles"}
# v3 evidence rule: an item is only 'missing' when the body part it belongs on is SEEN without it.
# SH17 'hand' = a bare hand (gloved hands are labelled 'gloves'); 'face' = a visible face.
EVIDENCE = {"gloves": "hand", "goggles": "face", "boots": "foot"}   # boots: foot seen, not a safety shoe
PART_REGION = {"hand": "gloves", "face": "goggles", "head": "helmet", "foot": "boots"}
FEET_ITEMS = {"boots"}


def region_box(person, item):
    x1, y1, x2, y2 = person
    w, h = x2 - x1, y2 - y1
    top, bottom, xe = REGIONS[item]
    return (x1 - xe * w, y1 + top * h, x2 + xe * w, y1 + bottom * h)


def _inside(pt, box):
    return box[0] <= pt[0] <= box[2] and box[1] <= pt[1] <= box[3]


# COCO keypoints: 0 nose 1-2 eyes 3-4 ears 5-6 shoulders 9-10 wrists 11-12 hips 15-16 ankles
KP = {"helmet": (0, 1, 2, 3, 4), "head": (0, 1, 2, 3, 4), "goggles": (1, 2), "face": (0, 1, 2), "mask": (0,),
      "vest": (5, 6, 11, 12), "gloves": (9, 10), "hand": (9, 10), "boots": (15, 16), "foot": (15, 16)}
# how far (as a fraction of the person's height) an item may be from its body point
KP_RADIUS = {"helmet": 0.22, "head": 0.22, "goggles": 0.12, "face": 0.15, "mask": 0.12, "vest": 0.35,
             "gloves": 0.14, "hand": 0.14, "boots": 0.14, "foot": 0.14}
KP_SEEN = 0.40
# for the 'hidden' test: a helmet seen from behind has no visible face points, but the shoulders show the
# head is in view - so shoulders count as evidence for head items.
KP_VISIBLE = {**KP, "helmet": (0, 1, 2, 3, 4, 5, 6), "head": (0, 1, 2, 3, 4, 5, 6)}


def _kp_points(p, cls):
    k = p.get("kps")
    if not k or cls not in KP:
        return None
    pts = [(k[i][0], k[i][1]) for i in KP[cls] if i < len(k) and k[i][2] >= KP_SEEN]
    if cls == "vest" and pts:                      # torso centre, not a shoulder
        pts = [(sum(x for x, _ in pts) / len(pts), sum(y for _, y in pts) / len(pts))]
    return pts


def part_hidden(p, item):
    """With pose keypoints: True when every body point an item belongs on is invisible (behind another
    worker, out of frame). Then the item can't be judged -> 'unverifiable', never 'missing'."""
    k = p.get("kps")
    if not k or item not in KP_VISIBLE:
        return False
    return not any(i < len(k) and k[i][2] >= KP_SEEN for i in KP_VISIBLE[item])


def _owner(pt, persons, region_of):
    # 1) pose keypoints: nearest matching body point wins (robust when two workers overlap / stand in a line)
    best, best_d = None, None
    for p in persons:
        pts = _kp_points(p, region_of)
        if not pts:
            continue
        hgt = max(1.0, p["bbox"][3] - p["bbox"][1])
        d = min(((pt[0] - x) ** 2 + (pt[1] - y) ** 2) ** 0.5 for x, y in pts) / hgt
        if d <= KP_RADIUS.get(region_of, 0.2) and (best_d is None or d < best_d):
            best, best_d = p, d
    if best is not None:
        return best
    return _owner_box(pt, persons, region_of)


def _owner_box(pt, persons, region_of):
    """Which person does a box centred at pt belong to? Among the people whose body region contains
    it: prefer a person whose OWN box contains it, then the one whose centre line is nearest, then the
    smaller (closer to camera-agnostic) box. Fixes items jumping to the wrong worker when two people
    overlap or stand in a line."""
    best, best_key = None, None
    for p in persons:
        if not _inside(pt, region_box(p["bbox"], region_of)):
            continue
        x1, y1, x2, y2 = p["bbox"]
        key = (0 if _inside(pt, p["bbox"]) else 1,
               round(abs(pt[0] - (x1 + x2) / 2) / max(1.0, x2 - x1), 2),
               area(p["bbox"]))
        if best_key is None or key < best_key:
            best, best_key = p, key
    return best


def assign_items(persons, items, frame=None, vcfg=None):
    """Give each PPE item box to the single best-matching person.

    persons: list of dicts with 'tid', 'bbox'
    items:   list of dicts with 'cls', 'conf', 'bbox'
    returns: {tid: {item_cls: best_conf}}
    """
    found = {p["tid"]: {} for p in persons}
    rejected = []
    for it in items:
        cls = it["cls"]
        if cls not in REGIONS:
            continue
        best_person = _owner(center(it["bbox"]), persons, cls)
        if best_person is None:
            continue
        best = best_person["tid"]
        if frame is not None and vcfg:
            ok, why = item_ok(frame, best_person["bbox"], it, vcfg)
            if not ok:
                rejected.append((cls, it["bbox"], why))
                continue
        found[best][cls] = max(found[best].get(cls, 0.0), it["conf"])
    return found, rejected


def _occluded(person, item, others, limit):
    """Region hidden by another person standing in front (larger y2 = closer)."""
    reg = region_box(person["bbox"], item)
    reg_area = area(reg)
    if reg_area <= 0:
        return False
    for o in others:
        if o["tid"] == person["tid"] or o["bbox"][3] <= person["bbox"][3]:
            continue
        if intersection(reg, o["bbox"]) / reg_area > limit:
            return True
    return False


def assign_parts(persons, parts):
    """Body parts (v3 model) -> {tid: {part: best_conf}}, same nearest-person rule as items."""
    seen = {p["tid"]: {} for p in persons}
    for d in parts or ():
        reg = PART_REGION.get(d["cls"])
        if reg is None:
            continue
        owner = _owner(center(d["bbox"]), persons, reg)
        if owner is not None:
            seen[owner["tid"]][d["cls"]] = max(seen[owner["tid"]].get(d["cls"], 0.0), d["conf"])
    return seen


def evaluate(persons, items, required, frame_w, frame_h, cfg, frame=None, vcfg=None, parts=None):
    """Return {tid: {item: (status, conf)}} for the required items of each person.

    required: {tid: [items required in that person's zone]}
    parts: body parts from the v3 model, or None with the v2 model only. With parts, gloves and
           goggles are 'missing' only if a bare hand / a face is seen on that person; otherwise
           they are 'unverifiable' (hands in pockets, face turned away).
    """
    found, rejected = assign_items(persons, items, frame, vcfg)
    seen = assign_parts(persons, parts) if parts is not None else None
    min_h = cfg["min_person_height_px"]
    min_conf = cfg["min_person_conf_for_missing"]
    margin = cfg["edge_margin_px"]
    occl = cfg["occlusion_overlap"]
    out = {}
    for p in persons:
        x1, y1, x2, y2 = p["bbox"]
        small = (y2 - y1) < min_h
        weak = p.get("track_conf", p["conf"]) < min_conf
        top_cut = y1 <= margin
        # feet: the person box of a close-up / seated upper body often ends a few px ABOVE the
        # frame edge, so also require a full-body shape before boots can be called 'missing'
        bottom_cut = (y2 >= frame_h * (1 - cfg.get("feet_margin_frac", 0.03))
                      or (y2 - y1) < cfg.get("full_body_min_aspect", 1.5) * (x2 - x1))
        res = {}
        for item in required.get(p["tid"], []):
            if item in found[p["tid"]]:
                res[item] = ("compliant", found[p["tid"]][item])
            elif part_hidden(p, item):
                res[item] = ("unverifiable", p["conf"])        # body point hidden (behind someone, cut off)
            elif seen is not None and item in EVIDENCE and EVIDENCE[item] not in seen[p["tid"]]:
                res[item] = ("unverifiable", p["conf"])        # body part not seen: cannot judge
            elif (small or weak
                  or (item in HEAD_ITEMS and top_cut)
                  or (item in FEET_ITEMS and bottom_cut)
                  or _occluded(p, item, persons, occl)):
                res[item] = ("unverifiable", p["conf"])
            else:
                res[item] = ("missing", p["conf"])
        out[p["tid"]] = res
    return out, rejected
