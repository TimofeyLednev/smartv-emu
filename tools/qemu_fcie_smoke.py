#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise the MStar FCIE host with synthetic eMMC data over QEMU's qtest.

Checks card enumeration, DMA reads/writes, event acknowledgement and the
board's interrupt bank. No Samsung firmware is loaded by this test.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import time

PROJECT = Path(__file__).resolve().parents[1]
FCIE = 0x1F222600
DMA_BUFFER = 0x40220000


class Qtest:
    def __init__(self, connection):
        self.connection = connection
        self.stream = connection.makefile("rb")

    def request(self, command):
        self.connection.sendall(command.encode("ascii") + b"\n")
        while True:
            line = self.stream.readline().decode("ascii").strip()
            if line.startswith("IRQ "):
                continue
            if line == "OK" or line.startswith("OK "):
                return line[2:].strip()
            raise ValueError("qtest command %r failed: %r" % (command, line))

    def read(self, index):
        return int(self.request("readl 0x%x" % (FCIE + 4 * index)), 0)

    def write(self, index, value):
        self.request("writel 0x%x 0x%x" % (FCIE + 4 * index, value))

    def command(self, number, argument=0, response=True, data=False, write=False):
        packet = bytes([0x40 | number]) + struct.pack(">I", argument) + b"\0"
        self.write(0x00, 0xFFFF)  # acknowledge previous completion
        self.write(0x12, 0xFFFF)  # acknowledge previous errors
        for index in range(0, len(packet), 2):
            self.write(0x60 + index // 2, int.from_bytes(packet[index:index + 2], "little"))
        self.write(0x11, 4 | (2 if response else 0) | (8 if data else 0) | (16 if write else 0))
        if not self.read(0x00) & 2:
            raise ValueError("CMD%d did not complete" % number)
        if self.read(0x12) & 0x1F:
            raise ValueError("CMD%d failed with status 0x%x" % (number, self.read(0x12)))
        # The recovered DMA worker waits for this exact four-event mask.
        if data and self.read(0x00) & 0x4805 != 0x4805:
            raise ValueError("CMD%d is missing the firmware's DMA completion events" % number)
        result = b"".join(struct.pack("<H", self.read(0x60 + i)) for i in range(9))
        return result[1:]

    def dma(self, blocks):
        offset = DMA_BUFFER - 0x40000000
        self.write(0x0B, blocks)
        self.write(0x1B, 512)
        self.write(0x1C, offset & 0xFFFF)
        self.write(0x1D, offset >> 16)

    def memory(self, size):
        return bytes.fromhex(self.request("read 0x%x 0x%x" % (DMA_BUFFER, size))[2:])


def run(qemu, output):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    fixture = output / "synthetic-emmc.raw"
    pattern = bytes((i * 37 + i // 512) & 255 for i in range(8192))
    with fixture.open("wb") as disk:
        disk.write(pattern)
        disk.truncate(8 * 1024 * 1024)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    command = [str(qemu.resolve()), "-machine", "samsung_mstar_ue40f6650",
               "-accel", "qtest", "-m", "128M", "-display", "none",
               "-monitor", "none", "-serial", "null", "-nic", "none",
               "-qtest", "tcp:127.0.0.1:%d,server=on,wait=off" % port,
               "-qtest-log", str(output / "qtest.log"),
               "-drive", "if=sd,format=raw,snapshot=on,file=" + str(fixture)]
    environment = os.environ.copy()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = {"scope": "Synthetic eMMC fixture; no Samsung firmware",
              "machine": "samsung_mstar_ue40f6650", "checks": [], "command": command}
    connection = None
    qtest = None
    with (output / "qemu-stderr.log").open("wb") as errors:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=errors,
                                   env=environment, creationflags=flags)
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise ValueError("QEMU exited before qtest connected")
                try:
                    connection = socket.create_connection(("127.0.0.1", port), timeout=3)
                    break
                except OSError:
                    time.sleep(0.05)
            if connection is None:
                raise ValueError("Timed out connecting to qtest")
            qtest = Qtest(connection)
            for direction in (0, 4):
                qtest.write(0x02, direction)
                for _ in range(3):
                    priority = qtest.read(0x02)
                    if priority & 0x8020 != 0x8020 or priority & 4 != direction:
                        raise ValueError("FCIE FIFO clock readiness or DMA direction is incorrect")
            qtest.write(0x02, 0)
            result["checks"].append("FIFO clock ready across repeated reads and both DMA directions")
            qtest.command(0, response=False)
            ocr = 0
            for _ in range(100):
                ocr = int.from_bytes(qtest.command(1, 0x40FF8000)[:4], "big")
                if ocr & 0x80000000:
                    break
                qtest.request("clock_step 1000000")
            if not ocr & 0x80000000 or not ocr & 0x00FF8000:
                raise ValueError("eMMC OCR is not ready: 0x%x" % ocr)
            cid = qtest.command(2)[:16]
            if not any(cid):
                raise ValueError("eMMC CID is empty")
            qtest.command(3, 0x10000)
            qtest.command(9, 0x10000)
            qtest.command(7, 0x10000)
            result["checks"].append("eMMC enumeration and OCR/CID/CSD responses")
            qtest.dma(1)
            qtest.command(8, data=True)
            ext = qtest.memory(512)
            if struct.unpack_from("<I", ext, 212)[0] != 8 * 1024 * 1024 // 512:
                raise ValueError("EXT_CSD DMA returned an incorrect sector count")
            result["checks"].append("EXT_CSD read by DMA")
            qtest.command(16, 512)
            qtest.dma(1)
            qtest.command(17, 0, data=True)
            if qtest.memory(512) != pattern[:512]:
                raise ValueError("Single-block read differs from the backing disk")
            result["checks"].append("single-block DMA read")
            qtest.dma(2)
            qtest.command(18, 512, data=True)
            if qtest.memory(1024) != pattern[512:1536]:
                raise ValueError("Multiple-block read differs from the backing disk")
            qtest.command(12)
            result["checks"].append("multiple-block DMA read and stop")
            payload = bytes((255 - i) & 255 for i in range(512))
            qtest.request("write 0x%x 0x200 0x%s" % (DMA_BUFFER, payload.hex()))
            qtest.dma(1)
            qtest.command(24, 4096, data=True, write=True)
            qtest.dma(1)
            qtest.command(17, 4096, data=True)
            if qtest.memory(512) != payload:
                raise ValueError("DMA write/readback failed")
            result["checks"].append("DMA write/readback in a temporary snapshot")
            qtest.request("writel 0x1f2032d0 0xffdf")  # IRQ 197: bank 4, bit 5
            qtest.write(0x01, 0x4000)
            if int(qtest.request("readl 0x1f2032f0"), 0) != 0x20:
                raise ValueError("FCIE completion did not reach the MStar interrupt bank")
            qtest.write(0x00, 0x4000)
            if int(qtest.request("readl 0x1f2032f0"), 0) != 0:
                raise ValueError("Acknowledged FCIE interrupt remained asserted")
            result["checks"].append("interrupt delivery and write-one-to-clear acknowledgement")
            result["status"] = "passed"
        except Exception as error:
            result.update(status="failed", error=str(error))
        finally:
            if qtest:
                qtest.stream.close()
            if connection:
                connection.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
    # snapshot=on must preserve the synthetic backing file, including writes.
    if fixture.read_bytes()[:8192] != pattern:
        result.update(status="failed", error="The backing fixture changed")
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("status", "checks", "error") if k in result}, indent=2))
    return 0 if result["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=PROJECT / "work/qemu-smoke/mstar-fcie")
    args = parser.parse_args()
    return run(args.qemu, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
