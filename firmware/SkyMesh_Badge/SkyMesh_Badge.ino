/*
  SkyMesh_Badge.ino — SkyMesh wearable badge
  Target: ESP32-S3-CAM development board (ESP32-S3, PSRAM, OV2640 socket)

  EASY WAY: SkyMesh → Devices → Add device → plug the badge in by USB → Install. The browser flashes the
  prebuilt firmware and stores Wi-Fi, server address and credentials on the chip. No files to edit.
  Arduino IDE (advanced): board "ESP32S3 Dev Module", Partition = Huge APP, USB CDC On Boot = Enabled,
  USB Mode = Hardware CDC and JTAG (the console then answers on both USB ports). secrets.h is optional.

  What it does
    - Authenticated heartbeat every 30 s (device token), with Wi-Fi RSSI, BSSID, heap, uptime,
      battery millivolts ONLY when a divider is fitted (BATTERY_ADC_PIN), and pending SOS ids.
    - SOS button: debounced long press (1.5 s), 10 s lockout, and no new alert while one is still
      unconfirmed. Each alert has a unique event id (boot counter + sequence) so retries are idempotent.
    - SOS delivery states, persisted in NVS so a reboot does not lose a queued alert:
        QUEUED    stored on the badge, not yet accepted by the backend
        SENT      backend stored the incident (HTTP 200/201 with incident id)
        CONFIRMED a responder acknowledged it (reported back via the heartbeat)
    - Optional BLE scan for SkyMesh anchors (name prefix filter) -> /api/v1/device/location.
      The backend turns readings into nearest-anchor / room-level / estimated positions, or
      "unavailable" — the badge never claims coordinates itself.
    - Watchdog, Wi-Fi reconnection, serial diagnostics (status | sos | clear | restart).

  It has no GPS. Without installed anchors the badge's position is shown as unavailable.
*/

#define SKY_ROLE         "badge"
#define FIRMWARE_VERSION "skymesh-badge-0.5.2"
#include "skymesh_common.h"
#include "badge_config.h"

#include <esp_task_wdt.h>
#include <esp_system.h>
#include <time.h>
#if ENABLE_BLE_SCAN
#include <BLEDevice.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>
#endif
#if ENABLE_BADGE_CAMERA
#include <esp_camera.h>
#include <img_converters.h>
static void requestPhoto(const char *reason, const String &eventId, const String &commandId);
static void takeAndSendPhoto();
static String g_cameraError = "no photo taken yet";
static String g_sensorName = "unknown";
#endif

#define HARDWARE_MODEL   "ESP32-S3-CAM"

static const uint32_t WDT_TIMEOUT_S        = 30;
static const uint16_t HTTP_TIMEOUT_MS      = 5000;
static const uint32_t WIFI_RESET_AFTER_MS  = 60UL * 1000;
static const uint32_t WIFI_REBOOT_AFTER_MS = 15UL * 60 * 1000;
static const uint8_t  MAX_EVENTS           = 8;

enum EvState : uint8_t { EV_EMPTY = 0, EV_QUEUED = 1, EV_SENT = 2, EV_CONFIRMED = 3 };
struct SosEvent {
  char     id[24];
  char     incident[28];
  uint8_t  state;
  uint8_t  attempts;
  uint32_t createdUptimeMs;
  uint32_t createdEpoch;  // 0 when the clock was not synced
};

static Preferences prefs;
static SosEvent  g_events[MAX_EVENTS];
static uint32_t  g_bootCount = 0, g_eventSeq = 0;
static uint32_t  g_hbIntervalMs = 30000, g_lastHb = 0, g_hbSeq = 0, g_locSeq = 0;
static uint32_t  g_nextSosTry = 0, g_sosBackoffMs = 2000;
static uint32_t  g_wifiDownSince = 0;
static uint32_t  g_lockoutUntil = 0, g_pressedSince = 0, g_confirmedAt = 0;
// Button timing: compiled defaults, replaced by the server's badge.sos_hold_ms / badge.sos_lockout_s settings.
static uint32_t  g_sosHoldMs = SOS_HOLD_MS, g_sosLockoutMs = SOS_LOCKOUT_MS;

