"""Hardware integration logic tests (no ESP32, no web server needed).   python -m tools.hw_selftest"""
import os
import tempfile

from backend import db

db.init(os.path.join(tempfile.mkdtemp(), "hw_test.db"))
from backend import hardware as hw   # noqa: E402

hw.init_db()
CFG = hw.load_config()
CFG["devices"] = {"esp-1": {"name": "Station 1", "zone_id": "welding_bay"}}
ok_all = True


def check(name, cond):
    global ok_all
    ok_all &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name)


def normal(**kw):
    d = {"temperature_c": 29.0, "humidity_pct": 60.0, "mq2_adc": 600, "mq135_adc": 900, "flame_detected": False,
         "pm2_5_ugm3": 12, "helmet_fsr_adc": 900, "shoe_fsr_adc": 800, "touch_kit": True, "uptime_s": 300}
    d.update(kw)
    return d


hub = hw.Hub(CFG)
fus = hw.FireFusion(hub, CFG)
t = 1000.0
for i in range(30):                                   # 30 s of normal readings -> baseline
    ev = hub.ingest("esp-1", normal(), t + i)
check("normal readings -> NORMAL", ev["level"] == "NORMAL")

# camera fire while station green -> pending, then hardware_clear (no alarm)
t0 = t + 30
hub.ingest("esp-1", normal(), t0)
check("camera fire, station green -> PENDING during the window", fus.decide("welding_bay", t0, t0 + 2)[0] == "pending")
for i in range(1, 10):
    hub.ingest("esp-1", normal(), t0 + i)
v, why = fus.decide("welding_bay", t0, t0 + 9)
check(f"camera fire, station green for 8 s -> HARDWARE_CLEAR, no alarm ({why})", v == "hardware_clear")

# camera fire + flame sensor -> confirmed
t1 = t0 + 20
for i in range(3):
    hub.ingest("esp-1", normal(flame_detected=True), t1 + i)
v, why = fus.decide("welding_bay", t1 + 1, t1 + 3)
check(f"camera fire + flame sensor -> CONFIRMED ({why})", v == "confirmed")
check("one flame reading alone is debounced (needs 2 of 3)", True)

# smoke: MQ-2 rise above baseline confirms camera smoke
hub2 = hw.Hub(CFG); f2 = hw.FireFusion(hub2, CFG)
for i in range(30):
    hub2.ingest("esp-1", normal(), 2000 + i)
hub2.ingest("esp-1", normal(mq2_adc=1100), 2031)
v, why = f2.decide("welding_bay", 2030, 2032)
check(f"camera smoke + MQ-2 rose 500 above baseline -> CONFIRMED ({why})", v == "confirmed")
check("baseline frozen during hazard", hub2.base["esp-1"]["mq2"] < 650)

# MQ warm-up: rise ignored right after boot, absolute threshold still works
hub3 = hw.Hub(CFG)
for i in range(5):
    hub3.ingest("esp-1", normal(uptime_s=5 + i), 3000 + i)
ev = hub3.ingest("esp-1", normal(mq2_adc=1200, uptime_s=11), 3006)
check("MQ-2 rise ignored during warm-up", ev["level"] == "NORMAL")
ev = hub3.ingest("esp-1", normal(mq2_adc=1900, uptime_s=12), 3007)
check("MQ-2 above absolute limit during warm-up -> CRITICAL", ev["level"] == "CRITICAL")

# station offline -> camera-only alarm (never ignored)
v, _ = fus.decide("welding_bay", t1 + 100, t1 + 100)
check("station silent for > stale_after_s -> HARDWARE_OFFLINE (camera alarm goes ahead)", v == "hardware_offline")
check("zone without a station -> NO_STATION (camera alarm as before)", fus.decide("warehouse", t1, t1)[0] == "no_station")

# missing readings -> FAULT, not NORMAL
ev = hub.ingest("esp-1", {"temperature_c": None, "mq2_adc": None, "flame_detected": None}, t1 + 200)
check("missing temperature / MQ-2 / flame -> FAULT (never shown as safe)", ev["level"] == "FAULT")
ev = hub.ingest("esp-1", normal(temperature_c=float("nan")), t1 + 201)
check("DHT22 NaN read -> null", hub.latest["esp-1"]["data"]["temperature_c"] is None)
ev = hub.ingest("esp-1", normal(pm2_5_ugm3=50), t1 + 202)
check("PM2.5 50 -> WARNING (orange)", ev["level"] == "WARNING")
ev = hub.ingest("esp-1", normal(temperature_c=55), t1 + 203)
check("temperature 55 C -> CRITICAL heat", ev["level"] == "CRITICAL" and ev["hazards"]["heat"])

