#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Check MIU/UTMI handshakes and the original LCD/Full HD panel GPIO paths."""
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


def run(qemu, output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    port, qmp_port = free_port(), free_port()
    command = [str(Path(qemu).resolve()), "-machine", "samsung_mstar_ue40f6650", "-accel", "qtest",
               "-m", "128M", "-display", "none", "-monitor", "none", "-serial", "null", "-nic", "none",
               "-qtest", "tcp:127.0.0.1:%d,server=on,wait=off" % port,
               "-qtest-log", str(output / "qtest.log"),
               "-qmp", "tcp:127.0.0.1:%d,server=on,wait=off" % qmp_port]
    environment = os.environ.copy()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = {"scope": "Synthetic RIU accesses; no Samsung firmware", "command": command,
              "checks": [], "status": "failed"}
    qmp, connection, qtest = None, None, None
    with (output / "qemu-stderr.log").open("wb") as errors:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=errors, env=environment, creationflags=flags)
        try:
            qmp = QMP(qmp_port)
            connection = socket.create_connection(("127.0.0.1", port), timeout=5)
            qtest = Qtest(connection)

            def read(address):
                return int(qtest.request("readb 0x%x" % address), 0)

            def write(address, value):
                qtest.request("writeb 0x%x 0x%x" % (address, value))

            write(0x1f203114, 1)
            if not read(0x1f203141) & 0x10:
                raise ValueError("MIU flush completion was not set")
            write(0x1f203114, 0)
            if read(0x1f203141) & 0x10:
                raise ValueError("MIU completion was not cleared with the request")
            result["checks"].append("MIU flush request/completion")
            addresses = (0x1f207578, 0x1f207478, 0x1f207278)
            for address in addresses:
                if read(address) != 0:
                    raise ValueError("UTMI PHY calibration starts active")
                write(address, 1)
                if read(address) & 3 != 3:
                    raise ValueError("UTMI PHY calibration did not complete")
                write(address, 0)
                if read(address) & 3 != 2:
                    raise ValueError("UTMI PHY status did not survive clearing its trigger")
            result["checks"].append("three UTMI PHY triggers and persistent completion status")
            oen, out, inp, mask = 0x1F002845, 0x1F002848, 0x1F002849, 2
            if not read(oen) & mask or not read(inp) & mask:
                raise ValueError("The LCD panel strap was not released and high")
            result["checks"].append("GPIO43 reset selects the LCD panel family")
            qmp.execute("qom-set", {"path": "/machine", "property": "panel-gpio43-high", "value": False})
            if read(inp) & mask:
                raise ValueError("External low did not reach the released GPIO43 input")
            write(inp, 0xFF)
            if read(inp) != 0xFD:
                raise ValueError("A register write changed the read-only GPIO43 input")
            result["checks"].append("external low and read-only input preserve adjacent bits")
            write(out, mask)
            write(oen, 0)
            if not read(inp) & mask:
                raise ValueError("An enabled GPIO43 output did not feed back its high latch")
            write(out, 0)
            qmp.execute("qom-set", {"path": "/machine", "property": "panel-gpio43-high", "value": True})
            if read(inp) & mask:
                raise ValueError("A driven low GPIO43 incorrectly followed the external high")
            write(oen, mask)
            if not read(inp) & mask:
                raise ValueError("Releasing GPIO43 did not expose the external high")
            result["checks"].append("direction changes select output feedback or external input")
            qtest.request("writel 0x1f002848 0xa35ae1b7")
            if int(qtest.request("readl 0x1f002848"), 0) != 0xA35AE3B7:
                raise ValueError("Word accesses did not preserve adjacent GPIO latches")
            qmp.execute("qom-set", {"path": "/machine", "property": "panel-gpio43-high", "value": False})
            if int(qtest.request("readw 0x1f002848"), 0) != 0xE1B7:
                raise ValueError("Halfword input feedback disagreed with byte/word accesses")
            result["checks"].append("byte, halfword and word accesses retain neighboring latches")
            qmp.execute("system_reset")
            if any(read(address) != 0 for address in addresses):
                raise ValueError("Reset did not clear PHY calibration status")
            result["checks"].append("reset clears PHY state")
            if read(oen) != mask or read(out) or read(inp):
                raise ValueError("Reset did not release GPIO43 while retaining the external low")
            qmp.execute("qom-set", {"path": "/machine", "property": "panel-gpio43-high", "value": True})
            if read(inp) != mask:
                raise ValueError("The LCD strap did not survive resetting the controller")
            result["checks"].append("reset releases the pad, clears latches and retains the board strap")
            fhd = 0x1F001E04
            if read(fhd) != 5:
                raise ValueError("GPIO10 was not released with the Full HD strap high")
            result["checks"].append("GPIO10 reset exposes the original Full HD strap")
            qmp.execute("qom-set", {"path": "/machine", "property": "panel-gpio10-high", "value": False})
            if read(fhd) != 1:
                raise ValueError("GPIO10 external low did not reach its released input")
            write(fhd, 0xFD)
            if read(fhd) != 0xF9:
                raise ValueError("A write changed GPIO10's read-only input bit")
            result["checks"].append("GPIO10 external low and read-only input preserve adjacent controls")
            write(fhd, 2)
            if read(fhd) != 6:
                raise ValueError("GPIO10 output high did not feed back through IN")
            qmp.execute("qom-set", {"path": "/machine", "property": "panel-gpio10-high", "value": True})
            write(fhd, 0)
            if read(fhd):
                raise ValueError("GPIO10 output low incorrectly followed the external high")
            write(fhd, 1)
            if read(fhd) != 5:
                raise ValueError("Releasing GPIO10 did not expose its external high")
            result["checks"].append("GPIO10 direction selects actual output feedback or the external strap")
            qtest.request("writel 0x1f001e04 0xa35ae1bd")
            if int(qtest.request("readl 0x1f001e04"), 0) != 0xA35AE1BD:
                raise ValueError("GPIO10 word access damaged adjacent bytes")
            qmp.execute("qom-set", {"path": "/machine", "property": "panel-gpio10-high", "value": False})
            if int(qtest.request("readw 0x1f001e04"), 0) != 0xE1B9:
                raise ValueError("GPIO10 halfword input feedback disagreed with byte accesses")
            result["checks"].append("GPIO10 byte, halfword and word access retain neighboring latches")
            qmp.execute("system_reset")
            if read(fhd) != 1:
                raise ValueError("GPIO10 reset did not release the pad and retain external low")
            qmp.execute("qom-set", {"path": "/machine", "property": "panel-gpio10-high", "value": True})
            if read(fhd) != 5:
                raise ValueError("GPIO10 external high did not survive controller reset")
            result["checks"].append("GPIO10 reset clears output state and retains the board strap")
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
    parser.add_argument("--output", type=Path, default=PROJECT / "work/qemu-smoke/riu")
    args = parser.parse_args()
    return run(args.qemu, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
