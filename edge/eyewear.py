"""Safety goggles vs normal glasses (12 Nov, jury: "normal spectacles are accepted as industrial goggles").

The v3 PPE model finds 'goggles' but cannot tell safety eyewear from spectacles / sunglasses: measured with celebrity
eyeglass photos pasted at CCTV size into our own room frames, it called 59 different spectacles / sunglasses faces
'goggles' at the live threshold (150 detections). Second stage, like the safety-shoe classifier:

  goggles box -> square crop of the eye region (eyes, brows, temples) -> CLIP ViT-B-32 image embedding
  -> small logistic head (models/eyewear_head.npz) trained on our own goggles frames + spectacles / sunglasses / bare
     faces (tools/eyewear.py) -> p = probability of SAFETY eyewear.

Three answers (settings models.eyewear_lo / eyewear_hi):
  p >= hi        goggles          -> item kept (compliant)
  lo <= p < hi   unclear          -> item dropped, goggles 'unverifiable' (never an alarm, never 'PPE complete')
  p < lo         normal glasses   -> item dropped; face seen -> goggles 'missing' (alarm)
Measured (whole scenes / identities held out): real safety goggles called 'normal glasses' 0 of 151; spectacles that the
detector called goggles -> 'normal glasses' 150 of 150. Runs in the AI process on the GPU: ~12 ms per crop (GTX 1650).
"""
import os

import cv2
import numpy as np

from .config import path


def eye_crop(frame, b, scale=2.2):
    """Square crop centred on the goggles box (the SAME geometry the head was trained on)."""
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    s = max(b[2] - b[0], (b[3] - b[1]) * 1.5) * scale / 2
    x1, y1, x2, y2 = int(cx - s), int(cy - s * 0.9), int(cx + s), int(cy + s * 1.1)
    h, w = frame.shape[:2]
    p = max(0, -x1, -y1, x2 - w, y2 - h)
    if p:
        frame = cv2.copyMakeBorder(frame, p, p, p, p, cv2.BORDER_REPLICATE)
    return frame[y1 + p:y2 + p, x1 + p:x2 + p]


class EyewearCheck:
    def __init__(self, m, device):
        import torch
        import open_clip
        self.torch = torch
        self.dev = "cuda" if device == 0 else "cpu"
        self.lo, self.hi = float(m.get("eyewear_lo", 0.2)), float(m.get("eyewear_hi", 0.5))
        self.max_crops = int(m.get("eyewear_max_crops", 6))
        self.model, _, self.prep = open_clip.create_model_and_transforms(
            m.get("eyewear_arch", "ViT-B-32"), pretrained=path(m["eyewear_clip"]), device=self.dev)
        self.model.eval()
        h = np.load(path(m["eyewear_head"]))
        self.w, self.b = h["w"].astype(np.float32), float(h["b"])
        self.T = h["T"].astype(np.float32)                   # zero-shot text directions: goggles, spectacles, sunglasses, none

    def probs(self, crops):
        from PIL import Image
        torch = self.torch
        if not crops:
            return []
        x = torch.stack([self.prep(Image.fromarray(cv2.cvtColor(c, cv2.COLOR_BGR2RGB))) for c in crops]).to(self.dev)
        with torch.no_grad():
            f = torch.nn.functional.normalize(self.model.encode_image(x), dim=-1).float().cpu().numpy()
        z = 100 * f @ self.T.T
        z = np.exp(z - z.max(1, keepdims=True))
        z /= z.sum(1, keepdims=True)
        logit = np.hstack([f, z]) @ self.w * 10 + self.b
        return [float(v) for v in 1 / (1 + np.exp(-logit))]

    def verdict(self, p):
        return "goggles" if p >= self.hi else ("unclear" if p >= self.lo else "normal_glasses")

    def check(self, frames_items):
        """frames_items: [(frame, [goggles item dicts])]. Sets d['eyewear_p'] and d['eyewear'] on each item. The
        highest-confidence max_crops boxes per call are scored; any others are 'unclear' (no alarm on them)."""
        jobs = [(f, d) for f, ds in frames_items for d in ds]
        jobs.sort(key=lambda j: -j[1]["conf"])
        run, rest = jobs[:self.max_crops], jobs[self.max_crops:]
        crops = [eye_crop(f, d["bbox"]) for f, d in run]
        for (f, d), p in zip(run, self.probs(crops)):
            d["eyewear_p"], d["eyewear"] = round(p, 3), self.verdict(p)
        for f, d in rest:
            d["eyewear_p"], d["eyewear"] = None, "unclear"

    def warmup(self):
        self.probs([np.zeros((64, 64, 3), np.uint8)])


def load(m, device):
    """EyewearCheck or None (setting off, files missing, open_clip not installed -> old behaviour, logged)."""
    if not m.get("eyewear_head"):
        return None
    for k in ("eyewear_head", "eyewear_clip"):
        if not m.get(k) or not os.path.exists(path(m[k])):
            print(f"[detector] eyewear check OFF: {m.get(k)} not found")
            return None
    try:
        e = EyewearCheck(m, device)
        print(f"[detector] eyewear check (safety goggles vs normal glasses) {m['eyewear_head']} lo {e.lo} hi {e.hi}")
        return e
    except Exception as ex:                                  # open_clip missing etc. -> run without it
        print(f"[detector] eyewear check OFF: {ex!r}")
        return None
