/*
 * WiFi 管理器：连接、非阻塞重连、AP 配网
 * 使用 Preferences 持久化配置
 */
#ifndef WIFI_MANAGER_H
#define WIFI_MANAGER_H

#include <WiFi.h>
#include <WebServer.h>
#include "StateStorage.h"

class WiFiManager {
public:
  WiFiManager(StateStorage& storage) : _storage(storage), _server(80) {}

  void begin() {
    WiFi.mode(WIFI_STA);
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

    WiFi.begin(ssid.c_str(), password.c_str());
    Serial.print("[wifi] connecting to ");
    Serial.println(ssid);

    for (int i = 0; i < timeoutS * 10; i++) {
      if (WiFi.status() == WL_CONNECTED) {
        Serial.print("[wifi] connected, IP: ");
        Serial.println(WiFi.localIP());
        _backoffIdx = 0;
        return true;
      }
      delay(100);
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

  // 非阻塞重连，在主循环中调用
  void reconnectTick() {
    if (isConnected()) {
      _backoffIdx = 0;
      return;
    }
    if (_apMode) return;

    unsigned long now = millis();
    if (now < _reconnectAt) return;

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

  // 启动 AP 配网模式，阻塞直到配网完成
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
  WebServer _server;
  bool _apMode = false;
  unsigned long _reconnectAt = 0;
  int _backoffIdx = 0;

  void handleRoot() {
    _server.send(200, "text/html", configPage());
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

  static String configPage() {
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
</style></head><body>
<div class="box">
<h1>AS5600 WiFi 配网</h1>
<form method="POST" action="/save">
<label>WiFi 名称</label>
<input name="ssid" placeholder="输入 WiFi SSID" required>
<label>WiFi 密码</label>
<input name="password" type="password" placeholder="输入密码">
<button type="submit">保存并重启</button>
</form>
<p class="hint">保存后 ESP32 将自动重启并连接 WiFi</p>
</div>
</body></html>)rawliteral";
  }
};

#endif
