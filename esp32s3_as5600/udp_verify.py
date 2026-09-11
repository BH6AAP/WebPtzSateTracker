# UDP 实时监听 AS5600 数据 (无限循环, 持续打印 angle/raw)
# 用法: python3 udp_verify.py
import socket, re

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
s.bind(('', 8091))
s.settimeout(3)
print('[UDP 实时监听] 端口 8091, 持续打印, Ctrl+C 退出')
try:
    while True:
        try:
            data, addr = s.recvfrom(1024)
            txt = data.decode('utf-8', 'ignore')
            m = re.search(r'"angle":(-?[\d.]+),"raw":(\d+)', txt)
            if m:
                print('angle=%s raw=%s' % (m.group(1), m.group(2)))
        except socket.timeout:
            print('[等待中] 未收到数据包...')
except KeyboardInterrupt:
    print('\n[退出]')
