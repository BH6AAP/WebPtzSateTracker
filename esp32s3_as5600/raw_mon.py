# 实时监控 AS5600 raw 值 (独立脚本, 不依赖 main.py)
import time
from machine import SoftI2C, Pin

ADDR = 0x36
REG_ANGLE = 0x0C
REG_STATUS = 0x0B

i2c = SoftI2C(sda=Pin(9), scl=Pin(8), freq=400000)

while True:
    try:
        i2c.writeto(ADDR, bytes([REG_STATUS]))
        st = i2c.readfrom(ADDR, 1)[0]
        i2c.writeto(ADDR, bytes([REG_ANGLE]))
        d = i2c.readfrom(ADDR, 2)
        raw = (d[0] << 8) | d[1]
        angle = (raw * 360.0) / 4096.0
        md = (st >> 5) & 1
        ml = (st >> 4) & 1
        mh = (st >> 3) & 1
        print("raw=%4d angle=%7.2f status=0x%02x MD=%d ML=%d MH=%d" % (raw, angle, st, md, ml, mh))
    except Exception as e:
        print("read err:", e)
    time.sleep_ms(100)
