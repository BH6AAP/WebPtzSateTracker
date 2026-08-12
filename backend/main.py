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
        self.pan = max(get_cfg("pan_min"), min(get_cfg("pan_max"), self.pan))
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
        pan = max(get_cfg("pan_min"), min(get_cfg("pan_max"), pan))
        tilt = max(get_cfg("tilt_min"), min(get_cfg("tilt_max"), tilt))
        return pan, tilt

    def at_limit(self, direction: str) -> bool:
        """判断某方向是否已达限位 (不结算运动)"""
        with self.lock:
            pan, tilt = self._estimate()
            eps = 0.01
            if "left" in direction and pan <= get_cfg("pan_min") + eps:
                return True
            if "right" in direction and pan >= get_cfg("pan_max") - eps:
                return True
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


def _reset_loop(tilt_s: float):
    """复位按钮: 水平轴按最短路径回 0° (AS5600 闭环), 俯仰轴按时长回 0°
    防缠绕解缠由"防缠绕复位"按钮负责, 复位仅负责回 0 点。
    每 100ms 读取实时反馈, 到位即停; 加超时兜底防止反馈失效时无限转动。
    """
    pan, _ = _sat_get_position()
    daz0 = _pan_delta(0.0, pan)
    pan_deadline = time.time() + max(30.0, abs(daz0) / get_cfg("pan_speed_dps") + 10)
    start = time.time()
    pan_done = False
    tilt_done = False
    pan_dir = "left"
    while not (pan_done and tilt_done):
        if time.time() > pan_deadline:
            pan_done = True
        elapsed = time.time() - start
        tilt_done = elapsed >= tilt_s
        if not pan_done:
            pan = _sat_get_position()[0]
            daz = _pan_delta(0.0, pan)
            if abs(daz) <= 0.5:
                pan_done = True
            else:
                pan_dir = "left" if daz < 0 else "right"
        if pan_done and tilt_done:
            break
        try:
            if not pan_done and not tilt_done:
                send(pelco.up_left() if pan_dir == "left" else pelco.up_right())
            elif not pan_done:
                send(pelco.left() if pan_dir == "left" else pelco.right())
            elif not tilt_done:
                send(pelco.up())
        except Exception:  # noqa: BLE001
            break
        time.sleep(0.1)
    try:
        send(pelco.stop())
    except Exception:  # noqa: BLE001
        pass
    with tracker.lock:
        tracker.pan = 0.0
        tracker.tilt = 0.0
        tracker._pan_base, tracker._tilt_base = 0.0, 0.0
        tracker.pan_accum = 0.0  # 复位后清零累计 (重新开始记录)
    _reanchor_zero()  # AS5600 以当前 raw 为新基准, 0 点对齐 (消除累计漂移)


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
    # 起始连续 pan (raw 域闭环实测, 未标定返回 None)
    with _encoder_lock:
        start_cont = _encoder_state.get("cont_raw")
    start_pan = _pan_cont_from_raw(start_cont) if start_cont is not None else None
    # 按固定时长转动 (复用持续转动机制, 与其他控制互斥)
    start_hold(direction, check_limit=False)
    time.sleep(ms / 1000.0)
    stop_hold()
    # 结束连续 pan, 计算有向增量 (右转正, 左转负)
    delta = None
    with _encoder_lock:
        end_cont = _encoder_state.get("cont_raw")
    if start_pan is not None and end_cont is not None:
        d = _pan_cont_from_raw(end_cont) - start_pan
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
    # 清理任务区命令队列: 停止持续转动、停止卫星跟踪、发送停止帧
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
    # 水平轴由 AS5600 闭环回 0° (无时间设置); 俯仰轴按时间复位回 0°
    with tracker.lock:
        tracker._finalize()
        cur_tilt = tracker.tilt
    tilt_s = max(0.1, (cur_tilt - get_cfg("tilt_min")) / get_cfg("tilt_speed_dps")) + 3
    # 兜底: 取配置的俯仰复位时长与计算值较大者
    tilt_s = max(tilt_s, get_cfg("reset_tilt_s"))

    def _run():
        try:
            _reset_loop(tilt_s)
        except Exception:  # noqa: BLE001
            pass
        finally:
            set_resetting(False)
    threading.Thread(target=_run, daemon=True).start()
    return ok({"detail": "复位指令已发送", "duration": round(tilt_s, 1)})


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
        accum = tracker.pan_accum
    if abs(accum) < 1.0:
        return ok({"detail": "无需防缠绕复位", "accum": 0, "duration": 0})
    direction = "left" if accum > 0 else "right"
    pan_speed = get_cfg("pan_speed_dps")
    duration = abs(accum) / pan_speed + 1.0  # 估算时长 (仅用于前端按钮恢复)
    set_resetting(True)

    def _run():
        try:
            # 用 AS5600 反馈闭环回退: 持续反向转动, 直到累计归零 (回到无缠绕起点)
            # 比时间估算精确, 且能正确处理多圈缠绕
            timeout = time.time() + max(30.0, abs(accum) / pan_speed * 2 + 5)
            while time.time() < timeout:
                with tracker.lock:
                    remaining = tracker.pan_accum
                if abs(remaining) < 1.0:
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
            _reanchor_zero()  # 回到 0° 起点, AS5600 基准对齐
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


