#!/usr/bin/env python3
"""UDP 实时嗅探器: 逐包显示 ESP8266 发往 8091 的广播报文原始数据。

与 ptz 主服务同端口共存 (SO_REUSEADDR, 广播包双方都能收到)。
用法:
  python3 udp_sniffer.py                    # 前台实时显示, Ctrl+C 停止
  nohup python3 udp_sniffer.py &            # 后台运行, 输出同时写日志文件
输出: 控制台实时逐包 + udp_sniff.log (20MB 轮转 x2)
"""
import json
import os
import signal
import socket
import time

PORT = 8091
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "udp_sniff.log")
LOG_MAX = 20 * 1024 * 1024

_stats = {"total": 0, "zero": 0, "drop": 0, "t0": time.time(), "last": 0.0,
          "last_angle": None, "rate_n": 0, "rate_t": time.time(), "rate": 0.0}


def _out(line: str):
    print(line, flush=True)
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX:
            os.replace(LOG_FILE, LOG_FILE + ".1")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _fmt_gap(gap: float) -> str:
    if gap >= 1.0:
        return f"\x1b[31m断流{gap:.2f}s\x1b[0m"
    return f"{gap*1000:.0f}ms"


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", PORT))
    sock.settimeout(1.0)
    _out(f"=== 实时嗅探启动 {time.strftime('%F %T')} port={PORT} pid={os.getpid()} ===")

    running = [True]

    def _stop(sig, frm):
        running[0] = False
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while running[0]:
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            _out(f"[{time.strftime('%T')}] ... 1s 无报文")
            continue
        now = time.time()
        s = _stats
        s["total"] += 1
        gap = (now - s["last"]) if s["last"] else 0.0
        s["last"] = now
        # 5s 窗口速率
        if now - s["rate_t"] >= 5:
            s["rate"] = s["rate_n"] / (now - s["rate_t"])
            s["rate_n"] = 0
            s["rate_t"] = now
        s["rate_n"] += 1
        raw = data.decode("utf-8", errors="replace")
        ts = time.strftime("%T") + f".{int(now%1*1000):03d}"
        try:
            j = json.loads(raw)
        except ValueError:
            _out(f"[{ts}] {addr[0]} 非JSON({len(data)}B): {raw[:120]!r}")
            continue
        if j.get("event") == "zero":
            s["zero"] += 1
            _out(f"[{ts}] {addr[0]} \x1b[33m★光电触发★\x1b[0m angle={j.get('angle')} raw={j.get('raw')} "
                 f"tick={j.get('tick')} (总触发={s['zero']})")
            s["last_angle"] = j.get("angle")
            continue
        angle = j.get("angle")
        d = ""
        if angle is not None and s["last_angle"] is not None:
            try:
                dd = float(angle) - float(s["last_angle"])
                d = f" Δ={dd:+.2f}"
                if abs(dd) > 300:   # unwrap 圈跳嫌疑
                    d += "\x1b[35m<圈跳>\x1b[0m"
            except (TypeError, ValueError):
                pass
        s["last_angle"] = angle
        _out(f"[{ts}] {addr[0]} angle={angle}{d} raw={j.get('raw')} "
             f"间隔={_fmt_gap(gap)} 速率={s['rate']:.1f}/s 总收={s['total']}")
    _out(f"=== 停止 {time.strftime('%F %T')} 总收={s['total']} 光电={s['zero']} ===")


if __name__ == "__main__":
    main()
