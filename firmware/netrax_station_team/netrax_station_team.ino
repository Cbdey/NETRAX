// NETRAX safety station - the team's industrial safety sketch, connected to the NETRAX dashboard (6 Nov 2026).
//
// What changed from the team's version (everything else - pins, sensors, thresholds, LEDs, Serial report - is unchanged):
//   1. SENDS the readings to NETRAX every second: HTTP POST JSON to /api/hw/telemetry (it never sent anything before).
//   2. Correct URL: http://<laptop-ip>:8000/api/hw/telemetry (was the old laptop IP + /api/rfid = the RFID card endpoint).
//   3. DEVICE_ID "esp32-ppe-01" = the station configured in config/hardware.yaml (zone: welding_bay), so the dashboard's
//      hardware page shows it and camera fire/smoke in the welding bay is cross-checked with these sensors.
//   4. Wi-Fi reconnects by itself if it drops; one failed send never blocks the sensors / buzzer.
//   5. DHT22 read every 2 s (it cannot be read faster - it returned NaN), last good value kept in between.
//   6. Helmet pressure missing = YELLOW warning, not RED critical (it switched buzzer + relay on all the time).
//   7. uptime_s is sent, so NETRAX ignores MQ gas "rises" during the sensors' 60 s warm-up.
//   8. NETRAX's reply can turn the station RED too (e.g. the camera saw fire in this zone) - the stronger of the
//      local rule and the dashboard's view wins.
//
// 11 Nov - merged the team's NEW version ("dashboard free, NEO-6M GPS"):
//   9.  NEO-6M GPS on UART2 (GPS TX -> GPIO16, GPS RX -> GPIO17, 9600 baud), read without blocking. With a fix the
//       position goes to NETRAX every 10 s (POST /api/location/hardware) -> events get the station's GPS location.
//   10. Buzzer ON continuously for WARNING or CRITICAL (team's new rule); relay only for CRITICAL. LEDs blink
//       (green 500 ms / yellow 250 ms / red 100 ms) like the team's version.
//   11. Vibration: the MPU6050 reading is sent as vibration_metric (how far the total acceleration is from 1 g, in g),
//       so the dashboard's vibration graph shows real values (the raw arrays were ignored).
//   12. The laptop address is in secrets.h (SECRET_NETRAX_HOST) - a new laptop IP = change one line there
//       (python -m tools.lan_setup --write does it for you), not this file.
//
// Arduino IDE: board "ESP32 Dev Module"; libraries: "DHT sensor library" (Adafruit) and "TinyGPSPlus" (Mikal Hart).
// WiFi + HTTPClient come with the ESP32 core.

#include <Wire.h>
#include <DHT.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <TinyGPSPlus.h>
#include <math.h>

// =====================================================
// 1. WI-FI AND DASHBOARD SETTINGS  (in secrets.h)
// =====================================================

// Wi-Fi name + password + laptop address live in secrets.h next to this file (NOT in git). Copy secrets.example.h ->
// secrets.h. It must be the SAME Wi-Fi as the laptop (2.4 GHz - the ESP32 has no 5 GHz).
#include "secrets.h"
#ifndef SECRET_NETRAX_HOST
#define SECRET_NETRAX_HOST "http://10.115.43.235:8000"   // old default - put the laptop address in secrets.h
#endif
const char* WIFI_SSID     = SECRET_WIFI_SSID;
const char* WIFI_PASSWORD = SECRET_WIFI_PASSWORD;

// The laptop's IP changes with the Wi-Fi: on the laptop run  python -m tools.lan_setup  (or ipconfig, Wi-Fi IPv4).
const String NETRAX_HOST  = SECRET_NETRAX_HOST;
const String DASHBOARD_URL = NETRAX_HOST + "/api/hw/telemetry";
const String LOCATION_URL  = NETRAX_HOST + "/api/location/hardware";
const char* DEVICE_ID     = "esp32-ppe-01";           // must match config/hardware.yaml

