"""
雅安 YD3040 云台 (Pelco-D) 网页控制后端
特性:
- 电机恒速 7.5°/s, 无角度回传, 无预置位, 无绝对定位
- 水平范围 0~355°, 垂直范围 0~90°
- 通过航位推算(dead reckoning)估算模糊位置
"""
from __future__ import annotations

import json
import os
import threading
import time

import serial
from flask import Flask, jsonify, request, send_from_directory, session
from waitress import serve

from pelco import PelcoD
import satellite
import auth

# ---------- 配置 ----------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

SERIAL_PORT = os.getenv("PTZ_SERIAL_PORT", "/dev/ttyUSB0")
BAUD_RATE = int(os.getenv("PTZ_BAUD", "9600"))
PTZ_ADDRESS = int(os.getenv("PTZ_ADDRESS", "1"))

# 默认云台参数 (可在设置中自定义并持久化)
DEFAULT_CONFIG = {
    "pan_speed_dps": 7.45,      # 水平有效速度 °/s (完整启停平均实测: 85°/11.41s)
    "tilt_speed_dps": 5.1799,   # 俯仰速度 °/s (实测 90°/17.375s, 抬头/低头平均)
    "tilt_up_speed_dps": 5.1799,    # 俯仰抬头速度 °/s (克服重力较慢, 需用标定分开测量)
    "tilt_down_speed_dps": 5.1799,  # 俯仰低头速度 °/s (重力加速较快, 需用标定分开测量)
    "accel_time": 1.27,         # 电机加速时间(秒): 保留字段, 线性模型下不使用
    "pan_min": 0.0,             # 水平最小角度 (已移除物理限位, 0°~360° 连续旋转)
    "pan_max": 360.0,           # 水平最大角度
    "tilt_min": 0.0,            # 俯仰最小角度
    "tilt_max": 90.0,           # 俯仰最大角度
    "reset_tilt_s": 15.0,       # 复位俯仰移动时长(秒)
}

_config_lock = threading.Lock()


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                import json
                saved = json.load(f)
                cfg.update({k: v for k, v in saved.items() if k in DEFAULT_CONFIG})
    except Exception:  # noqa: BLE001
        pass
    return cfg


def save_config(cfg: dict):
    try:
        import json
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


config = load_config()


def get_cfg(key: str):
    with _config_lock:
        return config.get(key, DEFAULT_CONFIG[key])


def update_config(data: dict) -> dict:
    """更新配置并持久化, 返回更新后的配置"""
    global config
    with _config_lock:
        for k in DEFAULT_CONFIG:
            if k in data and data[k] is not None:
                if isinstance(DEFAULT_CONFIG[k], bool):
                    config[k] = bool(data[k])
                else:
                    try:
                        config[k] = float(data[k])
                    except (ValueError, TypeError):
                        pass
        save_config(config)
        return dict(config)


app = Flask(__name__, static_folder=None)
# 会话签名密钥: 必须通过环境变量 PTZ_SECRET_KEY 提供, 无默认值 (安全)
app.secret_key = os.environ["PTZ_SECRET_KEY"]

# 不需要登录的 API (设备回传与登录本身)
def _is_public(path: str) -> bool:
    return (path.startswith("/api/auth/")
            or path == "/api/encoder"
            or path.startswith("/api/encoder/"))


@app.before_request
def _auth_check():
    """除公开接口外, 其余 /api/* 均需登录"""
    if request.path.startswith("/api/") and not _is_public(request.path):
        if not session.get("user"):
            return jsonify({"ok": False, "error": "未登录"}), 401
    return None


# ---------- 用户认证 ----------
# 会话 cookie 持久化: 30 天内刷新/重启浏览器均保持登录
app.config["PERMANENT_SESSION_LIFETIME"] = 30 * 24 * 3600
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    if not username or not password:
        return err("请输入用户名和密码")
    user = auth.authenticate(username, password)
    if user is None:
        return err("用户名或密码错误", 401)
    session.permanent = True
    session["user"] = user
    return ok({"user": user})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return ok({"logged_out": True})


@app.route("/api/auth/me")
def auth_me():
    user = session.get("user")
    if user is None:
        return err("未登录", 401)
    return ok({"user": user})
pelco = PelcoD(address=PTZ_ADDRESS)

# ---------- 串口管理 ----------
_serial_lock = threading.Lock()
_serial: serial.Serial | None = None

# 串口监控日志 (环形缓冲)
SERIAL_LOG_MAX = 200
_serial_log = []
_serial_log_lock = threading.Lock()

# 全局暂停: 暂停时所有控制命令不发送
_paused = False
_pause_lock = threading.Lock()

# 复位状态: 复位进行中
_resetting = False
_reset_lock = threading.Lock()


def is_resetting() -> bool:
    with _reset_lock:
        return _resetting


def set_resetting(p: bool):
    global _resetting
    with _reset_lock:
        _resetting = p


def is_paused() -> bool:
    with _pause_lock:
        return _paused


def set_paused(p: bool):
    global _paused
    with _pause_lock:
        _paused = p


def _log_serial(direction: str, data: bytes):
    """记录串口收发数据"""
    with _serial_log_lock:
        _serial_log.append({
            "time": time.strftime("%H:%M:%S"),
            "dir": direction,
            "hex": data.hex(" ").upper(),
        })
        if len(_serial_log) > SERIAL_LOG_MAX:
            del _serial_log[:len(_serial_log) - SERIAL_LOG_MAX]


def _scan_ports() -> list:
    """扫描可用串口设备, 配置端口优先 (USB 设备枚举编号不稳定, 自动兜底)"""
    import glob
    ports = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    if SERIAL_PORT in ports:
        ports.remove(SERIAL_PORT)
        ports.insert(0, SERIAL_PORT)
    return ports


def _try_open(port: str) -> serial.Serial | None:
    try:
        return serial.Serial(
            port=port,
            baudrate=BAUD_RATE,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.2,
        )
    except Exception:  # noqa: BLE001
        return None


def _close_serial():
    global _serial
    with _serial_lock:
        if _serial is not None:
            try:
                _serial.close()
            except Exception:  # noqa: BLE001
                pass
            _serial = None


def get_serial() -> serial.Serial:
    global _serial
    with _serial_lock:
        if _serial is not None and _serial.is_open:
            return _serial
        if _serial is not None:
            try:
                _serial.close()
            except Exception:  # noqa: BLE001
                pass
            _serial = None
        for port in _scan_ports():
            ser = _try_open(port)
            if ser is not None:
                _serial = ser
                return _serial
        raise RuntimeError("无法打开串口设备 (请检查 USB 转 485 连接)")


def send(cmd: bytes) -> None:
    """发送一帧指令到串口 (暂停时不发送; 写入失败自动重连一次)"""
    if is_paused():
        return
    for attempt in (0, 1):
        try:
            ser = get_serial()
            with _serial_lock:
                ser.write(cmd)
                ser.flush()
            _log_serial("TX", cmd)
            return
        except Exception as e:  # noqa: BLE001
            _close_serial()  # 僵尸句柄, 强制重连
            if attempt == 1:
                raise RuntimeError(f"串口发送失败: {e}") from e
            time.sleep(0.3)


def _serial_reader():
    """后台线程: 读取串口回传数据并记录"""
    while True:
        try:
            ser = get_serial()
            data = ser.read(64)
            if data:
                _log_serial("RX", data)
        except Exception:  # noqa: BLE001
            time.sleep(1)


threading.Thread(target=_serial_reader, daemon=True).start()


