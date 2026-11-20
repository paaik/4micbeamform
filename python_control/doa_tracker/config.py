"""Tracker settings: built-in defaults overlaid with an optional TOML file."""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "doa_tracker.toml"

DEFAULTS = {
    "device": {
        "transport": "auto",
        "usb_vid": 0x2886,
        "usb_pid": 0x001A,
        "i2c_bus": 1,
        "i2c_address": 0x2C,
        "poll_hz": 20.0,
    },
    "tracking": {
        "source": "auto_beam",
        "speech_only": True,
    },
    "dashboard": {
        "enabled": True,
        "host": "0.0.0.0",
        "port": 8765,
        "open_browser": False,
    },
    "lcd": {
        "enabled": True,
        "refresh_hz": 8.0,
    },
    "servo": {
        "enabled": True,
        "gpio": 13,
        "backend": "auto",
        "min_pulse_us": 500,
        "max_pulse_us": 2400,
        "center_azimuth_deg": 0.0,
        "direction": 1,
        "out_of_range": "park",
        "camera_assist": True,
        "camera_forward_cm": 7.5,
        "camera_right_cm": 5.0,
        "sound_range_m": 10.0,
        "deadband_deg": 3.0,
        "max_speed_dps": 300.0,
        "release_after_s": 2.0,
    },
    "tilt": {
        "enabled": True,
        "gpio": 12,
        "backend": "auto",
        "min_pulse_us": 1000,
        "max_pulse_us": 2000,
        "level_servo_deg": 90.0,
        "direction": -1,
        "min_elevation_deg": 0.0,
        "max_elevation_deg": 70.0,
        "scan_elevations": [15.0, 40.0, 65.0],
        "scan_dwell_s": 1.0,
        "scan_speed_dps": 60.0,
        "max_speed_dps": 120.0,
        "deadband_deg": 1.5,
        "min_confidence": 0.4,
        "lost_after_s": 2.0,
    },
    "camera": {
        "enabled": True,
        "width": 1280,
        "height": 960,
        "fps": 15,
        "jpeg_quality": 75,
        "hflip": True,
        "vflip": True,
        "hfov_deg": 62.2,
        "vfov_deg": 48.8,
    },
    "acoustic": {
        "enabled": True,
        "device": "auto",
        "sample_rate": 0,
        "fft_size": 8192,
        "hop_s": 0.1,
        "display_max_hz": 4000.0,
        "history_s": 30.0,
        "bpf_min_hz": 100.0,
        "bpf_max_hz": 800.0,
        "harmonics": 5,
        "min_harmonics": 3,
        "peak_db": 6.0,
        "tolerance_pct": 3.0,
        "confirm_s": 1.5,
        "hold_s": 2.0,
        "background_s": 60.0,
        "blades": 3,
        "spl_offset_db": 0.0,
    },
}

CHOICES = {
    ("device", "transport"): ("auto", "usb", "i2c"),
    ("tracking", "source"): ("auto_beam", "doa", "processed"),
    ("servo", "backend"): ("auto", "hardware", "software"),
    ("servo", "out_of_range"): ("park", "clamp", "mirror", "flip"),
    ("servo", "camera_assist"): (True, False),
    ("servo", "direction"): (1, -1),
    ("tilt", "backend"): ("auto", "hardware", "software"),
    ("tilt", "direction"): (1, -1),
}


def load(path: Path | str | None = None) -> dict:
    settings = copy.deepcopy(DEFAULTS)
    if path is not None:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
        for section, values in data.items():
            if section not in settings or not isinstance(values, dict):
                raise ValueError("Unknown section [{}] in {}".format(section, path))
            for key, value in values.items():
                if key not in settings[section]:
                    raise ValueError("Unknown setting {}.{} in {}".format(section, key, path))
                settings[section][key] = value
    validate(settings)
    return settings


