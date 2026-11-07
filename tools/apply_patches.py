#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Check and apply this prototype's patch series to a clean QEMU v10.1.0."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def git(source, *args):
    result = subprocess.run(["git", "-C", str(source), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout.decode("utf-8", errors="replace").strip()


def verify_sources(source, manifest):
    for name, expected in manifest["source_files"].items():
        path = (source / name).resolve()
        if not path.is_relative_to(source):
            raise ValueError("Source path escapes QEMU checkout")
        data = path.read_bytes().replace(b"\r\n", b"\n")
        if len(data) != expected["bytes"] or hashlib.sha256(data).hexdigest() != expected["sha256"]:
            raise ValueError("Patched source differs from this prototype: " + name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="QEMU v10.1.0 Git checkout")
    parser.add_argument("--check", action="store_true", help="Check without changing QEMU")
    parser.add_argument("--verify", action="store_true", help="Verify an already patched source tree")
    args = parser.parse_args()
    if args.check and args.verify:
        parser.error("Select either --check or --verify")
    source = args.source.resolve()
    try:
        manifest = json.loads((ROOT / "patches/manifest.json").read_text(encoding="utf-8"))
        if git(source, "rev-parse", "HEAD") != manifest["base_commit"]:
            raise ValueError("Use the exact QEMU v10.1.0 base commit: " + manifest["base_commit"])
        if args.verify:
            verify_sources(source, manifest)
            print("Verified all %d patched QEMU files" % manifest["files_changed"])
            return 0
        if git(source, "status", "--porcelain", "--untracked-files=no"):
            raise ValueError("Use a clean QEMU checkout; tracked files have local changes")
        series = (ROOT / "patches/series").read_text(encoding="utf-8").splitlines()
        if series != [patch["file"] for patch in manifest["patches"]]:
            raise ValueError("Patch series and manifest disagree")
        files = []
        for patch in manifest["patches"]:
            path = (ROOT / "patches" / patch["file"]).resolve()
            if path.parent != (ROOT / "patches").resolve():
                raise ValueError("Invalid patch filename")
            if hashlib.sha256(path.read_bytes()).hexdigest() != patch["sha256"]:
                raise ValueError("Patch hash differs from the manifest: " + patch["file"])
            files.append(str(path))
        git(source, "apply", "--check", *files)
        if args.check:
            print("All three patches apply cleanly to QEMU v10.1.0")
            return 0
        git(source, "apply", *files)
        verify_sources(source, manifest)
        print("Applied three patches; verified all %d QEMU files" % manifest["files_changed"])
        return 0
    except (OSError, ValueError, KeyError) as error:
        print("Patch preparation failed: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
