# -*- coding: utf-8 -*-
"""
Hamlib rotctld protocol TCP server.
Lets SkyRoof and other rotctld-capable software
control the PTZ over TCP directly.
"""
from __future__ import annotations

import logging
import socket
import socketserver
import threading
import time
from typing import Callable, Optional

_logger = logging.getLogger(__name__)


class RotctldHandler(socketserver.StreamRequestHandler):
    timeout = 10.0
    _last_command_time = 0.0

    def setup(self):
        super().setup()
        _logger.info("[rotctld] new connection: %s", self.client_address)

    def handle(self):
        while True:
            try:
                data = self.rfile.readline()
                if not data:
                    break
            except socket.timeout:
                continue
            except ConnectionError:
                break
            try:
                line = data.decode("ascii").strip()
            except (UnicodeDecodeError, UnicodeEncodeError):
                continue
            if not line:
                continue
            _logger.info("[rotctld] <- %s", line)
            reply = self._process(line)
            if reply is None:
                break
            self.wfile.write(reply)
            self.wfile.flush()

    def _process(self, line):
        RotctldHandler._last_command_time = time.time()
        parts = line.split(maxsplit=1)
        verb = parts[0] if parts else ""
        if verb == "p":
            return self._handle_get_pos()
        elif verb == "P":
            return self._handle_set_pos(parts)
        elif verb == "S":
            return self._handle_stop()
        elif verb == "q":
            return None
        elif verb == chr(92) + "dump_state":
            return self._handle_dump_state()
        elif verb == chr(92) + "get_info":
            return self._handle_get_info()
        elif verb == "":
            return b""
        else:
            return b"RPRT -1" + chr(10).encode()

    def _handle_get_pos(self):
        try:
            pos = self.rotctld._get_position()
            az = str(round(pos.get("pan", 0.0), 1)).encode()
            el = str(round(pos.get("tilt", 0.0), 1)).encode()
            self.wfile.write(az + chr(10).encode())
            self.wfile.flush()
            self.wfile.write(el + chr(10).encode())
            self.wfile.flush()
            return b""
        except Exception as e:
            _logger.error("[rotctld] get_pos failed: %s", e)
            return b"RPRT -1" + chr(10).encode()

    def _handle_set_pos(self, parts):
        if len(parts) < 2:
            return b"RPRT -1" + chr(10).encode()
        args = parts[1].split()
        if len(args) < 2:
            return b"RPRT -1" + chr(10).encode()
        try:
            az = float(args[0])
            el = float(args[1])
            self.rotctld._set_position(az, el)
            return b"RPRT 0" + chr(10).encode()
        except (ValueError, TypeError) as e:
            _logger.warning("[rotctld] set_pos invalid: %s: %s", parts[1], e)
            return b"RPRT -1" + chr(10).encode()

    def _handle_stop(self):
        try:
            self.rotctld._stop_motion()
            return b"RPRT 0" + chr(10).encode()
        except Exception as e:
            _logger.error("[rotctld] stop failed: %s", e)
            return b"RPRT -1" + chr(10).encode()

    def _handle_dump_state(self):
        try:
            pos = self.rotctld._get_position() or {"pan": 0, "tilt": 0}
            return (
                b"protocol: 1" + chr(10).encode()
                + b"azimuth: " + str(round(pos.get("pan", 0.0), 1)).encode() + chr(10).encode()
                + b"elevation: " + str(round(pos.get("tilt", 0.0), 1)).encode() + chr(10).encode()
                + b"model: YD3040 (Pelco-D via WebPtzSateTracker)" + chr(10).encode()
            )
        except Exception:
            return b"protocol: 1" + chr(10).encode() + b"model: YD3040" + chr(10).encode()

    def _handle_get_info(self):
        return (
            b"Rotator model:       YD3040 (via WebPtzSateTracker)" + chr(10).encode()
            + b"Backend interface:   Pelco-D (Serial/RS485)" + chr(10).encode()
            + b"Rotctld backend:   WebPtzSateTracker (Python)" + chr(10).encode()
            + b"Azimuth range:       0.0 to 360.0 deg" + chr(10).encode()
            + b"Elevation range:     0.0 to 90.0 deg" + chr(10).encode()
        )

    def finish(self):
        _logger.info("[rotctld] disconnected: %s", self.client_address)


class RotctldServer:
    """rotctld TCP server, receives callbacks from main.py"""

    def __init__(
        self,
        get_position: Callable[[], dict],
        set_position: Callable[[float, float], None],
        stop_motion: Callable[[], None],
        host: str = "0.0.0.0",
        port: int = 4533,
    ):
        self._get_position = get_position
        self._set_position = set_position
        self._stop_motion = stop_motion
        self.host = host
        self.port = port
        self._server = None
        self._thread = None

    @property
    def connected(self):
        return time.time() - RotctldHandler._last_command_time < 30.0

    def start(self):
        class _Handler(RotctldHandler):
            pass
        _Handler.rotctld = self
        self._server = socketserver.ThreadingTCPServer(
            (self.host, self.port), _Handler
        )
        self._server.allow_reuse_address = True
        self._server.timeout = 1.0
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="rotctld",
        )
        self._thread.start()
        _logger.info("[rotctld] listening on %s:%d", self.host, self.port)

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        _logger.info("[rotctld] stopped")