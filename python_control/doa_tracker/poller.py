"""Poll direction of arrival from the XVF3800 and publish snapshots to the other modules."""

from __future__ import annotations

import copy
import logging
import math
import threading
import time

from .transport import Transport, TransportError, open_transport

log = logging.getLogger(__name__)

BEAM_NAMES = ("Beam 1", "Beam 2", "Free-running", "Auto-select")

SIGNAL_CATEGORIES = {
    0: "silence",
    1: "mic",
    2: "unpacked mic",
    3: "mic + gain/delay",
    4: "reference 16 kHz",
    5: "reference + delay",
    6: "processed beam",
    7: "AEC residual / ASR",
    8: "user output",
    9: "post SHF",
    10: "reference at interface rate",
    11: "mic + gain",
    12: "reference + gain/delay",
}

BEAM_SOURCES = {0: "focused 0", 1: "focused 1", 2: "scanning", 3: "automatic"}


def num(value):
    if value is None:
        return None
    value = float(value)
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def degrees(radians):
    value = num(radians)
    if value is None:
        return None
    return math.degrees(value) % 360.0


def route_label(pair):
    category, source = int(pair[0]), int(pair[1])
    name = SIGNAL_CATEGORIES.get(category, "category {}".format(category))
    if category == 6:
        source_name = BEAM_SOURCES.get(source, str(source))
    elif category in (1, 2, 3, 11):
        source_name = "mic {}".format(source)
    else:
        source_name = str(source)
    return {"category": category, "source": source, "label": "{} {}".format(name, source_name)}


INFO_ONCE = (
    ("VERSION", "firmware", lambda v: [int(x) for x in v]),
    ("AEC_NUM_MICS", "mic_count", lambda v: int(v[0])),
    ("AEC_MIC_ARRAY_TYPE", "array_type", lambda v: int(v[0])),
    ("AEC_MIC_ARRAY_GEO", "mics", lambda v: [
        [num(v[i]), num(v[i + 1]), num(v[i + 2])] for i in range(0, len(v), 3)
    ]),
)

INFO_LIVE = (
    ("USB_BIT_DEPTH", "usb_bit_depth", lambda v: [int(x) for x in v]),
    ("I2S_INACTIVE", "i2s_inactive", lambda v: int(v[0])),
    ("AUDIO_MGR_OP_L", "output_l", route_label),
    ("AUDIO_MGR_OP_R", "output_r", route_label),
    ("AEC_FIXEDBEAMSELEVATION_VALUES", "fixed_elevation_deg", lambda v: [degrees(x) for x in v]),
)

FAST_EXTRAS = ("AEC_SPENERGY_VALUES", "AUDIO_MGR_SELECTED_AZIMUTHS")


class SharedState:
    """Latest snapshot plus module status, with a way to wait for the next update."""

    def __init__(self):
        self._cond = threading.Condition()
        self._snapshot = {"seq": 0, "t": 0.0, "ok": False, "error": "Starting", "tracked_deg": None}
        self._modules = {}

    def publish(self, snapshot: dict) -> None:
        with self._cond:
            self._snapshot = dict(snapshot, seq=self._snapshot["seq"] + 1)
            self._cond.notify_all()

    def set_module(self, name: str, status: dict) -> None:
        with self._cond:
            self._modules[name] = status

    def latest(self) -> dict:
        with self._cond:
            snapshot = dict(self._snapshot)
            snapshot["modules"] = copy.deepcopy(self._modules)
        return snapshot

    def wait_newer(self, seq: int, timeout: float) -> dict:
        with self._cond:
            self._cond.wait_for(lambda: self._snapshot["seq"] != seq, timeout)
        return self.latest()


