"""Live spectrogram and drone blade-pass-frequency (BPF) detection from a USB measurement microphone.

A multirotor's propellers hum at the blade-pass frequency, BPF = RPM / 60 x blades
(about 375 Hz for a hovering Parrot Bebop 2 Power: 7,500 RPM, 3 blades), plus a comb
of harmonics at whole multiples of it. Each spectrum is searched for that comb, and a
drone is flagged once the same comb persists for `confirm_s`.

The microphone is found by name and reopened every few seconds while it is missing,
so it can be plugged in or out while the tracker runs.
"""

from __future__ import annotations

import base64
import collections
import logging
import math
import queue
import threading
import time

import numpy as np

log = logging.getLogger(__name__)

REOPEN_S = 5.0
STALL_S = 3.0
STATUS_S = 0.5
ROWS = 256
DB_MIN, DB_MAX = -140.0, 0.0
RESPEAKER_NAMES = ("respeaker", "xvf")
FLOOR_HZ = 180.0
AVERAGE_FRAMES = 3
MAX_PROMINENCE_DB = 30.0
QUIET_DB = -125.0  # just above 16-bit quantisation noise per bin; quieter peaks are not sound
LEARN_S = 3.0
CONFIRM_FRACTION = 0.8
CONFIRM_SPREAD = 0.04
TRACK_DRIFT = 0.15
OCTAVE_SCORE = 0.7


