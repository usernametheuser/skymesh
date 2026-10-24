/*
  SkyMesh_Server.ino — SkyMesh camera / embedded server node
  Target: AI-Thinker ESP32-CAM (ESP32-D0WD, 4 MB flash, 4 MB PSRAM, OV2640)

  EASY WAY: SkyMesh → Devices → Add device → plug the ESP32-CAM-MB adapter in by USB → Install.
  The browser flashes the prebuilt firmware and stores Wi-Fi, server address and credentials on the chip.
  Arduino IDE (advanced): board "AI Thinker ESP32-CAM" (esp32:esp32:esp32cam), core esp32 3.x; secrets.h optional.

  What it does
    - Initialises the OV2640 with the AI-Thinker pin map; retries and reports failure honestly.
    - Joins Wi-Fi, reconnects on drop-outs, restarts only after a long outage.
    - Sends an authenticated heartbeat (device token) to the SkyMesh backend every 30 s.
    - Serves on port 80:
        GET /          plain-text node identity (no secrets)
        GET /status    JSON health (no secrets)
        GET /capture   one JPEG              — requires X-SkyMesh-Key header (or ?key=)
        GET /stream    MJPEG, one client     — requires key
        GET /control   ?cmd=framesize&val=vga|svga|xga|qvga, ?cmd=quality&val=10..40, ?cmd=restart — requires key
    - Serial console (115200): status | restart | help

  What it does NOT do
    - No person detection on the ESP32: frames are analysed on the backend computer.
    - It is not the SkyMesh application server (dashboard, database, chat run on the laptop / Pi).
*/

#define SKY_ROLE         "camera_node"
#define FIRMWARE_VERSION "skymesh-server-0.4.1"
#include "skymesh_common.h"

#include <esp_camera.h>
#include <img_converters.h>
#include <esp_http_server.h>
#include <esp_task_wdt.h>

#define HARDWARE_MODEL   "AI-Thinker ESP32-CAM"

// ---------------------------------------------------------------- AI-Thinker ESP32-CAM pin map (fixed by the board)
#define PWDN_GPIO_NUM   32
#define RESET_GPIO_NUM  -1
#define XCLK_GPIO_NUM    0
#define SIOD_GPIO_NUM   26
#define SIOC_GPIO_NUM   27
#define Y9_GPIO_NUM     35
#define Y8_GPIO_NUM     34
#define Y7_GPIO_NUM     39
#define Y6_GPIO_NUM     36
#define Y5_GPIO_NUM     21
#define Y4_GPIO_NUM     19
#define Y3_GPIO_NUM     18
#define Y2_GPIO_NUM      5
#define VSYNC_GPIO_NUM  25
#define HREF_GPIO_NUM   23
#define PCLK_GPIO_NUM   22
#define FLASH_LED_PIN    4   // high-power white LED; kept OFF
#define RED_LED_PIN     33   // small on-board LED, active LOW

static const uint32_t WDT_TIMEOUT_S        = 30;
static const uint32_t WIFI_RESET_AFTER_MS  = 60UL * 1000;       // re-begin Wi-Fi after 1 min down
static const uint32_t WIFI_REBOOT_AFTER_MS = 10UL * 60 * 1000;  // reboot after 10 min down
static const uint16_t HTTP_TIMEOUT_MS      = 5000;

static bool     g_cameraOk = false;
static String   g_cameraError = "not initialised";
static uint32_t g_heartbeatIntervalMs = 30000;
static uint32_t g_lastHeartbeat = 0;
static uint32_t g_heartbeatSeq = 0;
static int      g_lastHeartbeatHttp = 0;
static uint32_t g_wifiDownSince = 0;
static volatile bool g_streamBusy = false;
static httpd_handle_t g_httpd = nullptr;
static bool     g_resetReasonSent = false;

// ---------------------------------------------------------------- helpers