static void applySosConfig(JsonDocument &r) {
  if (!r["sos_hold_ms"].isNull()) g_sosHoldMs = constrain((uint32_t)(r["sos_hold_ms"] | SOS_HOLD_MS), 200u, 10000u);
  if (!r["sos_lockout_s"].isNull()) g_sosLockoutMs = constrain((uint32_t)((r["sos_lockout_s"] | 0.0f) * 1000), 0u, 600000u);
}
static bool      g_pressHandled = false, g_lastRaw = false, g_stable = false;
static uint32_t  g_lastEdge = 0;
static int       g_lastHbHttp = 0;
static bool      g_resetReasonSent = false;
static bool      g_cameraOk = false;

// ---------------------------------------------------------------- persistence

static void saveEvents() { prefs.putBytes("events", g_events, sizeof(g_events)); }

static void loadState() {
  prefs.begin("skymesh", false);
  g_bootCount = prefs.getUInt("boot", 0) + 1;
  prefs.putUInt("boot", g_bootCount);
  memset(g_events, 0, sizeof(g_events));
  if (prefs.getBytesLength("events") == sizeof(g_events)) prefs.getBytes("events", g_events, sizeof(g_events));
  for (auto &e : g_events) {
    if (e.state == EV_CONFIRMED) e.state = EV_EMPTY;  // confirmed alerts need no further work
    e.createdUptimeMs = 0;                            // uptime restarted
  }
  saveEvents();
}

static int countState(uint8_t st) {
  int n = 0;
  for (auto &e : g_events) n += e.state == st;
  return n;
}

// ---------------------------------------------------------------- feedback (only on fitted parts)

static void outPin(int pin, bool on, bool activeLow = false) {
  if (pin < 0) return;
  digitalWrite(pin, (on ^ activeLow) ? HIGH : LOW);
}

static void pulse(int pin, uint16_t ms) {
  if (pin < 0) return;
  outPin(pin, true);
  delay(ms);
  outPin(pin, false);
}

static void updateLed() {
#if STATUS_LED_PIN >= 0
  uint32_t t = millis();
  bool on;
  if (countState(EV_QUEUED)) on = (t / 120) % 2;                       // fast blink: not delivered
  else if (countState(EV_SENT)) on = true;                             // solid: delivered, awaiting responder
  else if (g_confirmedAt && t - g_confirmedAt < 8000) on = (t / 400) % 2;  // slow blink: confirmed
  else on = WiFi.status() == WL_CONNECTED ? (t % 3000) < 60 : (t % 1000) < 500;  // heartbeat flash / offline
  outPin(STATUS_LED_PIN, on, STATUS_LED_ACTIVE_LOW);
#endif
}

// ---------------------------------------------------------------- time

static uint32_t epochNow() {
  time_t now = time(nullptr);
  return now > 1700000000 ? (uint32_t)now : 0;  // 0 = clock not synced
}

static void isoTime(uint32_t epoch, char *out, size_t n) {
  time_t t = epoch;
  struct tm tm;
  gmtime_r(&t, &tm);
  strftime(out, n, "%Y-%m-%dT%H:%M:%SZ", &tm);
}

// ---------------------------------------------------------------- HTTP

static int postJson(const char *path, const String &body, String &response) {
  int code = skyPost(path, body, response, HTTP_TIMEOUT_MS);
  skyNoteResult(code);  // repeated network failures trigger a search for the server's new address
  return code;
}

// ---------------------------------------------------------------- SOS

