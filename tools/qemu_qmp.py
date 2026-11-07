#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Small local QMP client and lossless QEMU screen capture helpers."""
from __future__ import annotations

import json
from pathlib import Path
import re
import socket
import struct
import threading
import time
import zlib


def free_port():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


class QMP:
    def __init__(self, port, timeout=10):
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.socket = socket.create_connection(("127.0.0.1", port), timeout=timeout)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        self.stream = self.socket.makefile("rb")
        self.lock = threading.Lock()
        self.sequence = 0
        greeting = self._read()
        if "QMP" not in greeting:
            self.close()
            raise ValueError("QEMU did not send a QMP greeting")
        self.execute("qmp_capabilities")

    def _read(self):
        line = self.stream.readline()
        if not line:
            raise ConnectionError("QEMU closed the QMP connection")
        return json.loads(line)

    def execute(self, command, arguments=None):
        with self.lock:
            self.sequence += 1
            request = {"execute": command, "id": self.sequence}
            if arguments is not None:
                request["arguments"] = arguments
            self.socket.sendall(json.dumps(request).encode("utf-8") + b"\n")
            while True:
                response = self._read()
                if response.get("id") != self.sequence:
                    continue
                if "error" in response:
                    raise ValueError("QMP %s: %s" % (command, response["error"]))
                return response["return"]

    def close(self):
        self.stream.close()
        self.socket.close()


def read_ppm(data):
    header = re.match(rb"P6\s+(\d+)\s+(\d+)\s+255\n", data)
    if header is None:
        raise ValueError("Expected QEMU's P6/255 screenshot format")
    width, height = map(int, header.groups())
    pixels = data[header.end():]
    if not 0 < width <= 8192 or not 0 < height <= 8192 or len(pixels) != width * height * 3:
        raise ValueError("Screen dimensions and PPM payload length disagree")
    return width, height, pixels


def png_rgb(width, height, pixels):
    if len(pixels) != width * height * 3:
        raise ValueError("RGB payload does not match the PNG dimensions")

    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    scanlines = b"".join(b"\0" + pixels[y * width * 3:(y + 1) * width * 3] for y in range(height))
    header = struct.pack(">2I5B", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) +
            chunk(b"IDAT", zlib.compress(scanlines, 6)) + chunk(b"IEND", b""))


def capture(qmp, output):
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    ppm = output.with_suffix(".ppm")
    qmp.execute("screendump", {"filename": str(ppm), "format": "ppm"})
    width, height, pixels = read_ppm(ppm.read_bytes())
    output.write_bytes(png_rgb(width, height, pixels))
    return width, height, pixels
