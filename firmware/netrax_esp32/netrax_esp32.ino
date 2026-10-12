/*
  NETRAX safety station - ESP32 firmware (prototype)
  --------------------------------------------------------
  Sends one telemetry JSON per second to the NETRAX backend and every RFID scan as an attendance event.
  The backend replies with the LED / buzzer / relay state it wants (camera fire confirmed, PPE alert open, ...);
  the station always shows the STRONGER of that reply and its own local rule, so it still alarms with no Wi-Fi.

  Pin map = team guide "ESP32 PPE & Fire Safety - Pin / Data / Dashboard Guide" (GPIO numbers, not D-labels):
    DHT22 DATA 4 | MQ-2 AO 34 | MQ-135 AO 35 | thermistor node 32 | helmet FSR 36 (VP) | shoe FSR 39 (VN)
    flame DO 27 | MPU6050 SDA 21 / SCL 22 | RC522 SS 5, SCK 18, MOSI 23, MISO 19, RST 17 | PMS5003 TX -> 16 (RX2)
    LEDs green 33 / orange 14 / red 25 | buzzer 13 | relay 26 | TTP223 kit 2 / helmet 12 / shoe 15

  Libraries (Arduino Library Manager): "DHT sensor library" (Adafruit) + "Adafruit Unified Sensor", "MFRC522".
  Board: ESP32 Dev Module. Serial monitor 115200.

  BEFORE FLASHING: set WIFI_SSID, WIFI_PASS and SERVER (your laptop's IPv4 from `ipconfig`, port 8000).
  Thresholds here are PROTOTYPE values (raw ADC counts are not ppm) - same numbers as config/hardware.yaml.
*/
#include "secrets.h"        // Wi-Fi + laptop address (copy secrets.example.h -> secrets.h)
#include <WiFi.h>
#include <HTTPClient.h>
#include <SPI.h>
#include <Wire.h>
#include <MFRC522.h>
#include <DHT.h>

// ---------------------------------------------------------------- settings you must edit
const char* WIFI_SSID = SECRET_WIFI_SSID;     // secrets.h (not in git)
const char* WIFI_PASS = SECRET_WIFI_PASS;
const char* SERVER    = SECRET_SERVER; // laptop running the NETRAX backend (ipconfig on Wi-Fi "NOTHING")
const char* DEVICE_ID = "esp32-ppe-01";               // must match config/hardware.yaml
const char* FW        = "se-1.0";

// ---------------------------------------------------------------- pins
#define PIN_DHT        4
#define PIN_MQ2       34
#define PIN_MQ135     35
#define PIN_THERM     32
#define PIN_FSR_HELM  36
#define PIN_FSR_SHOE  39
#define PIN_FLAME     27
#define PIN_SDA       21
#define PIN_SCL       22
#define RFID_SS        5
#define RFID_SCK      18
#define RFID_MOSI     23
#define RFID_MISO     19
#define RFID_RST      17
#define PIN_PMS_RX    16
#define PIN_PMS_TX    -1      // PMS RX not used (passive read of its active-mode frames)
#define PIN_LED_G     33
#define PIN_LED_O     14
#define PIN_LED_R     25
#define PIN_BUZZER    13
#define PIN_RELAY     26
#define PIN_T_KIT      2      // GPIO 2 / 12 / 15 are boot-strapping pins: if the board will not boot or upload,
#define PIN_T_HELM    12      //   unplug the TTP223 OUT wires while flashing (see guide section 10)
#define PIN_T_SHOE    15

#define FLAME_ACTIVE_LOW  1   // most IR flame modules pull DO LOW on flame - set 0 if yours goes HIGH
#define RELAY_ACTIVE_LOW  0   // many relay boards switch on LOW - set 1 if the relay clicks the wrong way
#define BUZZER_ACTIVE_LOW 0

// ---------------------------------------------------------------- local alarm rule (works without Wi-Fi)
const float TEMP_CRITICAL_C = 50.0;
const int   MQ2_ABS         = 1800;
const int   PM25_WARN       = 35;
const int   PM25_CRITICAL   = 150;
const unsigned long MQ_WARMUP_MS = 60000;   // MQ heaters need ~1 min before readings mean anything

