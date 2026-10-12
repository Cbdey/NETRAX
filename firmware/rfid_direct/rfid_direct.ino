/*
  NETRAX RFID reader - DIRECT to the laptop (no Google Sheets).
  Based on the team's sketch (same Wi-Fi, same pins, same JSON to /api/rfid). What changed:
   1. EVERY card is sent, also cards this file does not know (Sushree, Sriya, Chandrabhanu, visitors).
      The laptop's worker list decides who it is.
   2. The LAPTOP decides ENTERED / EXITED from its database, so an ESP32 reboot no longer makes
      someone "enter" twice. The reply comes back to the reader:
        "result":"STUDENT ENTERED - ..."  / "STUDENT EXITED - ..."  -> short beep
        "result":"ACCESS DENIED - ..."                               -> long alarm beeps (not allowed in this zone)
        "result":"DUPLICATE ..."                                     -> card held too long, no beep
        anything else (unregistered card / error)                    -> 3 short beeps
   3. Boot check: prints "LAPTOP CHECK: OK" when the laptop can be reached (the hardware page turns the
      RFID chip green at the same moment).
  Wiring unchanged: RC522 SS 5, SCK 18, MISO 19, MOSI 23, RST 22; buzzer 4.
  BEFORE UPLOADING: SERVER = the NETRAX laptop's Wi-Fi IPv4 (ipconfig), same Wi-Fi as this ESP32.
*/
#include "secrets.h"        // Wi-Fi + laptop address (copy secrets.example.h -> secrets.h)
#include <WiFi.h>
#include <HTTPClient.h>
#include <SPI.h>
#include <MFRC522.h>
#include <time.h>

// ================= WIFI SETTINGS =================

const char* WIFI_SSID     = SECRET_WIFI_SSID;   // secrets.h (not in git)
const char* WIFI_PASSWORD = SECRET_WIFI_PASS;

const char* SERVER    = SECRET_SERVER;   // NETRAX laptop
const char* DEVICE_ID = "esp32-rfid-01";                 // config/hardware.yaml -> attendance.readers (zone)

// ================= RFID PINS =================

#define SS_PIN     5
#define RST_PIN    22
#define SCK_PIN    18
#define MISO_PIN   19
#define MOSI_PIN   23
#define BUZZER_PIN 4

MFRC522 rfid(SS_PIN, RST_PIN);

// ================= SCAN SETTINGS =================

const unsigned long SCAN_COOLDOWN = 2500;
unsigned long lastScanTime = 0;
String lastScannedUID = "";

// ================= HELPERS =================

// Names only for the Serial Monitor - the laptop decides who the card belongs to.
String getStudentName(const String& uid) {
  if (uid == "52504B5C") return "Bibhu Biswal";
  if (uid == "C32CA720") return "Anuska Dash";
  if (uid == "7366A320") return "Chinmaya Kumar Sahoo";
  return "UNKNOWN";
}

String readCardUID() {
  String uid = "";
  for (byte i = 0; i < rfid.uid.size; i++) {
    if (rfid.uid.uidByte[i] < 0x10) uid += "0";
    uid += String(rfid.uid.uidByte[i], HEX);
  }
  uid.toUpperCase();
  return uid;
}

String getTimestamp() {
  struct tm timeinfo;
  if (getLocalTime(&timeinfo, 200)) {
    char buffer[25];
    strftime(buffer, sizeof(buffer), "%Y-%m-%d %H:%M:%S", &timeinfo);
    return String(buffer);
  }
  return "ESP32-" + String(millis());
}

// "result":"..." from the laptop's JSON reply
String jsonString(const String& json, const String& key) {
  int k = json.indexOf("\"" + key + "\"");
  if (k < 0) return "";
  int colon = json.indexOf(':', k);
  int q1 = json.indexOf('"', colon + 1);
  int q2 = json.indexOf('"', q1 + 1);
  if (colon < 0 || q1 < 0 || q2 < 0) return "";
  return json.substring(q1 + 1, q2);
}

void beep(int onMs, int offMs, int times) {
  for (int i = 0; i < times; i++) {
    digitalWrite(BUZZER_PIN, HIGH); delay(onMs);
    digitalWrite(BUZZER_PIN, LOW);  delay(offMs);
  }
}

// ================= LAPTOP =================