// Every press is its own event. While an earlier alert is still open, SkyMesh re-alerts it
// (or opens a new one, per the server's sos.repeat_mode), so pressing again always reaches responders.
static void raiseSos() {
  int slot = -1;
  for (int i = 0; i < MAX_EVENTS && slot < 0; i++) if (g_events[i].state == EV_EMPTY) slot = i;
  for (int i = 0; i < MAX_EVENTS && slot < 0; i++) if (g_events[i].state == EV_CONFIRMED) slot = i;
  if (slot < 0) {
    // Every slot is busy: drop the oldest press that was already delivered (its incident stays open on the server).
    for (int i = 0; i < MAX_EVENTS; i++)
      if (g_events[i].state == EV_SENT && (slot < 0 || g_events[i].createdUptimeMs < g_events[slot].createdUptimeMs)) slot = i;
  }
  if (slot < 0) {
    Con.println("[sos] all queued alerts are still undelivered; this press is merged with them");
    pulse(VIBRATION_PIN, 80);
    return;
  }
  SosEvent &e = g_events[slot];
  memset(&e, 0, sizeof(e));
  snprintf(e.id, sizeof(e.id), "b%lu-s%lu", (unsigned long)g_bootCount, (unsigned long)++g_eventSeq);
  e.state = EV_QUEUED;
  e.createdUptimeMs = millis();
  e.createdEpoch = epochNow();
  saveEvents();
  Con.printf("[sos] QUEUED %s (not yet delivered)\n", e.id);
  pulse(VIBRATION_PIN, 300);
  pulse(BUZZER_PIN, 150);
  g_nextSosTry = 0;
  g_sosBackoffMs = 2000;
}

static void deliverSos() {
  if (WiFi.status() != WL_CONNECTED || millis() < g_nextSosTry) return;
  for (auto &e : g_events) {
    if (e.state != EV_QUEUED) continue;
    JsonDocument d;
    d["schema_version"] = 1;
    d["event_id"] = e.id;
    if (e.createdEpoch) {
      char ts[24];
      isoTime(e.createdEpoch, ts, sizeof(ts));
      d["timestamp"] = ts;
    }
    if (e.createdUptimeMs) d["uptime_ms"] = e.createdUptimeMs;
    String body, resp;
    serializeJson(d, body);
    int code = postJson("/api/v1/device/sos", body, resp);
    e.attempts++;
    if (code == 200 || code == 201) {
      JsonDocument r;
      if (deserializeJson(r, resp) == DeserializationError::Ok) {
        strlcpy(e.incident, r["incident_id"] | "", sizeof(e.incident));
        applySosConfig(r);
        if (r["repeated"] | false) Con.printf("[sos] %s re-alerted the open incident %s\n", e.id, e.incident);
        e.state = (r["confirmed"] | false) ? EV_CONFIRMED : EV_SENT;
        if (e.state == EV_CONFIRMED) g_confirmedAt = millis();
#if ENABLE_BADGE_CAMERA
        if (r["photo_wanted"] | false) requestPhoto("sos", String(e.id), "");  // a picture of the scene for responders
#endif
      } else {
        e.state = EV_SENT;
      }
      Con.printf("[sos] %s %s (incident %s)\n", e.id, e.state == EV_CONFIRMED ? "CONFIRMED" : "SENT", e.incident);
      pulse(BUZZER_PIN, 60);
      g_sosBackoffMs = 2000;
    } else {
      // Network errors and 5xx/429: keep QUEUED and retry with backoff. 401/403: credentials problem.
      Con.printf("[sos] %s NOT DELIVERED (http %d, attempt %u)%s\n", e.id, code, e.attempts,
                    code == 401 ? " - token rejected: run Add device again" : "");
      g_nextSosTry = millis() + g_sosBackoffMs;
      g_sosBackoffMs = min<uint32_t>(g_sosBackoffMs * 2, 30000);
    }
    saveEvents();
    return;  // one attempt per loop pass keeps the button and watchdog responsive
  }
}

static void pollButton() {
  bool raw = digitalRead(SOS_BUTTON_PIN) == (SOS_BUTTON_ACTIVE_LOW ? LOW : HIGH);
  uint32_t now = millis();
  if (raw != g_lastRaw) { g_lastRaw = raw; g_lastEdge = now; }
  if (now - g_lastEdge < 30) return;  // 30 ms debounce
  if (raw != g_stable) {
    g_stable = raw;
    if (raw) { g_pressedSince = now; g_pressHandled = false; }
  }
  if (g_stable && !g_pressHandled && now - g_pressedSince >= g_sosHoldMs) {
    g_pressHandled = true;  // one alert per press, however long it is held
    if ((int32_t)(g_lockoutUntil - now) > 0) {
      Con.printf("[sos] press ignored: wait %lu s between alerts\n", (unsigned long)((g_lockoutUntil - now + 999) / 1000));
    } else {
      g_lockoutUntil = now + g_sosLockoutMs;
      raiseSos();
    }
  }
}

