"""Find phones running the IP Webcam app on this laptop's Wi-Fi and put them into config/cameras.yaml.

    python -m tools.find_phones                          # list phones (+ one picture each in data/phones/)
    python -m tools.find_phones --write                  # cam_02 = phone 1 (Warehouse), cam_03 = phone 2 (Assembly)
    python -m tools.find_phones --cam02 172.20.163.40 --cam03 172.20.163.77   # set by hand

A phone counts only if http://<ip>:8080/shot.jpg returns a JPEG (other devices on port 8080, e.g. a router,
are ignored). With --write, a phone already set on cam_02 or cam_03 keeps its camera.
Restart run_all.bat afterwards so the edge picks up the new addresses."""
import argparse
import concurrent.futures as cf
import os
import re
import socket

import requests

from edge.config import path

CAMS = {"cam_02": "Warehouse (phone 1)", "cam_03": "Assembly line (phone 2)"}


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))            # no packet is sent; picks the Wi-Fi interface's address
        return s.getsockname()[0]
    finally:
        s.close()


def is_phone(ip, port=8080):
    try:
        with socket.create_connection((ip, port), timeout=0.6):
            pass
        r = requests.get(f"http://{ip}:{port}/shot.jpg", timeout=3)
        if r.ok and r.headers.get("Content-Type", "").startswith("image/jpeg"):
            return ip, r.content
    except (OSError, requests.RequestException):
        pass
    return None


def scan(port=8080):
    me = local_ip()
    base = me.rsplit(".", 1)[0]
    with cf.ThreadPoolExecutor(128) as ex:
        hits = [h for h in ex.map(lambda i: is_phone(f"{base}.{i}", port), range(1, 255)) if h]
    return me, hits


def current_sources():
    txt = open(path("config", "cameras.yaml"), encoding="utf-8").read()
    out = {}
    for cam in CAMS:
        m = re.search(rf"^  {cam}:\n(?:    .*\n)*?    source:\s*(\S+)", txt, re.M)
        out[cam] = m.group(1) if m else None
    return txt, out


def set_source(txt, cam, ip):
    return re.sub(rf"(^  {cam}:\n(?:    .*\n)*?    source:\s*)\S+", rf"\g<1>http://{ip}:8080/video", txt,
                  count=1, flags=re.M)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--cam02")
    ap.add_argument("--cam03")
    a = ap.parse_args()
    txt, cur = current_sources()
    if a.cam02 or a.cam03:
        for cam, ip in (("cam_02", a.cam02), ("cam_03", a.cam03)):
            if ip:
                txt = set_source(txt, cam, ip)
                print(f"{cam} {CAMS[cam]} -> http://{ip}:8080/video")
        open(path("config", "cameras.yaml"), "w", encoding="utf-8").write(txt)
        print("saved config/cameras.yaml - restart run_all.bat")
        return
    me, hits = scan()
    print(f"laptop {me} - scanned {me.rsplit('.', 1)[0]}.1-254 for IP Webcam (port 8080)")
    os.makedirs(path("data", "phones"), exist_ok=True)
    for ip, jpg in hits:
        p = path("data", "phones", f"{ip}.jpg")
        open(p, "wb").write(jpg)
        print(f"  phone found: {ip}   (picture: data/phones/{ip}.jpg)")
    if not hits:
        print("  no phone found - on each phone: same Wi-Fi as the laptop, open IP Webcam, tap 'Start server'")
        return
    if not a.write:
        print("run with --write to put them into cameras.yaml (or --cam02 IP --cam03 IP to choose)")
        return
    found = [ip for ip, _ in hits]
    assign = {}
    for cam, src in cur.items():                         # keep a phone on the camera it already has
        ip = re.sub(r"^http://([\d.]+):.*$", r"\1", src or "")
        if ip in found:
            assign[cam] = ip
            found.remove(ip)
    for cam in CAMS:
        if cam not in assign and found:
            assign[cam] = found.pop(0)
    for cam, ip in assign.items():
        txt = set_source(txt, cam, ip)
        print(f"{cam} {CAMS[cam]} -> http://{ip}:8080/video")
    for cam in CAMS:
        if cam not in assign:
            print(f"{cam} {CAMS[cam]} -> no phone left for it (start IP Webcam on the second phone)")
    open(path("config", "cameras.yaml"), "w", encoding="utf-8").write(txt)
    print("saved config/cameras.yaml - restart run_all.bat")


if __name__ == "__main__":
    main()