void checkLaptop() {
  WiFiClient client;
  HTTPClient http;
  String url = String(SERVER) + "/api/hw/rfid/ping?device_id=" + DEVICE_ID;
  if (!http.begin(client, url)) { Serial.println("LAPTOP CHECK: bad SERVER address"); return; }
  http.setConnectTimeout(4000);
  http.setTimeout(4000);
  int code = http.GET();
  if (code == 200) Serial.println("LAPTOP CHECK: OK - scans will show on the NETRAX dashboard");
  else {
    Serial.printf("LAPTOP CHECK: FAILED (%d %s)\n", code, http.errorToString(code).c_str());
    Serial.println("  -> backend running? same Wi-Fi? SERVER = laptop's Wi-Fi IPv4 (ipconfig)?");
  }
  http.end();
}

// returns the laptop's result line, "" when the laptop could not be reached
String sendToLaptop(const String& name, const String& uid, const String& timestamp) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[ERROR] Wi-Fi disconnected.");
    return "";
  }
  WiFiClient client;
  HTTPClient http;
  if (!http.begin(client, String(SERVER) + "/api/rfid")) {
    Serial.println("[ERROR] Could not connect to the laptop.");
    return "";
  }
  http.addHeader("Content-Type", "application/json");
  http.setConnectTimeout(5000);
  http.setTimeout(7000);

  String payload = "{";
  payload += "\"name\":\"" + name + "\",";
  payload += "\"uid\":\"" + uid + "\",";
  payload += "\"device_id\":\"" + String(DEVICE_ID) + "\",";
  payload += "\"event\":\"SCAN\",";                     // the laptop decides ENTERED / EXITED
  payload += "\"timestamp\":\"" + timestamp + "\"";
  payload += "}";
  Serial.println("[NETRAX] Sending: " + payload);

  int httpCode = http.POST(payload);
  String response = httpCode > 0 ? http.getString() : "";
  Serial.printf("[NETRAX] HTTP status: %d\n", httpCode);
  if (httpCode <= 0) Serial.println("[NETRAX] " + http.errorToString(httpCode));
  http.end();

  if (httpCode < 200 || httpCode >= 300) return "";
  String result = jsonString(response, "result");
  String msg = jsonString(response, "message");
  if (msg.length()) Serial.println("[NETRAX] " + msg);
  return result.length() ? result : "ERROR";
}

// ================= PROCESS CARD =================

void processCard(const String& uid) {
  String name = getStudentName(uid);
  Serial.println("\n------------------------------");
  Serial.println("UID: " + uid);
  Serial.println("Name (this file): " + name);

  String result = sendToLaptop(name, uid, getTimestamp());
  Serial.println("RESULT: " + (result.length() ? result : String("laptop not reachable")));

  if (result.startsWith("ACCESS DENIED"))       beep(700, 200, 3);   // not allowed in this zone
  else if (result.startsWith("STUDENT ENTERED")) beep(120, 0, 1);
  else if (result.startsWith("STUDENT EXITED"))  beep(120, 80, 2);
  else if (result.startsWith("DUPLICATE"))       { }                  // card held at the reader
  else {
    beep(100, 100, 3);                                                // unregistered card or laptop down
    if (!result.length()) Serial.println("Check: backend running, IP address, port 8000, same Wi-Fi.");
  }
  Serial.println("------------------------------");
}

// ================= SETUP =================

void setup() {
  Serial.begin(115200);
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, LOW);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Connecting to Wi-Fi");
  unsigned long startTime = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - startTime < 20000) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("[WIFI] Connected");
    Serial.print("[WIFI] IP: ");
    Serial.println(WiFi.localIP());
    configTime(19800, 0, "pool.ntp.org", "time.google.com");   // India Standard Time (UTC+5:30)
    checkLaptop();
  } else {
    Serial.println("[WIFI] Not connected; will retry in loop.");
  }

  SPI.begin(SCK_PIN, MISO_PIN, MOSI_PIN, SS_PIN);
  rfid.PCD_Init();
  Serial.println("[RFID] Reader initialized. Laptop: " + String(SERVER));
  Serial.println("[SYSTEM] Ready to scan.");
}

// ================= MAIN LOOP =================

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    static unsigned long lastRetry = 0;
    if (millis() - lastRetry >= 10000) {
      lastRetry = millis();
      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    }
  }

  if (!rfid.PICC_IsNewCardPresent()) { delay(20); return; }
  if (!rfid.PICC_ReadCardSerial())   { delay(20); return; }

  String uid = readCardUID();
  unsigned long now = millis();
  if (uid == lastScannedUID && now - lastScanTime < SCAN_COOLDOWN) {
    rfid.PICC_HaltA();
    rfid.PCD_StopCrypto1();
    return;
  }
  lastScannedUID = uid;
  lastScanTime = now;

  processCard(uid);

  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();
  delay(50);
}