// ---------------------------------------------------------------- heartbeat

static void sendHeartbeat() {
  JsonDocument d;
  d["schema_version"] = 1;
  d["firmware_version"] = FIRMWARE_VERSION;
  d["hardware_model"] = HARDWARE_MODEL;
  d["uptime_s"] = millis() / 1000;
  d["sequence"] = ++g_hbSeq;
  d["ip"] = WiFi.localIP().toString();
  d["rssi"] = WiFi.RSSI();
  d["bssid"] = WiFi.BSSIDstr();
  d["free_heap"] = ESP.getFreeHeap();
  if (psramFound()) d["psram_free"] = ESP.getFreePsram();
  if (!g_resetReasonSent) d["reset_reason"] = String((int)esp_reset_reason());  // the API field is text
#if BATTERY_ADC_PIN >= 0
  d["battery_mv"] = (int)(analogReadMilliVolts(BATTERY_ADC_PIN) * BATTERY_DIVIDER_RATIO);
#endif
  JsonObject caps = d["capabilities"].to<JsonObject>();
  caps["sos_button"] = true;
  caps["ble_scan"] = (bool)ENABLE_BLE_SCAN;
  caps["wifi_scan"] = (bool)ENABLE_BLE_SCAN;  // AP fingerprints for the map trainer ride on the same scan
  caps["battery_sense"] = BATTERY_ADC_PIN >= 0;
  caps["camera"] = (bool)ENABLE_BADGE_CAMERA;
#if ENABLE_BADGE_CAMERA
  caps["camera_sensor"] = g_sensorName;
  if (g_cameraError.length()) caps["camera_error"] = g_cameraError;
#endif
  caps["led"] = STATUS_LED_PIN >= 0;
  caps["clock_synced"] = epochNow() != 0;
  JsonArray pend = d["pending_sos"].to<JsonArray>();
  for (auto &e : g_events) if (e.state == EV_SENT) pend.add(e.id);
  String body, resp;
  serializeJson(d, body);
  int code = postJson("/api/v1/device/heartbeat", body, resp);
  g_lastHbHttp = code;
  if (code != 200) {
    Con.printf("[hb] failed: http %d%s\n", code, code == 401 ? " — token rejected" : "");
    return;
  }
  g_resetReasonSent = true;
  JsonDocument r;
  if (deserializeJson(r, resp) != DeserializationError::Ok) return;
  g_hbIntervalMs = constrain((uint32_t)(r["heartbeat_interval_s"] | 30), 5u, 300u) * 1000UL;
  applySosConfig(r);
#if ENABLE_BADGE_CAMERA
  for (JsonObject c : r["commands"].as<JsonArray>()) {
    if (strcmp(c["cmd"] | "", "photo") == 0) requestPhoto("request", "", String((const char *)(c["id"] | "")));
  }
#endif
  bool changed = false;
  for (JsonObject s : r["sos"].as<JsonArray>()) {
    const char *id = s["event_id"] | "";
    for (auto &e : g_events) {
      if (e.state == EV_SENT && strcmp(e.id, id) == 0 && (s["confirmed"] | false)) {
        e.state = EV_CONFIRMED;
        g_confirmedAt = millis();
        changed = true;
        Con.printf("[sos] %s CONFIRMED by a responder (%s)\n", e.id, (const char *)(s["state"] | "?"));
        pulse(VIBRATION_PIN, 150); delay(100); pulse(VIBRATION_PIN, 150);
      }
    }
  }
  if (changed) saveEvents();
}

// ---------------------------------------------------------------- BLE positioning