# ---------- 模糊位置(航位推算) ----------
class PositionTracker:
    """基于运动时长的位置估算"""

    def __init__(self):
        self.lock = threading.Lock()
        self.pan = get_cfg("pan_max")  # 初始位置 360° (即 0°)
        self.tilt = 0.0
        self._moving = None  # (direction, start_time)
        # 运动基准: 一次运动中估算量 = 基准 + 速度*时间, 外部(AS5600)只更新 pan 基准, 不影响 tilt 计时
        self._pan_base = self.pan
        self._tilt_base = self.tilt
        # 防缠绕: 从零点开始的水平累计转动量 (右转为正, 左转为负)
        self.pan_accum = 0.0

    def _clamp(self):
        # 水平 0~360° 连续旋转 (360° 与 0° 同位置, 取模归一); 俯仰受物理限位
        self.pan = self.pan % 360.0
        self.tilt = max(get_cfg("tilt_min"), min(get_cfg("tilt_max"), self.tilt))

    @staticmethod
    def _angle_for_time(elapsed: float, direction: str = "") -> tuple[float, float]:
        """纯线性模型: 转过角度 = 速度 * 时间 (与驱动逻辑一致)
        俯仰按方向选用抬头/低头速度 (重力影响, 两向速度不同)
        """
        if elapsed <= 0:
            return 0.0, 0.0
        pan_speed = get_cfg("pan_speed_dps")
        if "up" in direction:
            tilt_speed = get_cfg("tilt_up_speed_dps")
        elif "down" in direction:
            tilt_speed = get_cfg("tilt_down_speed_dps")
        else:
            tilt_speed = get_cfg("tilt_speed_dps")
        return pan_speed * elapsed, tilt_speed * elapsed

    def _finalize(self):
        """结算当前运动, 更新位置"""
        if not self._moving:
            return
        direction, start = self._moving
        elapsed = time.time() - start
        pan_delta, tilt_delta = self._angle_for_time(elapsed, direction)
        # 方向: 上=增大tilt(抬头), 下=减小tilt(低头), 左=减小pan, 右=增大pan
        pan, tilt = self._pan_base, self._tilt_base
        if "up" in direction:
            tilt += tilt_delta
        if "down" in direction:
            tilt -= tilt_delta
        if "left" in direction:
            pan -= pan_delta
        if "right" in direction:
            pan += pan_delta
        self.pan, self.tilt = pan, tilt
        self._clamp()
        self._moving = None

    def start_move(self, direction: str):
        with self.lock:
            self._finalize()
            self._pan_base, self._tilt_base = self.pan, self.tilt
            self._moving = (direction, time.time())

    def stop(self):
        with self.lock:
            self._finalize()

    def _estimate(self):
        """估算当前位置(含进行中的运动), 不结算"""
        pan, tilt = self.pan, self.tilt
        if self._moving:
            direction, start = self._moving
            elapsed = time.time() - start
            pan_delta, tilt_delta = self._angle_for_time(elapsed, direction)
            pan, tilt = self._pan_base, self._tilt_base
            if "up" in direction:
                tilt += tilt_delta
            if "down" in direction:
                tilt -= tilt_delta
            if "left" in direction:
                pan -= pan_delta
            if "right" in direction:
                pan += pan_delta
        # 水平取模归一 0~360; 俯仰受物理限位
        pan = pan % 360.0
        tilt = max(get_cfg("tilt_min"), min(get_cfg("tilt_max"), tilt))
        return pan, tilt

    def at_limit(self, direction: str) -> bool:
        """判断某方向是否已达限位 (不结算运动)
        水平: 0°~360° 连续旋转, 360° 与 0° 同位置 (光电零位), 不设左右限位,
              可顺时针/逆时针无限旋转 (仅俯仰有物理限位)。
        """
        with self.lock:
            pan, tilt = self._estimate()
            eps = 0.01
            if "up" in direction and tilt >= get_cfg("tilt_max") - eps:
                return True
            if "down" in direction and tilt <= get_cfg("tilt_min") + eps:
                return True
            return False

    def get(self) -> dict:
        with self.lock:
            pan, tilt = self._estimate()
            return {"pan": round(pan, 1), "tilt": round(tilt, 1),
                    "moving": self._moving is not None,
                    "pan_accum": round(self.pan_accum, 1)}

    def reset_to_limit(self, pan_s: float | None = None, tilt_s: float | None = None):
        """复位: 同时移动到 (pan_max, 0°) 初始限位, 两轴各自时长独立"""
        with self.lock:
            self._finalize()
            cur_pan, cur_tilt = self.pan, self.tilt
        if pan_s is None or tilt_s is None:
            if get_cfg("auto_reset"):
                # 按当前位置与目标限位、速度自动计算时长, 加 5s 冗余
                pan_s = max(0.1, (get_cfg("pan_max") - cur_pan) / get_cfg("pan_speed_dps")) + 5
                tilt_s = max(0.1, (cur_tilt - get_cfg("tilt_min")) / get_cfg("tilt_speed_dps")) + 5
            else:
                pan_s = get_cfg("reset_pan_s")
                tilt_s = get_cfg("reset_tilt_s")
        start = time.time()
        pan_done = False
        tilt_done = False
        while not (pan_done and tilt_done):
            elapsed = time.time() - start
            if not pan_done and elapsed >= pan_s:
                pan_done = True
            if not tilt_done and elapsed >= tilt_s:
                tilt_done = True
            if pan_done and tilt_done:
                break
            # 右=增大pan(到pan_max), 上=减小tilt(到0°)
            try:
                if pan_done:
                    send(pelco.up())          # 仅俯仰
                elif tilt_done:
                    send(pelco.right())       # 仅水平
                else:
                    send(pelco.up_right())    # 同时
            except Exception:  # noqa: BLE001
                break
            time.sleep(0.1)
        try:
            send(pelco.stop())
        except Exception:  # noqa: BLE001
            pass
        with self.lock:
            self.pan = get_cfg("pan_max")
            self.tilt = 0.0


tracker = PositionTracker()


# ---------- 持续转动管理 ----------
# 部分云台需持续发送指令才能保持转动, 按住按钮时持续发送, 松开时停止
_HOLD_INTERVAL = 0.1  # 发送间隔(秒)
_hold_lock = threading.Lock()
_hold_stop = threading.Event()
_hold_thread: threading.Thread | None = None

_MOVE_CMDS = {
    # 注意: 该云台接线反相, 标准 UP 帧物理表现为低头, 故 up(抬头) 需发 DOWN 帧
    "up": pelco.down,
    "down": pelco.up,
    "left": pelco.left,
    "right": pelco.right,
    "upleft": pelco.down_left,
    "upright": pelco.down_right,
    "downleft": pelco.up_left,
    "downright": pelco.up_right,
}

# ---------- 卫星跟踪 ----------
_last_sat_dir = None


def _sat_send_dir(direction: str):
    """卫星跟踪: 按方向发送指令并更新模糊位置"""
    global _last_sat_dir
    if direction != _last_sat_dir:
        if _last_sat_dir is not None:
            tracker.stop()
        if direction != "stop":
            tracker.start_move(direction)
        _last_sat_dir = direction
    try:
        if direction == "stop":
            send(pelco.stop())
        else:
            send(_MOVE_CMDS[direction]())
    except Exception:  # noqa: BLE001
        pass


def _sat_get_position():
    with tracker.lock:
        # 水平: AS5600 实时反馈优先 (encoder_post 已实时写入 tracker.pan), 反馈失效时回退航位推算
        enc_time = _encoder_state.get("time", 0.0)
        if time.time() - enc_time < 2.0:
            pan = tracker.pan
        else:
            pan = tracker._estimate()[0]
        tilt = tracker._estimate()[1]
        return pan, tilt


def _sat_set_position(pan: float, tilt: float):
    with tracker.lock:
        tracker._finalize()
        tracker.pan = pan
        tracker.tilt = tilt
        tracker._pan_base, tracker._tilt_base = pan, tilt


def _pan_delta(az: float, pan: float) -> float:
    """方位角差: 取最短路径 (云台 360° 连续旋转, 无限位)"""
    return (az - pan + 180) % 360 - 180


# 复位找零参数: 低速 0x08(~1.9°/s)过冲小, 一圈约 189s; 超时兜底防光电失效无限转
FIND_ZERO_SPEED = 0x08
FIND_ZERO_TIMEOUT = 240.0

# 超限回转参数: 堵转检测 (驱动一段时间回传角度无变化 => 线缆缠死/堵转)
RECOVER_SPEED = 0x10          # 回转中速 (物理 ~3.8°/s)
RECOVER_PROBE_S = 0.6         # 方向试探驱动时长 (秒)
RECOVER_STALL_S = 2.5         # 连续驱动多少秒回传角无实质变化判定堵转
RECOVER_STALL_TOL = 2.0       # 物理角: 该时长内角度变化小于此值视为堵转
RECOVER_DEADLINE_S = 240.0    # 回转总超时 (物理 ±540°@3.8°/s 约 284s, 取此值兜底)
RECOVER_TARGET_TOL = 10.0     # 回到 0° 多远算成功


def _get_enc_offset():
    """读取回传角度相对物理 0°(光电零位) 的偏移。
    返回 ESP32 域偏移 = angle - zero_angle (未除 4, 与回传角度同量纲),
    未标定或无数据返回 None。用最原始的 ESP32 累积角判断, 不依赖 cont_angle 换算。
    """
    with _encoder_lock:
        angle = _encoder_state.get("angle")
    zero = _encoder_cal.get("zero_angle")
    if angle is None or zero is None:
        return None
    return float(angle) - zero


