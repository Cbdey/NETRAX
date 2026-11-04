"""Check / set the IP Webcam phones for smooth multi-camera streaming (4 Nov).

    python -m tools.phone_tune                 show each phone's current video size, quality, fps limit
    python -m tools.phone_tune --apply         set video size 960x540 (or the closest the phone offers) + quality 55

Why: a phone streams MJPEG = one full JPEG per frame. At 1920x1080, quality 80-90, 30 fps that is roughly
20-40 Mbit/s per phone - over a phone hotspot with two phones the network and the laptop's video decoding
are overloaded and the picture freezes. The AI model looks at 640 px anyway, so 960x540 loses NOTHING for
PPE detection but cuts traffic and decoding by about 4x.
The IP Webcam app answers  /status.json?show_avail=1  (current + available values) and  /settings/<key>?set=<v>.
A value is only reported as changed after it is read back from the phone."""
import argparse
import sys

import requests

from edge.config import load_all

WANT_SIZE = (960, 540)
WANT_QUALITY = 55


def base(src):
    if not isinstance(src, str) or not src.startswith("http"):
        return None
    from urllib.parse import urlparse
    u = urlparse(src)
    return f"{u.scheme}://{u.netloc}"


def status(b):
    r = requests.get(f"{b}/status.json", params={"show_avail": 1}, timeout=4)
    r.raise_for_status()
    return r.json()


def _size(v):
    try:
        w, h = [int(x) for x in str(v).lower().split("x")]
        return w, h
    except ValueError:
        return None


def pick_size(avail):
    sizes = [s for s in (_size(v) for v in avail or []) if s]
    if not sizes:
        return None
    # landscape or portrait as the phone lists it; closest pixel count to 960x540, never above 1280x720
    target = WANT_SIZE[0] * WANT_SIZE[1]
    ok = [s for s in sizes if s[0] * s[1] <= 1280 * 720] or sizes
    w, h = min(ok, key=lambda s: abs(s[0] * s[1] - target))
    return f"{w}x{h}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    _, _, cams = load_all()
    phones = [(c, cam) for c, cam in cams.items() if base(cam.get("source"))]
    if not phones:
        print("no phone (http) cameras in config/cameras.yaml")
        return 1
    for cid, cam in phones:
        b = base(cam["source"])
        print(f"\n{cid} ({cam.get('name')}) {b}" + ("" if cam.get("enabled", True) else "  [enabled: false]"))
        try:
            st = status(b)
        except Exception as e:                   # noqa: BLE001
            print(f"  not reachable: {e.__class__.__name__} - is the phone on the same Wi-Fi with IP Webcam running?")
            continue
        cur, av = st.get("curvals") or {}, st.get("avail") or {}
        for k in ("video_size", "quality", "fps_limit", "video_chunk_len", "orientation"):
            if k in cur:
                print(f"  {k:<14} {cur[k]}")
        if not a.apply:
            continue
        changes = {}
        size = pick_size(av.get("video_size"))
        if size and cur.get("video_size") != size:
            changes["video_size"] = size
        if "quality" in cur and str(cur.get("quality")) != str(WANT_QUALITY):
            changes["quality"] = WANT_QUALITY
        for k, v in changes.items():
            try:
                requests.get(f"{b}/settings/{k}", params={"set": v}, timeout=4)
            except requests.RequestException as e:
                print(f"  could not set {k}: {e.__class__.__name__}")
        if changes:
            back = (status(b).get("curvals") or {})
            for k, v in changes.items():
                ok = str(back.get(k)) == str(v)
                print(f"  {k:<14} -> {back.get(k)} {'OK' if ok else '(phone did not accept - set it in the app: Video preferences)'}")
        else:
            print("  already fine")
    print("\nAlso in the IP Webcam app (Video preferences): 'FPS limit' 15, and keep the phone on a charger.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