#if ENABLE_BLE_SCAN
struct BleHit { char mac[18]; int rssi; };
static BleHit   g_hits[BLE_MAX_REPORTED];
static volatile int  g_hitCount = 0;
static volatile bool g_scanDone = false, g_scanning = false;
static uint32_t g_lastScan = 0;
static BLEScan *g_scan = nullptr;

static void onScanComplete(BLEScanResults results) {
  int n = 0;
  for (int i = 0; i < results.getCount() && n < BLE_MAX_REPORTED; i++) {
    BLEAdvertisedDevice dev = results.getDevice(i);
    if (!dev.haveName()) continue;
    String name = dev.getName().c_str();
    if (!name.startsWith(BLE_ANCHOR_PREFIX)) continue;  // privacy: report only SkyMesh anchors
    strlcpy(g_hits[n].mac, dev.getAddress().toString().c_str(), sizeof(g_hits[n].mac));
    g_hits[n].rssi = dev.getRSSI();
    n++;
  }
  g_hitCount = n;
  g_scanning = false;
  g_scanDone = true;
}

// Camera nodes announce themselves as Wi-Fi beacons ("SKYMESH-A-CAM-xxxx"): scan for those too.
static bool g_wifiScanStarted = false;

// Map trainer: the backend asks for faster scans (3 s) while someone is recording "I'm here" with this badge.
static uint32_t g_scanIntervalMs = BLE_SCAN_INTERVAL_MS;

static void bleTick() {
  if (!g_scanning && millis() - g_lastScan >= g_scanIntervalMs) {
    g_lastScan = millis();
    g_scanning = true;
    g_scan->clearResults();
    g_scan->start(BLE_SCAN_SECONDS, onScanComplete, false);
    if (WiFi.status() == WL_CONNECTED) {
      WiFi.scanNetworks(true, false, true, 120);  // async, passive, 120 ms per channel: the connection stays up
      g_wifiScanStarted = true;
    }
  }
  if (g_scanDone && g_wifiScanStarted && WiFi.scanComplete() == WIFI_SCAN_RUNNING) return;  // wait for both scans
  if (g_scanDone && WiFi.status() == WL_CONNECTED) {
    g_scanDone = false;
    int wifiHits = 0;
    int n = g_wifiScanStarted ? WiFi.scanComplete() : -1;
    JsonDocument d;
    d["schema_version"] = 1;
    d["kind"] = "ble";
    d["sequence"] = ++g_locSeq;
    if (epochNow()) { char ts[24]; isoTime(epochNow(), ts, sizeof(ts)); d["timestamp"] = ts; }
    // Access points heard, for map-trainer fingerprints: BSSID + signal only, never network names.
    // The strongest WIFI_MAX_REPORTED are sent; the backend drops randomised (phone hotspot) addresses.
    JsonArray aps = d["wifi"].to<JsonArray>();
    bool used[64] = {false};
    for (int k = 0; k < WIFI_MAX_REPORTED && n > 0; k++) {
      int best = -1;
      for (int i = 0; i < n && i < 64; i++) if (!used[i] && (best < 0 || WiFi.RSSI(i) > WiFi.RSSI(best))) best = i;
      if (best < 0) break;
      used[best] = true;
      String b = WiFi.BSSIDstr(best);
      b.toLowerCase();
      JsonObject o = aps.add<JsonObject>();
      o["id"] = b;
      o["rssi"] = WiFi.RSSI(best);
    }
    for (int i = 0; i < n && g_hitCount < BLE_MAX_REPORTED; i++) {
      if (!WiFi.SSID(i).startsWith(BLE_ANCHOR_PREFIX)) continue;  // camera beacons also count as anchors
      String b = WiFi.BSSIDstr(i);
      b.toLowerCase();
      strlcpy(g_hits[g_hitCount].mac, b.c_str(), sizeof(g_hits[0].mac));
      g_hits[g_hitCount].rssi = WiFi.RSSI(i);
      g_hitCount++;
      wifiHits++;
    }
    if (g_wifiScanStarted) WiFi.scanDelete();
    g_wifiScanStarted = false;
    JsonArray arr = d["ble"].to<JsonArray>();
    for (int i = 0; i < g_hitCount; i++) {
      JsonObject o = arr.add<JsonObject>();
      o["id"] = g_hits[i].mac;
      o["rssi"] = g_hits[i].rssi;
    }
    String body, resp;
    serializeJson(d, body);
    int code = postJson("/api/v1/device/location", body, resp);
    JsonDocument r;
    if (code == 200 && deserializeJson(r, resp) == DeserializationError::Ok) {
      g_scanIntervalMs = constrain((uint32_t)(r["scan_interval_s"] | (BLE_SCAN_INTERVAL_MS / 1000)), 3u, 60u) * 1000UL;
      if (!r["training"].isNull()) {
        Con.printf("[train] spot sample %d/%d%s\n", (int)(r["training"]["samples"] | 0), (int)(r["training"]["target"] | 0),
                   (r["training"]["done"] | false) ? " - done" : "");
        pulse(VIBRATION_PIN, 60);
      }
    }
    Con.printf("[pos] %d AP(s), %d beacon(s) (%d camera), %s, http %d\n", (int)aps.size(), (int)g_hitCount, wifiHits,
               (const char *)(r["source"] | "-"), code);
  }
}
#endif

