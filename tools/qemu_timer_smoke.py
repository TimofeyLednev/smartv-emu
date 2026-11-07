#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Check Cortex-A9 timer clock rates, prescaling, counters and IRQ flags.

qtest advances virtual time deterministically without firmware. Samsung uses
the unchanged kernel's configured 450 MHz peripheral rate. Versatile Express
checks that unconnected timer clocks retain the legacy 100 MHz default.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess

from qemu_fcie_smoke import Qtest
from qemu_qmp import QMP, free_port
PROJECT = Path(__file__).resolve().parents[1]


def run(qemu, output, machine):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    frequency = 450000000 if machine == "samsung_mstar_ue40f6650" else 100000000
    base = 0x16000000 if machine == "samsung_mstar_ue40f6650" else 0x1E000000
    gt, private, watchdog = base + 0x200, base + 0x600, base + 0x620
    qtest_port, qmp_port = free_port(), free_port()
    command = [str(qemu.resolve()), "-machine", machine, "-accel", "qtest", "-m", "128M",
               "-smp", "2", "-display", "none", "-monitor", "none", "-serial", "null", "-nic", "none",
               "-qtest", "tcp:127.0.0.1:%d,server=on,wait=off" % qtest_port,
               "-qtest-log", str(output / "qtest.log"),
               "-qmp", "tcp:127.0.0.1:%d,server=on,wait=off" % qmp_port]
    environment = os.environ.copy()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = {"scope": "Synthetic ARM timer clocks and IRQ flags; physical TV timing unmeasured",
              "command": command, "expected_frequency_hz": frequency, "checks": [], "status": "failed"}
    qmp = connection = qtest = None
    with (output / "qemu-stderr.log").open("wb") as errors:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=errors, env=environment, creationflags=flags)
        try:
            qmp = QMP(qmp_port)
            connection = socket.create_connection(("127.0.0.1", qtest_port), timeout=5)
            qtest = Qtest(connection)

            def write(address, value):
                qtest.request("writel 0x%x 0x%x" % (address, value))

            def read(address):
                return int(qtest.request("readl 0x%x" % address), 0)

            def step(ns):
                qtest.request("clock_step %d" % ns)

            def require(condition, message):
                if not condition:
                    raise ValueError(message)

            def counter():
                return read(gt + 4) << 32 | read(gt)

            def set_counter(value=0):
                write(gt + 8, 0)
                write(gt, value & 0xFFFFFFFF)
                write(gt + 4, value >> 32)
                write(gt + 12, 1)

            step(1000000000)
            require(counter() == 0, "Disabled global timer advanced")
            write(gt + 8, 1)
            step(1000000000)
            measured = counter()
            require(measured == frequency, "Global timer does not run at its configured input frequency")
            result["measured_ticks_per_second"] = measured
            result["checks"].append("global timer input rate and disabled counter")

            write(gt + 8, 0)
            frozen = counter()
            step(1000000)
            require(counter() == frozen, "Disabling the global timer did not freeze its count")
            result["checks"].append("global timer stop retains the count")

            set_counter()
            write(gt + 8, 1 | 255 << 8)
            step(2000000000)
            require(counter() == frequency * 2 // 256, "Global timer lost precision with prescaler 255")
            result["checks"].append("global timer fractional period at maximum prescaler")

            set_counter(0xFFFFFFF0)
            write(gt + 8, 1)
            step(160)
            require(counter() == 0xFFFFFFF0 + frequency * 160 // 1000000000,
                    "Global counter low/high rollover is incorrect")
            result["checks"].append("global timer 64-bit counter rollover")

            set_counter()
            write(gt + 16, frequency // 1000)
            write(gt + 20, 0)
            write(gt + 8, 7)
            step(1000000)
            require(read(gt + 12) == 0, "Global comparator fired before the next counter tick")
            step(20)
            require(read(gt + 12) == 1, "Global comparator did not fire at the configured clock rate")
            write(gt + 8, 1)
            write(gt + 12, 1)
            require(read(gt + 12) == 0, "Global comparator IRQ acknowledgement failed")
            result["checks"].append("global comparator boundary, fractional deadline and IRQ clear after disabling compare")

            set_counter()
            write(gt + 16, frequency // 1000)
            write(gt + 20, 0)
            write(gt + 24, frequency // 1000)
            write(gt + 8, 15)
            step(1500000)
            require(read(gt + 12) == 1 and read(gt + 16) == frequency // 500,
                    "Global comparator did not advance by its auto-increment period")
            write(gt + 12, 1)
            step(1000000)
            require(read(gt + 12) == 1, "Auto-incrementing global timer did not generate another IRQ")
            result["checks"].append("global comparator auto-increment and repeated IRQ")

            set_counter(1)
            write(gt + 16, 0xFFFFFFFF)
            write(gt + 20, 0x7FFFFFFF)
            write(gt + 8, 7 | 255 << 8)
            step(1000)
            require(read(gt + 12) == 0, "A saturated far-future deadline raised an early IRQ")
            result["checks"].append("far-future comparator deadline saturates without overflow")

            def start_private(address, load, control):
                write(address + 8, 0)
                write(address + 12, 1)
                write(address, load)
                write(address + 8, control)

            for address, name in ((private, "private timer"), (watchdog, "watchdog counter")):
                start_private(address, frequency // 1000 + 1, 5)
                step(1000000)
                require(0 < read(address + 4) <= 2 and read(address + 12) == 0,
                        name + " countdown does not match its clock")
                step(100)
                require(read(address + 12) == 1, name + " expiry did not latch an IRQ")
                write(address + 12, 1)
                require(read(address + 12) == 0, name + " IRQ clear failed")
                result["checks"].append(name + " input clock, one-shot expiry and IRQ clear")

            start_private(private, frequency, 5 | 255 << 8)
            step(2000000000)
            expected = frequency - frequency * 2 // 256
            require(abs(read(private + 4) - expected) <= 2 and read(private + 12) == 0,
                    "Private timer truncated the fractional period with prescaler 255")
            result["checks"].append("private timer fractional period at maximum prescaler")

            start_private(private, frequency // 1000, 7)
            step(3500000)
            require(abs(read(private + 4) - frequency // 2000) <= 4 and read(private + 12) == 1,
                    "Periodic private timer has the wrong rate or reload phase")
            write(private + 12, 1)
            step(1000000)
            require(read(private + 12) == 1, "Periodic private timer did not retrigger")
            write(private + 8, 0)
            frozen = read(private + 4)
            step(1000000)
            require(read(private + 4) == frozen, "Disabled private timer kept counting")
            result["checks"].append("private timer periodic reload, IRQ retrigger and stop")

            qmp.execute("system_reset")
            require(counter() == 0 and read(gt + 8) == 0 and read(gt + 12) == 0,
                    "SoC reset did not clear global timer state")
            for address in (private, watchdog):
                require(read(address + 8) == 0 and read(address + 12) == 0,
                        "SoC reset did not clear a private timer/watchdog")
            write(gt + 8, 1)
            step(1000000000)
            require(counter() == frequency, "SoC reset changed the timer input clock")
            result["checks"].append("SoC reset clears timers and retains the configured input frequency")
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
    print(json.dumps({k: result[k] for k in ("status", "measured_ticks_per_second", "checks", "error") if k in result}, indent=2))
    return 0 if result["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=PROJECT / "work/qemu-smoke/timers")
    parser.add_argument("--machine", choices=("samsung_mstar_ue40f6650", "vexpress-a9"),
                        default="samsung_mstar_ue40f6650")
    args = parser.parse_args()
    return run(args.qemu, args.output, args.machine)


if __name__ == "__main__":
    raise SystemExit(main())
