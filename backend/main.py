"""
雅安 YD3040 云台 (Pelco-D) 网页控制后端
特性:
- 电机恒速 7.5°/s, 无角度回传, 无预置位, 无绝对定位
- 水平范围 0~355°, 垂直范围 0~90°
- 通过航位推算(dead reckoning)估算模糊位置
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")

import serial
from flask import Flask, Response, jsonify, request, send_from_directory, session
from flask.sessions import SecureCookieSessionInterface
from waitress import serve
import ws_server  # WebSocket 实时推送 (与 SSE 并行, 前端 WS 优先/SSE 降级)

from pelco import PelcoD
import satellite
import streaming
import auth
import rotctld_server
import lotw

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
    "show_maidenhead_grid": False,   # 地图显示梅登海德网格
    "lotw_callsign": "",             # LoTW 登录呼号
    "lotw_password": "",             # LoTW 登录密码
    "vucc_bands": ["6m", "2m", "70cm", "sat"],  # VUCC 网格显示频段过滤
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
                dv = DEFAULT_CONFIG[k]
                if isinstance(dv, bool):
                    config[k] = bool(data[k])
                elif isinstance(dv, str):
                    config[k] = str(data[k]).strip()
                elif isinstance(dv, list):
                    v = data[k]
                    config[k] = [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []
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
            or path.startswith("/api/encoder/")
            or path == "/api/wsurl")  # 仅返回 WS 端点字符串, 无敏感数据, 供前端未登录也可探测


# WebSocket 握手鉴权: 复用 Flask session 签名验签 Cookie, 防止未登录连接收实时数据
_ws_signer = None
def _ws_check_auth(cookie_str: str) -> bool:
    global _ws_signer
    if not cookie_str:
        return False
    if _ws_signer is None:
        _ws_signer = SecureCookieSessionInterface().get_signing_serializer(app)
    sess_val = None
    for part in cookie_str.split(";"):
        k, _, v = part.strip().partition("=")
        if k == app.config.get("SESSION_COOKIE_NAME", "session"):
            sess_val = v
            break
    if not sess_val:
        return False
    try:
        return bool((_ws_signer.loads(sess_val) or {}).get("user"))
    except Exception:  # noqa: BLE001 签名无效/过期
        return False


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
    global _last_send_time
    if is_paused():
        return
    for attempt in (0, 1):
        try:
            ser = get_serial()
            with _serial_lock:
                ser.write(cmd)
                ser.flush()
            _log_serial("TX", cmd)
            _last_send_time = time.time()
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
        # 水平: 始终以编码器换算的物理角为准 (实时或最后已知值)。
        # 编码器短暂停更时不用航位推算回退 (推算按平均速度累积, 会漂移,
        # 曾导致: 云台物理已到 90°, 显示仍停在 70°), 保留最后编码器位置最稳。
        enc_pan = _encoder_state.get("pan")
        enc_pan_t = _encoder_state.get("pan_time", 0.0)
        # enc_ok: pan 值是否新鲜 (2.5s 内), 用 pan_time 而非 keepalive time,
        # 拒包时 time 刷新但 pan_time 不更新 → enc_ok 超时变 False → 跟踪循环停 move 防盲转
        enc_ok = enc_pan is not None and (time.time() - enc_pan_t) < 2.5
        if enc_pan is not None:
            pan = enc_pan
        else:
            pan = tracker._estimate()[0]   # 从未标定过编码器: 才回退推算
        tilt = tracker._estimate()[1]
        return pan, tilt, enc_ok


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
    global _zero_evt_angle, _last_pan_cont, _display_pan
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
    # 免回转路径: 不做滑行重锚 (基准保持手动设零值不变)。
    # 曾现: 免回转后仍重锚到当前回传角, 把用户手动基准覆盖掉 (~0.8°物理漂移)
    _reanchor_skip = already_at_zero

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
        # 紧急刹车: 立即中止复位 (send() 已被 paused 阻断, 云台静止;
        # 不重锚、不归零, 保持当前基准, 恢复后可重新复位)
        if is_paused():
            print("[reset] 紧急刹车中止复位", flush=True)
            return
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
        if _reanchor_skip:
            # 免回转 (已在基准附近): 基准未变, 不重锚 —— 保护手动设零基准
            print(f"[reset] 免回转, 保持基准不重锚 zero={zero_angle_found:.2f}", flush=True)
        else:
            # 滑行补偿: 单帧锁存云台 stop 后有滑行, 最终停止点 ≠ 光电触发点,
            # 若以触发点锚定 zero_angle, 停稳后 pan = 滑行量 ≠ 0 (显示与物理不符)。
            # 等滑行结束后以最终停止角重锚 0°, pan 精确归 0; 每次复位滑行量相近, 不累积。
            time.sleep(2.0)   # 等待滑行停止 (UDP 在线时 angle 持续刷新至最终位置)
            with _encoder_lock:
                final_angle = _encoder_state.get("angle")
                _pan_t = _encoder_state.get("pan_time", 0.0)
            if final_angle is None or time.time() - _pan_t > 1.0:
                # 数据已冻结 (滤波全拒/断联): 退回光电触发角, 避免锚到毛刺冻结值
                # (曾现: 毛刺冻结 angle=7079.94 被当滑行终点锚定, 基准错 107°ESP)
                final_angle = zero_angle_found
            _reanchor_zero(final_angle)
            print(f"[reset] 水平复位完成 光电触发={zero_angle_found:.2f} 滑行停止={final_angle:.2f} "
                  f"(滑行补偿 {(final_angle - zero_angle_found) / ENCODER_GEAR:.2f}°物理)", flush=True)
    else:
        print("[reset] 水平复位超时未触发光电, 停转 (请检查光电传感器)", flush=True)

    # 重置跟踪器位置与显示状态 (pan 立即归 0, 不等 UDP 回传)
    with tracker.lock:
        tracker.pan = 0.0
        tracker.tilt = 0.0
        tracker._pan_base, tracker._tilt_base = 0.0, 0.0
        tracker.pan_accum = 0.0
    _last_pan_cont = 0.0
    if zero_angle_found is not None:
        with _encoder_lock:
            _encoder_state["pan"] = 0.0
            _encoder_state["dpan"] = 0.0
            _display_pan = 0.0
            _encoder_state["cont_angle"] = 0.0
            _encoder_state["pan_time"] = time.time()
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


# ---------- SkyRoof / Hamlib rotctld 桥接 (TCP 4533) ----------
# 供 SkyRoof 等 rotctld 协议客户端直接连接操控云台
rotctld_srv = None  # 实际实例在 __main__ 中创建并赋值
_rotck_move_lock = threading.Lock()
_rotck_move_th = None
_rotck_target = None              # 最新待执行目标 (az, el), 游标式更新不丢弃
_rotck_stop_ev = threading.Event()  # 置位 = 取消进行中的 move_to


def _rotck_get_position():
    """rotctld 查询位置: 返回 dict {pan, tilt} (以编码器换算的物理角为准)"""
    pan, tilt, _ = _sat_get_position()
    return {"pan": pan, "tilt": tilt}


def _rotck_move_worker():
    """游标式循环: 逐个执行最新目标, 不丢命令; 取消置位即停
    (原实现: move_to 为阻塞式, 上一个线程活着时新命令被静默丢弃,
     SkyRoof 高频 P 命令下云台会一直朝旧目标转 = "不可控转动")"""
    global _rotck_target
    while True:
        with _rotck_move_lock:
            tgt = _rotck_target
            _rotck_target = None
        if tgt is None or _rotck_stop_ev.is_set():
            break
        az, el = tgt
        try:
            sat_tracker.move_to(az, el, cancel_ev=_rotck_stop_ev)
        except Exception as e:  # noqa: BLE001
            print(f"[rotctld] move_to failed: {e}", flush=True)


def _rotck_set_position(az, el):
    """rotctld P 命令: 记录最新目标, 后台线程逐个执行, 立即返回不阻塞协议连接。
    网页卫星/天体跟踪激活时屏蔽 SkyRoof 控制 (以网页为准), 避免两控制源抢云台"""
    global _rotck_move_th, _rotck_target
    if sat_tracker.is_tracking():
        print(f"[rotctld] P {az} {el} 被屏蔽: 网页跟踪进行中", flush=True)
        return
    _rotck_stop_ev.clear()
    with _rotck_move_lock:
        _rotck_target = (az, el)
        if _rotck_move_th is None or not _rotck_move_th.is_alive():
            _rotck_move_th = threading.Thread(
                target=_rotck_move_worker, daemon=True
            )
            _rotck_move_th.start()


def _rotck_stop_motion():
    """rotctld S 命令: 取消进行中的 move_to + 发停止帧, 云台立即停
    (原实现只发停止帧, 阻塞式 move_to 线程继续驱动 = 复位后仍朝旧方向转)"""
    _rotck_stop_ev.set()
    _sat_send_dir("stop")


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


# ---------- gzip 压缩 (弱 WiFi 链路下传输量减 ~75%, 页面加载明显提速) ----------
_GZIP_TYPES = {"text/html", "text/css", "application/javascript", "text/javascript",
               "application/json", "text/plain", "image/svg+xml"}


@app.after_request
def _gzip_response(resp):
    """对文本类 200 响应做 gzip; SSE 流(image/event-stream)与已压缩格式跳过"""
    if resp.mimetype == "text/event-stream":
        return resp
    if (resp.status_code != 200 or resp.mimetype not in _GZIP_TYPES
            or "gzip" in (resp.headers.get("Content-Encoding") or "")):
        return resp
    if resp.direct_passthrough:  # send_from_directory 的文件流, 允许读入内存
        resp.direct_passthrough = False
    data = resp.get_data()
    if len(data) < 1024:
        return resp
    resp.set_data(gzip.compress(data, 6))
    resp.headers["Content-Encoding"] = "gzip"
    resp.headers["Content-Length"] = str(len(resp.get_data()))
    resp.headers.add("Vary", "Accept-Encoding")
    return resp


# ---------- 页面 ----------
@app.route("/")
def index():
    resp = send_from_directory(FRONTEND_DIR, "index.html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# 天地图瓦片代理: 绕过 WAF 浏览器 UA 封禁 (服务器直连, 无浏览器请求头)
# 带磁盘缓存: 瓦片几乎不变, 命中直接回本地 (J1900 首屏提速 + 天地图故障时仍可显示)
_TDT_KEY = "d0ca322ca9f024d7673cd4d91e588290"
_TDT_LAYERS = {"cva": "cva_w", "vec": "vec_w"}
_TDT_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "tdt")
# 瓦片几乎不变: 长缓存 immutable → 浏览器端二次打开地图瓦片直接命中本地缓存,
# 不再经电力猫+隧道弱链路逐片重传 (服务器端仍有磁盘缓存, 首传命中即回)
_TILE_HEADERS = {"Content-Type": "image/png",
                 "Cache-Control": "public, max-age=2592000, immutable"}


def _tile_response(data: bytes):
    return Response(data, 200, _TILE_HEADERS)


@app.route("/tdt/<layer>/<int:z>/<int:x>/<int:y>")
def tdt_tile_proxy(layer: str, z: int, x: int, y: int):
    """天地图瓦片代理: 磁盘缓存命中直接回(下次同瓦片免回源), 未命中抓取并落盘。"""
    data = _fetch_tile(layer, z, x, y)
    if data is None:
        return err("tile proxy failed"), 502
    return _tile_response(data)


def _fetch_tile(layer: str, z: int, x: int, y: int) -> bytes | None:
    """抓取单张天地图瓦片: 先查服务器磁盘缓存, 未命中再从天地图拉取并写入缓存。
    供代理与后台预热线程共用 → 大量瓦片预存到 J1900, 任意访问从服务器分发。"""
    tdt_layer = _TDT_LAYERS.get(layer)
    if not tdt_layer:
        return None
    cache_path = os.path.join(_TDT_CACHE_DIR, layer, str(z), str(x), f"{y}.png")
    try:
        with open(cache_path, "rb") as f:  # 已缓存, 直接服务器分发
            return f.read()
    except OSError:
        pass
    url = (f"https://t0.tianditu.gov.cn/{tdt_layer}/wmts"
           f"?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0"
           f"&LAYER={layer}&STYLE=default&TILEMATRIXSet=w"
           f"&TILEMATRIX={z}&TILERow={y}&TILECol={x}"
           f"&FORMAT=tiles&tk={_TDT_KEY}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ""})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = resp.read()
    except Exception:
        return None
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "wb") as f:
            f.write(data)
    except Exception:  # noqa: BLE001
        pass  # 缓存写入失败不影响使用
    return data


def _prewarm_tiles():
    """后台预热: 把观测站周边常用缩放层级(3~8)的天地图瓦片(底图+标注)预先存到 J1900,
    使外网/内网访问时地图瓦片直接从服务器分发, 首打开即命中, 无需现场回源天地图。"""
    import math
    try:
        obs = satellite.get_observer()
        lat, lon = obs["lat"], obs["lon"]
    except Exception:  # noqa: BLE001
        lat, lon = 39.9, 116.4
    with ThreadPoolExecutor(max_workers=6) as ex:
        for z in range(3, 9):   # zoom 3~8
            n = 1 << z
            xt = int((lon + 180.0) / 360.0 * n)
            lat_r = math.radians(lat)
            yt = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
            for x in range(max(0, xt - 1), min(n, xt + 2)):
                for y in range(max(0, yt - 1), min(n, yt + 2)):
                    ex.submit(_fetch_tile, "vec", z, x, y)
                    ex.submit(_fetch_tile, "cva", z, x, y)
    print("[tdt] 瓦片预热完成")


@app.route("/<path:filename>")
def static_files(filename: str):
    """服务前端静态文件 (如 satellite.js)。
    静态资源加长缓存 immutable → 外网二次访问命中浏览器缓存, 不再经 Tunnel 重复慢传;
    HTML 走 no-cache 协商, 配合 ?v= 版本号即时更新而不过度缓存。
    """
    resp = send_from_directory(FRONTEND_DIR, filename)
    if filename.endswith((".js", ".css", ".jpg", ".jpeg", ".png", ".gif",
                          ".svg", ".ico", ".woff", ".woff2")):
        resp.headers["Cache-Control"] = "public, max-age=604800, immutable"
    else:
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/api/wsurl")
def ws_url():
    """告知前端 WS 推送端点。
    内网 IP → ws://IP:PORT (独立端口 8092, 直连);
    外网域名 → wss://<host>/ws (同一域名 /ws 路径, 同源 cookie 可通过握手鉴权;
     需在 Cloudflare Tunnel 把 <host>/ws 转发到 192.168.31.70:8092;
     未配置时前端连不上会自动回退 SSE, 功能不中断。)
    """
    host = request.host.split(":")[0]
    wport = os.getenv("PTZ_WS_PORT", "8092")
    ws_server.ensure_started(wport, _ws_frame_factory, _ws_check_auth)
    if host.replace(".", "").isdigit():   # 内网 IP
        return jsonify(ok=True, ws=f"ws://{host}:{wport}")
    # 外网域名: 走同一域名 /ws 路径 (Cloudflare 把 /ws 路径转发到 8092, 同源 cookie 可鉴权)
    return jsonify(ok=True, ws=f"wss://{host}/ws")


# ---------- 状态 ----------
def _status_payload() -> dict:
    """串口/系统状态 (供 /api/status 与 SSE 状态帧共用)"""
    ser = get_serial()
    # ESP32/UDP 在线状态: 编码器数据 2s 内有更新则在线
    enc_time = _encoder_state.get("time", 0.0)
    udp_online = (time.time() - enc_time) < 2.0
    return {"port": SERIAL_PORT, "baud": BAUD_RATE,
            "address": PTZ_ADDRESS, "open": ser.is_open,
            "paused": is_paused(),
            "resetting": is_resetting(),
            "udp_online": udp_online,
            "tangle_warn": _encoder_state.get("tangle_warn"),
            "pan_speed_dps": get_cfg("pan_speed_dps"),
            "tilt_speed_dps": get_cfg("tilt_speed_dps"),
            "pan_range": [get_cfg("pan_min"), get_cfg("pan_max")],
            "tilt_range": [get_cfg("tilt_min"), get_cfg("tilt_max")]}


@app.route("/api/status")
def status():
    try:
        return ok(_status_payload())
    except Exception as e:  # noqa: BLE001
        return err(str(e), 500)


# ---------- SSE 推流 (一条连接替代前端多路轮询) ----------
def _stream_state(norad, celestial):
    """状态帧: 串口状态 + 云台位置 + 目标位置 + 编码器角度 (卫星/天体)
    (角度并入 SSE, 内网/外网统一一条流实时更新; 不再依赖前端快轮询)
    """
    frame = {"status": _status_payload(), "pos": tracker.get()}
    # 编码器角度 (与 /api/encoder GET 同构): dpan/pan/tilt 供前端实时刷新方向线与角度
    with _encoder_lock:
        enc = dict(_encoder_state)
    enc["zero_angle"] = _encoder_cal.get("zero_angle")
    enc["now"] = time.time()
    enc["rejected"] = _last_rejected
    with tracker.lock:
        enc["tilt"] = tracker.tilt
    frame["enc"] = enc
    if celestial in ("moon", "sun"):
        pos = satellite.moon_position() if celestial == "moon" else satellite.sun_position()
        if pos:
            frame["sat"] = pos
            frame["sat_kind"] = celestial
    elif norad:
        pos = satellite.satellite_position(norad)
        if pos:
            frame["sat"] = pos
            frame["sat_kind"] = "sat"
    # 地图常显: 月球/太阳星下点 (晨昏线由前端按太阳星下点自行绘制)
    try:
        m = satellite.moon_position()
        s = satellite.sun_position()
        frame["cel"] = {
            "moon": {"sub_lat": m.get("sub_lat"), "sub_lon": m.get("sub_lon"), "visible": m.get("visible")} if m else None,
            "sun": {"sub_lat": s.get("sub_lat"), "sub_lon": s.get("sub_lon"), "visible": s.get("visible")} if s else None,
        }
    except Exception:  # noqa: BLE001
        frame["cel"] = None
    # SkyRoof / rotctld 客户端连接状态 (前端顶部指示灯)
    frame["rotctld_connected"] = bool(
        rotctld_srv is not None and rotctld_srv.connected
    )
    return frame


def _stream_serial():
    with _serial_log_lock:
        return {"log": list(_serial_log)}


def _stream_favorites():
    return {"favorites": satellite.get_favorites()}


def _stream_photocalib():
    with _photo_calib_lock:
        return {"running": _photo_calib["running"], "phase": _photo_calib["phase"],
                "result": _photo_calib.get("result")}


@app.route("/api/stream")
def stream():
    """SSE 推流: state(1s) / serial(0.8s) / favorites(30s) / photocalib(1s)
    目标位置由 query 参数指定: norad=<编号> 或 celestial=moon|sun
    """
    norad = request.args.get("norad") or None
    celestial = request.args.get("celestial") or None
    return streaming.make_stream_response({
        "state": (lambda: _stream_state(norad, celestial), streaming.STATE_INTERVAL),
        "serial": (_stream_serial, streaming.SERIAL_INTERVAL),
        "favorites": (_stream_favorites, streaming.FAVORITES_INTERVAL),
        "photocalib": (_stream_photocalib, streaming.PHOTOCALIB_INTERVAL),
    })


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


# ---------- LoTW / VUCC ----------
@app.route("/api/lotw/fetch", methods=["POST"])
def lotw_fetch():
    """触发 LoTW 日志拉取 (后台线程, 前端轮询 /api/lotw 状态)"""
    data = request.get_json(silent=True) or {}
    callsign = str(data.get("callsign") or get_cfg("lotw_callsign") or "").strip()
    password = str(data.get("password") or get_cfg("lotw_password") or "")
    if not callsign or not password:
        return err("请先在设置中填写 LoTW 呼号与密码")
    if get_cfg("lotw_callsign") != callsign or get_cfg("lotw_password") != password:
        update_config({"lotw_callsign": callsign, "lotw_password": password})
    lotw.start_fetch(callsign, password)
    return ok({"state": lotw.get_status()["state"]})


@app.route("/api/lotw")
def lotw_status():
    """LoTW 拉取状态 + 缓存 VUCC 数据 (bands 按频段: 已确认网格列表)"""
    return ok({"status": lotw.get_status(), **lotw.get_vucc()})


# ---------- 暂停控制 ----------
@app.route("/api/pause", methods=["POST"])
def pause():
    """清空所有命令并初始化: 停止持续转动、停止卫星跟踪、发送停止帧、重置航位推算位置"""
    global _pre_track_busy_ts
    data = request.get_json(silent=True) or {}
    paused = bool(data.get("paused", True))
    if paused:
        # 1. 停止持续转动
        stop_hold()
        # 1.5 取消跟踪前俯仰归零线程 (否则其循环空转, 恢复后突然继续动)
        _pre_track_stop.set()
        _pre_track_busy_ts = 0.0  # 允许下次跟踪立即重新开始归零
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
    """返回未来一段时间内的星下点轨迹 (缓存版: 过期返回旧值并后台重算)"""
    hours = float(request.args.get("hours", 6))
    step = float(request.args.get("step", 60))
    points = satellite.compute_track_cached(norad, hours, step)
    now = time.time()
    return ok({"points": [p for p in points if p["t"] >= now]})


# 俯仰轴无编码器反馈, 开启跟踪前若俯仰不在 0°, 需先开环按时间降到 0°
# (仰角位置不可信, 归零是唯一可校准的起点), 完成后才启动跟踪线程
_pre_track_stop = threading.Event()   # 置位 = 取消进行中的归零
_pre_track_busy = False                # 归零线程是否在跑
_pre_track_busy_ts = 0.0               # 归零开始时间戳 (超时防残留用)
_pre_track_lock = threading.Lock()
PRETRACK_TIMEOUT = 40.0                # 归零最长容忍; 超时强制释放残留 busy (归零最长20s)


def _pre_track_set_busy(b: bool):
    """统一更新归零 busy 标志与时间戳 (必须在 _pre_track_lock 内调用)"""
    global _pre_track_busy, _pre_track_busy_ts
    _pre_track_busy = b
    _pre_track_busy_ts = time.time() if b else 0.0


@app.route("/api/sat/track/<norad>", methods=["POST"])
def sat_track_start(norad: str):
    global _pre_track_busy, _pre_track_busy_ts
    is_cel = (norad in ("moon", "sun"))   # 天体目标: 月球/太阳, 无需 TLE
    if not is_cel:
        satellite.fetch_tle()
        if satellite.get_tle(norad) is None:
            return err("卫星不存在或无 TLE 数据", 404)
    with _pre_track_lock:
        if _pre_track_busy:
            # 归零异常残留: 超时未释放则强拆, 否则永久"俯仰归零中"云台不转
            if time.time() - _pre_track_busy_ts > PRETRACK_TIMEOUT:
                _pre_track_stop.set()
                try: send(pelco.stop())
                except Exception:  # noqa: BLE001
                    pass
                _pre_track_set_busy(False)
                print("[track] 归零 busy 超时残留, 强制释放", flush=True)
            else:
                return err("俯仰归零中, 请稍候")
        _pre_track_set_busy(True)
    print(f"[track] start {norad} cur_tilt={tracker.tilt:.1f} is_resetting={is_resetting()}", flush=True)
    _pre_track_stop.clear()
    with tracker.lock:
        tracker._finalize()
        cur_tilt = tracker.tilt
    if cur_tilt <= 0.5:
        # 俯仰已在 0° 附近, 直接开始跟踪
        with _pre_track_lock:
            _pre_track_set_busy(False)
        sat_tracker.start(norad, target=norad if is_cel else "sat")
        return ok({"tracking": True, "norad": norad, "target": norad if is_cel else "sat"})
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
                sat_tracker.start(norad, target=norad if is_cel else "sat")
            with _pre_track_lock:
                _pre_track_set_busy(False)

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
               "norad": sat_tracker.norad_id,
               "target": getattr(sat_tracker, "target", "sat")})


@app.route("/api/moon/position", methods=["GET"])
def moon_pos():
    pos = satellite.moon_position()
    if pos is None:
        return err("月球位置计算失败", 500)
    return ok(pos)


@app.route("/api/sun/position", methods=["GET"])
def sun_pos():
    pos = satellite.sun_position()
    if pos is None:
        return err("太阳位置计算失败", 500)
    return ok(pos)


@app.route("/api/sat/radar/<norad>", methods=["GET"])
def sat_radar(norad: str):
    """雷达图迹线: 返回所选过境的完整 AOS→LOS 方位/仰角序列。

    默认自动选择: 卫星当前在过境 → 本次过境; 否则 → 下一次过境。
    前端可用 ?aos=..&los=.. 指定任意过境 (过境列表点击选择)。
    """
    step = float(request.args.get("step", 30))
    aos_q = request.args.get("aos")
    los_q = request.args.get("los")
    if aos_q and los_q:
        aos, los = float(aos_q), float(los_q)
        meta = {"aos": aos, "los": los, "aos_az": 0, "los_az": 0}
        # 尝试从过境缓存补齐该过境的方位/峰值元数据
        for p in satellite.compute_passes_cached(norad):
            if abs(p["aos"] - aos) < 5:
                meta = p
                break
    else:
        cp = satellite.current_pass_info(norad)
        if cp:
            aos, los, meta = cp["aos"], cp["los"], cp
        else:
            passes = satellite.compute_passes_cached(norad)
            if not passes:
                return ok({"points": [], "pass": None})
            meta = passes[0]
            aos, los = meta["aos"], meta["los"]
    points = satellite.compute_radar_pass(norad, aos, los, step)
    now = time.time()
    return ok({"points": [p for p in points if p["t"] >= now], "pass": meta})


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
                  "pan_time": 0.0, "rssi": None}  # tangle_warn: 防缠绕越界告警文本
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

# ---------- UDP 回传智能滤波 ----------
# 原理: 无 485 指令时角度不该变, 变了是噪声; 有指令时用速度/方向判断
ENC_FILT_STATIONARY_TOL = 8.0    # 静止容差: ESP32° (含传动比, 相当物理 2°)
ENC_FILT_SPEED_MARGIN = 1.5      # 运动速度上限系数 (最高转速 × 1.5)
ENC_FILT_DIR_TOL = 0.5           # 反向判定死区: 物理°/s, 低于此视作静止抖动
_last_filt_angle = None           # 上次通过滤波的 ESP32 累积角度
_last_filt_time = 0.0             # 上次通过滤波的时间戳
_last_send_time = 0.0             # 上次发送 485 指令的时间
_last_rejected = False             # 上一个 UDP 包是否被滤波丢弃
_rej_stable_cnt = 0                # 连续拒绝计数 (拒绝包自身稳定时递增, 用于漂移恢复)
_rej_stable_angle = None           # 上一个被拒包的角度
_rej_stable_t = 0.0                # 上一个被拒包的时间


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


def _enc_data_filter(angle, now):
    """智能滤波: 根据云台运动状态判断 UDP 回传数据是否可信。
    返回 True=接受, False=丢弃。
    
    原理: 静止时角度不该变; 转动时用方向/速度判断误码。
    """
    global _last_filt_angle, _last_filt_time
    if _last_filt_angle is None or _last_filt_time == 0:
        _last_filt_angle = angle; _last_filt_time = now
        return True
    delta_a = angle - _last_filt_angle
    delta_t = now - _last_filt_time
    if delta_t <= 0:
        return True
    # 绝对跳变上限: 单包 >60°ESP(15°物理) 物理不可能 (全速 0.05s 仅 ~1.5°物理),
    # 任何模式下都拒。堵住"俯仰移动时 delta_t 被拉长、大毛刺速度被摊薄洗白"的洞
    # (曾现: +88.94°ESP 毛刺在俯仰移动期间被接受, 冻结编码器基准致后续全拒)。
    if abs(delta_a) > 60.0:
        return False
    with tracker.lock:  # noqa: E701
        moving = tracker._moving
    if moving is None:
        # 静止: 角度变化超容差则丢弃
        if abs(delta_a) > ENC_FILT_STATIONARY_TOL:
            return False
        _last_filt_angle = angle; _last_filt_time = now
        return True
    # 转动: 检查速度和方向
    direction = moving[0]
    phys_v = delta_a / ENCODER_GEAR / delta_t  # 物理°/s
    max_v = get_cfg("pan_speed_dps") * ENC_FILT_SPEED_MARGIN
    if direction == "right" and phys_v < -ENC_FILT_DIR_TOL:
        return False  # 右转但角度反方向减
    if direction == "left" and phys_v > ENC_FILT_DIR_TOL:
        return False  # 左转但角度反方向增
    if abs(phys_v) > max_v:
        return False  # 超速
    _last_filt_angle = angle; _last_filt_time = now
    return True


# --- AS5600 振荡 360° unwrap 偏移自动修正 ---
_UNWRAP_TOL = 60.0
_UNWRAP_LAST_CORRECT = 0.0

def _detect_unwrap_error(angle, now):
    global _last_filt_angle, _last_filt_time, _UNWRAP_LAST_CORRECT
    if _last_filt_angle is None or not _encoder_cal.get("zero_angle"):
        return False
    if now - _UNWRAP_LAST_CORRECT < 5.0:
        return False
    zero = _encoder_cal["zero_angle"]
    jump = angle - _last_filt_angle
    for sign in (1, -1):
        if abs(jump - sign * 360.0) <= _UNWRAP_TOL:
            _encoder_cal["zero_angle"] = float(zero) + sign * 360.0
            _save_encoder_cal()
            _last_filt_angle = angle
            _last_filt_time = now
            _UNWRAP_LAST_CORRECT = now
            print(f"[filter] 检测到 {sign:+}360° unwrap 偏移, zero_angle 已修正: {zero:.2f} → {_encoder_cal['zero_angle']:.2f}", flush=True)
            return True
    return False


def _reanchor_zero(angle=None):
    """云台物理回到 0° 后调用: 以当前 ESP32 累积角度为新基准对齐 0 点
    消除复位/标定过程中 AS5600 累计漂移。angle=None 时取最新上报值。
    """
    global _last_pan_cont, _last_filt_angle, _last_filt_time
    if angle is None:
        with _encoder_lock:
            angle = _encoder_state.get("angle")
    if angle is None:
        return
    _encoder_cal["zero_angle"] = float(angle)
    _last_pan_cont = None
    # 重置智能滤波基准: 若最后接受角度与锚定值不一致 (毛刺冻结的旧基准),
    # 不重置会导致后续所有正常回传与基准差超容差被全拒 -> pan_time 冻结 ->
    # enc_ok=False -> 跟踪永远走开环 3s 停 ("复位后再跟踪不动"根因)
    _last_filt_angle = None
    _last_filt_time = 0.0
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
# 合成零位检测: 跟踪 pan 模 360 的跳变
_prev_syn_zero_pan = None


_load_encoder_cal()


_display_pan = None   # 平滑显示角 (EMA): 每个 UDP 包都更新但抑制抖动/毛刺, 供前端方向线/大数字

def _update_display_pan(pan, trusted=False):
    """显示角平滑更新: 被滤波接受/拒绝的包都调用, 但:
    - 360° 回绕感知 (delta 归一化到 ±180)
    - >15°物理 (60°ESP) 毛刺: 保持不动
    - trusted=True (滤波接受的可信角): alpha=0.8 直接收敛, 空闲时无抖动
    - trusted=False (被拒包/运动中原始包): 抖动带 (4~40°ESP) 用 alpha=0.12 强抑制;
      大位移 (>10°物理) 才快跟 alpha=0.5, 保证复位/转向时仍实时跟随
    """
    global _display_pan
    if pan is None:
        return
    if _display_pan is None:
        _display_pan = pan
    else:
        delta = (pan - _display_pan + 180.0) % 360.0 - 180.0
        if abs(delta) > 60.0:
            return   # 大毛刺 (单包被拒/干扰), 保持
        if trusted:
            if abs(delta) < 0.05:
                return
            _display_pan += delta * 0.8
        else:
            if abs(delta) < 0.5:
                return   # 微抖 (<0.13°物理), 不动
            a = 0.5 if abs(delta) > 40.0 else 0.12
            _display_pan += delta * a
    with _encoder_lock:
        _encoder_state["dpan"] = _display_pan


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
    # --- 智能滤波 ---
    # 未标定时不做滤波
    trusted_accept = True
    if pan is not None and not _enc_data_filter(angle, now):
        global _last_rejected
        global _rej_stable_cnt, _rej_stable_angle, _rej_stable_t
        _last_rejected = True
        # 漂移恢复: 连续被拒且被拒包自身稳定 (>0.25s 停在同一角度) -> 认定
        # 云台已滑到新位置 (电机干扰抖动后停转 / 旧基准被毛刺冻结), 采信当前
        # 角度为新基准恢复流动。防 pan_time 永久冻结 -> enc_ok=False -> 开环
        # 超时死循环 (曾现: 抖动后停在新位置, 与旧基准差 187°ESP 永久全拒)。
        # 单包毛刺因后续包回归原值, 拒绝包间不稳定, cnt 归零, 不会误接受。
        if (_rej_stable_angle is not None
                and abs(angle - _rej_stable_angle) < 2.0
                and now - _rej_stable_t < 0.6):
            _rej_stable_cnt += 1
        else:
            _rej_stable_cnt = 1
        _rej_stable_angle = angle
        _rej_stable_t = now
        if _rej_stable_cnt >= 5:
            _last_filt_angle = angle
            _last_filt_time = now
            _rej_stable_cnt = 0
            _rej_stable_angle = None
            _last_rejected = False
            trusted_accept = False   # 恢复值仍可能带噪声: 走慢速平滑, 不猛拉 dpan
            print(f"[filter] 连续稳定确认, 编码器基准恢复: {angle:.2f}", flush=True)
            # fall through 走下方接受路径更新位置
        else:
            # 尝试检测并修正 AS5600 振荡后的 ±360° unwrap 偏移 (偏 90° 根因)
            _detect_unwrap_error(angle, now)
            # 丢弃: 只更新时间 (保活), 显示角经平滑更新 (抑制抖动/毛刺, 防方向线乱指)
            with _encoder_lock:
                _encoder_state["time"] = now
            _update_display_pan(pan)
            return
    if pan is None:
        # 未标定 (zero_angle=None) 时 pan/cont_angle 无意义, 但 angle/raw 仍需上报
        with _encoder_lock:
            _encoder_state["time"] = now
            _encoder_state["angle"] = angle
            _encoder_state["raw"] = raw
            _encoder_state["cont_angle"] = None
            _encoder_state["pan"] = None
            _encoder_state["dpan"] = None
            _encoder_state["pan_time"] = 0.0
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
    _last_rejected = False
    with _encoder_lock:
        _encoder_state["time"] = now
        _encoder_state["angle"] = angle
        _encoder_state["raw"] = raw
        _encoder_state["cont_angle"] = cont_angle
        _encoder_state["pan"] = pan
        _encoder_state["pan_time"] = now
        # 防缠绕越界判定: 越界即告警, 提示需复位归中 (ESP32 域 off)
        warn = _tangle_warn(off)
        if warn != _encoder_state.get("tangle_warn"):
            if warn:
                print(f"[tangle] {warn}", flush=True)
            _encoder_state["tangle_warn"] = warn
    _update_display_pan(pan, trusted=trusted_accept)   # 接受路径: 正常接受=可信角直接收敛; 漂移恢复=慢速平滑
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
            dir = tracker._moving[0] if tracker._moving else None
            if dir == "right":
                # 顺时针经过: 方向与复位/校准一致, 触发角度确定, 可安全锚定
                _reanchor_zero(payload.get("angle"))
            else:
                print(f"[encoder] 跟踪中逆时针经过光电零位 angle={payload.get('angle')}, 忽略 (挡片方向差)", flush=True)
            return
        # 手动/空闲状态经过光电零位: 不锚定 0 点。
        return
    _process_encoder_data(payload.get("angle"), payload.get("raw"))
    # 合成零位: 编码器离线或 event 丢失时, 通过 pan 绕 360→0 判断物理过零
    global _prev_syn_zero_pan
    pan_now = _encoder_state.get("pan")
    if pan_now is not None:
        if _prev_syn_zero_pan is not None and _prev_syn_zero_pan > 350 and pan_now < 10:
            if time.time() - _zero_last_t > 5.0:  # 5s 内无真实光电事件才触发
                if sat_tracker.is_tracking():
                    dir = tracker._moving[0] if tracker._moving else None
                    if dir == "right":
                        with _encoder_lock:
                            ang = _encoder_state.get("angle")
                        if ang is not None:
                            print(f"[syn_zero] 合成零位 pan={_prev_syn_zero_pan:.1f}→{pan_now:.1f} angle={ang:.1f}", flush=True)
                            _reanchor_zero(ang)
        _prev_syn_zero_pan = pan_now


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
    st["now"] = time.time()
    st["rejected"] = _last_rejected
    with tracker.lock:
        st["tilt"] = tracker.tilt   # 俯仰航位推算角, 供前端 250ms 快轮询实时显示
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
    """手动设置当前位置为物理 0° 基准角"""
    global _last_pan_cont, _display_pan
    with _encoder_lock:
        angle = _encoder_state.get("angle")
    if angle is None:
        pan_est, _ = tracker._estimate()
        cur_zero = _encoder_cal.get("zero_angle", 0.0)
        angle = pan_est * ENCODER_GEAR + cur_zero
        msg = f"已设置当前位置为 0° 基准 (编码器离线, 用估算角度 {pan_est:.1f}° 推算)"
    else:
        msg = f"已设置当前位置为 0° 基准 (angle={angle:.2f})"
    _reanchor_zero(float(angle))
    # 设零即当前位置=0°: 立即更新显示状态, 无 UDP 回传时 pan 也能马上归 0
    # (此前仅更新 zero_angle, pan 靠下一包 UDP 才刷新, 离线时永不更新)
    with _encoder_lock:
        _encoder_state["pan"] = 0.0
        _encoder_state["dpan"] = 0.0
        _encoder_state["cont_angle"] = 0.0
        _encoder_state["pan_time"] = time.time()
    _display_pan = 0.0
    with tracker.lock:
        tracker.pan = 0.0
        tracker._pan_base = 0.0
    _last_pan_cont = 0.0
    return ok({"zero_angle": round(float(angle), 2), "msg": msg})


if __name__ == "__main__":

    def _ws_frame_factory(target: dict):
        """WebSocket 推送帧表, 与 SSE 各帧完全对齐"""
        norad = target.get("norad") if target else None
        celestial = target.get("celestial") if target else None
        return [
            (streaming.STATE_INTERVAL, "state",
             lambda n=norad, c=celestial: _stream_state(n, c)),
            (streaming.SERIAL_INTERVAL, "serial", _stream_serial),
            (streaming.FAVORITES_INTERVAL, "favorites", _stream_favorites),
            (streaming.PHOTOCALIB_INTERVAL, "photocalib", _stream_photocalib),
        ]

    satellite.start_background_jobs()  # 后台: TLE 6h / 过境 10min
    # 后台预热地图瓦片到服务器: 外网/内网访问时从 J1900 分发, 无需回源天地图
    threading.Thread(target=_prewarm_tiles, daemon=True, name="tdt-prewarm").start()
    port = int(os.getenv("PTZ_PORT", "8090"))
    # 启动 rotctld 桥接服务器 (SkyRoof / Hamlib 客户端, 默认 4533)
    rotctld_srv = rotctld_server.RotctldServer(
        _rotck_get_position, _rotck_set_position, _rotck_stop_motion,
        host="0.0.0.0", port=int(os.getenv("ROTCTLD_PORT", "4533")),
    )
    rotctld_srv.start()
    # 启动 WebSocket 实时推送 (默认 8092; 与 waitress 并存, 前端 WS 优先/SSE 降级)
    ws_server.start(int(os.getenv("PTZ_WS_PORT", "8092")), _ws_frame_factory, _ws_check_auth)
    print(f"YD3040 云台控制服务启动: http://0.0.0.0:{port}")
    serve(app, host="0.0.0.0", port=port, threads=16)  # SSE 长连接各占 1 线程, 需留余量
