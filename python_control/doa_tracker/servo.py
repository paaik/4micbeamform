"""Turn a TowerPro SG90 so its horn points toward the tracked azimuth.

Hardware PWM comes from the Raspberry Pi 5 RP1 PWM0 block through sysfs, which
needs `dtoverlay=pwm,pin=<gpio>,func=4` in /boot/firmware/config.txt. lgpio's
software-timed pulses are the fallback; they work, but the horn jitters more.
"""

from __future__ import annotations

import glob
import logging
import math
import os
import shutil
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

RP1_PWM0 = "1f00098000.pwm"
HW_PWM_CHANNELS = {12: 0, 13: 1, 18: 2, 19: 3}
PERIOD_NS = 20_000_000
SYSFS_PWM = Path("/sys/class/pwm")
CHANNEL_FILES = ("period", "duty_cycle", "enable", "polarity")
UPDATE_HZ = 50.0
REOPEN_S = 5.0
EDGE_HYSTERESIS_DEG = 10.0


class ServoError(Exception):
    pass


def wrap180(degrees: float) -> float:
    return (degrees + 180.0) % 360.0 - 180.0


def camera_look_azimuth(azimuth: float, servo: dict) -> float:
    """Heading the camera must use to look at a sound the array hears at `azimuth`.

    The camera is camera_forward_cm along the array's 0° axis and camera_right_cm
    toward 270° (right, looking along 0°). The sound is taken to be sound_range_m
    out from the array centre; 0 treats it as far enough that the offset doesn't matter.
    """
    range_m = float(servo.get("sound_range_m") or 0.0)
    if range_m <= 0.0:
        return azimuth
    theta = math.radians(azimuth)
    camera_x = servo["camera_forward_cm"] / 100.0
    camera_y = -servo["camera_right_cm"] / 100.0
    dx = range_m * math.cos(theta) - camera_x
    dy = range_m * math.sin(theta) - camera_y
    if dx * dx + dy * dy < 1e-8:
        return azimuth
    return math.degrees(math.atan2(dy, dx)) % 360.0


def aim(azimuth: float, servo: dict, end: str = "front") -> tuple[float, str]:
    """Servo angle (0-180, 90 = centre) for an array azimuth, and the horn state: "front", "rear" or "parked".

    `end` is the current state. "flip" swaps ends and "park" parks only once the
    source is EDGE_HYSTERESIS_DEG past the edge of the front end's reach (and
    "park" resumes as soon as it is back within reach), so a source near the
    edge doesn't make the horn swing back and forth.
    """
    relative = wrap180(azimuth - servo["center_azimuth_deg"])
    mode = servo["out_of_range"]
    limit = 90.0 + EDGE_HYSTERESIS_DEG
    if mode == "flip":
        rear = end == "rear"
        if abs(wrap180(relative - 180.0) if rear else relative) > limit:
            rear = not rear
        end = "rear" if rear else "front"
        if rear:
            relative = wrap180(relative - 180.0)
        relative = max(-90.0, min(90.0, relative))
    elif mode == "park":
        if end == "parked" and abs(relative) > 90.0 or abs(relative) > limit:
            end, relative = "parked", 0.0
        else:
            end, relative = "front", max(-90.0, min(90.0, relative))
    else:
        end = "front"
        if abs(relative) > 90.0:
            if mode == "mirror":
                relative = math.copysign(180.0, relative) - relative
            else:
                relative = math.copysign(90.0, relative)
    return 90.0 + servo["direction"] * relative, end


def pointing_azimuth(angle: float, servo: dict, rear: bool = False) -> float:
    return (servo["center_azimuth_deg"] + (angle - 90.0) * servo["direction"] + (180.0 if rear else 0.0)) % 360.0


def pulse_us(angle: float, servo: dict) -> float:
    span = servo["max_pulse_us"] - servo["min_pulse_us"]
    return servo["min_pulse_us"] + span * max(0.0, min(180.0, angle)) / 180.0


def find_pwm_chip() -> Path | None:
    for chip in sorted(SYSFS_PWM.glob("pwmchip*")):
        if RP1_PWM0 in os.path.realpath(chip / "device"):
            return chip
    return None


