"""Capture the Raspberry Pi Camera v2 (IMX219) as JPEG frames for /camera.mjpg.

The Pi 5 has no hardware video encoder, so frames are JPEG-encoded in software
by Picamera2's JpegEncoder. The sensor runs in its 1640x1232 binned mode so the
picture keeps the full 62.2 x 48.8 degree field of view that the elevation and
azimuth maths assume.
"""

from __future__ import annotations

import collections
import io
import logging
import math
import os
import threading
import time

log = logging.getLogger(__name__)

REOPEN_S = 10.0
FULL_FOV_SENSOR_MODE = (1640, 1232)


class FrameSink(io.BufferedIOBase):
    """Picamera2 FileOutput target: every write() is one complete JPEG."""

    def __init__(self, hub):
        super().__init__()
        self.hub = hub
        self.times = collections.deque(maxlen=60)

    def writable(self) -> bool:
        return True

    def write(self, buf) -> int:
        self.hub.publish_frame(bytes(buf))
        self.times.append(time.monotonic())
        return len(buf)

    def fps(self) -> float:
        recent = [t for t in self.times if time.monotonic() - t <= 2.0]
        return round(len(recent) / 2.0, 1)


def open_camera(camera: dict, sink: FrameSink):
    """Start Picamera2 recording JPEGs into `sink`; returns an object with stop()."""
    os.environ.setdefault("LIBCAMERA_LOG_LEVELS", "*:WARN")
    from libcamera import Transform
    from picamera2 import Picamera2
    from picamera2.encoders import JpegEncoder
    from picamera2.outputs import FileOutput

    if not Picamera2.global_camera_info():
        raise RuntimeError("No camera detected; check the ribbon cable (Pi 5 needs a 22-to-15 pin cable)")
    picam2 = Picamera2()
    try:
        frame_us = int(1_000_000 / camera["fps"])
        config = picam2.create_video_configuration(
            main={"size": (camera["width"], camera["height"])},
            raw={"size": FULL_FOV_SENSOR_MODE},
            transform=Transform(hflip=camera["hflip"], vflip=camera["vflip"]),
            controls={"FrameDurationLimits": (frame_us, frame_us)},
            buffer_count=4,
        )
        picam2.configure(config)
        picam2.start_recording(JpegEncoder(q=camera["jpeg_quality"]), FileOutput(sink))
    except Exception:
        picam2.close()
        raise

    class Running:
        model = picam2.camera_properties.get("Model", "camera")

        @staticmethod
        def stop():
            try:
                picam2.stop_recording()
            finally:
                picam2.close()

    return Running()


class CameraModule:
    def __init__(self, camera: dict, state, hub):
        self.cfg = camera
        self.state = state
        self.hub = hub
        self.sink = FrameSink(hub)
        self.running = None
        self.last_attempt = -math.inf
        self.error = None

    def run(self, stop: threading.Event) -> None:
        try:
            while not stop.is_set():
                now = time.monotonic()
                if self.running is None and now - self.last_attempt >= REOPEN_S:
                    self._open(now)
                self._report()
                stop.wait(0.5)
        finally:
            self._close()

    def _open(self, now: float) -> None:
        self.last_attempt = now
        try:
            self.running = open_camera(self.cfg, self.sink)
        except Exception as exc:
            if str(exc) != self.error:
                log.warning("Camera unavailable: %s", exc)
            self.error = str(exc)
            self.running = None
            return
        self.error = None
        log.info("Camera %s streaming %dx%d at %d fps", self.running.model,
                 self.cfg["width"], self.cfg["height"], self.cfg["fps"])

    def _close(self) -> None:
        if self.running is not None:
            try:
                self.running.stop()
            except Exception:
                pass
            self.running = None

    def _report(self) -> None:
        self.state.set_module("camera", {
            "enabled": True,
            "ready": self.running is not None,
            "model": None if self.running is None else self.running.model,
            "width": self.cfg["width"],
            "height": self.cfg["height"],
            "fps": self.sink.fps(),
            "error": self.error,
        })
        self.state.set_module("vision", self.hub.status())
