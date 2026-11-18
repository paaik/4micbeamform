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
        "out_of_range": "clamp",
        "deadband_deg": 3.0,
        "max_speed_dps": 300.0,
        "release_after_s": 2.0,
    },
    "cloud": {
        "enabled": False,
        "url": "",
        "table": "detections",
        "bucket": "photos",
        "key_env": "SUPABASE_KEY",
        "device_id": "drone-detector-1",
        "auto_rows": True,
        "sample_interval_s": 1.0,
        "upload_interval_s": 5.0,
        "max_queue": 5000,
    },
}

CHOICES = {
    ("device", "transport"): ("auto", "usb", "i2c"),
    ("tracking", "source"): ("auto_beam", "doa", "processed"),
    ("servo", "backend"): ("auto", "hardware", "software"),
    ("servo", "out_of_range"): ("clamp", "mirror"),
    ("servo", "direction"): (1, -1),
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
    servo = settings["servo"]
    if not 0 < servo["min_pulse_us"] < servo["max_pulse_us"] < 20000:
        raise ValueError("servo pulse widths must satisfy 0 < min_pulse_us < max_pulse_us < 20000")
    if servo["max_speed_dps"] <= 0:
        raise ValueError("servo.max_speed_dps must be positive")

    # cloud codes
    cloud = settings["cloud"]
    if cloud["enabled"] and not str(cloud["url"]).startswith("https://"):
        raise ValueError("cloud.url must be your Supabase project URL (https://...) when cloud.enabled = true")
    if cloud["sample_interval_s"] <= 0 or cloud["upload_interval_s"] <= 0 or cloud["max_queue"] < 1:
        raise ValueError("cloud intervals must be positive and cloud.max_queue at least 1")
    # end of the cloud codes