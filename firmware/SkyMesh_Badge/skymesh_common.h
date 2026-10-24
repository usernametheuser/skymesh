// skymesh_common.h — shared by SkyMesh_Badge and SkyMesh_Server (identical copy in both folders).
//
// Settings (Wi-Fi, server address, device identity) live in the chip's own flash (NVS). They are written by
// the SkyMesh "Add device" wizard over USB, so nobody edits files. Serial protocol (115200 baud, one line each):
//   -> SKYMESH-INFO                 <- SKYMESH-INFO {"role":..,"fw":..,"chip":..,"mac":..,"configured":..}
//   -> SKYMESH-PROVISION {json}     <- SKYMESH-OK   (then the device restarts)   or   SKYMESH-ERR <reason>
//   -> SKYMESH-RESET                <- SKYMESH-OK   (settings erased, restarts)
// The device prints "SKYMESH-READY {...}" at boot. Pressing RESET three times quickly also erases the settings.
//
// If the server stops answering (e.g. the computer got a new IP address), the device broadcasts a discovery
// request on UDP 47808. It accepts a new address only with a reply signed using its own device token, so another
// computer on the network cannot redirect it.
#pragma once

#include <Arduino.h>
#include <Preferences.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <ArduinoJson.h>
#include <esp_system.h>
#include <esp_mac.h>
#include "mbedtls/md.h"

#if __has_include("secrets.h")
#include "secrets.h"  // optional, advanced: compile-time defaults used only when nothing is stored on the chip
#endif

#ifndef SKY_ROLE
#error "define SKY_ROLE before including skymesh_common.h"
#endif

static const uint16_t SKY_DISCOVERY_PORT = 47808;

// ---------------------------------------------------------------- console on every USB port

// ESP32-S3 boards often have two USB-C ports (native USB and a UART bridge). Built with "USB CDC on boot",
// Serial is the native port and Serial0 the UART: the console listens and answers on both.
#if defined(CONFIG_IDF_TARGET_ESP32S3) && ARDUINO_USB_CDC_ON_BOOT && ARDUINO_USB_MODE
#define SKY_DUAL_PORT 1
#else
#define SKY_DUAL_PORT 0
#endif

class SkyConsole : public Print {
 public:
  void begin() {
    Serial.setRxBufferSize(2048);
#if SKY_DUAL_PORT
    Serial.setTxTimeoutMs(0);  // never block when no computer is attached to the native port
    Serial.begin();
    Serial0.setRxBufferSize(2048);
    Serial0.begin(115200);
#else
    Serial.begin(115200);
#endif
  }
  size_t write(uint8_t c) override { return write(&c, 1); }
  size_t write(const uint8_t *b, size_t n) override {
#if SKY_DUAL_PORT
    if (Serial) Serial.write(b, n);
    Serial0.write(b, n);
#else
    Serial.write(b, n);
#endif
    return n;
  }
  int read() {
    if (Serial.available()) return Serial.read();
#if SKY_DUAL_PORT
    if (Serial0.available()) return Serial0.read();
#endif
    return -1;
  }
};
static SkyConsole Con;

// ---------------------------------------------------------------- stored configuration

struct SkyConfig {
  String ssid, pass, url, id, token, camkey, country;
  bool valid() const { return ssid.length() && url.startsWith("http") && token.startsWith("smd_"); }
};
static SkyConfig Cfg;
static Preferences skyPrefs;

static String skyDefault(const char *v) { return (v && strcmp(v, "CHANGE_ME") != 0 && !strstr(v, "CHANGE_ME")) ? String(v) : String(); }

static void skyLoadConfig() {
  skyPrefs.begin("skycfg", false);
  Cfg.ssid = skyPrefs.getString("ssid", "");
  Cfg.pass = skyPrefs.getString("pass", "");
  Cfg.url = skyPrefs.getString("url", "");
  Cfg.id = skyPrefs.getString("id", "");
  Cfg.token = skyPrefs.getString("token", "");
  Cfg.camkey = skyPrefs.getString("camkey", "");
  Cfg.country = skyPrefs.getString("country", "IN");
#if defined(WIFI_SSID) && defined(SKYMESH_DEVICE_TOKEN)
  if (!Cfg.valid()) {  // nothing stored on the chip: fall back to secrets.h if it was filled in
    Cfg.ssid = skyDefault(WIFI_SSID);
    Cfg.pass = skyDefault(WIFI_PASSWORD);
    Cfg.url = skyDefault(BACKEND_URL);
    Cfg.id = skyDefault(SKYMESH_DEVICE_ID);
    Cfg.token = skyDefault(SKYMESH_DEVICE_TOKEN);
#ifdef SKYMESH_CAMERA_KEY
    Cfg.camkey = skyDefault(SKYMESH_CAMERA_KEY);
#endif
  }
#endif
}

