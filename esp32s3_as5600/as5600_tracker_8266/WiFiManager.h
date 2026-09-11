/*
 * WiFi 管理器 (ESP8266 版): 连接、非阻塞重连、AP 配网
 * 使用 EEPROM 持久化配置
 */
#ifndef WIFI_MANAGER_H
#define WIFI_MANAGER_H

#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include "StateStorage.h"

class WiFiManager {
public:
  WiFiManager(StateStorage& storage) : _storage(storage), _server(80) {}

  void begin() {
    WiFi.mode(WIFI_STA);
    // 关闭 WiFi 省电模式: 弱信号下省电会导致连接失败/掉线
    WiFi.setSleepMode(WIFI_NONE_SLEEP);
    _apMode = false;
  }

  bool hasConfig() {
    return _storage.hasWiFi();
  }

  bool connect(int timeoutS = 15) {
    String ssid, password;
    _storage.getWiFi(ssid, password);
    if (ssid.length() == 0) return false;
    if (WiFi.status() == WL_CONNECTED) return true;

    Serial.print("[wifi] connecting to ");
    Serial.println(ssid);

    // 弱信号下连接不稳定, 多次尝试 (每次 timeoutS, 共 3 次)
    for (int attempt = 0; attempt < 3; attempt++) {
      if (attempt > 0) {
        Serial.printf("[wifi] retry %d...\n", attempt + 1);
        WiFi.disconnect();
        delay(500);
      }
      WiFi.begin(ssid.c_str(), password.c_str());
      for (int i = 0; i < timeoutS * 10; i++) {
        if (WiFi.status() == WL_CONNECTED) {
          Serial.print("[wifi] connected, IP: ");
          Serial.println(WiFi.localIP());
          _backoffIdx = 0;
          return true;
        }
        delay(100);
      }
    }
    Serial.println("[wifi] connect failed");
    return false;
  }

  bool isConnected() {
    return WiFi.status() == WL_CONNECTED;
  }

  IPAddress ip() {
    return isConnected() ? WiFi.localIP() : IPAddress(0, 0, 0, 0);
  }

  void reconnectTick() {
    if (isConnected()) {
      _backoffIdx = 0;
      return;
    }
    if (_apMode) return;

    unsigned long now = millis();
    // millis 回绕安全 (49.7 天): 用有符号差值判断是否到重连时刻
    if ((long)(now - _reconnectAt) < 0) return;

    String ssid, password;
    _storage.getWiFi(ssid, password);
    if (ssid.length() > 0) {
      Serial.printf("[wifi] reconnect attempt %d\n", _backoffIdx + 1);
      WiFi.begin(ssid.c_str(), password.c_str());
    }

    const unsigned long backoff[] = {5000, 10000, 30000};
    unsigned long delayMs = backoff[_backoffIdx % 3];
    _reconnectAt = now + delayMs;
    _backoffIdx++;
  }

  void startAPConfig() {
    _apMode = true;
    WiFi.mode(WIFI_AP);
    WiFi.softAP("AS5600-Setup", "12345678");

    IPAddress ip = WiFi.softAPIP();
    Serial.print("[wifi] AP mode started: AS5600-Setup, IP: ");
    Serial.println(ip);

    _server.on("/", HTTP_GET, [this]() { handleRoot(); });
    _server.on("/save", HTTP_POST, [this]() { handleSave(); });
    _server.begin();

    while (_apMode) {
      _server.handleClient();
      delay(5);
    }
  }

private:
  StateStorage& _storage;
  ESP8266WebServer _server;
  bool _apMode = false;
  unsigned long _reconnectAt = 0;
  int _backoffIdx = 0;