const unsigned long SEND_EVERY_MS = 1000;            // NETRAX marks the station OFFLINE after 6 s without data


// =====================================================
// 2. PIN CONFIGURATION
// =====================================================

// Sensors
#define DHT_PIN          4
#define DHT_TYPE         DHT22

#define FLAME_PIN        19

#define MQ2_PIN          34
#define MQ135_PIN        35

#define FSR_PIN          36

// Sharp dust sensor
#define PM_LED_PIN       18
#define PM_VO_PIN        32

// MPU6050
#define SDA_PIN          21
#define SCL_PIN          22
#define MOTION_ADDRESS   0x68

// LEDs
#define GREEN_LED        33
#define YELLOW_LED       13
#define RED_LED          27

// Outputs
#define BUZZER_PIN       14
#define RELAY_PIN        25

// GPS NEO-6M (UART2)
#define GPS_RX_PIN       16   // ESP32 receives from GPS TX
#define GPS_TX_PIN       17   // ESP32 transmits to GPS RX
#define GPS_BAUD_RATE    9600


// =====================================================
// 3. SENSOR SETTINGS
// =====================================================

DHT dht(DHT_PIN, DHT_TYPE);

const bool FLAME_ACTIVE_LOW = true;
const bool BUZZER_ACTIVE_HIGH = true;
const bool RELAY_ACTIVE_LOW = true;


// =====================================================
// 4. PROTOTYPE THRESHOLDS
// =====================================================

// Temperature
const float TEMP_WARNING = 40.0;
const float TEMP_CRITICAL = 50.0;

// MQ sensors: ADC values, NOT ppm
const int MQ2_WARNING = 1400;
const int MQ2_CRITICAL = 1800;

const int MQ135_WARNING = 1400;
const int MQ135_CRITICAL = 1800;

// Helmet pressure
const int FSR_THRESHOLD = 700;

// Dust sensor voltage divider
const float PM_DIVIDER_RATIO = 28.0 / 18.0;


// =====================================================
// 5. SENSOR VARIABLES
// =====================================================

float temperature = NAN;
float humidity = NAN;
unsigned long lastDhtRead = 0;

int flameRaw = HIGH;
bool flameDetected = false;

int mq2Value = 0;
int mq135Value = 0;

int fsrValue = 0;

int pmADC = 0;
float pmPinVoltage = 0;
float pmSensorVoltage = 0;

bool motionAvailable = false;

int16_t ax, ay, az;
int16_t gx, gy, gz;
float vibrationG = 0;             // | total acceleration - 1 g |, in g (MPU6050 +-2 g range: 16384 per g)

TinyGPSPlus gps;
unsigned long lastGpsSend = 0;
int lastGpsHttp = 0;
const unsigned long GPS_SEND_EVERY_MS = 10000;

bool ledState = false;
unsigned long lastBlink = 0;

bool temperatureWarning = false;
bool temperatureCritical = false;

bool mq2Warning = false;
bool mq2Critical = false;

bool mq135Warning = false;
bool mq135Critical = false;

bool helmetWarning = false;      // was helmetCritical: a missing helmet is a WARNING, not a fire-level alarm
bool fireCritical = false;

bool mediumDanger = false;
bool criticalDanger = false;

// NETRAX link
bool dashboardRed = false;        // NETRAX says: alarm in this zone (e.g. the camera confirmed fire)
bool dashboardOrange = false;     // NETRAX says: warning (e.g. PPE violation open in this zone)
int lastHttpCode = 0;
unsigned long lastSend = 0;
unsigned long lastWifiTry = 0;


// =====================================================
// 6. WI-FI
// =====================================================

