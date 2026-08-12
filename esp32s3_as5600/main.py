# AS5600 磁编码器角度采集 + UDP 广播上传
# 支持: WiFi 非阻塞重连 + Web 配网 + 多圈连续角度 (unwrap) + 断电恢复 + 看门狗
import usocket as socket
import time
from machine import Pin, SoftI2C, WDT
from wifi_manager import WiFiManager

# ========== 配置 ==========
UDP_PORT  = 8091       # UDP 广播端口 (后端监听此端口)
SEND_MS   = 20         # 发送间隔(ms): 50Hz, 降低反馈延迟 (原 100ms/10Hz)
STATE_FILE = "as5600_state.txt"

# AS5600 接线: SCL -> GPIO11, SDA -> GPIO12
AS5600_ADDR = 0x36
REG_ANGLE   = 0x0E  # 0x0E/0x0F 滤波后角度

# ========== 全局状态 ==========
_last_angle = None
_total_abs  = 0.0
_sock = None


def load_state():
    global _last_angle, _total_abs
    try:
        with open(STATE_FILE, "r") as f:
            vals = f.read().strip().split(",")
            _total_abs = float(vals[0])
            _last_angle = float(vals[1])
        print("[state] restored abs:", _total_abs, "last:", _last_angle)
    except Exception:
        _last_angle = None
        _total_abs = 0.0


def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            f.write("%.2f,%.2f" % (_total_abs, _last_angle if _last_angle is not None else 0.0))
    except Exception:
        pass


def unwrap(angle):
    global _last_angle, _total_abs
    if _last_angle is not None:
        delta = angle - _last_angle
        if delta > 180.0:
            delta -= 360.0
        elif delta < -180.0:
            delta += 360.0
        _total_abs += delta
    else:
        _total_abs = angle
    _last_angle = angle
    return _total_abs


def read_angle(i2c):
    try:
        i2c.writeto(AS5600_ADDR, bytes([REG_ANGLE]))
        data = i2c.readfrom(AS5600_ADDR, 2)
        raw = (data[0] << 8) | data[1]
        angle = (raw * 360.0) / 4096.0
        return angle, raw
    except Exception as e:
        print("[as5600] read error:", e)
        return None, None


def init_udp():
    global _sock
    _sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    _sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)


def main():
    # I2C
    i2c = SoftI2C(sda=Pin(12), scl=Pin(11), freq=400000)
    devices = i2c.scan()
    print("[i2c] devices:", [hex(d) for d in devices])
    if AS5600_ADDR not in devices:
        print("[i2c] WARNING: AS5600 not found!")

    load_state()

    # WiFi: 有配置则连接, 无配置或连接失败则进入 AP 配网
    wifi = WiFiManager()
    cfg = wifi.load_config()
    if cfg:
        wifi.connect(timeout_s=15)
    if not wifi.is_connected():
        print("[wifi] no config or connect failed, starting AP config...")
        wifi.start_ap_config()  # 阻塞直到配网完成(保存后自动重启)

    # UDP socket
    init_udp()

    # 看门狗: 10 秒无喂狗则硬复位 (防止 I2C/UDP 死锁)
    wdt = WDT(timeout=10000)
    print("[main] started, IP:", wifi.ip(), "UDP port:", UDP_PORT)

    last_send = time.ticks_ms()
    last_save = time.ticks_ms()

    while True:
        wdt.feed()  # 喂狗

        # 非阻塞 WiFi 重连 (断线时按 5/10/30s 退避重试)
        wifi.reconnect_tick()

        now = time.ticks_ms()

        # 周期发送角度数据
        if time.ticks_diff(now, last_send) >= SEND_MS:
            last_send = now
            angle, raw = read_angle(i2c)
            if angle is not None:
                abs_angle = unwrap(angle)
                if wifi.is_connected():
                    payload = '{"angle":%.2f,"raw":%d,"tick":%d}' % (abs_angle, raw, time.ticks_ms())
                    try:
                        _sock.sendto(payload.encode(), ('255.255.255.255', UDP_PORT))
                    except Exception as e:
                        print("[udp] send error:", e)
                        # 重建 socket
                        init_udp()
                else:
                    print("[wifi] offline, angle:", round(abs_angle, 1))

        # 周期保存圈数
        if time.ticks_diff(now, last_save) >= 5000:
            last_save = now
            save_state()

        time.sleep_ms(10)


main()