static void skySaveConfig() {
  skyPrefs.putString("ssid", Cfg.ssid);
  skyPrefs.putString("pass", Cfg.pass);
  skyPrefs.putString("url", Cfg.url);
  skyPrefs.putString("id", Cfg.id);
  skyPrefs.putString("token", Cfg.token);
  skyPrefs.putString("camkey", Cfg.camkey);
  skyPrefs.putString("country", Cfg.country);
}

// Join the stored Wi-Fi. The country sets the legal channel range: the default ("01", world-safe) only
// scans channels 1-11, so phone hotspots and routers on channel 12/13 were invisible. India and most of
// Europe/Asia allow 1-13; 802.11d lets the station follow the access point's own country information.
#include <esp_wifi.h>

// Why joining failed, in words a person can act on (printed once per change, not on every retry).
static const char *skyWifiReason(uint8_t r) {
  switch (r) {
    case 2: case 15: case 202: case 204:
      return "wrong Wi-Fi password (or the network uses WPA3-only: set the hotspot to WPA2/WPA3)";
    case 201: return "network not found: check the exact name (capitals, spaces) and that it is on 2.4 GHz";
    case 210: case 211: return "network found but its security type is not supported: use WPA2 (or WPA2/WPA3)";
    case 212: return "network signal too weak: move closer";
    case 203: case 205: return "the access point refused the connection (full, or MAC filtering)";
    case 8: return "left the network";
    default: return "connection lost";
  }
}

static void skyOnWifiEvent(WiFiEvent_t ev, WiFiEventInfo_t info) {
  static uint8_t lastReason = 0;
  if (ev == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
    uint8_t r = info.wifi_sta_disconnected.reason;
    if (r != lastReason) Con.printf("[wifi] cannot join \"%s\": %s (reason %u)\n", Cfg.ssid.c_str(), skyWifiReason(r), r);
    lastReason = r;
  } else if (ev == ARDUINO_EVENT_WIFI_STA_GOT_IP) {
    lastReason = 0;
    Con.printf("[wifi] connected to \"%s\" ip=%s rssi=%d channel=%d\n", Cfg.ssid.c_str(),
               WiFi.localIP().toString().c_str(), WiFi.RSSI(), WiFi.channel());
  }
}

// Serial command "scan": the 2.4 GHz networks this board can actually see.
static void skyScanPrint() {
  Con.println("[scan] scanning…");
  int n = WiFi.scanNetworks(false, true);
  if (n < 0) { Con.printf("[scan] failed (%d), try again\n", n); return; }
  for (int i = 0; i < n; i++) {
    Con.printf("[scan] %-32s rssi=%4d ch=%2d %s%s\n", WiFi.SSID(i).c_str(), WiFi.RSSI(i), WiFi.channel(i),
               WiFi.encryptionType(i) == WIFI_AUTH_WPA3_PSK ? "WPA3-only " : "",
               WiFi.SSID(i) == Cfg.ssid ? "<- configured" : "");
  }
  if (!n) Con.println("[scan] no networks seen: is the hotspot on 2.4 GHz and nearby?");
  WiFi.scanDelete();
}

static void skyWifiBegin() {
  static bool hooked = false;
  if (!hooked) { WiFi.onEvent(skyOnWifiEvent); hooked = true; }
  WiFi.mode(WIFI_STA);
  String cc = Cfg.country.length() == 2 ? Cfg.country : String("IN");
  cc.toUpperCase();
  esp_err_t e = esp_wifi_set_country_code(cc.c_str(), true);
  if (e != ESP_OK) Con.printf("[wifi] could not set country %s (0x%x)\n", cc.c_str(), e);
  WiFi.setAutoReconnect(true);
  WiFi.begin(Cfg.ssid.c_str(), Cfg.pass.c_str());
}