const unsigned long TELEMETRY_MS   = 1000;
const unsigned long DHT_MS         = 2100;  // DHT22 can only be read every ~2 s
const unsigned long BACKEND_HOLD_MS = 5000; // backend command is trusted for 5 s, then local rule only
const unsigned long RFID_COOLDOWN_MS = 3000;

DHT dht(PIN_DHT, DHT22);
MFRC522 rfid(RFID_SS, RFID_RST);
HardwareSerial PMS(2);

// ---------------------------------------------------------------- live state
float tempC = NAN, humid = NAN;
int pm1 = -1, pm25 = -1, pm10 = -1;
unsigned long pmAt = 0;
bool mpuOk = false;
float vib = NAN;                 // smoothed |acceleration - 1 g|, in g
int beLevel = 0;                 // backend command: 0 green, 1 orange, 2 red
bool beRelay = false;
unsigned long beAt = 0;
int outLevel = 0;
bool buzzerOn = false, relayOn = false;
String lastUid = "";
unsigned long lastScanMs = 0;
unsigned long beepUntil = 0;     // short RFID feedback beep
String localReason = "";

// ================================================================ helpers
void setOut(int pin, bool on, bool activeLow) { digitalWrite(pin, (on ^ activeLow) ? HIGH : LOW); }

bool flameNow() { int v = digitalRead(PIN_FLAME); return FLAME_ACTIVE_LOW ? (v == LOW) : (v == HIGH); }

void wifiKeepAlive() {           // non-blocking reconnect: the sensor loop never waits for Wi-Fi
  static unsigned long lastTry = 0;
  if (WiFi.status() == WL_CONNECTED) return;
  if (millis() - lastTry < 5000) return;
  lastTry = millis();
  Serial.println("[wifi] connecting...");
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASS);
}

String jnum(float v, int dp) { return isnan(v) ? String("null") : String(v, dp); }
String jint(int v) { return v < 0 ? String("null") : String(v); }
String jbool(bool b) { return b ? "true" : "false"; }

// ---------------------------------------------------------------- PMS5003 (32-byte frames, 0x42 0x4D)
void readPMS() {
  static uint8_t buf[32];
  static int n = 0;
  while (PMS.available()) {
    uint8_t c = PMS.read();
    if (n == 0 && c != 0x42) continue;
    if (n == 1 && c != 0x4D) { n = 0; continue; }
    buf[n++] = c;
    if (n == 32) {
      n = 0;
      uint16_t sum = 0;
      for (int i = 0; i < 30; i++) sum += buf[i];
      if (sum != ((buf[30] << 8) | buf[31])) continue;          // bad frame -> keep last valid values
      pm1  = (buf[10] << 8) | buf[11];                           // atmospheric values
      pm25 = (buf[12] << 8) | buf[13];
      pm10 = (buf[14] << 8) | buf[15];
      pmAt = millis();
    }
  }
  if (pmAt && millis() - pmAt > 10000) { pm1 = pm25 = pm10 = -1; }  // no valid frame for 10 s -> null
}

// ---------------------------------------------------------------- MPU6050 (raw I2C, no library)
bool mpuInit() {
  Wire.beginTransmission(0x68); Wire.write(0x6B); Wire.write(0);   // wake up
  return Wire.endTransmission() == 0;
}
void readMPU() {
  if (!mpuOk) return;
  Wire.beginTransmission(0x68); Wire.write(0x3B);
  if (Wire.endTransmission(false) != 0) { mpuOk = false; vib = NAN; return; }
  if (Wire.requestFrom(0x68, 6) != 6) return;
  int16_t ax = (Wire.read() << 8) | Wire.read();
  int16_t ay = (Wire.read() << 8) | Wire.read();
  int16_t az = (Wire.read() << 8) | Wire.read();
  float g = sqrtf((float)ax * ax + (float)ay * ay + (float)az * az) / 16384.0;  // +-2 g range
  float d = fabsf(g - 1.0);
  vib = isnan(vib) ? d : vib * 0.8 + d * 0.2;
}