static const char *resetReasonText() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "power_on";
    case ESP_RST_SW:      return "software";
    case ESP_RST_PANIC:   return "panic";
    case ESP_RST_INT_WDT: return "int_watchdog";
    case ESP_RST_TASK_WDT:return "task_watchdog";
    case ESP_RST_WDT:     return "other_watchdog";
    case ESP_RST_BROWNOUT:return "brownout";
    case ESP_RST_DEEPSLEEP:return "deep_sleep";
    default:              return "other";
  }
}

// Constant-time comparison so the key cannot be guessed byte by byte from response timing.
static bool keyEquals(const char *a, const char *b) {
  size_t la = strlen(a), lb = strlen(b);
  uint8_t diff = la != lb;
  for (size_t i = 0; i < la && i < lb; i++) diff |= a[i] ^ b[i];
  return diff == 0 && lb > 0 && strcmp(b, "CHANGE_ME") != 0;
}

static bool authorised(httpd_req_t *req) {
  char buf[96];
  const char *key = Cfg.camkey.c_str();
  if (httpd_req_get_hdr_value_str(req, "X-SkyMesh-Key", buf, sizeof(buf)) == ESP_OK && keyEquals(buf, key)) return true;
  size_t qlen = httpd_req_get_url_query_len(req);
  if (qlen > 0 && qlen < 256) {
    char q[256];
    if (httpd_req_get_url_query_str(req, q, sizeof(q)) == ESP_OK &&
        httpd_query_key_value(q, "key", buf, sizeof(buf)) == ESP_OK && keyEquals(buf, key)) return true;
  }
  return false;
}

static esp_err_t deny(httpd_req_t *req) {
  httpd_resp_set_status(req, "401 Unauthorized");
  httpd_resp_set_type(req, "application/json");
  return httpd_resp_sendstr(req, "{\"error\":\"missing or wrong X-SkyMesh-Key\"}");
}

// ---------------------------------------------------------------- camera

// Sensors without a hardware JPEG encoder (GC0308, GC032A, OV7725, BF3005…) are shipped on many
// "ESP32-CAM" kits. For those the node captures RGB565 and encodes JPEG in software (slower, VGA max).
static bool g_softJpeg = false;
static String g_sensorName = "unknown";

static camera_config_t cameraConfig(pixformat_t fmt, framesize_t size) {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer = LEDC_TIMER_0;
  c.pin_d0 = Y2_GPIO_NUM; c.pin_d1 = Y3_GPIO_NUM; c.pin_d2 = Y4_GPIO_NUM; c.pin_d3 = Y5_GPIO_NUM;
  c.pin_d4 = Y6_GPIO_NUM; c.pin_d5 = Y7_GPIO_NUM; c.pin_d6 = Y8_GPIO_NUM; c.pin_d7 = Y9_GPIO_NUM;
  c.pin_xclk = XCLK_GPIO_NUM; c.pin_pclk = PCLK_GPIO_NUM; c.pin_vsync = VSYNC_GPIO_NUM; c.pin_href = HREF_GPIO_NUM;
  c.pin_sccb_sda = SIOD_GPIO_NUM; c.pin_sccb_scl = SIOC_GPIO_NUM;
  c.pin_pwdn = PWDN_GPIO_NUM; c.pin_reset = RESET_GPIO_NUM;
  // RGB565 sensors such as the GC2145 ("RHYX M21-45" modules) drop frames at 20 MHz on the ESP32's camera bus;
  // 10 MHz is steady and still gives ~15 fps at 320x240.
  c.xclk_freq_hz = fmt == PIXFORMAT_JPEG ? 20000000 : 10000000;
  c.pixel_format = fmt;
  c.frame_size = size;
  c.jpeg_quality = 30;  // 10 = best … 63 = worst: low quality keeps frames ~6-10 KB for 12-15 fps over Wi-Fi
  c.fb_count = psramFound() ? 2 : 1;
  c.fb_location = psramFound() ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM;
  c.grab_mode = c.fb_count > 1 ? CAMERA_GRAB_LATEST : CAMERA_GRAB_WHEN_EMPTY;
  return c;
}

