/*
 * 状态持久化 (ESP8266 版): 用 EEPROM 模拟替代 ESP32 Preferences
 * 布局: [0..31]=ssid [32..63]=password [64..67]=total(float) [68..71]=magic
 */
#ifndef STATE_STORAGE_H
#define STATE_STORAGE_H

#include <EEPROM.h>

#define EEPROM_SIZE  512
#define OFF_SSID     0
#define OFF_PASS     32
#define OFF_TOTAL    64
#define OFF_TOTAL_MAGIC 72   // 累积角度有效性标记 (防止未初始化垃圾值)
#define OFF_MAGIC    68

class StateStorage {
public:
  bool begin() {
    EEPROM.begin(EEPROM_SIZE);
    return true;
  }

  bool hasWiFi() {
    return readMag() == 0xA5A5A5A5;
  }

  void getWiFi(String& ssid, String& password) {
    char buf[33];
    readStr(OFF_SSID, buf, 32);
    ssid = buf;
    readStr(OFF_PASS, buf, 32);
    password = buf;
  }

  void setWiFi(const String& ssid, const String& password) {
    writeStr(OFF_SSID, ssid.c_str(), 32);
    writeStr(OFF_PASS, password.c_str(), 32);
    writeMag();
    EEPROM.commit();
  }

  float getTotal() {
    uint32_t m;
    EEPROM.get(OFF_TOTAL_MAGIC, m);
    if (m != 0xB0B1B2B3) return 0.0f;   // 未写入过有效值, 从 0 开始
    float v;
    EEPROM.get(OFF_TOTAL, v);
    return isnan(v) ? 0.0f : v;
  }

  void setTotal(float total) {
    EEPROM.put(OFF_TOTAL, total);
    EEPROM.put(OFF_TOTAL_MAGIC, (uint32_t)0xB0B1B2B3);
    EEPROM.commit();
  }

private:
  void readStr(int off, char* buf, int len) {
    for (int i = 0; i < len; i++) {
      char c = EEPROM.read(off + i);
      if (c == 0) { buf[i] = 0; return; }
      buf[i] = c;
    }
    buf[len] = 0;
  }

  void writeStr(int off, const char* s, int len) {
    for (int i = 0; i < len; i++) {
      EEPROM.write(off + i, s[i] ? s[i] : 0);
      if (!s[i]) break;
    }
  }

  uint32_t readMag() {
    uint32_t m;
    EEPROM.get(OFF_MAGIC, m);
    return m;
  }

  void writeMag() {
    EEPROM.put(OFF_MAGIC, (uint32_t)0xA5A5A5A5);
  }
};

#endif