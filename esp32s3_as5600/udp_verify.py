import socket, re
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
s.bind(('', 8091))
s.settimeout(5)
print('[UDP 验证] 等待数据包...')
try:
    for i in range(5):
        data, addr = s.recvfrom(1024)
        txt = data.decode('utf-8', 'ignore')
        m = re.search(r'"angle":(-?[\d.]+),"raw":(\d+)', txt)
        if m:
            print('angle=%s raw=%s' % (m.group(1), m.group(2)))
except socket.timeout:
    print('[失败] 未收到 UDP 数据包')
