# Build and run

This snapshot targets **QEMU v10.1.0** and **T-MST12DEUC 1112.2**, built on
2013-09-11. Different QEMU or firmware versions have not been validated.
The helper scripts require **Python 3.11 or newer**. A complete run also needs
your locally extracted firmware and approximately 2 GiB for the synthetic disk.

## Apply the patches

Run these commands from the prototype repository. Git and Python must be on
your PATH. On Windows, use `python` in place of `python3` if appropriate.

```sh
git clone --depth 1 --branch v10.1.0 https://gitlab.com/qemu-project/qemu.git qemu-source
python3 tools/apply_patches.py qemu-source --check
python3 tools/apply_patches.py qemu-source
python3 tools/apply_patches.py qemu-source --verify
```

The helper checks the exact base commit and patch hashes, refuses tracked local
changes, then verifies all 61 resulting files. The patches are applied with
`git apply`; they are plain diffs rather than email patches for `git am`.

| Order | Patch | Purpose |
| --- | --- | --- |
| 1 | `0001-arm-timer-input-clocks.patch` | Clock inputs for Cortex-A9 timers and watchdog; preserves the existing fallback on other machines |
| 2 | `0002-mst12-device-models.patch` | MST12 peripherals, renderer, headers and build integration |
| 3 | `0003-samsung-ue40f6650-machine.patch` | Board wiring, provisional memory map and optional secure monitor |

## Linux build

A recent Debian/Ubuntu host can install the build prerequisites with:

```sh
sudo apt-get install build-essential git python3 python3-venv ninja-build pkg-config libglib2.0-dev libpixman-1-dev zlib1g-dev libslirp-dev
mkdir build
cd build
../qemu-source/configure --target-list=arm-softmmu --without-default-features --enable-tcg --enable-download --enable-pixman --enable-vnc --enable-slirp --disable-werror --disable-docs --disable-guest-agent --disable-tools
ninja qemu-system-arm
./qemu-system-arm -machine help
cd ..
```

The machine list should include `samsung_mstar_ue40f6650`. The browser viewer
uses QMP screen captures and does not require a GTK or SDL window. Libslirp
provides the optional user-mode Ethernet connection.

The research build was tested on Windows with MSYS2 UCRT64. The Linux commands
describe the corresponding native build; a Linux build of this exported
snapshot has not yet been run.

## Windows build

Use an **MSYS2 UCRT64** terminal and keep that toolchain's libraries together:

```sh
pacman -S --needed git make mingw-w64-ucrt-x86_64-gcc mingw-w64-ucrt-x86_64-python mingw-w64-ucrt-x86_64-ninja mingw-w64-ucrt-x86_64-pkgconf mingw-w64-ucrt-x86_64-glib2 mingw-w64-ucrt-x86_64-pixman mingw-w64-ucrt-x86_64-zlib mingw-w64-ucrt-x86_64-libslirp
mkdir build
cd build
../qemu-source/configure --target-list=arm-softmmu --without-default-features --enable-tcg --enable-download --enable-pixman --enable-vnc --enable-slirp --disable-werror --disable-docs --disable-guest-agent --disable-tools
ninja qemu-system-arm.exe
./qemu-system-arm.exe -machine help
cd ..
```

Apply the patches first, using the commands above. The terminal must be UCRT64
so that GLib and QEMU use the same C runtime. When launching from PowerShell or
Command Prompt, pass `--runtime-dir C:/msys64/ucrt64/bin` for a standard MSYS2
installation, or your installation's corresponding directory. This prepends
that directory to the child QEMU process's PATH.

An older GTK installation on PATH caused the exported launcher's QMP socket to
fail on the research host. Selecting the matching UCRT64 runtime resolves that
library mix. The repository does not bundle QEMU executables or DLLs.

## Supply your firmware locally