def find_microphone(sd, wanted):
    """(index, name) of the input device to capture from, or None.

    "auto" takes the first input with "usb" in its name that isn't the reSpeaker;
    a string picks the first input containing it; an int is a device index.
    """
    inputs = [(i, d["name"]) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
    if isinstance(wanted, int):
        return next(((i, name) for i, name in inputs if i == wanted), None)
    wanted = wanted.lower()
    for index, name in inputs:
        lower = name.lower()
        if wanted == "auto":
            if "usb" in lower and not any(word in lower for word in RESPEAKER_NAMES):
                return index, name
        elif wanted in lower:
            return index, name
    return None


def running_median(values: np.ndarray, half: int) -> np.ndarray:
    padded = np.pad(values, half, mode="edge")
    return np.median(np.lib.stride_tricks.sliding_window_view(padded, 2 * half + 1), axis=-1)


def parabolic_peak(db: np.ndarray, index: int) -> float:
    if 0 < index < len(db) - 1:
        a, b, c = db[index - 1], db[index], db[index + 1]
        denom = a - 2 * b + c
        if denom < 0:
            return index + 0.5 * (a - c) / denom
    return float(index)


class BpfDetector:
    """Find the strongest harmonic comb whose fundamental lies in [bpf_min_hz, bpf_max_hz]."""

    def __init__(self, cfg: dict, rate: int, n: int):
        self.cfg = cfg
        self.bin_hz = rate / n
        self.floor_half = max(3, round(FLOOR_HZ / self.bin_hz / 2))
        step = self.bin_hz / 2
        self.candidates = np.arange(cfg["bpf_min_hz"], cfg["bpf_max_hz"] + step / 2, step)
        orders = np.arange(1, cfg["harmonics"] + 1)
        centers = self.candidates[:, None] * orders[None, :] / self.bin_hz
        half = np.maximum(1.0, centers * cfg["tolerance_pct"] / 100.0)
        self.size = min(n // 2 + 1, int(math.ceil((centers + half).max())) + self.floor_half + 2)
        lo = np.clip(np.floor(centers - half).astype(int), 1, self.size - 2)
        hi = np.clip(np.ceil(centers + half).astype(int) + 1, lo + 1, self.size - 1)
        self.lo, self.hi = lo, hi
        self.bounds = np.stack([lo, hi], axis=-1).ravel()
        self.recent = collections.deque(maxlen=AVERAGE_FRAMES)
        self.background = None
        self.frames = 0
        self.alpha = 0.0 if cfg["background_s"] <= 0 else min(1.0, cfg["hop_s"] / cfg["background_s"])
        self.quick_frames = round(LEARN_S / cfg["hop_s"])

    def analyse(self, power: np.ndarray, learn: bool = True) -> dict | None:
        """Best comb in this spectrum as {bpf_hz, harmonics, strength_db, peaks_hz}, or None."""
        self.recent.append(power[:self.size])
        db = 10.0 * np.log10(np.mean(self.recent, axis=0) + 1e-20)
        reference = np.maximum(running_median(db, self.floor_half), QUIET_DB)
        if self.background is not None:
            reference = np.maximum(reference, self.background)
        if learn and self.alpha > 0:
            self.frames += 1
            if self.background is None:
                self.background = db.copy()
            else:
                rate = 1.0 / self.frames if self.frames <= self.quick_frames else self.alpha
                self.background += max(self.alpha, rate) * (db - self.background)
        prominence = np.clip(db - reference, 0.0, MAX_PROMINENCE_DB)

        heights = np.maximum.reduceat(prominence, self.bounds)[0::2].reshape(self.lo.shape)
        present = heights >= self.cfg["peak_db"]
        count = present.sum(axis=1)
        score = heights.mean(axis=1)
        valid = present[:, 0] & (count >= self.cfg["min_harmonics"])
        if not valid.any():
            return None
        best = int(np.argmax(np.where(valid, score, -1.0)))
        # A comb at f also lines up with every other tooth of f/2; prefer the lowest
        # fundamental that explains the peaks about as well.
        for divisor in (3, 2):
            target = self.candidates[best] / divisor
            near = np.flatnonzero(np.abs(self.candidates - target) <= self.bin_hz)
            near = near[valid[near] & (score[near] >= OCTAVE_SCORE * score[best])]
            if near.size:
                best = int(near[np.argmax(score[near])])
                break

        peaks = []
        for order in np.flatnonzero(present[best]):
            lo, hi = self.lo[best, order], self.hi[best, order]
            index = lo + int(np.argmax(db[lo:hi]))
            peaks.append(parabolic_peak(db, index) * self.bin_hz / (order + 1))
        return {
            "bpf_hz": float(np.median(peaks)),
            "harmonics": int(count[best]),
            "strength_db": float(score[best]),
            "peaks_hz": [round(float(f * (o + 1)), 1) for o, f in zip(np.flatnonzero(present[best]), peaks)],
        }


class BpfTracker:
    """Turn per-frame combs into a stable detection: confirm over `confirm_s`, hold for `hold_s`."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.window = collections.deque(maxlen=max(2, round(cfg["confirm_s"] / cfg["hop_s"])))
        self.detected = False
        self.bpf_hz = None
        self.since = None
        self.last_seen = None
        self.last = None

    def update(self, comb: dict | None, now: float) -> str | None:
        """Feed one frame's comb; returns "detected" or "lost" when the state changes."""
        self.window.append(None if comb is None else comb["bpf_hz"])
        if comb is not None:
            self.last = comb
        if self.detected:
            if comb is not None and abs(comb["bpf_hz"] - self.bpf_hz) <= TRACK_DRIFT * self.bpf_hz:
                self.bpf_hz += 0.3 * (comb["bpf_hz"] - self.bpf_hz)
                self.last_seen = now
            elif now - self.last_seen > self.cfg["hold_s"]:
                self.detected = False
                return "lost"
            return None
        hits = [f for f in self.window if f is not None]
        needed = CONFIRM_FRACTION * self.window.maxlen
        if len(hits) < needed:
            return None
        # Propellers hold their pitch; speech and passing noises wander.
        middle = float(np.median(hits))
        if sum(abs(f - middle) <= CONFIRM_SPREAD * middle for f in hits) < needed:
            return None
        self.detected, self.bpf_hz, self.since, self.last_seen = True, middle, now, now
        return "detected"


class Analyzer:
    """Cut audio into overlapping frames; each frame gives a display column and a detector result."""

    def __init__(self, cfg: dict, rate: int):
        self.n = cfg["fft_size"]
        self.hop = max(1, round(cfg["hop_s"] * rate))
        self.window = np.hanning(self.n).astype(np.float32)
        self.gain = 2.0 / float(self.window.sum())
        self.buffer = np.zeros(self.n, np.float32)
        self.pending = np.zeros(0, np.float32)
        bin_hz = rate / self.n
        self.max_hz = min(float(cfg["display_max_hz"]), rate / 2.0)
        self.display_bins = int(self.max_hz / bin_hz) + 1
        self.row_starts = np.floor(np.linspace(0, self.display_bins, ROWS, endpoint=False)).astype(int)
        self.detector = BpfDetector(cfg, rate, self.n)
        self.tracker = BpfTracker(cfg)
        self.background_s = cfg["background_s"]

    def feed(self, samples: np.ndarray):
        """Yield (column bytes, comb, rms dBFS) for every full hop in `samples`."""
        self.pending = np.concatenate((self.pending, samples))
        while len(self.pending) >= self.hop:
            block, self.pending = self.pending[:self.hop], self.pending[self.hop:]
            self.buffer = np.concatenate((self.buffer[self.hop:], block))[-self.n:]
            power = (np.abs(np.fft.rfft(self.buffer * self.window)) * self.gain) ** 2
            db = 10.0 * np.log10(power[:self.display_bins] + 1e-20)
            column = np.maximum.reduceat(db, self.row_starts)
            scaled = np.clip((column - DB_MIN) * (255.0 / (DB_MAX - DB_MIN)), 0, 255).astype(np.uint8)
            # Stop learning a flagged comb into the background, but not forever: a
            # source that never changes (a fan, mains hum) fades out after background_s.
            tracker = self.tracker
            learn = not tracker.detected or time.monotonic() - tracker.since > self.background_s
            comb = self.detector.analyse(power, learn)
            rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))
            yield scaled.tobytes(), comb, 20.0 * math.log10(rms + 1e-12)