@app.route("/api/sat/track/<norad>", methods=["POST"])
def sat_track_start(norad: str):
    satellite.fetch_tle()
    if satellite.get_tle(norad) is None:
        return err("卫星不存在或无 TLE 数据", 404)
    sat_tracker.start(norad)
    return ok({"tracking": True, "norad": norad})


@app.route("/api/sat/track/stop", methods=["POST"])
def sat_track_stop():
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
_encoder_state = {"time": 0.0, "angle": None, "raw": None, "pan": None, "cont_raw": None}
_encoder_lock = threading.Lock()

ENCODER_CAL_FILE = os.path.join(BASE_DIR, "encoder_cal.json")


class As5600Raw:
    """AS5600 原始计数(0~4095)多圈跟踪器
    全部在 raw 整数域计算, 避免角度浮点累计误差。
    - cont_raw = 圈数*4096 + raw, 跨 4095/0 线自动 ±4096
    - deadzone: 静止死区, 小于该值增量视为抖动不累计 (防随机游走漂移)
    - 动态死区: 运动期间(moving=True)死区降至 2 raw, 低速/短脉冲运动不丢失;
      静止时保持 15 raw 滤除传感器抖动
    """
    RAW_PER_REV = 4096

    def __init__(self, deadzone: int = 15):
        self.cont_raw = None
        self._last_raw = None
        self._deadzone = deadzone

    def update(self, raw: int, moving: bool = False) -> int:
        raw = int(raw) & 0x0FFF
        if self._last_raw is None:
            self.cont_raw = raw
        else:
            d = raw - self._last_raw
            if d > 2048:
                d -= self.RAW_PER_REV
            elif d < -2048:
                d += self.RAW_PER_REV
            dz = 2 if moving else self._deadzone
            if abs(d) < dz:
                d = 0
            self.cont_raw += d
        self._last_raw = raw
        return self.cont_raw

    def reset(self):
        """重置多圈基准: 下一次 update 以当前 raw 重新开始计数"""
        self.cont_raw = None
        self._last_raw = None


_as5600 = As5600Raw()

# raw 标定: raw_per_deg 每度raw数(持久化, 物理传动属性); origin 零点 cont_raw(内存)
# 旧格式 points 仅作角度兜底, 不再用于新计算
_encoder_cal = {"raw_per_deg": None}
_cal_origin = None      # 0 点对应的 cont_raw (设0点/复位时更新)
_cal_points = []        # 多点标定记录: [{"angle": 90, "cont": ...}, ...] (内存)


def _load_encoder_cal():
    global _encoder_cal
    _encoder_cal = {"raw_per_deg": None}
    try:
        if os.path.exists(ENCODER_CAL_FILE):
            with open(ENCODER_CAL_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, dict) and d.get("raw_per_deg"):
                    _encoder_cal["raw_per_deg"] = float(d["raw_per_deg"])
    except Exception:  # noqa: BLE001
        _encoder_cal = {"raw_per_deg": None}


