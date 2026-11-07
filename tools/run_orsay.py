#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Run the SamsungF prototype with local firmware and a localhost TV viewer."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from live_viewer import serve
from qemu_qmp import QMP, capture, free_port

ROOT = Path(__file__).resolve().parents[1]
MACHINE = "samsung_mstar_ue40f6650"
CMDLINE = ("console=ttyS0,115200 loglevel=7 panic=-1 "
           "LX_MEM=0x40200000,0xc600000 EMAC_MEM=0x50e00000,0x100000 "
           "LX_MEM2=0x60000000,0x10000000 root=/dev/mmcblk0p8 "
           "rootfstype=squashfs rootwait ro init=/sbin/init")


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def command(args, qmp_port):
    machine = MACHINE + ",secure-monitor=bootstrap,micom-enabled=on"
    if args.panel_rotation == 180:
        machine += ",panel-rotate-180=on"
    output = args.output.resolve()
    result = [str(args.qemu.resolve()), "-machine", machine,
              "-accel", "tcg,thread=single", "-m", "512M", "-smp", str(args.cpus),
              "-display", "none", "-monitor", "none", "-S", "-nic",
              "none" if args.offline else "user,model=mst12-emac,id=emac0",
              "-serial", "file:" + str(output / "serial.log"),
              "-serial", "file:" + str(output / "uart1-tx.bin"),
              "-kernel", str(args.kernel.resolve()), "-append", CMDLINE,
              "-drive", "if=sd,format=raw,snapshot=on,file=" + str(args.disk.resolve()).replace(",", ",,"),
              "-qmp", "tcp:127.0.0.1:%d,server=on,wait=off" % qmp_port,
              "-icount", "shift=%d,align=off,sleep=on" % args.icount_shift]
    if args.network_capture:
        result += ["-object", "filter-dump,id=tv-net-capture,netdev=emac0,file=" +
                   str(output / "ethernet.pcap").replace(",", ",,")]
    if args.debug:
        result += ["-d", "unimp,guest_errors", "-D", str(output / "qemu-debug.log")]
    return result


