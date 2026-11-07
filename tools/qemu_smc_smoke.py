#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Check SMC return, register preservation and Normal World on both CPUs.

Runs synthetic ARM code only. A protected MVBAR read must still raise an
undefined-instruction exception after each CPU returns from the monitor.
Requires keystone-engine in the isolated Python environment.
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
from qemu_qmp import QMP, free_port
from qemu_smp_smoke import uimage

PROJECT = Path(__file__).resolve().parents[1]
BUFFERS = (0x40220000, 0x40222000)
SERVICES = (0x202, 0x201, 0x1234)


def assemble(source, address):
    from keystone import Ks, KS_ARCH_ARM, KS_MODE_ARM, KS_MODE_LITTLE_ENDIAN

    code, _ = Ks(KS_ARCH_ARM, KS_MODE_ARM | KS_MODE_LITTLE_ENDIAN).asm(source, address)
    return bytes(code)


def program(cpu, mode):
    prelude = ""
    if cpu == 0:
        prelude = """
            ldr r0, =0x1f206740
            movw r1, #0x4021
            strh r1, [r0]
            mov r1, #4
            strh r1, [r0, #4]
            ldr r0, =0x16001000
            mov r1, #1
            str r1, [r0]
            ldr r1, =0x00020001
            str r1, [r0, #0xf00]
        """
    source = prelude + "ldr r0, =0x40218000\nmcr p15, 0, r0, c12, c0, 0\n"
    for index, service in enumerate(SERVICES):
        source += "ldr sp, =0x%x\n" % (BUFFERS[cpu] + index * 0x100)
        source += "\n".join("mov r%d, #%d" % (r, r * 0x11) for r in range(1, 13)) + "\n"
        source += """
            cmp r1, r1
            mrs lr, cpsr
            movw r0, #%d
            .word 0xe1600070
            stmia sp, {r0-r12, lr}
            mrs r0, cpsr
            str r0, [sp, #56]
        """ % service
    if mode == "bootstrap":
        source += """
            ldr sp, =0x%x
            ldr r0, [sp, #0x100]
            ldr r1, =0xabcdef12
            str r1, [r0, #0x100]
            ldr r2, [r0, #0x100]
            str r2, [sp, #0x314]
        """ % BUFFERS[cpu]
    source += """
        ldr sp, =0x%x
        mrs r1, cpsr
        bic r0, r1, #0x140
        msr cpsr_cx, r0
        mrs r2, cpsr
        str r2, [sp, #0x318]
        orr r0, r0, #0x140
        msr cpsr_cx, r0
        mrs r2, cpsr
        str r2, [sp, #0x31c]
        msr cpsr_cx, r1
        ldr r0, =after_ns
        str r0, [sp, #0x310]
        .word 0xee1c0f30
    after_ns:
        mov r0, #1
        str r0, [sp, #0x300]
    done:
        b done
    """ % BUFFERS[cpu]
    return source


