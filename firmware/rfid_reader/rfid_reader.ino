/*
  NETRAX RFID reader (restricted zone door) - based on the team's Google Sheets attendance sketch.

  WHAT CHANGED vs the old sketch
   1. EVERY card is sent to the NETRAX laptop (not only the 3 cards written in this file):
        GET http://<laptop-ip>:8000/api/hw/rfid?uid=7366A320&device_id=esp32-rfid-01&name=Chinmaya
      The laptop decides: ENTERED / EXITED, NOT AUTHORISED for this zone, or unknown card.
      It updates both dashboards live, speaks, raises the alert, and forwards the scan to the SAME
      Google Sheet (config/hardware.yaml -> attendance.sheets_url), so the sheet keeps working.
   2. Replies:  "STUDENT ENTERED"  / "STUDENT EXITED"  -> short beep (as before)
                "ACCESS DENIED ..."                    -> long alarm beep (worker not allowed here)
                "DUPLICATE"                            -> card held too long, no beep
                anything else                          -> 3 short error beeps
   3. If the laptop cannot be reached, the scan goes straight to Google Sheets like before (fallback),
      so attendance is never lost.
  Wiring is unchanged: RC522 SDA/SS 5, SCK 18, MOSI 23, MISO 19, RST 22; buzzer 4.
  BEFORE UPLOADING: set SERVER to the laptop's IPv4 (ipconfig) on the same Wi-Fi.
*/
#include "secrets.h"        // Wi-Fi + laptop address (copy secrets.example.h -> secrets.h)
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <SPI.h>
#include <MFRC522.h>

// ---------------------------------------------------------------- settings
const char* WIFI_SSID     = SECRET_WIFI_SSID;   // secrets.h (not in git)
const char* WIFI_PASSWORD = SECRET_WIFI_PASS;
const char* SERVER        = SECRET_SERVER;   // NETRAX laptop (ipconfig -> Wi-Fi IPv4)
const char* DEVICE_ID     = "esp32-rfid-01";                 // config/hardware.yaml -> attendance.readers
const char* scriptURL =                                       // fallback only (laptop unreachable)
  SECRET_SHEETS_URL;

// ---------------------------------------------------------------- pins
#define SS_PIN     5
#define RST_PIN    22
#define BUZZER_PIN 4

MFRC522 mfrc522(SS_PIN, RST_PIN);

String getUID() {
  String uid = "";
  for (byte i = 0; i < mfrc522.uid.size; i++) {
    if (mfrc522.uid.uidByte[i] < 0x10) uid += "0";
    uid += String(mfrc522.uid.uidByte[i], HEX);
  }
  uid.toUpperCase();
  return uid;
}

