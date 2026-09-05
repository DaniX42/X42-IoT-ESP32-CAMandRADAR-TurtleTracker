#include <Arduino.h>
#include <ArduinoJson.h>
#include <ArduinoOTA.h>
#include <PubSubClient.h>
#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <cstring>
#include "esp_camera.h"
#include "generated_secrets.h"

#ifndef TT_API_URL
#define TT_API_URL "http://192.168.1.100:8000"
#endif
#ifndef TT_MQTT_HOST
#define TT_MQTT_HOST "mqtt.example.local"
#endif
#ifndef TT_MQTT_PORT
#define TT_MQTT_PORT 1888
#endif
#ifndef TT_MQTT_USER
#define TT_MQTT_USER ""
#endif
#ifndef TT_MQTT_PASSWORD
#define TT_MQTT_PASSWORD ""
#endif
#ifndef TT_WIFI_SSID
#define TT_WIFI_SSID ""
#endif
#ifndef TT_WIFI_PASSWORD
#define TT_WIFI_PASSWORD ""
#endif
#ifndef TT_DEVICE_NAME
#define TT_DEVICE_NAME "turtle-cam-outdoor"
#endif
#ifndef TT_OTA_PASSWORD
#define TT_OTA_PASSWORD ""
#endif

namespace {
constexpr int8_t PWDN_GPIO_NUM = 32;
constexpr int8_t RESET_GPIO_NUM = -1;
constexpr int8_t XCLK_GPIO_NUM = 0;
constexpr int8_t SIOD_GPIO_NUM = 26;
constexpr int8_t SIOC_GPIO_NUM = 27;
constexpr int8_t Y9_GPIO_NUM = 35;
constexpr int8_t Y8_GPIO_NUM = 34;
constexpr int8_t Y7_GPIO_NUM = 39;
constexpr int8_t Y6_GPIO_NUM = 36;
constexpr int8_t Y5_GPIO_NUM = 21;
constexpr int8_t Y4_GPIO_NUM = 19;
constexpr int8_t Y3_GPIO_NUM = 18;
constexpr int8_t Y2_GPIO_NUM = 5;
constexpr int8_t VSYNC_GPIO_NUM = 25;
constexpr int8_t HREF_GPIO_NUM = 23;
constexpr int8_t PCLK_GPIO_NUM = 22;
constexpr unsigned long FRAME_INTERVAL_MS = 5000;

// GPIO16/17 are used by PSRAM on the AI Thinker ESP32-CAM.
// HLK-LD2450 UART2: sensor TX -> GPIO13 (RX2), sensor RX -> GPIO14 (TX2, optional).
constexpr int8_t RADAR_RX_GPIO_NUM = 13;
constexpr int8_t RADAR_TX_GPIO_NUM = 14;
constexpr unsigned long RADAR_BAUD_RATE = 256000;
constexpr unsigned long RADAR_SEND_INTERVAL_MS = 1000;
constexpr uint8_t RADAR_FRAME_HEADER[4] = {0xAA, 0xFF, 0x03, 0x00};
constexpr uint8_t RADAR_FRAME_FOOTER[2] = {0x55, 0xCC};
constexpr size_t RADAR_FRAME_LENGTH = 30;  // 4 header + 3 x 8 target bytes + 2 footer
constexpr size_t RADAR_TARGET_LENGTH = 8;

WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);
WebServer server(80);
HardwareSerial radarSerial(2);
uint8_t radarBuffer[RADAR_FRAME_LENGTH];
size_t radarBufferPos = 0;
unsigned long lastFrame = 0;
unsigned long lastRadarSend = 0;
unsigned long lastMqttAttempt = 0;
String baseTopic = String("turtle_tracker/") + TT_DEVICE_NAME;

// Decodes a LD2450 coordinate/speed pair: bit 15 of the high byte is the sign flag
// (set = positive), the remaining 15 bits are the magnitude.
int16_t decodeRadarValue(uint8_t lowByte, uint8_t highByte) {
  int16_t value = (int16_t)((highByte & 0x7F) << 8) | lowByte;
  if ((highByte & 0x80) == 0) value = -value;
  return value;
}

