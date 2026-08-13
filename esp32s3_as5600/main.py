# AS5600 磁编码器角度采集 + UDP 广播上传
# 支持: WiFi 非阻塞重连 + Web 配网 + 多圈连续角度 (unwrap) + 断电恢复 + 看门狗
# 支持: 光电零位传感器 (NPN 常开) 检测挡片, 触发时上报零位事件给后端
import usocket as socket
import time
from machine import Pin, SoftI2C, WDT
from wifi_manager import WiFiManager

# ========== 配置 ==========
UDP_PORT  = 8091       # UDP 广播端口 (后端监听此端口)
SEND_MS   = 10         # 发送间隔(ms): 100Hz, 反馈延迟降至 ~10ms
STATE_FILE = "as5600_state.txt"

# AS5600 接线: SCL -> GPIO8, SDA -> GPIO9
AS5600_ADDR = 0x36
REG_ANGLE   = 0x0C  # 0x0C/0x0D RAW ANGLE (不滤波, 高速旋转不卡死)
REG_STATUS  = 0x0B  # 状态寄存器: bit5=MD(检测到磁铁) bit4=ML(过弱) bit3=MH(过强)

# 光电零位传感器 (NPN 常开): 输出接 GPIO11, 传感器按型号接 5V/24V 与 GND
# 常开模式下平时(无物体)输出高阻 -> 上拉读 1, 挡片进入窗口 -> 拉低读 0
# 注意: 常开线平时浮空, 电机 EMI 易在线上产生毛刺 -> 需防抖 + 主循环确认引脚电平
ZERO_PIN = 11
ZERO_DEBOUNCE_MS = 30   # 光电触发防抖窗口 (过滤 EMI 毛刺/重复触发)

# ========== 全局状态 ==========
_last_angle = None
_total_abs = None  # None=尚未初始化(首次启动无状态文件); 数值=累积角度
_sock = None

# 光电零位中断标志 (中断回调置位, 主循环消费; 中断内禁止 I2C/网络操作)
_zero_flag = 0        # 有未处理的下降沿触发则 >0
_zero_last = 0        # 最近一次触发时的机器 tick 时间戳
_zero_pin = None      # 中断绑定后保存引用


def _zero_irq(pin):
    """光电下降沿中断: 只置标志+记时间, 主循环再读 raw 发送 (避免中断内 I2C/网络)
    常开线平时浮空(上拉读1), 挡片进入拉低(0) -> IRQ_FALLING 捕获。
    电机 EMI 会在浮空线上产生毛刺 -> 30ms 防抖, 过滤噪声/同一次触发的重复边沿。
    """
    global _zero_flag, _zero_last
    now = time.ticks_ms()
    if time.ticks_diff(now, _zero_last) < ZERO_DEBOUNCE_MS:
        return  # 防抖: 30ms 内重复触发视为噪声/同一次触发
    _zero_flag += 1
    _zero_last = now


def load_state():
    """恢复累积角度。关键: 只恢复 _total_abs, 不恢复旧 _last_angle。
    若恢复旧 _last_angle, 重启后 unwrap 首次 delta 会被限制在 ±180 内,
    导致 _total_abs 被拉回单圈角度, 累积永远失效。
    启动时 _last_angle=None, 首次 unwrap 直接用当前角度起步, 累积跨重启保持。
    """
    global _last_angle, _total_abs
    try:
        with open(STATE_FILE, "r") as f:
            vals = f.read().strip().split(",")
            _total_abs = float(vals[0])
        print("[state] restored abs:", _total_abs)
    except Exception:
        _total_abs = None  # 无有效状态 -> 首次 unwrap 从当前角度起步
    _last_angle = None  # 强制从当前角度重新起步


def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            f.write("%.2f" % _total_abs)
    except Exception:
        pass


INVERT_DIRECTION = False  # 云台正转时 AS5600 raw 增大(跨0线), 无需反转, angle 直接累积


