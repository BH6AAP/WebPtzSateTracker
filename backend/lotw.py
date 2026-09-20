"""
LoTW 日志拉取 + VUCC 网格统计
- 从 ARRL LoTW 下载已确认 QSO (ADIF 格式)
- 按频段统计已确认的梅登海德网格 (4 字符定位格, VUCC 计分标准)
- 缓存到 lotw_cache.json, 供前端地图叠加层显示

LoTW 下载端点 (官方):
https://lotw.arrl.org/lotwuser/lotwreport.adi?login=<call>&password=<pw>&qso_query=1&qso_qsl=yes&qso_qslsince=<date>
- qso_qsl=yes  → 只返回已确认 QSL 记录
- qso_qslsince → 只返回该日期之后的 QSL (LoTW 对全量查询返回 503, 必须用增量窗口)
- qso_qsldetail=yes → 返回 GRIDSQUARE/VUCC_GRIDS (否则无网格数据)
- 首次拉取用 1 年前窗口, 之后每次用上次成功拉取的时间做增量
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_FILE = os.path.join(BASE_DIR, "lotw_cache.json")

LOTW_URL = "https://lotw.arrl.org/lotwuser/lotwreport.adi"
FETCH_TIMEOUT = 60
FETCH_COOLDOWN = 30
INITIAL_SINCE_DAYS = 400   # 首次拉取窗口: 约 1 年前起 (LoTW 503 限流, 不能全量)

BAND_KEYS = {"6M": "6m", "2M": "2m", "70CM": "70cm", "SAT": "sat"}
FETCH_BANDS = ["6M", "2M", "70CM"]  # 卫星 QSO 在 2M/70CM 查询中带 SAT_NAME 返回, 无需单独拉取

_lock = threading.Lock()
_status = {"state": "idle", "msg": "", "updated_at": 0.0}
_cache = {"bands": {}, "qso_count": 0, "since": ""}
_last_fetch_at = 0.0


def _load_cache():
    global _cache, _status
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("bands"), dict):
                _cache = {"bands": data["bands"], "qso_count": data.get("qso_count", 0),
                          "since": data.get("since", "")}
                _status["updated_at"] = data.get("updated_at", 0)
    except Exception:  # noqa: BLE001
        pass


def _save_cache():
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"bands": _cache["bands"], "qso_count": _cache["qso_count"],
                       "since": _cache.get("since", ""),
                       "updated_at": _status["updated_at"]}, f,
                      ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


def get_vucc() -> dict:
    """返回缓存的 VUCC 数据 (前端渲染用)"""
    with _lock:
        return {"bands": dict(_cache["bands"]),
                "qso_count": _cache["qso_count"],
                "since": _cache.get("since", ""),
                "updated_at": _status["updated_at"]}


def get_status() -> dict:
    with _lock:
        return dict(_status)


def _fetch_adif(callsign: str, password: str, band: str, since: str) -> str:
    """按频段下载 LoTW ADIF, 返回文本; 凭据错误/网络异常抛异常"""
    params = urllib.parse.urlencode({
        "login": callsign,
        "password": password,
        "qso_query": "1",
        "qso_qsl": "yes",
        "qso_band": band,
        "qso_qslsince": since,
        "qso_qsldetail": "yes",        # 否则不返回 GRIDSQUARE/VUCC_GRIDS 字段
    })
    req = urllib.request.Request(f"{LOTW_URL}?{params}",
                                 headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_adif(text: str) -> tuple:
    """解析 LoTW ADIF, 返回 (bands: {key: set(4字符网格)}, qso_count)"""
    bands = {k: set() for k in BAND_KEYS.values()}
    qso_count = 0
    # ADIF 记录以 <EOR> 结尾; 字段形如 <TAG:len>值
    for rec in re.split(r"<EOR>", text, flags=re.I):
        fields = {}
        for m in re.finditer(r"<([A-Z0-9_]+):(\d+)>([^<]*)", rec, flags=re.I):
            tag, ln, val = m.group(1).upper(), int(m.group(2)), m.group(3)
            fields[tag] = val[:ln]
        if "BAND" not in fields:
            continue
        # 卫星 QSO 带 SAT_NAME, VUCC 中单独计分 (sat 类别)
        if fields.get("SAT_NAME"):
            band_key = "sat"
        else:
            band_key = BAND_KEYS.get(fields["BAND"].upper())
        if not band_key:
            continue
        if fields.get("QSL_RCVD", "Y").upper() != "Y":
            continue
        grids = set()
        g = fields.get("GRIDSQUARE", "").strip().upper()
        if g:
            grids.add(g[:4])  # VUCC 以 4 字符定位格计
        for vg in fields.get("VUCC_GRIDS", "").split(","):
            vg = vg.strip().upper()
            if len(vg) >= 4:
                grids.add(vg[:4])
        if not grids:
            continue
        bands[band_key] |= grids
        qso_count += 1
    return {k: sorted(v) for k, v in bands.items()}, qso_count


def fetch_lotw(callsign: str, password: str, force: bool = False) -> dict:
    """增量拉取 LoTW 日志并入缓存 (后台线程调用, 不阻塞请求线程)"""
    global _last_fetch_at, _cache, _status
    if not force:
        with _lock:
            if time.time() - _last_fetch_at < FETCH_COOLDOWN:
                return dict(_status)
    with _lock:
        _status.update(state="fetching", msg="正在拉取 LoTW 日志...")
        _last_fetch_at = time.time()
    try:
        prev_since = _cache.get("since", "")
        since = prev_since or time.strftime("%Y-%m-%d",
                                            time.gmtime(time.time() - INITIAL_SINCE_DAYS * 86400))
        merged = {k: set(v) for k, v in _cache["bands"].items()}
        qso_total = _cache["qso_count"]
        for adif_band in FETCH_BANDS:
            text = _fetch_adif(callsign, password, adif_band, since)
            if "<EOH>" not in text.upper():
                raise ValueError("LoTW 返回非 ADIF (登录凭据错误?)")
            bands, cnt = _parse_adif(text)
            for k, v in bands.items():
                merged.setdefault(k, set()).update(v)
            qso_total += cnt
        with _lock:
            _cache = {"bands": {k: sorted(v) for k, v in merged.items()},
                      "qso_count": qso_total,
                      "since": time.strftime("%Y-%m-%d")}
            _status.update(state="ok", msg=f"拉取完成: 新增 {qso_total} 个已确认 QSO", updated_at=time.time())
            _save_cache()
    except Exception as e:  # noqa: BLE001
        with _lock:
            _status.update(state="error", msg=f"拉取失败: {e}")
    with _lock:
        return dict(_status)


def start_fetch(callsign: str, password: str):
    """后台线程拉取 LoTW, 前端轮询状态"""
    threading.Thread(target=fetch_lotw, args=(callsign, password), daemon=True).start()


_load_cache()