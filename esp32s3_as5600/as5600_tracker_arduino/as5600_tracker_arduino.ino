/*
 * AS5600 磁编码器角度采集 + UDP 广播上传 (ESP32-S3 Arduino)
 * 功能: WiFi 配网/连接/重连、AP 配网、光电零位检测、UDP 状态上报
 * 输出: OTG USB-CDC / Hardware CDC 串口
 */
#include <WiFi.h>
#include <WiFiUDP.h>
#include <Wire.h>
#include <Adafruit_NeoPixel.h>
#include "AS5600Sensor.h"
#include "StateStorage.h"
#include "WiFiManager.h"

// ========== 配置 ==========
#define UDP_PORT      8091
#define SEND_MS       50          // 20Hz
#define STATUS_MS     3000        // 状态打印周期
#define SAVE_MS       5000        // 累积角度保存周期
#define ZERO_PIN      11
#define ZERO_ENABLE   false       // 光电零位开关
#define ZERO_DEBOUNCE_MS 30
#define LED_PIN       2           // 板载单色 LED (启动心跳)
#define WS2812_PIN    48          // WS2812 RGB 数据脚 (GPIO48)
#define NUM_LEDS      1

// I2C 引脚
#define SDA_PIN       4
#define SCL_PIN       5

// ========== 全局对象 ==========
AS5600Sensor sensor(SDA_PIN, SCL_PIN, 100000);
StateStorage storage;
WiFiManager wifi(storage);
WiFiUDP udp;
Adafruit_NeoPixel pixel(NUM_LEDS, WS2812_PIN, NEO_GRB + NEO_KHZ800);

// LED 状态色: 红=WiFi未连接, 蓝=AP配网中, 绿=联网成功
#define LED_RED    pixel.Color(60, 0, 0)
#define LED_BLUE   pixel.Color(0, 0, 60)
#define LED_GREEN  pixel.Color(0, 60, 0)

void setLed(uint32_t color) {
  pixel.setPixelColor(0, color);
  pixel.show();
}

volatile uint32_t zeroFlag = 0;
volatile uint32_t zeroLastMs = 0;

uint32_t lastSend = 0;
uint32_t lastStatus = 0;
uint32_t lastSave = 0;
uint16_t prevRaw = 0xFFFF;
int stuckCnt = 0;

// ========== 光电零位中断 ==========
void IRAM_ATTR onZeroIRQ() {
  uint32_t now = millis();
  if (now - zeroLastMs < ZERO_DEBOUNCE_MS) return;
  zeroFlag++;
  zeroLastMs = now;
}

// ========== UDP 发送 ==========
void sendUDP(const char* payload) {
  if (!wifi.isConnected()) return;
  // 子网定向广播 (192.168.x.255): 换网络无需改目标 IP, 自动适应当前子网
  udp.beginPacket(WiFi.broadcastIP(), UDP_PORT);
  udp.write((const uint8_t*)payload, strlen(payload));
  udp.endPacket();
}

