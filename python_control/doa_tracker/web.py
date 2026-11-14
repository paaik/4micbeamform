"""Serve the 3D dashboard page and stream snapshots to it as server-sent events."""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)

PAGE_PATH = Path(__file__).resolve().parent / "dashboard.html"
KEEPALIVE_S = 15.0


def make_handler(state, stop: threading.Event, page: bytes):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _send(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(page, "text/html; charset=utf-8")
            elif path == "/api/latest":
                self._send(json.dumps(state.latest()).encode("utf-8"), "application/json")
            elif path == "/events":
                self._stream()
            elif path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            else:
                self.send_error(404)

        def _stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            seq = None
            last_write = time.monotonic()
            try:
                while not stop.is_set():
                    snapshot = state.wait_newer(-1 if seq is None else seq, 1.0)
                    if snapshot["seq"] != seq:
                        seq = snapshot["seq"]
                        payload = json.dumps(snapshot, allow_nan=False)
                        self.wfile.write("data: {}\n\n".format(payload).encode("utf-8"))
                    elif time.monotonic() - last_write > KEEPALIVE_S:
                        self.wfile.write(b": keepalive\n\n")
                    else:
                        continue
                    self.wfile.flush()
                    last_write = time.monotonic()
            except OSError:
                return

    return Handler


class DashboardServer:
    def __init__(self, dashboard: dict, state, stop: threading.Event):
        self.host = dashboard["host"]
        self.port = dashboard["port"]
        page = PAGE_PATH.read_bytes()
        self.server = ThreadingHTTPServer((self.host, self.port), make_handler(state, stop, page))
        self.thread = threading.Thread(target=self.server.serve_forever, name="dashboard", daemon=True)

    def start(self) -> None:
        self.thread.start()
        for url in self.urls():
            log.info("Dashboard at %s", url)

    def urls(self) -> list[str]:
        if self.host not in ("0.0.0.0", "::", ""):
            return ["http://{}:{}/".format(self.host, self.port)]
        return ["http://{}:{}/".format(name, self.port) for name in ("localhost", socket.gethostname() + ".local")]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