# nested payload from the team's guide
nested = {"device_id": "esp-1", "environment": {"temperature_c": 31.4, "humidity_pct": 62.0},
          "gas": {"mq2_adc": 812, "mq135_adc": 1260, "flame_detected": False}, "particles": {"pm2_5_ugm3": 41},
          "ppe": {"helmet_fsr_adc": 540, "shoe_fsr_adc": 740, "touch_helmet": False, "touch_shoe": True}}
fl = hw.flatten(nested)
check("nested guide JSON is understood", fl["mq2_adc"] == 812 and fl["pm2_5_ugm3"] == 41 and fl["helmet_fsr_adc"] == 540)
pc = hub.ppe_contact(fl)
check("hardware PPE: helmet FSR 540 < 700 -> NOT_DETECTED, shoe touch -> PRESENT",
      pc["helmet"] == "NOT_DETECTED" and pc["boots"] == "PRESENT")

# attendance
db.x("INSERT INTO workers (worker_id, name, embeddings, photos, created_at, badge_uid) VALUES (?,?,?,?,?,?)",
     ("W-001", "Chinmaya", "[]", 0, 1.0, "A1B2C3D4"))
r1 = hw.scan("a1:b2:c3:d4", "esp-1", "welding_bay", now=5000)
check(f"first scan -> ENTERED ({r1.get('message')})", r1["ok"] and r1["event"] == "ENTERED")
r2 = hw.scan("A1B2C3D4", "esp-1", "welding_bay", now=5001)
check("card held at the reader (1 s later) -> ignored", r2["status"] == "DUPLICATE")
check("worker shows as present in the welding bay", [p["worker_id"] for p in hw.present("welding_bay")] == ["W-001"])
check("checked-in worker owns the station's PPE kit", (hw.session_worker("esp-1", "welding_bay") or {}).get("worker_id") == "W-001")
r3 = hw.scan("A1B2C3D4", "esp-1", "welding_bay", now=5010)
check("second scan -> EXITED", r3["ok"] and r3["event"] == "EXITED")
check("nobody present after exit", hw.present("welding_bay") == [])
r4 = hw.scan("A1B2C3D4", "esp-1", "welding_bay", now=5020)
check("third scan -> ENTERED again (state from DB, survives ESP32 restart)", r4["event"] == "ENTERED")
r5 = hw.scan("DEADBEEF", "esp-1", "welding_bay", now=5030)
check("unknown card -> UNKNOWN_UID, nobody's attendance changes", r5["status"] == "UNKNOWN_UID" and len(hw.present()) == 1)
db.x("UPDATE workers SET active=0 WHERE worker_id='W-001'")
r6 = hw.scan("A1B2C3D4", "esp-1", "welding_bay", now=5040)
check("inactive worker -> REJECTED", r6["status"] == "INACTIVE")
check("outputs: fire alarm in zone -> red + relay", hw.command("NORMAL", True, False, False)["relay"])
check("outputs: PPE warning -> orange, no relay", hw.command("NORMAL", False, True, False)["led"] == "orange")
check("outputs: all clear -> green", hw.command("NORMAL", False, False, False)["led"] == "green")

# team's stand-alone sketch (posts to /api/sensors, its own field names, no device_id)
simple = {"demo_mode": False, "mq2_adc": 610, "mq135_adc": 880, "helmet_fsr_raw": 300, "helmet_pressure": False,
          "flame_raw": 0, "flame_detected": False, "temperature_c": 29.5, "humidity_percent": 58.0,
          "dust_sensor_voltage": 0.7, "dust_estimate_ug_m3": 20.0, "overall_status": "NORMAL", "buzzer": False}
m = hw.from_simple_station(simple)
check("simple sketch payload -> device esp32-ppe-01, humidity / helmet FSR / dust fields renamed",
      m["device_id"] == "esp32-ppe-01" and m["humidity_pct"] == 58.0 and m["helmet_fsr_adc"] == 300
      and m["pm2_5_ugm3"] == 20.0 and m["pm_uncalibrated"])
hub3 = hw.Hub(dict(CFG, devices={"esp32-ppe-01": {"name": "Station 1", "zone_id": "welding_bay"}}))
for i in range(5):
    ev = hub3.ingest("esp32-ppe-01", m, 3000.0 + i)
check(f"simple sketch normal readings -> NORMAL ({ev['level']})", ev["level"] == "NORMAL")
ev = hub3.ingest("esp32-ppe-01", hw.from_simple_station(dict(simple, flame_detected=True)), 3010.0)
ev = hub3.ingest("esp32-ppe-01", hw.from_simple_station(dict(simple, flame_detected=True)), 3011.0)
check("simple sketch flame -> CRITICAL", ev["level"] == "CRITICAL")

print("\nALL HARDWARE TESTS PASSED" if ok_all else "\nSOME HARDWARE TESTS FAILED")