def _save_encoder_cal():
    try:
        with open(ENCODER_CAL_FILE, "w", encoding="utf-8") as f:
            json.dump(_encoder_cal, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        print(f"[encoder_cal] save error: {e!r}", flush=True)


def _pan_from_cont_raw(cont_raw):
    """连续 raw -> 云台 pan (0~360°), 未标定返回 None"""
    rpd = _encoder_cal.get("raw_per_deg")
    if not rpd or _cal_origin is None or cont_raw is None:
        return None
    return ((cont_raw - _cal_origin) / rpd) % 360.0


def _pan_cont_from_raw(cont_raw):
    """连续 raw -> 连续 pan (不取模, 防缠绕累计用), 未标定返回 None"""
    rpd = _encoder_cal.get("raw_per_deg")
    if not rpd or _cal_origin is None or cont_raw is None:
        return None
    return (cont_raw - _cal_origin) / rpd


def _reanchor_zero():
    """云台物理回到 0° 后调用: 以当前 raw 为新基准重新计数并对齐 0 点
    消除复位/标定过程中 AS5600 多圈累计的漂移。
    """
    global _cal_origin, _last_pan_cont
    with _encoder_lock:
        raw = _encoder_state.get("raw")
    _as5600.reset()
    _cal_origin = raw
    _last_pan_cont = None


# 上一次连续 pan (raw 域), 用于增量累计防缠绕
_last_pan_cont = None
# 最近一次云台运动结束时刻, 用于停驶惯性滑行段保持运动死区 (吞掉滑行量会累积超前)
_enc_motion_end = 0.0


def _hold_moving() -> bool:
    """持续转动/点动脉冲/自动标定转动是否进行中 (UDP 线程只读, GIL 下安全)
    自动标定转动是直发指令, 不经 start_hold, 需单独纳入运动判定,
    否则静止死区会吞掉 100Hz 下每帧 3.5 raw 的增量, 导致 cont_raw 不累计"""
    if tracker._moving is not None:
        return True
    try:
        return _auto_calib["phase"] == "turning"
    except NameError:
        return False


_load_encoder_cal()


def _process_encoder_data(angle, raw):
    """处理 AS5600 编码器数据, 更新水平轴位置 (HTTP POST 和 UDP 共用)
    raw 域: _as5600 多圈展开 -> 连续 raw -> 标定映射 pan / 连续 pan (防缠绕增量)
    angle 仅作兜底 (固件 unwrap 连续角度, 未标定或 raw 缺失时用)。
    """
    global _last_pan_cont, _enc_motion_end
    now = time.time()
    pan = None
    cont_raw = None
    # 动态死区: 云台运动期间死区降至 2 raw (低速/短脉冲运动不丢失)
    moving = (sat_tracker.is_tracking()
              or is_resetting()
              or _hold_moving())
    if moving:
        _enc_motion_end = now
    else:
        # 停止后 1s 内仍用运动死区: 电机断电后惯性滑行段增量渐减,
        # 若立即切回静止死区 15 raw 会吞掉尾段滑行量, 导致每次步进
        # AS5600 显示比物理少 0.4~1°, 跟踪中反复补差形成累积超前
        moving = (now - _enc_motion_end) < 1.0
    if raw is not None:
        try:
            cont_raw = _as5600.update(raw, moving=moving)
            pan = _pan_from_cont_raw(cont_raw)
        except (ValueError, TypeError):
            pass
    if pan is None and angle is not None:
        try:
            angle = float(angle)
            # 未标定或 raw 缺失时, 直接透传固件角度 (不折叠, 与旧行为一致)
            pan = float(angle) % 360.0
        except (ValueError, TypeError):
            pass
    if pan is None:
        return
    # 防缠绕累计: raw 域连续 pan 增量 (跨 4096/0 线已由多圈展开处理, 无跳变)
    if cont_raw is not None:
        cont_pan = _pan_cont_from_raw(cont_raw)
        if cont_pan is not None:
            if _last_pan_cont is not None:
                delta = cont_pan - _last_pan_cont
                # 异常跳变(复位/标定瞬间) 视为 0, 不污染累计
                if abs(delta) <= 180.0:
                    with tracker.lock:
                        tracker.pan_accum += delta
            _last_pan_cont = cont_pan
    with _encoder_lock:
        _encoder_state["time"] = now
        _encoder_state["angle"] = angle
        _encoder_state["raw"] = raw
        _encoder_state["cont_raw"] = cont_raw
        _encoder_state["pan"] = pan
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


# ---------- UDP 监听 (AS5600 编码器数据) ----------
def _udp_encoder_listener():
    """监听 UDP 广播, 接收 ESP32 AS5600 编码器数据"""
    import socket as _socket
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 8091))
    print("[udp] AS5600 监听已启动: 0.0.0.0:8091", flush=True)
    while True:
        try:
            data, addr = sock.recvfrom(1024)
            payload = json.loads(data.decode("utf-8"))
            _process_encoder_data(payload.get("angle"), payload.get("raw"))
        except Exception:  # noqa: BLE001
            pass