def _probe_recover_direction():
    """反馈试探回转方向: 向 left 驱动 RECOVER_PROBE_S, 比较偏移是否向 0 收敛。
    该云台 left/right 实际转向可能与标准帧相反, 不硬编码方向, 以回传角度为准。
    返回: 'left'/'right'/'stop' (无有效反馈时 stop, 不做试探)
    """
    off0 = _get_enc_offset()
    if off0 is None:
        print("[reset/probe] 无初始偏移, 放弃试探", flush=True)
        return "stop"
    try:
        send(_MOVE_CMDS["left"](RECOVER_SPEED))
        time.sleep(RECOVER_PROBE_S)
        _stop_send()
    except Exception as e:  # noqa: BLE001
        _stop_send()
        print(f"[reset/probe] 串口发送失败: {e}", flush=True)
        return "stop"
    off1 = _get_enc_offset()
    if off1 is None:
        print("[reset/probe] 试探后无偏移数据", flush=True)
        return "stop"
    print(f"[reset/probe] off0={off0:.0f} off1={off1:.0f} dOff={off1-off0:.0f}°ESP", flush=True)
    if abs(off1) < abs(off0):
        return "left"          # left 使偏移收敛
    return "right"             # left 使偏移发散 -> 用 right


def _recover_from_tangle():
    """复位前处理超限: 基于回传角度 angle 与标定零位 zero_angle 的偏移
    (ESP32 域 = angle - zero_angle) 判断是否越出防缠绕范围。
    物理范围 [-540, 540] 对应 ESP32 域 [-2160, 2160] (×4)。
    越界则试探出收敛方向后自动往 0° 回转; 用回传角度闭环判定堵转(回不去)立即停止。
    返回: (ok, detail)
      ok=True    已回到 0° 附近, 可继续找零
      ok=False   堵转/超时回不去, 已停止并保持原位置
    """
    off = _get_enc_offset()
    if off is None:
        return True, "无编码器数据或未标定, 按范围内处理"
    # ESP32 域范围: TANGLE_MIN/MAX 已是 ESP32 域 (物理 ±540° × 4)
    if TANGLE_MIN <= off <= TANGLE_MAX:
        return True, f"在范围内 (偏移 {off:.0f}°ESP)"
    # 试探收敛方向 (不假设硬件方向)
    direction = _probe_recover_direction()
    if direction == "stop":
        return False, "无编码器反馈, 无法判断回转方向"
    print(f"[reset] 检测到超限 偏移={off:.0f}°ESP, 试探方向={direction}", flush=True)
    start = time.time()
    # 堵转检测状态 (用回传角度偏移, ESP32 域)
    stall_last_t = time.time()
    stall_last_off = _get_enc_offset()
    # 回转闭环: 持续朝收敛方向转, 偏移回到目标容差内停止
    # 目标容差换算: 物理 ±10° → ESP32 ±40°
    target_tol = RECOVER_TARGET_TOL * ENCODER_GEAR
    stall_tol = RECOVER_STALL_TOL * ENCODER_GEAR
    while time.time() - start < RECOVER_DEADLINE_S:
        cur = _get_enc_offset()
        if cur is not None and abs(cur) <= target_tol:
            print(f"[reset] 已回到 0° 附近 (偏移={cur:.0f}°ESP)", flush=True)
            _stop_send()
            return True, "已回到 0°"
        # 堵转检测: 驱动期间偏移长时间不收敛 => 回不去
        now = time.time()
        if cur is not None and abs(cur - stall_last_off) < stall_tol \
                and now - stall_last_t >= RECOVER_STALL_S:
            print(f"[reset] 堵转检测: 电机在转但偏移不变 ({cur:.0f}°ESP), 判定回不去", flush=True)
            _stop_send()
            return False, "线缆缠死/堵转, 无法回转"
        if cur is not None and abs(cur - stall_last_off) >= stall_tol:
            stall_last_t = now
            stall_last_off = cur
        try:
            send(_MOVE_CMDS[direction](RECOVER_SPEED))
        except Exception:  # noqa: BLE001
            _stop_send()
            return False, "串口发送失败"
        time.sleep(0.05)
    _stop_send()
    print(f"[reset] 回转超时, 仍未回到 0° (偏移={_get_enc_offset()}°ESP)", flush=True)
    return False, "回转超时, 无法回到 0°"


def _stop_send():
    """发送停止帧 (复位/回转用)"""
    try:
        send(pelco.stop())
    except Exception:  # noqa: BLE001
        pass


def _reset_loop(tilt_s: float):
    """复位: 水平找光电 0° + 俯仰同时归零 (斜向同时运动)。
    水平低速(0x08)找光电精确停 0°, 俯仰全速(0x20)按时长归零, 两轴同时驱动。
    水平以光电触发为锚点精确停 0°, 俯仰按时长归零。
    """
    global _zero_evt_angle
    _zero_evt.clear()
    reset_start_t = time.time()   # 复位开始时间, 用于忽视复位前的旧光电事件
    zero = _encoder_cal.get("zero_angle")
    if zero is None:
        try: send(pelco.stop())
        except Exception: pass
        print("[reset] 未标定 zero_angle, 无法复位", flush=True)
        return
    off = _get_enc_offset()
    if off is None:
        try: send(pelco.stop())
        except Exception: pass
        print("[reset] 无编码器数据, 无法复位", flush=True)
        return

    # 已在光电窗口: 当前角度接近标定基准 zero_angle 才免回转。
    # 注意: 必须用标定基准 zero_angle 判断, 不能用 _zero_last_angle (最近一次触发),
    # 否则手动转圈经过光电后停在别处也会被误判为"已在窗口", 导致电机不动。
    with _encoder_lock:
        cur_angle = _encoder_state.get("angle")
    already_at_zero = (zero is not None
            and cur_angle is not None
            and abs(cur_angle - zero) < 10.0)

    zero_angle_found = None   # 光电触发时的 ESP32 累积角度
    horizontal_done = already_at_zero
    direction = None

    if already_at_zero:
        zero_angle_found = zero
        print(f"[reset] 已在标定 0° 附近 (angle={cur_angle:.1f}, zero={zero:.1f}), 免回转", flush=True)
    else:
        # 参照 zero_angle 试探回转方向
        direction = _probe_recover_direction()
        if direction == "stop":
            try: send(pelco.stop())
            except Exception: pass
            print("[reset] 无编码器反馈, 无法判断回转方向", flush=True)
            return
        # 关键: 清掉试探期间可能产生的光电事件, 避免主循环捡到旧事件误停
        _zero_evt.clear()
        _zero_evt_angle = None
        print(f"[reset] 水平回转方向={direction} 偏移={off:.0f}°ESP 目标=光电 0°", flush=True)

    # ===== 合并复位: 水平找光电 + 俯仰同时归零 (斜向) =====
    # 水平低速(0x08)找光电精确停; 俯仰全速(0x20)按时长归零
    tilt_done = (tilt_s <= 0)
    tilt_deadline = time.time() + tilt_s if tilt_s > 0 else None
    find_deadline = time.time() + FIND_ZERO_TIMEOUT
    _loop_cnt = 0

    while time.time() < find_deadline:
        # 光电触发 -> 水平完成 (只接受复位开始后的新事件, 忽视旧事件)
        if _zero_evt.is_set():
            if _zero_evt_t >= reset_start_t:
                zero_angle_found = _zero_evt_angle
                _zero_evt.clear()
                horizontal_done = True
                print(f"[reset] 水平已到光电 0° angle={zero_angle_found:.2f}", flush=True)
            else:
                # 旧事件 (复位前残留/试探期间延迟到达): 清掉继续转
                _zero_evt.clear()
                print(f"[reset] 忽视旧光电事件 angle={_zero_evt_angle} (t={_zero_evt_t-reset_start_t:.1f}s)", flush=True)

        # 俯仰时长到 -> 俯仰完成
        if not tilt_done and tilt_deadline is not None and time.time() >= tilt_deadline:
            tilt_done = True
            print(f"[reset] 俯仰时长到 ({tilt_s:.1f}s)", flush=True)

        # 两轴都完成 -> 停止
        if horizontal_done and tilt_done:
            break

        # 发送指令: 斜向(水平低速+俯仰全速) / 仅水平 / 仅俯仰
        if not horizontal_done and not tilt_done:
            if direction == "left":
                cmd = pelco.up_left(FIND_ZERO_SPEED, 0x20)
            else:
                cmd = pelco.up_right(FIND_ZERO_SPEED, 0x20)
        elif not horizontal_done:
            cmd = _MOVE_CMDS[direction](FIND_ZERO_SPEED)
        else:
            cmd = pelco.up()   # 俯仰归零 (标准 UP 帧 = 低头, 与 _finalize 一致)
        _loop_cnt += 1
        if _loop_cnt % 20 == 1:  # 每 ~2s 打印一次角度
            _cur_off = _get_enc_offset()
            print(f"[reset] 驱动中 dir={direction} off={_cur_off:.0f}°ESP "
                  f"t={time.time()-find_deadline+FIND_ZERO_TIMEOUT:.1f}s", flush=True)
        try:
            send(cmd)
        except Exception:  # noqa: BLE001
            break
        time.sleep(0.1)
    try: send(pelco.stop())
    except Exception: pass

    if zero_angle_found is not None:
        print(f"[reset] 水平复位完成, 光电触发 angle={zero_angle_found:.2f} (zero_angle 保持不变)", flush=True)
    else:
        print("[reset] 水平复位超时未触发光电, 停转 (请检查光电传感器)", flush=True)

    # 重置跟踪器位置
    with tracker.lock:
        tracker.pan = 0.0
        tracker.tilt = 0.0
        tracker._pan_base, tracker._tilt_base = 0.0, 0.0
        tracker.pan_accum = 0.0
    with _encoder_lock:
        cont = _encoder_state.get("cont_angle")
    print(f"[reset] 复位完成 cont={cont if cont is None else round(cont, 1)}°", flush=True)