static void powerCycleSensor() {
  pinMode(PWDN_GPIO_NUM, OUTPUT);
  digitalWrite(PWDN_GPIO_NUM, HIGH); delay(200);
  digitalWrite(PWDN_GPIO_NUM, LOW);  delay(200);
}

// ---------------------------------------------------------------- picture settings (remembered on the chip)

static int g_softQuality = 35;  // software-JPEG quality 1..100 (higher = better, bigger frames)

static framesize_t framesizeFromName(const String &n) {
  if (n == "qqvga") return FRAMESIZE_QQVGA;
  if (n == "qvga") return FRAMESIZE_QVGA;
  if (n == "vga") return FRAMESIZE_VGA;
  if (n == "svga") return FRAMESIZE_SVGA;
  return FRAMESIZE_INVALID;
}

static bool framesizeAllowed(framesize_t fs) {
  if (fs == FRAMESIZE_INVALID) return false;
  if (!psramFound()) return fs == FRAMESIZE_QQVGA || fs == FRAMESIZE_QVGA;
  if (g_softJpeg) return fs != FRAMESIZE_SVGA;  // RGB565 is 2 bytes/pixel and encoded in software
  return true;
}

static void applyImageSettings() {
  sensor_t *s = esp_camera_sensor_get();
  if (!s) return;
  if (s->set_hmirror) s->set_hmirror(s, skyPrefs.getBool("hmirror", false) ? 1 : 0);
  if (s->set_vflip) s->set_vflip(s, skyPrefs.getBool("vflip", false) ? 1 : 0);
  // Not every sensor implements every control (the GC2145 lacks some): call only what exists.
  if (s->set_brightness) s->set_brightness(s, skyPrefs.getInt("bright", 0));
  if (s->set_contrast) s->set_contrast(s, skyPrefs.getInt("contrast", 0));
  if (s->set_saturation) s->set_saturation(s, skyPrefs.getInt("sat", 0));
  int q = skyPrefs.getInt("quality", 35);  // 1..100, higher = better
  g_softQuality = q;
  if (!g_softJpeg && s->set_quality) s->set_quality(s, 63 - (q * 53) / 100);  // sensor scale: 10 best … 63 worst
  framesize_t fs = framesizeFromName(skyPrefs.getString("fs", ""));
  if (framesizeAllowed(fs) && s->set_framesize) s->set_framesize(s, fs);
}
static void applyOrientation() { applyImageSettings(); }

// ---------------------------------------------------------------- flash LED (GPIO4)
// Dimmable with PWM on LEDC channel 7 (timer 3): the camera clock uses channel 0 / timer 0.
// The LED is very bright and gets hot: SkyMesh caps it at 80% and it is only a fill light.
static const uint8_t FLASH_CHANNEL = 7;
static int g_flash = 0;  // 0..100 %

static void setFlash(int pct) {
  g_flash = constrain(pct, 0, 80);
  ledcWrite(FLASH_LED_PIN, (g_flash * 255) / 100);
}

static void initFlash() {
  ledcAttachChannel(FLASH_LED_PIN, 5000, 8, FLASH_CHANNEL);
  setFlash(skyPrefs.getInt("flash", 0));
}

static void noteSensor(const char *mode) {
  sensor_t *s = esp_camera_sensor_get();
  if (s) {
    camera_sensor_info_t *info = esp_camera_sensor_get_info(&s->id);
    g_sensorName = info ? info->name : String("PID 0x") + String(s->id.PID, HEX);
  }
  applyOrientation();
  Con.printf("[cam] sensor %s ready, %s (%s PSRAM)\n", g_sensorName.c_str(), mode, psramFound() ? "with" : "no");
}