threading.Thread(target=_udp_encoder_listener, daemon=True).start()


@app.route("/api/encoder", methods=["POST"])
def encoder_post():
    """接收 AS5600 磁编码器数据 (HTTP POST 兼容保留, UDP 为主)"""
    data = request.get_json(silent=True) or {}
    raw = data.get("raw")
    angle = data.get("angle")
    if raw is None and angle is None:
        return err("缺少 raw 或 angle")
    _process_encoder_data(angle, raw)
    with _encoder_lock:
        st = dict(_encoder_state)
    return ok({"raw": raw, "angle": angle,
               "pan": st.get("pan"), "cont_raw": st.get("cont_raw")})


@app.route("/api/encoder", methods=["GET"])
def encoder_get():
    with _encoder_lock:
        return ok(dict(_encoder_state))


@app.route("/api/encoder/calibrate", methods=["GET", "POST"])
def encoder_calibrate():
    """AS5600 raw 域多点标定:
    1. set_origin : 云台对准 0°, 记录当前 cont_raw 为 0 点
    2. add_point  : 转到 90°/180°/270° 等刻度, 逐个记录 {angle, cont_raw}
    3. finish     : 用全部标定点对原点最小二乘拟合 raw_per_deg (消除单点误差)
    完成后 raw_per_deg 持久化, 后期计算全在 raw 整数域进行。
    """
    global _cal_origin, _cal_points, _last_pan_cont
    if request.method == "GET":
        with _encoder_lock:
            latest = dict(_encoder_state)
        rpd = _encoder_cal.get("raw_per_deg")
        return ok({"cal": _encoder_cal, "latest": latest,
                   "origin": _cal_origin, "points": list(_cal_points),
                   "ratio": (rpd / 4096.0) if rpd else None,
                   "pan": _pan_from_cont_raw(latest.get("cont_raw")),
                   "pan_cont": _pan_cont_from_raw(latest.get("cont_raw"))})

    data = request.get_json(silent=True) or {}
    action = data.get("action")
    if action not in ("set_origin", "add_point", "finish"):
        return err("action 需为 set_origin / add_point / finish")

    def _cur_cont_raw():
        with _encoder_lock:
            return _encoder_state.get("cont_raw")

    if action == "set_origin":
        cont = _cur_cont_raw()
        if cont is None:
            return err("尚未收到 AS5600 raw 数据, 请先确认 UDP 数据流")
        _cal_origin = cont
        _cal_points = []
        with tracker.lock:
            tracker._finalize()
            tracker.pan_accum = 0.0
        _last_pan_cont = None
        return ok({"origin": _cal_origin, "pan": _pan_from_cont_raw(cont)})

    if action == "add_point":
        if _cal_origin is None:
            return err("请先设 0 点 (set_origin)")
        cont = _cur_cont_raw()
        if cont is None:
            return err("尚未收到 AS5600 raw 数据, 请先确认 UDP 数据流")
        try:
            angle_deg = float(data.get("angle_deg"))
        except (ValueError, TypeError):
            return err("angle_deg 格式错误")
        if not (0 < angle_deg < 360):
            return err("angle_deg 需在 (0, 360) 之间")
        # 同角度覆盖旧点 (允许重复记录覆盖)
        _cal_points = [p for p in _cal_points if abs(p["angle"] - angle_deg) > 0.5]
        _cal_points.append({"angle": angle_deg, "cont": cont})
        _cal_points.sort(key=lambda p: p["angle"])
        return ok({"points": list(_cal_points),
                   "count": len(_cal_points),
                   "angle": angle_deg, "cont": cont})

    # finish: 带截距最小二乘拟合 raw_per_deg 与 0 点 (不再硬锚定用户设的 0 点,
    # 由全部点共同确定, 自动修正 0 点对准误差与比例误差)
    if _cal_origin is None:
        return err("请先设 0 点 (set_origin)")
    if len(_cal_points) < 2:
        return err(f"标定点不足 ({len(_cal_points)}/2), 请至少记录 2 个点")
    pts = [(0.0, float(_cal_origin))] + [(float(p["angle"]), float(p["cont"])) for p in _cal_points]
    n = len(pts)
    sx = sum(a for a, _ in pts)
    sy = sum(c for _, c in pts)
    sxx = sum(a * a for a, _ in pts)
    sxy = sum(a * c for a, c in pts)
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-9:
        return err("标定点数据无效 (请确保角度各不相同)")
    rpd = (n * sxy - sx * sy) / denom
    origin_new = (sy - rpd * sx) / n
    _cal_origin = origin_new
    _encoder_cal["raw_per_deg"] = rpd
    _save_encoder_cal()
    with tracker.lock:
        tracker._finalize()
        tracker.pan_accum = 0.0
    _last_pan_cont = None
    # 各点残差 (用于前端展示标定质量)
    residuals = [round(p["cont"] - _cal_origin - rpd * p["angle"], 1) for p in _cal_points]
    return ok({"cal": _encoder_cal, "origin": _cal_origin,
               "points": list(_cal_points),
               "raw_per_deg": rpd, "ratio": rpd / 4096.0,
               "residuals": residuals,
               "pan": _pan_from_cont_raw(_cur_cont_raw())})