  void handleRoot() {
    // 扫描 WiFi 网络 (AP 模式下 STA 接口仍可扫描)
    int n = WiFi.scanNetworks();
    String nets = "";
    if (n > 0) {
      for (int i = 0; i < n; i++) {
        String ssid = WiFi.SSID(i);
        if (ssid.length() == 0) continue;  // 隐藏 SSID 跳过
        int rssi = WiFi.RSSI(i);
        // 信号强度颜色: >=-60 绿, >=-70 黄, 否则红
        const char* color = rssi >= -60 ? "#4ade80" : (rssi >= -70 ? "#facc15" : "#f87171");
        // 转义 HTML 特殊字符, 防止 SSID 注入
        ssid.replace("&", "&amp;");
        ssid.replace("<", "&lt;");
        ssid.replace(">", "&gt;");
        ssid.replace("\"", "&quot;");
        ssid.replace("'", "&#39;");
        nets += "<div class=\"net-row\" data-ssid=\"" + ssid + "\" onclick=\"pick(this)\">"
                "<span class=\"net-ssid\">" + ssid + "</span>"
                "<span class=\"net-rssi\" style=\"color:" + String(color) + "\">" + String(rssi) + " dBm</span>"
                "</div>";
      }
    } else {
      nets = "<div class=\"net-row\"><span class=\"net-ssid\">未扫描到 WiFi</span></div>";
    }
    _server.send(200, "text/html", configPage(nets));
  }

  void handleSave() {
    String ssid = _server.arg("ssid");
    String password = _server.arg("password");
    ssid.trim();
    password.trim();

    if (ssid.length() > 0) {
      _storage.setWiFi(ssid, password);
      _server.send(200, "text/html",
        "<h1>Saved! Rebooting...</h1><p>SSID: " + ssid + "</p>");
      delay(1000);
      ESP.restart();
    } else {
      _server.send(400, "text/plain", "Invalid SSID");
    }
  }

  static String configPage(const String& nets) {
    return R"rawliteral(<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AS5600 WiFi 配网</title>
<style>
body{font-family:sans-serif;background:#1e293b;color:#e2e8f0;margin:0;padding:20px}
h1{color:#7dd3fc;font-size:18px}
.box{background:#0f172a;border-radius:10px;padding:20px;max-width:400px;margin:0 auto}
input{width:100%;padding:12px;margin:8px 0;border:1px solid #475569;border-radius:8px;
background:#1e293b;color:#e2e8f0;font-size:16px;box-sizing:border-box}
button{width:100%;padding:14px;border:none;border-radius:8px;background:#0ea5e9;
color:#fff;font-size:16px;cursor:pointer;margin-top:8px}
button:active{background:#0284c7}
label{font-size:13px;color:#94a3b8}
.hint{font-size:12px;color:#64748b;margin-top:12px}
.nets{margin-top:14px;border-top:1px solid #334155;padding-top:10px}
.net-title{font-size:13px;color:#94a3b8;margin-bottom:6px}
.net-row{display:flex;justify-content:space-between;align-items:center;padding:9px 10px;
margin:4px 0;background:#1e293b;border:1px solid #334155;border-radius:8px;
cursor:pointer;font-size:14px}
.net-row:active{background:#334155}
.net-ssid{color:#e2e8f0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-right:8px}
.net-rssi{font-size:12px;flex-shrink:0}
</style></head><body>
<div class="box">
<h1>AS5600 WiFi 配网</h1>
<form method="POST" action="/save">
<label>WiFi 名称</label>
<input name="ssid" id="ssid" placeholder="输入 WiFi SSID" required>
<label>WiFi 密码</label>
<input name="password" type="password" placeholder="输入密码">
<button type="submit">保存并重启</button>
</form>
<div class="nets">
<div class="net-title">扫描到的 WiFi（点击选择）</div>
)rawliteral" + nets + R"rawliteral(
</div>
<p class="hint">保存后 ESP8266 将自动重启并连接 WiFi</p>
</div>
<script>
function pick(el){ document.getElementById('ssid').value = el.dataset.ssid; }
</script>
</body></html>)rawliteral";
  }
};

#endif