void sendRadarTargets() {
  while (radarSerial.available()) {
    uint8_t incoming = (uint8_t)radarSerial.read();
    if (radarBufferPos == 0 && incoming != RADAR_FRAME_HEADER[0]) continue;
    radarBuffer[radarBufferPos++] = incoming;
    if (radarBufferPos < RADAR_FRAME_LENGTH) continue;
    radarBufferPos = 0;
    if (memcmp(radarBuffer, RADAR_FRAME_HEADER, sizeof(RADAR_FRAME_HEADER)) != 0) continue;
    if (memcmp(radarBuffer + RADAR_FRAME_LENGTH - 2, RADAR_FRAME_FOOTER, sizeof(RADAR_FRAME_FOOTER)) != 0) continue;
    if (millis() - lastRadarSend < RADAR_SEND_INTERVAL_MS) continue;
    lastRadarSend = millis();

    JsonDocument document;
    JsonArray targets = document["targets"].to<JsonArray>();
    for (uint8_t target = 0; target < 3; target++) {
      const uint8_t* base = radarBuffer + 4 + target * RADAR_TARGET_LENGTH;
      int16_t x = decodeRadarValue(base[0], base[1]);
      int16_t y = decodeRadarValue(base[2], base[3]);
      int16_t speed = decodeRadarValue(base[4], base[5]);
      if (x == 0 && y == 0) continue;  // No target in this slot
      JsonObject entry = targets.add<JsonObject>();
      entry["x_mm"] = x;
      entry["y_mm"] = y;
      entry["speed_mm_s"] = speed * 10;
    }
    if (targets.size() == 0) return;
    String payload;
    serializeJson(document, payload);
    HTTPClient http;
    http.begin(String(TT_API_URL) + "/api/radar/" + TT_DEVICE_NAME);
    http.addHeader("Content-Type", "application/json");
    http.POST(payload);
    http.end();
  }
}

String topic(const char* suffix) { return baseTopic + "/" + suffix; }

void publishDiscovery(const char* component, const char* objectId, const char* name, const char* stateTopic,
                     const char* unit, const char* valueTemplate, const char* deviceClass = nullptr) {
  String discovery = String("homeassistant/") + component + "/" + TT_DEVICE_NAME + "/" + objectId + "/config";
  JsonDocument document;
  document["name"] = name;
  document["unique_id"] = String(TT_DEVICE_NAME) + "_" + objectId;
  document["state_topic"] = stateTopic;
  if (unit) document["unit_of_measurement"] = unit;
  if (valueTemplate) document["value_template"] = valueTemplate;
  if (deviceClass) document["device_class"] = deviceClass;
  JsonObject device = document["device"].to<JsonObject>();
  device["identifiers"][0] = TT_DEVICE_NAME;
  device["name"] = "Turtle Tracker Camera";
  device["manufacturer"] = "X42";
  device["model"] = "ESP32-CAM AI Thinker";
  document["availability_topic"] = topic("availability");
  document["payload_available"] = "online";
  document["payload_not_available"] = "offline";
  String payload;
  serializeJson(document, payload);
  mqtt.publish(discovery.c_str(), payload.c_str(), true);
}

void publishDiscovery() {
  const String state = topic("state");
  publishDiscovery("sensor", "x", "Tortoise X", state.c_str(), "m", "{{ value_json.x }}");
  publishDiscovery("sensor", "y", "Tortoise Y", state.c_str(), "m", "{{ value_json.y }}");
  publishDiscovery("sensor", "last_seen", "Tortoise Last Seen", state.c_str(), nullptr, "{{ value_json.last_seen }}", "timestamp");
  publishDiscovery("sensor", "confidence", "Tortoise Detection Confidence", state.c_str(), "%", "{{ (value_json.confidence * 100) | round(1) }}");
  publishDiscovery("binary_sensor", "inside_house", "Tortoise Inside House", state.c_str(), nullptr, "{{ value_json.inside_house }}", "occupancy");
}

void connectMqtt() {
  if (mqtt.connected() || millis() - lastMqttAttempt < 5000) return;
  lastMqttAttempt = millis();
  String clientId = String(TT_DEVICE_NAME) + "-" + String((uint32_t)ESP.getEfuseMac(), HEX);
  if (mqtt.connect(clientId.c_str(), TT_MQTT_USER, TT_MQTT_PASSWORD, topic("availability").c_str(), 1, true, "offline")) {
    mqtt.publish(topic("availability").c_str(), "online", true);
    publishDiscovery();
  }
}