static bool initCamera() {
  esp_err_t err = ESP_FAIL;
  for (int attempt = 1; attempt <= 3; attempt++) {
    camera_config_t c = cameraConfig(PIXFORMAT_JPEG, FRAMESIZE_QVGA);  // 320x240: minimal quality, high frame rate
    err = esp_camera_init(&c);
    if (err == ESP_OK) { noteSensor("hardware JPEG"); g_cameraError = ""; return true; }
    esp_camera_deinit();
    if (err == ESP_ERR_NOT_SUPPORTED) break;  // the sensor itself has no JPEG mode: retrying cannot help
    g_cameraError = String("esp_camera_init failed: 0x") + String(err, HEX);
    Con.printf("[cam] attempt %d: %s\n", attempt, g_cameraError.c_str());
    powerCycleSensor();
  }
  if (err == ESP_ERR_NOT_SUPPORTED) {
    Con.println("[cam] this sensor has no JPEG encoder: using RGB565 + software JPEG");
    const framesize_t sizes[] = {FRAMESIZE_QVGA, FRAMESIZE_QQVGA};  // software JPEG: keep frames small for 12-15 fps
    for (framesize_t fs : sizes) {
      camera_config_t c = cameraConfig(PIXFORMAT_RGB565, fs);
      err = esp_camera_init(&c);
      if (err == ESP_OK) {
        g_softJpeg = true;
        noteSensor(fs == FRAMESIZE_QVGA ? "RGB565 320x240, software JPEG" : "RGB565 160x120, software JPEG");
        g_cameraError = "";
        return true;
      }
      esp_camera_deinit();
      powerCycleSensor();
    }
    g_cameraError = String("sensor has no JPEG mode and RGB565 init failed: 0x") + String(err, HEX);
    Con.printf("[cam] %s\n", g_cameraError.c_str());
  }
  return false;
}

// One JPEG for the current frame; `owned` is true when `buf` must be freed (software-encoded).
static bool frameJpeg(camera_fb_t *fb, uint8_t **buf, size_t *len, bool *owned) {
  if (fb->format == PIXFORMAT_JPEG) { *buf = fb->buf; *len = fb->len; *owned = false; return true; }
  *owned = true;
  return frame2jpg(fb, g_softQuality, buf, len);  // default 35: small frames, fast encode
}

// ---------------------------------------------------------------- frame-rate bookkeeping

static const uint32_t STREAM_MIN_FRAME_MS = 66;  // cap at ~15 fps: enough for video, gentle on Wi-Fi
static volatile float g_streamFps = 0;
static volatile uint32_t g_lastFrameBytes = 0;

// ---------------------------------------------------------------- Wi-Fi beacon (lets badges locate themselves)
//
// Bluetooth + camera + Wi-Fi does not fit the ESP32's instruction RAM, so the camera announces itself with a
// tiny access point instead: SSID "SKYMESH-A-CAM-xxxx", WPA2 with a random password nobody knows (it is only
// a beacon, nothing can join). Badges hear it in their Wi-Fi scan and report its signal strength; SkyMesh
// uses the camera's map position as the anchor. The AP follows the channel of the main Wi-Fi connection.

#ifndef CAMERA_WIFI_BEACON
#define CAMERA_WIFI_BEACON 1
#endif
#if CAMERA_WIFI_BEACON
static String g_beaconId;
static void startBeacon() {
  uint8_t rnd[16];
  esp_fill_random(rnd, sizeof(rnd));
  String pass = skyHex(rnd, sizeof(rnd));  // 32 random hex characters, never stored or shown
  String mac = skyMac();
  String ssid = "SKYMESH-A-CAM-" + mac.substring(12, 14) + mac.substring(15, 17);
  WiFi.mode(WIFI_AP_STA);
  if (!WiFi.softAP(ssid.c_str(), pass.c_str(), 1, 0, 1)) {  // channel follows the station; 1 client max
    Con.println("[beacon] could not start the positioning beacon");
    return;
  }
  g_beaconId = WiFi.softAPmacAddress();
  g_beaconId.toLowerCase();
  Con.printf("[beacon] %s (%s): badges near this camera can be placed on the map\n", ssid.c_str(), g_beaconId.c_str());
}
#endif

// ---------------------------------------------------------------- HTTP handlers

