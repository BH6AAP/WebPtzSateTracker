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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sgp4.api import Satrec, jday

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TLE_CACHE_FILE = os.path.join(BASE_DIR, "tle_cache.json")
FAVORITES_FILE = os.path.join(BASE_DIR, "sat_favorites.json")
OBSERVER_FILE = os.path.join(BASE_DIR, "sat_observer.json")

TLE_URLS = [
    "https://db.satnogs.org/api/tle/?format=json",  # SatNOGS JSON (全量卫星, 首选)
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
    return round(lat, 6), round(lon, 6)


def set_observer(lat: float, lon: float, alt: float = 0.0):
    with _obs_lock:
        # 精度保留 6 位小数 (~0.1m 地表分辨率)
        _observer["lat"] = round(float(lat), 6)
        _observer["lon"] = round(float(lon), 6)
        _observer["alt"] = round(float(alt), 2)
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


# 观测站 ECEF 缓存 (观测站坐标极少变化, 每帧计算省去重复三角函数; set_observer 改坐标后自动失效)
_obs_ecef_cache = None


def _observer_ecef() -> tuple:
    """返回 (obs, obs_ecef), obs_ecef 带缓存"""
    global _obs_ecef_cache
    obs = get_observer()
    c = _obs_ecef_cache
    if c is not None and c[0] == obs["lat"] and c[1] == obs["lon"] and c[2] == obs["alt"]:
        return obs, c[3]
    ecef = _geodetic_to_ecef(obs["lat"], obs["lon"], obs["alt"] / 1000.0)
    _obs_ecef_cache = (obs["lat"], obs["lon"], obs["alt"], ecef)
    return obs, ecef


# ---------- TLE 数据源 (可配置: config.json 的 tle_urls, 空=默认源) ----------
DEFAULT_TLE_URLS = list(TLE_URLS)
_CONFIG_FILE = os.path.join(BASE_DIR, "config.json")


def get_tle_urls() -> list:
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            urls = json.load(f).get("tle_urls")
        if isinstance(urls, list) and urls:
            return [str(u) for u in urls if str(u).strip()]
    except Exception:  # noqa: BLE001
        pass
    return list(DEFAULT_TLE_URLS)


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


def _parse_tle_json(text: str) -> dict:
    """解析 JSON 格式 TLE (SatNOGS 等), 返回 norad_id -> {name,line1,line2}"""
    result = {}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return result
    if not isinstance(data, list):
        return result
    for item in data:
        if not isinstance(item, dict):
            continue
        line1 = item.get("tle1", "")
        line2 = item.get("tle2", "")
        if not (line1.startswith("1 ") and line2.startswith("2 ")):
            continue
        norad = str(item.get("norad_cat_id", "")).strip()
        if not norad:
            try:
                norad = line1[2:7].strip()
            except Exception:  # noqa: BLE001
                continue
        name = item.get("tle0", "")
        if name.startswith("0 "):
            name = name[2:]
        result[norad] = {"name": name.strip(), "line1": line1.strip(), "line2": line2.strip()}
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
    for url in get_tle_urls():
        try:
            text = _fetch_url(url)
            parsed = _parse_tle_text(text)
            if not parsed:
                parsed = _parse_tle_json(text)
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
        _bump_tle_version(merged)
        try:
            with open(TLE_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass
    return dict(merged)


def get_tle(norad_id: str) -> dict | None:
    with _tle_lock:
        return _tle.get(str(norad_id))


# TLE 内容版本号: 内容变化时更新; 过境全量重算以此决定是否跳过 (J1900 省算力)
_tle_version = 0
_passes_tle_version = -1


def _bump_tle_version(tle: dict) -> None:
    """TLE 数据变化时递增版本号"""
    global _tle_version
    ver = hash(json.dumps(tle, sort_keys=True)) & 0x7FFFFFFF
    if ver != _tle_version:
        _tle_version = ver


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


_fav_cache = {"time": 0.0, "data": []}   # 收藏列表计算结果缓存 (30s), 过境中的回溯细扫很贵
FAV_CACHE_AGE = 30.0


def get_favorites() -> list:
    """收藏列表富数据: 直接读后台预计算缓存, 请求线程零计算、永不阻塞。
    服务刚启动缓存尚未产出时返回空列表, 后台任务算完后经 favorites 帧推送。"""
    with _fav_lock:
        return list(_fav_cache["data"])


_fav_refresh_lock = threading.Lock()


def _compute_one_favorite(f: dict, now: float) -> tuple:
    """计算单颗收藏卫星的富数据, 返回 (排序键, item)。"""
    norad = str(f["norad"])
    item = dict(f)
    passes = compute_passes_cached(norad)  # 读后台过境缓存, 不现场计算
    pos = satellite_position(norad, now)
    cur_el = pos["elevation"] if pos else -90
    if cur_el > 0:
        # 正在过境: 用本次过境信息 (passes[0] 是下一次过境, 峰值/时间均不对)
        cp = current_pass_info(norad, now)
        if cp is not None:
            item["next_aos"] = cp["aos"]
            item["next_los"] = cp["los"]
            item["max_el"] = round(cp["max_el"], 1)
            item["aos_az"] = round(cp["aos_az"], 1)
            item["los_az"] = round(cp["los_az"], 1)
            item["status"] = "in_pass"
            return (0, item)
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
    return (sort_key, item)


def _fav_sort_key(item: dict) -> float:
    """缓存内排序键 (与 _compute_one_favorite 一致)"""
    if item.get("status") == "in_pass":
        return 0
    aos = item.get("next_aos")
    if aos:
        return max(1, aos - time.time())
    return 1e12


def _compute_favorites() -> list:
    """全量重算收藏列表富数据并写入缓存 (后台定时维护, 请求不触发)"""
    with _fav_lock:
        favs = list(_favorites)
    now = time.time()
    enriched = []
    for f in favs:
        try:
            sort_key, item = _compute_one_favorite(f, now)
            enriched.append((sort_key, item))
        except Exception:  # noqa: BLE001
            continue
    enriched.sort(key=lambda x: x[0])
    result = [x[1] for x in enriched]
    _fav_cache["time"] = time.time()
    _fav_cache["data"] = result
    return result


def _kick_fav_refresh() -> None:
    """后台重算收藏列表 (并发去重: 已在重算则跳过)"""
    if not _fav_refresh_lock.acquire(blocking=False):
        return

    def _bg():
        try:
            _compute_favorites()
        except Exception:  # noqa: BLE001
            pass
        finally:
            _fav_refresh_lock.release()

    threading.Thread(target=_bg, daemon=True).start()


def _favs_loop() -> None:
    """后台持续预计算收藏列表富数据: 用户请求只读缓存, 零计算"""
    time.sleep(2.0)  # 等 update_all_passes 预热过境缓存, 避免冷算重复 (每颗 ~1s)
    try:
        _compute_favorites()  # 启动立即产出缓存
    except Exception:  # noqa: BLE001
        pass
    while True:
        time.sleep(FAV_CACHE_AGE)  # 每 30s 全量刷新 (过境缓存命中, 成本低)
        try:
            _compute_favorites()
        except Exception:  # noqa: BLE001
            pass


def add_favorite(norad_id: str) -> bool:
    norad_id = str(norad_id)
    tle = get_tle(norad_id)
    if tle is None:
        return False
    with _fav_lock:
        if norad_id in [f["norad"] for f in _favorites]:
            return True
        _favorites.append({"norad": norad_id, "name": tle.get("name", "")})
        _save_favorites()
    # 增量: 仅计算新收藏的这颗卫星并按排序插入缓存, 不触发全量重算 (省计算)
    try:
        sort_key, item = _compute_one_favorite({"norad": norad_id, "name": tle.get("name", "")}, time.time())
        with _fav_lock:
            cur = [x for x in _fav_cache["data"] if str(x["norad"]) != norad_id]
            cur.append(item)
            cur.sort(key=_fav_sort_key)
            _fav_cache["time"] = time.time()
            _fav_cache["data"] = cur
    except Exception:  # noqa: BLE001
        # 计算失败: 使缓存失效, 由后台任务补算
        with _fav_lock:
            _fav_cache["time"] = 0.0
    return True


def remove_favorite(norad_id: str) -> bool:
    global _favorites
    norad_id = str(norad_id)
    with _fav_lock:
        before = len(_favorites)
        _favorites = [f for f in _favorites if f["norad"] != norad_id]
        if len(_favorites) != before:
            _save_favorites()
            # 增量: 直接从缓存移除该卫星 (不触发全量重算)
            _fav_cache["data"] = [x for x in _fav_cache["data"] if str(x["norad"]) != norad_id]
            return True
    return False


def _load_tle_cache():
    """启动时从 tle_cache.json 预热内存 TLE: 请求路径零网络等待。
    否则服务重启后首个 /api/favorites 请求会同步 fetch_tle (最长 30s 网络超时),
    造成"偶尔列表加载不出来"。
    """
    global _tle, _tle_fetch_time
    try:
        if os.path.exists(TLE_CACHE_FILE):
            with open(TLE_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data:
                with _tle_lock:
                    _tle = data
                    _tle_fetch_time = time.time()
                _bump_tle_version(data)
    except Exception:  # noqa: BLE001
        pass


_load_tle_cache()
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
    obs, obs_ecef = _observer_ecef()
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


# ---------- 月球位置 (Meeus 低精度算法, 误差 ~0.3°, 云台跟踪足够) ----------
def moon_position(when: float | None = None):
    """计算月球站心方位角/仰角/距离, 返回 dict 或 None (与 satellite_position 同构)"""
    when = time.time() if when is None else when
    dt = datetime.fromtimestamp(when, tz=timezone.utc)
    jd, fr = jday(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second + dt.microsecond / 1e6)
    T = (jd + fr - 2451545.0) / 36525.0
    r = math.radians
    # 月球地心黄经/黄纬/距离 (主要摄动项)
    Lp = r(218.3164477 + 481267.88123421 * T)
    D = r(297.8501921 + 445267.1114034 * T)
    M = r(357.5291092 + 35999.0502909 * T)
    Mp = r(134.9633964 + 477198.8675055 * T)
    F = r(93.2720950 + 483202.0175233 * T)
    lam = Lp + r(6.289 * math.sin(Mp) + 1.274 * math.sin(2 * D - Mp)
                 + 0.658 * math.sin(2 * D) + 0.214 * math.sin(2 * Mp)
                 - 0.186 * math.sin(M) - 0.059 * math.sin(2 * D - 2 * Mp)
                 - 0.057 * math.sin(2 * D - M - Mp) + 0.053 * math.sin(2 * D + Mp))
    beta = r(5.128 * math.sin(F) + 0.281 * math.sin(Mp + F)
             - 0.278 * math.sin(Mp - F) - 0.173 * math.sin(2 * D - F))
    dist = 385001 - 20905 * math.cos(Mp) - 3699 * math.cos(2 * D - Mp) - 2956 * math.cos(2 * D)
    # 黄道 -> 赤道 (RA/DEC)
    eps = r(23.4392911 - 0.0130042 * T)
    sin_b, cos_b = math.sin(beta), math.cos(beta)
    sin_l, cos_l = math.sin(lam), math.cos(lam)
    sin_e, cos_e = math.sin(eps), math.cos(eps)
    dec = math.asin(sin_b * cos_e + cos_b * sin_e * sin_l)
    ra = math.atan2(sin_l * cos_e - math.tan(beta) * sin_e, cos_l)  # [-pi,pi]
    # 地心赤道坐标 -> ECEF (绕 Z 轴转 GMST, 与 TEME->ECEF 同法)
    theta = math.radians(_gmst_deg(jd + fr))
    rm = dist  # km
    mx = rm * math.cos(dec) * math.cos(ra)
    my = rm * math.cos(dec) * math.sin(ra)
    mz = rm * math.sin(dec)
    c, s = math.cos(theta), math.sin(theta)
    moon_ecef = (mx * c + my * s, -mx * s + my * c, mz)
    mn = math.sqrt(moon_ecef[0] ** 2 + moon_ecef[1] ** 2 + moon_ecef[2] ** 2)
    obs, obs_ecef = _observer_ecef()
    az, el = _ecef_to_azel(obs_ecef, moon_ecef, obs["lat"], obs["lon"])
    return {
        "azimuth": round(az, 1),
        "elevation": round(el, 1),
        "distance": round(dist, 0),
        "sub_lat": round(math.degrees(math.asin(moon_ecef[2] / mn)), 2),
        "sub_lon": round(math.degrees(math.atan2(moon_ecef[1], moon_ecef[0])), 2),
        "visible": el > 0,
    }


# ---------- 太阳位置 (Meeus 太阳几何, 误差 ~0.01°, 与 moon_position 同构) ----------
def sun_position(when: float | None = None):
    """计算太阳站心方位角/仰角/距离, 返回 dict 或 None (与 moon_position 同构)"""
    when = time.time() if when is None else when
    dt = datetime.fromtimestamp(when, tz=timezone.utc)
    jd, fr = jday(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second + dt.microsecond / 1e6)
    T = (jd + fr - 2451545.0) / 36525.0
    r = math.radians
    # 太阳几何 (黄经 = 平均黄经 + 中心差, 精度 ~0.01°)
    L0 = 280.46646 + 36000.76983 * T + 0.0003032 * T * T
    M = 357.52911 + 35999.05029 * T - 0.0001537 * T * T
    M_r = r(M)
    C = (1.914602 - 0.004817 * T - 0.000014 * T * T) * math.sin(M_r) \
        + (0.019993 - 0.000101 * T) * math.sin(2 * M_r) \
        + 0.000289 * math.sin(3 * M_r)
    lam = r(L0 + C)
    dist_au = 1.000001018 * (1 - 0.016708634 * math.cos(M_r) - 0.000139737 * math.cos(2 * M_r))
    # 黄道 -> 赤道 (RA/DEC)
    eps = r(23.439291 - 0.0130042 * T)
    sin_e, cos_e = math.sin(eps), math.cos(eps)
    sin_l, cos_l = math.sin(lam), math.cos(lam)
    ra = math.atan2(cos_e * sin_l, cos_l)          # [-pi,pi]
    dec = math.asin(sin_e * sin_l)
    dist = dist_au * 149597870.7                    # AU -> km
    # 地心赤道坐标 -> ECEF (绕 Z 轴转 GMST, 与月球同法)
    theta = math.radians(_gmst_deg(jd + fr))
    mx = dist * math.cos(dec) * math.cos(ra)
    my = dist * math.cos(dec) * math.sin(ra)
    mz = dist * math.sin(dec)
    c, s = math.cos(theta), math.sin(theta)
    sun_ecef = (mx * c + my * s, -mx * s + my * c, mz)
    sn = math.sqrt(sun_ecef[0] ** 2 + sun_ecef[1] ** 2 + sun_ecef[2] ** 2)
    obs, obs_ecef = _observer_ecef()
    az, el = _ecef_to_azel(obs_ecef, sun_ecef, obs["lat"], obs["lon"])
    return {
        "azimuth": round(az, 1),
        "elevation": round(el, 1),
        "distance": round(dist, 0),
        "sub_lat": round(math.degrees(math.asin(sun_ecef[2] / sn)), 2),
        "sub_lon": round(math.degrees(math.atan2(sun_ecef[1], sun_ecef[0])), 2),
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


def current_pass_info(norad_id: str, now: float | None = None) -> dict | None:
    """计算正在进行的过境段信息, 返回 {aos, los, aos_az, los_az, max_el, max_t} 或 None。

    用途: 卫星当前在轨时, compute_passes 从"现在"起扫无法识别已开始的过境,
    passes[0] 实际是下一次过境。本函数回溯找本次 AOS、前扫找 LOS,
    并计算本次过境的完整峰值 (含未来未到达部分)。
    """
    now = time.time() if now is None else now
    if _get_satrec(norad_id) is None:
        return None
    pos = satellite_position(norad_id, now)
    if pos is None or pos["elevation"] <= 0:
        return None
    # 回溯找本次 AOS (仰角由负转正), 最多 6 小时
    aos, aos_az = None, 0.0
    t = now
    prev_el = None
    for _ in range(720):
        p = satellite_position(norad_id, t)
        el = p["elevation"] if p else -90
        if prev_el is not None and prev_el < 0 <= el:
            aos, aos_az = t, p["azimuth"] if p else 0.0
            break
        prev_el = el
        t -= 30.0
    if aos is None:
        aos = max(0.0, now - 6 * 3600)
    # 前扫找本次 LOS (仰角由正转负), 最多 6 小时
    los, los_az = None, 0.0
    t = now
    prev_el = None
    for _ in range(720):
        p = satellite_position(norad_id, t)
        el = p["elevation"] if p else -90
        if prev_el is not None and prev_el >= 0 > el:
            los, los_az = t, p["azimuth"] if p else 0.0
            break
        prev_el = el
        t += 30.0
    if los is None:
        los = now + 6 * 3600
    # 本次过境全段峰值 (粗扫 30s + 峰值附近 1s 细扫)
    max_el, max_t = -90.0, now
    x = aos
    while x <= los:
        p = satellite_position(norad_id, x)
        el = p["elevation"] if p else -90
        if el > max_el:
            max_el, max_t = el, x
        x += 30.0
    peak = _refine_peak(norad_id, max_t, 30.0)
    if peak is not None:
        max_el, max_t = peak
    return {"aos": aos, "los": los, "aos_az": aos_az, "los_az": los_az,
            "max_el": max_el, "max_t": max_t}


# ---------- 过境缓存与后台任务 ----------
_pass_cache = {}            # norad -> {"time": 计算时间戳, "passes": [...]}
_pass_cache_lock = threading.Lock()
PASSES_CACHE_AGE = 600      # 过境列表每 10 分钟重算
PASSES_HOURS = 48.0         # 缓存过境计算时长


def compute_passes_cached(norad_id: str, force: bool = False) -> list:
    """读取过境缓存; 未缓存时现场计算, 过期时返回旧值并交后台重算 (请求不阻塞)"""
    norad = str(norad_id)
    with _pass_cache_lock:
        c = _pass_cache.get(norad)
        if c and not force and (time.time() - c["time"]) < PASSES_CACHE_AGE:
            return list(c["passes"])
        stale = list(c["passes"]) if c else None
    if stale is not None and not force:
        # 有旧数据: 立即返回旧值, 48h×30s 步进的 SGP4 重算 (~1s/颗) 放后台,
        # 避免请求线程被计算阻塞拖慢前端列表 (偶发"列表加载不出来")
        _kick_pass_refresh(norad)
        return stale
    t0 = time.time()
    passes = compute_passes(norad, hours=PASSES_HOURS, min_elev=0)
    print(f"[passes] {norad} 冷计算 {time.time() - t0:.2f}s / {len(passes)} 场", flush=True)
    with _pass_cache_lock:
        _pass_cache[norad] = {"time": time.time(), "passes": passes}
    return passes


_pass_kick_lock = threading.Lock()


def _kick_pass_refresh(norad: str) -> None:
    """触发单颗卫星的后台过境重算 (并发去重: 已在重算则跳过)"""
    if not _pass_kick_lock.acquire(blocking=False):
        return

    def _bg():
        try:
            passes = compute_passes(norad, hours=PASSES_HOURS, min_elev=0)
            with _pass_cache_lock:
                _pass_cache[norad] = {"time": time.time(), "passes": passes}
        except Exception:  # noqa: BLE001
            pass
        finally:
            _pass_kick_lock.release()

    threading.Thread(target=_bg, daemon=True).start()


# ---------- 轨迹/雷达图缓存: 选中卫星时秒开, 过期返回旧值并后台重算 ----------
class SwrCache:
    """stale-while-revalidate 缓存
    未过期直接返回; 过期返回旧值并触发后台重算; 首次(无旧值)现场计算。
    compute 闭包返回 None 时不缓存。
    """

    def __init__(self, ttl: float):
        self.ttl = ttl
        self._data = {}        # key -> {"time": ts, "value": ...}
        self._lock = threading.Lock()
        self._busy = set()     # 正在重算的 key (去重, 防重复线程)

    def get(self, key: str, compute):
        with self._lock:
            c = self._data.get(key)
            if c and (time.time() - c["time"]) < self.ttl:
                return c["value"]
            stale = c["value"] if c else None
            need_recalc = key not in self._busy
            if need_recalc:
                self._busy.add(key)
        if stale is not None:
            if need_recalc:
                self._spawn(key, compute)
            return stale
        value = compute()  # 首次: 无旧值可回, 现场算一次
        with self._lock:
            self._busy.discard(key)
            if value is not None:
                self._data[key] = {"time": time.time(), "value": value}
        return value

    def _spawn(self, key: str, compute):
        def _bg():
            try:
                v = compute()
                with self._lock:
                    self._busy.discard(key)
                    if v is not None:
                        self._data[key] = {"time": time.time(), "value": v}
            except Exception:  # noqa: BLE001
                with self._lock:
                    self._busy.discard(key)
        threading.Thread(target=_bg, daemon=True).start()


_track_cache = SwrCache(600.0)   # 星下点轨迹 10 分钟
_radar_cache = SwrCache(60.0)    # 雷达图点位 1 分钟


def compute_track_cached(norad_id: str, hours: float, step: float) -> list:
    """星下点轨迹 (缓存版): 返回 [{lat, lon, t}...], 调用方按 t>=now 过滤陈旧头部"""
    def _compute():
        points = []
        end = time.time() + hours * 3600
        t = time.time()
        while t <= end:
            pos = satellite_position(norad_id, t)
            if pos:
                points.append({"lat": pos["sub_lat"], "lon": pos["sub_lon"], "t": t})
            t += step
        return points
    return _track_cache.get(f"{norad_id}|{hours}|{step}", _compute)


def compute_radar_cached(norad_id: str, minutes: float, step: float) -> list:
    """雷达图 az/el 点位 (缓存版): 返回 [{az, el, t}...]"""
    def _compute():
        points = []
        end = time.time() + minutes * 60
        t = time.time()
        while t <= end:
            pos = satellite_position(norad_id, t)
            if pos:
                points.append({"az": pos["azimuth"], "el": pos["elevation"], "t": t})
            t += step
        return points
    return _radar_cache.get(f"{norad_id}|{minutes}|{step}", _compute)


def compute_radar_pass(norad_id: str, aos: float, los: float, step: float = 30.0) -> list:
    """计算指定过境窗口 [aos, los] 内的方位/仰角迹线点 (雷达图完整 AOS→LOS 迹线)"""
    points = []
    t = aos
    while t <= los:
        pos = satellite_position(norad_id, t)
        if pos:
            points.append({"az": pos["azimuth"], "el": pos["elevation"], "t": t})
        t += step
    return points


_pass_updating = threading.Lock()


def update_all_passes() -> None:
    """后台重算所有收藏卫星的过境列表 (TLE 内容未变时跳过, 避免无谓的重算风暴)"""
    global _passes_tle_version
    if _passes_tle_version == _tle_version:
        return  # TLE 没变, 已算过的过境列表仍然有效
    if not _pass_updating.acquire(blocking=False):
        return  # 上一次计算还没结束, 跳过本次
    try:
        with _fav_lock:
            favs = list(_favorites)
        if not favs:
            _passes_tle_version = _tle_version
            return
        # 并发预热: 每颗 48h 过境冷算 1~6s, 串行下重启后列表几十秒才就绪; 并行降约 4 倍
        with ThreadPoolExecutor(max_workers=4) as ex:
            for f in favs:
                ex.submit(compute_passes_cached, str(f["norad"]), True)
        _passes_tle_version = _tle_version
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
    # 启动后立即后台拉取最新 TLE (网络失败自动回退 tle_cache.json), 不阻塞请求
    threading.Thread(target=lambda: fetch_tle(force=True), daemon=True).start()
    # 启动后立即在后台算一次过境, 不阻塞服务启动
    threading.Thread(target=update_all_passes, daemon=True).start()
    # 收藏列表富数据持续预计算: 用户请求只读缓存, 零计算
    threading.Thread(target=_favs_loop, daemon=True).start()


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
        self._pan_moving = False    # 水平是否在持续移动 (预测停判断用)
        # 分段跟踪状态
        self._pan_unwrapped = None      # 连续化后的云台 pan (沿最短路径累加)
        self._target_az_unwrapped = None  # 当前段目标方位 (连续化)
        self._target_el = None          # 当前段目标仰角
        self.target = "sat"             # 跟踪目标类型: 'sat' / 'moon'
        self._target_fn = None          # 目标位置函数: t -> {"azimuth","elevation",...}
        self._hb = 0.0                  # 跟踪循环心跳时间戳 (看门狗监控用)
        # 惯性推算 (UDP 丢失时开环跟踪)
        self._dr_start_pan = None  # 断联时的最后编码器角度
        self._dr_start_time = 0.0  # 断联时刻
        self._dr_start_dir = 0     # 断联时的方向: 1=right, -1=left, 0=stop

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

    def start(self, norad_id: str, target: str = "sat"):
        """启动跟踪. target: 'sat'=卫星(norad_id) / 'moon'=月球 / 'sun'=太阳 (后两者忽略 norad_id)"""
        self.stop()  # 先停止旧线程 (stop 内部自行加锁)
        with self._lock:
            self.target = target if target in ("moon", "sun") else "sat"
            self.norad_id = str(norad_id) if target == "sat" else target
            if target == "moon":
                self._target_fn = moon_position
            elif target == "sun":
                self._target_fn = sun_position
            else:
                self._target_fn = lambda t, n=self.norad_id: satellite_position(n, t)
            # 每个线程一个私有 stop Event (参数传入), 避免 join 超时的旧线程
            # 通过 self._stop 属性"复活"到新 Event 上而失联永生
            stop_ev = threading.Event()
            self._stop = stop_ev
            self._pan_moving = False  # 新跟踪周期: 水平初始静止
            self._hb = time.time()    # 心跳初始化为当前时刻
            self._thread = threading.Thread(target=self._loop, args=(stop_ev,), daemon=True)
            self._thread.start()
            # 看门狗: 循环心跳超时(阻塞/卡死)时强制发 stop, 防云台单帧锁存持续转动
            threading.Thread(target=self._watchdog, args=(stop_ev,), daemon=True).start()

    def stop(self):
        t = None
        with self._lock:
            if self._stop is not None:
                self._stop.set()
            t = self._thread
            self._thread = None
            self.norad_id = None
        if t is not None and t.is_alive():
            t.join(timeout=2.0)

    def is_tracking(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def _watchdog(self, stop_ev):
        """跟踪看门狗: 若跟踪循环心跳超时(线程阻塞/卡死), 强制发送 stop 帧。
        云台为单帧锁存型: 收到一帧 move 后持续转动直到 stop 帧,
        循环阻塞数秒未发 stop 会导致云台"突然连续转动"。
        """
        while not stop_ev.is_set():
            time.sleep(0.5)
            if time.time() - self._hb > 2.5:
                print("[track] watchdog: 循环心跳超时 >2.5s, 强制停止云台", flush=True)
                try:
                    self.send_dir("stop")
                except Exception:  # noqa: BLE001
                    pass
                self._hb = time.time()   # 防连续触发刷屏

    def _pan_delta(self, az: float, pan: float) -> float:
        """最短路径方位角差: 结果范围 [-180, 180], 自动处理 0/360 越线"""
        return (az - pan + 180) % 360 - 180

    def _drive(self, direction: str, seconds: float, ignore_stop: bool = False,
               stop_ev=None, cancel_ev=None):
        """在指定时间内持续发送同一方向指令 (每 100ms 一帧)
        ignore_stop=True 时不检查 _stop (用于独立的 move_to 线程)
        stop_ev: 跟踪线程私有停止标志 (None 时用 self._stop)
        cancel_ev: 额外取消标志 (move_to 等独立线程可用它中断, 置位即发停止帧)
        """
        if seconds <= 0:
            return
        ev = stop_ev if stop_ev is not None else self._stop
        end = time.time() + seconds
        while time.time() < end:
            if cancel_ev is not None and cancel_ev.is_set():
                self.send_dir("stop")
                return
            if not ignore_stop and ev.is_set():
                return
            self.send_dir(direction)
            if ignore_stop:
                time.sleep(0.1)
            else:
                ev.wait(0.1)

    def move_to(self, target_pan=None, target_tilt=None, timeout: float = 15.0,
                cancel_ev=None):
        """移动到指定 pan/tilt 位置, 用于测试运动精度
        cancel_ev: 置位即停止 (供 rotctld 桥接等外部控制中断用)"""
        pos = self.get_position()
        if pos is None:
            return False
        pan, tilt, _ = pos
        threshold = 0.5  # 指定位置用更精确的死区
        pan_need = False
        pan_dir = None
        pan_sec = 0.0
        if cancel_ev is not None and cancel_ev.is_set():
            self.send_dir("stop")
            return False
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
            self._drive(tilt_dir + pan_dir, both, ignore_stop=True, cancel_ev=cancel_ev)
        if pan_need and pan_sec > tilt_sec + 0.05:
            self._drive(pan_dir, pan_sec - both, ignore_stop=True, cancel_ev=cancel_ev)
        elif tilt_need and tilt_sec > pan_sec + 0.05:
            self._drive(tilt_dir, tilt_sec - both, ignore_stop=True, cancel_ev=cancel_ev)
        self.send_dir("stop")
        return True

    def _loop(self, stop_ev=None):
        """闭环跟踪: 预测性控制, 根据卫星角速度提前瞄准

        stop_ev: 本线程私有停止标志 (线程内禁止通过 self._stop 访问,
                 防止 start() 替换 Event 后旧线程失联)

        改进点:
        - 动态提前量: 基础 0.3s + 角速度补偿, 卫星越快提前越多 (原 1.0s,
          过境方位速率 ~1.5°/s 时稳态超前 ~2° 的提前偏差已降至 <1°)
        - 角速度预测: 记录方位/仰角变化率, 叠加反应延迟内的预测位移
        - 取消脉冲点动 (大天线: 频繁启停抖动被放大): 恒速持续移动 + 预测停,
          误差超死区才启动; 移动中误差进入滑行窗口即提前停, 靠惯性滑进
          死区, 避免全速过冲反向 (左右抽搐根源)。窄八木死区取 1.5°
        - 俯仰: 死区/步长 1.0°, 减少启停
        """
        cycle = 0.1
        deadzone = 0.8       # 水平死区(度): 曾 1.5 致稳态落后卫星 ~1°; 收窄后稳态 <0.8°
        stop_lead = 2.0      # 预测停窗口(度): 移动中误差<此值提前停, 靠滑行入死区
                             # (滑行距离实测约 <1°, 此值=死区+滑行余量)
        tilt_step = 1.0      # 俯仰死区/步长(度): 原 0.5° 小步快跑, 大天线放宽减启停
        lead_s = 1.2         # 基础提前量(秒): 查询未来位置, 云台提前到位等待而非追赶
        reaction_s = 0.3     # 云台反应延迟(秒): 额外预测补偿

        # 角速度跟踪
        prev_az = None
        prev_el = None
        prev_t = None

        while not stop_ev.is_set():
            cycle_start = time.time()
            self._hb = cycle_start   # 心跳: 看门狗据此判断循环是否卡死
            try:
                t_mark = time.time()
                now = time.time()
                tgt = self._target_fn(now + lead_s) if self._target_fn else None
                t_target = time.time() - t_mark
                if tgt is None:
                    print("[track] 目标位置计算失败 (TLE/SGP4), 停止跟踪", flush=True)
                    self.send_dir("stop")
                    break
                az, el = tgt["azimuth"], tgt["elevation"]

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

                t_mark = time.time()
                pan, tilt, enc_ok = self.get_position()
                t_pos = time.time() - t_mark
                if not enc_ok:
                    # 编码器失联: 开环惯性推算 (卫星角速度, 不依赖 PTZ 速度)
                    _max_dr = 3.0   # 开环最长 3s, 超时停转防盲转
                    if self._dr_start_pan is not None and self._dr_start_dir != 0:
                        now = time.time()
                        dr_elapsed = now - self._dr_start_time
                        if dr_elapsed > _max_dr:
                            print(f"[track] 开环推算超时 {dr_elapsed:.0f}s, 停止", flush=True)
                            if self._pan_moving:
                                self._pan_moving = False
                                self.send_dir("stop")
                            stop_ev.wait(0.1)
                            continue
                        # 用卫星角速度估算移, 不是用 PTZ 速度 (PTZ 有预测停并非一直运动)
                        pan = (self._dr_start_pan + az_rate * dr_elapsed) % 360.0
                        # 开环驱动: 保持最后方向, 不管 daz
                        dir_str = "right" if self._dr_start_dir > 0 else "left"
                        if not self._pan_moving:
                            self._pan_moving = True
                        self.send_dir(dir_str)
                        # 开环期间跳过 daz/pan_mode 等反馈逻辑, 仅处理 tilt
                        _dr_active = True
                    else:
                        if self._pan_moving:
                            self._pan_moving = False
                            self.send_dir("stop")
                        stop_ev.wait(0.1)
                        continue
                else:
                    # 编码器正常: 标记非开环 (状态保存移到 daz 计算之后,
                    # 此处引用 daz 会 UnboundLocalError 且被 except 吞掉 -> 云台不动无日志)
                    _dr_active = False
                daz = self._pan_delta(az_target, pan)
                del_ = el_target - tilt
                if enc_ok:
                    # 保存状态供开环推算使用 (daz 已定义)
                    self._dr_start_pan = pan
                    self._dr_start_time = now
                    self._dr_start_dir = 1 if daz > 0 else -1 if daz < 0 else 0

                # ---- 俯仰: 死区 + 步进 (无编码器, 只能按时长驱动) ----
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

                if not _dr_active:
                    # ---- 水平: 死区 + 持续移动 + 预测停 (AS5600 闭环, 恒速 7.5°/s) ----
                    adaz = abs(daz)
                    if self._pan_moving and adaz <= stop_lead:
                        pan_mode = "stop"
                    elif adaz <= deadzone:
                        pan_mode = "stop"
                    else:
                        pan_mode = "move"
                    if (pan >= self.pan_max and daz > 0) or (pan <= self.pan_min and daz < 0):
                        pan_mode = "stop"
                    pan_dir = "right" if daz > 0 else "left"
                    self._dr_start_dir = 1 if daz > 0 else -1 if daz < 0 else 0

                    if pan_mode != "stop" or tilt_dir is not None:
                        # 水平或俯仰任一在动即打印 (此前要求俯仰在动才打,
                        # 卫星在地平线下 el<0 时水平驱动无日志, 形成观测盲区)
                        print(f"[track] az={az:.1f} pan={pan:.1f} daz={daz:.1f} "
                              f"az_rate={az_rate:.2f} el={el:.1f} tilt={tilt:.1f} del={del_:.1f} "
                              f"el_rate={el_rate:.2f} pan={pan_mode} tilt={tilt_dir}", flush=True)

                    # ---- 发送指令 ----
                    if pan_mode == "move":
                        self._pan_moving = True
                        t_mark = time.time()
                        self.send_dir(tilt_dir + pan_dir if tilt_dir is not None else pan_dir)
                        t_send = time.time() - t_mark
                        if max(t_target, t_pos, t_send) > 0.3:
                            print(f"[track] SLOW tgt={t_target:.2f}s pos={t_pos:.2f}s "
                                  f"send={t_send:.2f}s", flush=True)
                    elif tilt_dir is not None:
                        self._pan_moving = False
                        speed = self.tilt_up_speed if tilt_dir == "up" else self.tilt_down_speed
                        self._drive(tilt_dir, tilt_step / speed, stop_ev=stop_ev)
                        self.send_dir("stop")
                        continue
                    else:
                        self._pan_moving = False
                        self.send_dir("stop")
                else:
                    # 开环期间: 仅处理俯仰 (水平已由上面开环路径驱动)
                    if tilt_dir is not None:
                        speed = self.tilt_up_speed if tilt_dir == "up" else self.tilt_down_speed
                        self._drive(tilt_dir, tilt_step / speed, stop_ev=stop_ev)
                        self.send_dir("stop")
                        continue
            except Exception as e:  # noqa: BLE001
                # 静默吞异常会导致"跟踪在跑但云台不动且无日志"的诊断黑洞
                print(f"[track] 循环异常: {e!r}", flush=True)
            elapsed = time.time() - cycle_start
            remaining = cycle - elapsed
            if remaining > 0:
                stop_ev.wait(remaining)