// ---------------------------------------------------------------- optional camera

#if ENABLE_BADGE_CAMERA
// Photos: the camera is started only to take one picture, then stopped (battery, heat, privacy).
static bool g_photoPending = false;
static String g_photoReason, g_photoEvent, g_photoCommand;

static camera_config_t badgeCameraConfig(pixformat_t fmt, framesize_t size) {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0; c.ledc_timer = LEDC_TIMER_0;
  c.pin_d0 = CAM_PIN_D0; c.pin_d1 = CAM_PIN_D1; c.pin_d2 = CAM_PIN_D2; c.pin_d3 = CAM_PIN_D3;
  c.pin_d4 = CAM_PIN_D4; c.pin_d5 = CAM_PIN_D5; c.pin_d6 = CAM_PIN_D6; c.pin_d7 = CAM_PIN_D7;
  c.pin_xclk = CAM_PIN_XCLK; c.pin_pclk = CAM_PIN_PCLK; c.pin_vsync = CAM_PIN_VSYNC; c.pin_href = CAM_PIN_HREF;
  c.pin_sccb_sda = CAM_PIN_SIOD; c.pin_sccb_scl = CAM_PIN_SIOC; c.pin_pwdn = CAM_PIN_PWDN; c.pin_reset = CAM_PIN_RESET;
  c.xclk_freq_hz = fmt == PIXFORMAT_JPEG ? 20000000 : 10000000;  // GC2145 is steadier at 10 MHz
  c.pixel_format = fmt; c.frame_size = size; c.jpeg_quality = 12;
  c.fb_count = 1; c.fb_location = psramFound() ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM; c.grab_mode = CAMERA_GRAB_LATEST;
  return c;
}

static bool cameraStart() {
  framesize_t big = psramFound() ? FRAMESIZE_VGA : FRAMESIZE_QVGA;
  camera_config_t c = badgeCameraConfig(PIXFORMAT_JPEG, big);
  esp_err_t err = esp_camera_init(&c);
  if (err == ESP_ERR_NOT_SUPPORTED) {  // GC2145 and friends: no JPEG mode in the sensor
    esp_camera_deinit();
    c = badgeCameraConfig(PIXFORMAT_RGB565, big);
    err = esp_camera_init(&c);
  }
  if (err != ESP_OK) {
    esp_camera_deinit();
    g_cameraError = String("camera init failed: 0x") + String(err, HEX) + " (check the camera pins in badge_config.h)";
    return false;
  }
  sensor_t *s = esp_camera_sensor_get();
  if (s) {
    camera_sensor_info_t *info = esp_camera_sensor_get_info(&s->id);
    g_sensorName = info ? info->name : String("PID 0x") + String(s->id.PID, HEX);
  }
  return true;
}

static void requestPhoto(const char *reason, const String &eventId, const String &commandId) {
  g_photoPending = true;
  g_photoReason = reason;
  g_photoEvent = eventId;
  g_photoCommand = commandId;
}

