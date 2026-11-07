#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Opt-in TLS compatibility bridge for a private Orsay firmware probe.

QEMU's existing packet redirectors carry Ethernet frames to this host tool.
Only the selected Smart Hub HTTPS destinations are translated to a local TLS
server. The server accepts the firmware's TLS 1.0/AES handshake and forwards
the original HTTP request to the original service using verified modern TLS.
Other packets retain their bytes and direction. No guest code is changed.
"""
from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import shutil
import ssl
import struct
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import warnings

from network_packets import dns_message

PROJECT = Path(__file__).resolve().parents[1]
DOMAINS = ("infolink.pavv.co.kr", "devinfolink.pavv.co.kr")
HOST_IP = ipaddress.IPv4Address("10.0.2.2").packed
HUBSITE_CA_SHA256 = "c2441cd566311b0f770ffc2d2a02588dc4bf08f8fc898e4b0e416cc862053700"
GUEST_SOCKET_TIMEOUT = 180


def checksum(data):
    if len(data) & 1:
        data += b"\0"
    total = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    while total >> 16:
        total = (total & 65535) + (total >> 16)
    return (~total) & 65535


def listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    sock.settimeout(0.5)
    return sock


def certificate(output):
    cert, key = output / "server-cert.pem", output / "server-key.pem"
    if cert.is_file() and key.is_file():
        return cert, key
    config = output / "openssl.cnf"
    config.write_text("[req]\ndistinguished_name=dn\n[dn]\n", encoding="ascii")
    openssl = os.environ.get("SAMSUNGF_OPENSSL") or shutil.which("openssl")
    if not openssl or not Path(openssl).is_file():
        raise ValueError("Install OpenSSL 3 or pass --openssl to the launcher")
    command = [str(openssl), "req", "-config", str(config), "-x509", "-newkey", "rsa:2048",
               "-sha256", "-days", "30", "-noenc", "-subj", "/CN=infolink.pavv.co.kr",
               "-keyout", str(key), "-out", str(cert)]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   creationflags=flags)
    return cert, key


class OrsayNetworkBridge:
    def __init__(self, output, addresses=(), hubsite_ca=None, id_prefix="orsay-bridge",
                 guest_socket_timeout=GUEST_SOCKET_TIMEOUT):
        if re.fullmatch(r"[a-z][a-z0-9-]{0,39}", id_prefix) is None:
            raise ValueError("Bridge IDs require a lowercase identifier of at most 40 characters")
        if not 1 <= guest_socket_timeout <= 3600:
            raise ValueError("Guest socket timeout must be finite and between 1 and 3600 seconds")
        self.id_prefix = id_prefix
        self.guest_socket_timeout = guest_socket_timeout
        self.output = Path(output).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        (self.output / "responses").mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.addresses = {ipaddress.IPv4Address(a).packed for a in addresses}
        self.flows = {}
        self.connections = []
        self.threads = []
        self.filters = []
        self.chardevs = []
        self.frames = self.rewrites = self.request_count = 0
        self.status = "created"
        self.ports = {name: listener() for name in ("tx-in", "tx-out", "rx-in", "rx-out")}
        cert, key = certificate(self.output)
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            self.tls.minimum_version = ssl.TLSVersion.TLSv1
        self.tls.set_ciphers("AES256-SHA:AES128-SHA:@SECLEVEL=0")
        self.tls.load_cert_chain(cert, key)
        self.upstream_tls = ssl.create_default_context()
        self.upstream_tls.minimum_version = ssl.TLSVersion.TLSv1_2
        self.upstream_ca = None
        if hubsite_ca is not None:
            ca_path = Path(hubsite_ca).resolve()
            ca_pem = ca_path.read_text(encoding="ascii")
            ca_hash = hashlib.sha256(ssl.PEM_cert_to_DER_cert(ca_pem)).hexdigest()
            if ca_hash != HUBSITE_CA_SHA256:
                raise ValueError("Expected the original firmware's Samsung Hubsite CA certificate")
            self.upstream_tls.load_verify_locations(cadata=ca_pem)
            # The 2009 Samsung CA/chain uses SHA1 and noncritical basic constraints.
            # Keep chain, dates and hostname verification, with TLS >= 1.2.
            self.upstream_tls.verify_flags &= ~ssl.VERIFY_X509_STRICT
            self.upstream_tls.set_ciphers("DEFAULT:@SECLEVEL=0")
            self.upstream_ca = {"path": str(ca_path), "der_sha256": ca_hash,
                                "legacy_certificate_profile": True,
                                "source": "Exact Samsung CA recovered from InfoLink/lib/ca_hubsite.pem"}
        bridge = self

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def process_request_thread(self, conn, address):
                # TCG can take more than 20 host seconds between the SYN and
                # ClientHello. A waiting guest must not block other clients.
                conn.settimeout(bridge.guest_socket_timeout)
                started = time.monotonic()
                bridge.event("tls_started", peer_port=address[1],
                             timeout_seconds=bridge.guest_socket_timeout)
                try:
                    conn = bridge.tls.wrap_socket(conn, server_side=True)
                    bridge.event("tls_connected", version=conn.version(), cipher=conn.cipher()[0],
                                 handshake_seconds=round(time.monotonic() - started, 3))
                except OSError as error:
                    conn.close()
                    bridge.event("tls_failed", error=str(error),
                                 handshake_seconds=round(time.monotonic() - started, 3))
                    return
                super().process_request_thread(conn, address)

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_GET(self):
                host = (self.headers.get("Host") or "").split(":", 1)[0].lower()
                if host not in DOMAINS or not self.path.startswith("/") or self.path.startswith("//"):
                    self.send_error(400, "Unexpected bridge destination")
                    return
                headers = {name: value for name, value in self.headers.items()
                           if name.lower() not in ("host", "connection", "accept-encoding", "transfer-encoding")}
                headers["Accept-Encoding"] = "identity"
                url = "https://" + host + self.path
                try:
                    try:
                        response = urlopen(Request(url, headers=headers), timeout=20, context=bridge.upstream_tls)
                    except HTTPError as error:
                        response = error
                    with response:
                        body = response.read(16 * 1024 * 1024 + 1)
                        if len(body) > 16 * 1024 * 1024:
                            raise ValueError("Service response exceeds 16 MiB")
                        code, response_headers = response.code, dict(response.headers)
                    with bridge.lock:
                        bridge.request_count += 1
                        number = bridge.request_count
                    path = bridge.output / "responses" / ("%04d.bin" % number)
                    path.write_bytes(body)
                    bridge.event("http_response", host=host, path=self.path, status=code, bytes=len(body),
                                 body_file=str(path.relative_to(bridge.output)), sha256=hashlib.sha256(body).hexdigest())
                    self.send_response(code)
                    for name, value in response_headers.items():
                        if name.lower() not in ("content-length", "connection", "transfer-encoding", "server", "date"):
                            self.send_header(name, value)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                    self.close_connection = True
                except (OSError, URLError, ValueError) as error:
                    bridge.event("upstream_failed", host=host, path=self.path, error=str(error))
                    self.send_error(502, "Upstream service failed")

        self.server = Server(("127.0.0.1", 0), Handler)
        self.https_port = self.server.server_address[1]

    def event(self, kind, **values):
        with self.lock:
            with (self.output / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(dict(event=kind, time=time.time(), **values)) + "\n")

    def report(self):
        with self.lock:
            return {"status": self.status, "domains": DOMAINS, "https_port": self.https_port,
                    "id_prefix": self.id_prefix,
                    "addresses": sorted(str(ipaddress.IPv4Address(a)) for a in self.addresses),
                    "frames": self.frames, "rewritten_frames": self.rewrites, "requests": self.request_count,
                    "guest_patched": False, "upstream_certificate_verification": True,
                    "upstream_ca": self.upstream_ca,
                    "upstream_minimum_tls": "TLS 1.2",
                    "local_legacy_tls": "TLS 1.0 with RSA AES-CBC/SHA1 compatibility",
                    "guest_socket_timeout_seconds": self.guest_socket_timeout,
                    "limits": ["No replacement Smart Hub responses are synthesized", "Only IPv4 unfragmented HTTPS is translated",
                               "The original firmware disables certificate checks for its Hub requests",
                               "Factory identity and physical network timing remain unverified"]}

    def save_report(self):
        (self.output / "result.json").write_text(json.dumps(self.report(), indent=2) + "\n", encoding="utf-8")

    def rewrite(self, frame):
        with self.lock:
            self.frames += 1
            if len(frame) < 34 or frame[12:14] != b"\x08\x00":
                return frame
            ip = frame[14:]
            header, size = (ip[0] & 15) * 4, int.from_bytes(ip[2:4], "big")
            if ip[0] >> 4 != 4 or header < 20 or not header <= size <= len(ip):
                return frame
            if int.from_bytes(ip[6:8], "big") & 0x3fff:
                return frame
            source, dest, transport = ip[12:16], ip[16:20], ip[header:size]
            if ip[9] == 17 and len(transport) >= 8 and transport[:2] == b"\x00\x35":
                try:
                    msg = dns_message(transport[8:])
                    if any(q["name"].lower() in DOMAINS for q in msg["questions"]):
                        values = {ipaddress.IPv4Address(r["value"]).packed for r in msg["records"] if r["type"] == 1}
                        added = values - self.addresses
                        self.addresses.update(values)
                        if added:
                            self.event("dns_mapped", addresses=sorted(str(ipaddress.IPv4Address(a)) for a in added))
                except (ValueError, struct.error):
                    pass
                return frame
            if ip[9] != 6 or len(transport) < 20:
                return frame
            sport, dport = struct.unpack_from("!HH", transport)
            new_source, new_dest, new_sport, new_dport = source, dest, sport, dport
            if dest in self.addresses and dport == 443:
                self.flows[(source, sport)] = (dest, dport)
                new_dest, new_dport = HOST_IP, self.https_port
            elif source == HOST_IP and sport == self.https_port and (dest, dport) in self.flows:
                new_source, new_sport = self.flows[(dest, dport)]
            else:
                return frame
            packet = bytearray(ip[:size])
            packet[12:16], packet[16:20] = new_source, new_dest
            struct.pack_into("!HH", packet, header, new_sport, new_dport)
            packet[10:12] = packet[header + 16:header + 18] = b"\0\0"
            struct.pack_into("!H", packet, 10, checksum(bytes(packet[:header])))
            pseudo = new_source + new_dest + struct.pack("!BBH", 0, 6, size - header)
            struct.pack_into("!H", packet, header + 16, checksum(pseudo + bytes(packet[header:])))
            self.rewrites += 1
            return frame[:14] + bytes(packet) + frame[14 + size:]

    def accept(self, listener_):
        while not self.stopped.is_set():
            try:
                conn, _ = listener_.accept()
                conn.settimeout(0.5)
                self.connections.append(conn)
                return conn
            except socket.timeout:
                continue
            except OSError:
                if self.stopped.is_set():
                    return None
                raise
        return None

    def pump(self, direction):
        out = self.accept(self.ports[direction + "-out"])
        into = self.accept(self.ports[direction + "-in"])
        if out is None or into is None:
            return
        buffer = bytearray()
        try:
            while not self.stopped.is_set():
                try:
                    data = out.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    break
                buffer.extend(data)
                while len(buffer) >= 4:
                    length = int.from_bytes(buffer[:4], "big")
                    if not 0 < length <= 65536:
                        raise ValueError("Invalid QEMU redirector frame length")
                    if len(buffer) < 4 + length:
                        break
                    frame = self.rewrite(bytes(buffer[4:4 + length]))
                    del buffer[:4 + length]
                    into.sendall(struct.pack("!I", len(frame)) + frame)
        except (OSError, ValueError) as error:
            if not self.stopped.is_set():
                self.event("filter_failed", direction=direction, error=str(error))

    def start(self):
        self.status = "running"
        for function, args in ((self.server.serve_forever, ()), (self.pump, ("tx",)),
                               (self.pump, ("rx",)), (self.report_loop, ())):
            thread = threading.Thread(target=function, args=args, daemon=True)
            thread.start()
            self.threads.append(thread)
        self.save_report()

    def report_loop(self):
        while not self.stopped.wait(0.5):
            self.save_report()

    def qemu_args(self):
        result = []
        for name, sock in self.ports.items():
            result += ["-chardev", "socket,id=%s-%s,host=127.0.0.1,port=%d" %
                       (self.id_prefix, name, sock.getsockname()[1])]
        for direction in ("tx", "rx"):
            result += ["-object", "filter-redirector,id=%s-%s,netdev=emac0,queue=%s,indev=%s-%s-in,outdev=%s-%s-out" %
                       (self.id_prefix, direction, direction, self.id_prefix, direction,
                        self.id_prefix, direction)]
        return result



    def close(self):
        self.stopped.set()
        if self.threads:
            self.server.shutdown()
        self.server.server_close()
        for sock in self.connections + list(self.ports.values()):
            sock.close()
        for thread in self.threads:
            thread.join(2)
        self.status = "stopped"
        self.save_report()
