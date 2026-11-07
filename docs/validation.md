# Prototype validation

Snapshot date: **2026-11-07**. QEMU base: **v10.1.0**, commit
`f8b2f64e2336a28bf0d50b6ef8a7d8c013e9bcf3`. Research host: Windows,
MSYS2 UCRT64, native `arm-softmmu` build. Python helpers require 3.11 or newer.

## Patch and artifact checks

The three patches apply cleanly to the pinned base, reproduce all **61**
recorded target files, and reverse to the base without added source files.
The resulting files were also hash-verified in a separate full QEMU checkout.
[The patch manifest](../patches/manifest.json) records these checks and hashes.

All six screenshot hashes and PNG dimensions match
[their manifest](../screenshots/manifest.json). The firmware manifest contains
metadata only. The release includes source patches, helpers, documents and
screenshots; original firmware and private runtime artifacts stay outside it.
[validation.json](validation.json) records the component and launcher results;
[SHA256SUMS](../SHA256SUMS) covers every other file in the public folder.

## Synthetic component results

These checks use generated RAM/disk fixtures and synthetic instructions.
They do not require Samsung firmware.

| Check | Verified groups | Result |
| --- | ---: | --- |
| FCIE/eMMC enumeration, DMA and interrupts | 7 | Pass |
| GOP composition, formats, mirrors and rotated panel | 26 | Pass |
| Mali register, MMU, page-fault and IRQ handling | 13 | Pass |
| Mali rendering, shader dependencies, textures and writeback | 27 | Pass |
| RIU, MIU/USB status and LCD/Full HD panel straps | 13 | Pass |
| Secure-monitor bootstrap on both CPUs | 7 | Pass |
| Secondary-core mailbox, wake-up and reset | 5 | Pass |
| MST12 A9 timers and watchdog | 12 | Pass |
| Existing `vexpress-a9` timer fallback | 12 | Pass |
| Optional bridge packet/TLS protocol | 11 | Pass |

That is **133 check groups**. Component results were retained from export
validation against the current research binary. GOP and RIU were rerun after
removing the exported tools' workspace-specific DLL lookup. The bridge check
includes real loopback TLS 1.0 handshakes, split packet headers, checksum
preservation and delayed ClientHello; it does not claim successful online app
downloads.

Local QEMU style checks had already passed for the current machine, GOP and
shader/renderer changes. The prototype is maintained independently; running
`checkpatch.pl` is not an upstream submission.

## Launcher checks

The exported launcher was exercised using the locally supplied original
kernel and the new synthetic disk. A bounded fresh boot captures the
1920×1080 panel and leaves the disk's SHA-256 unchanged. A separate viewer
session verifies the served page, actual PNG capture, a Menu tap through the
MICOM property and shutdown through **Stop TV**. The optional bridge was also
attached for a bounded launch and shut down with the VM.

On this host, a legacy GTK library on PATH initially caused an invalid socket
descriptor and a QMP timeout. Explicitly selecting the matching UCRT64 DLL
directory resolved it. The launcher now supports `--runtime-dir` and establishes
QMP while the guest is paused before continuing boot.

These short launch checks verify packaging and control transport. They do not
automatically recognize a completed Orsay dashboard. The published Smart Hub
captures come from the retained research VM described in the screenshot
manifest, after interactive setup and required agreement.

## Reproduce component checks

After building the patched QEMU, run these from the prototype repository.
Replace `build/qemu-system-arm` with your actual executable, including `.exe`
on Windows. In PowerShell, ensure the matching runtime is on PATH first:

```powershell
$env:PATH = 'C:\msys64\ucrt64\bin;' + $env:PATH
```

Run the firmware-free qtest suites:

```sh
python3 tools/qemu_fcie_smoke.py --qemu build/qemu-system-arm
python3 tools/qemu_gop_smoke.py --qemu build/qemu-system-arm --panel-rotate-180
python3 tools/qemu_mali_smoke.py --qemu build/qemu-system-arm
python3 tools/qemu_mali_render_smoke.py --qemu build/qemu-system-arm
python3 tools/qemu_riu_smoke.py --qemu build/qemu-system-arm
python3 tools/qemu_timer_smoke.py --qemu build/qemu-system-arm
python3 tools/qemu_timer_smoke.py --qemu build/qemu-system-arm --machine vexpress-a9 --output work/qemu-smoke/timers-vexpress
```

The synthetic SMP and SMC instruction tests additionally need Keystone. Install
it in a virtual environment (shown here for a Linux shell):

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements-test.txt
python3 tools/qemu_smp_smoke.py --qemu build/qemu-system-arm
python3 tools/qemu_smc_smoke.py --qemu build/qemu-system-arm --secure-monitor bootstrap
```

On Windows, create `.venv` with `python -m venv .venv`, then use
`.venv/Scripts/python.exe` for the pip and instruction-test commands.

For the standalone bridge protocol check, install OpenSSL 3 on PATH and run:

```sh
python3 tools/orsay_network_bridge_smoke.py --output work/network-bridge/smoke
```

Reports and generated fixtures are written under ignored `work/` directories.
Use a fresh `--output` path when retaining separate runs.

## Limits of this evidence

The export was checked against the exact pinned source and its existing
Windows research build. It has not yet been rebuilt on Linux or tested on a
different host. The screenshots use the earlier independent ALU renderer;
the patch series contains the later, synthetically verified shader lowering.

No physical board RAM map, cycle accuracy, secure OS, original bootloader
execution, working video/audio, visible browser UI or restarted setup
persistence is established. [NOTES.md](../NOTES.md) lists the remaining work.