# ---------- 自动转一圈标定 (起终点物理 0° 锚定, 消除人工对准误差) ----------
_auto_calib_lock = threading.Lock()
_auto_calib = {
    "running": False,   # 后台转动线程是否在跑
    "phase": "idle",    # idle / turning / awaiting_confirm / done
    "t0": 0.0,          # 转动开始时刻
    "c0": None,         # 起点 cont_raw (设 0 点后)
    "samples": [],      # [(elapsed, cont_raw)] 转动采样
    "stop_cmd": None,   # 后台线程停止 Event
}


def _read_cont_raw():
    with _encoder_lock:
        return _encoder_state.get("cont_raw")


def _auto_calib_worker():
    """后台: 开环右转约一圈, 期间采样 (elapsed, cont_raw), 到时停止
    转动圈数以起终点物理 0° 为绝对锚点, 中途采样仅用于速度校准"""
    global _auto_calib
    with _auto_calib_lock:
        t0 = _auto_calib["t0"]
        c0 = _auto_calib["c0"]
        stop_ev = _auto_calib["stop_cmd"] = threading.Event()
        _auto_calib["phase"] = "turning"
    dur = 360.0 / get_cfg("pan_speed_dps") + 12.0  # 一圈时长 + 12s 兜底超时
    samples = []
    try:
        while not stop_ev.is_set():
            elapsed = time.time() - t0
            if elapsed >= dur:
                break
            try:
                send(pelco.right())
            except Exception:  # noqa: BLE001
                break
            cont = _read_cont_raw()
            samples.append((elapsed, cont))
            # AS5600 增量 ≥16384 raw (=AS5600 4圈≈云台一圈) 即停, 用户微调量最小
            if c0 is not None and cont is not None and abs(cont - c0) >= 16384:
                break
            stop_ev.wait(0.1)
        # 多次发 stop, 确保云台真正停止 (防止被残留指令覆盖)
        for _ in range(3):
            try:
                send(pelco.stop())
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.15)
    except Exception:  # noqa: BLE001
        try:
            send(pelco.stop())
        except Exception:  # noqa: BLE001
            pass
    # 等待云台停稳 (连续 3 帧增量 < 3 raw, 最多 5s) 再提示用户
    last = _read_cont_raw()
    settled = 0
    for _ in range(50):
        time.sleep(0.1)
        cur = _read_cont_raw()
        if last is not None and cur is not None and abs(cur - last) < 3:
            settled += 1
            if settled >= 3:
                break
        else:
            settled = 0
        last = cur
    with _auto_calib_lock:
        _auto_calib["samples"] = samples
        _auto_calib["running"] = False
        _auto_calib["phase"] = "awaiting_confirm"


