"""
WebSocket 实时推送服务 (替代/并共享 SSE 事件流的 WS 通道)

背景: Cloudflare Tunnel 会对 SSE 流做缓冲/偶尔合并帧, 导致外网"部分不同步"。
本项目用 Python 3.14, gevent/eventlet 装不上(C 扩展不兼容), 无法把 waitress 换掉。
故用纯 Python 的 websockets(asyncio) 单独起一个 WS 服务, 与 waitress(HTTP/API/SSE)
并存在同一进程, 零 monkey-patch, 串口/rotctld/UDP 完全不受影响。

- 前端 WebSocket 优先连接本服务; 连不上时自动回退 SSE (/api/stream)。
- 帧数据与 SSE 状态帧完全一致, 前端分发逻辑完全复用。
- 鉴权: 握手路径 /ws。内网暴露; 外网经 Cloudflare 加路由暴露时建议配合
  Cloudflare Access 策略或在本 handler 加 token 校验(暂未内置, 保持简洁)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

import websockets
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

PING_INTERVAL = 8.0   # 心跳/持续连接, 防 Cloudflare/负载均衡空闲回收
_TICK = 0.2           # 帧调度最小节拍

# frame_factory: callable(target)->[(interval, name, fn)] ; fn() 返回当前快照(dict)
# check_auth: callable(cookie_str)->bool ; None=不鉴权
_frame_factory = None
_check_auth = None
_conn_counter = 0
_started = False
_start_lock = threading.Lock()


def _encode(name: str, data) -> str:
    return json.dumps({"t": name, "data": data}, default=str)


async def _pump(ws, frames):
    """按各自间隔推送各类型帧; 与 SSE 帧间隔一致"""
    next_run = [0.0] * len(frames)
    while True:
        now = time.monotonic()
        for i, (iv, name, fn) in enumerate(frames):
            if now >= next_run[i]:
                try:
                    await ws.send(_encode(name, fn()))
                except ConnectionClosed:
                    raise
                next_run[i] = now + iv
        await asyncio.sleep(_TICK)


async def _handler(ws):
    # 握手鉴权: 校验登录 Cookie, 未登录直接拒绝 (前端会自动回退 SSE)
    if _check_auth is not None:
        cookie = ws.request.headers.get("Cookie", "") or ""
        if not _check_auth(cookie):
            logger.info("[ws] reject unauthenticated %s", ws.remote_address)
            await ws.close(code=4401)
            return
    global _conn_counter
    _conn_counter += 1
    conn = _conn_counter
    # 从路径解析目标 (与 SSE 的 ?norad=/?celestial= 对齐)
    target = {"norad": None, "celestial": None}
    q = ws.request.path or "/ws"
    if "?" in q:
        for pair in q.split("?", 1)[1].split("&"):
            if "=" not in pair:
                continue
            k, v = pair.split("=", 1)
            if k == "norad":
                target["norad"] = v
            elif k == "celestial":
                target["celestial"] = v
    frames = _frame_factory(target) if _frame_factory else []
    logger.info("[ws] #%s connected path=%s frames=%d", conn, q, len(frames))
    pump = asyncio.create_task(_pump(ws, frames))
    try:
        async for msg in ws:   # 客户端一般不发消息; 保持连接
            pass
    except ConnectionClosed:
        pass
    finally:
        pump.cancel()
    logger.info("[ws] #%s closed", conn)


async def _serve(port: int):
    async with serve(_handler, "0.0.0.0", port, ping_interval=PING_INTERVAL):
        logger.info("[ws] listening on 0.0.0.0:%d", port)
        await asyncio.Future()


def ensure_started(port: int, frame_factory, check_auth=None):
    """幂等启动: 已在运行则直接返回, 供任意 HTTP 路由首次触发即拉起"""
    global _frame_factory, _check_auth, _started
    with _start_lock:
        _frame_factory = frame_factory
        _check_auth = check_auth
        if _started:
            return None
        _started = True
        return _launch(int(port))


def start(port: int, frame_factory, check_auth=None) -> threading.Thread:
    """显式启动(与 ensure_started 等效, 幂等)"""
    return ensure_started(port, frame_factory, check_auth)


def _launch(port: int) -> threading.Thread:
    def _run():
        try:
            asyncio.run(_serve(port))
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("[ws] server stopped")

    t = threading.Thread(target=_run, daemon=True, name="ws-push")
    t.start()
    return t