sat_tracker = satellite.SatelliteTracker(_sat_send_dir, _sat_get_position, _sat_set_position)
sat_tracker.pan_min = get_cfg("pan_min")
sat_tracker.pan_max = get_cfg("pan_max")
sat_tracker.tilt_min = get_cfg("tilt_min")
sat_tracker.tilt_max = get_cfg("tilt_max")
sat_tracker.pan_speed = get_cfg("pan_speed_dps")
sat_tracker.tilt_speed = get_cfg("tilt_speed_dps")
sat_tracker.tilt_up_speed = get_cfg("tilt_up_speed_dps")
sat_tracker.tilt_down_speed = get_cfg("tilt_down_speed_dps")
sat_tracker.accel_time = get_cfg("accel_time")


def _hold_loop(direction: str, check_limit: bool = True):
    """持续发送移动指令, 直到停止或到达限位

    check_limit=False: 速度校准用, 不做限位判断, 严格按请求时长转动
    (校准需测全行程实际速度, 且估位本身不准确, 限位判断会提前中断转动)
    """
    tracker.start_move(direction)
    while not _hold_stop.is_set():
        if check_limit and tracker.at_limit(direction):
            break
        try:
            send(_MOVE_CMDS[direction]())
        except Exception:  # noqa: BLE001
            break
        _hold_stop.wait(_HOLD_INTERVAL)
    tracker.stop()
    # 结束时发送停止
    try:
        send(pelco.stop())
    except Exception:  # noqa: BLE001
        pass


def _stop_hold_locked():
    """停止持续转动 (调用方需已持有 _hold_lock)"""
    global _hold_thread, _hold_stop
    if _hold_thread is not None:
        _hold_stop.set()
        _hold_thread.join(timeout=0.5)
        _hold_thread = None
    try:
        send(pelco.stop())
    except Exception:  # noqa: BLE001
        pass


def start_hold(direction: str, check_limit: bool = True):
    """开始持续转动"""
    global _hold_thread, _hold_stop
    with _hold_lock:
        _stop_hold_locked()
        _hold_stop = threading.Event()
        _hold_thread = threading.Thread(
            target=_hold_loop, args=(direction, check_limit), daemon=True)
        _hold_thread.start()


def stop_hold():
    """停止持续转动"""
    with _hold_lock:
        _stop_hold_locked()


def ok(data: dict) -> tuple:
    return jsonify({"ok": True, **data})


def err(msg: str, code: int = 400) -> tuple:
    return jsonify({"ok": False, "detail": msg}), code


# ---------- 页面 ----------
@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/<path:filename>")
def static_files(filename: str):
    """服务前端静态文件 (如 satellite.js)"""
    return send_from_directory(FRONTEND_DIR, filename)


# ---------- 状态 ----------
@app.route("/api/status")
def status():
    try:
        ser = get_serial()
        # ESP32/UDP 在线状态: 编码器数据 2s 内有更新则在线
        enc_time = _encoder_state.get("time", 0.0)
        udp_online = (time.time() - enc_time) < 2.0
        return ok({"port": SERIAL_PORT, "baud": BAUD_RATE,
                   "address": PTZ_ADDRESS, "open": ser.is_open,
                   "paused": is_paused(),
                   "resetting": is_resetting(),
                   "udp_online": udp_online,
                   "tangle_warn": _encoder_state.get("tangle_warn"),
                   "pan_speed_dps": get_cfg("pan_speed_dps"),
                   "tilt_speed_dps": get_cfg("tilt_speed_dps"),
                   "pan_range": [get_cfg("pan_min"), get_cfg("pan_max")],
                   "tilt_range": [get_cfg("tilt_min"), get_cfg("tilt_max")]})
    except Exception as e:  # noqa: BLE001
        return err(str(e), 500)


# ---------- 设置 ----------
@app.route("/api/settings", methods=["GET"])
def get_settings():
    with _config_lock:
        return ok(dict(config))


@app.route("/api/settings", methods=["POST"])
def post_settings():
    data = request.get_json(silent=True) or {}
    cfg = update_config(data)
    return ok(cfg)


# ---------- 暂停控制 ----------
@app.route("/api/pause", methods=["POST"])
def pause():
    """清空所有命令并初始化: 停止持续转动、停止卫星跟踪、发送停止帧、重置航位推算位置"""
    data = request.get_json(silent=True) or {}
    paused = bool(data.get("paused", True))
    if paused:
        # 1. 停止持续转动
        stop_hold()
        # 2. 停止卫星跟踪
        try:
            sat_tracker.stop()
        except Exception:  # noqa: BLE001
            pass
        # 3. 结算航位推算位置并发送停止帧 (清空未发送命令)
        try:
            with tracker.lock:
                tracker._finalize()
            send(pelco.stop())
        except Exception:  # noqa: BLE001
            pass
    set_paused(paused)
    return ok({"paused": is_paused()})


# ---------- 方向控制 ----------
@app.route("/api/move/<direction>", methods=["POST"])
def move(direction: str):
    if direction == "stop":
        stop_hold()
        return ok({"direction": "stop"})
    if direction not in _MOVE_CMDS:
        return err("无效方向")
    if is_resetting():
        return ok({"direction": direction, "blocked": True, "detail": "正在复位，请稍后"})
    # 手动控制时停止卫星跟踪 (避免跟踪循环指令干扰)
    if sat_tracker.is_tracking():
        sat_tracker.stop()
    if tracker.at_limit(direction):
        return ok({"direction": direction, "blocked": True, "detail": "已达限位"})
    start_hold(direction)
    return ok({"direction": direction})


@app.route("/api/pulse", methods=["POST"])
def pulse():
    """点动脉冲: 朝指定方向转动固定时长 (短按触发)
    水平轴用 AS5600 实测实际转过角度并返回; 俯仰轴按速度×时长估算。
    """
    data = request.get_json(silent=True) or {}
    direction = str(data.get("dir", "")).strip().lower()
    ms = float(data.get("ms", 200) or 200)
    if direction not in _MOVE_CMDS:
        return err("无效方向")
    if is_resetting():
        return err("正在复位，请稍后", 409)
    if sat_tracker.is_tracking():
        sat_tracker.stop()
    ms = max(50.0, min(2000.0, ms))
    # 起始连续角度 (物理连续角, 已由 _process_encoder_data 换算, 无需再次减零位)
    with _encoder_lock:
        start_cont = _encoder_state.get("cont_angle")
    # 按固定时长转动 (复用持续转动机制, 与其他控制互斥)
    start_hold(direction, check_limit=False)
    time.sleep(ms / 1000.0)
    stop_hold()
    # 结束连续角度, 计算有向增量 (右转正, 左转负)
    delta = None
    with _encoder_lock:
        end_cont = _encoder_state.get("cont_angle")
    if start_cont is not None and end_cont is not None:
        d = end_cont - start_cont
        delta = round(d, 2)
    if delta is None and ("up" in direction or "down" in direction):
        speed = get_cfg("tilt_up_speed_dps" if "up" in direction else "tilt_down_speed_dps")
        delta = round(speed * ms / 1000.0, 2)
        if "down" in direction:
            delta = -delta
    return ok({"direction": direction, "ms": ms, "delta": delta})


