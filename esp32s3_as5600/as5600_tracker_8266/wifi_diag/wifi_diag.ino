/*
 * WiFi 诊断 v2 (ESP8266): 关闭省电 + 详细错误码 + 错误密码对照测试
 */
#include <ESP8266WiFi.h>
#include <EEPROM.h>

#define EEPROM_SIZE 512
#define OFF_SSID    0
#define OFF_PASS    32

void readStr(int off, char* buf, int len) {
  for (int i = 0; i < len; i++) {
    char c = EEPROM.read(off + i);
    if (c == 0) { buf[i] = 0; return; }
    buf[i] = c;
  }
  buf[len] = 0;
}

const char* statusStr(int st) {
  switch (st) {
    case WL_IDLE_STATUS: return "IDLE";
    case WL_NO_SSID_AVAIL: return "NO_SSID_AVAIL";
    case WL_CONNECTED: return "CONNECTED";
    case WL_CONNECT_FAILED: return "CONNECT_FAILED";
    case WL_WRONG_PASSWORD: return "WRONG_PASSWORD";
    case WL_DISCONNECTED: return "DISCONNECTED";
    default: return "OTHER";
  }
}

void tryConnect(const char* ssid, const char* pass, int seconds, const char* tag) {
  Serial.printf("\n[%s] connecting to '%s' pass='%s'...\n", tag, ssid, pass);
  WiFi.disconnect();
  delay(200);
  WiFi.begin(ssid, pass);
  for (int i = 0; i < seconds * 2; i++) {
    delay(500);
    int st = WiFi.status();
    Serial.printf("[%s] t=%ds status=%d(%s)\n",
                  tag, (i + 1) / 2, st, statusStr(st));
    if (st == WL_CONNECTED) {
      Serial.printf("[%s] CONNECTED! IP=%s\n", tag, WiFi.localIP().toString().c_str());
      return;
    }
  }
  Serial.printf("[%s] FAILED\n", tag);
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println();
  Serial.println("==== WiFi Diag v2 (ESP8266) ====");

  // 关闭 WiFi 省电模式 (弱信号下省电会导致连接失败)
  WiFi.setSleepMode(WIFI_NONE_SLEEP);
  Serial.println("[cfg] WiFi sleep disabled");

  EEPROM.begin(EEPROM_SIZE);
  char ssid[33], pass[33];
  readStr(OFF_SSID, ssid, 32);
  readStr(OFF_PASS, pass, 32);
  Serial.printf("[eeprom] ssid='%s' pass='%s'\n", ssid, pass);

  // 1. 扫描
  Serial.println("[scan] scanning...");
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  delay(200);
  int n = WiFi.scanNetworks();
  Serial.printf("[scan] found %d networks\n", n);
  for (int i = 0; i < n; i++) {
    Serial.printf("[scan] %2d: %-20s RSSI=%d dBm ch=%d\n",
                  i, WiFi.SSID(i).c_str(), WiFi.RSSI(i), WiFi.channel(i));
  }

  // 2. 正确密码连接 (15s)
  tryConnect(ssid, pass, 15, "good");

  // 3. 故意错误密码连接 (10s) - 验证 WRONG_PASSWORD 上报
  tryConnect(ssid, "wrongpass123", 10, "bad");

  // 4. 再试一次正确密码 (15s)
  tryConnect(ssid, pass, 15, "good2");

  Serial.println("\n==== Diag done ====");
}

void loop() { delay(1000); }
