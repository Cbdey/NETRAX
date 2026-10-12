"""Safety-station simulator: acts like the ESP32 so the hardware page, fire cross-check and RFID attendance
can be tested and demoed with no board connected. Standard library only.

    python -m tools.hw_sim                       # normal readings, 1 per second (Ctrl+C to stop)
    python -m tools.hw_sim --scenario flame      # flame sensor on from second 5 to 20
    python -m tools.hw_sim --scenario smoke      # MQ-2 + PM2.5 climb (smoke) after warm-up
    python -m tools.hw_sim --scenario dust       # PM2.5 35-80 -> orange WARNING only
    python -m tools.hw_sim --scenario offline    # sends 10 s, then goes silent (station OFFLINE)
    python -m tools.hw_sim --rfid A1B2C3D4       # one card scan (run twice = enter, then exit)
    python -m tools.hw_sim --camera-fire         # also posts a CAMERA fire event for the station's zone
                                                 # -> with 'normal' it is NOT alarmed (station clear),
                                                 #    with 'flame' it is CONFIRMED (camera + sensors)
Options: --server http://127.0.0.1:8000  --device esp32-ppe-01  --seconds 60

The camera event is labelled simulated (camera_id sim_cam); it is only for testing the cross-check.
"""
import argparse
import json
import math
import random
import sys
import time
import urllib.error
import urllib.request
import uuid


def post(server, path, body, timeout=3):
    req = urllib.request.Request(server.rstrip("/") + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode(errors="ignore")[:200]}
    except (urllib.error.URLError, OSError) as e:
        return 0, {"error": str(e)}


def reading(t, scenario, uptime0):
    """One telemetry frame at second t of the scenario. Values look like the real sensors (raw ADC)."""
    n = lambda s: random.gauss(0, s)
    up = uptime0 + t
    d = {"temperature_c": round(29.5 + 0.3 * math.sin(t / 40) + n(0.05), 1), "humidity_pct": round(61 + n(0.4), 1),
         "thermistor_adc": int(1830 + n(4)), "mq2_adc": int(620 + n(12)), "mq135_adc": int(1180 + n(15)),
         "flame_detected": False, "pm1_0_ugm3": max(0, int(8 + n(1))), "pm2_5_ugm3": max(0, int(14 + n(1.5))),
         "pm10_ugm3": max(0, int(20 + n(2))), "helmet_fsr_adc": int(910 + n(20)), "shoe_fsr_adc": int(760 + n(20)),
         "touch_kit": True, "touch_helmet": True, "touch_shoe": False, "mpu6050_available": True,
         "vibration_metric": round(abs(0.02 + n(0.006)), 3), "uptime_s": up, "rssi": -58 + int(n(2)), "fw": "sim-1.0"}
    if scenario == "flame" and 5 <= t <= 20:
        d["flame_detected"] = True
        d["temperature_c"] = round(d["temperature_c"] + min(8, (t - 5) * 0.6), 1)
    if scenario == "smoke" and t >= 5:
        k = min(1.0, (t - 5) / 12)
        d["mq2_adc"] = int(620 + 1500 * k + n(20))
        d["mq135_adc"] = int(1180 + 700 * k + n(20))
        d["pm2_5_ugm3"] = int(14 + 220 * k)
        d["pm10_ugm3"] = int(20 + 260 * k)
    if scenario == "dust" and t >= 5:
        d["pm2_5_ugm3"] = int(40 + 30 * abs(math.sin(t / 9)))
        d["pm10_ugm3"] = d["pm2_5_ugm3"] + 25
    return d


def camera_fire(server, zone, kind="fire"):
    now = time.time()
    body = {"event_id": f"sim-{kind}-{uuid.uuid4().hex[:8]}", "type": kind, "severity": "Critical",
            "camera_id": "sim_cam", "camera_name": "Simulated camera", "zone_id": zone, "zone_name": zone,
            "confidence": 0.71, "captured_at": now, "started_at": now, "onset_at": now - 1.5, "items_missing": []}
    code, r = post(server, "/api/events", body, timeout=20)     # backend waits up to verify_window_s
    print(f"[camera] {kind} event -> HTTP {code}  status={r.get('status')}  verification={r.get('verification')}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    ap.add_argument("--device", default="esp32-ppe-01")
    ap.add_argument("--scenario", default="normal", choices=["normal", "flame", "smoke", "dust", "offline"])
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--rfid", help="send one card scan with this UID and exit")
    ap.add_argument("--camera-fire", action="store_true", help="post a simulated camera fire event at second 8")
    ap.add_argument("--zone", default="welding_bay", help="zone for --camera-fire")
    ap.add_argument("--uptime", type=int, default=600, help="simulated uptime at start (>60 = MQ warmed up)")
    a = ap.parse_args()

    if a.rfid:
        code, r = post(a.server, "/api/hw/rfid", {"uid": a.rfid, "device_id": a.device})
        print(f"HTTP {code}: {r.get('status')} {r.get('event') or ''} - {r.get('message') or r.get('error') or ''}")
        return 0 if code == 200 else 1

    print(f"Simulating {a.device} -> {a.server}  scenario={a.scenario}  (Ctrl+C to stop)")
    t0 = time.time()
    for t in range(a.seconds):
        if a.scenario == "offline" and t >= 10:
            if t == 10:
                print("[sim] going silent - the dashboard should show the station OFFLINE after ~6 s")
            time.sleep(1)
            continue
        if a.camera_fire and t == 8:
            import threading
            threading.Thread(target=camera_fire, args=(a.server, a.zone), daemon=True).start()
        d = reading(t, a.scenario, a.uptime)
        code, r = post(a.server, "/api/hw/telemetry", dict(d, device_id=a.device))
        if code != 200:
            print(f"[{t:3d}s] HTTP {code} {r.get('error', '')}")
        else:
            print(f"[{t:3d}s] T={d['temperature_c']} MQ2={d['mq2_adc']} PM2.5={d['pm2_5_ugm3']} "
                  f"flame={int(d['flame_detected'])} -> {r.get('level')} led={r.get('led')} relay={r.get('relay')} "
                  f"{'; '.join(r.get('reasons') or [])}")
        time.sleep(max(0, t0 + t + 1 - time.time()))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nstopped")