static esp_err_t rootHandler(httpd_req_t *req) {
  String s = String("SkyMesh camera node\nid: ") + Cfg.id + "\nfirmware: " + FIRMWARE_VERSION +
             "\ncamera: " + (g_cameraOk ? "ok" : "unavailable") + "\n";
  httpd_resp_set_type(req, "text/plain");
  return httpd_resp_sendstr(req, s.c_str());
}

static esp_err_t statusHandler(httpd_req_t *req) {
  JsonDocument d;
  d["device_id"] = Cfg.id;
  d["firmware_version"] = FIRMWARE_VERSION;
  d["hardware_model"] = HARDWARE_MODEL;
  d["uptime_s"] = millis() / 1000;
  d["camera_ok"] = g_cameraOk;
  if (!g_cameraOk) d["camera_error"] = g_cameraError;
  d["sensor"] = g_sensorName;
  d["software_jpeg"] = g_softJpeg;
  d["stream_fps"] = g_streamFps;
  d["frame_bytes"] = g_lastFrameBytes;
  d["psram"] = psramFound();
  d["free_heap"] = ESP.getFreeHeap();
  d["rssi"] = WiFi.RSSI();
  d["stream_busy"] = (bool)g_streamBusy;
  d["last_heartbeat_http"] = g_lastHeartbeatHttp;
  String out;
  serializeJson(d, out);
  httpd_resp_set_type(req, "application/json");
  return httpd_resp_sendstr(req, out.c_str());
}

static esp_err_t captureHandler(httpd_req_t *req) {
  if (!authorised(req)) return deny(req);
  if (!g_cameraOk) {
    httpd_resp_set_status(req, "503 Service Unavailable");
    return httpd_resp_sendstr(req, "camera unavailable");
  }
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    httpd_resp_set_status(req, "503 Service Unavailable");
    return httpd_resp_sendstr(req, "frame capture failed");
  }
  uint8_t *jpg;
  size_t len;
  bool owned;
  if (!frameJpeg(fb, &jpg, &len, &owned)) {
    esp_camera_fb_return(fb);
    httpd_resp_set_status(req, "503 Service Unavailable");
    return httpd_resp_sendstr(req, "JPEG encoding failed");
  }
  httpd_resp_set_type(req, "image/jpeg");
  httpd_resp_set_hdr(req, "Cache-Control", "no-store");
  esp_err_t r = httpd_resp_send(req, (const char *)jpg, len);
  if (owned) free(jpg);
  esp_camera_fb_return(fb);
  return r;
}

#define PART_BOUNDARY "skymeshframe"
static esp_err_t streamHandler(httpd_req_t *req) {
  if (!authorised(req)) return deny(req);
  if (!g_cameraOk) { httpd_resp_set_status(req, "503 Service Unavailable"); return httpd_resp_sendstr(req, "camera unavailable"); }
  if (g_streamBusy) { httpd_resp_set_status(req, "409 Conflict"); return httpd_resp_sendstr(req, "stream already in use"); }
  g_streamBusy = true;
  httpd_resp_set_type(req, "multipart/x-mixed-replace;boundary=" PART_BOUNDARY);
  httpd_resp_set_hdr(req, "Cache-Control", "no-store");
  esp_err_t res = ESP_OK;
  char head[96];
  uint32_t windowStart = millis(), frames = 0, lastFrame = 0;
  while (res == ESP_OK) {
    uint32_t since = millis() - lastFrame;
    if (since < STREAM_MIN_FRAME_MS) delay(STREAM_MIN_FRAME_MS - since);
    lastFrame = millis();
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) { res = ESP_FAIL; break; }
    uint8_t *jpg;
    size_t len;
    bool owned;
    if (!frameJpeg(fb, &jpg, &len, &owned)) { esp_camera_fb_return(fb); res = ESP_FAIL; break; }
    int n = snprintf(head, sizeof(head), "\r\n--" PART_BOUNDARY "\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n", (unsigned)len);
    res = httpd_resp_send_chunk(req, head, n);
    if (res == ESP_OK) res = httpd_resp_send_chunk(req, (const char *)jpg, len);
    g_lastFrameBytes = len;
    if (owned) free(jpg);
    esp_camera_fb_return(fb);
    frames++;
    if (millis() - windowStart >= 2000) {
      g_streamFps = frames * 1000.0f / (millis() - windowStart);
      frames = 0;
      windowStart = millis();
    }
  }
  g_streamFps = 0;
  g_streamBusy = false;
  return res;
}