def export_channel(chip: Path, channel: int, timeout: float = 2.0) -> Path:
    path = chip / "pwm{}".format(channel)
    if not path.exists():
        (chip / "export").write_text(str(channel))
    deadline = time.monotonic() + timeout
    while not os.access(path / "duty_cycle", os.W_OK):
        if time.monotonic() > deadline:
            raise ServoError("{} is not writable by this user".format(path))
        time.sleep(0.05)
    return path


class HardwarePwm:
    name = "hardware PWM"

    def __init__(self, gpio: int):
        if gpio not in HW_PWM_CHANNELS:
            raise ServoError("GPIO{} has no RP1 hardware PWM channel (use 12, 13, 18 or 19)".format(gpio))
        chip = find_pwm_chip()
        if chip is None:
            raise ServoError("RP1 PWM0 is not enabled; add dtoverlay=pwm,pin={},func=4 to "
                             "/boot/firmware/config.txt and reboot".format(gpio))
        try:
            self.path = export_channel(chip, HW_PWM_CHANNELS[gpio])
            self._write("duty_cycle", 0)
            self._write("period", PERIOD_NS)
            self._write("enable", 1)
        except PermissionError as exc:
            raise ServoError("No permission for {} ({}); run --prepare-hardware as root first".format(chip, exc)) from exc

    def _write(self, name: str, value: int) -> None:
        (self.path / name).write_text(str(value))

    def pulse(self, microseconds: float) -> None:
        self._write("duty_cycle", int(microseconds * 1000))

    def release(self) -> None:
        self._write("duty_cycle", 0)

    def close(self) -> None:
        self._write("duty_cycle", 0)
        self._write("enable", 0)


def open_header_gpiochip(lgpio):
    """Pi 5 exposes the 40-pin header as pinctrl-rp1; its gpiochip number depends on the kernel."""
    for device in sorted(glob.glob("/dev/gpiochip*")):
        handle = lgpio.gpiochip_open(int(device[len("/dev/gpiochip"):]))
        label = lgpio.gpio_get_chip_info(handle)[3]
        if label.startswith("pinctrl-rp1") or label.startswith("pinctrl-bcm"):
            return handle
        lgpio.gpiochip_close(handle)
    raise ServoError("No Raspberry Pi header GPIO chip found")


class SoftwarePwm:
    name = "lgpio software PWM"

    def __init__(self, gpio: int):
        try:
            import lgpio
        except ImportError as exc:
            raise ServoError("lgpio is not installed (sudo apt install python3-lgpio)") from exc
        self._lgpio = lgpio
        self._gpio = gpio
        self._handle = open_header_gpiochip(lgpio)
        try:
            lgpio.gpio_claim_output(self._handle, gpio, 0)
        except lgpio.error as exc:
            lgpio.gpiochip_close(self._handle)
            raise ServoError("Cannot claim GPIO{} ({})".format(gpio, exc)) from exc

    def pulse(self, microseconds: float) -> None:
        self._lgpio.tx_servo(self._handle, self._gpio, int(microseconds), 50)

    def release(self) -> None:
        self._lgpio.tx_servo(self._handle, self._gpio, 0)

    def close(self) -> None:
        self.release()
        self._lgpio.gpio_free(self._handle, self._gpio)
        self._lgpio.gpiochip_close(self._handle)


def open_driver(servo: dict):
    backend = servo["backend"]
    if backend in ("auto", "hardware"):
        try:
            return HardwarePwm(servo["gpio"])
        except (ServoError, OSError) as exc:
            if backend == "hardware":
                raise
            log.warning("Hardware PWM unavailable (%s); falling back to lgpio software pulses", exc)
    return SoftwarePwm(servo["gpio"])


