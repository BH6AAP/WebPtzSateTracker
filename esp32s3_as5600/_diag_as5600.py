# 临时诊断: SCL=8 SDA=9, 扫描 + 连续读 raw
from machine import Pin, SoftI2C
import time

AS5600_ADDR = 0x36
i2c = SoftI2C(sda=Pin(9), scl=Pin(8), freq=400000)

devs = i2c.scan()
print("scan:", [hex(d) for d in devs])

try:
    i2c.writeto(AS5600_ADDR, bytes([0x0B]))
    st = i2c.readfrom(AS5600_ADDR, 1)[0]
    print("STATUS=0x%02X MD:%d ML:%d MH:%d" % (st, st & 1, (st >> 1) & 1, (st >> 2) & 1))
except Exception as e:
    print("STATUS err:", e)

for i in range(15):
    try:
        i2c.writeto(AS5600_ADDR, bytes([0x0E]))
        d = i2c.readfrom(AS5600_ADDR, 2)
        raw = (d[0] << 8) | d[1]
        print("raw=%d (0x%04X)" % (raw, raw))
    except Exception as e:
        print("read err:", e)
    time.sleep_ms(100)
