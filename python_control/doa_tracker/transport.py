"""Read XVF3800 control parameters over USB (USB firmware) or I2C (I2S firmware).

Both links carry the same XMOS device-control protocol: a read names a
resource ID and command ID (with bit 7 set) and returns one status byte
followed by the payload. Parameter layouts come from xvf_host.PARAMETERS.
"""

from __future__ import annotations

import logging
import struct
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import libusb_package  # noqa: F401
except ImportError:
    if sys.platform.startswith("win"):
        raise
    # xvf_host imports libusb_package at module level but only calls it on Windows.
    sys.modules["libusb_package"] = types.ModuleType("libusb_package")

import usb.core  # noqa: E402
import usb.util  # noqa: E402

import xvf_host  # noqa: E402

log = logging.getLogger(__name__)

CONTROL_SUCCESS = 0
SERVICER_COMMAND_RETRY = 64
MAX_RETRIES = 100
USB_TIMEOUT_MS = 1000

TYPE_SIZE = {"uint8": 1, "char": 1, "uint16": 2, "uint32": 4, "int32": 4, "float": 4, "radians": 4}
TYPE_FORMAT = {"uint8": "B", "uint16": "H", "uint32": "I", "int32": "i", "float": "f", "radians": "f"}


class TransportError(Exception):
    """The device answered, but not with usable data."""


class Transport:
    name = ""

    def raw_read(self, resid: int, cmdid: int, length: int) -> bytes:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def read(self, name: str):
        resid, cmdid, count, _access, kind = xvf_host.PARAMETERS[name][:5]
        length = count * TYPE_SIZE[kind] + 1
        for _ in range(MAX_RETRIES):
            response = self.raw_read(resid, 0x80 | cmdid, length)
            status = response[0]
            if status == CONTROL_SUCCESS:
                break
            if status != SERVICER_COMMAND_RETRY:
                raise TransportError("{} returned status {}".format(name, status))
            time.sleep(0.002)
        else:
            raise TransportError("{} kept asking for a retry".format(name))
        payload = bytes(response[1:length])
        if len(payload) != length - 1:
            raise TransportError("{} returned {} bytes, expected {}".format(name, len(payload), length - 1))
        if kind == "char":
            return payload.rstrip(b"\x00").decode("utf-8", errors="ignore")
        return struct.unpack("<" + TYPE_FORMAT[kind] * count, payload)


class UsbTransport(Transport):
    name = "usb"

    def __init__(self, vid: int, pid: int):
        device = xvf_host.find(vid=vid, pid=pid)
        if device is None:
            raise TransportError("No XVF3800 on USB ({:04x}:{:04x})".format(vid, pid))
        self._device = device
        try:
            self.read("VERSION")
        except usb.core.USBError as exc:
            device.close()
            hint = " (install the udev rule in deploy/)" if exc.errno == 13 else ""
            raise TransportError("XVF3800 found on USB but not readable: {}{}".format(exc, hint)) from exc

    def raw_read(self, resid: int, cmdid: int, length: int) -> bytes:
        response = self._device.dev.ctrl_transfer(
            usb.util.CTRL_IN | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE,
            0, cmdid, resid, length, USB_TIMEOUT_MS)
        return bytes(response)

    def close(self) -> None:
        self._device.close()


class I2cTransport(Transport):
    name = "i2c"

    def __init__(self, bus: int, address: int):
        try:
            from smbus2 import SMBus, i2c_msg
        except ImportError as exc:
            raise TransportError("smbus2 is not installed, so I2C is unavailable") from exc
        try:
            self._bus = SMBus(bus)
        except OSError as exc:
            raise TransportError("Cannot open /dev/i2c-{} ({})".format(bus, exc)) from exc
        self._msg = i2c_msg
        self._address = address
        try:
            self.read("VERSION")
        except OSError as exc:
            self._bus.close()
            raise TransportError("No XVF3800 at I2C address {:#04x} on bus {} ({})".format(address, bus, exc)) from exc

    def raw_read(self, resid: int, cmdid: int, length: int) -> bytes:
        request = self._msg.write(self._address, [resid, cmdid, length])
        response = self._msg.read(self._address, length)
        self._bus.i2c_rdwr(request, response)
        return bytes(list(response))

    def close(self) -> None:
        self._bus.close()


def open_transport(device: dict) -> Transport:
    kind = device["transport"]
    errors = []
    if kind in ("auto", "usb"):
        try:
            return UsbTransport(device["usb_vid"], device["usb_pid"])
        except (TransportError, usb.core.USBError, ValueError) as exc:
            errors.append(str(exc))
    if kind in ("auto", "i2c"):
        try:
            return I2cTransport(device["i2c_bus"], device["i2c_address"])
        except TransportError as exc:
            errors.append(str(exc))
    raise TransportError("; ".join(errors))