void setup() {
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);
  pixel.begin();
  setLed(LED_RED);   // 默认: WiFi 未连接

  Serial.begin(115200);
  unsigned long t = millis();
  while (!Serial && (millis() - t < 2000)) {
    digitalWrite(LED_PIN, !digitalRead(LED_PIN));
    delay(100);
  }

  Serial.println();
  Serial.println("====================");
  Serial.println("AS5600 Tracker (ESP32-S3 Arduino)");
  Serial.println("====================");

  storage.begin();

  // 光电零位
  if (ZERO_ENABLE) {
    pinMode(ZERO_PIN, INPUT_PULLUP);
    attachInterrupt(digitalPinToInterrupt(ZERO_PIN), onZeroIRQ, FALLING);
    Serial.printf("[zero] enabled on GPIO%d\n", ZERO_PIN);
  } else {
    Serial.println("[zero] disabled");
  }

  // I2C / AS5600
  if (!sensor.begin()) {
    Serial.println("[as5600] WARNING: not found!");
  } else {
    Serial.printf("[as5600] found at 0x%02X\n", sensor.address());
  }

  // 恢复累积角度
  float savedTotal = storage.getTotal();
  sensor.restoreTotal(savedTotal);
  Serial.printf("[state] restored total: %.2f\n", savedTotal);

  // WiFi
  wifi.begin();
  if (wifi.hasConfig()) {
    wifi.connect(15);
  }
  if (!wifi.isConnected()) {
    Serial.println("[wifi] starting AP config...");
    setLed(LED_BLUE);   // AP 配网中 -> 蓝色
    wifi.startAPConfig();
  }
  if (wifi.isConnected()) {
    setLed(LED_GREEN);  // 联网成功 -> 绿色
  }

  udp.begin(UDP_PORT);
  Serial.print("[main] started, IP: ");
  Serial.print(wifi.ip());
  Serial.printf(" UDP port: %d\n", UDP_PORT);
}

void loop() {
  wifi.reconnectTick();

  uint32_t now = millis();

  // 状态 LED: 绿=联网, 红=断线/重连中 (500ms 刷新)
  static uint32_t lastLed = 0;
  if (now - lastLed >= 500) {
    lastLed = now;
    setLed(wifi.isConnected() ? LED_GREEN : LED_RED);
  }

  // 光电零位事件
  if (ZERO_ENABLE && zeroFlag) {
    zeroFlag = 0;
    if (digitalRead(ZERO_PIN) == LOW) {
      float angle; uint16_t raw;
      if (sensor.readAngle(angle, raw)) {
        float absAngle = sensor.unwrap(angle);
        char payload[128];
        snprintf(payload, sizeof(payload),
                 "{\"event\":\"zero\",\"raw\":%u,\"angle\":%.2f,\"tick\":%lu}",
                 raw, absAngle, now);
        sendUDP(payload);
        Serial.printf("[zero] raw=%u abs=%.2f\n", raw, absAngle);
      }
    }
  }

  // 周期发送角度
  if (now - lastSend >= SEND_MS) {
    lastSend = now;
    float angle; uint16_t raw;
    if (sensor.readAngle(angle, raw)) {
      // 卡死检测
      if (AS5600Sensor::isRawStuck(raw, prevRaw)) {
        stuckCnt++;
        if (stuckCnt == 50) {
          Serial.printf("[warn] raw stuck at %u\n", raw);
        }
      } else {
        stuckCnt = 0;
      }
      prevRaw = raw;

      float absAngle = sensor.unwrap(angle);
      char payload[128];
      snprintf(payload, sizeof(payload),
               "{\"angle\":%.2f,\"raw\":%u,\"tick\":%lu}",
               absAngle, raw, now);
      sendUDP(payload);

      // 周期串口调试打印 (500ms)
      static uint32_t lastDbg = 0;
      if (now - lastDbg >= 500) {
        lastDbg = now;
        Serial.printf("[dbg] raw=%u angle=%.2f\n", raw, absAngle);
      }

      if (!wifi.isConnected()) {
        Serial.printf("[wifi] offline angle=%.2f\n", absAngle);
      }
    }
  }

  // 周期状态打印
  if (now - lastStatus >= STATUS_MS) {
    lastStatus = now;
    uint8_t st = sensor.readStatus();
    if (st != 0xFF) {
      bool md = (st & 0x20) != 0;
      bool ml = (st & 0x10) != 0;
      bool mh = (st & 0x08) != 0;
      if (!md || mh) {
        Serial.printf("[as5600] status=0x%02X MD=%d ML=%d MH=%d\n", st, md, ml, mh);
      }
    }
  }

  // 周期保存累积角度
  if (now - lastSave >= SAVE_MS) {
    lastSave = now;
    storage.setTotal(sensor.total());
  }

  delay(2);
}
