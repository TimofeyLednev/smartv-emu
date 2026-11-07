#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise the MStar Utgard register adapter, shared IRQ and MMU over qtest.

Uses synthetic command lists and page tables, without Samsung firmware.
Nonempty rendering is expected to report an error until an execution engine
is implemented; an interrupt alone is not evidence of rendered pixels.
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
GP = 0x1F280000
L2 = 0x1F282000
MMUS = tuple(0x1F280000 + address for address in (0x6000, 0x8000, 0xA000, 0xC000, 0xE000))
PPS = tuple(0x1F290000 + index * 0x4000 for index in range(4))
IRQ_MASK = 0x1F2032DC
IRQ_PENDING = 0x1F2032FC
IRQ_BIT = 1 << (245 - 240)
MIU = 0x40000000
PD = 0x00400000
PT = 0x00401000
DATA = 0x00402000
VA = 0x12345000


class Mali:
    def __init__(self, qtest):
        self.qtest = qtest

    def read(self, base, register):
        low = int(self.qtest.request("readw 0x%x" % (base + register * 2)), 0)
        high = int(self.qtest.request("readw 0x%x" % (base + register * 2 + 4)), 0)
        return low | high << 16

    def write(self, base, register, value):
        self.qtest.request("writew 0x%x 0x%x" % (base + register * 2, value & 0xFFFF))
        self.qtest.request("writew 0x%x 0x%x" % (base + register * 2 + 4, value >> 16))

    def memory_word(self, address, value):
        self.qtest.request("writel 0x%x 0x%x" % (MIU + address, value))

    def pending(self):
        return int(self.qtest.request("readw 0x%x" % IRQ_PENDING), 0)

    def unmask(self):
        self.qtest.request("writew 0x%x 0x%x" % (IRQ_MASK, 0xFFFF ^ IRQ_BIT))

    def reset_gp(self):
        self.write(GP, 0x20, 1 << 10)
        self.write(GP, 0x28, 0xFFFFFFFF)

    def reset_pp(self, index):
        self.write(PPS[index], 0x100C, 1 << 7)
        self.write(PPS[index], 0x1024, 0xFFFFFFFF)

    def map_page(self, flags=7):
        self.memory_word(PD + (VA >> 22) * 4, PT | 1)
        self.memory_word(PT + ((VA >> 12) & 1023) * 4, DATA | flags)
        self.memory_word(DATA, 0xDEADBEEF)

    def enable_mmu(self, index):
        self.write(MMUS[index], 8, 6)
        self.write(MMUS[index], 0, PD)
        self.write(MMUS[index], 0x1C, 3)
        self.write(MMUS[index], 8, 0)

    def start_gp(self, address=VA):
        self.write(GP, 0, address)
        self.write(GP, 4, address + 8)
        self.write(GP, 0x2C, 1 << 13)
        self.write(GP, 0x20, 1)

    def start_pp(self, index, address=VA):
        self.write(PPS[index], 0, address)
        self.write(PPS[index], 0x1028, 1 << 9)
        self.write(PPS[index], 0x100C, 1 << 6)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def run(qemu, output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    port, qmp_port = free_port(), free_port()
    command = [str(Path(qemu).resolve()), "-machine", "samsung_mstar_ue40f6650", "-accel", "qtest",
               "-m", "128M", "-display", "none", "-monitor", "none", "-serial", "null", "-nic", "none",
               "-qtest", "tcp:127.0.0.1:%d,server=on,wait=off" % port,
               "-qtest-log", str(output / "qtest.log"),
               "-qmp", "tcp:127.0.0.1:%d,server=on,wait=off" % qmp_port,
               "-global", "mst12-mali.trace-jobs=on",
               "-d", "unimp,guest_errors", "-D", str(output / "qemu-debug.log")]
    environment = os.environ.copy()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = {"scope": "Synthetic Mali command lists and page tables; no Samsung firmware or rendered UI",
              "command": command, "checks": [], "status": "failed"}
    qmp, connection, qtest = None, None, None
    with (output / "qemu-stderr.log").open("wb") as errors:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=errors, env=environment, creationflags=flags)
        try:
            qmp = QMP(qmp_port)
            connection = socket.create_connection(("127.0.0.1", port), timeout=5)
            qtest = Qtest(connection)
            mali = Mali(qtest)

            require(mali.read(GP, 0x6C) >> 16 == 0x0B07, "GP does not identify a supported Mali-400")
            for pp in PPS:
                require(mali.read(pp, 0x1000) >> 16 == 0xCD07, "PP identification differs across cores")
                mali.write(pp, 0x1000, 0)
                require(mali.read(pp, 0x1000) == 0xCD070100, "PP identification is writable")
            require(mali.read(L2, 4) == 0x06100206, "L2 size is incorrect")
            result["checks"].append("read-only GP/L2/four-PP identification")

            mali.write(PPS[1], 0x1044, 0xC01ACAFE)
            require(mali.read(PPS[1], 0x1044) == 0xC01ACAFE, "MStar split-halfword write/read failed")
            require(mali.read(PPS[0], 0x1044) == 0, "PP register banks alias")
            lane = PPS[1] + 0x1044 * 2
            qtest.request("writew 0x%x 0xffff" % (lane + 2))
            qtest.request("writew 0x%x 0xffff" % (lane + 6))
            require(mali.read(PPS[1], 0x1044) == 0xC01ACAFE, "Padding writes changed a register")
            qtest.request("writeb 0x%x 0x12" % (lane + 1))
            qtest.request("writeb 0x%x 0x34" % (lane + 5))
            require(mali.read(PPS[1], 0x1044) == 0x341A12FE, "Byte writes changed the wrong lane")
            result["checks"].append("halfword/byte lanes, padding and separate PP register banks")

            mali.write(GP, 0x20, 1 << 10)
            require(mali.read(GP, 0x24) == 1 << 19, "GP soft reset did not signal reset completion")
            mali.write(GP, 0x28, 1 << 19)
            require(mali.read(GP, 0x24) == 0, "GP high-half IRQ acknowledgement failed")
            for pp in PPS:
                mali.write(pp, 0x100C, 1 << 7)
                require(mali.read(pp, 0x1020) == 1 << 12, "PP soft reset did not complete")
                mali.write(pp, 0x1024, 1 << 12)
                mali.write(pp, 0x100C, 1 << 5)
                require(mali.read(pp, 0x1020) == 0, "PP force reset raised soft-reset completion")
                mali.write(pp, 0x1044, 0xC01A0000)
                require(mali.read(pp, 0x1044) == 0xC01A0000, "PP hard-reset write probe failed")
            result["checks"].append("GP/PP soft-reset completion, high-half clear and PP force reset")

            for mmu in MMUS:
                mali.write(mmu, 0, 0xCAFEBABE)
                require(mali.read(mmu, 0) == 0xCAFEB000, "MMU directory address is not page-aligned")
                mali.write(mmu, 8, 6)
                require(mali.read(mmu, 0) == 0, "MMU hard reset did not clear its directory")
                require(mali.read(mmu, 4) & 1 == 0, "MMU high-half reset write enabled paging")
                mali.write(mmu, 8, 0)
                require(mali.read(mmu, 4) & 1, "MMU paging was not enabled")
                mali.write(mmu, 8, 2)
                require(mali.read(mmu, 4) & 4, "MMU did not enter stall")
                mali.write(mmu, 8, 3)
                require(mali.read(mmu, 4) & 0x80000004 == 0x80000000, "MMU did not leave stall")
                mali.write(mmu, 8, 1)
                require(mali.read(mmu, 4) & 1 == 0, "MMU paging was not disabled")
            result["checks"].append("five MMUs: directory alignment, reset, paging and stall commands")

            mali.unmask()
            mali.write(GP, 0x24, 0x10000)
            require(mali.pending() == 0, "Masked raw IRQ reached the board")
            mali.write(GP, 0x2C, 0x10000)
            require(mali.pending() == IRQ_BIT, "GP IRQ did not reach board IRQ245")
            mali.write(PPS[2], 0x1020, 1)
            mali.write(PPS[2], 0x1028, 1)
            mali.write(GP, 0x28, 0x10000)
            require(mali.pending() == IRQ_BIT, "Clearing GP IRQ lost a simultaneous PP IRQ")
            mali.write(PPS[2], 0x1024, 1)
            require(mali.pending() == 0, "Shared GPU IRQ stayed asserted after all sources cleared")
            result["checks"].append("masked/shared IRQ245 delivery and independent source acknowledgement")

            mali.reset_gp()
            mali.write(GP, 0, 0x200)
            mali.write(GP, 4, 0x200)
            mali.write(GP, 8, 0x300)
            mali.write(GP, 12, 0x300)
            mali.write(GP, 0x2C, 3)
            mali.write(GP, 0x20, 3)
            require(mali.read(GP, 0x30) == 3 and mali.read(GP, 0x68) & 0xA == 0,
                    "Empty GP command lists did not finish idle")
            result["checks"].append("empty VS/PLBU lists complete without rendering or DMA")

            mali.reset_gp()
            mali.enable_mmu(0)
            mali.map_page()
            mali.write(MMUS[0], 8, 2)
            mali.start_gp()
            require(mali.read(GP, 0x68) & 2 and mali.read(GP, 0x24) == 0,
                    "GP fetched a command while its MMU was stalled")
            mali.write(MMUS[0], 8, 3)
            require(mali.read(MMUS[0], 0x14) == 0 and mali.read(GP, 0x24) == 1 << 13,
                    "Mapped GP fetch failed or falsely completed an unsupported list")
            require(mali.pending() == IRQ_BIT, "GP command error did not deliver an IRQ")
            result["checks"].append("two-level GP page walk, stalled fetch/resume and unsupported-command error")

            mali.reset_gp()
            mali.enable_mmu(0)
            mali.memory_word(PD + (VA >> 22) * 4, 0)
            mali.start_gp()
            require(mali.read(MMUS[0], 0x14) == 1 and mali.read(MMUS[0], 0x0C) == VA,
                    "Missing directory entry did not latch its fault address")
            require(mali.read(MMUS[0], 4) & 2 and mali.read(GP, 0x24) == 0,
                    "Unmapped GP command produced a completion")
            require(mali.pending() == IRQ_BIT, "MMU page fault did not deliver IRQ245")
            mali.map_page()
            mali.write(MMUS[0], 0x18, 1)
            require(mali.read(MMUS[0], 4) & 2, "Acknowledging the interrupt resumed an unresolved fault")
            mali.write(MMUS[0], 8, 5)
            require(mali.read(MMUS[0], 4) & 2 == 0 and mali.read(GP, 0x24) == 1 << 13,
                    "Repaired page fault did not retry the pending fetch")
            result["checks"].append("missing DTE, latched fault/IRQ and repair with PAGE_FAULT_DONE")

            mali.reset_gp()
            mali.enable_mmu(0)
            mali.map_page()
            mali.memory_word(PT + (((VA >> 12) & 1023) + 1) * 4, 0)
            mali.memory_word(DATA + 4092, 0xDEADBEEF)
            mali.start_gp(VA + 4092)
            require(mali.read(MMUS[0], 0x14) == 0 and mali.read(MMUS[0], 4) & 2 == 0,
                    "Debug command-list capture introduced a fault on a later unmapped word")
            require(mali.read(GP, 0x24) == 1 << 13,
                    "Debug capture changed the unsupported first-command result")
            require("capture unavailable: va=0x12346000" in
                    (output / "qemu-debug.log").read_text(encoding="utf-8", errors="replace"),
                    "Debug capture did not report the unavailable next page")
            result["checks"].append("read-only job trace cannot introduce a fault for an unfetched command")

            mali.reset_gp()
            mali.enable_mmu(0)
            mali.map_page(flags=5)
            mali.start_gp()
            require(mali.read(MMUS[0], 0x14) == 1 and mali.read(MMUS[0], 0x0C) == VA,
                    "GP read ignored a write-only PTE")
            mali.write(MMUS[0], 8, 6)
            mali.reset_gp()
            result["checks"].append("PTE read permissions reject a write-only command page")

            for index in range(4):
                mali.enable_mmu(index + 1)
                mali.map_page()
                mali.reset_pp(index)
                mali.start_pp(index)
                require(mali.read(MMUS[index + 1], 0x14) == 0,
                        "PP%d did not use its mapped MMU" % index)
                require(mali.read(PPS[index], 0x1020) == 1 << 9,
                        "PP%d falsely completed unsupported rendering" % index)
                require(mali.read(PPS[index], 0x1008) & 1 == 0,
                        "PP%d stayed active after a command error" % index)
                mali.reset_pp(index)
            result["checks"].append("each PP fetches through its own MMU and reports unsupported rendering")

            debug = (output / "qemu-debug.log").read_text(encoding="utf-8", errors="replace")
            require("word=0xdeadbeef" in debug, "Translated GPU fetch did not read the synthetic data page")
            result["checks"].append("translated command data observed in GPU diagnostics")

            qmp.execute("system_reset")
            mali.unmask()
            require(mali.pending() == 0 and mali.read(GP, 0x24) == 0, "Reset left GPU IRQs pending")
            for index, pp in enumerate(PPS):
                require(mali.read(pp, 0x1020) == 0 and mali.read(pp, 0x1044) == 0,
                        "System reset left PP%d state" % index)
            for mmu in MMUS:
                require(mali.read(mmu, 0) == 0 and mali.read(mmu, 4) & 7 == 0,
                        "System reset left an MMU active")
            result["checks"].append("system reset clears engine/MMU/IRQ state")
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
    print(json.dumps({key: result[key] for key in ("status", "checks", "error") if key in result}, indent=2))
    return 0 if result["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=PROJECT / "work/qemu-smoke/mali")
    args = parser.parse_args()
    return run(args.qemu, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
