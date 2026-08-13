"""
Pelco-D 云台协议实现
帧格式: FF add cmd1 cmd2 data1 data2 sum
sum = 除 0xFF 外其余字节和的低 8 位
"""
from __future__ import annotations


class PelcoD:
    """Pelco-D 指令构造与解析"""

    SYNC = 0xFF

    def __init__(self, address: int = 1):
        self.address = address & 0xFF

    def _frame(self, cmd1: int, cmd2: int, data1: int, data2: int) -> bytes:
        """构造一帧 Pelco-D 指令"""
        add = self.address
        body = bytes([add, cmd1, cmd2, data1, data2])
        checksum = (sum(body)) & 0xFF
        return bytes([self.SYNC]) + body + bytes([checksum])

    # ---------- 基本运动指令 ----------
    def up(self, speed: int = 0x20) -> bytes:
        return self._frame(0x00, 0x08, 0x00, speed & 0x3F)

    def down(self, speed: int = 0x20) -> bytes:
        return self._frame(0x00, 0x10, 0x00, speed & 0x3F)

    def left(self, speed: int = 0x20) -> bytes:
        return self._frame(0x00, 0x04, speed & 0x3F, 0x00)

    def right(self, speed: int = 0x20) -> bytes:
        return self._frame(0x00, 0x02, speed & 0x3F, 0x00)

    def stop(self) -> bytes:
        return self._frame(0x00, 0x00, 0x00, 0x00)

    # ---------- 斜向运动指令 ----------
    # tilt_speed: 可选, 单独指定俯仰速度 (默认与水平同速)
    def up_left(self, speed: int = 0x20, tilt_speed: int = None) -> bytes:
        if tilt_speed is None:
            tilt_speed = speed
        return self._frame(0x00, 0x0C, speed & 0x3F, tilt_speed & 0x3F)

    def up_right(self, speed: int = 0x20, tilt_speed: int = None) -> bytes:
        if tilt_speed is None:
            tilt_speed = speed
        return self._frame(0x00, 0x0A, speed & 0x3F, tilt_speed & 0x3F)

    def down_left(self, speed: int = 0x20) -> bytes:
        return self._frame(0x00, 0x14, speed & 0x3F, speed & 0x3F)

    def down_right(self, speed: int = 0x20) -> bytes:
        return self._frame(0x00, 0x12, speed & 0x3F, speed & 0x3F)

    # ---------- 变焦指令 ----------
    def zoom_in(self) -> bytes:
        return self._frame(0x00, 0x20, 0x00, 0x00)

    def zoom_out(self) -> bytes:
        return self._frame(0x00, 0x40, 0x00, 0x00)

    # ---------- 扩展指令 ----------
    def set_preset(self, preset: int) -> bytes:
        return self._frame(0x00, 0x03, 0x00, preset & 0xFF)

    def call_preset(self, preset: int) -> bytes:
        return self._frame(0x00, 0x07, 0x00, preset & 0xFF)

    def delete_preset(self, preset: int) -> bytes:
        return self._frame(0x00, 0x05, 0x00, preset & 0xFF)

    def aux_on(self, aux: int = 1) -> bytes:
        return self._frame(0x00, 0x09, 0x00, aux & 0xFF)

    def aux_off(self, aux: int = 1) -> bytes:
        return self._frame(0x00, 0x0B, 0x00, aux & 0xFF)

    def reboot(self) -> bytes:
        return self._frame(0x00, 0x0F, 0x00, 0x00)

    # ---------- 位置查询 ----------
    def query_pan(self) -> bytes:
        return self._frame(0x00, 0x51, 0x00, 0x00)

    def query_tilt(self) -> bytes:
        return self._frame(0x00, 0x53, 0x00, 0x00)

    # ---------- 绝对角度控制 ----------
    def pan_absolute(self, angle: float) -> bytes:
        """水平绝对定位, (D1<<8)+D2 = angle*100"""
        value = int(round(angle * 100))
        value = max(0, min(36000, value))
        return self._frame(0x00, 0x4B, (value >> 8) & 0xFF, value & 0xFF)

    def tilt_absolute(self, angle: float) -> bytes:
        """
        垂直绝对定位
        负角: (D1<<8)+D2 = |angle|*100
        正角: (D1<<8)+D2 = 36000 - angle*100
        """
        if angle < 0:
            value = int(round(abs(angle) * 100))
        else:
            value = int(round(36000 - angle * 100))
        value = max(0, min(36000, value))
        return self._frame(0x00, 0x4D, (value >> 8) & 0xFF, value & 0xFF)

    # ---------- 位置回传解析 ----------
    @staticmethod
    def parse_pan_response(data: bytes) -> float | None:
        """解析水平位置回传: FF add 00 59 PMSB PLSB sum -> 角度"""
        if len(data) < 7 or data[0] != 0xFF or data[3] != 0x59:
            return None
        pmsb, plsb = data[4], data[5]
        pdata = pmsb * 256 + plsb
        return pdata / 100.0

    @staticmethod
    def parse_tilt_response(data: bytes) -> float | None:
        """解析垂直位置回传: FF add 00 5B TMSB TLSB sum -> 角度"""
        if len(data) < 7 or data[0] != 0xFF or data[3] != 0x5B:
            return None
        tmsb, tlsb = data[4], data[5]
        tdata1 = tmsb * 256 + tlsb
        if tdata1 > 18000:
            return (36000 - tdata1) / 100.0
        return -tdata1 / 100.0

    @staticmethod
    def checksum_valid(data: bytes) -> bool:
        """校验一帧数据的 checksum"""
        if len(data) < 7:
            return False
        body = data[1:6]
        return (sum(body) & 0xFF) == data[6]
