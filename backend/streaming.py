"""SSE 推流: 一条长连接合并推送状态帧/串口日志/收藏列表/光电标定,
替代前端多路 HTTP 轮询 (J1900 上 ~6 req/s 常驻 → 1 条连接)。
帧函数由 main.py 注入, 本模块只负责 SSE 协议与节拍控制。
"""
from __future__ import annotations

import json
import time

from flask import Response

STATE_INTERVAL = 0.4      # 状态帧 (串口状态/云台位置/目标位置/编码器角度) — 角度也走 SSE, 外网/内网统一实时
ENC_INTERVAL = 0.1        # 编码器角度帧 (高频: 大数字角度实时跟随, 不随低频状态帧阶梯跳变)
SERIAL_INTERVAL = 2.5      # 串口日志帧 (200 条完整列表较大, 降频避免慢链路下大帧阻塞状态帧到达)
FAVORITES_INTERVAL = 30.0  # 收藏列表帧
PHOTOCALIB_INTERVAL = 1.0  # 光电标定状态帧


def _sse(event: str, payload) -> str:
    """构造一帧 SSE 数据 (紧凑 JSON)"""
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


def make_stream_response(frame_fns: dict) -> Response:
    """构造 SSE 流响应。

    frame_fns: {事件名: (帧函数, 最小周期秒)}; 帧函数返回 None 则本拍跳过。
    每帧函数异常被吞掉 (返回 None), 单帧失败不影响流存活。
    客户端断开时 waitress 关闭生成器 → GeneratorExit 退出, 不产生失联线程。

    反缓冲头说明 (外网经 Cloudflare Tunnel 访问时关键):
    - Cache-Control: no-cache, no-transform  → 禁止代理压缩/缓冲 SSE 数据块
    - X-Accel-Buffering: no                  → 让 nginx 类反代不缓冲
    - 每拍发 `: ping` 注释行, 保持数据持续流动, 防代理等到攒够一批才转发
    """
    last = {name: 0.0 for name in frame_fns}

    def gen():
        try:
            while True:
                now = time.time()
                for name, (fn, interval) in frame_fns.items():
                    if now - last[name] < interval:
                        continue
                    last[name] = now
                    try:
                        data = fn()
                    except Exception:  # noqa: BLE001
                        data = None
                    if data is not None:
                        yield _sse(name, data)
                # 持续小流量 + 注释帧: 穿透 Cloudflare/nginx 缓冲
                yield ": ping\n\n"
                time.sleep(0.2)
        except GeneratorExit:
            pass

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache, no-transform",
                             "X-Accel-Buffering": "no"})
