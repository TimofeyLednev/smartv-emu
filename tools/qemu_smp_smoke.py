#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise secondary-core mailbox/SGI boot using only synthetic ARM code."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import time
import zlib

from qemu_fcie_smoke import Qtest
from qemu_qmp import QMP, free_port

PROJECT = Path(__file__).resolve().parents[1]


def uimage(payload):
    header = bytearray(struct.pack(
        ">7I4B32s", 0x27051956, 0, 0, len(payload), 0x40208000,
        0x40208000, zlib.crc32(payload), 5, 2, 2, 0, b"SamsungF synthetic SMP"))
    struct.pack_into(">I", header, 4, zlib.crc32(header))
    return bytes(header) + payload


def run(qemu, output):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    kernel = output / "synthetic.uImage"
    # CPU0 sends the SGI in response to a RAM flag. Accessing a banked GIC
    # register through qtest with TCG running has no current CPU context.
    kernel.write_bytes(uimage(struct.pack(
        "<15I", 0xe59f0028,       # ldr r0, signal
        0xe5901000, 0xe3510000, 0x0afffffc,  # wait for signal
        0xe3a01000, 0xe5801000,  # clear signal
        0xe59f2014, 0xe3a01001, 0xe5821000,  # enable GIC distributor
        0xe59f100c, 0xe5821f00, 0xeafffff4,  # send SGI 1; loop
        0x40220008, 0x16001000, 0x00020001)))
    secondary = output / "secondary.bin"
    secondary.write_bytes(struct.pack(
        "<10I", 0xee100fb0,       # mrc p15, 0, r0, c0, c0, 5 (MPIDR)
        0xe59f1014, 0xe5810000,   # ldr r1, done; str r0, [r1]
        0xe59f2010, 0xe592300c,   # ldr r2, GICC; ldr r3, [r2, #12]
        0xe5813004, 0xeafffffe,   # str r3, [r1, #4]; loop
        0xe320f000, 0x40220000, 0x16000100))
    port, qmp_port = free_port(), free_port()
    command = [str(qemu.resolve()), "-machine", "samsung_mstar_ue40f6650",
               "-accel", "tcg", "-smp", "2", "-m", "128M", "-S",
               "-display", "none", "-monitor", "none", "-serial", "null", "-nic", "none",
               "-kernel", str(kernel), "-device",
               "loader,file=%s,addr=0x40210004,force-raw=on" % secondary,
               "-qtest", "tcp:127.0.0.1:%d,server=on,wait=off" % port,
               "-qtest-log", str(output / "qtest.log"),
               "-qmp", "tcp:127.0.0.1:%d,server=on,wait=off" % qmp_port]
    environment = os.environ.copy()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = {"scope": "Synthetic dual-core execution; no Samsung firmware",
              "command": command, "checks": [], "status": "failed"}
    qmp, connection, qtest = None, None, None
    with (output / "qemu-stderr.log").open("wb") as errors:
        process = subprocess.Popen(command, env=environment, creationflags=flags,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors)
        try:
            qmp = QMP(qmp_port)
            connection = socket.create_connection(("127.0.0.1", port), timeout=5)
            qtest = Qtest(connection)

            def read(address):
                return int(qtest.request("readl 0x%x" % address), 0)

            def step():
                qmp.execute("cont")
                time.sleep(0.05)
                qmp.execute("stop")

            def waiting():
                registers = qmp.execute("human-monitor-command", {"command-line": "info registers 1"})
                pc = int(re.search(r"R15=([0-9a-fA-F]+)", registers).group(1), 16)
                if not 0x40001000 <= pc < 0x40001050 or read(0x40220000):
                    raise ValueError("Secondary CPU ran before its complete boot handshake")

            step()
            waiting()
            result["checks"].append("secondary core waits with an empty mailbox")
            qtest.request("writew 0x1f206740 0x4021")
            step()
            waiting()
            result["checks"].append("high-half write alone does not release the core")
            qtest.request("writew 0x1f206744 0x4")
            step()
            waiting()
            result["checks"].append("complete mailbox waits for the boot SGI")
            qtest.request("writel 0x40220008 0x1")  # Ask CPU0 to send SGI 1.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                step()
                if read(0x40220000) & 0xff == 1 and read(0x40220004):
                    break
            else:
                raise ValueError("Secondary program did not execute on CPU1")
            if read(0x40220004) != 1023:
                raise ValueError("Boot SGI was still pending at secondary entry")
            result["checks"].append("CPU1 executes the target and boot SGI is acknowledged")
            qtest.request("writel 0x40220000 0")
            qmp.execute("system_reset")
            step()
            waiting()
            if read(0x1f206740) or read(0x1f206744):
                raise ValueError("Reset did not clear the secondary mailbox")
            result["checks"].append("reset parks the secondary core and clears the mailbox")
            result["status"] = "passed"
        except (OSError, ValueError, ConnectionError) as error:
            result["error"] = str(error)
        finally:
            if process.poll() is not None:
                result["qemu_exit_code"] = process.returncode
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
    parser.add_argument("--output", type=Path, default=PROJECT / "work/qemu-smoke/mstar-smp")
    args = parser.parse_args()
    return run(args.qemu, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