static esp_err_t controlHandler(httpd_req_t *req) {
  if (!authorised(req)) return deny(req);
  char q[128] = {0}, cmd[24] = {0}, val[16] = {0};
  httpd_req_get_url_query_str(req, q, sizeof(q));
  httpd_query_key_value(q, "cmd", cmd, sizeof(cmd));
  httpd_query_key_value(q, "val", val, sizeof(val));
  httpd_resp_set_type(req, "application/json");
  sensor_t *s = g_cameraOk ? esp_camera_sensor_get() : nullptr;
  if (!strcmp(cmd, "restart")) {
    httpd_resp_sendstr(req, "{\"ok\":true,\"action\":\"restarting\"}");
    delay(200);
    ESP.restart();
  }
  if (!strcmp(cmd, "flash")) {  // works even if the camera sensor failed
    int v = atoi(val);
    skyPrefs.putInt("flash", constrain(v, 0, 80));
    setFlash(v);
    return httpd_resp_sendstr(req, "{\"ok\":true}");
  }
  if (!s) { httpd_resp_set_status(req, "503 Service Unavailable"); return httpd_resp_sendstr(req, "{\"ok\":false,\"error\":\"camera unavailable\"}"); }
  int rc = -1;
  if (!strcmp(cmd, "framesize")) {
    framesize_t fs = framesizeFromName(String(val));
    if (framesizeAllowed(fs)) { skyPrefs.putString("fs", val); applyImageSettings(); rc = 0; }
  } else if (!strcmp(cmd, "quality")) {
    int qv = atoi(val);
    if (qv >= 1 && qv <= 100) { skyPrefs.putInt("quality", qv); applyImageSettings(); rc = 0; }
  } else if (!strcmp(cmd, "brightness") || !strcmp(cmd, "contrast") || !strcmp(cmd, "saturation")) {
    int v = atoi(val);
    if (v >= -2 && v <= 2) {
      skyPrefs.putInt(!strcmp(cmd, "brightness") ? "bright" : !strcmp(cmd, "contrast") ? "contrast" : "sat", v);
      applyImageSettings();
      rc = 0;
    }
  } else if (!strcmp(cmd, "reset")) {  // back to the defaults
    for (const char *k : {"bright", "contrast", "sat", "quality", "fs", "hmirror", "vflip"}) skyPrefs.remove(k);
    applyImageSettings();
    rc = 0;
  } else if (!strcmp(cmd, "hmirror") || !strcmp(cmd, "vflip")) {
    bool on = !strcmp(val, "1");
    skyPrefs.putBool(cmd, on);
    applyOrientation();
    rc = 0;
  } else if (!strcmp(cmd, "rotate180")) {  // hmirror + vflip together = 180° rotation
    bool on = !(skyPrefs.getBool("hmirror", false) && skyPrefs.getBool("vflip", false));
    skyPrefs.putBool("hmirror", on);
    skyPrefs.putBool("vflip", on);
    applyOrientation();
    rc = 0;
  }
  if (rc != 0) { httpd_resp_set_status(req, "400 Bad Request"); return httpd_resp_sendstr(req, "{\"ok\":false,\"error\":\"unsupported command or value\"}"); }
  return httpd_resp_sendstr(req, "{\"ok\":true}");
}

static void startHttp() {
  httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
  cfg.server_port = 80;
  cfg.max_open_sockets = 4;
  cfg.lru_purge_enable = true;
  if (httpd_start(&g_httpd, &cfg) != ESP_OK) { Con.println("[http] server failed to start"); return; }
  httpd_uri_t routes[] = {
    {"/", HTTP_GET, rootHandler, nullptr}, {"/status", HTTP_GET, statusHandler, nullptr},
    {"/capture", HTTP_GET, captureHandler, nullptr}, {"/stream", HTTP_GET, streamHandler, nullptr},
    {"/control", HTTP_GET, controlHandler, nullptr},
  };
  for (auto &r : routes) httpd_register_uri_handler(g_httpd, &r);
  Con.println("[http] listening on :80");
}


