// Copy this file to secrets.h (same folder) and fill in your Wi-Fi. secrets.h is git-ignored.
#pragma once
#define SECRET_WIFI_SSID     "YOUR_WIFI_NAME"
#define SECRET_WIFI_PASSWORD "YOUR_WIFI_PASSWORD"
// NETRAX laptop on the SAME Wi-Fi: http://<laptop Wi-Fi IPv4>:8000  (python -m tools.lan_setup --write fills it in)
#define SECRET_NETRAX_HOST   "http://192.168.1.10:8000"
