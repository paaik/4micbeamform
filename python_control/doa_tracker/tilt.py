"""Tilt the camera between 0 and 90 degrees of elevation to find and follow a drone.

With nothing detected the tilt servo scans: it steps through `scan_elevations`,
pausing `scan_dwell_s` at each so the camera gets sharp frames, then reverses.
When the off-Pi detector reports a box above `min_confidence`, it moves to that
box's elevation and keeps following it. After `lost_after_s` without one it
goes back to scanning from the nearest scan position.
"""

from __future__ import annotations

import logging
import math
import threading
import time

from .servo import REOPEN_S, UPDATE_HZ, ServoError, open_driver, pulse_us

log = logging.getLogger(__name__)


def servo_angle(elevation: float, tilt: dict) -> float:
    return tilt["level_servo_deg"] + tilt["direction"] * elevation


def sweep_positions(tilt: dict) -> list[tuple[float, str]]:
    low, high = tilt["min_elevation_deg"], tilt["max_elevation_deg"]
    middle = (low + high) / 2.0
    return [(servo_angle(e, tilt), "elevation {:4.1f}°".format(e)) for e in (low, middle, high, middle, low)]


class TiltController:
    def __init__(self, tilt: dict, state, hub=None):
        self.cfg = tilt
        self.state = state
        self.hub = hub
        self.scan = sorted(float(e) for e in tilt["scan_elevations"])
        self.scan_index = 0
        self.scan_step = 1
        self.elevation = self.scan[0]
        self.target = self.scan[0]
        self.mode = "scan"
        self.arrived_at = None
        self.last_seq = None
        self.last_seen = -math.inf
        self.lock = None
        self.driver = None
        self.last_attempt = -math.inf
        self.error = None

    def run(self, stop: threading.Event) -> None:
        last = time.monotonic()
        try:
            while not stop.is_set():
                now = time.monotonic()
                if self.driver is None and now - self.last_attempt >= REOPEN_S:
                    self._open(now)
                self._step(now, now - last)
                last = now
                self._report(now)
                stop.wait(1.0 / UPDATE_HZ)
        finally:
            self._close()

    def _open(self, now: float) -> None:
        self.last_attempt = now
        try:
            self.driver = open_driver(self.cfg)
        except (ServoError, OSError) as exc:
            if str(exc) != self.error:
                log.warning("Tilt servo unavailable: %s", exc)
            self.error = str(exc)
            return
        self.error = None
        log.info("Tilt servo on GPIO%d via %s", self.cfg["gpio"], self.driver.name)
        self._send()

    def _step(self, now: float, elapsed: float) -> None:
        self._follow_detections(now)
        if self.mode == "track" and now - self.last_seen > self.cfg["lost_after_s"]:
            log.info("Drone lost at %.1f° elevation; scanning", self.elevation)
            self.mode, self.lock = "scan", None
            self.scan_index = min(range(len(self.scan)), key=lambda i: abs(self.scan[i] - self.elevation))
            self.target, self.arrived_at = self.scan[self.scan_index], None
        if self.mode == "scan":
            self._advance_scan(now)

        if self.driver is None:
            return
        delta = self.target - self.elevation
        if abs(delta) > 0.05:
            speed = self.cfg["max_speed_dps"] if self.mode == "track" else self.cfg["scan_speed_dps"]
            step = speed * elapsed
            self.elevation += max(-step, min(step, delta))
            self._send()

    def _follow_detections(self, now: float) -> None:
        result = None if self.hub is None else self.hub.latest_result()
        if result is None or result["seq"] == self.last_seq:
            return
        self.last_seq = result["seq"]
        for detection in result["detections"]:
            if detection["confidence"] >= self.cfg["min_confidence"] and detection["elevation_deg"] is not None:
                break
        else:
            return
        goal = max(self.cfg["min_elevation_deg"], min(self.cfg["max_elevation_deg"], detection["elevation_deg"]))
        if self.mode != "track":
            log.info("Drone found (%s %.0f%%) at %.1f° elevation; tracking",
                     detection["label"], detection["confidence"] * 100, detection["elevation_deg"])
        self.mode, self.lock, self.last_seen = "track", detection, now
        if abs(goal - self.target) >= self.cfg["deadband_deg"]:
            self.target = goal

    def _advance_scan(self, now: float) -> None:
        if abs(self.elevation - self.target) > 0.05 or len(self.scan) < 2:
            return
        if self.arrived_at is None:
            self.arrived_at = now
        if now - self.arrived_at < self.cfg["scan_dwell_s"]:
            return
        if not 0 <= self.scan_index + self.scan_step < len(self.scan):
            self.scan_step = -self.scan_step
        self.scan_index += self.scan_step
        self.target, self.arrived_at = self.scan[self.scan_index], None

    def _send(self) -> None:
        try:
            self.driver.pulse(pulse_us(servo_angle(self.elevation, self.cfg), self.cfg))
        except OSError as exc:
            log.warning("Tilt servo output failed: %s", exc)
            self.error = str(exc)
            self._close()

    def _close(self) -> None:
        if self.driver is not None:
            try:
                self.driver.close()
            except Exception:
                pass
            self.driver = None

    def _report(self, now: float) -> None:
        moving = abs(self.target - self.elevation) > 0.05
        if self.hub is not None:
            self.hub.pose = {"elevation_deg": self.elevation if self.driver else None, "moving": moving}
        self.state.set_module("tilt", {
            "enabled": True,
            "gpio": self.cfg["gpio"],
            "backend": None if self.driver is None else self.driver.name,
            "mode": self.mode,
            "elevation_deg": round(self.elevation, 1),
            "target_deg": round(self.target, 1),
            "servo_deg": round(servo_angle(self.elevation, self.cfg), 1),
            "moving": moving,
            "lock": self.lock,
            "last_seen_s": None if self.lock is None else round(now - self.last_seen, 2),
            "error": self.error,
        })