@app.route("/api/move/to", methods=["POST"])
def move_to_position():
    """移动到指定 pan/tilt 位置, 用于测试运动精度
    云台不支持绝对定位, 采用基于速度的估算移动 (速度×时间)。
    """
    data = request.get_json(silent=True) or {}
    target_pan = data.get("pan")
    target_tilt = data.get("tilt")
    if target_pan is None and target_tilt is None:
        return err("请指定 pan 或 tilt")
    # 停止当前持续转动和卫星跟踪, 避免冲突
    stop_hold()
    sat_tracker.stop()
    _sat_send_dir("stop")
    # 后台线程执行移动, 避免 HTTP 请求阻塞
    threading.Thread(
        target=sat_tracker.move_to,
        args=(target_pan, target_tilt),
        daemon=True,
    ).start()
    return ok({"moving": True, "mode": "estimate", "pan": target_pan, "tilt": target_tilt})


# ---------- 辅助开关 ----------
@app.route("/api/aux/<action>", methods=["POST"])
def aux(action: str):
    data = request.get_json(silent=True) or {}
    aux_no = int(data.get("aux", 1))
    aux_no = max(1, min(2, aux_no))
    try:
        if action == "on":
            send(pelco.aux_on(aux_no))
        elif action == "off":
            send(pelco.aux_off(aux_no))
        else:
            return err("无效操作")
    except RuntimeError as e:
        return err(str(e), 500)
    return ok({"action": action, "aux": aux_no})


# ---------- 模糊位置 ----------
@app.route("/api/position")
def position():
    return ok(tracker.get())


# ---------- 串口监控 ----------
@app.route("/api/seriallog")
def seriallog():
    with _serial_log_lock:
        return ok({"log": list(_serial_log)})


# ---------- 复位 ----------
@app.route("/api/reset", methods=["POST"])
def reset():
    if is_resetting():
        return err("复位正在进行中")
    # 清理任务区命令队列: 停止持续转动、停止卫星跟踪、取消俯仰归零、发送停止帧
    _pre_track_stop.set()
    stop_hold()
    try:
        sat_tracker.stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        with tracker.lock:
            tracker._finalize()
        send(pelco.stop())
    except Exception:  # noqa: BLE001
        pass
    set_resetting(True)
    # 超限回转检测: 当前已越出防缠绕范围时, 先尝试自动回 0°; 回不去则立即提示, 不执行复位
    rec_ok, rec_detail = _recover_from_tangle()
    if not rec_ok:
        set_resetting(False)
        return ok({"detail": rec_detail, "abort": True})
    # 水平轴由低速转圈 + 光电零位找 0 (无时间设置); 俯仰轴按时间复位回 0°
    with tracker.lock:
        tracker._finalize()
        cur_tilt = tracker.tilt
    tilt_s = max(0.1, (cur_tilt - get_cfg("tilt_min")) / get_cfg("tilt_speed_dps")) + 3
    # 兜底: 取配置的俯仰复位时长与计算值较大者; 俯仰复位最长 17s (满行程 90°/5.18°s)
    tilt_s = min(17.0, max(tilt_s, get_cfg("reset_tilt_s")))

    def _run():
        try:
            _reset_loop(tilt_s)
        except Exception:  # noqa: BLE001
            pass
        finally:
            set_resetting(False)
    threading.Thread(target=_run, daemon=True).start()
    # 找零最多转一圈: 低速速率 = 全速 × 速度档位/0x20, 最坏时长 = 360°/低速速率
    low_speed = get_cfg("pan_speed_dps") * FIND_ZERO_SPEED / 0x20
    pan_s = 360.0 / low_speed if low_speed > 0 else FIND_ZERO_TIMEOUT
    total_s = max(tilt_s, pan_s)
    return ok({"detail": "复位指令已发送 (找光电零位后停在物理 0°)", "duration": round(total_s, 1),
               "find_s": round(pan_s, 1), "recover": rec_detail})


# ---------- 防缠绕复位 ----------
@app.route("/api/anti_tangle_reset", methods=["POST"])
def anti_tangle_reset():
    """防缠绕复位: 按累计水平转动量反向回退, 避免线缆缠绕
    pan_accum > 0 表示净右转, 需左转回退; < 0 表示净左转, 需右转回退
    """
    if is_resetting():
        return err("复位正在进行中")
    stop_hold()
    try:
        sat_tracker.stop()
    except Exception:  # noqa: BLE001
        pass
    with tracker.lock:
        tracker._finalize()
        accum = tracker.pan_accum   # ESP32 域 (angle - zero_angle)
    if abs(accum) < 4.0:   # 物理 < 1° 视为无需复位
        return ok({"detail": "无需防缠绕复位", "accum": 0, "duration": 0})
    direction = "left" if accum > 0 else "right"
    pan_speed = get_cfg("pan_speed_dps")
    accum_phys = abs(accum) / ENCODER_GEAR   # ESP32 域 -> 物理角
    duration = accum_phys / pan_speed + 1.0  # 估算时长 (仅用于前端按钮恢复)
    set_resetting(True)

    def _run():
        try:
            # 用 AS5600 反馈闭环回退: 持续反向转动, 直到累计归零 (回到无缠绕起点)
            # 比时间估算精确, 且能正确处理多圈缠绕
            timeout = time.time() + max(30.0, accum_phys / pan_speed * 2 + 5)
            while time.time() < timeout:
                with tracker.lock:
                    remaining = tracker.pan_accum
                if abs(remaining) < 4.0:   # ESP32 域, 物理 < 1°
                    break
                try:
                    send(_MOVE_CMDS[direction]())
                except Exception:  # noqa: BLE001
                    break
                time.sleep(0.05)
            try:
                send(pelco.stop())
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass
        finally:
            with tracker.lock:
                tracker._finalize()
                tracker.pan_accum = 0.0  # 防缠绕复位后清零
            # zero_angle 只由光电校准/手动设零设置, 防缠绕复位不改基准
            set_resetting(False)

    threading.Thread(target=_run, daemon=True).start()
    return ok({"detail": "防缠绕复位中",
                "accum": round(accum, 1),
                "direction": direction,
                "duration": round(duration, 1)})


# ---------- 校准转动 ----------
@app.route("/api/calibrate", methods=["POST"])
def calibrate():
    """按指定方向转动指定时长后停止, 用于速度标定"""
    data = request.get_json(silent=True) or {}
    direction = data.get("direction")
    try:
        duration = float(data.get("duration", 5))
    except (ValueError, TypeError):
        return err("时长格式错误")
    if direction not in _MOVE_CMDS:
        return err("无效方向")

    def _run():
        try:
            # 校准模式: 不做限位判断, 严格按请求时长转动
            start_hold(direction, check_limit=False)
            time.sleep(max(0.1, duration))
            stop_hold()
        except Exception:  # noqa: BLE001
            stop_hold()
        finally:
            # 校准后清零累计 (校准转动不应计入防缠绕)
            with tracker.lock:
                tracker._finalize()
                tracker.pan_accum = 0.0
                global _last_pan_cont
                _last_pan_cont = None
    threading.Thread(target=_run, daemon=True).start()
    return ok({"direction": direction, "duration": duration})


# ---------- 卫星跟踪接口 ----------
@app.route("/api/sat/tle", methods=["GET"])
def sat_tle():
    tle = satellite.fetch_tle()
    items = [{"norad": k, "name": v.get("name", "")} for k, v in tle.items()]
    items.sort(key=lambda x: x["name"])
    return ok({"count": len(items), "satellites": items})


@app.route("/api/sat/tle/refresh", methods=["POST"])
def sat_tle_refresh():
    def _run():
        satellite.fetch_tle(force=True)
    threading.Thread(target=_run, daemon=True).start()
    return ok({"detail": "TLE 刷新中"})


@app.route("/api/sat/observer", methods=["GET"])
def sat_observer_get():
    return ok(satellite.get_observer())


@app.route("/api/sat/observer", methods=["POST"])
def sat_observer_set():
    data = request.get_json(silent=True) or {}
    try:
        if data.get("grid"):
            lat, lon = satellite.maidenhead_to_latlon(str(data["grid"]))
            alt = float(data.get("alt", 0))
        else:
            lat = float(data.get("lat", 39.9))
            lon = float(data.get("lon", 116.4))
            alt = float(data.get("alt", 0))
    except (ValueError, TypeError):
        return err("经纬度或网格格式错误")
    satellite.set_observer(lat, lon, alt)
    return ok(satellite.get_observer())