@app.route("/api/encoder/autocalib", methods=["GET", "POST"])
def encoder_autocalib():
    """自动转一圈标定: start(开环右转一圈) -> 用户调回物理 0° -> confirm(计算 rpd + 校准速度)"""
    global _last_pan_cont
    if request.method == "GET":
        with _auto_calib_lock:
            ac = dict(_auto_calib)
        elapsed = 0.0
        if ac["t0"]:
            elapsed = round(time.time() - ac["t0"], 1)
        return ok({"status": ac["phase"], "running": ac["running"],
                   "elapsed": elapsed, "c0": ac["c0"]})

    data = request.get_json(silent=True) or {}
    action = data.get("action")
    if action == "start":
        if is_paused():
            return err("云台处于暂停状态, 请先恢复")
        with _auto_calib_lock:
            if _auto_calib["running"] or _auto_calib["phase"] == "turning":
                return err("自动标定正在进行中")
            if _cal_origin is None:
                return err("请先设 0 点 (set_origin)")
            c0 = _read_cont_raw()
            if c0 is None:
                return err("尚未收到 AS5600 raw 数据")
            _auto_calib["t0"] = time.time()
            _auto_calib["c0"] = c0
            _auto_calib["samples"] = []
            _auto_calib["phase"] = "turning"
            _auto_calib["running"] = True
        # 停止其他运动源, 防止并发驱动云台 (覆盖 stop)
        try:
            stop_hold()
        except Exception:  # noqa: BLE001
            pass
        try:
            sat_tracker.stop()
        except Exception:  # noqa: BLE001
            pass
        threading.Thread(target=_auto_calib_worker, daemon=True).start()
        return ok({"msg": "开始自动标定: 云台右转约一圈, 完成后请把云台调回物理 0° 再点确认"})

    if action == "confirm":
        with _auto_calib_lock:
            if _auto_calib["phase"] != "awaiting_confirm":
                return err("当前无待确认的自动标定 (请先 start)")
            c0 = _auto_calib["c0"]
            samples = list(_auto_calib["samples"])
        if c0 is None:
            return err("缺少起点数据")
        c1 = _read_cont_raw()
        if c1 is None:
            return err("尚未收到 AS5600 raw 数据")
        delta = c1 - c0
        if abs(delta) < 12000 or abs(delta) > 20000:
            return err(f"转过的 raw 量异常 ({delta}), 请确认云台已回到物理 0° 附近 (一圈约 ±16400)")
        rpd = delta / 360.0
        # 速度校准: 固定以 AS5600 4圈(=16384 raw, 物理一圈误差<0.1%)为目标,
        # 从采样找增量达到 16384 的时刻, 与用户微调量无关, 稳定
        T_cross = None
        target = 16384
        for i in range(1, len(samples)):
            d_prev = abs(samples[i - 1][1] - c0) if samples[i - 1][1] is not None else 0.0
            d_cur = abs(samples[i][1] - c0) if samples[i][1] is not None else 0.0
            if d_cur >= target:
                if d_cur != d_prev:
                    frac = (target - d_prev) / (d_cur - d_prev)
                    T_cross = samples[i - 1][0] + frac * (samples[i][0] - samples[i - 1][0])
                else:
                    T_cross = samples[i][0]
                break
        # 采样未达到 delta (用户手动微调过), 用自动转动段总时长近似
        if T_cross is None and samples:
            T_cross = samples[-1][0]
        result = {"raw_per_deg": rpd, "ratio": rpd / 4096.0}
        if T_cross and T_cross > 1.0:
            v_real = 360.0 / T_cross
            update_config({"pan_speed_dps": v_real})
            sat_tracker.pan_speed = v_real
            result["pan_speed_dps"] = v_real
            result["delta_raw"] = delta
        _encoder_cal["raw_per_deg"] = rpd
        _save_encoder_cal()
        with tracker.lock:
            tracker._finalize()
            tracker.pan_accum = 0.0
        _last_pan_cont = None
        with _auto_calib_lock:
            _auto_calib["phase"] = "done"
        return ok(result)

    return err("action 需为 start / confirm")


if __name__ == "__main__":
    satellite.start_background_jobs()  # 后台: TLE 6h / 过境 10min
    port = int(os.getenv("PTZ_PORT", "8090"))
    print(f"YD3040 云台控制服务启动: http://0.0.0.0:{port}")
    serve(app, host="0.0.0.0", port=port)
