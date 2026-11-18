"""`--check`: report which parts of the setup are reachable before running as a service."""

from __future__ import annotations

import os
import platform
import subprocess

from . import servo as servo_mod
from .transport import open_transport


def line(ok: bool | None, label: str, detail: str) -> None:
    mark = {True: "ok  ", False: "FAIL", None: "--  "}[ok]
    print("[{}] {:<10} {}".format(mark, label, detail))


def run(settings: dict) -> int:
    failed = False
    line(None, "python", "{} on {}".format(platform.python_version(), platform.platform()))

    try:
        transport = open_transport(settings["device"])
        try:
            version = ".".join(str(v) for v in transport.read("VERSION"))
            doa = transport.read("DOA_VALUE")
            line(True, "reSpeaker", "{} firmware {}, DOA {}°, speech {}".format(
                transport.name.upper(), version, doa[0], doa[1]))
        finally:
            transport.close()
    except Exception as exc:
        failed = True
        line(False, "reSpeaker", str(exc))

    if settings["lcd"]["enabled"]:
        spi = os.path.exists("/dev/spidev0.0")
        failed |= not spi
        line(spi, "lcd", "/dev/spidev0.0 present" if spi else "/dev/spidev0.0 missing; enable SPI with raspi-config")
    else:
        line(None, "lcd", "disabled")

# cloud codes
    cloud = settings["cloud"]
    if cloud["enabled"]:
        from .cloud import check_connection
        ok, detail = check_connection(cloud)
        failed |= not ok
        line(ok, "cloud", detail)
    else:
        line(None, "cloud", "disabled")
# end of cloud codes

    servo = settings["servo"]
    if not servo["enabled"]:
        line(None, "servo", "disabled")
        return 1 if failed else 0
    gpio = servo["gpio"]
    chip = servo_mod.find_pwm_chip()
    channel = servo_mod.HW_PWM_CHANNELS.get(gpio)
    if chip is None or channel is None:
        try:
            import lgpio  # noqa: F401
            fallback = servo["backend"] != "hardware"
        except ImportError:
            fallback = False
        detail = "no hardware PWM for GPIO{}".format(gpio)
        line(fallback, "servo", detail + ("; lgpio software pulses will be used" if fallback else
                                          "; add dtoverlay=pwm,pin={},func=4 (lgpio fallback unavailable)".format(gpio)))
        failed |= not fallback
    else:
        path = chip / "pwm{}".format(channel)
        writable = os.access(path / "duty_cycle", os.W_OK)
        line(writable or os.geteuid() == 0, "servo",
             "GPIO{} -> {} ({})".format(gpio, path, "writable" if writable else "not exported for this user yet"))
    try:
        mux = subprocess.run(["pinctrl", "get", str(gpio)], capture_output=True, text=True, timeout=3).stdout.strip()
        line("PWM0" in mux or None, "pin mux", mux or "pinctrl printed nothing")
    except (OSError, subprocess.SubprocessError):
        line(None, "pin mux", "pinctrl not available")
    return 1 if failed else 0
