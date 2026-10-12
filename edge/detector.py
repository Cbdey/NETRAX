"""Loads the models and runs inference on one frame.

Person detection uses a COCO-pretrained YOLOv8 (trained on ~64k images of people in
every kind of scene), because the PPE dataset's 'person' class only saw construction
sites and misses people indoors / close to the camera. PPE items and fire/smoke come
from our own fine-tuned models."""
import os

from ultralytics import YOLO

from .config import path

BODY_PARTS = {"head", "face", "hand", "foot"}   # v3 model: evidence that a body part is visible, not PPE


def pick_device(pref):
    import torch
    if pref == "cpu":
        return "cpu"
    if torch.cuda.is_available():
        return 0                 # FP32 on purpose: GTX 16xx cards can misbehave in FP16
    if pref == "cuda":
        print("[detector] CUDA requested but not available - using CPU")
    return "cpu"


class Detector:
    def __init__(self, s):
        m = s["models"]
        self.imgsz = m["imgsz"]
        self.device = pick_device(m.get("device", "auto"))
        self.ppe = YOLO(path(m["ppe"]))
        fire_path = self._pick(m["fire"], m.get("fire_fallback"))
        self.fire = YOLO(fire_path)
        print(f"[detector] PPE main {m['ppe']} | fire {os.path.basename(fire_path)}")
        # v3 hybrid: an auxiliary model (ppe_v3*) supplies the classes in aux_items (gloves, goggles and the
        # body parts used as evidence); the main model keeps everything else (helmet, vest, mask).
        self.aux, self.aux_items = None, set(m.get("aux_items") or [])
        aux = m.get("ppe_aux")
        if aux and os.path.exists(path(aux)):
            self.aux = YOLO(path(aux))
            print(f"[detector] aux PPE model {aux}: {sorted(self.aux_items)}")
        elif aux:
            print(f"[detector] aux PPE model {aux} not found - running main PPE model only")
        main_names = set(self.ppe.names.values())
        aux_names = set(self.aux.names.values()) if self.aux else set()
        self.supported = (main_names - (self.aux_items if self.aux else set())) | (aux_names & self.aux_items)
        self.has_parts = bool(self.aux) and bool(aux_names & BODY_PARTS & self.aux_items)
        # Safety shoes: the v3 model finds every foot, our own classifier (trained on the team's shoe
        # videos, tools/shoes.py) decides safety shoe vs normal footwear on the ankle crop.
        self.shoe, self.shoe_min = None, float(m.get("shoe_min_prob", 0.6))
        sc = m.get("shoe_cls")
        if sc and os.path.exists(path(sc)) and self.has_parts:
            self.shoe = YOLO(path(sc))
            print(f"[detector] safety-shoe classifier {sc}: {list(self.shoe.names.values())}")
        self.supported.discard("boots")                  # public 'boots' class misses real shoes
        if self.shoe is not None:
            self.supported.add("boots")
        pm = m.get("person", "coco")
        self.pose = False
        if pm == "ppe":
            self.person = None
        else:
            self.person = None
            if pm == "pose":     # persons + 17 body keypoints in one pass (COCO-pretrained YOLOv8n-pose)
                try:
                    local = path("models", "yolov8n-pose.pt")
                    self.person = YOLO(local if os.path.exists(local) else "yolov8n-pose.pt")
                    self.pose = True
                except Exception as e:
                    print(f"[detector] pose model unavailable ({e!r}) - using the COCO person detector")
            if self.person is None:
                local = path("models", "yolov8n.pt")
                self.person = YOLO(local if os.path.exists(local) else "yolov8n.pt")  # auto-download once
        self.ppe_conf = s["thresholds"]["ppe_conf"]
        self.default_conf = self.ppe_conf.get("default", 0.45)
        self.last_parts = []      # body parts (head/face/hand/foot) from the v3 model, [] for v2
        self.fire_conf = s["thresholds"]["fire_conf"]
        self.min_ppe = min(v for k, v in self.ppe_conf.items() if k not in ("person", "default"))
        self.min_fire = min(self.fire_conf.values())
        self._warmup()

    def _warmup(self):
        """First CUDA inference takes ~1 s (kernel load); do it now, not on the first real frame."""
        import numpy as np
        blank = np.zeros((self.imgsz, self.imgsz, 3), np.uint8)
        for m in (self.ppe, self.fire, self.person, self.aux):
            if m is not None:
                m.predict(blank, imgsz=self.imgsz, device=self.device, verbose=False)

    @staticmethod
    def _pick(p, fallback):
        if os.path.exists(path(p)) or not fallback:
            return path(p)
        print(f"[detector] {p} not found - using {fallback}")
        return path(fallback)

    def _run(self, model, frame, conf, classes=None):
        r = model.predict(frame, imgsz=self.imgsz, conf=conf, device=self.device,
                          classes=classes, verbose=False)[0]
        out = []
        if r.boxes is None:
            return out
        for b, c, k in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist(), r.boxes.cls.tolist()):
            out.append({"cls": model.names[int(k)], "conf": float(c), "bbox": b})
        return out

    def _run_pose(self, frame, conf):
        r = self.person.predict(frame, imgsz=self.imgsz, conf=conf, device=self.device, classes=[0], verbose=False)[0]
        out = []
        if r.boxes is None:
            return out
        kps = r.keypoints.data.tolist() if r.keypoints is not None else [None] * len(r.boxes)
        for b, c, k in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist(), kps):
            out.append({"cls": "person", "conf": float(c), "bbox": b, "kps": k})   # k: 17 x [x, y, conf]
        return out

    def ppe_detect(self, frame):
        """Returns (persons, items); persons without track ids yet."""
        dets = self._run(self.ppe, frame, self.min_ppe if self.person else
                         min(self.ppe_conf.values()))
        if self.aux is not None:
            dets = [d for d in dets if d["cls"] not in self.aux_items]
            dets += [d for d in self._run(self.aux, frame, self.min_ppe) if d["cls"] in self.aux_items]
        kept = [d for d in dets if d["cls"] != "person"
                and d["conf"] >= self.ppe_conf.get(d["cls"], self.default_conf)]
        self.last_parts = [d for d in kept if d["cls"] in BODY_PARTS]
        items = [d for d in kept if d["cls"] not in BODY_PARTS and d["cls"] != "boots"]
        if self.shoe is not None:
            items += self.classify_shoes(frame, [d for d in self.last_parts if d["cls"] == "foot"])
        if self.person is not None and self.pose:
            persons = self._run_pose(frame, self.ppe_conf["person"])
        elif self.person is not None:
            persons = self._run(self.person, frame, self.ppe_conf["person"], classes=[0])
        else:
            persons = [d for d in dets if d["cls"] == "person" and d["conf"] >= self.ppe_conf["person"]]
        for p in persons:
            p["cls"] = "person"
        return persons, items

    def classify_shoes(self, frame, feet):
        """Each detected foot -> 'boots' item if the classifier says safety shoe."""
        out = []
        if not feet:
            return out
        h, w = frame.shape[:2]
        crops, keep = [], []
        for f in feet:
            x1, y1, x2, y2 = f["bbox"]
            mx, my = (x2 - x1) * 0.15, (y2 - y1) * 0.15
            x1, y1 = max(0, int(x1 - mx)), max(0, int(y1 - my))
            x2, y2 = min(w, int(x2 + mx)), min(h, int(y2 + my))
            if x2 - x1 >= 8 and y2 - y1 >= 8:
                crops.append(frame[y1:y2, x1:x2])
                keep.append(f)
        if not crops:
            return out
        res = self.shoe.predict(crops, imgsz=128, device=self.device, verbose=False)
        safe_id = next((k for k, n in self.shoe.names.items() if n == "safety_shoe"), 0)
        for f, r in zip(keep, res):
            p = float(r.probs.data[safe_id])
            f["safety_prob"] = p
            if p >= self.shoe_min:
                out.append({"cls": "boots", "conf": p * f["conf"] ** 0.5, "bbox": f["bbox"]})
        return out

    def fire_detect(self, frame):
        dets = self._run(self.fire, frame, self.min_fire)
        return [d for d in dets if d["conf"] >= self.fire_conf.get(d["cls"], 1.0)]
