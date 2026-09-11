/*
 * AS5600 磁编码器驱动
 * 支持多地址探测、角度读取、状态读取、跨圈累积
 */
#ifndef AS5600_SENSOR_H
#define AS5600_SENSOR_H

#include <Wire.h>

#define AS5600_REG_RAW_ANGLE 0x0C
#define AS5600_REG_ANGLE     0x0E
#define AS5600_REG_STATUS    0x0B
#define AS5600_REG_AGC       0x1A

class AS5600Sensor {
public:
  AS5600Sensor(uint8_t sda, uint8_t scl, uint32_t freq = 100000)
    : _sda(sda), _scl(scl), _freq(freq), _addr(0x36) {}

  bool begin() {
    Wire.begin(_sda, _scl, _freq);
    _addr = detectAddress();
    return _addr != 0;
  }

  uint8_t address() const { return _addr; }

  bool found() const {
    Wire.beginTransmission(_addr);
    return Wire.endTransmission() == 0;
  }

  // 读取 12bit 角度并转成 0-360 度
  bool readAngle(float& angle, uint16_t& raw) {
    uint16_t v = readWord(AS5600_REG_ANGLE);
    if (v == 0xFFFF) return false;
    raw = v & 0x0FFF;
    angle = (raw * 360.0f) / 4096.0f;
    return true;
  }

  // 读取原始 12bit 值
  uint16_t readRaw() {
    return readWord(AS5600_REG_RAW_ANGLE) & 0x0FFF;
  }

  // 读取状态寄存器，bit5=MD, bit4=ML, bit3=MH
  uint8_t readStatus() {
    return readByte(AS5600_REG_STATUS);
  }

  uint8_t readAGC() {
    return readByte(AS5600_REG_AGC);
  }

  // 跨圈解卷，输入单圈角度 0-360，输出累积角度
  float unwrap(float angle) {
    if (!_init) {
      _total = _restoredValid ? _restoredTotal : angle;
      _last = angle;
      _init = true;
      return _total;
    }
    float delta = angle - _last;
    if (delta > 180.0f) delta -= 360.0f;
    else if (delta < -180.0f) delta += 360.0f;
    _total += delta;
    _last = angle;
    return _total;
  }

  void restoreTotal(float total) {
    _restoredTotal = total;
    _restoredValid = true;
  }

  float total() const { return _total; }

  static bool isRawStuck(uint16_t raw, uint16_t prev) {
    return (prev != 0xFFFF) && (raw == prev);
  }

private:
  uint8_t _sda, _scl;
  uint32_t _freq;
  uint8_t _addr;
  bool _init = false;
  float _last = 0;
  float _total = 0;
  float _restoredTotal = 0;
  bool _restoredValid = false;

  uint8_t detectAddress() {
    const uint8_t candidates[] = {0x36, 0x35, 0x34, 0x37};
    for (uint8_t addr : candidates) {
      Wire.beginTransmission(addr);
      if (Wire.endTransmission() == 0) return addr;
    }
    return 0;
  }

  uint16_t readWord(uint8_t reg) {
    Wire.beginTransmission(_addr);
    Wire.write(reg);
    if (Wire.endTransmission(false) != 0) return 0xFFFF;
    Wire.requestFrom(_addr, (uint8_t)2);
    if (Wire.available() < 2) return 0xFFFF;
    uint16_t v = ((uint16_t)Wire.read() << 8) | Wire.read();
    return v;
  }

  uint8_t readByte(uint8_t reg) {
    Wire.beginTransmission(_addr);
    Wire.write(reg);
    if (Wire.endTransmission(false) != 0) return 0xFF;
    Wire.requestFrom(_addr, (uint8_t)1);
    return Wire.available() ? Wire.read() : 0xFF;
  }
};

#endif
