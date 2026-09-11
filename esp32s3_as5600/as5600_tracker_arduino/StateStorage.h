/*
 * 状态持久化：使用 ESP32 Preferences (NVS)
 * 保存 WiFi 配置与累积角度
 */
#ifndef STATE_STORAGE_H
#define STATE_STORAGE_H

#include <Preferences.h>

class StateStorage {
public:
  StateStorage(const char* ns = "ptztracker") : _ns(ns) {}

  bool begin() {
    return _prefs.begin(_ns, false);
  }

  void end() {
    _prefs.end();
  }

  bool hasWiFi() {
    return _prefs.getString("ssid", "").length() > 0;
  }

  void getWiFi(String& ssid, String& password) {
    ssid = _prefs.getString("ssid", "");
    password = _prefs.getString("password", "");
  }

  void setWiFi(const String& ssid, const String& password) {
    _prefs.putString("ssid", ssid);
    _prefs.putString("password", password);
  }

  float getTotal() {
    return _prefs.getFloat("total", 0.0f);
  }

  void setTotal(float total) {
    _prefs.putFloat("total", total);
  }

  void clearTotal() {
    _prefs.remove("total");
  }

private:
  const char* _ns;
  Preferences _prefs;
};

#endif
