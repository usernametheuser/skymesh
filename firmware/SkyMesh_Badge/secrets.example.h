// SkyMesh_Badge (ESP32-S3-CAM wearable) settings.
// Copy to secrets.h (same folder) and fill in. secrets.h is git-ignored.
// Credentials come from:  python backend/skymesh.py enroll-device --type badge --name "Badge 01" --owner <username>
#pragma once

#define WIFI_SSID            "CHANGE_ME"
#define WIFI_PASSWORD        "CHANGE_ME"
#define BACKEND_URL          "http://192.168.1.10:8000"   // no trailing slash
#define SKYMESH_DEVICE_ID    "badge_CHANGE_ME"
#define SKYMESH_DEVICE_TOKEN "smd_CHANGE_ME"

// Optional: backend certificate PEM when BACKEND_URL is https://
// #define BACKEND_CA_CERT "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n"

// Optional NTP server for real timestamps (e.g. your router or the backend host). Without it the
// backend timestamps events on receipt, which is the safe default on an offline LAN.
// #define NTP_SERVER "192.168.1.10"