void connectWiFi() {
  Serial.println();
  Serial.println("========== WI-FI CONNECTION ==========");
  Serial.print("Connecting to Wi-Fi: ");
  Serial.println(WIFI_SSID);

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long startAttempt = millis();
  const unsigned long WIFI_TIMEOUT_MS = 20000;
  while (WiFi.status() != WL_CONNECTED && millis() - startAttempt < WIFI_TIMEOUT_MS) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("Wi-Fi Status: CONNECTED");
    Serial.print("ESP32 IP Address: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("Wi-Fi Status: NOT CONNECTED - check name / password (2.4 GHz only). Will keep retrying.");
  }
  Serial.print("NETRAX URL: ");
  Serial.println(DASHBOARD_URL);
  Serial.println("======================================");
}

void keepWiFi() {                 // never blocks the sensors: one retry every 10 s
  if (WiFi.status() == WL_CONNECTED) return;
  if (millis() - lastWifiTry < 10000) return;
  lastWifiTry = millis();
  Serial.println("[wifi] reconnecting...");
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}


// =====================================================
// 7. MPU6050 REGISTER FUNCTIONS
// =====================================================

bool writeRegister(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(MOTION_ADDRESS);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

bool readRegisters(uint8_t reg, uint8_t *data, uint8_t length) {
  Wire.beginTransmission(MOTION_ADDRESS);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) {
    return false;
  }
  uint8_t received = Wire.requestFrom((uint8_t)MOTION_ADDRESS, length, (uint8_t)true);
  if (received != length) {
    while (Wire.available()) Wire.read();
    return false;
  }
  for (int i = 0; i < length; i++) {
    data[i] = Wire.read();
  }
  return true;
}

bool readMotionSensor() {
  uint8_t data[14];
  if (!readRegisters(0x3B, data, 14)) {
    return false;
  }
  ax = (int16_t)((data[0] << 8) | data[1]);
  ay = (int16_t)((data[2] << 8) | data[3]);
  az = (int16_t)((data[4] << 8) | data[5]);
  gx = (int16_t)((data[8] << 8) | data[9]);
  gy = (int16_t)((data[10] << 8) | data[11]);
  gz = (int16_t)((data[12] << 8) | data[13]);
  float g = sqrt((float)ax * ax + (float)ay * ay + (float)az * az) / 16384.0;
  vibrationG = fabs(g - 1.0);
  return true;
}


// =====================================================
// 8. BUZZER, RELAY, LEDS
// =====================================================

void setBuzzer(bool state) {
  if (BUZZER_ACTIVE_HIGH) {
    digitalWrite(BUZZER_PIN, state ? HIGH : LOW);
  } else {
    digitalWrite(BUZZER_PIN, state ? LOW : HIGH);
  }
}

void setRelay(bool state) {
  if (RELAY_ACTIVE_LOW) {
    digitalWrite(RELAY_PIN, state ? LOW : HIGH);
  } else {
    digitalWrite(RELAY_PIN, state ? HIGH : LOW);
  }
}

void setIndicators(bool red, bool yellow) {
  // team's new version: the LED of the current state BLINKS - green 500 ms, yellow 250 ms, red 100 ms
  unsigned long every = red ? 100 : yellow ? 250 : 500;
  if (millis() - lastBlink >= every) {
    lastBlink = millis();
    ledState = !ledState;
  }
  digitalWrite(GREEN_LED, (!red && !yellow && ledState) ? HIGH : LOW);
  digitalWrite(YELLOW_LED, (!red && yellow && ledState) ? HIGH : LOW);
  digitalWrite(RED_LED, (red && ledState) ? HIGH : LOW);
}


// =====================================================
// 9. SHARP DUST SENSOR
// =====================================================

void readDustSensor() {
  digitalWrite(PM_LED_PIN, LOW);
  delayMicroseconds(280);
  pmADC = analogRead(PM_VO_PIN);
  pmPinVoltage = analogReadMilliVolts(PM_VO_PIN) / 1000.0;
  delayMicroseconds(40);
  digitalWrite(PM_LED_PIN, HIGH);
  delayMicroseconds(9680);
  pmSensorVoltage = pmPinVoltage * PM_DIVIDER_RATIO;
}


