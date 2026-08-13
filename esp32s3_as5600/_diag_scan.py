# 临时诊断: 只测试 2 组最稳妥的 SCL/SDA 组合 (避开会触发复位的引脚)
from machine import Pin, SoftI2C
import time

AS5600_ADDR = 0x36
COMBOS = [
    (11, 12),  # 默认
    (12, 11),  # 对调
]

def read_raw(i2c):
    try:
        i2c.writeto(AS5600_ADDR, bytes([0x0E]))
        d = i2c.readfrom(AS5600_ADDR, 2)
        return (d[0] << 8) | d[1]
    except Exception:
        return None

for scl, sda in COMBOS:
    try:
        i2c = SoftI2C(sda=Pin(sda), scl=Pin(scl), freq=400000)
        devs = i2c.scan()
        if AS5600_ADDR in devs and len(devs) == 1:
            print("HIT  SCL=%d SDA=%d -> raw=%s" % (scl, sda, read_raw(i2c)))
        elif devs:
            print("SCL=%d SDA=%d -> %d devs (总线异常)" % (scl, sda, len(devs)))
        else:
            print("SCL=%d SDA=%d -> no devices" % (scl, sda))
    except Exception as e:
        print("SCL=%d SDA=%d -> ERR %r" % (scl, sda, e))
    time.sleep_ms(30)
