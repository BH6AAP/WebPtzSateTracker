/*
 * OTA 固件更新 (ESP8266 版): HTTP 网页上传 + ArduinoOTA 双通道
 * - HTTP: 浏览器访问 http://<esp-ip>:8080 上传 .bin (需口令)
 * - ArduinoOTA: Arduino IDE "通过网络端口" 直传 (UDP 8266, 局域网)
 */
#ifndef OTA_UPDATE_H
#define OTA_UPDATE_H

#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <ArduinoOTA.h>
#include <Updater.h>

#define OTA_PORT        8080
#define OTA_TOKEN       "ptz2026"       // 上传口令 (网页表单校验)
// OTA 分区大小: NodeMCU 4M flash 默认布局 OTA 分区 1MB; 不能超过实际分区
#define OTA_MAX_SKETCH  (0x100000)      // 1MB

namespace OTAUpdate {

ESP8266WebServer server(OTA_PORT);

static void handleUpload() {
  if (server.arg("token") != OTA_TOKEN) {
    server.send(403, "text/plain", "Forbidden: bad token");
    return;
  }
  HTTPUpload& up = server.upload();
  if (up.status == UPLOAD_FILE_START) {
    Serial.printf("[ota] upload start: %s (%u bytes)\n",
                  up.filename.c_str(), up.contentLength);
    if (!Update.begin(OTA_MAX_SKETCH)) {
      Update.printError(Serial);
    }
  } else if (up.status == UPLOAD_FILE_WRITE) {
    if (Update.write(up.buf, up.currentSize) != up.currentSize) {
      Update.printError(Serial);
    }
  } else if (up.status == UPLOAD_FILE_END) {
    if (Update.end(true)) {
      Serial.printf("[ota] success %u bytes, rebooting...\n", up.totalSize);
      server.send(200, "text/plain", "OK. Rebooting...");
      delay(1000);
      ESP.restart();
    } else {
      Update.printError(Serial);
      server.send(500, "text/plain", "Update failed");
    }
  }
}

static const char PAGE[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AS5600 OTA 固件更新</title>
<style>
body{font-family:sans-serif;background:#1e293b;color:#e2e8f0;margin:0;padding:20px}
.box{background:#0f172a;border-radius:10px;padding:24px;max-width:420px;margin:0 auto}
h1{color:#7dd3fc;font-size:18px;margin-top:0}
label{display:block;font-size:13px;color:#94a3b8;margin:12px 0 4px}
input[type=text],input[type=file]{width:100%;padding:10px;margin:4px 0;
border:1px solid #475569;border-radius:8px;background:#1e293b;color:#e2e8f0;
font-size:14px;box-sizing:border-box}
button{width:100%;padding:13px;margin-top:16px;border:none;border-radius:8px;
background:#0ea5e9;color:#fff;font-size:15px;font-weight:600;cursor:pointer}
button:disabled{background:#475569;cursor:not-allowed}
#msg{font-size:12px;color:#f87171;margin-top:10px;min-height:16px}
</style></head><body>
<div class="box">
<h1>AS5600 固件更新</h1>
<form id=f onsubmit="return doUp(this)">
<label>更新口令</label>
<input type="text" name="token" placeholder="OTA_TOKEN" autocomplete="off">
<label>固件文件 (.bin)</label>
<input type="file" name="fw" accept=".bin">
<button id=btn>开始上传</button>
<div id=msg></div>
</form>
</div>
<script>
function doUp(f){
  var tok=f.token.value.trim();
  var file=f.fw.files[0];
  if(!tok){msg('请输入口令');return false;}
  if(!file){msg('请选择 .bin 文件');return false;}
  var fd=new FormData();
  fd.append('token',tok);
  fd.append('sketch',file,file.name);
  var x=new XMLHttpRequest();
  x.open('POST','/update',true);
  x.upload.onprogress=function(e){
    if(e.lengthComputable){
      var p=Math.round(e.loaded/e.total*100);
      btn.textContent='上传中 '+p+'%';
    }
  };
  x.onload=function(){ msg(x.status==200?'上传成功，设备重启中...':'失败: '+x.status); };
  x.onerror=function(){ msg('网络错误'); };
  x.send(fd);
  return false;
}
function msg(s){document.getElementById('msg').textContent=s;}
</script>
</body></html>
)rawliteral";

void begin() {
  // ArduinoOTA
  ArduinoOTA.setHostname("as5600-tracker");
  ArduinoOTA.onStart([]() {
    Serial.println("[ota] ArduinoOTA start");
  });
  ArduinoOTA.onEnd([]() {
    Serial.println("[ota] ArduinoOTA done");
  });
  ArduinoOTA.onProgress([](unsigned int p, unsigned int t) {
    Serial.printf("[ota] %u/%u\r", p, t);
  });
  ArduinoOTA.onError([](ota_error_t e) {
    Serial.printf("[ota] error %u\n", e);
    ESP.restart();
  });
  ArduinoOTA.begin();

  // HTTP OTA
  server.on("/", HTTP_GET, []() {
    server.send(200, "text/html", FPSTR(PAGE));
  });
  server.on("/update", HTTP_POST,
            []() {
              // 兜底: 无文件/无上传回调时也需响应, 否则连接挂起 (curl 超时)
              server.send(400, "text/plain", "No file uploaded");
            },
            handleUpload);
  server.begin();

  Serial.printf("[ota] HTTP :%d + ArduinoOTA ready\n", OTA_PORT);
}

void handle() {
  ArduinoOTA.handle();
  server.handleClient();
}

}  // namespace OTAUpdate

#endif