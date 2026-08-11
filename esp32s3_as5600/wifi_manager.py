# WiFi 管理器: 非阻塞重连 + Web 配网
import network
import socket
import time
import json
import os

CONFIG_FILE = "wifi_config.json"


class WiFiManager:
    """WiFi 连接管理: 自动重连(非阻塞, 退避) + AP 配网模式"""

    # 退避间隔(秒), 循环使用
    BACKOFF = [5, 10, 30]

    def __init__(self):
        self.wlan = network.WLAN(network.STA_IF)
        self.wlan.active(True)
        self._config = None
        self._reconnect_at = 0
        self._backoff_idx = 0
        self._ap_mode = False

    # ---------- 配置持久化 ----------
    def load_config(self):
        """从 flash 读取 WiFi 配置, 返回 dict 或 None"""
        try:
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f)
                if cfg.get("ssid") and cfg.get("password") is not None:
                    self._config = cfg
                    return cfg
        except Exception:
            pass
        return None

    def save_config(self, ssid, password):
        """保存 WiFi 配置到 flash"""
        self._config = {"ssid": ssid, "password": password}
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(self._config, f)
        except Exception:
            pass

    # ---------- 连接 ----------
    def is_connected(self):
        return self.wlan.isconnected()

    def ip(self):
        return self.wlan.ifconfig()[0] if self.is_connected() else "0.0.0.0"

    def connect(self, timeout_s=15):
        """阻塞连接 (启动时用), 成功返回 True"""
        if not self._config:
            return False
        if self.is_connected():
            return True
        ssid = self._config["ssid"]
        pwd = self._config["password"]
        print("[wifi] connecting to", ssid)
        self.wlan.connect(ssid, pwd)
        for _ in range(timeout_s * 2):
            if self.is_connected():
                print("[wifi] connected, IP:", self.ip())
                self._backoff_idx = 0
                return True
            time.sleep(0.5)
        print("[wifi] connect failed")
        return False

    def reconnect_tick(self):
        """非阻塞重连: 在主循环中每帧调用, 到时间点才触发连接尝试

        - 连接正常时直接返回
        - 断线后按退避间隔重试 (5s -> 10s -> 30s 循环)
        - 重连期间不阻塞主循环
        """
        if self.is_connected():
            self._backoff_idx = 0
            return
        if self._ap_mode:
            return  # 配网模式中, 不自动重连
        now = time.ticks_ms()
        if now < self._reconnect_at:
            return
        # 触发一次连接 (wlan.connect 本身非阻塞, 内部异步)
        if self._config:
            print("[wifi] reconnect attempt", self._backoff_idx + 1)
            self.wlan.connect(self._config["ssid"], self._config["password"])
        # 设定下次重试时间
        delay = self.BACKOFF[self._backoff_idx % len(self.BACKOFF)]
        self._reconnect_at = time.ticks_add(now, delay * 1000)
        self._backoff_idx += 1

    # ---------- AP 配网模式 ----------
    def start_ap_config(self):
        """启动 AP 热点 + 内置 Web 页面, 用户手机连上后输入 WiFi 密码

        配网成功后保存配置并重启
        """
        self._ap_mode = True
        ap = network.WLAN(network.AP_IF)
        ap.active(True)
        ap.config(essid="AS5600-Setup", password="12345678")
        ip = ap.ifconfig()[0]
        print("[wifi] AP mode started: AS5600-Setup")
        print("[wifi] Open http://%s/ to configure WiFi" % ip)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", 80))
        sock.listen(3)
        sock.settimeout(1.0)

        while self._ap_mode:
            try:
                conn, addr = sock.accept()
                self._handle_http(conn)
            except OSError:
                pass  # timeout, 继续等待
            except Exception as e:
                print("[wifi] AP error:", e)

    def _handle_http(self, conn):
        """处理配网 HTTP 请求"""
        try:
            req = conn.recv(1024).decode("utf-8", "replace")
            line = req.split("\r\n")[0] if req else ""
            # GET / -> 返回配网页面
            if "GET /" in line and "POST" not in line:
                html = self._config_page()
                conn.send("HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n")
                conn.sendall(html)
            # POST /save -> 保存配置
            elif "POST /save" in line:
                body = req.split("\r\n\r\n", 1)[-1] if "\r\n\r\n" in req else ""
                params = self._parse_form(body)
                ssid = params.get("ssid", "").strip()
                pwd = params.get("password", "").strip()
                if ssid:
                    self.save_config(ssid, pwd)
                    conn.send("HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n")
                    conn.sendall("<h1>Saved! Rebooting...</h1>"
                                 "<p>SSID: %s</p>" % ssid)
                    conn.close()
                    time.sleep(1)
                    import machine
                    machine.reset()
                else:
                    conn.send("HTTP/1.1 400 Bad Request\r\n\r\nInvalid SSID")
            conn.close()
        except Exception as e:
            print("[wifi] HTTP error:", e)
            try:
                conn.close()
            except Exception:
                pass

    @staticmethod
    def _parse_form(body):
        """解析 application/x-www-form-urlencoded"""
        params = {}
        for pair in body.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                # URL decode (简单处理)
                v = v.replace("+", " ").replace("%21", "!").replace("%40", "@")
                params[k] = v
        return params

    @staticmethod
    def _config_page():
        return """<!DOCTYPE html>
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
</body></html>"""