def run(qemu, output, mode):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    kernel, secondary, vectors = (output / name for name in
                                  ("synthetic.uImage", "secondary.bin", "normal-vectors.bin"))
    kernel.write_bytes(uimage(assemble(program(0, mode), 0x40208000)))
    secondary.write_bytes(assemble(program(1, mode), 0x40210004))
    vectors.write_bytes(assemble("""
        b .
        b undefined
        b .
        b .
        b .
        b .
        b .
        b .
    undefined:
        mrc p15, 0, r0, c0, c0, 5
        tst r0, #1
        ldr r1, =0x40220304
        ldrne r1, =0x40222304
        mov r0, #1
        str r0, [r1]
        mrs r0, cpsr
        str r0, [r1, #4]
        str lr, [r1, #8]
        movs pc, lr
    """, 0x40218000))
    port, qmp_port = free_port(), free_port()
    command = [str(qemu.resolve()), "-machine", "samsung_mstar_ue40f6650,secure-monitor=" + mode,
               "-accel", "tcg", "-smp", "2", "-m", "128M", "-S", "-display", "none",
               "-monitor", "none", "-serial", "null", "-nic", "none", "-kernel", str(kernel)]
    for path, address in ((secondary, 0x40210004), (vectors, 0x40218000)):
        command += ["-device", "loader,file=%s,addr=0x%x,force-raw=on" % (path, address)]
    command += ["-qtest", "tcp:127.0.0.1:%d,server=on,wait=off" % port,
                "-qtest-log", str(output / "qtest.log"),
                "-qmp", "tcp:127.0.0.1:%d,server=on,wait=off" % qmp_port]
    environment = os.environ.copy()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = {"scope": "Synthetic dual-core SMC diagnostic; no Samsung firmware",
              "command": command, "secure_monitor": mode, "status": "failed", "checks": []}
    qmp = connection = qtest = None
    with (output / "qemu-stderr.log").open("wb") as errors:
        process = subprocess.Popen(command, env=environment, creationflags=flags,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors)
        try:
            qmp = QMP(qmp_port)
            connection = socket.create_connection(("127.0.0.1", port), timeout=5)
            qtest = Qtest(connection)

            def read(address):
                return int(qtest.request("readl 0x%x" % address), 0)

            def execute():
                qmp.execute("cont")
                deadline = time.monotonic() + 5
                while process.poll() is None and time.monotonic() < deadline:
                    if all(read(base + 0x300) == 1 for base in BUFFERS):
                        qmp.execute("stop")
                        return
                    time.sleep(0.01)
                qmp.execute("stop")
                raise ValueError("Synthetic code did not complete on both CPUs: " +
                                 qmp.execute("human-monitor-command", {"command-line": "info registers -a"})[:1500])

            def verify():
                for cpu, base in enumerate(BUFFERS):
                    for index in range(len(SERVICES)):
                        address = base + index * 0x100
                        values = [read(address + i * 4) for i in range(15)]
                        expected = (0, 0, 0) if mode == "zero" else (0x1000, 0x50f00000, 0xffffffff)
                        if values[0] != expected[index]:
                            raise ValueError("Unexpected SMC return on CPU%d for service 0x%x" %
                                             (cpu, SERVICES[index]))
                        if values[1:13] != [r * 0x11 for r in range(1, 13)]:
                            raise ValueError("SMC skipped the following instruction or clobbered registers")
                        if values[13] != values[14] or values[14] & 0x1f != 0x13:
                            raise ValueError("SMC did not restore CPSR, flags and SVC link register")
                    if read(base + 0x304) != 1 or read(base + 0x308) & 0x1f != 0x1b:
                        raise ValueError("Protected MVBAR read did not trap in Normal World")
                    if read(base + 0x30c) != read(base + 0x310):
                        raise ValueError("Normal World trap was not the intended protected MVBAR read")
                    if mode == "bootstrap" and read(base + 0x314) != 0xabcdef12:
                        raise ValueError("Guest could not read/write the returned WSM physical address")
                    if read(base + 0x318) & 0x140 or read(base + 0x31c) & 0x140 != 0x140:
                        raise ValueError("Normal World could not control FIQ/abort masks on CPU%d: "
                                         "clear=0x%x set=0x%x" %
                                         (cpu, read(base + 0x318), read(base + 0x31c)))
                if read(0x40000088) != 6:
                    raise ValueError("Monitor did not observe exactly six SMC calls")

            execute()
            verify()
            result["checks"] += ["zero returned for shared-memory queries and an arbitrary service on both CPUs"
                                 if mode == "zero" else "valid WSM base/size and error for an unknown service on both CPUs",
                                 "instruction immediately after SMC executes (PC advances once)",
                                 "r1-r12, SVC SP/LR and CPSR flags preserved",
                                 "protected MVBAR access confirms return to Normal World on both CPUs",
                                 "Normal World controls FIQ and asynchronous-abort masks on both CPUs"]
            if mode == "bootstrap":
                result["checks"].append("guest reads/writes the actual WSM page returned by the monitor")
            for base in BUFFERS:
                qtest.request("writel 0x%x 0" % (base + 0x300))
                qtest.request("writel 0x%x 0" % (base + 0x304))
            qmp.execute("system_reset")
            qmp.execute("query-status")
            if read(0x40000088) != 0:
                raise ValueError("Reset did not clear the monitor call count")
            if mode == "bootstrap" and read(0x50f00100) != 0:
                raise ValueError("Reset did not clear the WSM page")
            execute()
            verify()
            result["checks"].append("monitor vectors and both CPU return paths work again after reset")
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
    parser.add_argument("--output", type=Path, default=PROJECT / "work/qemu-smoke/smc-zero")
    parser.add_argument("--secure-monitor", choices=("zero", "bootstrap"), default="zero")
    args = parser.parse_args()
    return run(args.qemu, args.output, args.secure_monitor)


if __name__ == "__main__":
    raise SystemExit(main())