def validate(args):
    for label, path in (("QEMU executable", args.qemu), ("Kernel", args.kernel), ("Disk", args.disk)):
        if not path.is_file():
            raise ValueError(label + " file does not exist: " + str(path))
    manifest = json.loads((ROOT / "docs/firmware-manifest.json").read_text(encoding="utf-8"))
    kernel = next(item for item in manifest["payloads"] if item["file"] == "uImage")
    if args.kernel.stat().st_size != kernel["bytes"] or file_hash(args.kernel) != kernel["sha256"]:
        raise ValueError("Kernel differs from the tested T-MST12DEUC 1112.2 uImage")
    if args.disk.stat().st_size < 2048 * 1024 * 1024:
        raise ValueError("Use the synthetic GPT disk from tools/prepare_disk.py (at least 2 GiB)")
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError("Choose a fresh output directory for this VM")
    if args.offline and (args.hubsite_ca or args.network_capture):
        raise ValueError("The TLS bridge and packet capture require networking")
    if args.hubsite_ca and not args.hubsite_ca.is_file():
        raise ValueError("Supply the original firmware's ca_hubsite.pem")
    if args.runtime_dir and not args.runtime_dir.is_dir():
        raise ValueError("QEMU runtime directory does not exist: " + str(args.runtime_dir))
    if not 0 <= args.port < 65536 or not 0 <= args.seconds < float("inf"):
        raise ValueError("Use a valid local viewer port and a finite nonnegative duration")
    if not 1 <= args.hubsite_timeout <= 3600:
        raise ValueError("Hub guest socket timeout must be finite and between 1 and 3600 seconds")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, help="Directory containing the matching Windows QEMU DLLs")
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--disk", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "work" / time.strftime("orsay-%Y%m%d-%H%M%S"))
    parser.add_argument("--cpus", type=int, choices=(1, 2), default=2)
    parser.add_argument("--panel-rotation", type=int, choices=(0, 180), default=180)
    parser.add_argument("--icount-shift", type=int, choices=range(11), default=3)
    parser.add_argument("--offline", action="store_true", help="Disable the emulated Ethernet connection")
    parser.add_argument("--hubsite-ca", type=Path, help="Opt in to the legacy Hub TLS bridge with the original firmware CA")
    parser.add_argument("--hubsite-timeout", type=float, default=900, help="Bridge guest timeout in host seconds")
    parser.add_argument("--openssl", type=Path, help="OpenSSL 3 executable for the optional bridge")
    parser.add_argument("--network-capture", action="store_true", help="Save a private packet capture in the output folder")
    parser.add_argument("--debug", action="store_true", help="Save QEMU guest-error diagnostics")
    parser.add_argument("--port", type=int, default=0, help="Local viewer port; zero chooses a free port")
    parser.add_argument("--no-open", action="store_true", help="Print the viewer URL without opening a browser")
    parser.add_argument("--seconds", type=float, default=0, help="Zero runs the viewer until stopped; a positive value makes a bounded headless observation")
    parser.add_argument("--dry-run", action="store_true", help="Validate inputs and print the base command without starting a VM")
    args = parser.parse_args()
    process = qmp = bridge = None
    before = None
    result = {"machine": MACHINE, "firmware": "T-MST12DEUC 1112.2",
              "status": "starting", "original_firmware_patched": False,
              "disk_snapshot": True, "orsay_ui_automatically_verified": False}
    try:
        validate(args)
        qmp_port = free_port()
        qemu_command = command(args, qmp_port)
        if args.dry_run:
            print(json.dumps({"command": qemu_command,
                              "optional_tls_bridge": bool(args.hubsite_ca),
                              "bridge_redirector_ports": "assigned at startup",
                              "output_created": False}, indent=2))
            return 0
        args.output = args.output.resolve()
        args.output.mkdir(parents=True, exist_ok=True)
        before = file_hash(args.disk)
        result["disk_sha256_before"] = before
        if args.hubsite_ca:
            if args.openssl:
                os.environ["SAMSUNGF_OPENSSL"] = str(args.openssl.resolve())
            from orsay_network_bridge import OrsayNetworkBridge
            bridge = OrsayNetworkBridge(args.output / "network-bridge",
                                         hubsite_ca=args.hubsite_ca,
                                         guest_socket_timeout=args.hubsite_timeout)
            bridge.start()
            qemu_command += bridge.qemu_args()
        result["command"] = qemu_command
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        environment = os.environ.copy()
        if args.runtime_dir:
            environment["PATH"] = str(args.runtime_dir.resolve()) + os.pathsep + environment.get("PATH", "")
        with (args.output / "qemu.log").open("wb") as log:
            process = subprocess.Popen(qemu_command, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT,
                                       env=environment, creationflags=flags)
            result["qemu_pid"] = process.pid
            qmp = QMP(qmp_port, timeout=30)
            qmp.execute("cont")
            result["status"] = "running"
            (args.output / "session.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            if args.seconds:
                deadline = time.monotonic() + args.seconds
                while process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.2)
                if process.poll() is None:
                    capture(qmp, args.output / "screen.png")
            else:
                serve(process, qmp, args.output, port=args.port, open_browser=not args.no_open,
                      scope="Experimental original Orsay firmware; rendering and peripherals incomplete",
                      remote_control=True)
            result["status"] = "stopped"
    except KeyboardInterrupt:
        result["status"] = "stopped"
    except (OSError, ValueError, KeyError) as error:
        result.update(status="failed", error=str(error))
        print("Prototype launch failed: " + str(error), file=sys.stderr)
    finally:
        if process and process.poll() is None:
            try:
                if qmp:
                    qmp.execute("quit")
                else:
                    process.terminate()
                process.wait(timeout=10)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=10)
        if qmp:
            qmp.close()
        if bridge:
            bridge.close()
        if before is not None:
            result["disk_sha256_after"] = file_hash(args.disk)
            result["disk_unchanged"] = result["disk_sha256_after"] == before
            if not result["disk_unchanged"]:
                result.update(status="failed", error="Backing disk hash changed")
            (args.output / "session.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if not args.dry_run:
        print("Session %s; backing disk unchanged: %s" % (result["status"], result.get("disk_unchanged")), flush=True)
    return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