@app.route("/api/sat/position/<norad>", methods=["GET"])
def sat_position(norad: str):
    pos = satellite.satellite_position(norad)
    if pos is None:
        return err("卫星不存在或无 TLE 数据", 404)
    return ok(pos)


@app.route("/api/sat/passes/<norad>", methods=["GET"])
def sat_passes(norad: str):
    """读取后台过境缓存, 按参数过滤"""
    hours = float(request.args.get("hours", 24))
    min_elev = float(request.args.get("min_elev", 0))
    now = time.time()
    cutoff = now + hours * 3600
    passes = [p for p in satellite.compute_passes_cached(norad)
              if p["aos"] <= cutoff and p["max_el"] >= min_elev]
    result = []
    for p in passes:
        result.append({
            "aos": p["aos"], "los": p["los"],
            "aos_az": round(p["aos_az"], 1), "los_az": round(p["los_az"], 1),
            "max_el": round(p["max_el"], 1),
            "duration": round(p["los"] - p["aos"], 0),
        })
    return ok({"passes": result})


@app.route("/api/sat/search")
def sat_search():
    """卫星检索: 按名称/编号匹配, 返回最多 20 条"""
    q = (request.args.get("q") or "").strip().lower()
    if not q:
        return ok({"count": 0, "satellites": []})
    tle = satellite.fetch_tle()
    items = [{"norad": k, "name": v.get("name", "")} for k, v in tle.items()]
    matches = [s for s in items
               if q in s["name"].lower() or q in s["norad"]]
    matches.sort(key=lambda s: (not s["name"].lower().startswith(q), s["name"].lower()))
    return ok({"count": len(matches), "satellites": matches[:20]})


@app.route("/api/sat/track/<norad>", methods=["GET"])
def sat_track(norad: str):
    """返回未来一段时间内的星下点轨迹"""
    hours = float(request.args.get("hours", 6))
    step = float(request.args.get("step", 60))
    points = []
    now = time.time()
    end = now + hours * 3600
    t = now
    while t <= end:
        pos = satellite.satellite_position(norad, t)
        if pos:
            points.append({"lat": pos["sub_lat"], "lon": pos["sub_lon"], "t": t})
        t += step
    return ok({"points": points})


# 俯仰轴无编码器反馈, 开启跟踪前若俯仰不在 0°, 需先开环按时间降到 0°
# (仰角位置不可信, 归零是唯一可校准的起点), 完成后才启动跟踪线程
_pre_track_stop = threading.Event()   # 置位 = 取消进行中的归零
_pre_track_busy = False                # 归零线程是否在跑
_pre_track_lock = threading.Lock()


@app.route("/api/sat/track/<norad>", methods=["POST"])
def sat_track_start(norad: str):
    global _pre_track_busy
    satellite.fetch_tle()
    if satellite.get_tle(norad) is None:
        return err("卫星不存在或无 TLE 数据", 404)
    with _pre_track_lock:
        if _pre_track_busy:
            return err("俯仰归零中, 请稍候")
        _pre_track_busy = True
    _pre_track_stop.clear()
    with tracker.lock:
        tracker._finalize()
        cur_tilt = tracker.tilt
    if cur_tilt <= 0.5:
        # 俯仰已在 0° 附近, 直接开始跟踪
        with _pre_track_lock:
            _pre_track_busy = False
        sat_tracker.start(norad)
        return ok({"tracking": True, "norad": norad})
    # 俯仰开环归零: 按当前推算 tilt 时长降到 0° (最长 90°/5.18°s ≈ 17s)
    tilt_s = min(max(1.0, cur_tilt / get_cfg("tilt_down_speed_dps") + 1.0), 20.0)

    def _run():
        try:
            tracker.start_move("down")
            end = time.time() + tilt_s
            while time.time() < end and not _pre_track_stop.is_set():
                try:
                    send(_MOVE_CMDS["down"]())   # down=低头 (该云台 pelco.up 即低头)
                except Exception:  # noqa: BLE001
                    break
                time.sleep(0.1)
        finally:
            try:
                send(pelco.stop())
            except Exception:  # noqa: BLE001
                pass
            with tracker.lock:
                tracker._finalize()
                tracker.tilt = 0.0      # 已按时长归零, 俯仰位置对齐 0°
                tracker._tilt_base = 0.0
            if not _pre_track_stop.is_set():
                sat_tracker.start(norad)
            with _pre_track_lock:
                _pre_track_busy = False

    threading.Thread(target=_run, daemon=True).start()
    return ok({"tracking": False, "norad": norad,
               "detail": f"俯仰归零中 {tilt_s:.0f}s"})


@app.route("/api/sat/track/stop", methods=["POST"])
def sat_track_stop():
    _pre_track_stop.set()   # 取消进行中的俯仰归零
    sat_tracker.stop()
    _sat_send_dir("stop")
    return ok({"tracking": False})


@app.route("/api/sat/track/status", methods=["GET"])
def sat_track_status():
    return ok({"tracking": sat_tracker.is_tracking(),
               "norad": sat_tracker.norad_id})


@app.route("/api/sat/radar/<norad>", methods=["GET"])
def sat_radar(norad: str):
    """返回未来一段时间内的方位角/仰角序列 (用于雷达图)"""
    minutes = float(request.args.get("minutes", 30))
    step = float(request.args.get("step", 30))
    points = []
    now = time.time()
    end = now + minutes * 60
    t = now
    while t <= end:
        pos = satellite.satellite_position(norad, t)
        if pos:
            points.append({"az": pos["azimuth"], "el": pos["elevation"], "t": t})
        t += step
    return ok({"points": points})


@app.route("/api/sat/favorites", methods=["GET"])
def sat_favorites_get():
    return ok({"favorites": satellite.get_favorites()})


@app.route("/api/sat/favorites", methods=["POST"])
def sat_favorites_add():
    data = request.get_json(silent=True) or {}
    norad = str(data.get("norad", ""))
    if not norad:
        return err("缺少 norad")
    if not satellite.add_favorite(norad):
        return err("卫星不存在或无 TLE 数据", 404)
    return ok({"favorites": satellite.get_favorites()})


@app.route("/api/sat/favorites/<norad>", methods=["DELETE"])
def sat_favorites_del(norad: str):
    satellite.remove_favorite(norad)
    return ok({"favorites": satellite.get_favorites()})


# ---------- AS5600 磁编码器回传(水平轴位置校正) ----------
_encoder_state = {"time": 0.0, "angle": None, "raw": None, "pan": None, "cont_angle": None,
                  "tangle_warn": None}  # tangle_warn: 防缠绕越界告警文本
_encoder_lock = threading.Lock()

ENCODER_CAL_FILE = os.path.join(BASE_DIR, "encoder_cal.json")

# 标定: zero_angle = 光电 0° 对应的 ESP32 累积角度(持久化, 由光电校零/复位写入)
# pan = (angle - zero_angle) % 360, 直接使用 ESP32 多圈 unwrap 角度, 与 raw 无关
_encoder_cal = {"zero_angle": None}

# ---------- 防缠绕角度范围 (ESP32 域) ----------
# 防缠绕累计角直接基于 ESP32 回传角度: off = angle - zero_angle (物理0°映射的 ESP32 记录角)。
# 4:1 减速下 物理 1° = ESP32 4°, 允许左右各 1.5 圈(物理 ±540°) = ESP32 域 ±2160°。
# 与 _recover_from_tangle 超限回转判定同域, 不再除 4。
TANGLE_MIN = -2160.0
TANGLE_MAX = 2160.0


def _tangle_warn(off):
    """判定 ESP32 域累计偏移 (angle - zero_angle) 是否越出防缠绕范围, 返回告警文本 (None=正常)"""
    if off is None:
        return None
    if off < TANGLE_MIN:
        return f"防缠绕越界: 累计偏移 {off:.0f}°ESP < 下限 {TANGLE_MIN:.0f}°ESP (物理 {off/ENCODER_GEAR:.0f}°)"
    if off > TANGLE_MAX:
        return f"防缠绕越界: 累计偏移 {off:.0f}°ESP > 上限 {TANGLE_MAX:.0f}°ESP (物理 {off/ENCODER_GEAR:.0f}°)"
    return None


