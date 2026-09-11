/*
 * AS5600 测试 (Arduino ESP32-S3) - OTG USB-CDC 输出版
 * 通过 OTG 口 (原生 USB-CDC, GPIO19/20) 打印
 * 接线: SDA=GPIO4, SCL=GPIO5, VCC=3.3V, GND=GND
 */
#include <Wire.h>

#define AS5600_ADDR    0x36
#define REG_RAW_ANGLE  0x0C
#define REG_ANGLE      0x0E
#define REG_STATUS     0x0B
#define REG_AGC        0x1A
#define REG_CONF       0x07   // CONF 高字节: SF[1:0] + FTH[4:2] + WD[5]
#define REG_CONF_LOW   0x08   // CONF 低字节: HYST[3:2] + PM[1:0]
#define LED_PIN        2

class AS5600 {
public:
  uint8_t _addr = AS5600_ADDR;
  void begin(int sda, int scl, uint32_t freq = 100000) {
    Wire.begin(sda, scl, freq);
  }
  void setAddr(uint8_t a) { _addr = a; }
  bool found() {
    Wire.beginTransmission(_addr);
    return Wire.endTransmission() == 0;
  }
  uint16_t readAngle(uint8_t reg) {
    Wire.beginTransmission(_addr);
    Wire.write(reg);
    if (Wire.endTransmission(false) != 0) return 0xFFFF;
    Wire.requestFrom(_addr, (uint8_t)2);
    if (Wire.available() < 2) return 0xFFFF;
    uint16_t v = ((uint16_t)Wire.read() << 8) | Wire.read();
    return v & 0x0FFF;
  }
  uint8_t readStatus() {
    Wire.beginTransmission(_addr);
    Wire.write(REG_STATUS);
    if (Wire.endTransmission(false) != 0) return 0xFF;
    Wire.requestFrom(_addr, (uint8_t)1);
    return Wire.available() ? Wire.read() : 0xFF;
  }
  uint8_t readAGC() {
    Wire.beginTransmission(_addr);
    Wire.write(REG_AGC);
    if (Wire.endTransmission(false) != 0) return 0xFF;
    Wire.requestFrom(_addr, (uint8_t)1);
    return Wire.available() ? Wire.read() : 0xFF;
  }
  uint8_t readReg8(uint8_t reg) {
    Wire.beginTransmission(_addr);
    Wire.write(reg);
    if (Wire.endTransmission(false) != 0) return 0xFF;
    Wire.requestFrom(_addr, (uint8_t)1);
    return Wire.available() ? Wire.read() : 0xFF;
  }
  void writeReg8(uint8_t reg, uint8_t val) {
    Wire.beginTransmission(_addr);
    Wire.write(reg);
    Wire.write(val);
    Wire.endTransmission();
  }
  // 开启内置滤波: SF=11(慢速滤波最强) FTH=111(快速滤波阈值最大) HYST=11(迟滞3LSB), PM 保持 NOM
  // 写后回读验证; 返回 false = 总线异常写不进去
  bool enableFilter() {
    uint8_t beforeH = readReg8(REG_CONF);
    uint8_t beforeL = readReg8(REG_CONF_LOW);
    writeReg8(REG_CONF, 0x3F);
    writeReg8(REG_CONF_LOW, 0x0C);
    delay(10);
    uint8_t afterH = readReg8(REG_CONF);
    uint8_t afterL = readReg8(REG_CONF_LOW);
    Serial.printf("[conf] before=0x%02X%02X after=0x%02X%02X -> %s\n",
                  beforeH, beforeL, afterH, afterL,
                  (afterH == 0x3F && afterL == 0x0C) ? "OK" : "FAIL");
    return (afterH == 0x3F && afterL == 0x0C);
  }
};

AS5600 as5600;

void setup() {
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);

  as5600.begin(4, 5, 100000);

  // 初始化串口（Hardware CDC / USB-OTG CDC 均适用）
  Serial.setTxTimeoutMs(0);
  Serial.begin(115200);

  // 等待主机打开串口（最多 2 秒）
  unsigned long t = millis();
  while (!Serial && (millis() - t < 2000)) {
    digitalWrite(LED_PIN, !digitalRead(LED_PIN));
    delay(100);
  }

  Serial.println();
  Serial.println("====================");
  Serial.println("AS5600 test (Arduino, OTG USB-CDC)");
  Serial.println("====================");

  // 全地址段扫描多组 I2C 引脚组合: 找总线上所有设备 (含 AS5600L@0x40)
  const uint8_t pairs[][2] = {{8, 9}, {4, 5}, {1, 2}, {6, 7}, {17, 18}, {41, 42}};   // (SDA, SCL)
  int foundIdx = -1;
  uint8_t foundAddr = 0;
  for (int i = 0; i < (int)(sizeof(pairs) / sizeof(pairs[0])); i++) {
    as5600.begin(pairs[i][0], pairs[i][1], 100000);
    delay(5);
    Serial.printf("[scan] SDA=GPIO%d SCL=GPIO%d:", pairs[i][0], pairs[i][1]);
    int n = 0;
    for (uint8_t a = 0x08; a < 0x80; a++) {
      Wire.beginTransmission(a);
      uint8_t err = Wire.endTransmission();
      if (err == 0) {
        Serial.printf(" 0x%02X", a);
        n++;
        if (!foundAddr) { foundAddr = a; foundIdx = i; }
      } else if (a == 0x36 || a == 0x40) {
        Serial.printf(" 0x%02X(err%d)", a, err);   // 重点地址无应答时打印错误码
      }
    }
    Serial.printf(" (%d dev)", n);
    if (foundIdx == i) Serial.println(" <- device");
    else Serial.println(" <- empty");
  }

  if (foundAddr) {
    as5600.setAddr(foundAddr);
    Serial.printf("AS5600 using SDA=GPIO%d SCL=GPIO%d addr=0x%02X (%s)\n",
                  pairs[foundIdx][0], pairs[foundIdx][1], foundAddr,
                  foundAddr == 0x40 ? "AS5600L" : "AS5600");
    // 滤波已移除: 与 ESP8266 测试固件等价, 排除 CONF 写入差异
    // as5600.enableFilter();
  } else {
    Serial.println("ERROR: no device on bus (any pin pair). Check module power/wiring.");
  }
}

void loop() {
  static uint32_t last = 0;
  uint32_t now = millis();
  if (now - last < 25) return;   // 25ms 打印一次
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
