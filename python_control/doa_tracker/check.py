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

    if settings["camera"]["enabled"]:
        ok, detail = check_camera()
        failed |= not ok
        line(ok, "camera", detail)
    else:
        line(None, "camera", "disabled")

    for label, key in (("pan servo", "servo"), ("tilt servo", "tilt")):
        if settings[key]["enabled"]:
            failed |= not check_servo(label, settings[key])
        else:
            line(None, label, "disabled")

    if settings["acoustic"]["enabled"]:
        ok, detail = check_microphone(settings["acoustic"])
        failed |= ok is False
        line(ok, "mic", detail)
    else:
        line(None, "mic", "disabled")

    if settings["cloud"]["enabled"]:
        from .cloud import check_connection
        ok, detail = check_connection(settings["cloud"])
        failed |= not ok
        line(ok, "cloud", detail)
    else:
        line(None, "cloud", "disabled")
    return 1 if failed else 0


def check_microphone(acoustic: dict) -> tuple[bool | None, str]:
    try:
        import sounddevice as sd
    except (ImportError, OSError) as exc:
        return False, "sounddevice/PortAudio missing (sudo apt install libportaudio2): {}".format(exc)
    from .acoustic import find_microphone

    inputs = ["{}: {}".format(i, d["name"].strip()) for i, d in enumerate(sd.query_devices())
              if d["max_input_channels"] > 0]
    found = find_microphone(sd, acoustic["device"])
    if found is None:
        # Not a failure: the tracker keeps looking and starts once the mic is plugged in.
        return None, "no input matches device = {!r}; inputs: {}".format(acoustic["device"], "; ".join(inputs) or "none")
    rate = int(sd.query_devices(found[0])["default_samplerate"])
    return True, "{} (device {}, {} Hz)".format(found[1].strip(), found[0], rate)


def check_camera() -> tuple[bool, str]:
    # Listing cameras while the tracker service streams makes libcamera log a harmless
    # "Device or resource busy"; keep its log output to fatal errors here.
    os.environ.setdefault("LIBCAMERA_LOG_LEVELS", "*:FATAL")
    try:
        from picamera2 import Picamera2
    except ImportError:
        return False, "picamera2 missing (sudo apt install python3-picamera2)"
    cameras = Picamera2.global_camera_info()
    if not cameras:
        return False, "no camera found; check the ribbon cable (Pi 5 needs a 22-to-15 pin cable)"
    names = ", ".join("{} (camera {})".format(c.get("Model"), c.get("Num", 0)) for c in cameras)
    if subprocess.run(["systemctl", "is-active", "--quiet", "doa-tracker"], check=False).returncode == 0:
        names += "; doa-tracker.service is running and should be streaming it"
    return True, names


def check_servo(label: str, servo: dict) -> bool:
    gpio = servo["gpio"]
    chip = servo_mod.find_pwm_chip()
    channel = servo_mod.HW_PWM_CHANNELS.get(gpio)
    if chip is None or channel is None:
        try:
            import lgpio  # noqa: F401
            ok = servo["backend"] != "hardware"
        except ImportError:
            ok = False
        detail = "no hardware PWM for GPIO{}".format(gpio)
        line(ok, label, detail + ("; lgpio software pulses will be used" if ok else
                                  "; enable the pwm overlay (lgpio fallback unavailable)"))
    else:
        path = chip / "pwm{}".format(channel)
        writable = os.access(path / "duty_cycle", os.W_OK)
        ok = writable or os.geteuid() == 0
        line(ok, label,
             "GPIO{} -> {} ({})".format(gpio, path, "writable" if writable else "not exported for this user yet"))
    try:
        mux = subprocess.run(["pinctrl", "get", str(gpio)], capture_output=True, text=True, timeout=3).stdout.strip()
        line("PWM0" in mux or None, "pin mux", mux or "pinctrl printed nothing")
    except (OSError, subprocess.SubprocessError):
        line(None, "pin mux", "pinctrl not available")
    return ok