// =====================================================
// 10. READ ALL SENSORS
// =====================================================

void readAllSensors() {
  if (millis() - lastDhtRead >= 2000 || lastDhtRead == 0) {      // DHT22: at most one read every 2 s
    lastDhtRead = millis();
    float t = dht.readTemperature();
    float h = dht.readHumidity();
    if (!isnan(t)) temperature = t;
    if (!isnan(h)) humidity = h;
  }

  flameRaw = digitalRead(FLAME_PIN);
  flameDetected = FLAME_ACTIVE_LOW ? (flameRaw == LOW) : (flameRaw == HIGH);

  mq2Value = analogRead(MQ2_PIN);
  mq135Value = analogRead(MQ135_PIN);
  fsrValue = analogRead(FSR_PIN);

  readDustSensor();
  motionAvailable = readMotionSensor();
}


// =====================================================
// 11. SAFETY CONDITION CHECK (local rule - works even without Wi-Fi)
// =====================================================

void checkSafetyConditions() {
  temperatureWarning = temperatureCritical = false;
  mq2Warning = mq2Critical = false;
  mq135Warning = mq135Critical = false;
  helmetWarning = fireCritical = false;

  if (!isnan(temperature)) {
    if (temperature >= TEMP_CRITICAL) temperatureCritical = true;
    else if (temperature >= TEMP_WARNING) temperatureWarning = true;
  }
  if (mq2Value >= MQ2_CRITICAL) mq2Critical = true;
  else if (mq2Value >= MQ2_WARNING) mq2Warning = true;

  if (mq135Value >= MQ135_CRITICAL) mq135Critical = true;
  else if (mq135Value >= MQ135_WARNING) mq135Warning = true;

  if (fsrValue < FSR_THRESHOLD) helmetWarning = true;
  if (flameDetected) fireCritical = true;

  criticalDanger = temperatureCritical || mq2Critical || mq135Critical || fireCritical;
  mediumDanger = !criticalDanger && (temperatureWarning || mq2Warning || mq135Warning || helmetWarning);

  // the stronger of the local rule and NETRAX's view (the dashboard may know about a camera-confirmed fire here)
  bool red = criticalDanger || dashboardRed;
  bool yellow = mediumDanger || dashboardOrange;
  setIndicators(red, yellow);
  setBuzzer(red || mediumDanger);  // team's new rule: buzzer ON continuously for WARNING or CRITICAL
  setRelay(red);                   // relay only for CRITICAL
}


// =====================================================
// 11b. GPS (NEO-6M) - never blocks; position to NETRAX every 10 s once there is a fix
// =====================================================

void readGPS() {
  while (Serial2.available() > 0) {
    gps.encode(Serial2.read());
  }
}

void sendLocation() {
  if (WiFi.status() != WL_CONNECTED || !gps.location.isValid() || gps.location.age() > 5000) return;
  String j = "{\"device_id\":\"" + String(DEVICE_ID) + "\",\"fix\":true";
  j += ",\"lat\":" + String(gps.location.lat(), 6);
  j += ",\"lon\":" + String(gps.location.lng(), 6);
  if (gps.hdop.isValid()) j += ",\"accuracy_m\":" + String(gps.hdop.hdop() * 5.0, 1);   // rough: HDOP x 5 m
  j += "}";
  HTTPClient http;
  http.setConnectTimeout(800);
  http.setTimeout(1200);
  if (!http.begin(LOCATION_URL)) return;
  http.addHeader("Content-Type", "application/json");
  lastGpsHttp = http.POST(j);
  http.end();
}


// =====================================================
// 12. SEND TO NETRAX  (POST JSON to /api/hw/telemetry, read the reply)
// =====================================================

