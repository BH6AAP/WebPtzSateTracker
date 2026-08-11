/*
  AS5600 磁编码器角度采集 + HTTP POST 上传
  硬件: ESP8266 NodeMCU / ESP-12E
  接线:
    AS5600 VCC -> 3.3V
    AS5600 GND -> GND
    AS5600 SCL -> D1 (GPIO5)
    AS5600 SDA -> D2 (GPIO4)
    AS5600 DIR -> GND (旋转方向, 可接3.3V反转)
  数据格式: JSON {"angle":123.45,"raw":2048,"tick":123456}
  目标: 本地 ptz 服务 /api/encoder
*/

#include <Wire.h>
#include <ESP8266WiFi.h>
#include <ESP8266HTTPClient.h>

// ========== 用户配置 ==========
const char* WIFI_SSID     = "CMCC-123";
const char* WIFI_PASSWORD = "19990621";

// ptz 服务地址: 如果 ESP 和 ptz 在同一局域网, 填 ptz 主机的内网 IP
// 例如 "http://192.168.31.66:8090/api/encoder"
const char* PTZ_URL       = "http://192.168.31.66:8090/api/encoder";
const uint16_t SEND_MS    = 100;          // 发送间隔(ms)

// AS5600 I2C
#define AS5600_ADDR 0x36
#define REG_ANGLE   0x0E  // 0x0C/0x0D 是原始角度, 0x0E/0x0F 是滤波后角度
#define SDA_PIN     4
#define SCL_PIN     5

WiFiClient client;
HTTPClient http;
unsigned long lastSend = 0;

uint16_t readAngle() {
  Wire.beginTransmission(AS5600_ADDR);
  Wire.write(REG_ANGLE);
  if (Wire.endTransmission(false) != 0) {
    return 0xFFFF;
  }
  Wire.requestFrom(AS5600_ADDR, 2);
  if (Wire.available() < 2) {
    return 0xFFFF;
  }
  uint8_t high = Wire.read();
  uint8_t low  = Wire.read();
  return ((uint16_t)high << 8) | low;
}

void setup() {
  Serial.begin(115200);
  delay(100);
  Serial.println("\nAS5600 HTTP Sender starting...");

  Wire.begin(SDA_PIN, SCL_PIN);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Connecting WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  Serial.print("WiFi connected, IP: ");
  Serial.println(WiFi.localIP());
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi lost, reconnecting...");
    WiFi.reconnect();
    delay(2000);
    return;
  }

  unsigned long now = millis();
  if (now - lastSend >= SEND_MS) {
    lastSend = now;

    uint16_t raw = readAngle();
    if (raw == 0xFFFF) {
      Serial.println("AS5600 read error");
      return;
    }
    // AS5600 角度为 12bit, 4096 = 360°
    float angle = (raw * 360.0f) / 4096.0f;

    char payload[128];
    snprintf(payload, sizeof(payload),
             "{\"angle\":%.2f,\"raw\":%u,\"tick\":%lu}",
             angle, raw, now);

    Serial.print("POST ");
    Serial.print(payload);
    Serial.print(" -> ");

    http.begin(client, PTZ_URL);
    http.addHeader("Content-Type", "application/json");
    int code = http.POST(payload);
    Serial.println(code);
    http.end();
  }
}
