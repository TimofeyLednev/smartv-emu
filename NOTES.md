# UE40F6650 / MST12 board notes

Prototype snapshot: 2026-11-07. Source firmware: **T-MST12DEUC 1112.2**,
2013-09-11. Its kernel identifies itself as **Linux 3.0.33**, and its board
strings identify the MStar Edison platform. Model capacity and panel resolution
were supplied by the owner; the motherboard has not been measured or dumped.

These notes distinguish recovered firmware behavior from choices made to run
it in QEMU. Source-file names below refer to the files created by the patches
inside a QEMU checkout.

## UE40F6650 profile

| Item | Prototype configuration and evidence |
| --- | --- |
| CPU | Cortex-A9, one or two cores; the application launcher uses two. Exact silicon/cache revision remains unverified. |
| Panel | 1920×1080, matching the owner's model specification. Original application surfaces can be 1280×720; early framebuffer buffers are 800×1080. |
| RAM | `-m 512M`, matching the owner's reported capacity. Extra vendor regions exceed that physical budget. |
| Clocks | Firmware-compatible bootstrap CPU PLL configuration corresponds to 900 MHz; A9 timers/watchdog receive 450 MHz. Physical clock rates and runtime PLL changes remain unverified. |
| Panel strap | GPIO43 defaults high to select the original LCD branch; GPIO10 defaults high for the original Full HD branch. Electrical pulls are unmeasured. |
| Orientation | Machine rotation defaults off. The application launcher selects 180° for upright output in this synthetic configuration. |
| Factory EEPROM | Synthetic 32 KiB store; only the emulated NIC's MAC is seeded. No recovered factory identity. |
| Storage | Synthetic GPT with firmware partition names; no original flash layout or U-Boot payload is available. |

## Kernel and reserved memory

The Linux bring-up bank starts at `0x40200000`. QEMU's standard machine RAM is
split into at most 256 MiB there and a remainder at `0x60000000`. The launcher
restricts Linux's first bank below the original graphics reservations:

```text
LX_MEM=0x40200000,0xc600000
LX_MEM2=0x60000000,0x10000000
EMAC_MEM=0x50e00000,0x100000
```

The second-bank placement and size are emulator choices using the kernel's
`LX_MEM2` ABI. The launcher exposes 198 MiB plus 256 MiB to Linux before its
own accounting. Some original vendor requests need separately backed regions:

| CPU physical address | Size / purpose | Evidence or limitation |
| --- | --- | --- |
| `0x40000000` | 2 MiB bootstrap region | Synthetic monitor vectors, loader and secondary-core entry |
| `0x40200000` | First Linux bank | Firmware kernel mapping; allocation size is provisional |
| `0x50e00000` | 1 MiB Ethernet DMA pool | Required `EMAC_MEM` ABI; placement is provisional |
| `0x50f00000` | Two 4 KiB pages | Synthetic secure-monitor shared memory and empty log |
| `0x51000000` | 32 MiB display pool | Early framebuffer MIU-relative base `0x11000000` |
| `0x53000000` | `0x8e4000` bytes | Additional vendor MIU0 requests |
| `0x53904000` | 140 MiB | Original GOP_1 reservation |
| `0x5c504000` | `0x1495000` bytes | Additional vendor MIU0 requests |
| `0x5da80000` | `0x41000` bytes | Original kernel TV log mapping, overlapping vendor space |
| `0x60000000` | Second Linux bank | Provisional use of the original second-bank ABI |
| `0xa0000000` | `0x5702000` bytes | Vendor MIU1 requests, including loaded decoder data |
| `0xa5800000` | 4 MiB | Original GOP_0 request |
| `0xa5c00000` | 2 MiB | Additional vendor MIU1 requests |

These regions intentionally preserve the current diagnostic behavior. They
do not establish a consistent 512 MiB physical DRAM layout. Reconciling vendor
allocations and aliases with the real board remains required work.

## Recovered device addresses

Firmware kernel addresses in the `0xfd...` RIU window correspond to the physical
`0x1f...` addresses below. Interrupt numbers are the firmware's numbering;
the board model aggregates MStar interrupt banks into its Cortex-A9 GIC.