Extract your own **T-MST12DEUC 1112.2** upgrade package using an MSD11-capable
extractor. [unixtract](https://github.com/theubusu/unixtract) is a useful public
format reference and extraction tool. Its documented invocation is:

```sh
unixtract /path/to/upgrade.msd /path/to/private/extracted
```

The prototype consumes extracted images; it does not bundle a firmware
download or an extractor. Gather these six files in one private directory:

```text
uImage
rootfs.img
exe.img
uncompexe.img
rocommon.img
seret.bin
```

Extraction tools may use different output names or additional subdirectories.
Use [firmware-manifest.json](firmware-manifest.json) to identify each payload by
its size and SHA-256 before naming it for the disk builder. `secos.bin` is listed
for provenance but is not used by the bootstrap monitor.

Build a new synthetic disk outside the public repository:

```sh
python3 tools/prepare_disk.py --images /path/to/private/images --output /path/to/private/tv.raw
```

The builder verifies every required input, copies the original payload bytes,
and writes a GPT with two application banks and empty writable partitions.
It refuses to overwrite an existing disk. A `tv.raw.json` sidecar records the
partition choices, copied payload hashes and complete disk hash.

This is a **synthetic 2 GiB disk**, not a flash dump of the TV. Empty writable
filesystems and missing factory records cause expected startup warnings and
can affect settings persistence. Disk size does not describe physical RAM.

## Launch the original applications

On Linux:

```sh
python3 tools/run_orsay.py --qemu build/qemu-system-arm --kernel /path/to/private/images/uImage --disk /path/to/private/tv.raw
```

On Windows, from this repository:

```powershell
.\Run-Orsay.cmd --qemu build/qemu-system-arm.exe --runtime-dir C:/msys64/ucrt64/bin --kernel C:/private/images/uImage --disk C:/private/tv.raw
```

Quote any argument whose path contains spaces. `Run-Orsay.cmd` uses a local
`.venv` if present and otherwise uses `python` from PATH. Basic launch and
viewer operation use Python's standard library only.

QEMU starts paused so the launcher can establish control, then resumes the
original kernel. The defaults are two cores, single-thread TCG, `-m 512M`,
`-icount shift=3`, a 1920×1080 panel and 180° output rotation. These reproduce
the current diagnostic profile, including its additional vendor RAM regions.

The launcher prints and normally opens `http://127.0.0.1:<port>/`. The viewer
captures the VM's display and sends MICOM taps. **Menu** opens Samsung settings;
**Smart Hub** opens Hub startup. Startup and navigation can be slow. Black
captures during initialization are possible, and a fixed wait does not prove
that the dashboard has loaded.

Useful options:

| Option | Effect |
| --- | --- |
| `--offline` | Disconnect the Ethernet device |
| `--no-open` | Print the local viewer address without opening a browser |
| `--port 60609` | Request a particular free localhost viewer port |
| `--panel-rotation 0` | Inspect unrotated panel output |
| `--seconds 120` | Make a bounded observation and save `screen.png`, without serving a viewer |
| `--output /path/to/private/run` | Select a fresh output directory |
| `--debug` | Save QEMU unimplemented-operation and guest-error diagnostics |
| `--network-capture` | Save the guest's Ethernet packets locally |
| `--dry-run` | Validate inputs and print the base QEMU command without starting it |

The default output is an ignored `work/orsay-<timestamp>/` directory. It contains
serial logs and `session.json`. The viewer also records its address, captures
and remote events. **Stop TV** or **Ctrl+C** shuts down only this launch.
QEMU uses `snapshot=on`; the launcher verifies that the backing disk's hash
is unchanged after shutdown. VM writes are temporary.

## Optional Smart Hub TLS bridge

The original firmware uses legacy TLS for Hub setup. The optional bridge
translates only selected Hub HTTPS connections, accepts the original TLS 1.0
handshake and forwards requests with TLS 1.2 or newer. It retains upstream
certificate, hostname and date verification and forwards actual service
responses.

Supply the original `InfoLink/lib/ca_hubsite.pem` recovered from `exe.img` and
OpenSSL 3 on the host. The CA's DER SHA-256 must be:

```text
c2441cd566311b0f770ffc2d2a02588dc4bf08f8fc898e4b0e416cc862053700
```

For example, extend the Windows command with:

```powershell
--hubsite-ca C:/private/ca_hubsite.pem --openssl C:/msys64/ucrt64/bin/openssl.exe
```

On Linux, install OpenSSL and use `--hubsite-ca /path/to/private/ca_hubsite.pem`.
The bridge remains active for the launch and defaults to a 900-host-second
guest timeout. `--hubsite-timeout` can change it. The bridge is incompatible
with `--offline`.

The firmware's original country and terms UI still makes its own requests.
Choose agreements through its normal remote interface; no consent is selected
by the launcher. Availability of Samsung's legacy services, application
synchronization and a visible browser window remain separate concerns.
