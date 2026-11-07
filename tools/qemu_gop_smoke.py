#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise banked GOP registers and verify real QMP scanout pixels.

Uses synthetic pixels without Samsung firmware. Checks bank isolation,
commit/force-write, padded stride, RGB565, mirrored address/direction,
disable, bounds and reset.
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

from qemu_fcie_smoke import Qtest
from qemu_qmp import QMP, capture, free_port

PROJECT = Path(__file__).resolve().parents[1]
GOP = 0x1F203E00
DIRECT_GOP = 0x1F240400
BUFFER = 0x51000000


def run(qemu, output, native=False, panel_rotate=False):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    qtest_port, qmp_port = free_port(), free_port()
    machine = "samsung_mstar_ue40f6650" + (",native-scanout=on" if native else "")
    if panel_rotate:
        machine += ",panel-rotate-180=on"
    command = [str(Path(qemu).resolve()), "-machine", machine,
               "-accel", "qtest", "-m", "128M", "-display", "none", "-monitor", "none",
               "-serial", "null", "-nic", "none",
               "-qtest", "tcp:127.0.0.1:%d,server=on,wait=off" % qtest_port,
               "-qtest-log", str(output / "qtest.log"),
               "-qmp", "tcp:127.0.0.1:%d,server=on,wait=off" % qmp_port]
    environment = os.environ.copy()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = {"scope": "Synthetic GOP fixtures; no Samsung firmware", "command": command,
              "checks": [], "status": "failed"}
    qmp, connection, qtest = None, None, None
    with (output / "qemu-stderr.log").open("wb") as errors:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=errors, env=environment, creationflags=flags)
        try:
            qmp = QMP(qmp_port)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    connection = socket.create_connection(("127.0.0.1", qtest_port), timeout=2)
                    break
                except OSError:
                    if process.poll() is not None:
                        raise ValueError("QEMU exited before qtest connected")
                    time.sleep(0.05)
            if connection is None:
                raise ValueError("Timed out connecting to qtest")
            qtest = Qtest(connection)

            def write(offset, value):
                qtest.request("writel 0x%x 0x%x" % (GOP + offset, value))

            def read(offset):
                return int(qtest.request("readl 0x%x" % (GOP + offset)), 0)

            def direct_write(bank, offset, value, width="l"):
                qtest.request("write%s 0x%x 0x%x" %
                              (width, DIRECT_GOP + bank * 0x200 + offset, value))

            def direct_read(bank, offset, width="l"):
                return int(qtest.request("read%s 0x%x" %
                                        (width, DIRECT_GOP + bank * 0x200 + offset)), 0)

            def select(bank):
                write(0x1fc, bank)

            def pixels(tag):
                return capture(qmp, output / (tag + ".png"))

            def blank(tag):
                if any(pixels(tag)[2]):
                    raise ValueError("Expected a black screen at " + tag)

            def expect_frame(tag, width, height, rgb, left=0, top=0):
                if native:
                    expected = (width, height, bytes(rgb))
                else:
                    panel = bytearray(1920 * 1080 * 3)
                    visible_width = min(width, 1920 - left)
                    for y in range(min(height, 1080 - top)):
                        start = ((top + y) * 1920 + left) * 3
                        panel[start:start + visible_width * 3] = rgb[y * width * 3:
                                                                      (y * width + visible_width) * 3]
                    if panel_rotate:
                        rotated = bytearray(len(panel))
                        for channel in range(3):
                            rotated[channel::3] = panel[channel::3][::-1]
                        panel = rotated
                    expected = (1920, 1080, bytes(panel))
                if pixels(tag) != expected:
                    raise ValueError("GOP output differs from the complete expected frame: " + tag)

            def layout(width, height, pitch, address=BUFFER, format=5, left=0, top=0,
                       mirror=0, read_offset=0):
                select(0)
                write(0, 8 | mirror)  # progressive general control
                write(0x1f8, 0x688)  # Mux stages select GOP 0, 1, 2, 3.
                select(1)
                write(0, 1 | (format << 4))
                write(0x28, 0xfc)  # GOP0 six-bit constant alpha: fully opaque.
                relative = address - 0x40000000
                write(4, (relative >> 5) & 0xffff)
                write(8, (relative >> 21) & 0x1ff)
                write(0x10, left * (4 if format == 5 else 2) // 32)
                write(0x14, (left + width) * (4 if format == 5 else 2) // 32)
                write(0x18, top)
                write(0x20, top + height)
                write(0x24, pitch // 32)
                write(0x30, read_offset >> 5 & 0xffff)
                write(0x34, read_offset >> 21 & 0xffff)

            def commit():
                write(0x1fc, 0xf101)
                if read(0x1fc) & 0xf000:
                    raise ValueError("GOP update busy bits were not acknowledged")
                write(0x1fc, 1)

            blank("reset-black")
            select(0)
            write(0x40, 0x1234)
            select(1)
            write(0x40, 0xabcd)
            select(0)
            if read(0x40) != 0x1234:
                raise ValueError("GOP bank 0 changed through a bank 1 write")
            select(1)
            if read(0x40) != 0xabcd:
                raise ValueError("GOP bank 1 did not retain its independent registers")
            result["checks"].append("bank isolation and global selector")
            for bank in range(16):
                direct_write(bank, 0x44, 0x12340000 + bank)
                select(bank)
                if read(0x44) != 0x12340000 + bank:
                    raise ValueError("Direct write is not visible in selected bank %d" % bank)
                qtest.request("writew 0x%x 0x%x" % (GOP + 0x44, 0x5a00 + bank))
                if direct_read(bank, 0x44) != 0x12345a00 + bank:
                    raise ValueError("Banked halfword write is not visible through direct MMIO")
            direct_write(0, 0x1fc, 3, "w")
            if read(0x1fc) != 3:
                raise ValueError("The direct and banked global selectors differ")
            select(6)
            if direct_read(0, 0x1fc, "w") != 6:
                raise ValueError("The banked selector is not visible through direct MMIO")
            direct_write(15, 0x1ff, 0xa5, "b")
            if direct_read(15, 0x1fc) != 0xa5000000 or read(0x1fc) != 6:
                raise ValueError("The last direct-bank byte aliases the global selector")
            result["checks"].append("all 16 direct banks share storage and byte lanes with banked MMIO")
            width, height, pitch = 64, 3, 288
            expected = bytearray()
            frame = bytearray()
            for y in range(height):
                for x in range(width):
                    red, green, blue = (x * 3 + y * 7) & 255, (x * 11 + y * 17) & 255, (x * 19 + y * 23) & 255
                    frame += bytes((blue, green, red, (x + y) & 255))
                    expected += bytes((red, green, blue))
                frame += b"\x7e" * (pitch - width * 4)
            qtest.request("write 0x%x 0x%x 0x%s" % (BUFFER, len(frame), frame.hex()))
            layout(width, height, pitch)
            blank("uncommitted-black")
            result["checks"].append("register programming remains invisible before commit")
            commit()
            expect_frame("argb-padded", width, height, expected)
            result["checks"].append("ARGB8888 scanout and padded pitch, every pixel checked")
            result["checks"].append("update acknowledgement clears four busy bits")
            def mirrored(rgb, width, height, flags):
                return b"".join(rgb[(y * width + x) * 3:(y * width + x + 1) * 3]
                                for y in (range(height - 1, -1, -1) if flags & 0x2000 else range(height))
                                for x in (range(width - 1, -1, -1) if flags & 0x1000 else range(width)))

            for flags, tag in ((0x1000, "hmirror"), (0x2000, "vmirror"), (0x3000, "hvmirror")):
                last_row = (height - 1) * pitch if flags & 0x2000 else 0
                layout(width, height, pitch, address=BUFFER + last_row, mirror=flags)
                commit()
                expect_frame("argb-" + tag, width, height, mirrored(expected, width, height, flags))
                result["checks"].append("ARGB8888 %s samples every pixel with padded pitch" % tag)
            layout(width, height, pitch, mirror=0x2000, read_offset=(height - 1) * pitch)
            commit()
            expect_frame("vmirror-read-offset", width, height, mirrored(expected, width, height, 0x2000))
            result["checks"].append("GOP0 RBLK offset supplies the first mirrored row without shifting the buffer base")
            layout(width, height, pitch, address=0x40000000, mirror=0x2000)
            commit()
            blank("vmirror-before-ram")
            result["checks"].append("backward row fetch outside RAM is rejected without reading another region")
            layout(width, height, pitch)
            commit()
            write(0x1fc, 0x201)
            write(0, 0x50)  # disabled, force write
            blank("disabled-black")
            write(0, 0x51)
            expect_frame("force-enabled", width, height, expected)
            result["checks"].append("force-write enable/disable without a separate commit")
            write(0, 0x50)
            select(1)  # Leave force-write mode before programming direct banks.
            direct_write(9, 0, 8)
            for offset, value in ((0, 0x51), (4, (BUFFER - 0x40000000) >> 5 & 0xffff),
                                  (8, (BUFFER - 0x40000000) >> 21), (0x10, 0),
                                  (0x14, width // 8), (0x18, 0), (0x20, height),
                                  (0x24, pitch // 32), (0x28, 0xff)):
                direct_write(10, offset, value)
            blank("direct-uncommitted")
            direct_write(0, 0x1fc, 0xf409, "w")
            if read(0x1fc) != 0x409:
                raise ValueError("Direct update does not share the acknowledged selector")
            expect_frame("direct-gop3", width, height, expected)
            direct_write(9, 0, 0x3008)
            relative = BUFFER + (height - 1) * pitch - 0x40000000
            direct_write(10, 4, relative >> 5 & 0xffff)
            direct_write(10, 8, relative >> 21 & 0x1ff)
            # Unlike GOP0/1, GOP2/3 start at the programmed row address.
            direct_write(10, 0x30, 0xffff)
            direct_write(10, 0x34, 0xffff)
            direct_write(0, 0x1fc, 9, "w")
            direct_write(0, 0x1fc, 0x409, "w")
            expect_frame("direct-gop3-mirrored", width, height, mirrored(expected, width, height, 0x3000))
            result["checks"].append("GOP3 backward row address and both mirrors work through direct bank commits")
            direct_write(9, 0, 8)
            relative = BUFFER - 0x40000000
            direct_write(10, 4, relative >> 5 & 0xffff)
            direct_write(10, 8, relative >> 21 & 0x1ff)
            direct_write(10, 0x30, 0)
            direct_write(10, 0x34, 0)
            # The selected GOP update must not expose another GOP's staged data.
            direct_write(1, 0, 0x51)
            direct_write(10, 0, 0x50)
            direct_write(0, 0x1fc, 9, "w")
            direct_write(0, 0x1fc, 0x409, "w")
            blank("direct-selected-commit")
            direct_write(10, 0, 0x51)
            direct_write(0, 0x1fc, 0x409, "w")
            blank("direct-held-pulse")
            direct_write(0, 0x1fc, 9, "w")
            direct_write(0, 0x1fc, 0x409, "w")
            expect_frame("direct-next-pulse", width, height, expected)
            direct_write(0, 0x1fc, 0x209, "w")
            direct_write(1, 0, 0x50)
            direct_write(10, 0, 0x50)
            blank("direct-force-disable")
            direct_write(10, 0, 0x51)
            expect_frame("direct-force-enable", width, height, expected)
            direct_write(10, 0, 0x50)
            direct_write(0, 0x1fc, 0, "w")
            result["checks"].append("direct GOP3 pixel scanout, selected update pulse and force-write")
            layout(64, 3, 288, address=0x70000000)
            commit()
            blank("unmapped-black")
            layout(64, 3, 32)
            commit()
            blank("short-pitch-black")
            layout(64, 3, 288)
            write(0x20, 65535)
            commit()
            blank("oversize-black")
            result["checks"].append("unmapped buffers, short pitch and oversized dimensions are rejected")
            width, height, pitch = 32, 2, 64
            colors = [0xf800, 0x07e0, 0x001f, 0xffff, 0, 0x8410]
            expected = bytearray()
            frame = bytearray()
            for n in range(width * height):
                packed = colors[n % len(colors)]
                frame += struct.pack("<H", packed)
                expected += bytes((((packed >> 11) & 31) * 255 // 31,
                                   ((packed >> 5) & 63) * 255 // 63, (packed & 31) * 255 // 31))
            qtest.request("write 0x%x 0x%x 0x%s" % (BUFFER, len(frame), frame.hex()))
            layout(width, height, pitch, format=0)
            commit()
            expect_frame("rgb565", width, height, expected)
            result["checks"].append("RGB565 conversion, every pixel checked")
            layout(width, height, pitch, address=BUFFER + pitch, format=0, mirror=0x3000)
            commit()
            expect_frame("rgb565-mirrored", width, height, mirrored(expected, width, height, 0x3000))
            result["checks"].append("RGB565 mirrors use two-byte source pixels and the programmed last-row address")
            if not native:
                layout(width, height, pitch, format=0, left=560, top=100)
                commit()
                expect_frame("panel-position", width, height, expected, left=560, top=100)
                result["checks"].append("1920x1080 panel preserves window position and black background")
                layout(width, height, pitch, format=0, left=1904, top=1079)
                commit()
                expect_frame("panel-clipped", width, height, expected, left=1904, top=1079)
                result["checks"].append("window pixels clip safely at the right/bottom panel edges")
                qmp.execute("system_reset")
                foreground = BUFFER + 0x1000
                red = bytes((255, 0, 0)) * 16
                qtest.request("write 0x%x 0x40 0x%s" %
                              (BUFFER, (bytes((0, 0, 255, 255)) * 16).hex()))
                opacity = [0, 64, 128, 255, 255, 128, 64, 0] * 2
                fg = b"".join(bytes((255, 0, 0, a)) for a in opacity)
                qtest.request("write 0x%x 0x40 0x%s" % (foreground, fg.hex()))
                layout(8, 2, 32)
                direct_write(6, 0, 0x8008)
                relative = foreground - 0x40000000
                for offset, value in ((0, 0x4051), (4, relative >> 5 & 0xffff),
                                      (8, relative >> 21), (0x14, 1), (0x20, 2),
                                      (0x24, 1), (0x28, 255)):
                    direct_write(7, offset, value)
                commit()
                blended = b"".join(bytes((255 - a, 0, a)) for a in opacity)
                expect_frame("pixel-alpha-layers", 8, 2, blended)
                result["checks"].append("GOP mux composes transparent, partial and opaque foreground pixels")
                direct_write(0, 0x1f8, 0x60a)  # GOP2, GOP1, GOP0, GOP3.
                commit()
                expect_frame("mux-reversed", 8, 2, red)
                result["checks"].append("reversing mux order changes the actual front layer")
                direct_write(0, 0x1f8, 0x688)
                direct_write(6, 0, 8)
                commit()
                expect_frame("inverse-pixel-alpha", 8, 2,
                             b"".join(bytes((a, 0, 255 - a)) for a in opacity))
                result["checks"].append("GOP alpha polarity follows the general alpha-inverse control")
                direct_write(7, 0, 0x51)
                direct_write(7, 0x28, 64)
                commit()
                expect_frame("constant-alpha-layer", 8, 2, bytes((191, 0, 64)) * 16)
                result["checks"].append("constant alpha blends without using framebuffer alpha bytes")
                direct_write(1, 0, 0x50)
                direct_write(7, 0x28, 255)
                direct_write(6, 0xd4, 0x800)
                direct_write(6, 0xd8, 0x800)
                commit()
                expect_frame("stretched-layer", 16, 4, bytes((0, 0, 255)) * 64)
                result["checks"].append("12-bit horizontal and vertical stretch ratios scale real source pixels")
                # A patterned row ensures scaling tests address selection, not
                # only the output rectangle of a solid fill.
                colors = [bytes((x * 31, 255 - x * 31, x * 17)) for x in range(8)]
                row = b"".join(bytes((c[2], c[1], c[0], 255)) for c in colors)
                qtest.request("write 0x%x 0x40 0x%s" % (foreground, (row * 2).hex()))
                expected = b"".join(c * 2 for c in colors) * 4
                expect_frame("stretched-pattern", 16, 4, expected)
                direct_write(6, 0, 0xb008)
                relative = foreground + 32 - 0x40000000
                direct_write(7, 4, relative >> 5 & 0xffff)
                direct_write(7, 8, relative >> 21 & 0x1ff)
                commit()
                expected = b"".join(c * 2 for c in reversed(colors)) * 4
                expect_frame("stretched-mirrored-pattern", 16, 4, expected)
                result["checks"].append("mirror source coordinates are applied before stretch sampling")
                direct_write(6, 0xc8, 1904)
                direct_write(6, 0xd0, 1079)
                commit()
                expect_frame("stretched-clipped", 16, 4, expected, left=1904, top=1079)
                result["checks"].append("stretch selects each source pixel and clips at panel boundaries")
            qmp.execute("system_reset")
            blank("after-reset-black")
            if any(direct_read(bank, 0x44) for bank in range(16)) or direct_read(0, 0x1fc):
                raise ValueError("Reset did not clear the direct register view")
            result["checks"].append("reset clears scanout and register state")
            result["status"] = "passed"
        except (OSError, ValueError, ConnectionError) as error:
            result["error"] = str(error)
        finally:
            if qmp is not None:
                qmp.close()
            if qtest is not None:
                qtest.stream.close()
            if connection is not None:
                connection.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("status", "checks", "error") if k in result}, indent=2))
    return 0 if result["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=PROJECT / "work/qemu-smoke/gop")
    parser.add_argument("--native", action="store_true", help="Check the diagnostic view of the first framebuffer")
    parser.add_argument("--panel-rotate-180", action="store_true",
                        help="Check the complete panel composition with inverted mounting")
    args = parser.parse_args()
    return run(args.qemu, args.output, args.native, args.panel_rotate_180)


if __name__ == "__main__":
    raise SystemExit(main())