// ---------------------------------------------------------------- local rule
int localLevel(int mq2) {
  localReason = "";
  bool warm = millis() > MQ_WARMUP_MS;
  if (flameNow()) localReason = "flame";
  else if (warm && mq2 >= MQ2_ABS) localReason = "MQ-2 smoke/gas";
  else if (!isnan(tempC) && tempC >= TEMP_CRITICAL_C) localReason = "high temperature";
  else if (pm25 >= PM25_CRITICAL) localReason = "PM2.5 smoke";
  if (localReason.length()) return 2;
  if (pm25 >= PM25_WARN) { localReason = "PM2.5 high"; return 1; }
  return 0;
}

void applyOutputs(int lvl, bool relayWanted) {
  outLevel = lvl;
  setOut(PIN_LED_G, lvl == 0, false);
  setOut(PIN_LED_O, lvl == 1, false);
  setOut(PIN_LED_R, lvl == 2, false);
  relayOn = (lvl == 2) || relayWanted;
  setOut(PIN_RELAY, relayOn, RELAY_ACTIVE_LOW);
  unsigned long ms = millis();
  bool pattern = false;
  if (lvl == 2) pattern = (ms / 250) % 2;              // fast alarm
  else if (lvl == 1) pattern = (ms % 3000) < 150;      // short warning chirp every 3 s
  buzzerOn = pattern || ms < beepUntil;
  setOut(PIN_BUZZER, buzzerOn, BUZZER_ACTIVE_LOW);
}

// ---------------------------------------------------------------- backend
int levelFromReply(const String& r) {
  if (r.indexOf("\"led\":\"red\"") >= 0 || r.indexOf("\"led\": \"red\"") >= 0) return 2;
  if (r.indexOf("\"led\":\"orange\"") >= 0 || r.indexOf("\"led\": \"orange\"") >= 0) return 1;
  return 0;
}

void sendTelemetry(int mq2, int mq135, int therm, int fsrH, int fsrS, bool flame, int lvl) {
  if (WiFi.status() != WL_CONNECTED) return;
  String j = "{\"device_id\":\"" + String(DEVICE_ID) + "\",\"fw\":\"" + FW + "\"";
  j += ",\"uptime_s\":" + String(millis() / 1000) + ",\"rssi\":" + String(WiFi.RSSI());
  j += ",\"temperature_c\":" + jnum(tempC, 1) + ",\"humidity_pct\":" + jnum(humid, 1);
  j += ",\"thermistor_adc\":" + String(therm) + ",\"mq2_adc\":" + String(mq2) + ",\"mq135_adc\":" + String(mq135);
  j += ",\"flame_detected\":" + jbool(flame);
  j += ",\"pm1_0_ugm3\":" + jint(pm1) + ",\"pm2_5_ugm3\":" + jint(pm25) + ",\"pm10_ugm3\":" + jint(pm10);
  j += ",\"helmet_fsr_adc\":" + String(fsrH) + ",\"shoe_fsr_adc\":" + String(fsrS);
  j += ",\"touch_kit\":" + jbool(digitalRead(PIN_T_KIT)) + ",\"touch_helmet\":" + jbool(digitalRead(PIN_T_HELM));
  j += ",\"touch_shoe\":" + jbool(digitalRead(PIN_T_SHOE));
  j += ",\"mpu6050_available\":" + jbool(mpuOk) + ",\"vibration_metric\":" + jnum(vib, 3);
  j += ",\"alarm_level\":\"" + String(lvl == 2 ? "DANGER" : lvl == 1 ? "WARNING" : "NORMAL") + "\"";
  j += ",\"alarm_reason\":\"" + localReason + "\"";
  j += ",\"green_led\":" + jbool(outLevel == 0) + ",\"orange_led\":" + jbool(outLevel == 1);
  j += ",\"red_led\":" + jbool(outLevel == 2) + ",\"buzzer_on\":" + jbool(buzzerOn) + ",\"relay_on\":" + jbool(relayOn) + "}";

  HTTPClient http;
  http.setConnectTimeout(800);
  http.setTimeout(1200);                 // short: a missing laptop must not freeze the alarm loop
  http.begin(String(SERVER) + "/api/hw/telemetry");
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(j);
  if (code == 200) {
    String r = http.getString();
    beLevel = levelFromReply(r);
    beRelay = r.indexOf("\"relay\":true") >= 0 || r.indexOf("\"relay\": true") >= 0;
    beAt = millis();
  } else {
    Serial.printf("[telemetry] HTTP %d\n", code);
  }
  http.end();
}

