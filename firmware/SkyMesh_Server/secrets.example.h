// SkyMesh_Server (ESP32-CAM node) settings.
// Copy this file to secrets.h (same folder) and fill in real values. secrets.h is git-ignored.
// Credentials come from:  python backend/skymesh.py enroll-device --type camera_node --name "..."
#pragma once

#define WIFI_SSID            "CHANGE_ME"
#define WIFI_PASSWORD        "CHANGE_ME"

// Base URL of the SkyMesh backend on the LAN (laptop / Raspberry Pi), no trailing slash.
// http:// works on a closed LAN; https:// requires BACKEND_CA_CERT below.
#define BACKEND_URL          "http://192.168.1.10:8000"

#define SKYMESH_DEVICE_ID    "cam_CHANGE_ME"
#define SKYMESH_DEVICE_TOKEN "smd_CHANGE_ME"
// Shared key the backend presents when it fetches /capture. Also required for /stream and /control.
#define SKYMESH_CAMERA_KEY   "CHANGE_ME"

// Optional: PEM of the backend certificate (backend/data/tls/cert.pem) when BACKEND_URL is https://
// #define BACKEND_CA_CERT "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n"
