"""
卫星跟踪模块
- TLE 获取与缓存 (AMSAT / CelesTrak)
- 卫星位置计算 (sgp4: TEME -> ECEF -> 站心方位角/仰角)
- 过境计算
- 云台自动跟踪
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone

from sgp4.api import Satrec, jday

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TLE_CACHE_FILE = os.path.join(BASE_DIR, "tle_cache.json")
FAVORITES_FILE = os.path.join(BASE_DIR, "sat_favorites.json")
OBSERVER_FILE = os.path.join(BASE_DIR, "sat_observer.json")

TLE_URLS = [
    "https://www.amsat.org/tle/current/daily-bulletin.txt",
    "https://celestrak.org/NORAD/elements/gp.php?GROUP=amateur&FORMAT=tle",
]

# 观测站 (由设置提供, 默认北京附近)
_observer = {"lat": 39.9, "lon": 116.4, "alt": 0.0}
_obs_lock = threading.Lock()

# TLE 缓存: norad_id -> {"name","line1","line2"}
_tle = {}
_tle_lock = threading.Lock()
_tle_fetch_time = 0.0
TLE_MAX_AGE = 6 * 3600  # TLE 每 6 小时更新一次


# ---------- 观测站 ----------
def maidenhead_to_latlon(grid: str):
    """梅登海得网格 -> (纬度, 经度), 取网格中心"""
    g = grid.strip().upper()
    if len(g) < 4:
        raise ValueError("网格格式错误")
    lon = -180 + (ord(g[0]) - ord('A')) * 20 + int(g[2]) * 2
    lat = -90 + (ord(g[1]) - ord('A')) * 10 + int(g[3]) * 1
    if len(g) >= 6:
        lon += (ord(g[4]) - ord('A')) * 5 / 60 + 2.5 / 60
        lat += (ord(g[5]) - ord('A')) * 2.5 / 60 + 1.25 / 60
    else:
        lon += 1.0
        lat += 0.5
    return round(lat, 4), round(lon, 4)


def set_observer(lat: float, lon: float, alt: float = 0.0):
    with _obs_lock:
        _observer["lat"] = float(lat)
        _observer["lon"] = float(lon)
        _observer["alt"] = float(alt)
    try:
        with open(OBSERVER_FILE, "w", encoding="utf-8") as f:
            json.dump(_observer, f)
    except Exception:  # noqa: BLE001
        pass


def get_observer() -> dict:
    with _obs_lock:
        return dict(_observer)


def _load_observer():
    try:
        if os.path.exists(OBSERVER_FILE):
            with open(OBSERVER_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                _observer.update({k: float(v) for k, v in saved.items() if k in _observer})
    except Exception:  # noqa: BLE001
        pass


_load_observer()


# ---------- TLE 获取与解析 ----------
def _parse_tle_text(text: str) -> dict:
    """解析 TLE 文本 (AMSAT 公告 / CelesTrak), 返回 norad_id -> {name,line1,line2}"""
    result = {}
    lines = text.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        if line.startswith("1 ") and i + 1 < n and lines[i + 1].strip().startswith("2 "):
            line1 = line
            line2 = lines[i + 1].strip()
            try:
                norad = line1[2:7].strip()
            except Exception:  # noqa: BLE001
                norad = ""
            name = ""
            if i > 0:
                prev = lines[i - 1].strip()
                if prev and not prev.startswith(("1 ", "2 ", "SB ", "QST ", "AMSAT ", "From ", "To ", "$ORB")):
                    name = prev
            if norad:
                result[norad] = {"name": name, "line1": line1, "line2": line2}
            i += 2
        else:
            i += 1
    return result


def _fetch_url(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_tle(force: bool = False) -> dict:
    """获取 TLE 数据并缓存到文件, 返回 norad_id -> {name,line1,line2}"""
    global _tle, _tle_fetch_time
    with _tle_lock:
        if not force and _tle and (time.time() - _tle_fetch_time) < TLE_MAX_AGE:
            return dict(_tle)
    merged = {}
    for url in TLE_URLS:
        try:
            text = _fetch_url(url)
            parsed = _parse_tle_text(text)
            if parsed:
                merged.update(parsed)
                break
        except Exception:  # noqa: BLE001
            continue
    if not merged:
        # 尝试从本地缓存加载
        try:
            if os.path.exists(TLE_CACHE_FILE):
                with open(TLE_CACHE_FILE, "r", encoding="utf-8") as f:
                    merged = json.load(f)
        except Exception:  # noqa: BLE001
            pass
    if merged:
        with _tle_lock:
            _tle = merged
            _tle_fetch_time = time.time()
        try:
            with open(TLE_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass
    return dict(merged)


def get_tle(norad_id: str) -> dict | None:
    with _tle_lock:
        return _tle.get(str(norad_id))


def get_all_tle() -> dict:
    with _tle_lock:
        return dict(_tle)


# ---------- 收藏卫星 ----------
_favorites = []
_fav_lock = threading.Lock()


def _load_favorites():
    global _favorites
    try:
        if os.path.exists(FAVORITES_FILE):
            with open(FAVORITES_FILE, "r", encoding="utf-8") as f:
                _favorites = json.load(f)
    except Exception:  # noqa: BLE001
        _favorites = []


def _save_favorites():
    try:
        with open(FAVORITES_FILE, "w", encoding="utf-8") as f:
            json.dump(_favorites, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


def get_favorites() -> list:
    with _fav_lock:
        favs = list(_favorites)
    now = time.time()
    enriched = []
    for f in favs:
        norad = str(f["norad"])
        item = dict(f)
        passes = compute_passes_cached(norad)  # 读后台缓存, 不现场计算
        pos = satellite_position(norad, now)
        cur_el = pos["elevation"] if pos else -90
        if passes:
            p = passes[0]
            item["next_aos"] = p["aos"]
            item["next_los"] = p["los"]
            item["max_el"] = round(p["max_el"], 1)
            item["aos_az"] = round(p["aos_az"], 1)
            item["los_az"] = round(p["los_az"], 1)
            if cur_el > 0:
                item["status"] = "in_pass"
                sort_key = 0  # 正在过境排最前
            else:
                item["status"] = "upcoming"
                sort_key = max(1, p["aos"] - now)
        else:
            item["status"] = "no_pass"
            item["next_aos"] = None
            item["next_los"] = None
            item["max_el"] = None
            item["aos_az"] = None
            item["los_az"] = None
            sort_key = 1e12
        enriched.append((sort_key, item))
    enriched.sort(key=lambda x: x[0])
    return [x[1] for x in enriched]


def add_favorite(norad_id: str) -> bool:
    norad_id = str(norad_id)
    tle = get_tle(norad_id)
    if tle is None:
        return False
    with _fav_lock:
        if norad_id not in [f["norad"] for f in _favorites]:
            _favorites.append({"norad": norad_id, "name": tle.get("name", "")})
            _save_favorites()
    return True


def remove_favorite(norad_id: str) -> bool:
    norad_id = str(norad_id)
    with _fav_lock:
        before = len(_favorites)
        _favorites = [f for f in _favorites if f["norad"] != norad_id]
        if len(_favorites) != before:
            _save_favorites()
            return True
    return False


_load_favorites()


# ---------- 坐标转换 ----------
def _gmst_deg(jd: float) -> float:
    """格林尼治平恒星时 (度)"""
    T = (jd - 2451545.0) / 36525.0
    g = 280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * T * T - T * T * T / 38710000.0
    return g % 360.0


def _geodetic_to_ecef(lat_deg: float, lon_deg: float, alt_km: float):
    """WGS84 大地坐标 -> ECEF (km)"""
    a = 6378.137
    f = 1.0 / 298.257223563
    e2 = f * (2 - f)
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    N = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    x = (N + alt_km) * math.cos(lat) * math.cos(lon)
    y = (N + alt_km) * math.cos(lat) * math.sin(lon)
    z = (N * (1 - e2) + alt_km) * math.sin(lat)
    return x, y, z


def _teme_to_ecef(r, jd: float):
    """TEME -> ECEF (绕 Z 轴旋转 GMST)"""
    theta = math.radians(_gmst_deg(jd))
    c, s = math.cos(theta), math.sin(theta)
    x, y, z = r
    return (x * c + y * s, -x * s + y * c, z)


def _ecef_to_azel(obs_ecef, sat_ecef, lat_deg: float, lon_deg: float):
    """ECEF 卫星坐标 -> 站心方位角/仰角"""
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    dx = sat_ecef[0] - obs_ecef[0]
    dy = sat_ecef[1] - obs_ecef[1]
    dz = sat_ecef[2] - obs_ecef[2]
    east = -math.sin(lon) * dx + math.cos(lon) * dy
    north = -math.sin(lat) * math.cos(lon) * dx - math.sin(lat) * math.sin(lon) * dy + math.cos(lat) * dz
    up = math.cos(lat) * math.cos(lon) * dx + math.cos(lat) * math.sin(lon) * dy + math.sin(lat) * dz
    az = (math.degrees(math.atan2(east, north))) % 360.0
    el = math.degrees(math.asin(up / math.sqrt(east * east + north * north + up * up)))
    return az, el


# ---------- 卫星位置计算 ----------
_satrec_cache = {}
_satrec_lock = threading.Lock()


def _get_satrec(norad_id: str) -> Satrec | None:
    tle = get_tle(norad_id)
    if not tle:
        fetch_tle()  # 首次调用时自动获取 TLE
        tle = get_tle(norad_id)
    if not tle:
        return None
    key = str(norad_id)
    with _satrec_lock:
        sat = _satrec_cache.get(key)
        if sat is None:
            try:
                sat = Satrec.twoline2rv(tle["line1"], tle["line2"])
                _satrec_cache[key] = sat
            except Exception:  # noqa: BLE001
                return None
        return sat


def satellite_position(norad_id: str, when: float | None = None):
    """计算卫星站心方位角/仰角/距离/星下点, 返回 dict 或 None"""
    sat = _get_satrec(norad_id)
    if sat is None:
        return None
    when = time.time() if when is None else when
    dt = datetime.fromtimestamp(when, tz=timezone.utc)
    jd, fr = jday(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second + dt.microsecond / 1e6)
    e, r, v = sat.sgp4(jd, fr)
    if e != 0:
        return None
    obs = get_observer()
    obs_ecef = _geodetic_to_ecef(obs["lat"], obs["lon"], obs["alt"] / 1000.0)
    sat_ecef = _teme_to_ecef(r, jd + fr)
    az, el = _ecef_to_azel(obs_ecef, sat_ecef, obs["lat"], obs["lon"])
    # 星下点 (卫星 ECEF -> 经纬度)
    slat = math.degrees(math.asin(sat_ecef[2] / math.sqrt(sat_ecef[0] ** 2 + sat_ecef[1] ** 2 + sat_ecef[2] ** 2)))
    slon = math.degrees(math.atan2(sat_ecef[1], sat_ecef[0]))
    dist = math.sqrt(sum(c * c for c in sat_ecef)) - 6378.137
    alt_km = max(0.0, (sat.a - 1.0) * 6378.137)  # 轨道高度 (sat.a 为地球半径单位)
    return {
        "azimuth": round(az, 1),
        "elevation": round(el, 1),
        "distance": round(dist, 0),
        "sub_lat": round(slat, 2),
        "sub_lon": round(slon, 2),
        "alt_km": round(alt_km, 0),
        "visible": el > 0,
    }


# ---------- 过境计算 ----------
def compute_passes(norad_id: str, hours: float = 24.0, min_elev: float = 0.0, step: float = 30.0):
    """计算未来 hours 小时内的过境, 返回过境列表"""
    sat = _get_satrec(norad_id)
    if sat is None:
        return []
    now = time.time()
    end = now + hours * 3600
    t = now
    prev_el = None
    passes = []
    cur = None
    while t <= end:
        pos = satellite_position(norad_id, t)
        el = pos["elevation"] if pos else -90
        if prev_el is not None and prev_el < 0 <= el:
            # AOS
            cur = {"aos": t, "aos_az": pos["azimuth"] if pos else 0, "max_el": 0, "max_t": t}
        if cur is not None and el > cur["max_el"]:
            cur["max_el"] = el
            cur["max_t"] = t
        # 检测仰角由升转降: 在粗采样(30s)下会错过峰值, 在峰值附近细扫 (1s) 以精确求得最大仰角
        if cur is not None and prev_el is not None and el < prev_el and cur["max_el"] >= prev_el - 0.5:
            peak = _refine_peak(norad_id, cur["max_t"], step)
            if peak is not None:
                cur["max_el"], cur["max_t"] = peak
        if prev_el is not None and prev_el >= 0 > el and cur is not None:
            # LOS
            cur["los"] = t
            cur["los_az"] = pos["azimuth"] if pos else 0
            if cur["max_el"] >= min_elev:
                passes.append(cur)
            cur = None
        prev_el = el
        t += step
    # 处理未结束的过境
    if cur is not None:
        cur["los"] = end
        cur["los_az"] = 0
        if cur["max_el"] >= min_elev:
            passes.append(cur)
    return passes


def _refine_peak(norad_id: str, center_t: float, coarse: float) -> tuple | None:
    """在中心时刻附近用 1s 步长细扫, 返回 (精确最大仰角, 对应时刻)"""
    best_el = -90.0
    best_t = center_t
    radius = coarse * 1.5  # 细扫窗口覆盖粗采样相邻点
    s = max(0.0, center_t - radius)
    e = center_t + radius
    x = s
    while x <= e:
        pos = satellite_position(norad_id, x)
        el = pos["elevation"] if pos else -90
        if el > best_el:
            best_el = el
            best_t = x
        x += 1.0
    return (best_el, best_t)


# ---------- 过境缓存与后台任务 ----------
_pass_cache = {}            # norad -> {"time": 计算时间戳, "passes": [...]}
_pass_cache_lock = threading.Lock()
PASSES_CACHE_AGE = 600      # 过境列表每 10 分钟重算
PASSES_HOURS = 48.0         # 缓存过境计算时长


def compute_passes_cached(norad_id: str, force: bool = False) -> list:
    """读取过境缓存, 未缓存或过期时现场计算并写入缓存"""
    norad = str(norad_id)
    with _pass_cache_lock:
        c = _pass_cache.get(norad)
        if c and not force and (time.time() - c["time"]) < PASSES_CACHE_AGE:
            return list(c["passes"])
    passes = compute_passes(norad, hours=PASSES_HOURS, min_elev=0)
    with _pass_cache_lock:
        _pass_cache[norad] = {"time": time.time(), "passes": passes}
    return passes


_pass_updating = threading.Lock()


def update_all_passes() -> None:
    """后台重算所有收藏卫星的过境列表"""
    if not _pass_updating.acquire(blocking=False):
        return  # 上一次计算还没结束, 跳过本次
    try:
        with _fav_lock:
            favs = list(_favorites)
        for f in favs:
            try:
                compute_passes_cached(str(f["norad"]), force=True)
            except Exception:  # noqa: BLE001
                pass
    finally:
        _pass_updating.release()


def start_background_jobs() -> None:
    """启动后台任务: TLE 每 6 小时更新, 过境列表每 10 分钟重算"""
    def _tle_loop():
        while True:
            time.sleep(TLE_MAX_AGE)
            try:
                fetch_tle(force=True)
            except Exception:  # noqa: BLE001
                pass

    def _passes_loop():
        while True:
            time.sleep(PASSES_CACHE_AGE)
            update_all_passes()

    threading.Thread(target=_tle_loop, daemon=True).start()
    threading.Thread(target=_passes_loop, daemon=True).start()
    # 启动后立即在后台算一次过境, 不阻塞服务启动
    threading.Thread(target=update_all_passes, daemon=True).start()


# ---------- 云台自动跟踪 ----------
class SatelliteTracker:
    """根据卫星目标方位角/仰角自动控制云台逼近"""

    def __init__(self, send_dir, get_position, set_position):
        self.send_dir = send_dir      # 发送方向指令: send_dir('left'/'right'/'up'/'down'/'stop')
        self.get_position = get_position  # 返回 (pan, tilt)
        self.set_position = set_position  # 设置云台位置 (pan, tilt)
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self.norad_id = None
        self.az_threshold = 0.5   # 方位角死区(度)
        self.interval = 1.0       # 控制周期(秒)
        self.pan_min = 5.0        # 水平最小角度 (限位)
        self.pan_max = 360.0      # 水平最大角度 (限位)
        self.tilt_min = 0.0       # 俯仰最小角度 (限位)
        self.tilt_max = 90.0      # 俯仰最大角度 (限位)
        self.pan_speed = 7.45     # 水平速度 (度/秒): 完整启停平均实测
        self.tilt_speed = 5.18    # 俯仰速度 (度/秒): 抬头/低头平均 (用于复位等)
        self.tilt_up_speed = 5.18    # 俯仰抬头速度 (度/秒): 克服重力较慢
        self.tilt_down_speed = 5.18  # 俯仰低头速度 (度/秒): 重力加速较快
        self.accel_time = 0.5     # 保留字段, 线性模型下不使用
        self._last_pan_dir = None   # 上次水平方向 (用于 hysteresis)
        self._last_tilt_dir = None  # 上次俯仰方向 (用于 hysteresis)
        # 分段跟踪状态
        self._pan_unwrapped = None      # 连续化后的云台 pan (沿最短路径累加)
        self._target_az_unwrapped = None  # 当前段目标方位 (连续化)
        self._target_el = None          # 当前段目标仰角

    def _time_for_angle(self, angle: float, speed: float) -> float:
        """纯线性模型: 时间 = 角度 / 速度 (忽略启动加速)"""
        if angle <= 0:
            return 0.0
        return angle / speed

    def _angle_for_time(self, elapsed: float, speed: float) -> float:
        """纯线性模型: 转过角度 = 速度 * 时间"""
        if elapsed <= 0:
            return 0.0
        return speed * elapsed

    def start(self, norad_id: str):
        self.stop()  # 先停止旧线程 (stop 内部自行加锁)
        with self._lock:
            self.norad_id = norad_id
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self):
        with self._lock:
            if self._thread is not None:
                self._stop.set()
                self._thread.join(timeout=0.5)
                self._thread = None
            self.norad_id = None

    def is_tracking(self) -> bool:
        with self._lock:
            return self._thread is not None

    def _pan_delta(self, az: float, pan: float) -> float:
        """最短路径方位角差: 结果范围 [-180, 180], 自动处理 0/360 越线"""
        return (az - pan + 180) % 360 - 180

    def _drive(self, direction: str, seconds: float, ignore_stop: bool = False):
        """在指定时间内持续发送同一方向指令 (每 100ms 一帧)
        ignore_stop=True 时不检查 _stop (用于独立的 move_to 线程)
        """
        if seconds <= 0:
            return
        end = time.time() + seconds
        while time.time() < end:
            if not ignore_stop and self._stop.is_set():
                return
            self.send_dir(direction)
            if ignore_stop:
                time.sleep(0.1)
            else:
                self._stop.wait(0.1)

    def move_to(self, target_pan=None, target_tilt=None, timeout: float = 15.0):
        """移动到指定 pan/tilt 位置, 用于测试运动精度"""
        pos = self.get_position()
        if pos is None:
            return False
        pan, tilt = pos
        threshold = 0.5  # 指定位置用更精确的死区
        pan_need = False
        pan_dir = None
        pan_sec = 0.0
        if target_pan is not None:
            daz = self._pan_delta(float(target_pan), pan)
            if abs(daz) > threshold:
                pan_need = True
                pan_dir = "right" if daz > 0 else "left"
                pan_sec = min(self._time_for_angle(abs(daz), self.pan_speed), timeout)
        tilt_need = False
        tilt_dir = None
        tilt_sec = 0.0
        if target_tilt is not None:
            target_tilt = float(target_tilt)
            if self.tilt_min <= target_tilt <= self.tilt_max:
                del_ = target_tilt - tilt
                if abs(del_) > threshold:
                    tilt_need = True
                    tilt_dir = "up" if del_ > 0 else "down"  # up=抬头(增大tilt)
                    # 抬头/低头速度不同 (重力影响), 分开使用
                    tilt_speed = self.tilt_up_speed if del_ > 0 else self.tilt_down_speed
                    tilt_sec = min(self._time_for_angle(abs(del_), tilt_speed), timeout)
        if not pan_need and not tilt_need:
            self.send_dir("stop")
            return True
        both = min(pan_sec, tilt_sec)
        if both > 0.05 and pan_need and tilt_need:
            self._drive(tilt_dir + pan_dir, both, ignore_stop=True)
        if pan_need and pan_sec > tilt_sec + 0.05:
            self._drive(pan_dir, pan_sec - both, ignore_stop=True)
        elif tilt_need and tilt_sec > pan_sec + 0.05:
            self._drive(tilt_dir, tilt_sec - both, ignore_stop=True)
        self.send_dir("stop")
        return True

    def _loop(self):
        """闭环跟踪: 预测性控制, 根据卫星角速度提前瞄准

        改进点:
        - 动态提前量: 基础 1.0s + 角速度补偿, 卫星越快提前越多
        - 角速度预测: 记录方位/仰角变化率, 叠加反应延迟内的预测位移
        - 小步快跑: 俯仰死区 0.5° (原 1°), 步长 0.5°, 反应更快
        - 水平死区 0.4° (原 0.6°), 减少跟踪滞后
        """
        cycle = 0.1
        deadzone = 0.4       # 水平死区(度)
        move_per_cycle = self.pan_speed * cycle
        pulse_zone = move_per_cycle + deadzone
        tilt_step = 0.5      # 俯仰死区/步长(度): 小步快跑
        lead_s = 1.0         # 基础提前量(秒): 查询未来位置
        reaction_s = 0.3     # 云台反应延迟(秒): 额外预测补偿

        # 角速度跟踪
        prev_az = None
        prev_el = None
        prev_t = None

        while not self._stop.is_set():
            cycle_start = time.time()
            try:
                now = time.time()
                sat = satellite_position(self.norad_id, now + lead_s)
                if sat is None:
                    self.send_dir("stop")
                    break
                az, el = sat["azimuth"], sat["elevation"]

                # ---- 计算角速度 (用连续周期的未来位置差分) ----
                az_rate = 0.0
                el_rate = 0.0
                if prev_az is not None and prev_t is not None:
                    dt = now - prev_t
                    if dt > 0.05:
                        az_rate = self._pan_delta(az, prev_az) / dt
                        el_rate = (el - prev_el) / dt
                prev_az = az
                prev_el = el
                prev_t = now

                # ---- 预测性目标: 未来位置 + 反应延迟内的位移 ----
                az_target = az + az_rate * reaction_s
                el_target = el + el_rate * reaction_s

                pan, tilt = self.get_position()
                daz = self._pan_delta(az_target, pan)
                del_ = el_target - tilt

                # ---- 俯仰: 小步快跑, 死区 0.5° ----
                tilt_dir = None
                if el_target > 0 and self.tilt_min <= el_target <= self.tilt_max:
                    if del_ > tilt_step:
                        tilt_dir = "up"
                    elif del_ < -tilt_step:
                        tilt_dir = "down"
                if tilt_dir == "up" and tilt >= self.tilt_max - 0.01:
                    tilt_dir = None
                elif tilt_dir == "down" and tilt <= self.tilt_min + 0.01:
                    tilt_dir = None

                # ---- 水平: 三段控制 (AS5600 闭环) ----
                adaz = abs(daz)
                if adaz <= deadzone:
                    pan_mode = "stop"
                elif adaz <= pulse_zone:
                    pan_mode = "pulse"
                else:
                    pan_mode = "move"
                pan_dir = "right" if daz > 0 else "left"

                if tilt_dir is not None and pan_mode != "stop":
                    print(f"[track] az={az:.1f} pan={pan:.1f} daz={daz:.1f} "
                          f"az_rate={az_rate:.2f} el={el:.1f} tilt={tilt:.1f} del={del_:.1f} "
                          f"el_rate={el_rate:.2f} pan={pan_mode} tilt={tilt_dir}", flush=True)

                # ---- 发送指令 ----
                if pan_mode == "move":
                    self.send_dir(tilt_dir + pan_dir if tilt_dir is not None else pan_dir)
                elif tilt_dir is not None:
                    # 俯仰步进: 运行步长所需时间, 到位后重新评估
                    speed = self.tilt_up_speed if tilt_dir == "up" else self.tilt_down_speed
                    self._drive(tilt_dir, tilt_step / speed)
                    self.send_dir("stop")
                    continue
                elif pan_mode == "pulse":
                    # 动态时长点动: 按误差/速度计算脉冲时长, 精确消除残差
                    # (AS5600 实测位置在下一周期作为新起点, 形成闭环修正)
                    sec = max(0.03, adaz / self.pan_speed)
                    self.send_dir(pan_dir)
                    self._stop.wait(sec)
                    self.send_dir("stop")
                else:
                    self.send_dir("stop")
            except Exception:  # noqa: BLE001
                pass
            elapsed = time.time() - cycle_start
            remaining = cycle - elapsed
            if remaining > 0:
                self._stop.wait(remaining)