def _load_encoder_cal():
    global _encoder_cal
    _encoder_cal = {"zero_angle": None}
    try:
        if os.path.exists(ENCODER_CAL_FILE):
            with open(ENCODER_CAL_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, dict):
                    if d.get("zero_angle") is not None:
                        _encoder_cal["zero_angle"] = float(d["zero_angle"])
    except Exception:  # noqa: BLE001
        _encoder_cal = {"zero_angle": None}


def _save_encoder_cal():
    try:
        with open(ENCODER_CAL_FILE, "w", encoding="utf-8") as f:
            json.dump(_encoder_cal, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        print(f"[encoder_cal] save error: {e!r}", flush=True)


# 云台输出轴 1 圈 = AS5600 4 圈 (4:1 减速): ESP32 累积角 = 物理角 × 4。
# 后端统一换算为物理角, 故下面映射均除以 4。
ENCODER_GEAR = 4.0


def _pan_from_angle(angle):
    """ESP32 累积角度 -> 云台 pan (0~360°, 物理角), 未标定返回 None。
    360° 与 0° 同位置 = 光电零位。顺时针递增, 逆时针从 360 递减 (不用负值)。
    """
    zero = _encoder_cal.get("zero_angle")
    if zero is None or angle is None:
        return None
    return ((float(angle) - zero) / ENCODER_GEAR) % 360.0


def _pan_cont_from_angle(angle):
    """ESP32 累积角度 -> 连续物理 pan (不取模, 防缠绕累计用), 未标定返回 None"""
    zero = _encoder_cal.get("zero_angle")
    if zero is None or angle is None:
        return None
    return (float(angle) - zero) / ENCODER_GEAR


def _reanchor_zero(angle=None):
    """云台物理回到 0° 后调用: 以当前 ESP32 累积角度为新基准对齐 0 点
    消除复位/标定过程中 AS5600 累计漂移。angle=None 时取最新上报值。
    """
    global _last_pan_cont
    if angle is None:
        with _encoder_lock:
            angle = _encoder_state.get("angle")
    if angle is None:
        return
    _encoder_cal["zero_angle"] = float(angle)
    _last_pan_cont = None
    _save_encoder_cal()  # 复位后 0 点变化, 持久化防重启丢失
    print(f"[encoder] 零点锚定 angle={angle:.2f} (光电零位/复位)", flush=True)


# 光电零位事件 (复位找零时由 UDP 线程置位, _reset_loop 消费)
_zero_evt = threading.Event()
_zero_evt_angle = None
_zero_evt_t = 0.0          # 光电事件到达服务器的时间戳 (用于复位时忽视旧事件)
# 最近一次光电零位触发 (ESP32 累积角度域): 用于判断挡片是否仍停在窗口内
_zero_last_angle = None
_zero_last_t = 0.0


# 上一次回传角度偏移 (ESP32 域), 用于增量累计防缠绕
_last_pan_cont = None


_load_encoder_cal()


def _process_encoder_data(angle, raw):
    """处理 AS5600 编码器数据, 更新水平轴位置 (HTTP POST 和 UDP 共用)
    直接使用 ESP32 固件 unwrap 累积角度 (多圈展开), 计算 pan = (angle - zero) % 360。
    raw 仅作诊断显示, 不参与计算 (raw 是 0~4096 循环的单圈值)。
    """
    global _last_pan_cont
    now = time.time()
    pan = None
    cont_angle = None
    # 固件 unwrap 已处理角度差分 (±180° 截断, 天然抗抖动), 后端直接透传累积角度。
    if angle is not None:
        try:
            angle = float(angle)
            pan = _pan_from_angle(angle)
            cont_angle = _pan_cont_from_angle(angle)
        except (ValueError, TypeError):
            pass
    if pan is None:
        # 未标定 (zero_angle=None) 时 pan/cont_angle 无意义, 但 angle/raw 仍需上报
        with _encoder_lock:
            _encoder_state["time"] = now
            _encoder_state["angle"] = angle
            _encoder_state["raw"] = raw
            _encoder_state["cont_angle"] = None
            _encoder_state["pan"] = None
            _encoder_state["tangle_warn"] = None
        return
    # 防缠绕累计: 直接用 ESP32 域偏移 off = angle - zero_angle 增量 (与超限回转同域)
    zero = _encoder_cal.get("zero_angle")
    off = (float(angle) - zero) if zero is not None else None
    if off is not None:
        if _last_pan_cont is not None:
            delta = off - _last_pan_cont
            # 异常跳变(复位/标定瞬间) 视为 0, 不污染累计 (ESP32 域 ±180° = 物理 ±45°)
            if abs(delta) <= 180.0:
                with tracker.lock:
                    tracker.pan_accum += delta
        _last_pan_cont = off
    with _encoder_lock:
        _encoder_state["time"] = now
        _encoder_state["angle"] = angle
        _encoder_state["raw"] = raw
        _encoder_state["cont_angle"] = cont_angle
        _encoder_state["pan"] = pan
        # 防缠绕越界判定: 越界即告警, 提示需复位归中 (ESP32 域 off)
        warn = _tangle_warn(off)
        if warn != _encoder_state.get("tangle_warn"):
            if warn:
                print(f"[tangle] {warn}", flush=True)
            _encoder_state["tangle_warn"] = warn
    with tracker.lock:
        if tracker._moving is not None:
            direction, start = tracker._moving
            elapsed = now - start
            if elapsed > 0:
                _, tilt_delta = tracker._angle_for_time(elapsed, direction)
                if "up" in direction:
                    tracker._tilt_base += tilt_delta
                if "down" in direction:
                    tracker._tilt_base -= tilt_delta
                tracker.tilt = tracker._tilt_base
                tracker._clamp()
            tracker._pan_base = pan
            tracker._moving = (direction, now)
        tracker.pan = pan


# ---------- UDP 监听 (AS5600 编码器数据 + 光电零位事件) ----------
def _handle_encoder_payload(payload: dict):
    """统一处理 ESP32 上报: 编码数据 / 光电零位事件"""
    if payload.get("event") == "zero":
        global _zero_evt_angle, _zero_last_angle, _zero_last_t
        angle = payload.get("angle")
        # 记录最近一次零位触发, 供复位判断挡片是否已停在窗口内
        if angle is not None:
            _zero_last_angle = float(angle)
            _zero_last_t = time.time()
        if _photo_calib["running"]:
            # 光电自动校零: 采样触发点 (起点 0° / 一圈后终点)
            # 直接使用 ESP32 zero 报文自带的累积角度 (与触发时刻严格同步, 无时间差)
            angle_val = payload.get("angle")
            if angle_val is None:
                with _encoder_lock:
                    angle_val = _encoder_state.get("angle")
            print(f"[photo_calib] 收到zero angle={angle_val} phase={_photo_calib['phase']}", flush=True)
            _photo_calib_on_trigger(angle_val)
            return
        if is_resetting():
            # 复位找零: 置事件供 _reset_loop 消费 (以触发瞬间 angle 精确锚定 0 点)
            global _zero_evt_t
            _zero_evt_angle = payload.get("angle")
            if _zero_evt_angle is None:
                with _encoder_lock:
                    _zero_evt_angle = _encoder_state.get("angle")
            _zero_evt_t = time.time()   # 记录事件到达时间, 供复位忽视旧事件
            _zero_evt.set()
            return
        if sat_tracker.is_tracking():
            # 跟踪中经过光电零位: 仅记录, 不改基准 (zero_angle 只由校准/手动设零设置)
            print(f"[encoder] 跟踪中经过光电零位 angle={payload.get('angle')}", flush=True)
            return
        # 手动/空闲状态经过光电零位: 不锚定 0 点。
        # 绝对 0°(360°) 由标定时的 zero_angle 固定, 手动转动不应重设基准。
        return
    _process_encoder_data(payload.get("angle"), payload.get("raw"))


def _udp_encoder_listener():
    """监听 UDP 广播, 接收 ESP32 AS5600 编码器数据与零位事件"""
    import socket as _socket
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 8091))
    print("[udp] AS5600 监听已启动: 0.0.0.0:8091", flush=True)
    while True:
        try:
            data, addr = sock.recvfrom(1024)
            payload = json.loads(data.decode("utf-8"))
            _handle_encoder_payload(payload)
        except Exception as e:  # noqa: BLE001
            print(f"[udp] handler error: {e!r}", flush=True)


threading.Thread(target=_udp_encoder_listener, daemon=True).start()