def prepare_hardware(servos: list[dict], group: str) -> int:
    """Run as root before the service starts: export each servo's PWM channel and hand it to `group`."""
    for servo in servos:
        if not servo["enabled"] or servo["backend"] == "software":
            continue
        channel = HW_PWM_CHANNELS.get(servo["gpio"])
        chip = find_pwm_chip()
        if channel is None or chip is None:
            log.warning("Hardware PWM is not available for GPIO%d; it will use software pulses", servo["gpio"])
            continue
        try:
            path = export_channel(chip, channel)
            for name in CHANNEL_FILES:
                target = path / name
                if target.exists():
                    shutil.chown(target, group=group)
                    os.chmod(target, 0o664)
            log.info("Exported %s for group %s", path, group)
        except (OSError, LookupError, ServoError) as exc:
            log.warning("Could not prepare hardware PWM for GPIO%d: %s", servo["gpio"], exc)
    return 0


# Azimuths for the pan test, unwrapped so the sign gives the direction: counter-clockwise
# 0 -> 90 -> 180, then clockwise 180 -> 90 -> 0 -> 270 (-90) -> 180 (-180).
PAN_TEST_PATH = (0.0, 90.0, 180.0, 90.0, 0.0, -90.0, -180.0)
PAN_TEST_DPS = 30.0
PAN_TEST_PAUSE_S = 1.0


END_CHANGES = {
    "front": "tracking with the front end of the horn",
    "rear": "swapping to the rear end of the horn",
    "parked": "out of range; parking at the centre",
}


def pan_test(servo: dict) -> int:
    """Turn slowly through PAN_TEST_PATH, aiming like the tracker does (end swaps, parking)."""
    driver = open_driver(servo)
    log.info("Servo on GPIO%d via %s; turning at %.0f°/s, out_of_range = %s",
             servo["gpio"], driver.name, PAN_TEST_DPS, servo["out_of_range"])
    step = PAN_TEST_DPS / UPDATE_HZ
    max_turn = servo["max_speed_dps"] / UPDATE_HZ
    azimuth, end = PAN_TEST_PATH[0], "front"
    angle = aim(azimuth, servo)[0]
    try:
        for goal in PAN_TEST_PATH:
            while True:
                azimuth = goal if abs(goal - azimuth) <= step else azimuth + math.copysign(step, goal - azimuth)
                target, changed = aim(azimuth % 360.0, servo, end)
                if changed != end:
                    log.info("  azimuth %5.1f°: %s", azimuth % 360.0, END_CHANGES[changed])
                end = changed
                angle += max(-max_turn, min(max_turn, target - angle))
                driver.pulse(pulse_us(angle, servo))
                if azimuth == goal and abs(target - angle) < 1e-6:
                    break
                time.sleep(1.0 / UPDATE_HZ)
            log.info("azimuth %5.1f°  servo %5.1f°  pulse %4.0f us  %s points at %5.1f°",
                     goal % 360.0, angle, pulse_us(angle, servo),
                     "parked, front end" if end == "parked" else end + " end",
                     pointing_azimuth(angle, servo, end == "rear"))
            time.sleep(PAN_TEST_PAUSE_S)
    finally:
        driver.close()
    return 0


def sweep(servo: dict, positions: list[tuple[float, str]]) -> int:
    """Move through (servo angle, description) pairs at 20°/s, so a tilt test cannot slam into the stop."""
    driver = open_driver(servo)
    speed = 20.0
    log.info("Servo on GPIO%d via %s, moving at %.0f°/s", servo["gpio"], driver.name, speed)
    # Start at the level pose (1500 us on this kit). The first goal can be the
    # end of travel, and an instant jump there drives the plate into the bracket.
    angle = float(servo.get("level_servo_deg", positions[0][0]))
    try:
        driver.pulse(pulse_us(angle, servo))
        log.info("servo %5.1f°  pulse %4.0f us  start (level pose)", angle, pulse_us(angle, servo))
        time.sleep(1.0)
        for goal, description in positions:
            while abs(goal - angle) > 0.05:
                step = math.copysign(min(speed / UPDATE_HZ, abs(goal - angle)), goal - angle)
                angle += step
                driver.pulse(pulse_us(angle, servo))
                time.sleep(1.0 / UPDATE_HZ)
            angle = goal
            log.info("servo %5.1f°  pulse %4.0f us  %s", angle, pulse_us(angle, servo), description)
            time.sleep(1.0)
    finally:
        driver.close()
    return 0