static void takeAndSendPhoto() {
  if (!g_photoPending || WiFi.status() != WL_CONNECTED) return;
  g_photoPending = false;
  Con.printf("[cam] taking a photo (%s)\n", g_photoReason.c_str());
  esp_task_wdt_reset();
  if (!cameraStart()) { Con.printf("[cam] %s\n", g_cameraError.c_str()); return; }
  for (int i = 0; i < 4; i++) {  // let auto-exposure settle on the first frames
    camera_fb_t *warm = esp_camera_fb_get();
    if (warm) esp_camera_fb_return(warm);
  }
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) { g_cameraError = "frame capture failed"; esp_camera_deinit(); return; }
  uint8_t *jpg = fb->buf;
  size_t len = fb->len;
  bool owned = false;
  if (fb->format != PIXFORMAT_JPEG) {
    owned = frame2jpg(fb, 80, &jpg, &len);
    if (!owned) { g_cameraError = "JPEG encoding failed"; esp_camera_fb_return(fb); esp_camera_deinit(); return; }
  }
  esp_task_wdt_reset();
  String q = "?reason=" + g_photoReason;
  if (g_photoEvent.length()) q += "&event_id=" + g_photoEvent;
  if (g_photoCommand.length()) q += "&command_id=" + g_photoCommand;
  String resp;
  int code = skyPostBinary("/api/v1/device/photo", q, jpg, len, "image/jpeg", resp);
  Con.printf("[cam] photo %u bytes (%s) sent: http %d\n", (unsigned)len, g_sensorName.c_str(), code);
  g_cameraError = code == 201 ? "" : String("upload failed: http ") + code;
  if (owned) free(jpg);
  esp_camera_fb_return(fb);
  esp_camera_deinit();  // camera off again
}
#endif

// ---------------------------------------------------------------- Wi-Fi & serial

static void maintainWifi() {
  if (WiFi.status() == WL_CONNECTED) { g_wifiDownSince = 0; return; }
  uint32_t now = millis();
  if (!g_wifiDownSince) { g_wifiDownSince = now; Con.println("[wifi] disconnected"); return; }
  static uint32_t lastKick = 0;
  if (now - g_wifiDownSince > WIFI_RESET_AFTER_MS && now - lastKick > WIFI_RESET_AFTER_MS) {
    lastKick = now;
    WiFi.disconnect();
    WiFi.begin(Cfg.ssid.c_str(), Cfg.pass.c_str());
  }
  // Queued SOS events are in NVS, so a reboot does not lose them.
  if (now - g_wifiDownSince > WIFI_REBOOT_AFTER_MS) ESP.restart();
}

static void printStatus() {
  Con.printf("id=%s fw=%s server=%s boot=%lu wifi=%s ip=%s rssi=%d last_hb_http=%d clock=%s\n", Cfg.id.c_str(), FIRMWARE_VERSION, Cfg.url.c_str(),
                (unsigned long)g_bootCount, WiFi.status() == WL_CONNECTED ? "up" : "down", WiFi.localIP().toString().c_str(),
                WiFi.RSSI(), g_lastHbHttp, epochNow() ? "synced" : "not synced");
  static const char *names[] = {"EMPTY", "QUEUED", "SENT", "CONFIRMED"};
  for (auto &e : g_events) if (e.state != EV_EMPTY) Con.printf("  sos %s %s attempts=%u incident=%s\n", e.id, names[e.state], e.attempts, e.incident);
#if BATTERY_ADC_PIN >= 0
  Con.printf("  battery %d mV\n", (int)(analogReadMilliVolts(BATTERY_ADC_PIN) * BATTERY_DIVIDER_RATIO));
#else
  Con.println("  battery: not measured (no divider configured)");
#endif
}

