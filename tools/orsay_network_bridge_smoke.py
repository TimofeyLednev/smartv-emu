#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Check packet preservation, redirector framing and actual legacy TLS sockets."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import ssl
import struct
import time
import warnings

from orsay_network_bridge import OrsayNetworkBridge, checksum, HOST_IP


def tcp_frame(source, dest, sport, dport, flags=0x18):
    ip = bytearray.fromhex("450000000001400040060000") + socket.inet_aton(source) + socket.inet_aton(dest)
    tcp = bytearray(struct.pack("!HHIIBBHHH", sport, dport, 123, 456, 0x50, flags, 32768, 0, 0))
    tcp.extend(b"a real payload with odd length\0")
    struct.pack_into("!H", ip, 2, 20 + len(tcp))
    struct.pack_into("!H", ip, 10, checksum(bytes(ip)))
    struct.pack_into("!H", tcp, 16, checksum(bytes(ip[12:20]) + struct.pack("!BBH", 0, 6, len(tcp)) + tcp))
    return bytes.fromhex("52540012345652550a0002020800") + bytes(ip) + bytes(tcp)


def valid_checksums(frame):
    assert checksum(frame[14:34]) == 0
    assert checksum(frame[26:34] + struct.pack("!BBH", 0, 6, len(frame) - 34) + frame[34:]) == 0


