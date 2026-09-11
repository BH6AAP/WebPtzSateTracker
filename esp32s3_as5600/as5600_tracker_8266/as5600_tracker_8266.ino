/*
 * AS5600 磁编码器角度采集 + UDP 广播上传 (ESP8266 Arduino)
 * 功能: WiFi 配网/连接/重连、AP 配网、UDP 状态上报
 * 输出: UART0 (115200)
 */
#include <ESP8266WiFi.h>
#include <WiFiUDP.h>
#include <Wire.h>
#include "AS5600Sensor.h"
#include "StateStorage.h"
#include "WiFiManager.h"
#include "OTAUpdate.h"

// ========== 配置 ==========
#define FIRMWARE_VERSION "2.0.3"    // v2.0.3: FALLING+脉宽确认 (修复CHANGE状态机ESP8266不可靠)
#define UDP_PORT      8091
#define SEND_MS       50          // 20Hz
#define STATUS_MS     3000        // 状态打印周期
#define SAVE_MS       5000        // 累积角度保存检查周期
#define SAVE_DELTA    2.0f        // 累积角度变化 >2° 才写 EEPROM (防磨损: 5s写一次约6天报废)
#define LED_PIN       2           // 板载 LED (ESP-12E 低电平亮)
#define ZERO_PIN      12          // 光电零位传感器 (D6)
#define ZERO_ENABLE   true        // 光电零位开关
#define ZERO_DEBOUNCE_MS 50       // 中断去抖 (ms): 挡片遮挡 >> 50ms, 够滤边缘抖动
#define ZERO_MIN_WIDTH_MS 5       // 遮挡最短时长 (ms): loop 确认用, 短于为噪声

// I2C 引脚 (与 8266 测试固件一致, 已验证正常)
#define SDA_PIN       4
#define SCL_PIN       5

// ========== 全局对象 ==========
AS5600Sensor sensor(SDA_PIN, SCL_PIN, 100000);
StateStorage storage;
WiFiManager wifi(storage);
WiFiUDP udp;

bool ledState = false;

uint32_t lastSend = 0;
uint32_t lastStatus = 0;
uint32_t lastSave = 0;
uint16_t prevRaw = 0xFFFF;
int stuckCnt = 0;
float lastSavedTotal = 0;      // 上次 EEPROM 保存的累积角度 (防磨损用)
float _angleRate = 0.0f;        // 最近角速度 (ESP 累积角度域 °/s), 光电触发角度补偿用
uint32_t _lastRateT = 0;
float _lastRateAngle = 0.0f;

volatile uint32_t zeroFlag = 0;
volatile uint32_t zeroCount = 0;
volatile uint32_t zeroTrigMs = 0;   // FALLING 中断时刻 (ms): 用于 loop 脉宽确认

void ICACHE_RAM_ATTR onZeroIRQ() {
  uint32_t now = millis();
  if (now - zeroTrigMs < ZERO_DEBOUNCE_MS) return;  // 中断去抖
  zeroTrigMs = now;   // 记录触发时刻, loop 里做脉宽确认
  zeroFlag++;
}

// ========== UDP 发送 ==========

// ========== UDP 发送 ==========
void sendUDP(const char* payload) {
  if (!wifi.isConnected()) return;
  // 子网定向广播 (192.168.x.255): 换网络无需改目标 IP
  udp.beginPacket(WiFi.broadcastIP(), UDP_PORT);
  udp.write((const uint8_t*)payload, strlen(payload));
  udp.endPacket();
}

void setLedOn(bool on) {
  digitalWrite(LED_PIN, on ? LOW : HIGH);   // 低电平点亮
}

