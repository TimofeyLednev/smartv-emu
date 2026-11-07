#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Check functional Utgard polygon setup and pixel output with synthetic RAM.

No Samsung firmware is used. Raw GP-to-PP packet encoding is represented
internally; unknown geometry and shaders must report an error.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import struct
import subprocess

from qemu_fcie_smoke import Qtest
from qemu_mali_smoke import GP, MIU, MMUS, PPS, Mali, require
from qemu_qmp import QMP, free_port
PROJECT = Path(__file__).resolve().parents[1]

COMMANDS = 0x00408000
ARRAY = 0x00409000
BLOCKS = 0x0040A000
HEAP = 0x0040C000
VS_COMMANDS = 0x00410000
VS_CODE = 0x00411000
VS_UNIFORMS = 0x00412000
VS_ATTRIBUTES = 0x00413000
VS_VARYINGS = 0x00414000
VS_INPUT = 0x00415000
VS_OUTPUT = 0x00416000
PP_STATE = 0x00417000
PP_SHADER = 0x00418000
DRAW_STATE = 0x00419000
PP_LIST = 0x0041A000
TARGET = 0x00420000
POSITIONS = 0x00430000
VARYINGS = 0x00431000
TEX_DESCRIPTOR = 0x00432000
TEX_ARRAY = 0x00433000
TEX_IMAGE = 0x00440000
RENDER_PD = 0x00450000
RENDER_PT = 0x00451000

GP_FIELDS = [("mul0_src0", 5), ("mul0_src1", 5), ("mul1_src0", 5), ("mul1_src1", 5),
             ("mul0_neg", 1), ("mul1_neg", 1),
             ("acc0_src0", 5), ("acc0_src1", 5), ("acc1_src0", 5), ("acc1_src1", 5),
             ("acc0_neg0", 1), ("acc0_neg1", 1), ("acc1_neg0", 1), ("acc1_neg1", 1),
             ("load_addr", 9), ("load_offset", 3), ("register0", 4), ("attribute0", 1),
             ("register1", 4), ("temporary0", 1), ("temporary1", 1),
             ("branch", 1), ("branch_lo", 1),
             ("store_x", 3), ("store_y", 3), ("store_z", 3), ("store_w", 3),
             ("acc_op", 3), ("complex_op", 4), ("store0", 4), ("varying0", 1),
             ("store1", 4), ("varying1", 1), ("mul_op", 3), ("pass_op", 3),
             ("complex_src", 5), ("pass_src", 5), ("unknown", 4), ("branch_target", 8)]


def gp_instruction(**fields):
    defaults = {name: 21 for name, _ in GP_FIELDS if "src" in name}
    defaults.update({name: 7 for name, _ in GP_FIELDS if name.startswith("store_")})
    defaults.update(load_offset=7, pass_op=2)
    defaults.update(fields)
    value = shift = 0
    for name, size in GP_FIELDS:
        value |= defaults.get(name, 0) << shift
        shift += size
    assert shift == 128
    return value.to_bytes(16, "little")


def vs_packets(shader_size=32, count=2):
    return [(VS_CODE, 0x40000000 | shader_size << 12), (0, 0x10000040),
            (0, 0x10000042), (VS_UNIFORMS, 0x30010000),
            (VS_ATTRIBUTES, 0x20020000), (VS_VARYINGS, 0x20020008),
            (3, 0x10000041), (count << 24, 0),
            (0, 0x60000000), (0, 0x50000000)]


def float_word(value):
    return struct.unpack("<I", struct.pack("<f", value))[0]


def setup_packets():
    return [(0x200, 0x1000010B), (0, 0x1000010C),
            (0x01000000, 0x10000109), (2, 0x30000000),
            (ARRAY, 0x28000001), (0, 0x10000107),
            (float_word(32), 0x10000108), (0, 0x10000105),
            (float_word(16), 0x10000106), (HEAP, 0x10000103),
            (HEAP + 4096, 0x10000104), (0, 0x50000000)]