class DoaPoller:
    def __init__(self, device: dict, tracking: dict, state: SharedState):
        self.device = device
        self.source = tracking["source"]
        self.speech_only = tracking["speech_only"]
        self.state = state
        self.period = 1.0 / device["poll_hz"]
        self.transport: Transport | None = None
        self.tracked = None
        self.tracked_at = None
        self.last_error = None
        self._reset_link_state()

    def _reset_link_state(self):
        self.info = {}
        self.azimuths = (None,) * 4
        self.energy = (None,) * 4
        self.selected = (None, None)
        self.once_queue = list(INFO_ONCE)
        self.cycle = 0
        self.reads = 0
        self.rate_window_start = time.monotonic()

    def run(self, stop: threading.Event) -> None:
        try:
            while not stop.is_set():
                started = time.monotonic()
                try:
                    self.poll_once()
                except Exception as exc:
                    self._fail(exc)
                    stop.wait(1.0)
                    continue
                remaining = self.period - (time.monotonic() - started)
                if remaining > 0:
                    stop.wait(remaining)
        finally:
            self._close()

    def poll_once(self) -> None:
        if self.transport is None:
            self.transport = open_transport(self.device)
            self._reset_link_state()
            self.last_error = None
            log.info("Connected to the XVF3800 over %s", self.transport.name.upper())

        doa = self.transport.read("DOA_VALUE")
        self.azimuths = self.transport.read("AEC_AZIMUTH_VALUES")
        if self.source == "processed":
            self.selected = self.transport.read("AUDIO_MGR_SELECTED_AZIMUTHS")
        self._read_extra()

        snapshot = self._snapshot(doa)
        self.state.publish(snapshot)
        self._log_rate(snapshot)

    def _read_extra(self) -> None:
        """One slower read per cycle so the direction keeps its full update rate."""
        self.cycle += 1
        if self.once_queue:
            self._store_info(*self.once_queue.pop(0))
            return
        if self.cycle % 2 == 0:
            name = FAST_EXTRAS[(self.cycle // 2) % len(FAST_EXTRAS)]
            try:
                value = self.transport.read(name)
            except TransportError:
                return
            if name == "AEC_SPENERGY_VALUES":
                self.energy = value
            else:
                self.selected = value
        else:
            self._store_info(*INFO_LIVE[(self.cycle // 2) % len(INFO_LIVE)])

    def _store_info(self, name, key, parse) -> None:
        try:
            self.info[key] = parse(self.transport.read(name))
        except TransportError:
            self.info[key] = None

    def _source_angle(self, doa_deg: int):
        if self.source == "doa":
            return float(doa_deg)
        if self.source == "processed":
            return degrees(self.selected[0])
        return degrees(self.azimuths[3])

    def _snapshot(self, doa) -> dict:
        now = time.time()
        doa_deg = int(doa[0]) % 360
        speech = bool(int(doa[1]))
        raw = self._source_angle(doa_deg)
        live = raw is not None and (speech or not self.speech_only)
        if live:
            self.tracked = raw
            self.tracked_at = now

        beams = []
        for index, name in enumerate(BEAM_NAMES):
            beams.append({
                "name": name,
                "azimuth_deg": degrees(self.azimuths[index]),
                "energy": num(self.energy[index]),
            })
        snapshot = {
            "t": now,
            "ok": True,
            "error": None,
            "transport": self.transport.name,
            "doa_deg": doa_deg,
            "speech": speech,
            "beams": beams,
            "selected_deg": [degrees(self.selected[0]), degrees(self.selected[1])],
            "tracking_source": self.source,
            "tracked_deg": self.tracked,
            "tracking_live": live,
            "tracked_age_s": None if self.tracked_at is None else now - self.tracked_at,
        }
        snapshot.update(self.info)
        return snapshot

    def _fail(self, exc: Exception) -> None:
        message = str(exc) or exc.__class__.__name__
        if message != self.last_error:
            log.warning("XVF3800 unavailable: %s", message)
            self.last_error = message
        self._close()
        self.state.publish({
            "t": time.time(),
            "ok": False,
            "error": message,
            "tracking_source": self.source,
            "tracked_deg": self.tracked,
            "tracking_live": False,
        })

    def _close(self) -> None:
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:
                pass
            self.transport = None

    def _log_rate(self, snapshot: dict) -> None:
        self.reads += 1
        elapsed = time.monotonic() - self.rate_window_start
        if elapsed < 10.0:
            return
        tracked = snapshot["tracked_deg"]
        log.info("DOA %3d°  tracked %s  %s  %.1f Hz",
                 snapshot["doa_deg"],
                 "---" if tracked is None else "{:5.1f}°".format(tracked),
                 "speech" if snapshot["speech"] else "quiet",
                 self.reads / elapsed)
        self.reads = 0
        self.rate_window_start = time.monotonic()