static void skyEraseConfig() {
  skyPrefs.clear();
  Cfg = SkyConfig();
}

static String skyMac() {
  uint8_t m[6];
  esp_read_mac(m, ESP_MAC_WIFI_STA);
  char b[18];
  snprintf(b, sizeof(b), "%02x:%02x:%02x:%02x:%02x:%02x", m[0], m[1], m[2], m[3], m[4], m[5]);
  return String(b);
}

static void skyPrintInfo(const char *prefix) {
  JsonDocument d;
  d["role"] = SKY_ROLE;
  d["fw"] = FIRMWARE_VERSION;
  d["chip"] = ESP.getChipModel();
  d["mac"] = skyMac();
  d["configured"] = Cfg.valid();
  d["id"] = Cfg.id;
  d["ssid"] = Cfg.ssid;
  d["url"] = Cfg.url;
  d["country"] = Cfg.country;
  d["psram"] = psramFound();
  Con.print(prefix);
  Con.print(' ');
  serializeJson(d, Con);
  Con.println();
}

// Three resets within a few seconds of each other erase the settings (no buttons or tools needed).
static void skyCheckTripleReset() {
  esp_reset_reason_t r = esp_reset_reason();
  uint32_t n = skyPrefs.getUInt("quick", 0);
  n = (r == ESP_RST_POWERON || r == ESP_RST_EXT) ? n + 1 : 0;
  if (n >= 3) {
    Con.println("[setup] reset pressed 3 times: erasing Wi-Fi and server settings");
    skyEraseConfig();
    n = 0;
  }
  skyPrefs.putUInt("quick", n);
}
static void skyClearTripleResetAfterBoot() {
  static bool done = false;
  if (!done && millis() > 5000) { done = true; skyPrefs.putUInt("quick", 0); }
}

// ---------------------------------------------------------------- serial commands

static String skyLine;

// Returns true if the line was a SkyMesh setup command (the sketch handles everything else).
static bool skyHandleLine(const String &line) {
  if (line == "SKYMESH-INFO") { skyPrintInfo("SKYMESH-INFO"); return true; }
  if (line == "scan" || line == "SKYMESH-SCAN") { skyScanPrint(); return true; }
  if (line == "SKYMESH-RESET") {
    skyEraseConfig();
    Con.println("SKYMESH-OK");
    delay(300);
    ESP.restart();
    return true;
  }
  if (line.startsWith("SKYMESH-PROVISION ")) {
    JsonDocument d;
    if (deserializeJson(d, line.substring(18)) != DeserializationError::Ok) { Con.println("SKYMESH-ERR invalid json"); return true; }
    SkyConfig c;
    c.ssid = (const char *)(d["ssid"] | "");
    c.pass = (const char *)(d["pass"] | "");
    c.url = (const char *)(d["url"] | "");
    c.id = (const char *)(d["id"] | "");
    c.token = (const char *)(d["token"] | "");
    c.camkey = (const char *)(d["camkey"] | "");
    c.country = (const char *)(d["country"] | "IN");
    if (!c.valid()) { Con.println("SKYMESH-ERR missing wifi, url or token"); return true; }
    if (c.ssid.length() > 32 || c.pass.length() > 63) { Con.println("SKYMESH-ERR wifi name or password too long"); return true; }
    Cfg = c;
    skySaveConfig();
    Con.println("SKYMESH-OK");
    delay(300);
    ESP.restart();
    return true;
  }
  return false;
}

// Non-blocking: call every loop. Calls `other` for lines that are not setup commands.
static void skyPollConsole(void (*other)(const String &)) {
  int c;
  while ((c = Con.read()) >= 0) {
    if (c == '\r') continue;
    if (c == '\n') {
      String line = skyLine;
      skyLine = "";
      line.trim();
      if (line.length() && !skyHandleLine(line) && other) other(line);
    } else if (skyLine.length() < 1500) {
      skyLine += (char)c;
    }
  }
}

// ---------------------------------------------------------------- HTTP to the backend