class SpectrumHub:
    """Recent spectrogram columns for /spectrum/events, with a way to wait for new ones."""

    def __init__(self, cfg: dict):
        self._cond = threading.Condition()
        self._columns = collections.deque(maxlen=max(1, round(cfg["history_s"] / cfg["hop_s"])))
        self._seq = 0
        self._meta = None

    def reset(self, meta: dict) -> None:
        with self._cond:
            self._meta = dict(meta, id=(self._meta or {}).get("id", 0) + 1, capacity=self._columns.maxlen)
            self._columns.clear()
            self._cond.notify_all()

    def publish(self, t: float, column: bytes, mark: int, bpf_hz) -> None:
        with self._cond:
            self._seq += 1
            self._columns.append([self._seq, round(t, 2), base64.b64encode(column).decode("ascii"), mark,
                                  None if bpf_hz is None else round(bpf_hz, 1)])
            self._cond.notify_all()

    def wait(self, after_seq: int, meta_id, timeout: float):
        """(meta, columns newer than after_seq); all columns when the meta changed since meta_id."""
        def ready():
            return self._meta is not None and (self._meta["id"] != meta_id or
                                               (self._columns and self._columns[-1][0] > after_seq))
        with self._cond:
            self._cond.wait_for(ready, timeout)
            if self._meta is None:
                return None, []
            if self._meta["id"] != meta_id:
                return dict(self._meta), list(self._columns)
            return self._meta, [c for c in self._columns if c[0] > after_seq]