// ---------------------------------------------------------------- backend client

static void sendHeartbeat() {
  JsonDocument d;
  d["schema_version"] = 1;
  d["firmware_version"] = FIRMWARE_VERSION;
  d["hardware_model"] = HARDWARE_MODEL;
  d["uptime_s"] = millis() / 1000;
  d["sequence"] = ++g_heartbeatSeq;
  d["ip"] = WiFi.localIP().toString();
  d["rssi"] = WiFi.RSSI();
  d["bssid"] = WiFi.BSSIDstr();
  d["free_heap"] = ESP.getFreeHeap();
  if (psramFound()) d["psram_free"] = ESP.getFreePsram();
  d["camera_ok"] = g_cameraOk;
  if (!g_resetReasonSent) d["reset_reason"] = resetReasonText();
  JsonObject caps = d["capabilities"].to<JsonObject>();
  caps["camera"] = g_cameraOk;
  caps["psram"] = psramFound();
  caps["endpoints"] = "status,capture,stream,control";
  caps["sensor"] = g_sensorName;
  caps["software_jpeg"] = g_softJpeg;
  caps["stream_fps"] = roundf(g_streamFps * 10) / 10;
  caps["hmirror"] = skyPrefs.getBool("hmirror", false);
  caps["vflip"] = skyPrefs.getBool("vflip", false);
  JsonObject pic = caps["picture"].to<JsonObject>();
  pic["flash"] = g_flash;
  pic["brightness"] = skyPrefs.getInt("bright", 0);
  pic["contrast"] = skyPrefs.getInt("contrast", 0);
  pic["saturation"] = skyPrefs.getInt("sat", 0);
  pic["quality"] = skyPrefs.getInt("quality", 35);
  pic["framesize"] = skyPrefs.getString("fs", "qvga");
  caps["frame_bytes"] = g_lastFrameBytes;
#if CAMERA_WIFI_BEACON
  caps["beacon_id"] = g_beaconId;  // SkyMesh uses this camera as a positioning anchor once it is placed on the map
#endif
  if (!g_cameraOk) caps["camera_error"] = g_cameraError;
  String body, resp;
  serializeJson(d, body);

  int code = skyPost("/api/v1/device/heartbeat", body, resp, HTTP_TIMEOUT_MS);
  skyNoteResult(code);  // repeated network failures trigger a search for the server's new address
  g_lastHeartbeatHttp = code;
  if (code == 200) {
    g_resetReasonSent = true;
    JsonDocument r;
    if (deserializeJson(r, resp) == DeserializationError::Ok) {
      uint32_t iv = r["heartbeat_interval_s"] | 30;
      g_heartbeatIntervalMs = constrain(iv, 5u, 300u) * 1000UL;
    }
    Con.printf("[hb] ok seq=%u rssi=%d\n", g_heartbeatSeq, WiFi.RSSI());
  } else if (code == 401) {
    Con.println("[hb] 401: this camera was removed or re-enrolled in SkyMesh. Add it again with Devices -> Add device.");
  } else {
    Con.printf("[hb] failed: %d (server %s)\n", code, Cfg.url.c_str());
  }
}

// ---------------------------------------------------------------- Wi-Fi supervision