void setupOta() {
  ArduinoOTA.setHostname(TT_DEVICE_NAME);
  if (strlen(TT_OTA_PASSWORD) > 0) ArduinoOTA.setPassword(TT_OTA_PASSWORD);
  ArduinoOTA.onStart([]() { Serial.println("OTA update started"); });
  ArduinoOTA.onEnd([]() { Serial.println("OTA update finished"); });
  ArduinoOTA.onError([](ota_error_t error) { Serial.printf("OTA error: %u\n", error); });
  ArduinoOTA.begin();
}

bool setupCamera() {
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM; config.pin_d1 = Y3_GPIO_NUM; config.pin_d2 = Y4_GPIO_NUM; config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM; config.pin_d5 = Y7_GPIO_NUM; config.pin_d6 = Y8_GPIO_NUM; config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM; config.pin_pclk = PCLK_GPIO_NUM; config.pin_vsync = VSYNC_GPIO_NUM; config.pin_href = HREF_GPIO_NUM;
  config.pin_sscb_sda = SIOD_GPIO_NUM; config.pin_sscb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM; config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000; config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size = FRAMESIZE_UXGA; config.jpeg_quality = 10; config.fb_count = psramFound() ? 2 : 1;
  if (esp_camera_init(&config) != ESP_OK) return false;
#ifdef TT_DOOR_CAMERA
  sensor_t* sensor = esp_camera_sensor_get();
  if (sensor) {
    sensor->set_quality(sensor, 12);
    sensor->set_brightness(sensor, 0);
    sensor->set_contrast(sensor, 0);
    sensor->set_saturation(sensor, 0);
    sensor->set_gain_ctrl(sensor, 1);
    sensor->set_exposure_ctrl(sensor, 1);
    sensor->set_whitebal(sensor, 1);
  }
#endif
  return true;
}

void sendFrame() {
  camera_fb_t* frame = esp_camera_fb_get();
  if (!frame) return;
  HTTPClient http;
  String url = String(TT_API_URL) + "/api/frames/" + TT_DEVICE_NAME;
  http.begin(url);
  http.addHeader("Content-Type", "image/jpeg");
  int status = http.POST(frame->buf, frame->len);
  if (status >= 200 && status < 300) {
    JsonDocument response;
    if (deserializeJson(response, http.getString()) == DeserializationError::Ok && response["accepted"] == true) {
      JsonObject position = response["position"].as<JsonObject>();
      JsonDocument state;
      state["x"] = position["x"];
      state["y"] = position["y"];
      state["speed"] = position["speed"];
      state["confidence"] = position["confidence"];
      state["inside_house"] = position["inside_house"] ? "ON" : "OFF";
      state["last_seen"] = position["timestamp"];
      String payload;
      serializeJson(state, payload);
      mqtt.publish(topic("state").c_str(), payload.c_str(), true);
    }
  }
  http.end();
  esp_camera_fb_return(frame);
}

}

void setup() {
  Serial.begin(115200);
  radarSerial.begin(RADAR_BAUD_RATE, SERIAL_8N1, RADAR_RX_GPIO_NUM, RADAR_TX_GPIO_NUM);
  if (!setupCamera()) { Serial.println("Camera initialization failed"); delay(1000); ESP.restart(); }
  WiFi.mode(WIFI_STA);
  WiFi.begin(TT_WIFI_SSID, TT_WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print('.'); }
  Serial.printf("\nIP: %s\n", WiFi.localIP().toString().c_str());
  setupOta();
  mqtt.setServer(TT_MQTT_HOST, TT_MQTT_PORT);
  mqtt.setBufferSize(1024); // default 256 bytes is too small for HA discovery payloads; publish() fails silently otherwise
  server.on("/health", []() { server.send(200, "text/plain", "ok"); });
  server.begin();
}

void loop() {
  ArduinoOTA.handle();
  server.handleClient();
  connectMqtt();
  mqtt.loop();
  sendRadarTargets();
  if (millis() - lastFrame >= FRAME_INTERVAL_MS) { lastFrame = millis(); sendFrame(); }
}