void setup() {
  pinMode(LED_PIN, OUTPUT);
  setLedOn(true);

  Serial.begin(115200);
  delay(300);

  Serial.println();
  Serial.println("====================");
  Serial.println("AS5600 Tracker (ESP8266)");
  Serial.printf("Firmware v%s\n", FIRMWARE_VERSION);
  Serial.println("====================");

  storage.begin();

  // 光电零位
  if (ZERO_ENABLE) {
    pinMode(ZERO_PIN, INPUT_PULLUP);
    attachInterrupt(digitalPinToInterrupt(ZERO_PIN), onZeroIRQ, FALLING);
    Serial.printf("[zero] enabled on GPIO%d (FALLING+脉宽确认)\n", ZERO_PIN);
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
  lastSavedTotal = savedTotal;
  Serial.printf("[state] restored total: %.2f\n", savedTotal);

  // WiFi
  wifi.begin();
  if (wifi.hasConfig()) {
    wifi.connect(15);
  }
  if (!wifi.isConnected()) {
    Serial.println("[wifi] starting AP config...");
    wifi.startAPConfig();
  }

  udp.begin(UDP_PORT);
  Serial.print("[main] started, IP: ");
  Serial.print(wifi.ip());
  Serial.printf(" UDP port: %d\n", UDP_PORT);

  OTAUpdate::begin();
}

void loop() {
  wifi.reconnectTick();
  OTAUpdate::handle();

  uint32_t now = millis();

  // 状态 LED: 亮=联网, 闪=断线/重连中 (500ms 刷新)
  static uint32_t lastLed = 0;
  if (now - lastLed >= 500) {
    lastLed = now;
    ledState = !ledState;
    if (wifi.isConnected()) setLedOn(true);
    else setLedOn(ledState);
  }

  // 光电零位事件: FALLING 中断 + loop 脉宽确认
  if (ZERO_ENABLE && zeroFlag) {
    zeroFlag = 0;
    // 确认遮挡持续时间 (脉宽): LOOP 轮询 LOW 状态, 过滤极窄噪声毛刺
    uint32_t widthMs = 0;
    while (widthMs < 200 && digitalRead(ZERO_PIN) == LOW) {
      delay(1);
      widthMs++;
    }
    if (widthMs >= ZERO_MIN_WIDTH_MS) {
      // 脉宽确认: 真实挡片遮挡 (开始遮挡→出遮挡), 触发可靠
      float angle; uint16_t raw;
      if (sensor.readAngle(angle, raw)) {
        float absAngle = sensor.unwrap(angle);
        // 角度补偿: loop 读取滞后于 FALLING 中断时刻, 按最近角速度回推
        float dt = (float)(now - zeroTrigMs) / 1000.0f;
        if (dt > 0.001f && dt < 2.0f) {
          absAngle -= _angleRate * dt;
        }
        char payload[128];
        snprintf(payload, sizeof(payload),
                 "{\"event\":\"zero\",\"raw\":%u,\"angle\":%.2f,\"tick\":%lu,\"rssi\":%d,\"ver\":\"%s\",\"zc\":%lu}",
                 raw, absAngle, now, WiFi.RSSI(), FIRMWARE_VERSION, zeroCount);
        for (int i = 0; i < 3; i++) {
          sendUDP(payload);
          if (i < 2) delay(100);
        }
        zeroCount++;
        Serial.printf("[zero] raw=%u abs=%.2f width=%lums\n", raw, absAngle, widthMs);
      }
    } else {
      Serial.printf("[zero] 忽略窄脉冲 width=%lums\n", widthMs);
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
      // 角速度 (ESP 累积角度域 °/s): 用于光电触发时刻角度补偿
      uint32_t nowMs = now;
      if (_lastRateT != 0) {
        float dtS = (nowMs - _lastRateT) / 1000.0f;
        if (dtS > 0.01f && dtS < 2.0f) {
          float r = (absAngle - _lastRateAngle) / dtS;
          // 限幅: 正常云台速度 ~30°ESP/s (7.5°物理), 防毛刺污染
          if (r > -800.0f && r < 800.0f) _angleRate = r;
        }
      }
      _lastRateT = nowMs;
      _lastRateAngle = absAngle;
      char payload[128];
      snprintf(payload, sizeof(payload),
               "{\"angle\":%.2f,\"raw\":%u,\"tick\":%lu,\"rssi\":%d,\"ver\":\"%s\"}",
               absAngle, raw, now, WiFi.RSSI(), FIRMWARE_VERSION);
      sendUDP(payload);

      // 周期串口调试打印 (500ms)
      static uint32_t lastDbg = 0;
      if (now - lastDbg >= 500) {
        lastDbg = now;
        Serial.printf("[dbg] raw=%u angle=%.2f rssi=%d\n", raw, absAngle, WiFi.RSSI());
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

  // 周期保存累积角度 (仅在角度变化超阈值时写 EEPROM, 防磨损)
  if (now - lastSave >= SAVE_MS) {
    lastSave = now;
    float cur = sensor.total();
    if (fabs(cur - lastSavedTotal) >= SAVE_DELTA) {
      storage.setTotal(cur);
      lastSavedTotal = cur;
    }
  }

  delay(2);
}