static void maintainWifi() {
  if (WiFi.status() == WL_CONNECTED) {
    if (g_wifiDownSince) Con.printf("[wifi] connected, ip %s rssi %d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
    g_wifiDownSince = 0;
    return;
  }
  uint32_t now = millis();
  if (g_wifiDownSince == 0) { g_wifiDownSince = now; Con.printf("[wifi] connecting to %s\n", Cfg.ssid.c_str()); return; }
  uint32_t down = now - g_wifiDownSince;
  static uint32_t lastKick = 0;
  if (down > WIFI_RESET_AFTER_MS && now - lastKick > WIFI_RESET_AFTER_MS) {
    lastKick = now;
    Con.println("[wifi] still not connected: retrying (check the Wi-Fi name/password, 2.4 GHz only)");
    WiFi.disconnect();
    WiFi.begin(Cfg.ssid.c_str(), Cfg.pass.c_str());
  }
  if (down > WIFI_REBOOT_AFTER_MS) {
    Con.println("[wifi] down for 10 min: rebooting");
    ESP.restart();
  }
}

static void printStatus() {
  Con.printf("id=%s fw=%s server=%s camera=%s%s wifi=%s ip=%s rssi=%d heap=%u psram=%s last_hb_http=%d uptime=%lus\n",
             Cfg.id.c_str(), FIRMWARE_VERSION, Cfg.url.c_str(), g_cameraOk ? "ok" : "FAIL ", g_cameraOk ? "" : g_cameraError.c_str(),
             WiFi.status() == WL_CONNECTED ? "up" : "down", WiFi.localIP().toString().c_str(), WiFi.RSSI(),
             ESP.getFreeHeap(), psramFound() ? "yes" : "no", g_lastHeartbeatHttp, millis() / 1000);
}

static void handleCommand(const String &cmd) {
  if (cmd == "status") printStatus();
  else if (cmd == "restart") ESP.restart();
  else if (cmd == "forget-wifi") { Con.println("[setup] erasing settings"); skyEraseConfig(); delay(200); ESP.restart(); }
  else Con.println("commands: status | restart | forget-wifi");
}

// ---------------------------------------------------------------- setup / loop

void setup() {
  Con.begin();
  delay(300);
  pinMode(FLASH_LED_PIN, OUTPUT);
  digitalWrite(FLASH_LED_PIN, LOW);
  pinMode(RED_LED_PIN, OUTPUT);
  digitalWrite(RED_LED_PIN, HIGH);  // off (active LOW)
  skyLoadConfig();
  skyCheckTripleReset();
  initFlash();  // restores the saved flash level (0 = off)
  Con.printf("\nSkyMesh camera node %s (%s), reset: %s\n", FIRMWARE_VERSION, Cfg.id.c_str(), resetReasonText());

  esp_task_wdt_config_t wdt = {.timeout_ms = WDT_TIMEOUT_S * 1000, .idle_core_mask = 0, .trigger_panic = true};
  if (esp_task_wdt_reconfigure(&wdt) != ESP_OK) esp_task_wdt_init(&wdt);
  esp_task_wdt_add(NULL);

  g_cameraOk = initCamera();
  if (!g_cameraOk) Con.println("[cam] camera unavailable; node keeps reporting health without images");
  skyPrintInfo("SKYMESH-READY");

  if (!Cfg.valid()) {
    Con.println("[setup] not set up yet: open SkyMesh -> Devices -> Add device, and keep this camera plugged in by USB");
    return;  // loop() only listens for the setup command until then
  }
  skyWifiBegin();        // non-blocking: the console stays responsive
  WiFi.setSleep(false);  // lower latency for the video stream
#if CAMERA_WIFI_BEACON
  startBeacon();
#endif
  startHttp();
}

void loop() {
  esp_task_wdt_reset();
  skyPollConsole(handleCommand);
  skyClearTripleResetAfterBoot();
  if (!Cfg.valid()) {
    digitalWrite(RED_LED_PIN, (millis() % 2000) < 150 ? LOW : HIGH);  // short blink: waiting for setup
    delay(10);
    return;
  }
  maintainWifi();
  if (WiFi.status() == WL_CONNECTED && (g_lastHeartbeat == 0 || millis() - g_lastHeartbeat >= g_heartbeatIntervalMs)) {
    g_lastHeartbeat = millis();
    sendHeartbeat();
  }
  digitalWrite(RED_LED_PIN, WiFi.status() == WL_CONNECTED ? HIGH : ((millis() / 500) % 2 ? LOW : HIGH));
  delay(20);
}