| Device | CPU physical address | Current implementation |
| --- | --- | --- |
| Cortex-A9 private peripherals/GIC | `0x16000000` | QEMU A9 MPCore, custom timer input clocks |
| L2 controller | `0x15000000` | QEMU `l2x0` model |
| RIU | `0x1f000000`, 4 MiB | Spaced register windows and interrupt aggregation |
| UART0 | `0x1f201300` | `serial-mm`, spaced 16550-style register access |
| PIU UART1 / MICOM | `0x1f220c00` | Original packet protocol and timed remote releases |
| FCIE/eMMC | `0x1f222600` | Command/response FIFO, EXT_CSD, RAM DMA and interrupts |
| Ethernet MAC / PHY | `0x1f204000` / `0x1f245000` | TX FIFO, RX descriptor ring, filtering and firmware IRQ201 |
| GE | `0x1f205000` | Guest RAM drawing and completion |
| GOP banked window / global commit | `0x1f203e00` / `0x1f203ffc` | Layer scanout, mux, alpha and stretch |
| GOP direct banks | `0x1f240400` | Shared storage with banked registers |
| Mali | `0x1f280000` | GP/four PP units, five MMUs, shaders, textures and firmware IRQ245 |
| Scaler MLoad | `0x1f263e00` | Latched RAM DMA and ordered scaler register writes |
| PQ coefficient SRAM | `0x1f264700` | Y/C banks, timer completion, readback and migration state |
| Hardware IIC masters | `0x1f223000` + port offsets | Four masters, timed PIO and MIU0 DMA |
| Factory GPIO IIC2 | `0x1f205620` | Shared EEPROM via GPIO66/67 |
| PM eFuse controller | `0x1f004000` | Command/busy/data behavior; synthetic fuse contents |
| TSP | `0x1f202a00` | Partial firmware-facing command and polling behavior |
| MIU PLL | `0x1f221a00` / `0x1f221b00` | Bootstrap coefficients and control storage |
| PCMCIA | `0x1f206880` | Empty-slot status and bounded bus cycles |
| MVD | `0x1f202200` | Timed reset; no Aeon R2 video execution |
| EHCI controllers | `0x1f204800`, `0x1f201a00`, `0x1f227200` | QEMU EHCI adaptation; broader firmware/device coverage remains incomplete |

The secondary-core boot mailbox is at `0x1f206740` / `0x1f206744`, with the
firmware's wake-up SGI. CPU1 starts after the complete mailbox and interrupt,
and returns to the parked state on reset.

## Display and input

GE and Mali draw into guest RAM. GOP composes that RAM into a 1920×1080 QEMU
console. Implemented formats include ARGB8888 and RGB565 scanout, layer mux,
alpha, clipping, horizontal/vertical mirrors and 12-bit stretch ratios.
The original 1280×720 application surfaces use `0xaab` stretch ratios.

GOP RBLK read offset supplies the starting mirrored row independently of the
buffer base. Mirror coordinates are applied before stretch sampling. Panel
rotation occurs after composition.

The software Mali implementation executes a recovered subset of vertex and
fragment instructions, including textured triangles, blending, scissor and
writeback. It preserves unwritten varying components and accounts for aliased
and parallel ALU dependencies. Unsupported jobs produce explicit errors.
Cache behavior, further instruction/texture formats, display interrupts,
filter fidelity and physical GPU timing remain incomplete.

MICOM taps arrive through the original UART1 protocol. Smart Hub is remote key
`0x79`; Menu is `0x1a`. Timed release packets reach the original release path,
so taps can be distinguished from held buttons. The browser viewer sends keys
through the machine's MICOM property and captures the actual GOP console.

## Secure-monitor bootstrap

The machine offers `secure-monitor=off`, `zero` and `bootstrap`. `zero` returns
`r0=0` after SMC while preserving other registers and returning to Normal World;
that answer is insufficient for `tzdrv`'s memory queries.

`bootstrap` supplies a 4 KiB shared page, a separate empty 4 KiB log,
registration metadata and bounded known ABI replies. Unknown services report
an error. It allows the original `tzdrv` to initialize but does not run
Samsung's secure OS, trusted applications, DRM or a secure boot chain.

## Network and storage limits

The original Ethernet driver exchanges real packets through QEMU user
networking. Its configured synthetic MAC reaches the firmware via EEPROM
offset `0x1e04`. Other factory data remains synthetic or empty.

The optional host TLS bridge retains actual Samsung responses. The retained
research VM parsed model `13_X12`, a RU panel list and the complete terms
document, then reached ordinary setup completion and the Apps dashboard.
Those observations depend on live service availability and are not a model of
Samsung's servers. App synchronization and a visible browser remain unresolved.

The disk builder copies unmodified payloads into two banks and reserves empty
writable partitions. GPT offsets, sizes and GUIDs are prototype choices.
Snapshots discard guest disk writes at shutdown; EEPROM persistence and setup
survival across restart have not been established.

## Next hardware work

1. Resolve black dashboard regions, color fidelity and visible browser output
   using captured original rendering jobs.
2. Reconcile the original vendor allocations with the reported 512 MiB DRAM.
3. Recover the physical factory configuration and establish persistent storage.
4. Extend secure services only from verified ABI evidence.
5. Implement Aeon R2 decoder execution, audio and additional GPU/scaler behavior.
6. Recover an original bootloader and validate the BootROM/MBOOT/U-Boot path.

Useful public references are [QEMU](https://www.qemu.org/),
[linux-chenxing.org](https://linux-chenxing.org/),
[SamyGO](https://forum.samygo.tv/) and
[unixtract](https://github.com/theubusu/unixtract). Cross-model MStar data is a
research lead rather than proof of this board's wiring.
