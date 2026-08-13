import socket, re, time

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('', 8091))
print('[UDP 8091 raw 实时监控] Ctrl+C 停止')

last_raw = None
last_t = time.time()

while True:
    try:
        data, addr = s.recvfrom(1024)
        txt = data.decode('utf-8', 'ignore')
        m = re.search(r'"raw":(\d+)', txt)
        if m:
            raw = int(m.group(1))
            now = time.time()
            if last_raw is None or raw != last_raw:
                dt = now - last_t
                delta = raw - (last_raw or raw)
                print(f't={now:.1f}s raw={raw:4d}  delta={delta:+4d}  dt={dt:.2f}s', flush=True)
                last_raw = raw
                last_t = now
    except Exception as e:
        print('[err]', e, flush=True)
        time.sleep(0.5)