def validate(settings: dict) -> None:
    for (section, key), allowed in CHOICES.items():
        if settings[section][key] not in allowed:
            raise ValueError("{}.{} must be one of {}".format(section, key, ", ".join(map(str, allowed))))
    if settings["device"]["poll_hz"] <= 0:
        raise ValueError("device.poll_hz must be positive")
    if settings["lcd"]["refresh_hz"] <= 0:
        raise ValueError("lcd.refresh_hz must be positive")
    for section in ("servo", "tilt"):
        servo = settings[section]
        if not 0 < servo["min_pulse_us"] < servo["max_pulse_us"] < 20000:
            raise ValueError("{} pulse widths must satisfy 0 < min_pulse_us < max_pulse_us < 20000".format(section))
        if servo["max_speed_dps"] <= 0:
            raise ValueError("{}.max_speed_dps must be positive".format(section))
    pan = settings["servo"]
    for key in ("camera_forward_cm", "camera_right_cm"):
        value = pan[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not -100 <= value <= 100:
            raise ValueError("servo.{} must be a length from -100 to 100 cm".format(key))
    if isinstance(pan["sound_range_m"], bool) or not isinstance(pan["sound_range_m"], (int, float)) \
            or not 0 <= pan["sound_range_m"] <= 1000:
        raise ValueError("servo.sound_range_m must be 0 (source far away) or a distance up to 1000 m")

    tilt = settings["tilt"]
    if settings["servo"]["enabled"] and tilt["enabled"] and settings["servo"]["gpio"] == tilt["gpio"]:
        raise ValueError("servo.gpio and tilt.gpio must be different pins")
    low, high = tilt["min_elevation_deg"], tilt["max_elevation_deg"]
    if not 0 <= low < high <= 90:
        raise ValueError("tilt elevations must satisfy 0 <= min_elevation_deg < max_elevation_deg <= 90")
    for elevation in (low, high):
        servo_angle = tilt["level_servo_deg"] + tilt["direction"] * elevation
        if not 0 <= servo_angle <= 180:
            raise ValueError("tilt elevation {}° needs servo angle {}°, outside 0-180; check level_servo_deg "
                             "and direction".format(elevation, servo_angle))
    scan = tilt["scan_elevations"]
    if not isinstance(scan, list) or not scan or not all(low <= float(e) <= high for e in scan):
        raise ValueError("tilt.scan_elevations must be a non-empty list within min/max_elevation_deg")
    if tilt["scan_speed_dps"] <= 0 or tilt["scan_dwell_s"] < 0 or tilt["lost_after_s"] <= 0:
        raise ValueError("tilt.scan_speed_dps and lost_after_s must be positive, scan_dwell_s not negative")

    camera = settings["camera"]
    if not (64 <= camera["width"] <= 3280 and 64 <= camera["height"] <= 2464):
        raise ValueError("camera width/height must be within the IMX219's 3280x2464")
    if not 1 <= camera["fps"] <= 60 or not 10 <= camera["jpeg_quality"] <= 95:
        raise ValueError("camera.fps must be 1-60 and camera.jpeg_quality 10-95")
    if not (0 < camera["hfov_deg"] < 180 and 0 < camera["vfov_deg"] < 180):
        raise ValueError("camera field of view must be between 0 and 180 degrees")

    acoustic = settings["acoustic"]
    device = acoustic["device"]
    if isinstance(device, bool) or not isinstance(device, (int, str)) or device == "":
        raise ValueError('acoustic.device must be "auto", part of the input device name, or a device index')
    fft = acoustic["fft_size"]
    if not (isinstance(fft, int) and 1024 <= fft <= 65536 and fft & (fft - 1) == 0):
        raise ValueError("acoustic.fft_size must be a power of two from 1024 to 65536")
    if acoustic["sample_rate"] != 0 and not 8000 <= acoustic["sample_rate"] <= 192000:
        raise ValueError("acoustic.sample_rate must be 0 (the microphone's own rate) or 8000-192000")
    if not (0.02 <= acoustic["hop_s"] <= 1.0 and 5 <= acoustic["history_s"] <= 600):
        raise ValueError("acoustic.hop_s must be 0.02-1.0 and acoustic.history_s 5-600")
    if not 20 <= acoustic["bpf_min_hz"] < acoustic["bpf_max_hz"] <= 5000:
        raise ValueError("acoustic BPF search must satisfy 20 <= bpf_min_hz < bpf_max_hz <= 5000")
    if acoustic["display_max_hz"] < 500:
        raise ValueError("acoustic.display_max_hz must be at least 500")
    if not (2 <= acoustic["harmonics"] <= 12 and 1 <= acoustic["min_harmonics"] <= acoustic["harmonics"]):
        raise ValueError("acoustic needs 2 <= harmonics <= 12 and 1 <= min_harmonics <= harmonics")
    if acoustic["peak_db"] <= 0 or not 0.5 <= acoustic["tolerance_pct"] <= 10:
        raise ValueError("acoustic.peak_db must be positive and tolerance_pct 0.5-10")
    if acoustic["confirm_s"] <= 0 or acoustic["hold_s"] < 0 or acoustic["background_s"] < 0:
        raise ValueError("acoustic.confirm_s must be positive; hold_s and background_s not negative")
    if not (isinstance(acoustic["blades"], int) and acoustic["blades"] >= 1):
        raise ValueError("acoustic.blades must be a whole number of propeller blades")
    offset = acoustic["spl_offset_db"]
    if isinstance(offset, bool) or not isinstance(offset, (int, float)) or not 0 <= offset <= 200:
        raise ValueError("acoustic.spl_offset_db must be a number from 0 (off, show dBFS) to 200")
