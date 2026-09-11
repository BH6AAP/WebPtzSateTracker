# -*- coding: utf-8 -*-
"""ESP8266 AS5600 UDP (8091) 实时监看: angle/raw/rssi/速率/丢包率
用法: python3 raw_udp_mon.py   (Ctrl+C 停止)
丢包率: 按滑动窗口(1s)内 tick 增量推算发送速率, 设备重启 tick 回退时自动重置
"""
import socket, json, time

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('', 8091))
s.settimeout(2)
print('[UDP 8091 实时监看] 等待数据... Ctrl+C 停止', flush=True)

total = 0
window_pkts = 0
window_start = time.time()
window_first_tick = None
last_tick = None
last_angle = None
last_report = time.time()
zero_count = 0
zero_last_t = None

while True:
    try:
        data, addr = s.recvfrom(1024)
    except socket.timeout:
        print('[无数据] ESP8266 离线?', flush=True)
        window_pkts = 0
        window_first_tick = None
        window_start = time.time()
        continue
    total += 1
    window_pkts += 1
    try:
        d = json.loads(data.decode())
    except Exception:
        continue
    tick = d.get('tick')
    if tick is not None:
        if window_first_tick is None or (last_tick is not None and tick < last_tick):
            window_first_tick = tick  # 窗口起点 / 设备重启(tick回退)重置
        last_tick = tick
    if d.get('event') == 'zero':
        # 光电传感器报告: 累计次数/触发时刻/距上次间隔/事件详情
        zero_count += 1
        now0 = time.time()
        gap = (now0 - zero_last_t) if zero_last_t is not None else None
        zero_last_t = now0
        print(f'══ 光电传感器报告 #{zero_count} ══', flush=True)
        print(f'  ┌ 触发时间: {time.strftime("%H:%M:%S", time.localtime(now0))}', flush=True)
        print(f'  ├ 距上次:   {gap:.1f}s' if gap is not None else '  ├ 距上次:   (首次)', flush=True)
        print(f'  ├ AS5600 raw: {d.get("raw")}  angle: {d.get("angle")}', flush=True)
        print(f'  └ WiFi rssi: {d.get("rssi")} dBm (挡片经光电窗口)', flush=True)
    now = time.time()
    if now - last_report >= 1.0:  # 每秒一行
        dt = now - window_start
        rx_hz = window_pkts / dt if dt > 0 else 0.0
        tx_hz = 0.0
        if window_pkts > 1 and window_first_tick is not None and last_tick is not None:
            span_ms = last_tick - window_first_tick
            if span_ms > 0:
                tx_hz = (window_pkts - 1) / span_ms * 1000.0
        loss = max(0.0, (1 - rx_hz / tx_hz) * 100) if tx_hz > 0 else 0.0
        rssi = d.get('rssi', '?')
        grade = ('优' if rssi > -55 else '良' if rssi > -67 else '中' if rssi > -75 else '差') if isinstance(rssi, int) else '?'
        angle = d.get('angle')
        da = f'{angle - last_angle:+.2f}' if (angle is not None and last_angle is not None) else '--'
        print(f'[{time.strftime("%H:%M:%S")}] {addr[0]} angle={angle} Δ={da} raw={d.get("raw")} '
              f'rssi={rssi}dBm({grade}) 收={rx_hz:.1f}Hz 发≈{tx_hz:.1f}Hz 丢包≈{loss:.0f}% '
              f'光电触发={zero_count}次 总收={total}', flush=True)
        if angle is not None:
            last_angle = angle
        last_report = now
        window_pkts = 0
        window_first_tick = None
        window_start = now