String urlEncode(String str) {
  String out = "";
  const char* hex = "0123456789ABCDEF";
  for (unsigned int i = 0; i < str.length(); i++) {
    char c = str.charAt(i);
    if (isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') out += c;
    else { out += '%'; out += hex[(c >> 4) & 0x0F]; out += hex[c & 0x0F]; }
  }
  return out;
}

void beep(int onMs, int offMs, int times) {
  for (int i = 0; i < times; i++) {
    digitalWrite(BUZZER_PIN, HIGH); delay(onMs);
    digitalWrite(BUZZER_PIN, LOW);  delay(offMs);
  }
}
void successBeep() { beep(150, 0, 1); }
void errorBeep()   { beep(100, 100, 3); }
void deniedBeep()  { beep(700, 200, 3); }          // NOT AUTHORISED for this zone

// Names only for the Serial Monitor - the laptop's worker list decides who the card belongs to.
String localName(String uid) {
  if (uid == "C32CA720") return "Anuska";
  if (uid == "52504B5C") return "Bibhu";
  if (uid == "7366A320") return "Chinmaya";
  return "";
}

void handleReply(String response) {
  response.trim();
  Serial.print("Server Response: "); Serial.println(response);
  if (response.indexOf("ACCESS DENIED") >= 0) { Serial.println(">>> NOT AUTHORISED FOR THIS ZONE <<<"); deniedBeep(); }
  else if (response.indexOf("STUDENT ENTERED") >= 0) { Serial.println(">>> ENTERED <<<"); successBeep(); }
  else if (response.indexOf("STUDENT EXITED") >= 0) { Serial.println(">>> EXITED <<<"); successBeep(); }
  else if (response.indexOf("DUPLICATE") >= 0) { Serial.println("(card held at the reader - ignored)"); }
  else { Serial.println("Unregistered card or error."); errorBeep(); }
}

// 1st choice: the NETRAX laptop (plain HTTP on the local Wi-Fi)
bool sendToLaptop(String uid, String name) {
  String url = String(SERVER) + "/api/hw/rfid?uid=" + urlEncode(uid) + "&device_id=" + urlEncode(DEVICE_ID);
  if (name.length()) url += "&name=" + urlEncode(name);
  Serial.println("Laptop: " + url);
  HTTPClient http;
  if (!http.begin(url)) return false;
  http.setTimeout(4000);
  int code = http.GET();
  Serial.printf("HTTP Response Code: %d\n", code);
  if (code <= 0) { Serial.println("Laptop not reachable: " + http.errorToString(code)); http.end(); return false; }
  handleReply(http.getString());
  http.end();
  return true;
}

// Fallback: Google Sheets directly (old behaviour) when the laptop is off / unreachable
void sendToGoogle(String uid, String name) {
  String url = String(scriptURL) + "?name=" + urlEncode(name.length() ? name : String("UNKNOWN")) + "&uid=" + urlEncode(uid);
  Serial.println("Fallback to Google Sheets: " + url);
  WiFiClientSecure client; client.setInsecure();
  HTTPClient http;
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
  if (!http.begin(client, url)) { errorBeep(); return; }
  http.setTimeout(15000);
  int code = http.GET();
  Serial.printf("HTTP Response Code: %d\n", code);
  if (code > 0) handleReply(http.getString()); else { Serial.println(http.errorToString(code)); errorBeep(); }
  http.end();
}

// Boot check: tells you at once whether scans will reach the NETRAX dashboard or only Google Sheets.
void checkLaptop() {
  HTTPClient http;
  String url = String(SERVER) + "/api/hw/rfid/ping?device_id=" + urlEncode(DEVICE_ID);
  if (!http.begin(url)) { Serial.println("LAPTOP CHECK: bad SERVER address"); return; }
  http.setTimeout(4000);
  int code = http.GET();
  if (code == 200) Serial.println("LAPTOP CHECK: OK - scans will show on the NETRAX dashboard");
  else {
    Serial.printf("LAPTOP CHECK: FAILED (%d %s)\n", code, http.errorToString(code).c_str());
    Serial.println("  -> is the backend running? same Wi-Fi? SERVER = laptop's Wi-Fi IPv4 (ipconfig)?");
    Serial.println("  -> until fixed, scans go ONLY to Google Sheets (fallback)");
  }
  http.end();
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, LOW);
  SPI.begin(18, 19, 23, 5);
  mfrc522.PCD_Init();
  delay(500);
  Serial.println("\n=== NETRAX RFID reader " + String(DEVICE_ID) + " ===");
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Connecting to WiFi");
  for (int i = 0; i < 30 && WiFi.status() != WL_CONNECTED; i++) { delay(500); Serial.print("."); }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) { Serial.print("WiFi Connected! ESP32 IP: "); Serial.println(WiFi.localIP()); }
  else Serial.println("WiFi Connection Failed!");
  checkLaptop();
  Serial.println("Laptop: " + String(SERVER) + "   Place RFID card near reader...");
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi disconnected. Reconnecting...");
    WiFi.reconnect();
    delay(3000);
    return;
  }
  if (!mfrc522.PICC_IsNewCardPresent() || !mfrc522.PICC_ReadCardSerial()) return;

  String uid = getUID();
  String name = localName(uid);
  Serial.println("\n--------------------------------");
  Serial.println("Card UID: " + uid + (name.length() ? "  (" + name + ")" : ""));

  if (!sendToLaptop(uid, name)) sendToGoogle(uid, name);   // every card goes out; the laptop decides

  mfrc522.PICC_HaltA();
  mfrc522.PCD_StopCrypto1();
  Serial.println("Remove card...");
  delay(1500);
  Serial.println("Ready for next card.");
}