def pp_instruction(fields, stop=True):
    sizes = (34, 62, 41, 43, 30, 44, 31, 30, 41, 73, 64, 64)
    value, shift, mask = 0, 32, 0
    for index, size in enumerate(sizes):
        if index in fields:
            assert 0 <= fields[index] < 1 << size
            value |= fields[index] << shift
            shift += size
            mask |= 1 << index
    words = (shift + 31) // 32
    value |= words | int(stop) << 5 | 1 << 6 | mask << 7
    return value.to_bytes(words * 4, "little")


def constant_fragment(color):
    constant = int.from_bytes(struct.pack("<4e", *color), "little")
    move = 12 | 0xE4 << 4 | 15 << 32 | 31 << 38
    return pp_instruction({5: move, 10: constant})


def texture_fragment(varying_offset=15):
    varying = 3 << 5 | varying_offset << 10 | 15 << 24 | 15 << 28
    sampler = 0x39001 << 42
    move = 14 | 0xE4 << 4 | 15 << 32 | 31 << 38
    return pp_instruction({0: varying, 1: sampler, 5: move})


def set_bits(value, start, size, field):
    assert 0 <= field < 1 << size
    return (value & ~(((1 << size) - 1) << start)) | field << start


def texture_descriptor(nearest=True):
    value = 0x16 | 8 << 16
    for start, size, field in ((42, 2, 1), (72, 1, 1), (75, 1, int(nearest)),
                                (76, 1, int(nearest)), (77, 3, 1), (80, 3, 1),
                                (86, 13, 2), (99, 13, 2), (112, 13, 1),
                                (222, 26, TEX_IMAGE >> 6)):
        value = set_bits(value, start, size, field)
    return value.to_bytes(64, "little")


def draw_packets(clip=None):
    packets = setup_packets()[:-1]
    packets += [(0x2600, 0x1000010B), (DRAW_STATE, 0x80000000 | POSITIONS >> 4),
                (TEX_ARRAY + 16, 0x10000101)]
    if clip:
        x0, y0, x1, y1 = clip
        packets.append(((x0 & 3) << 30 | (y1 - 1) << 15 | y0,
                        0x70000000 | (x1 - 1) << 13 | x0 >> 2))
    packets += [(6 << 24, 0x00240000), (0, 0x50000000)]
    return packets


