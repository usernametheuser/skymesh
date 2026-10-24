// Hardware options for the SkyMesh wearable badge. Pins marked UNVERIFIED must be checked against
// YOUR board's schematic before wiring anything (see firmware/README.md and docs/wiring.md).
#pragma once

// ---- SOS button -------------------------------------------------------------------------------
// GPIO0 is the BOOT button present on ESP32-S3 dev boards, so SOS can be tested with no wiring.
// For a real badge, wire a momentary button from a free GPIO to GND and set the pin here
// (GPIO14 is free on common ESP32-S3-CAM N16R8 boards — UNVERIFIED for your board).
// Do not hold GPIO0 while pressing RESET: that enters download mode.
#define SOS_BUTTON_PIN        0
#define SOS_BUTTON_ACTIVE_LOW 1
// Defaults only: SkyMesh sends the live values (Administration -> Settings -> badge.sos_hold_ms / badge.sos_lockout_s).
#define SOS_HOLD_MS           1500   // long press required: avoids accidental alerts
#define SOS_LOCKOUT_MS        5000   // minimum gap between two alerts

// ---- Feedback (optional; -1 = not fitted) ------------------------------------------------------
// Many ESP32-S3-CAM boards have a WS2812 RGB LED on GPIO48 rather than a plain LED: a plain
// digitalWrite will not drive it. Only set STATUS_LED_PIN for a plain LED you have wired.
#define STATUS_LED_PIN        -1
#define STATUS_LED_ACTIVE_LOW 0
#define BUZZER_PIN            -1     // active buzzer via transistor, never directly on a GPIO if >20 mA
#define VIBRATION_PIN         -1     // vibration motor via transistor + flyback diode

// ---- Battery measurement (optional) ----------------------------------------------------------
// Requires a resistor divider from the battery + to an ADC1 pin (e.g. 100k/100k -> ratio 2.0).
// Leave -1 if no divider is fitted: the badge then reports NO battery value (never a made-up one).
#define BATTERY_ADC_PIN       -1
#define BATTERY_DIVIDER_RATIO 2.0f

// ---- Positioning ------------------------------------------------------------------------------
// BLE scan for SkyMesh anchors. Only devices whose advertised name starts with this prefix are
// reported, so nearby phones/laptops are never sent to the backend.
#define ENABLE_BLE_SCAN       1
#define BLE_ANCHOR_PREFIX     "SKYMESH-A"
#define BLE_SCAN_SECONDS      2
#define BLE_SCAN_INTERVAL_MS  10000
#define BLE_MAX_REPORTED      8
// Map trainer: Wi-Fi access points heard (BSSID + signal, no names) are sent with each location report
// so the backend can match them against spots trained with "I'm here".
#define WIFI_MAX_REPORTED     20

// ---- Camera (photos with SOS / on a responder's request) ------------------------------------
// The camera is powered up only to take a photo, then switched off again. Works with JPEG sensors
// (OV2640) and RGB565-only sensors such as the GC2145 (encoded to JPEG on the chip).
// Pin map below is the common ESP32-S3-CAM (ESP32-S3-WROOM N16R8 / S3R8) layout, identical to
// Espressif's ESP32-S3-EYE map — UNVERIFIED: the badge reports a camera error if it does not match.
#ifndef ENABLE_BADGE_CAMERA
#define ENABLE_BADGE_CAMERA   1
#endif
#define CAM_PIN_PWDN  -1
#define CAM_PIN_RESET -1
#define CAM_PIN_XCLK  15
#define CAM_PIN_SIOD   4
#define CAM_PIN_SIOC   5
#define CAM_PIN_D7    16
#define CAM_PIN_D6    17
#define CAM_PIN_D5    18
#define CAM_PIN_D4    12
#define CAM_PIN_D3    10
#define CAM_PIN_D2     8
#define CAM_PIN_D1     9
#define CAM_PIN_D0    11
#define CAM_PIN_VSYNC  6
#define CAM_PIN_HREF   7
#define CAM_PIN_PCLK  13
