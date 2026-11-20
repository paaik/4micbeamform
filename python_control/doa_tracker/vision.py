"""Camera frames out, detections back in.

The camera module publishes JPEG frames tagged with the tilt elevation and pan
azimuth at capture time. The off-Pi detector reads them from /camera.mjpg and
posts its boxes to /api/detections, quoting the frame number. Each box is then
turned into an elevation and azimuth using where the camera was pointing when
that frame was taken, so detector latency doesn't skew the result.
"""

from __future__ import annotations

import collections
import math
import threading
import time

FRAME_HISTORY = 120
DETECTOR_TIMEOUT_S = 3.0
MAX_BOXES = 20


def pixel_angle(offset: float, fov_deg: float) -> float:
    """Angle off the optical axis for a normalised offset from the image centre (-0.5..0.5)."""
    return math.degrees(math.atan(2.0 * offset * math.tan(math.radians(fov_deg) / 2.0)))


class FrameHub:
    def __init__(self, camera: dict, state):
        self.hfov = camera["hfov_deg"]
        self.vfov = camera["vfov_deg"]
        self.state = state
        self._cond = threading.Condition()
        self._lock = threading.Lock()
        self._seq = 0
        self._jpeg = None
        self._meta = collections.OrderedDict()
        self.pose = {"elevation_deg": None, "moving": False}
        self.result = None
        self._posts = collections.deque(maxlen=30)

    # Frames -----------------------------------------------------------

    def publish_frame(self, jpeg: bytes) -> int:
        servo = self.state.module("servo") or {}
        facing = servo.get("pointing_deg") if servo.get("backend") and servo.get("end") != "rear" else None
        meta = {
            "t": time.time(),
            "elevation_deg": self.pose.get("elevation_deg"),
            "tilt_moving": self.pose.get("moving", False),
            "azimuth_deg": facing,
        }
        with self._cond:
            self._seq += 1
            self._jpeg = jpeg
            self._meta[self._seq] = meta
            while len(self._meta) > FRAME_HISTORY:
                self._meta.popitem(last=False)
            self._cond.notify_all()
            return self._seq

    def wait_frame(self, after: int, timeout: float):
        """Next frame newer than `after` as (seq, jpeg, meta), or None on timeout."""
        with self._cond:
            if not self._cond.wait_for(lambda: self._jpeg is not None and self._seq != after, timeout):
                return None
            return self._seq, self._jpeg, self._meta[self._seq]

    # Detections -------------------------------------------------------

    def post_detections(self, payload: dict, client: str) -> dict:
        """Store one detector result; raises ValueError if the payload is malformed."""
        seq = int(payload["seq"])
        boxes = payload.get("detections") or []
        if not isinstance(boxes, list):
            raise ValueError("detections must be a list")
        with self._cond:
            meta = self._meta.get(seq)
        if meta is None:
            raise ValueError("frame {} is too old or unknown".format(seq))

        detections = []
        for box in boxes[:MAX_BOXES]:
            x1, y1, x2, y2 = (min(1.0, max(0.0, float(v))) for v in box["box"])
            cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            elevation = azimuth = None
            if meta["elevation_deg"] is not None:
                elevation = meta["elevation_deg"] + pixel_angle(0.5 - cy, self.vfov)
            if meta["azimuth_deg"] is not None:
                azimuth = (meta["azimuth_deg"] + pixel_angle(0.5 - cx, self.hfov)) % 360.0
            detections.append({
                "label": str(box.get("label", "object"))[:40],
                "confidence": round(float(box.get("confidence", 0.0)), 3),
                "box": [round(v, 4) for v in (x1, y1, x2, y2)],
                "elevation_deg": None if elevation is None else round(elevation, 2),
                "azimuth_deg": None if azimuth is None else round(azimuth, 2),
            })
        detections.sort(key=lambda d: d["confidence"], reverse=True)

        now = time.monotonic()
        result = {
            "seq": seq,
            "received": now,
            "frame_age_s": round(time.time() - meta["t"], 3),
            "frame_elevation_deg": meta["elevation_deg"],
            "tilt_moving": meta["tilt_moving"],
            "detections": detections,
            "model": str(payload.get("model", ""))[:80],
            "inference_ms": payload.get("inference_ms"),
            "client": client,
        }
        with self._lock:
            self.result = result
            self._posts.append(now)
        self.state.set_module("vision", self.status(), notify=True)
        return {"ok": True, "detections": len(detections)}

    def latest_result(self):
        with self._lock:
            return self.result

    def status(self) -> dict:
        now = time.monotonic()
        with self._lock:
            result = self.result
            posts = [t for t in self._posts if now - t <= 2.0]
        online = result is not None and now - result["received"] <= DETECTOR_TIMEOUT_S
        status = {"detector_online": online, "detector_fps": round(len(posts) / 2.0, 1) if online else 0.0}
        if result is not None:
            status.update({k: v for k, v in result.items() if k != "received"})
            status["age_s"] = round(now - result["received"], 2)
            if not online:
                status["detections"] = []
        return status
