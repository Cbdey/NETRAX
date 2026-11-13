"""Test one RTSP / IP camera for 10 s, without the AI (13 Nov, Axis P1447-LE).

    python -m tools.rtsp_test cam_01                       # the camera from config/cameras.yaml
    python -m tools.rtsp_test "rtsp://{axis}@192.168.0.90/axis-media/media.amp?resolution=1280x960"
    python -m tools.rtsp_test cam_01 --seconds 20

Prints: reachable, opened yes/no, resolution, camera fps, average frame age (how old the newest picture is when
NETRAX picks it up, sampled 20x per second), the delay of frames waiting in the stream buffer, and the first error
in plain words. Saves one picture to _to_delete/axis_test.jpg. The login ({axis} -> secrets.local.yaml) is never printed.
Stop run_all.bat first if the camera allows only a few viewers."""
import argparse
import os
import threading
import time

from edge.stream import (StreamReader, cctv_label, fill_source, mask, open_rtsp, reachable,  # noqa: F401 (sets FFmpeg options)
                         resolve_rtsp, rtsp_unreachable_text)
from edge.relay import Relay
import cv2                                                                         # noqa: E402

from edge.config import load_cameras, path                                         # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="camera id (cam_01) or an rtsp:// URL")
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("--out", default=path("_to_delete", "axis_test.jpg"))
    ap.add_argument("--relay", type=int, default=None, help="relay port on another laptop (cameras.yaml relay:, e.g. 8554)")
    a = ap.parse_args()
    src, relay = a.target, a.relay
    if not src.lower().startswith(("rtsp", "http")):
        cams = load_cameras(include_off=True)
        if src not in cams:
            print(f"unknown camera {src} - cameras.yaml has {', '.join(cams)}")
            return 2
        relay = relay or cams[src].get("relay")
        src = cams[src]["source"]
    print(f"source:      {mask(src)}")
    if not isinstance(src, str) or not src.lower().startswith(("rtsp", "http")):
        print("this camera is not a network camera (webcam index or file) - use python -m tools.camdiag --only <cam>")
        return 2
    print(f"transport:   OPENCV_FFMPEG_CAPTURE_OPTIONS = {os.environ.get('OPENCV_FFMPEG_CAPTURE_OPTIONS')}")
    url, err = fill_source(src)
    if err:
        print(f"opened:      no\nfirst error: {err}")
        return 1
    direct = url
    url, err = resolve_rtsp(src, url, Relay(relay) if relay and url.lower().startswith("rtsp") else None)
    if err:
        print(f"reachable:   no\nopened:      no\nfirst error: {err}")
        return 1
    print("reachable:   yes, " + ("directly" if url == direct else
                                   f"through the relay {mask(url).split('@')[-1].split('/')[0]} (camera on another laptop)"))
    t0 = time.time()
    cap = open_rtsp(url) if url.lower().startswith("rtsp") else cv2.VideoCapture(url)
    t_open = time.time() - t0
    if not cap or not cap.isOpened():
        print(f"opened:      no (after {t_open:.1f} s)")
        print(f"first error: {cctv_label(src)} answered on the network but the video did not open. Most likely the user / "
              "password in secrets.local.yaml is wrong (401), or the URL path / resolution is not accepted by the camera "
              "(try resolution=1280x960, then 1920x1080&fps=10).")
        return 1
    print(f"opened:      yes ({t_open:.1f} s)")
    st = {"frame": None, "ts": 0.0, "n": 0, "fail": 0, "first": None, "lag": [], "base": None, "run": True}

    def reader():                                  # same as the edge: read continuously, keep only the newest frame
        while st["run"]:
            okr, f = cap.read()
            now = time.time()
            if not okr:
                st["fail"] += 1
                if st["fail"] > 3:
                    break
                continue
            pts = cap.get(cv2.CAP_PROP_POS_MSEC)
            if pts and pts > 0:
                d = now * 1000 - pts
                st["base"] = d if st["base"] is None else min(st["base"], d)
                st["lag"].append(d - st["base"])
            st["frame"], st["ts"] = f, now
            st["n"] += 1
            st["first"] = st["first"] or now

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    ages, end = [], time.time() + a.seconds
    while time.time() < end and th.is_alive():
        if st["ts"]:
            ages.append((time.time() - st["ts"]) * 1000)
        time.sleep(0.05)
    st["run"] = False
    th.join(6)
    f = st["frame"]
    if f is None:
        print("frames:      0\nfirst error: the stream opened but sent no picture (camera busy / codec not supported - "
              "try videocodec=h264 in the URL)")
        cap.release()
        return 1
    dur = max(0.1, st["ts"] - st["first"])
    fps = (st["n"] - 1) / dur if st["n"] > 1 else 0.0
    lag = sorted(st["lag"])
    print(f"resolution:  {f.shape[1]}x{f.shape[0]}")
    print(f"camera fps:  {fps:.1f}  ({st['n']} frames in {dur:.1f} s)")
    print(f"frame age:   average {sum(ages) / len(ages):.0f} ms, max {max(ages):.0f} ms (time since NETRAX received the newest picture)")
    if lag:
        print(f"buffer wait: p50 {lag[len(lag) // 2]:.0f} ms, max {lag[-1]:.0f} ms (frames waiting in the stream, "
              "above the camera's own delay)")
    if st["fail"]:
        print(f"read errors: {st['fail']}")
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    cv2.imwrite(a.out, f)
    print(f"picture:     {os.path.relpath(a.out, path())}")
    cap.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
