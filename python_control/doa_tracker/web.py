"""Serve the 3D dashboard page and stream snapshots to it as server-sent events.

With the camera enabled it also serves the camera as MJPEG (/camera.mjpg, each
part tagged X-Frame-Seq) and accepts detector results (POST /api/detections).
With the measurement microphone enabled, /spectrum/events streams spectrogram
columns (recent history first, then about 10 per second).
"""

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
BOUNDARY = "frame"
MAX_POST_BYTES = 65536


def make_handler(state, stop: threading.Event, page: bytes, hub, spectrum=None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

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
            elif path in ("/camera.mjpg", "/camera.jpg") and hub is None:
                self.send_error(404, "Camera is disabled ([camera] enabled = false in doa_tracker.toml, or --no-camera)")
            elif path == "/camera.mjpg":
                self._mjpeg()
            elif path == "/camera.jpg":
                self._snapshot()
            elif path == "/spectrum/events" and spectrum is None:
                self.send_error(404, "Measurement microphone is disabled ([acoustic] enabled = false, or --no-acoustic)")
            elif path == "/spectrum/events":
                self._spectrum()
            elif path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_error(404)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path != "/api/detections" or hub is None:
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_POST_BYTES:
                self.send_error(413 if length else 411)
                return
            try:
                payload = json.loads(self.rfile.read(length))
                reply = hub.post_detections(payload, self.client_address[0])
            except (ValueError, KeyError, TypeError) as exc:
                body = json.dumps({"ok": False, "error": str(exc)}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._send(json.dumps(reply).encode("utf-8"), "application/json")

        def _snapshot(self) -> None:
            frame = hub.wait_frame(-1, 2.0)
            if frame is None:
                self.send_error(503, "No camera frames yet")
                return
            self._send(frame[1], "image/jpeg")

        def _mjpeg(self) -> None:
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=" + BOUNDARY)
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            seq = -1
            try:
                while not stop.is_set():
                    frame = hub.wait_frame(seq, 1.0)
                    if frame is None:
                        continue
                    seq, jpeg, meta = frame
                    head = ("--{}\r\nContent-Type: image/jpeg\r\nContent-Length: {}\r\n"
                            "X-Frame-Seq: {}\r\nX-Frame-Time: {:.3f}\r\n\r\n").format(
                                BOUNDARY, len(jpeg), seq, meta["t"])
                    self.wfile.write(head.encode("ascii"))
                    self.wfile.write(jpeg)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
            except OSError:
                return

        def _event_stream_headers(self) -> None:
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

        def _spectrum(self) -> None:
            self._event_stream_headers()
            seq, meta_id = 0, None
            last_write = time.monotonic()
            try:
                while not stop.is_set():
                    meta, columns = spectrum.wait(seq, meta_id, 1.0)
                    message = {}
                    if meta is not None and meta["id"] != meta_id:
                        message["meta"] = meta
                        meta_id = meta["id"]
                    if columns:
                        message["cols"] = columns
                        seq = columns[-1][0]
                    if message:
                        self.wfile.write("data: {}\n\n".format(json.dumps(message)).encode("utf-8"))
                    elif time.monotonic() - last_write > KEEPALIVE_S:
                        self.wfile.write(b": keepalive\n\n")
                    else:
                        continue
                    self.wfile.flush()
                    last_write = time.monotonic()
            except OSError:
                return

        def _stream(self) -> None:
            self._event_stream_headers()
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
    def __init__(self, dashboard: dict, state, stop: threading.Event, hub=None, spectrum=None):
        self.host = dashboard["host"]
        self.port = dashboard["port"]
        self.camera = hub is not None
        page = PAGE_PATH.read_bytes()
        self.server = ThreadingHTTPServer((self.host, self.port), make_handler(state, stop, page, hub, spectrum))
        self.thread = threading.Thread(target=self.server.serve_forever, name="dashboard", daemon=True)

    def start(self) -> None:
        self.thread.start()
        for url in self.urls():
            log.info("Dashboard at %s", url)
        if self.camera:
            log.info("Camera stream at %scamera.mjpg", self.urls()[-1])
        else:
            log.info("Camera disabled; /camera.mjpg will return 404")

    def urls(self) -> list[str]:
        if self.host not in ("0.0.0.0", "::", ""):
            return ["http://{}:{}/".format(self.host, self.port)]
        return ["http://{}:{}/".format(name, self.port) for name in ("localhost", socket.gethostname() + ".local")]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