static void handleCommand(const String &cmd) {
  if (cmd == "status") printStatus();
  else if (cmd == "sos") raiseSos();  // bench test without the button
#if ENABLE_BADGE_CAMERA
  else if (cmd == "photo") requestPhoto("test", "", "");
#endif
  else if (cmd == "clear") { memset(g_events, 0, sizeof(g_events)); saveEvents(); Con.println("[sos] local queue cleared"); }
  else if (cmd == "restart") ESP.restart();
  else if (cmd == "forget-wifi") { Con.println("[setup] erasing settings"); skyEraseConfig(); delay(200); ESP.restart(); }
  else Con.println("commands: status | sos | clear | restart | forget-wifi");
}

// ---------------------------------------------------------------- setup / loop

void setup() {
  Con.begin();
  delay(300);
  skyLoadConfig();
  skyCheckTripleReset();
  Con.printf("\nSkyMesh badge %s (%s), reset reason %d\n", FIRMWARE_VERSION, Cfg.id.c_str(), (int)esp_reset_reason());
  skyPrintInfo("SKYMESH-READY");

  esp_task_wdt_config_t wdt = {.timeout_ms = WDT_TIMEOUT_S * 1000, .idle_core_mask = 0, .trigger_panic = true};
  if (esp_task_wdt_reconfigure(&wdt) != ESP_OK) esp_task_wdt_init(&wdt);
  esp_task_wdt_add(NULL);

  pinMode(SOS_BUTTON_PIN, SOS_BUTTON_ACTIVE_LOW ? INPUT_PULLUP : INPUT_PULLDOWN);
  if (STATUS_LED_PIN >= 0) pinMode(STATUS_LED_PIN, OUTPUT);
  if (BUZZER_PIN >= 0) { pinMode(BUZZER_PIN, OUTPUT); digitalWrite(BUZZER_PIN, LOW); }
  if (VIBRATION_PIN >= 0) { pinMode(VIBRATION_PIN, OUTPUT); digitalWrite(VIBRATION_PIN, LOW); }
#if BATTERY_ADC_PIN >= 0
  analogSetPinAttenuation(BATTERY_ADC_PIN, ADC_11db);
#endif

  loadState();
  if (countState(EV_QUEUED) || countState(EV_SENT)) Con.printf("[sos] %d undelivered/unconfirmed alert(s) restored from flash\n",
                                                                 countState(EV_QUEUED) + countState(EV_SENT));
  if (!Cfg.valid()) {
    Con.println("[setup] not set up yet: open SkyMesh -> Devices -> Add device, and keep this badge plugged in by USB");
    return;  // loop() only listens for the setup command until then
  }
  skyWifiBegin();
#ifdef NTP_SERVER
  configTime(0, 0, NTP_SERVER);
#endif
#if ENABLE_BLE_SCAN
  BLEDevice::init("");  // no advertising name: the badge does not broadcast an identity
  g_scan = BLEDevice::getScan();
  g_scan->setActiveScan(false);  // passive: lower power, nothing transmitted
  g_scan->setInterval(160);
  g_scan->setWindow(80);
#endif
}

void loop() {
  esp_task_wdt_reset();
  skyPollConsole(handleCommand);
  skyClearTripleResetAfterBoot();
  if (!Cfg.valid()) {
    // Unconfigured: slow double blink, waiting for the Add-device wizard.
#if STATUS_LED_PIN >= 0
    uint32_t t = millis() % 2000;
    outPin(STATUS_LED_PIN, t < 100 || (t > 250 && t < 350), STATUS_LED_ACTIVE_LOW);
#endif
    delay(10);
    return;
  }
  pollButton();
  maintainWifi();
  deliverSos();
#if ENABLE_BADGE_CAMERA
  takeAndSendPhoto();  // after the SOS itself has gone out: the alert never waits for the picture
#endif
  if (WiFi.status() == WL_CONNECTED && (!g_lastHb || millis() - g_lastHb >= g_hbIntervalMs || (countState(EV_SENT) && millis() - g_lastHb >= 5000))) {
    // Poll faster while an alert awaits confirmation so the wearer gets feedback quickly.
    g_lastHb = millis();
    sendHeartbeat();
  }
#if ENABLE_BLE_SCAN
  bleTick();
#endif
  updateLed();
  delay(10);
}