def unwrap(angle):
    global _last_angle, _total_abs
    if _last_angle is None:
        # 首次调用: 有恢复的累积值则保留; 无状态文件则从当前单圈角度起步
        if _total_abs is None:
            _total_abs = angle
        _last_angle = angle
        return _total_abs
    delta = angle - _last_angle
    if delta > 180.0:
        delta -= 360.0
    elif delta < -180.0:
        delta += 360.0
    if INVERT_DIRECTION:
        _total_abs -= delta
    else:
        _total_abs += delta
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


def read_status(i2c):
    """读取 AS5600 状态寄存器, 诊断磁场是否正常"""
    try:
        i2c.writeto(AS5600_ADDR, bytes([REG_STATUS]))
        s = i2c.readfrom(AS5600_ADDR, 1)[0]
        return s
    except Exception:
        return -1


def init_udp():
    global _sock
    _sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    _sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)


def main():
    # 光电零位输入 (NPN 常开 + 上拉): 平时 1, 挡片进入 0
    # 用硬件下降沿中断捕获, 避免高速旋转时轮询漏检第二次触发
    global _zero_pin, _zero_flag
    _zero_pin = Pin(ZERO_PIN, Pin.IN, Pin.PULL_UP)
    _zero_flag = 0
    _zero_pin.irq(trigger=Pin.IRQ_FALLING, handler=_zero_irq)

    # I2C
    i2c = SoftI2C(sda=Pin(9), scl=Pin(8), freq=400000)
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
    last_status = time.ticks_ms()
    _prev_raw = None
    _stuck_cnt = 0

    while True:
        wdt.feed()  # 喂狗

        # 光电零位: 消费中断标志 (下降沿已由 _zero_irq 捕获)
        if _zero_flag:
            _zero_flag = 0
            # 常开线噪声过滤: 确认引脚此刻确实为低 (挡片真实进入窗口)。
            # 噪声毛刺极短, 主循环(10ms)再读时引脚已回高 -> 忽略, 不误报零位。
            if _zero_pin.value() != 0:
                continue
            angle, raw = read_angle(i2c)
            if raw is not None and wifi.is_connected():
                abs_angle = unwrap(angle) if angle is not None else 0.0
                payload = '{"event":"zero","raw":%d,"angle":%.2f,"tick":%d}' % (
                    raw, abs_angle, time.ticks_ms())
                try:
                    _sock.sendto(payload.encode(), ('255.255.255.255', UDP_PORT))
                    print("[zero] 光电零位触发 raw:", raw, "abs:", round(abs_angle, 1))
                except Exception as e:
                    print("[udp] zero send error:", e)

        # 非阻塞 WiFi 重连 (断线时按 5/10/30s 退避重试)
        wifi.reconnect_tick()

        now = time.ticks_ms()

        # 周期发送角度数据
        if time.ticks_diff(now, last_send) >= SEND_MS:
            last_send = now
            angle, raw = read_angle(i2c)
            if angle is not None:
                # 卡死检测: raw 连续 50 次(约0.5s)不变则打印警告
                if _prev_raw is not None and raw == _prev_raw:
                    _stuck_cnt += 1
                    if _stuck_cnt == 50:
                        st = read_status(i2c)
                        print("[warn] raw stuck at", raw, "status:", hex(st) if st >= 0 else "N/A")
                else:
                    _stuck_cnt = 0
                _prev_raw = raw
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

        # 周期读取 STATUS 寄存器 (每 3s 打印一次磁场状态)
        if time.ticks_diff(now, last_status) >= 3000:
            last_status = now
            st = read_status(i2c)
            if st >= 0:
                md = (st >> 5) & 1  # 磁铁检测
                ml = (st >> 4) & 1  # 过弱
                mh = (st >> 3) & 1  # 过强
                # 降低告警阈值: 仅检测不到磁铁(MD=0)或磁场过强(MH=1)才告警
                # 忽略"过弱"(ML=1): 磁场偏弱但功能正常, 避免频繁刷屏
                if not md or mh:
                    print("[as5600] 磁场异常! status=%02x MD=%d ML=%d MH=%d" % (st, md, ml, mh))

        # 周期保存圈数
        if time.ticks_diff(now, last_save) >= 5000:
            last_save = now
            save_state()

        time.sleep_ms(2)  # 2ms 空转, 由 SEND_MS 判断主导 100Hz 发送周期


main()