def receive(sock, count):
    result = bytearray()
    while len(result) < count:
        part = sock.recv(count - len(result))
        if not part:
            raise ValueError("Socket closed before the entire frame arrived")
        result.extend(part)
    return bytes(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("work/network-bridge/smoke-v1"))
    args = parser.parse_args()
    bridge = OrsayNetworkBridge(args.output, ["18.185.119.2"], guest_socket_timeout=900)
    checks, sockets = [], []
    try:
        for flags in (2, 0x18, 0x11):
            original = tcp_frame("10.0.2.15", "18.185.119.2", 42424, 443, flags)
            out = bridge.rewrite(original)
            assert len(out) == len(original)
            assert out[30:34] == HOST_IP and int.from_bytes(out[36:38], "big") == bridge.https_port
            changed = {i for i, (a, b) in enumerate(zip(original, out)) if a != b}
            assert changed <= {24, 25, 30, 31, 32, 33, 36, 37, 50, 51}
            valid_checksums(out)
            reply = tcp_frame("10.0.2.2", "10.0.2.15", bridge.https_port, 42424, flags)
            restored = bridge.rewrite(reply)
            assert restored[26:30] == socket.inet_aton("18.185.119.2")
            assert int.from_bytes(restored[34:36], "big") == 443
            assert restored[38:50] == reply[38:50] and restored[52:] == reply[52:]
            valid_checksums(restored)
            checks.append("TCP flags 0x%x: translation preserves sequence, flags and payload with valid checksums" % flags)
        other = tcp_frame("10.0.2.15", "1.1.1.1", 42425, 443)
        assert bridge.rewrite(other) == other
        checks.append("unselected traffic preserves every byte")
        labels = b"".join(bytes([len(label)]) + label.encode("ascii") for label in "infolink.pavv.co.kr".split(".")) + b"\0"
        dns = struct.pack("!6H", 123, 0x8180, 1, 1, 0, 0) + labels + struct.pack("!HH", 1, 1)
        dns += b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + socket.inet_aton("3.66.253.184")
        udp = struct.pack("!4H", 53, 40001, 8 + len(dns), 0) + dns
        ip = bytearray.fromhex("450000000001400040110000") + socket.inet_aton("10.0.2.3") + socket.inet_aton("10.0.2.15")
        struct.pack_into("!H", ip, 2, 20 + len(udp))
        struct.pack_into("!H", ip, 10, checksum(bytes(ip)))
        packet = other[:14] + bytes(ip) + udp
        assert bridge.rewrite(packet) == packet
        assert socket.inet_aton("3.66.253.184") in bridge.addresses
        checks.append("a compressed DNS response learns Hub addresses without changing its bytes")
        bridge.start()
        for direction in ("tx", "rx"):
            out = socket.create_connection(bridge.ports[direction + "-out"].getsockname(), timeout=3)
            into = socket.create_connection(bridge.ports[direction + "-in"].getsockname(), timeout=3)
            sockets.extend((out, into))
            framed = struct.pack("!I", len(other)) + other
            for size in (1, 2, len(framed) - 3):
                out.sendall(framed[:size])
                framed = framed[size:]
            assert receive(into, len(other) + 4) == struct.pack("!I", len(other)) + other
        checks.append("both real redirector socket directions preserve split headers and complete packets")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1
        ctx.set_ciphers("AES256-SHA:AES128-SHA:@SECLEVEL=0")
        with ctx.wrap_socket(socket.create_connection(("127.0.0.1", bridge.https_port), timeout=3),
                             server_hostname="infolink.pavv.co.kr") as conn:
            assert conn.version() == "TLSv1"
            conn.sendall(b"GET / HTTP/1.1\r\nHost: invalid.example\r\nConnection: close\r\n\r\n")
            assert b"400" in conn.recv(65536).split(b"\r\n")[0]
        checks.append("a full TLS 1.0 AES handshake carries HTTP and rejects an unselected host")
        waiting = socket.create_connection(("127.0.0.1", bridge.https_port), timeout=3)
        sockets.append(waiting)
        with ctx.wrap_socket(socket.create_connection(("127.0.0.1", bridge.https_port), timeout=3),
                             server_hostname="infolink.pavv.co.kr") as conn:
            conn.sendall(b"GET / HTTP/1.1\r\nHost: invalid.example\r\nConnection: close\r\n\r\n")
            assert b"400" in conn.recv(65536).split(b"\r\n")[0]
        checks.append("a client waiting to send ClientHello does not block another real TLS handshake")
        # Reproduce the captured guest's delay beyond the former 20-second
        # host timeout, then complete a real handshake on that same socket.
        time.sleep(21)
        with ctx.wrap_socket(waiting, server_hostname="infolink.pavv.co.kr") as conn:
            assert conn.version() == "TLSv1"
            conn.sendall(b"GET / HTTP/1.1\r\nHost: invalid.example\r\nConnection: close\r\n\r\n")
            assert b"400" in conn.recv(65536).split(b"\r\n")[0]
        checks.append("a real TLS handshake succeeds after ClientHello is delayed more than 20 host seconds")
        assert bridge.report()["guest_socket_timeout_seconds"] == 900
        for timeout in (0, -1, 3601, float("inf"), float("nan")):
            try:
                OrsayNetworkBridge(args.output / "invalid-timeout", guest_socket_timeout=timeout)
            except ValueError:
                pass
            else:
                raise AssertionError("Invalid guest socket timeout was accepted")
        checks.append("an extended guest timeout is reported and invalid budgets are rejected before allocating sockets")
        short = OrsayNetworkBridge(args.output / "short-timeout", guest_socket_timeout=1)
        try:
            short.start()
            with socket.create_connection(("127.0.0.1", short.https_port), timeout=3) as conn:
                assert conn.recv(1) == b""
            deadline = time.monotonic() + 1
            events = []
            while time.monotonic() < deadline:
                path = short.output / "events.jsonl"
                if path.exists():
                    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                if any(row["event"] == "tls_failed" for row in events):
                    break
                time.sleep(0.01)
            assert any(row["event"] == "tls_started" and row["timeout_seconds"] == 1 for row in events)
            assert any(row["event"] == "tls_failed" and "timed out" in row["error"] for row in events)
        finally:
            short.close()
        checks.append("a configured one-second timeout closes an idle real TLS socket and records its start and failure")
    finally:
        for conn in sockets:
            conn.close()
        bridge.close()
    result = {"status": "passed", "checks": checks, "bridge": bridge.report()}
    (bridge.output / "verification.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