void sendToDashboard() {
  if (WiFi.status() != WL_CONNECTED) {
    lastHttpCode = -1;
    return;
  }
  String j = "{";
  j += "\"device_id\":\"" + String(DEVICE_ID) + "\"";
  j += ",\"uptime_s\":" + String(millis() / 1000);
  if (!isnan(temperature)) j += ",\"temperature_c\":" + String(temperature, 1);
  if (!isnan(humidity))    j += ",\"humidity_pct\":" + String(humidity, 1);
  j += ",\"flame_detected\":" + String(flameDetected ? "true" : "false");
  j += ",\"mq2_adc\":" + String(mq2Value);
  j += ",\"mq135_adc\":" + String(mq135Value);
  j += ",\"helmet_fsr_adc\":" + String(fsrValue);
  j += ",\"pm_adc\":" + String(pmADC);
  j += ",\"pm_sensor_v\":" + String(pmSensorVoltage, 3);       // raw voltage only - PM2.5 is NOT calibrated
  j += ",\"mpu6050_available\":" + String(motionAvailable ? "true" : "false");
  if (motionAvailable) {
    j += ",\"vibration_metric\":" + String(vibrationG, 3);
    j += ",\"accel\":[" + String(ax) + "," + String(ay) + "," + String(az) + "]";
    j += ",\"gyro\":[" + String(gx) + "," + String(gy) + "," + String(gz) + "]";
  }
  if (gps.location.isValid()) {
    j += ",\"gps\":{\"lat\":" + String(gps.location.lat(), 6) + ",\"lon\":" + String(gps.location.lng(), 6) + "}";
  }
  j += ",\"alarm_level\":\"" + String(criticalDanger ? "CRITICAL" : mediumDanger ? "WARNING" : "NORMAL") + "\"";
  j += ",\"buzzer_on\":" + String((criticalDanger || mediumDanger || dashboardRed) ? "true" : "false");
  j += ",\"relay_on\":" + String((criticalDanger || dashboardRed) ? "true" : "false");
  j += ",\"rssi\":" + String(WiFi.RSSI());
  j += "}";

  HTTPClient http;
  http.setConnectTimeout(800);
  http.setTimeout(1200);                       // a slow laptop never freezes the station
  if (!http.begin(DASHBOARD_URL)) {
    lastHttpCode = -2;
    return;
  }
  http.addHeader("Content-Type", "application/json");
  lastHttpCode = http.POST(j);
  if (lastHttpCode == 200) {
    String r = http.getString();               // e.g. {"led":"red","buzzer":"alarm","relay":true,...}
    dashboardRed = r.indexOf("\"led\":\"red\"") >= 0;
    dashboardOrange = r.indexOf("\"led\":\"orange\"") >= 0;
  } else {
    dashboardRed = dashboardOrange = false;    // no answer: local rule only (never "safe" because of silence)
  }
  http.end();
}


// =====================================================
// 13. SERIAL MONITOR REPORT (short, every 5 s)
// =====================================================