void sendScan(const String& uid) {
  Serial.println("[rfid] card " + uid);
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[rfid] no Wi-Fi - scan NOT recorded");     // never pretend attendance was saved
    beepUntil = millis() + 600;
    return;
  }
  HTTPClient http;
  http.setConnectTimeout(800);
  http.setTimeout(2500);
  http.begin(String(SERVER) + "/api/hw/rfid");
  http.addHeader("Content-Type", "application/json");
  int code = http.POST("{\"uid\":\"" + uid + "\",\"device_id\":\"" + String(DEVICE_ID) + "\"}");
  String r = code > 0 ? http.getString() : "";
  http.end();
  Serial.printf("[rfid] HTTP %d %s\n", code, r.c_str());
  bool ok = code == 200 && r.indexOf("\"ok\":true") >= 0;
  beepUntil = millis() + (ok ? 120 : 600);   // short beep = recorded, long beep = unknown card / error
}

void readRFID() {
  if (!rfid.PICC_IsNewCardPresent() || !rfid.PICC_ReadCardSerial()) return;
  String uid = "";
  for (byte i = 0; i < rfid.uid.size; i++) {
    if (rfid.uid.uidByte[i] < 0x10) uid += "0";
    uid += String(rfid.uid.uidByte[i], HEX);
  }
  uid.toUpperCase();
  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();
  unsigned long now = millis();
  if (uid == lastUid && now - lastScanMs < RFID_COOLDOWN_MS) return;   // card held at the reader
  lastUid = uid; lastScanMs = now;
  sendScan(uid);
}

// ================================================================ setup / loop
void setup() {
  Serial.begin(115200);
  pinMode(PIN_FLAME, INPUT);
  pinMode(PIN_T_KIT, INPUT); pinMode(PIN_T_HELM, INPUT); pinMode(PIN_T_SHOE, INPUT);
  pinMode(PIN_LED_G, OUTPUT); pinMode(PIN_LED_O, OUTPUT); pinMode(PIN_LED_R, OUTPUT);
  pinMode(PIN_BUZZER, OUTPUT); pinMode(PIN_RELAY, OUTPUT);
  applyOutputs(0, false);
  analogReadResolution(12);
  analogSetAttenuation(ADC_11db);        // ~0-3.3 V on the ADC pins (keep MQ AO behind its divider)

  dht.begin();
  PMS.begin(9600, SERIAL_8N1, PIN_PMS_RX, PIN_PMS_TX);
  Wire.begin(PIN_SDA, PIN_SCL);
  mpuOk = mpuInit();
  SPI.begin(RFID_SCK, RFID_MISO, RFID_MOSI, RFID_SS);
  rfid.PCD_Init();

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("NETRAX station %s  fw %s  MPU6050 %s\n", DEVICE_ID, FW, mpuOk ? "ok" : "not found");
}

void loop() {
  static unsigned long lastTel = 0, lastDht = 0, lastMpu = 0;
  unsigned long now = millis();

  wifiKeepAlive();
  readPMS();
  readRFID();
  if (now - lastMpu >= 50) { lastMpu = now; readMPU(); }
  if (now - lastDht >= DHT_MS) {
    lastDht = now;
    float t = dht.readTemperature(), h = dht.readHumidity();
    tempC = t; humid = h;                 // NaN on a failed read -> sent as null, never as 0
  }

  int mq2 = analogRead(PIN_MQ2);
  int lvl = localLevel(mq2);
  bool beFresh = beAt && now - beAt < BACKEND_HOLD_MS;
  int shown = max(lvl, beFresh ? beLevel : 0);
  applyOutputs(shown, beFresh && beRelay);

  if (now - lastTel >= TELEMETRY_MS) {
    lastTel = now;
    sendTelemetry(mq2, analogRead(PIN_MQ135), analogRead(PIN_THERM), analogRead(PIN_FSR_HELM),
                  analogRead(PIN_FSR_SHOE), flameNow(), lvl);
    Serial.printf("T=%.1f H=%.0f MQ2=%d PM2.5=%d flame=%d lvl=%d/%d wifi=%d\n", tempC, humid, mq2, pm25,
                  flameNow(), lvl, beFresh ? beLevel : -1, WiFi.status() == WL_CONNECTED);
  }
  delay(10);
}
