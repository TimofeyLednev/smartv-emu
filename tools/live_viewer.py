#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Serve actual QMP screen captures to a browser on the local host."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import time
from urllib.parse import urlsplit
import webbrowser

from qemu_qmp import capture

PROJECT = Path(__file__).resolve().parents[1]


def serve(process, qmp, output, port=0, open_browser=True,
          scope="Firmware logo diagnostic; no Orsay UI", remote_control=False):
    state = {"running": True}
    page = (PROJECT / "ui/display.html").read_bytes()
    if remote_control:
        page = page.replace(b"Framebuffer diagnostic", b"Orsay TV")
        page = page.replace(b'<nav id="remote" hidden>', b'<nav id="remote">')
    output = Path(output).resolve()
    remote_events = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def respond(self, code, content_type, body):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/":
                self.respond(200, "text/html; charset=utf-8", page)
            elif path == "/screen.png":
                try:
                    screenshot = output / "live-screen.png"
                    capture(qmp, screenshot)
                    self.respond(200, "image/png", screenshot.read_bytes())
                except (OSError, ValueError, ConnectionError):
                    self.respond(503, "text/plain", b"TV stopped")
            else:
                self.respond(404, "text/plain", b"Not found")

        def do_POST(self):
            path = urlsplit(self.path).path
            if path == "/remote" and remote_control:
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 256:
                        raise ValueError("Invalid remote request length")
                    key = json.loads(self.rfile.read(size))["key"]
                    if type(key) is not int or not 0 <= key <= 255:
                        raise ValueError("Remote key must be a byte")
                    qmp.execute("qom-set", {"path": "/machine/micom",
                                "property": "remote-key", "value": key})
                    remote_events.append({"time": time.time(), "key": hex(key)})
                    (output / "live-remote-events.json").write_text(
                        json.dumps(remote_events, indent=2) + "\n", encoding="utf-8")
                    self.respond(200, "application/json", b'{"submitted":true}')
                except (OSError, ValueError, ConnectionError, KeyError, TypeError) as error:
                    self.respond(400, "application/json",
                                 json.dumps({"error": str(error)}).encode("utf-8"))
            elif path == "/stop":
                state["running"] = False
                self.respond(200, "application/json", b'{"stopped":true}')
            else:
                self.respond(404, "text/plain", b"Not found")

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 0.25
    url = "http://127.0.0.1:%d/" % server.server_address[1]
    report = {"url": url, "qemu_pid": process.pid, "status": "running",
              "screen_source": "QMP capture of the running guest framebuffer",
              "scope": scope, "remote_control": remote_control}
    report_file = output / "live-viewer.json"
    report_file.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("Live TV display: " + url + " (Stop TV in the browser or Ctrl+C to exit)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        while state["running"] and process.poll() is None:
            server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        report["status"] = "stopped"
        report_file.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