class AcousticModule:
    def __init__(self, cfg: dict, state, hub: SpectrumHub):
        self.cfg = cfg
        self.state = state
        self.hub = hub
        self.sd = None
        self.stream = None
        self.device = None
        self.rate = None
        self.analyzer = None
        self.audio = queue.Queue(maxsize=200)
        self.dropped = 0
        self.last_attempt = -math.inf
        self.last_audio = 0.0
        self.last_status = -math.inf
        self.level = None
        self.error = None

    def run(self, stop: threading.Event) -> None:
        try:
            while not stop.is_set():
                now = time.monotonic()
                if self.stream is None:
                    if now - self.last_attempt >= REOPEN_S:
                        self._open(now)
                    if self.stream is None:
                        self._report(force=True)
                        stop.wait(0.5)
                    continue
                try:
                    block = self.audio.get(timeout=0.5)
                except queue.Empty:
                    if now - self.last_audio > STALL_S or not self.stream.active:
                        self._fail("{} stopped sending audio (unplugged?)".format(self.device))
                    self._report()
                    continue
                self.last_audio = time.monotonic()
                for column, comb, level in self.analyzer.feed(block):
                    self._frame(column, comb, level)
                self._report()
        finally:
            self._close()

    def _callback(self, indata, frames, time_info, status) -> None:
        try:
            self.audio.put_nowait(indata[:, 0].copy())
        except queue.Full:
            self.dropped += 1

    def _open(self, now: float) -> None:
        self.last_attempt = now
        try:
            if self.sd is None:
                try:
                    import sounddevice
                except (ImportError, OSError) as exc:
                    raise RuntimeError("sounddevice/PortAudio missing (sudo apt install libportaudio2; "
                                       "pip install sounddevice): {}".format(exc)) from exc
                self.sd = sounddevice
            else:
                # PortAudio only lists devices present when it started; rescan for hot-plugged mics.
                self.sd._terminate()
                self.sd._initialize()
            found = find_microphone(self.sd, self.cfg["device"])
            if found is None:
                raise RuntimeError("No USB microphone found" if self.cfg["device"] == "auto"
                                   else "No input device matches {!r}".format(self.cfg["device"]))
            self.stream, self.rate = self._start(*found)
        except Exception as exc:
            if str(exc) != self.error:
                log.warning("Measurement microphone unavailable: %s", exc)
            self.error = str(exc)
            self.stream = None
            return
        self.device = found[1].strip()
        self.error = None
        self.last_audio = time.monotonic()
        self.analyzer = Analyzer(self.cfg, self.rate)
        self.hub.reset({
            "device": self.device,
            "rate": self.rate,
            "fft_size": self.cfg["fft_size"],
            "hop_s": self.analyzer.hop / self.rate,
            "max_hz": self.analyzer.max_hz,
            "rows": ROWS,
            "db_min": DB_MIN,
            "db_max": DB_MAX,
            "bpf_min_hz": self.cfg["bpf_min_hz"],
            "bpf_max_hz": self.cfg["bpf_max_hz"],
            "harmonics": self.cfg["harmonics"],
        })
        log.info("Measurement microphone %r at %d Hz; spectrogram 0-%.0f Hz, BPF search %g-%g Hz",
                 self.device, self.rate, self.analyzer.max_hz, self.cfg["bpf_min_hz"], self.cfg["bpf_max_hz"])

    def _start(self, index: int, name: str):
        info = self.sd.query_devices(index)
        last = None
        native = int(info["default_samplerate"])
        for rate in dict.fromkeys((self.cfg["sample_rate"] or native, native, 48000, 44100)):
            for channels in dict.fromkeys((1, int(info["max_input_channels"]))):
                try:
                    self.sd.check_input_settings(device=index, samplerate=rate, channels=channels, dtype="float32")
                    stream = self.sd.InputStream(device=index, samplerate=rate, channels=channels,
                                                 dtype="float32", callback=self._callback)
                    stream.start()
                    return stream, int(rate)
                except Exception as exc:
                    last = exc
        raise RuntimeError("cannot open {}: {}".format(name.strip(), last))

    def _frame(self, column: bytes, comb, level: float) -> None:
        tracker = self.analyzer.tracker
        change = tracker.update(comb, time.monotonic())
        self.level = level
        blades = self.cfg["blades"]
        if change == "detected":
            log.info("Drone BPF detected: %.1f Hz (%.0f RPM with %d blades), %d/%d harmonics",
                     tracker.bpf_hz, tracker.bpf_hz * 60 / blades, blades, comb["harmonics"], self.cfg["harmonics"])
        elif change == "lost":
            log.info("Drone BPF signature lost (was %.1f Hz)", tracker.bpf_hz)
        mark = 2 if tracker.detected else (1 if comb is not None else 0)
        shown = tracker.bpf_hz if tracker.detected else (comb or {}).get("bpf_hz")
        self.hub.publish(time.time(), column, mark, shown)
        if change:
            self._report(force=True, notify=True)

    def _report(self, force: bool = False, notify: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last_status < STATUS_S:
            return
        self.last_status = now
        tracker = self.analyzer.tracker if self.analyzer is not None and self.stream is not None else None
        detected = bool(tracker and tracker.detected)
        last = tracker.last if tracker else None
        bpf = tracker.bpf_hz if detected else None
        level = None if self.level is None or self.stream is None else round(self.level, 1)
        offset = self.cfg["spl_offset_db"]
        self.state.set_module("acoustic", {
            "enabled": True,
            "ready": self.stream is not None,
            "device": self.device if self.stream is not None else None,
            "sample_rate": self.rate if self.stream is not None else None,
            "level_dbfs": level,
            "level_db_spl": None if level is None or not offset else round(level + offset, 1),
            "detected": detected,
            "bpf_hz": None if bpf is None else round(bpf, 1),
            "rpm": None if bpf is None else round(bpf * 60 / self.cfg["blades"]),
            "blades": self.cfg["blades"],
            "harmonics": last["harmonics"] if detected and last else None,
            "harmonics_searched": self.cfg["harmonics"],
            "strength_db": round(last["strength_db"], 1) if detected and last else None,
            "peaks_hz": last["peaks_hz"] if detected and last else [],
            "detected_for_s": round(now - tracker.since, 1) if detected else None,
            "search_hz": [self.cfg["bpf_min_hz"], self.cfg["bpf_max_hz"]],
            "dropped_blocks": self.dropped,
            "error": self.error,
        }, notify=notify)

    def _fail(self, message: str) -> None:
        log.warning("Measurement microphone: %s", message)
        if self.analyzer is not None and self.analyzer.tracker.detected:
            log.info("Drone BPF signature lost (was %.1f Hz)", self.analyzer.tracker.bpf_hz)
        self.error = message
        self._close()
        self._report(force=True, notify=True)

    def _close(self) -> None:
        if self.stream is not None:
            try:
                self.stream.abort()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        self.level = None
        while not self.audio.empty():
            self.audio.get_nowait()