def run(qemu, output):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    port, qmp_port = free_port(), free_port()
    command = [str(qemu.resolve()), "-machine", "samsung_mstar_ue40f6650",
               "-accel", "qtest", "-m", "128M", "-display", "none",
               "-monitor", "none", "-serial", "null", "-nic", "none",
               "-qtest", "tcp:127.0.0.1:%d,server=on,wait=off" % port,
               "-qtest-log", str(output / "qtest.log"),
               "-qmp", "tcp:127.0.0.1:%d,server=on,wait=off" % qmp_port,
               "-global", "mst12-mali.trace-jobs=on",
               "-d", "unimp,guest_errors", "-D", str(output / "qemu-debug.log")]
    environment = os.environ.copy()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = {"scope": "Synthetic functional Utgard command processing; raw polygon encoding unverified",
              "command": command, "status": "failed", "checks": []}
    qmp = connection = qtest = None
    with (output / "qemu-stderr.log").open("wb") as errors:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=errors, env=environment, creationflags=flags)
        try:
            qmp = QMP(qmp_port)
            connection = socket.create_connection(("127.0.0.1", port), timeout=5)
            qtest = Qtest(connection)
            mali = Mali(qtest)

            def count(name):
                return qmp.execute("qom-get", {"path": "/machine/mali", "property": name})

            def start(packets, end=None):
                mali.reset_gp()
                for i, (argument, opcode) in enumerate(packets):
                    mali.memory_word(COMMANDS + i * 8, argument)
                    mali.memory_word(COMMANDS + i * 8 + 4, opcode)
                mali.write(GP, 8, COMMANDS)
                mali.write(GP, 12, end if end is not None else COMMANDS + len(packets) * 8)
                mali.write(GP, 0x10, HEAP)
                mali.write(GP, 0x14, HEAP + 4096)
                mali.write(GP, 0x2C, 2 | 1 << 14)
                mali.write(GP, 0x20, 2)

            def write_bytes(address, data):
                qtest.request("write 0x%x 0x%x 0x%s" % (MIU + address, len(data), data.hex()))

            def start_vs(code, input_descriptor=0x8003, output_descriptor=0x8020):
                mali.reset_gp()
                write_bytes(VS_CODE, code)
                write_bytes(VS_UNIFORMS, struct.pack("<4f", 10, 4, 2, 0.5))
                write_bytes(VS_ATTRIBUTES, struct.pack("<2I", VS_INPUT, input_descriptor))
                write_bytes(VS_VARYINGS, struct.pack("<2I", VS_OUTPUT, output_descriptor))
                write_bytes(VS_COMMANDS, b"".join(struct.pack("<2I", *p) for p in vs_packets(len(code))))
                mali.write(GP, 0, VS_COMMANDS)
                mali.write(GP, 4, VS_COMMANDS + 80)
                mali.write(GP, 0x2C, 1 | 1 << 13)
                mali.write(GP, 0x20, 1)

            def read_bytes(address, size):
                return bytes.fromhex(qtest.request("read 0x%x 0x%x" % (MIU + address, size))[2:])

            def start_pp(color=0xFFC08040, width=0, height=0, layout=0, pitch=20):
                mali.reset_pp(0)
                frame = [0] * 24
                frame[0:2] = [PP_LIST, PP_STATE]
                frame[3:6] = [0x22, 0xFFFFFF, 0]
                frame[6:10] = [color] * 4
                frame[10:12] = [width, height]
                frame[16:23] = [1, 31, 0x77, 1, 0, 0xC0C, 0x8888]
                for index, value in enumerate(frame):
                    mali.write(PPS[0], index * 4, value)
                wb = [2, TARGET, 3, 0, layout, pitch, 0, 0, 0, 0, 0, 0]
                for index, value in enumerate(wb):
                    mali.write(PPS[0], 0x100 + index * 4, value)
                mali.write(PPS[0], 0x1028, 1 | 1 << 9)
                mali.write(PPS[0], 0x100C, 1 << 6)

            def expect_pixels(expected, pitch=160, width=32, height=16):
                actual = read_bytes(TARGET, pitch * 16 + 64)
                for y in range(16):
                    for x in range(pitch // 4):
                        offset = y * pitch + x * 4
                        wanted = expected(x, y) if x < width and y < height else b"\xA5" * 4
                        require(actual[offset:offset + 4] == wanted,
                                "Pixel (%d,%d) is %s, expected %s" %
                                (x, y, actual[offset:offset + 4].hex(), wanted.hex()))
                require(actual[pitch * 16:] == b"\xA5" * 64, "Writeback overran the image")

            def background(mask=0, color=(1, 1, 1, 1)):
                state = [0] * 16
                state[2] = mask << 28
                state[8] = 0xF007
                state[9] = PP_SHADER
                write_bytes(PP_STATE, struct.pack("<16I", *state))
                write_bytes(PP_SHADER, constant_fragment(color))

            def drawing(code, blend=False, textured=False):
                state = [0] * 16
                state[2] = 0xFC8BC412 if blend else 0xFC3B1AD2
                state[3:7] = [0x3E, 0xFFFF0000, 0xFF000007, 0xFF000007]
                state[8] = 0xF807
                state[9] = PP_SHADER + 256
                state[14] = 0x1000
                if textured:
                    state[12:14] = [TEX_ARRAY, 0x4022]
                    state[15] = VARYINGS
                write_bytes(DRAW_STATE, struct.pack("<16I", *state))
                write_bytes(PP_SHADER + 256, code)
                write_bytes(POSITIONS, struct.pack("<16f", 0, 0, 0.5, 1,
                    32, 0, 0.5, 1, 32, 16, 0.5, 1, 0, 16, 0.5, 1))
                write_bytes(TEX_ARRAY + 16, struct.pack("<6H", 0, 1, 2, 0, 2, 3))

            mali.unmask()
            for i in range(2):
                mali.memory_word(ARRAY + i * 4, BLOCKS + i * 512)
                mali.memory_word(BLOCKS + i * 512, 0x12345678 + i)
            start(setup_packets())
            require(mali.read(GP, 0x24) == 2 and not mali.read(GP, 0x68) & 8,
                    "Valid setup-only GP batch did not finish idle")
            require(count("gp-setups") == 1 and count("gp-commands") == 12,
                    "The decoded GP batch was not accounted for")
            require(mali.pending() != 0 and mali.read(GP, 0x84) == COMMANDS + 96,
                    "GP completion IRQ or command pointer is missing")
            result["checks"].append("complete setup decodes tile dimensions, pointers and heap, then signals completion")

            for i in range(2):
                value = int(qtest.request("readl 0x%x" % (MIU + BLOCKS + i * 512)), 0)
                require(value == 0x12345678 + i, "Functional GP setup changed raw polygon records")
            result["checks"].append("functional empty-list representation preserves CPU-visible raw polygon RAM")

            bad = setup_packets()
            bad[3] = (1, 0x30000000)
            start(bad)
            require(mali.read(GP, 0x24) == 1 << 14 and count("gp-setups") == 1,
                    "Inconsistent tile geometry falsely completed")
            result["checks"].append("inconsistent pointer capacity and block stride report command failure")

            start(setup_packets()[:-1])
            require(mali.read(GP, 0x24) == 1 << 14 and count("gp-setups") == 1,
                    "Unterminated setup falsely completed")
            result["checks"].append("missing END cannot create a completed polygon batch")

            bad = setup_packets()
            bad.insert(-1, (3 << 24, 0x00040000))
            start(bad)
            require(mali.read(GP, 0x24) == 1 << 14 and count("gp-setups") == 1,
                    "Unsupported triangle draw falsely completed")
            result["checks"].append("unsupported geometry reports failure rather than producing an empty batch")

            start(setup_packets(), COMMANDS + 95)
            require(mali.read(GP, 0x24) == 1 << 14,
                    "Misaligned command end was accepted")
            result["checks"].append("malformed 64-bit command boundaries report failure")

            arithmetic = (gp_instruction(acc0_src0=0, acc0_src1=12,
                acc1_src0=1, acc1_src1=13, attribute0=1, store_x=0, store_y=1, varying0=1) +
                gp_instruction(mul0_src0=2, mul0_src1=14, mul1_src0=3, mul1_src1=15,
                               attribute0=1, store_z=2, store_w=3, varying1=1))
            write_bytes(VS_INPUT, struct.pack("<8f", 1.25, -2, 3, 1, -0.5, 8, -5, 1))
            start_vs(arithmetic)
            actual = bytes.fromhex(qtest.request("read 0x%x 32" % (MIU + VS_OUTPUT))[2:])
            expected = struct.pack("<8f", 11.25, 2, 6, 0.5, 9.5, 12, -10, 0.5)
            require(actual == expected and mali.read(GP, 0x24) == 1,
                    "Vertex shader arithmetic or float32 output differs from the independent calculation")
            require(count("gp-vertices") == 2, "Executed vertices were not counted")
            result["checks"].append("vertex shader computes add/multiply for separate vertices and writes exact float32 outputs")

            start_vs(arithmetic, output_descriptor=0x800F)
            actual = bytes.fromhex(qtest.request("read 0x%x 32" % (MIU + VS_OUTPUT))[2:])
            require(actual[:8] == struct.pack("<4e", 11.25, 2, 6, 0.5) and
                    actual[16:24] == struct.pack("<4e", 9.5, 12, -10, 0.5),
                    "Vertex varying conversion or per-vertex half-float stride is wrong")
            result["checks"].append("half-float varyings use IEEE rounding and the submitted vertex stride")

            partial = (arithmetic[:16] + gp_instruction(mul0_src0=2, mul0_src1=14,
                attribute0=1, store_z=2, varying1=1))
            for descriptor, size, packing in ((0x8003, 4, "<3f"), (0x800F, 2, "<3e")):
                write_bytes(VS_OUTPUT, b"\xA5" * 48)
                start_vs(partial, output_descriptor=descriptor)
                actual = read_bytes(VS_OUTPUT, 48)
                expected = b"".join(struct.pack(packing, *xyz) + b"\xA5" * (16 - size * 3)
                                    for xyz in ((11.25, 2, 6), (9.5, 12, -10))) + b"\xA5" * 16
                require(actual == expected and mali.read(GP, 0x24) == 1,
                        "Masked varying stores changed unwritten W, stride padding, or following RAM")
                result["checks"].append("XYZ %s varying stores preserve unwritten W, stride padding and guard bytes" %
                                        ("float32" if size == 4 else "half-float"))

            vertices_before_bad_shader = count("gp-vertices")
            bad_shader = gp_instruction(acc0_src0=0, acc0_src1=12, acc_op=3, attribute0=1,
                                         store_x=0, varying0=1)
            start_vs(bad_shader)
            require(mali.read(GP, 0x24) == 1 << 13 and count("gp-vertices") == vertices_before_bad_shader,
                    "Unknown shader opcode falsely completed or changed the successful vertex count")
            result["checks"].append("unknown active vertex opcode reports a GP shader error")

            stream = b"".join(struct.pack("<4I", 0, 0xB8000000 | x,
                0xE0000002 | (BLOCKS + x * 512) >> 3, 0xB0000000) for x in range(2))
            write_bytes(PP_LIST, stream + struct.pack("<2I", 0, 0xBC000000))
            background()
            start(setup_packets())
            write_bytes(TARGET, b"\xA5" * (160 * 16 + 64))
            start_pp()
            require(mali.read(PPS[0], 0x1020) == 1, "Valid clear did not complete PP")
            expect_pixels(lambda x, y: bytes((0xC0, 0x80, 0x40, 0xFF)))
            require(count("pp-tiles") == 2 and count("pp-pixels") == 512,
                    "Real tile and writeback pixel counters are wrong")
            result["checks"].append("PP clear writes exact BGRA pixels for aligned zero-size registers and preserves row padding")

            background(15, (1, 0.25, 0, 0.5))
            write_bytes(TARGET, b"\xA5" * (160 * 16 + 64))
            start_pp(width=30, height=12)
            expect_pixels(lambda x, y: bytes((0, 64, 255, 128)), width=31, height=13)
            result["checks"].append("constant fragment execution honors a partial tile boundary without touching pixels outside the surface")

            background()
            drawing(constant_fragment((1, 0, 0, 0.5)), blend=True)
            start(draw_packets((4, 3, 24, 13)))
            write_bytes(TARGET, b"\xA5" * (160 * 16 + 64))
            start_pp(color=0xFF000000)
            require(mali.read(PPS[0], 0x1020) == 1, "Indexed triangle draw did not complete PP")
            expect_pixels(lambda x, y: bytes((0, 0, 128 if 4 <= x < 24 and 3 <= y < 13 else 0, 255)))
            result["checks"].append("indexed triangle rasterization applies scissor, source-alpha blending, and unique shared-edge coverage")

            drawing(constant_fragment((0, 0.5, 1, 1)))
            start(draw_packets())
            write_bytes(TARGET, b"\xA5" * (160 * 16 + 64))
            start_pp()
            expect_pixels(lambda x, y: bytes((255, 128, 0, 255)))
            result["checks"].append("constant fragment results are reused within a draw and refreshed for a new shader job")

            seed = int.from_bytes(struct.pack("<4e", 0.25, 0.5, 0.75, 1), "little")
            move = 12 | 0xE4 << 4 | 15 << 32 | 31 << 38
            reverse = 0x1B << 4 | 15 << 32 | 31 << 38
            drawing(pp_instruction({5: move, 10: seed}, stop=False) +
                    pp_instruction({5: reverse}))
            start(draw_packets())
            write_bytes(TARGET, b"\xA5" * (160 * 16 + 64))
            start_pp()
            require(mali.read(PPS[0], 0x1020) == 1,
                    "Aliased vector swizzle did not complete PP")
            expect_pixels(lambda x, y: bytes((128, 191, 255, 64)))
            result["checks"].append("single vector ALU captures all source components before an aliased swizzle write")

            parallel_move = 0xE4 << 4 | 1 << 28 | 15 << 32 | 31 << 38
            output_move = 1 | 0xE4 << 4 | 15 << 32 | 31 << 38
            drawing(pp_instruction({3: move, 5: parallel_move, 10: seed},
                                   stop=False) +
                    pp_instruction({5: output_move}))
            start(draw_packets())
            write_bytes(TARGET, b"\xA5" * (160 * 16 + 64))
            start_pp()
            require(mali.read(PPS[0], 0x1020) == 1,
                    "Parallel vector units did not complete PP")
            expect_pixels(lambda x, y: bytes(4))
            result["checks"].append("parallel vector units read the original bank when one unit overwrites another unit's source")

            half = int.from_bytes(struct.pack("<4e", 0.5, 0.5, 0.5, 0.5), "little")
            multiply = 12 | 0xE4 << 4 | 13 << 14 | 0xE4 << 18 | 15 << 32
            add_pipeline = 0xE4 << 4 | 13 << 14 | 0xE4 << 18 | 15 << 32 | 1 << 43
            drawing(pp_instruction({3: multiply, 5: add_pipeline,
                                    10: seed, 11: half}))
            start(draw_packets())
            write_bytes(TARGET, b"\xA5" * (160 * 16 + 64))
            start_pp()
            require(mali.read(PPS[0], 0x1020) == 1,
                    "Vector multiply/accumulate forwarding did not complete PP")
            expect_pixels(lambda x, y: bytes((223, 191, 159, 255)))
            result["checks"].append("unscaled vector multiply forwards its complete result to the simultaneous accumulator")

            red = int.from_bytes(struct.pack("<4e", 1, 0, 0, 1), "little")
            wrong = pp_instruction({5: move, 10: red}, stop=False)
            branch_words = len(pp_instruction({5: move, 9: 0, 10: seed},
                                              stop=False)) // 4
            # Compare old r0.x with untouched r1.x, skipping the red stop.
            branch = 4 << 4 | 1 << 17 | (branch_words + len(wrong) // 4) << 41
            drawing(pp_instruction({5: move, 9: branch, 10: seed},
                                   stop=False) + wrong + pp_instruction({}))
            start(draw_packets())
            write_bytes(TARGET, b"\xA5" * (160 * 16 + 64))
            start_pp()
            require(mali.read(PPS[0], 0x1020) == 1,
                    "Branch with a simultaneous ALU write did not complete PP")
            expect_pixels(lambda x, y: bytes((191, 128, 64, 255)))
            result["checks"].append("branch predicates see the old register bank before a simultaneous ALU destination is committed")

            drawing(texture_fragment(), textured=True)
            write_bytes(VARYINGS, struct.pack("<16f", 0, 0, 0, 1, 1, 0, 0, 1,
                                             1, 1, 0, 1, 0, 1, 0, 1))
            write_bytes(TEX_DESCRIPTOR, texture_descriptor())
            write_bytes(TEX_ARRAY, struct.pack("<I", TEX_DESCRIPTOR))
            texels = (bytes((0, 0, 255, 255)), bytes((0, 255, 0, 255)),
                      bytes((255, 0, 0, 255)), bytes((255, 255, 255, 255)))
            write_bytes(TEX_IMAGE, b"".join(texels))
            start(draw_packets())
            write_bytes(TARGET, b"\xA5" * (160 * 16 + 64))
            start_pp()
            require(mali.read(PPS[0], 0x1020) == 1, "Textured triangle draw did not complete PP")
            expect_pixels(lambda x, y: texels[(y >= 8) * 2 + (x >= 16)])
            result["checks"].append("fragment texture loads sample all four nearest-filtered texels from interpolated coordinates")

            drawing(pp_instruction({10: 0}, stop=False) +
                    texture_fragment(varying_offset=12), textured=True)
            start(draw_packets())
            write_bytes(TARGET, b"\xA5" * (160 * 16 + 64))
            start_pp()
            require(mali.read(PPS[0], 0x1020) == 1,
                    "Register-relative varying load did not complete PP")
            expect_pixels(lambda x, y: texels[(y >= 8) * 2 + (x >= 16)])
            result["checks"].append("register-relative varying loads retain all runtime inputs and decoded half constants across instructions")

            varying = 3 << 5 | 15 << 10 | 15 << 24 | 15 << 28
            missing_sample = 0x39001 << 42 | 31 << 30
            drawing(pp_instruction({0: varying, 1: missing_sample,
                                    5: move, 10: seed}), textured=True)
            start(draw_packets())
            write_bytes(TARGET, b"\xA5" * (160 * 16 + 64))
            start_pp()
            require(mali.read(PPS[0], 0x1020) == 1 << 9 and
                    read_bytes(TARGET, 160 * 16 + 64) == b"\xA5" * (160 * 16 + 64),
                    "Discarding an unused texture value suppressed its sampling failure")
            result["checks"].append("an overwritten texture result still executes its sampler and preserves failed-job RAM")
            drawing(texture_fragment(), textured=True)
            start(draw_packets())

            write_bytes(VARYINGS, struct.pack("<16f", *((0.5, 0.5, 0, 1) * 4)))
            write_bytes(TEX_DESCRIPTOR, texture_descriptor(nearest=False))
            write_bytes(TARGET, b"\xA5" * (160 * 16 + 64))
            start_pp()
            expect_pixels(lambda x, y: bytes((128, 128, 128, 255)))
            result["checks"].append("bilinear filtering combines four texels with exact center weights")

            write_bytes(TARGET, b"\xA5" * 2048)
            start_pp(layout=2, pitch=2)
            actual = read_bytes(TARGET, 2048)
            require(actual == bytes((128, 128, 128, 255)) * 512,
                    "Tiled writeback did not fill both Utgard tiles")
            result["checks"].append("Utgard tiled writeback produces actual color data for every tile")

            bad_texture = set_bits(int.from_bytes(texture_descriptor(), "little"), 0, 6, 0x3F)
            write_bytes(TEX_DESCRIPTOR, bad_texture.to_bytes(64, "little"))
            write_bytes(TARGET, b"\xA5" * (160 * 16 + 64))
            start_pp()
            require(mali.read(PPS[0], 0x1020) == 1 << 9 and
                    read_bytes(TARGET, 160 * 16 + 64) == b"\xA5" * (160 * 16 + 64),
                    "Unsupported texture format falsely completed or changed the destination")
            result["checks"].append("unsupported texture formats fail before RAM writeback")

            background()
            write_bytes(TEX_DESCRIPTOR, texture_descriptor())
            write_bytes(RENDER_PD, b"\0" * 4096)
            write_bytes(RENDER_PT, b"".join(struct.pack("<I", 0x00400000 + i * 4096 | 7)
                                            for i in range(1024)))
            mali.memory_word(RENDER_PD + 4, RENDER_PT | 1)
            for index in (0, 1):
                mali.write(MMUS[index], 8, 6)
                mali.write(MMUS[index], 0, RENDER_PD)
                mali.write(MMUS[index], 0x1C, 3)
                mali.write(MMUS[index], 8, 0)
            start(setup_packets())
            mali.memory_word(RENDER_PT + ((TARGET >> 12) & 1023) * 4, TARGET | 3)
            write_bytes(TARGET, b"\xA5" * (160 * 16 + 64))
            start_pp()
            require(mali.read(MMUS[1], 0x14) == 1 and
                    mali.read(MMUS[1], 0x0C) == TARGET and mali.read(MMUS[1], 4) & 32 and
                    mali.read(PPS[0], 0x1020) == 0 and mali.read(PPS[0], 0x1008) & 1,
                    "Protected writeback did not produce a pending PP write fault")
            require(read_bytes(TARGET, 160 * 16 + 64) == b"\xA5" * (160 * 16 + 64),
                    "Writeback modified a protected page")
            mali.memory_word(RENDER_PT + ((TARGET >> 12) & 1023) * 4, TARGET | 7)
            mali.write(MMUS[1], 0x18, 1)
            mali.write(MMUS[1], 8, 5)
            require(mali.read(PPS[0], 0x1020) == 1 and not mali.read(PPS[0], 0x1008) & 1,
                    "PP did not resume after write-fault repair")
            expect_pixels(lambda x, y: bytes((0xC0, 0x80, 0x40, 0xFF)))
            result["checks"].append("writeback enforces PP page permissions, preserves protected RAM, and resumes after fault repair")

            qmp.execute("system_reset")
            require(count("gp-setups") == count("gp-commands") == count("pp-tiles") == 0,
                    "System reset left functional GPU counters")
            result["checks"].append("system reset clears functional GPU state and counters")
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
    parser.add_argument("--output", type=Path, default=PROJECT / "work/qemu-smoke/mali-render")
    args = parser.parse_args()
    return run(args.qemu, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
