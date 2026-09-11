/*
 * AS5600 测试 (Arduino ESP8266) - UART0 输出版
 * 通过 CH340 TTL 口打印
 * 接线: SDA=GPIO4, SCL=GPIO5, VCC=3.3V, GND=GND
 */
#include <Wire.h>

#define AS5600_ADDR    0x36
#define REG_RAW_ANGLE  0x0C
#define REG_ANGLE      0x0E
#define REG_STATUS     0x0B
#define REG_AGC        0x1A
#define LED_PIN        2   // ESP-12E 板载 LED 通常是 GPIO2

class AS5600 {
public:
  void begin(int sda, int scl, uint32_t freq = 100000) {
    Wire.begin(sda, scl);
    Wire.setClock(freq);
  }
  bool found() {
    Wire.beginTransmission(AS5600_ADDR);
    return Wire.endTransmission() == 0;
  }
  uint16_t readAngle(uint8_t reg) {
    Wire.beginTransmission(AS5600_ADDR);
    Wire.write(reg);
    if (Wire.endTransmission(false) != 0) return 0xFFFF;
    Wire.requestFrom(AS5600_ADDR, (uint8_t)2);
    if (Wire.available() < 2) return 0xFFFF;
    uint16_t v = ((uint16_t)Wire.read() << 8) | Wire.read();
    return v & 0x0FFF;
  }
  uint8_t readStatus() {
    Wire.beginTransmission(AS5600_ADDR);
    Wire.write(REG_STATUS);
    if (Wire.endTransmission(false) != 0) return 0xFF;
    Wire.requestFrom(AS5600_ADDR, (uint8_t)1);
    return Wire.available() ? Wire.read() : 0xFF;
  }
  uint8_t readAGC() {
    Wire.beginTransmission(AS5600_ADDR);
    Wire.write(REG_AGC);
    if (Wire.endTransmission(false) != 0) return 0xFF;
    Wire.requestFrom(AS5600_ADDR, (uint8_t)1);
    return Wire.available() ? Wire.read() : 0xFF;
  }
};

AS5600 as5600;

void setup() {
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);  // ESP-12E LED 低电平亮

  Serial.begin(115200);
  delay(300);

  as5600.begin(4, 5, 100000);

  Serial.println();
  Serial.println("====================");
  Serial.println("AS5600 test (Arduino ESP8266, UART0)");
  Serial.println("SDA=GPIO4 SCL=GPIO5 ADDR=0x36");
  Serial.println("====================");

  Serial.println("I2C scan:");
  int foundCount = 0;
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.printf("  found 0x%02X\n", addr);
      foundCount++;
      delay(5);
    }
  }
  if (foundCount == 0) {
    Serial.println("  no I2C device found!");
  }

  if (as5600.found()) {
    Serial.println("AS5600 found at 0x36");
  } else {
    Serial.println("ERROR: AS5600 not found at 0x36! Check wiring.");
  }
}

void loop() {
  static uint32_t last = 0;
  uint32_t now = millis();
  if (now - last < 200) return;
  last = now;

  digitalWrite(LED_PIN, !digitalRead(LED_PIN));

  uint8_t status = as5600.readStatus();
  bool md = (status & 0x20) != 0;
  bool ml = (status & 0x10) != 0;
  bool mh = (status & 0x08) != 0;
  uint16_t raw = as5600.readAngle(REG_RAW_ANGLE);
  uint16_t ang = as5600.readAngle(REG_ANGLE);
  uint8_t agc  = as5600.readAGC();

  Serial.printf("[%lu] raw=%4u ang=%4u agc=%3u status=0x%02X MD=%d ML=%d MH=%d\n",
                now, raw, ang, agc, status, md, ml, mh);
}
