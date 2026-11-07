#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Build the synthetic 2 GiB eMMC disk from locally supplied firmware images.

Partition names/numbers follow T-MST12DEUC 1112.2. Offsets, sizes and GUIDs
are emulator choices, not a physical TV flash dump. Inputs are hash checked.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import uuid
import zlib

ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024
NAMES = ["", "UBOOT", "NUTX_0", "NUTX_1", "EX_PARTITION", "SERET_0", "SERET_1",
         "KERNEL_0", "ROOTFS_0", "KERNEL_1", "ROOTFS_1", "SECUREMAC0", "SECUREMAC1",
         "SECUREMAC2", "DRMREGION_A", "DRMREGION_B", "RWAREA", "EXE_0", "EXE_1",
         "UNCOMPEXE_0", "UNCOMPEXE_1", "ROCOMMON_0", "ROCOMMON_1", "EMANUAL",
         "CONTENTS", "SWU", "RWCOMMON"]
PAYLOADS = {5: "seret.bin", 6: "seret.bin", 7: "uImage", 8: "rootfs.img",
            9: "uImage", 10: "rootfs.img", 17: "exe.img", 18: "exe.img",
            19: "uncompexe.img", 20: "uncompexe.img", 21: "rocommon.img", 22: "rocommon.img"}


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def gpt_header(current, alternate, entries_lba, sectors, disk_guid, entries_crc):
    header = bytearray(struct.pack("<8sIIIIQQQQ16sQIII", b"EFI PART", 0x10000,
                                  92, 0, 0, current, alternate, 34, sectors - 34,
                                  disk_guid.bytes_le, entries_lba, 128, 128, entries_crc))
    struct.pack_into("<I", header, 16, zlib.crc32(header) & 0xFFFFFFFF)
    return bytes(header).ljust(512, b"\0")


def build(images, output, size_mib=2048):
    images, output = images.resolve(), output.resolve()
    if output.exists():
        raise ValueError("Choose a new output filename; an existing disk is never replaced")
    if size_mib < 2048 or size_mib > 16384 or size_mib & (size_mib - 1):
        raise ValueError("Disk size must be a power of two between 2048 and 16384 MiB")
    manifest = json.loads((ROOT / "docs/firmware-manifest.json").read_text(encoding="utf-8"))
    expected = {item["file"]: item for item in manifest["payloads"]}
    for name in sorted(set(PAYLOADS.values())):
        path = images / name
        entry = expected[name]
        if path.stat().st_size != entry["bytes"] or file_hash(path) != entry["sha256"]:
            raise ValueError("Firmware image differs from T-MST12DEUC 1112.2: " + name)
    sectors = size_mib * MIB // 512
    namespace = uuid.UUID("291bfb71-03c3-4e43-b730-9f0ee84cda92")
    disk_guid = uuid.uuid5(namespace, "SamsungF diagnostic GPT")
    type_guid = uuid.UUID("0fc63daf-8483-4772-8e79-3d69d8477de4")
    entries, partitions, cursor = bytearray(128 * 128), [], 2048
    for index, name in enumerate(NAMES[1:], 1):
        payload = PAYLOADS.get(index)
        minimum = 285 if index in (19, 20) else 64 if index in (14, 15, 16, 26) else 8
        size = max(minimum, (expected[payload]["bytes"] + MIB - 1) // MIB if payload else 0) * MIB
        last = cursor + size // 512 - 1
        if last >= sectors - 33:
            raise ValueError("Partitions do not fit the requested disk size")
        struct.pack_into("<16s16sQQQ72s", entries, (index - 1) * 128,
                         type_guid.bytes_le, uuid.uuid5(namespace, name).bytes_le,
                         cursor, last, 0, name.encode("utf-16le").ljust(72, b"\0"))
        partitions.append({"number": index, "name": name, "first_lba": cursor,
                           "last_lba": last, "bytes": size, "payload": payload})
        cursor = last + 1
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as disk:
        disk.truncate(size_mib * MIB)
        mbr = bytearray(512)
        struct.pack_into("<B3sB3sII", mbr, 446, 0, b"\0\x02\0", 0xEE, b"\xff\xff\xff",
                         1, min(sectors - 1, 0xFFFFFFFF))
        mbr[510:] = b"\x55\xaa"
        crc = zlib.crc32(entries) & 0xFFFFFFFF
        for offset, data in ((0, mbr), (512, gpt_header(1, sectors - 1, 2, sectors, disk_guid, crc)),
                             (1024, entries), ((sectors - 33) * 512, entries),
                             ((sectors - 1) * 512, gpt_header(sectors - 1, 1, sectors - 33,
                                                          sectors, disk_guid, crc))):
            disk.seek(offset)
            disk.write(data)
        for part in partitions:
            if part["payload"] is None:
                continue
            path = images / part["payload"]
            digest = hashlib.sha256()
            disk.seek(part["first_lba"] * 512)
            with path.open("rb") as source:
                while chunk := source.read(8 * MIB):
                    disk.write(chunk)
                    digest.update(chunk)
            if digest.hexdigest() != expected[path.name]["sha256"]:
                raise ValueError("Firmware image changed during copying: " + path.name)
            part.update(payload_bytes=expected[path.name]["bytes"],
                        payload_sha256=digest.hexdigest())
            print("Copied %s to %s" % (path.name, part["name"]), flush=True)
    result = {"purpose": "Synthetic GPT/eMMC prototype disk; physical layout unverified",
              "firmware": "T-MST12DEUC 1112.2", "disk_bytes": size_mib * MIB,
              "disk_sha256": file_hash(output), "root_device": "/dev/mmcblk0p8",
              "source_images_verified": True, "partitions": partitions}
    output.with_suffix(output.suffix + ".json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=Path, required=True, help="Local decrypted payload directory")
    parser.add_argument("--output", type=Path, required=True, help="New raw disk outside the public repository")
    parser.add_argument("--size-mib", type=int, default=2048)
    args = parser.parse_args()
    try:
        result = build(args.images, args.output, args.size_mib)
        print("Prepared synthetic disk; SHA-256: " + result["disk_sha256"])
        return 0
    except (OSError, ValueError, KeyError) as error:
        print("Disk preparation failed: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