@app.route("/api/encoder", methods=["POST"])
def encoder_post():
    """接收 AS5600 磁编码器数据 / 光电零位事件 (HTTP POST 兼容保留, UDP 为主)"""
    data = request.get_json(silent=True) or {}
    _handle_encoder_payload(data)
    if data.get("event") == "zero":
        return ok({"event": "zero", "zero_angle": _encoder_cal.get("zero_angle")})
    raw = data.get("raw")
    angle = data.get("angle")
    if raw is None and angle is None:
        return err("缺少 raw 或 angle")
    with _encoder_lock:
        st = dict(_encoder_state)
    return ok({"raw": raw, "angle": angle,
               "pan": st.get("pan"), "cont_angle": st.get("cont_angle")})


@app.route("/api/encoder", methods=["GET"])
def encoder_get():
    with _encoder_lock:
        st = dict(_encoder_state)
    st["zero_angle"] = _encoder_cal.get("zero_angle")
    return ok(st)



# ---------- 光电自动校零 + 测速 (零位由光电传感器自动锚定, 全程无需手动对准) ----------
# 原理: 云台全速右转, 利用光电挡片触发两次(起点 0° + 转过一圈再回 0°)。
# 直接使用 ESP32 累积角度: 两次触发间的累积角度增量 = 输出轴一圈对应的 AS5600 转角,
# 得传动比 (AS5600 圈数/输出轴1圈); 由 360/一圈耗时 得实际水平速度。
# 触发瞬间角度由 ESP32 读取, 不受转动速度/过冲影响, 起终点均精确锚定 0°。
_photo_calib_lock = threading.Lock()
_photo_calib = {
    "running": False,   # 是否在光电校零/测速中
    "mode": "auto",     # auto(后端控制转圈) / logonly(只记录不控制)
    "phase": "idle",    # idle / waiting_first / waiting_second / done
    "start_angle": None,  # 第一次触发 ESP32 累积角度 (起点 0°)
    "end_angle": None,    # 第二次触发 ESP32 累积角度
    "t0": 0.0,          # 第一次触发时刻
    "t1": 0.0,          # 第二次触发时刻
    "stop_cmd": None,   # 后台转动线程停止 Event
}


def _photo_calib_on_trigger(angle_val):
    """光电校零期间, zero 事件到达时采样触发点 (由 _handle_encoder_payload 调用)
    直接使用 ESP32 累积角度 (unwrap 多圈度数) 计算, 抗丢包/卡死
    """
    with _photo_calib_lock:
        if not _photo_calib["running"]:
            return
        now = time.time()
        ph = _photo_calib["phase"]
        if ph == "waiting_first":
            # 第一次触发: 起点 0°
            _photo_calib["start_angle"] = angle_val
            _photo_calib["t0"] = now
            _photo_calib["phase"] = "waiting_second"
            print(f"[photo_calib] 起点触发 angle={angle_val}", flush=True)
        elif ph == "waiting_second":
            # 第二次触发: 用时间间隔(>3s)判断
            elapsed = now - _photo_calib["t0"]
            print(f"[photo_calib] 二次触发 angle={angle_val} elapsed={elapsed:.1f}s", flush=True)
            if elapsed >= 3.0:
                _photo_calib["end_angle"] = angle_val
                _photo_calib["t1"] = now
                _photo_calib["phase"] = "done"
                print(f"[photo_calib] 终点触发 angle={angle_val} elapsed={elapsed:.1f}s", flush=True)


def _photo_calib_worker():
    """后台: 控制云台全速右转一圈, 两次光电触发后校零+测速"""
    global _last_pan_cont
    try:
        with _photo_calib_lock:
            stop_ev = _photo_calib["stop_cmd"] = threading.Event()
            _photo_calib["phase"] = "waiting_first"
        deadline = time.time() + 180.0  # 一圈 + 兜底
        while time.time() < deadline:
            with _photo_calib_lock:
                phase = _photo_calib["phase"]
            if phase == "done":
                break
            try:
                send(pelco.right())  # 全速右转找光电触发
            except Exception:  # noqa: BLE001
                break
            stop_ev.wait(0.02)
    finally:
        for _ in range(3):
            try:
                send(pelco.stop())
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.15)
        # 结算
        with _photo_calib_lock:
            done = _photo_calib["phase"] == "done"
            start_angle = _photo_calib.get("start_angle")
            end_angle = _photo_calib.get("end_angle")
            t0 = _photo_calib["t0"]
            t1 = _photo_calib["t1"]
            _photo_calib["running"] = False
            _photo_calib["phase"] = "idle"
        result = None
        if (done and start_angle is not None and end_angle is not None
                and (t1 - t0) > 1.0):
            angle_delta = end_angle - start_angle
            as5600_revs = abs(angle_delta) / 360.0
            if as5600_revs > 0.1:
                v_real = 360.0 / (t1 - t0)
                # 以终点 (第二次光电触发 = 光电 0° 位置) 作为绝对 0 基准
                _encoder_cal["zero_angle"] = float(end_angle)
                _last_pan_cont = None
                _save_encoder_cal()
                update_config({"pan_speed_dps": v_real})
                sat_tracker.pan_speed = v_real
                with tracker.lock:
                    tracker._finalize()
                    if mode == "auto":
                        tracker.pan = 0.0
                        tracker.tilt = 0.0
                        tracker._pan_base, tracker._tilt_base = 0.0, 0.0
                        tracker.pan_accum = 0.0
                result = {
                    "ok": True,
                    "ratio": round(as5600_revs, 4),
                    "pan_speed_dps": round(v_real, 3),
                    "as5600_revs": round(as5600_revs, 2),
                    "zero_angle": round(float(end_angle), 2),
                    "angle_delta": round(angle_delta, 1),
                    "elapsed": round(t1 - t0, 1),
                }
        if result is None:
            result = {"ok": False, "detail": "光电校零未完成 (角度差无效或触发不足), 请检查光电传感器/ESP32 累积角度"}
        with _photo_calib_lock:
            _photo_calib["result"] = result


@app.route("/api/encoder/photocalib", methods=["GET", "POST"])
def encoder_photocalib():
    """光电自动校零+测速: 后端控制云台自动转一圈, 两次触发后校零+测速"""
    if request.method == "GET":
        with _photo_calib_lock:
            running = _photo_calib["running"]
            phase = _photo_calib["phase"]
            result = _photo_calib.get("result")
        return ok({"running": running, "phase": phase, "result": result})

    data = request.get_json(silent=True) or {}
    action = data.get("action")
    if action == "start":
        if is_paused():
            return err("云台处于暂停状态, 请先恢复")
        with _photo_calib_lock:
            if _photo_calib["running"]:
                return err("光电校零/测速正在进行中")
            _photo_calib["running"] = True
            _photo_calib["mode"] = "auto"
            _photo_calib["phase"] = "waiting_first"
            _photo_calib["result"] = None
        # 停止其他运动源
        try:
            stop_hold()
        except Exception:  # noqa: BLE001
            pass
        try:
            sat_tracker.stop()
        except Exception:  # noqa: BLE001
            pass
        threading.Thread(target=_photo_calib_worker, daemon=True).start()
        return ok({"msg": "光电自动校零开始: 云台自动转一圈, 光电触发两次完成校零与测速"})
    if action == "stop":
        with _photo_calib_lock:
            stop_ev = _photo_calib.get("stop_cmd")
            if not _photo_calib["running"]:
                return ok({"msg": "当前没有进行中的光电校零/测速"})
            if stop_ev:
                stop_ev.set()
            _photo_calib["running"] = False
            _photo_calib["phase"] = "idle"
        try:
            send(pelco.stop())
        except Exception:  # noqa: BLE001
            pass
        return ok({"msg": "已停止光电校零/测速"})
    return err("action 需为 start / stop")


@app.route("/api/encoder/setzero", methods=["POST"])
def encoder_setzero():
    """手动设置当前位置为物理 0° 基准角 (zero_angle = 当前 ESP32 累积角度)"""
    with _encoder_lock:
        angle = _encoder_state.get("angle")
    if angle is None:
        return err("无编码器数据, 无法设置基准角")
    _reanchor_zero(float(angle))
    return ok({"zero_angle": round(float(angle), 2), "msg": f"已设置当前位置为 0° 基准 (angle={angle:.2f})"})


if __name__ == "__main__":
    satellite.start_background_jobs()  # 后台: TLE 6h / 过境 10min
    port = int(os.getenv("PTZ_PORT", "8090"))
    print(f"YD3040 云台控制服务启动: http://0.0.0.0:{port}")
    serve(app, host="0.0.0.0", port=port)
