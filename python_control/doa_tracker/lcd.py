"""Show the tracked azimuth on the Waveshare 1.69" ST7789 LCD.

Panel wiring, SPI speed, rotation and offsets stay in display_config/config.py.
"""

from __future__ import annotations

import logging
import math
import os
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

DISPLAY_DIR = Path(__file__).resolve().parent.parent / "display_config"
REOPEN_S = 10.0

BG = "#0d1016"
GRID = "#2c3542"
MUTED = "#8e99aa"
TEXT = "#e8edf4"
AMBER = "#ffb020"
SPEECH = "#ff6a2a"
OK = "#3dd68c"
BAD = "#ff5d6c"
STALE = "#5b6575"

BOLD_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)
REGULAR_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)


def load_font(candidates, size):
    from PIL import ImageFont

    for path in candidates:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def load_display_driver():
    if str(DISPLAY_DIR) not in sys.path:
        sys.path.insert(0, str(DISPLAY_DIR))
    import display_driver

    return display_driver


def centered_text(draw, center_x, top, text, font, fill):
    left, _, right, _ = draw.textbbox((0, 0), text, font=font)
    draw.text((center_x - (right - left) / 2 - left, top), text, font=font, fill=fill)


def frame_key(snapshot: dict):
    tracked = snapshot.get("tracked_deg")
    servo = snapshot.get("modules", {}).get("servo") or {}
    pointing = servo.get("pointing_deg")
    return (
        bool(snapshot.get("ok")),
        None if tracked is None else round(tracked) % 360,
        bool(snapshot.get("speech")),
        bool(snapshot.get("tracking_live")),
        None if pointing is None else round(pointing) % 360,
        servo.get("end"),
        servo.get("source"),
        elevation_text(snapshot),
        drone_text(snapshot),
        (snapshot.get("error") or "")[:30],
    )


def drone_text(snapshot: dict):
    """"DRONE 375Hz" while the measurement mic hears a blade-pass signature, else None."""
    acoustic = snapshot.get("modules", {}).get("acoustic") or {}
    if not acoustic.get("detected") or acoustic.get("bpf_hz") is None:
        return None
    return "DRONE {:.0f}Hz".format(acoustic["bpf_hz"])


def elevation_text(snapshot: dict):
    """("EL 45° LOCK", tracking?) for the tilt servo, or None when it isn't running."""
    tilt = snapshot.get("modules", {}).get("tilt") or {}
    if not tilt.get("backend"):
        return None
    tracking = tilt.get("mode") == "track"
    return "EL {:.0f}° {}".format(tilt.get("elevation_deg", 0), "LOCK" if tracking else "SCAN"), tracking


def render(snapshot: dict, width: int, height: int, fonts: dict):
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)
    ok = bool(snapshot.get("ok"))
    tracked = snapshot.get("tracked_deg")
    speech = bool(snapshot.get("speech"))
    live = bool(snapshot.get("tracking_live"))
    servo = snapshot.get("modules", {}).get("servo") or {}

    drone = drone_text(snapshot)
    draw.text((14, 10), drone or "AZIMUTH", font=fonts["small"], fill=BAD if drone else MUTED)
    status = BAD if not ok else (SPEECH if speech else OK)
    draw.ellipse((width - 26, 12, width - 12, 26), fill=status)
    elevation = elevation_text(snapshot)
    if elevation is not None:
        text, tracking = elevation
        left, _, right, _ = draw.textbbox((0, 0), text, font=fonts["small"])
        draw.text((width - 36 - (right - left), 10), text, font=fonts["small"], fill=OK if tracking else MUTED)

    cx, cy, radius = width // 2, 122, 78
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=GRID, width=2)
    for step in range(12):
        angle = math.radians(step * 30)
        inner = radius - (16 if step % 3 == 0 else 8)
        color = AMBER if step == 0 else GRID
        draw.line((cx + math.cos(angle) * inner, cy - math.sin(angle) * inner,
                   cx + math.cos(angle) * radius, cy - math.sin(angle) * radius), fill=color, width=2)

    pointing = servo.get("pointing_deg")
    if pointing is not None and servo.get("backend"):
        angle = math.radians(pointing)
        reach = radius - 26
        draw.line((cx, cy, cx + math.cos(angle) * reach, cy - math.sin(angle) * reach), fill=TEXT, width=2)

    if tracked is not None:
        angle = math.radians(tracked)
        reach = radius - 6
        tip = (cx + math.cos(angle) * reach, cy - math.sin(angle) * reach)
        color = (SPEECH if speech else AMBER) if (ok and live) else STALE
        draw.line((cx, cy, tip[0], tip[1]), fill=color, width=5)
        draw.ellipse((tip[0] - 7, tip[1] - 7, tip[0] + 7, tip[1] + 7), fill=color)
    draw.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), fill=TEXT)

    number = "---" if tracked is None else "{}°".format(round(tracked) % 360)
    centered_text(draw, cx, 206, number, fonts["big"], TEXT if ok else MUTED)

    if not ok:
        footer, color = (snapshot.get("error") or "No reSpeaker")[:30], BAD
    else:
        parts = ["speech" if speech else "quiet"]
        if servo.get("backend"):
            if servo.get("end") == "parked":
                parts.append("servo parked")
            else:
                parts.append("servo {:.0f}°{}".format(servo.get("angle_deg", 0), {
                    "rear": " rear", "front": " cam" if servo.get("source") == "camera" else ""}[servo.get("end", "front")]))
        footer, color = "  ·  ".join(parts), MUTED
    centered_text(draw, cx, height - 22, footer, fonts["small"], color)
    return image


class LcdModule:
    def __init__(self, lcd: dict, state):
        self.period = 1.0 / lcd["refresh_hz"]
        self.state = state
        self.display = None
        self.show = None
        self.size = (240, 280)
        self.fonts = None
        self.last_key = None
        self.last_attempt = -math.inf
        self.error = None

    def run(self, stop: threading.Event) -> None:
        try:
            while not stop.is_set():
                now = time.monotonic()
                if self.display is None and now - self.last_attempt >= REOPEN_S:
                    self._open(now)
                if self.display is not None:
                    self._refresh()
                self.state.set_module("lcd", {"enabled": True, "ready": self.display is not None, "error": self.error})
                stop.wait(self.period)
        finally:
            self._goodbye()

    def _open(self, now: float) -> None:
        self.last_attempt = now
        try:
            driver = load_display_driver()
            self.display = driver.create_display()
            self.show = driver.show_pil_image
            self.size = (driver.cfg.WIDTH, driver.cfg.HEIGHT)
        except Exception as exc:
            if str(exc) != self.error:
                log.warning("LCD unavailable: %s", exc)
            self.error = str(exc)
            self.display = None
            return
        self.fonts = {"big": load_font(BOLD_FONTS, 50), "small": load_font(REGULAR_FONTS, 15)}
        self.error = None
        self.last_key = None
        log.info("LCD ready (%dx%d)", *self.size)

    def _refresh(self) -> None:
        snapshot = self.state.latest()
        key = frame_key(snapshot)
        if key == self.last_key:
            return
        try:
            self.show(self.display, render(snapshot, self.size[0], self.size[1], self.fonts))
        except Exception as exc:
            log.warning("LCD write failed: %s", exc)
            self.error = str(exc)
            self.display = None
            return
        self.last_key = key

    def _goodbye(self) -> None:
        if self.display is None:
            return
        snapshot = {"ok": False, "error": "Tracker stopped", "tracked_deg": None}
        try:
            self.show(self.display, render(snapshot, self.size[0], self.size[1], self.fonts))
        except Exception:
            pass