static int skyPost(const char *path, const String &body, String &response, uint16_t timeoutMs = 5000) {
  String url = Cfg.url + path;
  HTTPClient http;
  WiFiClient plain;
  WiFiClientSecure tls;
  if (url.startsWith("https://")) {
#ifdef BACKEND_CA_CERT
    tls.setCACert(BACKEND_CA_CERT);
    if (!http.begin(tls, url)) return -100;
#else
    return -101;  // devices talk plain HTTP on the LAN unless a CA certificate is compiled in
#endif
  } else if (!http.begin(plain, url)) {
    return -100;
  }
  http.setTimeout(timeoutMs);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", String("Bearer ") + Cfg.token);
  int code = http.POST(body);
  if (code > 0) response = http.getString();
  http.end();
  return code;
}

// Upload raw bytes (e.g. a JPEG photo) with the device token. `query` is appended to the path as-is.
static int skyPostBinary(const char *path, const String &query, const uint8_t *data, size_t len, const char *type,
                         String &response) {
  String url = Cfg.url + path + query;
  HTTPClient http;
  WiFiClient plain;
  if (!url.startsWith("http://") || !http.begin(plain, url)) return -100;
  http.setTimeout(15000);
  http.addHeader("Content-Type", type);
  http.addHeader("Authorization", String("Bearer ") + Cfg.token);
  int code = http.POST(const_cast<uint8_t *>(data), len);
  if (code > 0) response = http.getString();
  http.end();
  return code;
}

// ---------------------------------------------------------------- server discovery (signed)

static String skyHex(const uint8_t *b, size_t n) {
  static const char *h = "0123456789abcdef";
  String s;
  s.reserve(n * 2);
  for (size_t i = 0; i < n; i++) { s += h[b[i] >> 4]; s += h[b[i] & 15]; }
  return s;
}

static String skySha256Hex(const String &in) {
  uint8_t out[32];
  mbedtls_md(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), (const uint8_t *)in.c_str(), in.length(), out);
  return skyHex(out, 32);
}

static String skyHmacHex(const String &key, const String &msg) {
  uint8_t out[32];
  mbedtls_md_hmac(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), (const uint8_t *)key.c_str(), key.length(),
                  (const uint8_t *)msg.c_str(), msg.length(), out);
  return skyHex(out, 32);
}

// Ask the LAN where the SkyMesh server is now. Returns true if the stored address changed.
static bool skyDiscoverServer() {
  if (WiFi.status() != WL_CONNECTED || !Cfg.id.length()) return false;
  WiFiUDP udp;
  if (!udp.begin(0)) return false;
  uint8_t rnd[12];
  esp_fill_random(rnd, sizeof(rnd));
  String nonce = skyHex(rnd, sizeof(rnd));
  JsonDocument q;
  q["skymesh"] = "discover";
  q["id"] = Cfg.id;
  q["nonce"] = nonce;
  String qs;
  serializeJson(q, qs);
  udp.beginPacket(WiFi.broadcastIP(), SKY_DISCOVERY_PORT);
  udp.print(qs);
  udp.endPacket();
  String key = skySha256Hex(Cfg.token);  // the server stores exactly this hash of the token
  uint32_t t0 = millis();
  bool changed = false;
  while (millis() - t0 < 1500) {
    int n = udp.parsePacket();
    if (n <= 0) { delay(20); continue; }
    char buf[512];
    n = udp.read(buf, sizeof(buf) - 1);
    if (n <= 0) continue;
    buf[n] = 0;
    JsonDocument r;
    if (deserializeJson(r, buf) != DeserializationError::Ok) continue;
    String url = (const char *)(r["url"] | "");
    String sig = (const char *)(r["sig"] | "");
    if (!url.startsWith("http") || sig != skyHmacHex(key, nonce + "|" + url)) continue;  // not our server
    if (url != Cfg.url) {
      Con.printf("[net] SkyMesh server moved: %s -> %s\n", Cfg.url.c_str(), url.c_str());
      Cfg.url = url;
      skyPrefs.putString("url", url);
      changed = true;
    }
    break;
  }
  udp.stop();
  return changed;
}

// Call after each request: after 3 network failures in a row, look for the server again.
static void skyNoteResult(int httpCode) {
  static uint8_t failures = 0;
  static uint32_t lastDiscovery = 0;
  if (httpCode > 0) { failures = 0; return; }
  if (++failures >= 3 && millis() - lastDiscovery > 60000) {
    lastDiscovery = millis();
    failures = 0;
    Con.println("[net] server not answering: searching the network for it");
    skyDiscoverServer();
  }
}