class ServoController:
    def __init__(self, servo: dict, state):
        self.cfg = servo
        self.state = state
        self.driver = None
        self.angle = 90.0
        self.target = 90.0
        self.end = "front"
        self.source = "sound"
        self.holding = False
        self.last_move = 0.0
        self.last_attempt = -math.inf
        self.error = None

    def run(self, stop: threading.Event) -> None:
        last = time.monotonic()
        try:
            while not stop.is_set():
                now = time.monotonic()
                if self.driver is None and now - self.last_attempt >= REOPEN_S:
                    self._open(now)
                self._step(now - last)
                last = now
                self._report()
                stop.wait(1.0 / UPDATE_HZ)
        finally:
            self._close()

    def _open(self, now: float) -> None:
        self.last_attempt = now
        try:
            self.driver = open_driver(self.cfg)
        except (ServoError, OSError) as exc:
            if str(exc) != self.error:
                log.warning("Servo unavailable: %s", exc)
            self.error = str(exc)
            return
        self.error = None
        log.info("Servo on GPIO%d via %s", self.cfg["gpio"], self.driver.name)
        if self.cfg["camera_forward_cm"] or self.cfg["camera_right_cm"]:
            log.info("Camera is %.1f cm forward and %.1f cm toward 270° of the array centre; "
                     "sound bearings are aimed for a source %.1f m away",
                     self.cfg["camera_forward_cm"], self.cfg["camera_right_cm"], self.cfg["sound_range_m"])
        self._send()

    def _goal_azimuth(self):
        """The camera's drone azimuth while the tilt is locked on (camera_assist), else the sound direction."""
        if self.cfg["camera_assist"]:
            tilt = self.state.module("tilt") or {}
            lock = tilt.get("lock") if tilt.get("mode") == "track" else None
            if lock and lock.get("azimuth_deg") is not None:
                return lock["azimuth_deg"], "camera"
        tracked = self.state.latest().get("tracked_deg")
        if tracked is None:
            return None, "sound"
        return camera_look_azimuth(tracked, self.cfg), "sound"

    def _step(self, elapsed: float) -> None:
        azimuth, source = self._goal_azimuth()
        if source != self.source:
            log.info("Pan servo now following the %s", "drone in the camera image" if source == "camera" else "sound")
            self.source = source
        if azimuth is not None:
            goal, end = aim(azimuth, self.cfg, self.end)
            if end != self.end or abs(goal - self.target) >= self.cfg["deadband_deg"]:
                self.target, self.end = goal, end
        if self.driver is None:
            return
        delta = self.target - self.angle
        if abs(delta) > 0.05:
            step = self.cfg["max_speed_dps"] * elapsed
            self.angle += max(-step, min(step, delta))
            self._send()
        elif (self.holding and self.cfg["release_after_s"] > 0
              and time.monotonic() - self.last_move > self.cfg["release_after_s"]):
            try:
                self.driver.release()
                self.holding = False
            except OSError as exc:
                self._lost(exc)

    def _send(self) -> None:
        try:
            self.driver.pulse(pulse_us(self.angle, self.cfg))
        except OSError as exc:
            self._lost(exc)
            return
        self.holding = True
        self.last_move = time.monotonic()

    def _lost(self, exc: Exception) -> None:
        log.warning("Servo output failed: %s", exc)
        self.error = str(exc)
        self._close()

    def _close(self) -> None:
        if self.driver is not None:
            try:
                self.driver.close()
            except Exception:
                pass
            self.driver = None
            self.holding = False

    def _report(self) -> None:
        self.state.set_module("servo", {
            "enabled": True,
            "gpio": self.cfg["gpio"],
            "backend": None if self.driver is None else self.driver.name,
            "angle_deg": round(self.angle, 1),
            "target_deg": round(self.target, 1),
            "pointing_deg": round(pointing_azimuth(self.angle, self.cfg, self.end == "rear"), 1),
            "end": self.end,
            "source": self.source,
            "holding": self.holding,
            "error": self.error,
        })
