/*
 * 一次性 EEPROM 配网写入工具 (ESP8266)
 * 把 WiFi 凭据写进 EEPROM, 然后可烧回 tracker 固件
 */
#include <EEPROM.h>

#define EEPROM_SIZE 512
#define OFF_SSID    0
#define OFF_PASS    32
#define OFF_MAGIC   68

void writeStr(int off, const char* s, int len) {
  for (int i = 0; i < len; i++) {
    EEPROM.write(off + i, s[i] ? s[i] : 0);
    if (!s[i]) break;
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  EEPROM.begin(EEPROM_SIZE);
  writeStr(OFF_SSID, "CMCC-123", 32);
  writeStr(OFF_PASS, "19990621", 32);
  EEPROM.put(OFF_MAGIC, (uint32_t)0xA5A5A5A5);
  EEPROM.commit();
  Serial.println("WiFi saved to EEPROM: CMCC-123/19990621");
}

void loop() { delay(1000); }