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
    session["user"] = user
    return ok(user)


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return ok({"logged_out": True})


@app.route("/api/auth/me")
def auth_me():
    user = session.get("user")
    if user is None:
        return err("未登录", 401)
    return ok(user)
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
            self.pan_accum -= pan_delta
        if "right" in direction:
            pan += pan_delta
            self.pan_accum += pan_delta
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
    """复位: 水平轴用 AS5600 闭环走最短路径回 0°, 俯仰轴按时长回 0°
    每 100ms 读取 AS5600 实时 pan, 按最短路径方向转动, 到位即停
    """
    start = time.time()
    pan_done = False
    tilt_done = False
    while not (pan_done and tilt_done):
        elapsed = time.time() - start
        tilt_done = elapsed >= tilt_s
        if not pan_done:
            pan = _sat_get_position()[0]
            daz = _pan_delta(0.0, pan)
            if abs(daz) <= 0.5:
                pan_done = True
        if pan_done and tilt_done:
            break
        try:
            if not pan_done and not tilt_done:
                send(pelco.up_right() if daz > 0 else pelco.up_left())
            elif not pan_done:
                send(pelco.right() if daz > 0 else pelco.left())
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
        tracker.pan_accum = 0.0  # 复位后清零累计


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
        return ok({"port": SERIAL_PORT, "baud": BAUD_RATE,
                   "address": PTZ_ADDRESS, "open": ser.is_open,
                   "paused": is_paused(),
                   "resetting": is_resetting(),
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
    duration = abs(accum) / pan_speed + 1.0  # 加 1s 冗余补偿启停
    set_resetting(True)

    def _run():
        try:
            tracker.start_move(direction)
            start_t = time.time()
            while time.time() - start_t < duration:
                try:
                    send(_MOVE_CMDS[direction]())
                except Exception:  # noqa: BLE001
                    break
                time.sleep(0.1)
            tracker.stop()
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
_encoder_state = {"time": 0.0, "angle": None, "raw": None, "pan": None}
_encoder_lock = threading.Lock()

ENCODER_CAL_FILE = os.path.join(BASE_DIR, "encoder_cal.json")
# 两点线性标定: as5600_angle -> pan
# points: [{"as5600_angle": 0, "pan": 0}, {"as5600_angle": 720, "pan": 360}]
_encoder_cal = {"points": []}


def _load_encoder_cal():
    global _encoder_cal
    try:
        if os.path.exists(ENCODER_CAL_FILE):
            with open(ENCODER_CAL_FILE, "r", encoding="utf-8") as f:
                _encoder_cal = json.load(f)
    except Exception:  # noqa: BLE001
        _encoder_cal = {"points": []}


def _save_encoder_cal():
    try:
        with open(ENCODER_CAL_FILE, "w", encoding="utf-8") as f:
            json.dump(_encoder_cal, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        print(f"[encoder_cal] save error: {e!r}", flush=True)


def _encoder_angle_to_pan(angle):
    """根据标定点把 AS5600 角度映射为云台 pan (0~360° 连续)
    - 0 点: 1:1 透传
    - 1 点: 平移 (offset) 映射
    - >=2 点: 线性比例映射
    """
    points = _encoder_cal.get("points", [])
    if not points:
        return float(angle) % 360.0
    pts = sorted(points, key=lambda x: x["as5600_angle"])
    if len(pts) == 1:
        a0, p0 = pts[0]["as5600_angle"], pts[0]["pan"]
        return (float(angle) - a0 + p0) % 360.0
    a0, p0 = pts[0]["as5600_angle"], pts[0]["pan"]
    a1, p1 = pts[-1]["as5600_angle"], pts[-1]["pan"]
    if a1 == a0:
        return p0 % 360.0
    # 线性映射后取模 (支持 as5600 多圈对应云台一圈)
    pan = p0 + (float(angle) - a0) * (p1 - p0) / (a1 - a0)
    return pan % 360.0


_load_encoder_cal()


def _process_encoder_data(angle, raw):
    """处理 AS5600 编码器数据, 更新水平轴位置 (HTTP POST 和 UDP 共用)"""
    now = time.time()
    pan = None
    if angle is not None:
        try:
            angle = float(angle)
            pan = _encoder_angle_to_pan(angle)
        except (ValueError, TypeError):
            pass
    if pan is None and raw is not None:
        try:
            angle = int(raw) * 360.0 / 4096.0
            pan = _encoder_angle_to_pan(angle)
        except (ValueError, TypeError):
            pass
    if pan is None:
        return
    with _encoder_lock:
        _encoder_state["time"] = now
        _encoder_state["angle"] = angle
        _encoder_state["raw"] = raw
        _encoder_state["pan"] = pan
    with tracker.lock:
        if tracker._moving is not None:
            tracker._pan_base = pan
            tracker._moving = (tracker._moving[0], now)
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
    """接收 AS5600 磁编码器数据 (HTTP POST 兼容保留)"""
    data = request.get_json(silent=True) or {}
    angle = data.get("angle")
    raw = data.get("raw")
    pan = None
    if angle is not None:
        try:
            angle = float(angle)
            pan = _encoder_angle_to_pan(angle)
        except (ValueError, TypeError):
            pass
    if pan is None and raw is not None:
        try:
            angle = int(raw) * 360.0 / 4096.0
            pan = _encoder_angle_to_pan(angle)
        except (ValueError, TypeError):
            pass
    if pan is None:
        return err("缺少有效的 angle 或 raw")
    _process_encoder_data(angle, raw)
    return ok({"angle": angle, "raw": raw, "pan": pan})


@app.route("/api/encoder", methods=["GET"])
def encoder_get():
    with _encoder_lock:
        return ok(dict(_encoder_state))


@app.route("/api/encoder/calibrate", methods=["GET", "POST"])
def encoder_calibrate():
    """标定: 记录 AS5600 角度与云台 pan 的两点对应关系"""
    global _encoder_cal
    if request.method == "GET":
        with _encoder_lock:
            latest = dict(_encoder_state)
        return ok({"cal": _encoder_cal, "latest": latest})

    data = request.get_json(silent=True) or {}

    # 直接设置标定点: [{"as5600_angle": 0, "pan": 0}, ...] (1 点=仅偏移, 2 点=比例)
    points = data.get("points")
    if points is not None:
        try:
            pts = [{"as5600_angle": float(p["as5600_angle"]),
                    "pan": float(p["pan"])} for p in points]
            if not pts:
                return err("标定点不能为空")
            _encoder_cal["points"] = pts
            _save_encoder_cal()
            with _encoder_lock:
                angle = _encoder_state.get("angle")
            # 标定后清零防缠绕累计
            with tracker.lock:
                tracker._finalize()
                tracker.pan_accum = 0.0
            return ok({"cal": _encoder_cal,
                       "mapped_pan": _encoder_angle_to_pan(angle) if angle is not None else None})
        except (KeyError, ValueError, TypeError):
            return err("points 格式错误, 需要 [{as5600_angle, pan}, ...]")

    # 单点记录: 用当前 AS5600 角度, 标定为指定 pan
    pan = data.get("pan")
    if pan is not None:
        try:
            pan = float(pan)
        except (ValueError, TypeError):
            return err("pan 格式错误")
        with _encoder_lock:
            angle = _encoder_state.get("angle")
        if angle is None:
            return err("尚未收到 AS5600 数据, 无法标定")
        _encoder_cal["points"].append({"as5600_angle": angle, "pan": pan})
        _save_encoder_cal()
        # 标定后清零防缠绕累计
        with tracker.lock:
            tracker._finalize()
            tracker.pan_accum = 0.0
        return ok({"cal": _encoder_cal, "mapped_pan": _encoder_angle_to_pan(angle)})

    return err("请提供 points 或 pan")


if __name__ == "__main__":
    satellite.start_background_jobs()  # 后台: TLE 6h / 过境 10min
    port = int(os.getenv("PTZ_PORT", "8090"))
    print(f"YD3040 云台控制服务启动: http://0.0.0.0:{port}")
    serve(app, host="0.0.0.0", port=port)