void printReport() {
  Serial.println();
  Serial.println("============ NETRAX SAFETY STATION ============");
  if (isnan(temperature)) Serial.println("DHT22: SENSOR ERROR");
  else { Serial.print("Temp "); Serial.print(temperature, 1); Serial.print(" C  Humidity "); Serial.print(humidity, 0); Serial.println(" %"); }
  Serial.print("Flame: "); Serial.println(flameDetected ? "FIRE DETECTED" : "normal");
  Serial.print("MQ-2 ADC "); Serial.print(mq2Value); Serial.print("   MQ-135 ADC "); Serial.println(mq135Value);
  Serial.print("Helmet FSR "); Serial.print(fsrValue); Serial.println(helmetWarning ? "  (no pressure - check helmet)" : "  (pressure ok)");
  Serial.print("Dust sensor "); Serial.print(pmSensorVoltage, 2); Serial.println(" V (not calibrated)");
  Serial.print("Motion sensor: "); if (motionAvailable) { Serial.print("ok, vibration "); Serial.print(vibrationG, 3); Serial.println(" g"); } else Serial.println("NOT AVAILABLE");
  Serial.print("GPS: ");
  if (gps.location.isValid()) {
    Serial.print(gps.location.lat(), 6); Serial.print(", "); Serial.print(gps.location.lng(), 6);
    Serial.print("  satellites "); Serial.print(gps.satellites.isValid() ? (int)gps.satellites.value() : 0);
    Serial.print(lastGpsHttp == 200 ? "  (sent to NETRAX)" : "  (not sent yet)"); Serial.println();
  } else {
    Serial.print("waiting for satellite fix (characters from GPS: "); Serial.print(gps.charsProcessed());
    Serial.println(gps.charsProcessed() < 10 ? " - check GPS wiring TX->16, RX->17)" : ")");
  }
  Serial.print("Local status: "); Serial.println(criticalDanger ? "RED - CRITICAL" : mediumDanger ? "YELLOW - WARNING" : "GREEN - NORMAL");
  Serial.print("NETRAX: ");
  if (lastHttpCode == 200) Serial.println(dashboardRed ? "connected - dashboard says ALARM" : dashboardOrange ? "connected - dashboard says WARNING" : "connected - OK");
  else if (lastHttpCode == -1) Serial.println("no Wi-Fi");
  else { Serial.print("NOT reached (HTTP "); Serial.print(lastHttpCode); Serial.println(") - check laptop IP, same Wi-Fi, NETRAX running"); }
  Serial.println("===============================================");
}


// =====================================================
// 14. SETUP
// =====================================================

void setup() {
  Serial.begin(115200);
  delay(1000);

  pinMode(FLAME_PIN, INPUT);
  pinMode(PM_LED_PIN, OUTPUT);
  pinMode(GREEN_LED, OUTPUT);
  pinMode(YELLOW_LED, OUTPUT);
  pinMode(RED_LED, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(RELAY_PIN, OUTPUT);

  digitalWrite(PM_LED_PIN, HIGH);
  setIndicators(false, false);
  setBuzzer(false);
  setRelay(false);

  analogReadResolution(12);
  analogSetPinAttenuation(MQ2_PIN, ADC_11db);
  analogSetPinAttenuation(MQ135_PIN, ADC_11db);
  analogSetPinAttenuation(FSR_PIN, ADC_11db);
  analogSetPinAttenuation(PM_VO_PIN, ADC_11db);

  dht.begin();
  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.setClock(100000);
  delay(100);
  Serial.println(writeRegister(0x6B, 0x00) ? "Motion sensor wake-up command sent" : "Motion sensor communication failed");

  // GPS NEO-6M on UART2. Bigger receive buffer: an HTTP send can take ~1 s, the GPS keeps talking meanwhile.
  Serial2.setRxBufferSize(2048);
  Serial2.begin(GPS_BAUD_RATE, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
  Serial.println("NEO-6M GPS started (TX -> GPIO16, RX -> GPIO17, 9600 baud). First fix outdoors can take 1-5 min.");

  connectWiFi();
  Serial.println("System initialization complete. Serial Monitor: 115200 baud");
}


// =====================================================
// 15. MAIN LOOP  (sensors + local alarm every loop, send every second, report every 5 s)
// =====================================================

unsigned long lastReport = 0;

void loop() {
  keepWiFi();
  readGPS();
  readAllSensors();
  checkSafetyConditions();

  if (millis() - lastSend >= SEND_EVERY_MS) {
    lastSend = millis();
    sendToDashboard();
    checkSafetyConditions();                   // apply NETRAX's reply at once
  }
  if (millis() - lastGpsSend >= GPS_SEND_EVERY_MS) {
    lastGpsSend = millis();
    sendLocation();
  }
  if (millis() - lastReport >= 5000) {
    lastReport = millis();
    printReport();
  }
  readGPS();
  delay(20);                                   // short: smooth LED blinking, GPS buffer emptied often
}
