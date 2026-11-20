"""Detect drones in the Pi camera stream on a PC and send the boxes back to the Pi.

    python -m drone_detector --pi http://respeaker.local:8765 --weights drone.pt
    python -m drone_detector --pi http://192.168.1.50:8765 --weights drone.pt --show

The Pi serves its camera at /camera.mjpg with every frame numbered. This reads
the stream, always running the model on the newest frame (older ones are
dropped so latency never builds up), and posts the boxes with that frame number
to /api/detections. The Pi turns them into an elevation for the tilt servo and
shows them on the dashboard. Results are posted even when nothing is found, so
the Pi can tell the detector is alive.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("drone_detector")


class StreamReader(threading.Thread):
    """Keep only the newest frame from /camera.mjpg, reconnecting whenever the stream drops."""

    def __init__(self, url: str):
        super().__init__(name="stream", daemon=True)
        self.url = url
        self.stop = threading.Event()
        self._cond = threading.Condition()
        self._frame = None
        self.received = 0

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                with urllib.request.urlopen(self.url, timeout=10) as stream:
                    log.info("Receiving %s", self.url)
                    self._read(stream)
            except (OSError, ValueError) as exc:
                log.warning("Camera stream unavailable (%s); retrying in 2 s", exc)
            self.stop.wait(2.0)

    def _read(self, stream) -> None:
        while not self.stop.is_set():
            line = stream.readline()
            if not line:
                raise ValueError("stream ended")
            if not line.startswith(b"--"):
                continue
            headers = {}
            while True:
                line = stream.readline()
                if not line:
                    raise ValueError("stream ended")
                line = line.strip()
                if not line:
                    break
                key, _, value = line.decode("latin-1").partition(":")
                headers[key.strip().lower()] = value.strip()
            jpeg = stream.read(int(headers["content-length"]))
            frame = (int(headers.get("x-frame-seq", -1)), jpeg)
            with self._cond:
                self._frame = frame
                self.received += 1
                self._cond.notify_all()

    def newest(self, after: int, timeout: float):
        """(seq, jpeg) of the newest frame if it is newer than `after`, else None after `timeout`."""
        with self._cond:
            if not self._cond.wait_for(lambda: self._frame is not None and self._frame[0] != after, timeout):
                return None
            return self._frame


def load_detector(args):
    """Return detect(image) -> list of {label, confidence, box: [x1, y1, x2, y2] normalised}."""
    from ultralytics import YOLO

    model = YOLO(args.weights)
    names = model.names
    class_ids = None
    if args.classes:
        lookup = {name.lower(): index for index, name in names.items()}
        unknown = [c for c in args.classes if c.lower() not in lookup]
        if unknown:
            raise SystemExit("Model has no class {}; it has: {}".format(", ".join(unknown), ", ".join(names.values())))
        class_ids = [lookup[c.lower()] for c in args.classes]
    log.info("Loaded %s (classes: %s)", args.weights,
             ", ".join(names[i] for i in (class_ids or names.keys())))

    def detect(image):
        result = model.predict(image, conf=args.conf, imgsz=args.imgsz, device=args.device,
                               classes=class_ids, verbose=False)[0]
        boxes = result.boxes
        return [
            {"label": names[int(c)], "confidence": float(p), "box": [float(v) for v in xyxy]}
            for xyxy, p, c in zip(boxes.xyxyn.tolist(), boxes.conf.tolist(), boxes.cls.tolist())
        ]

    return detect


def draw(image, detections):
    import cv2

    height, width = image.shape[:2]
    for index, det in enumerate(detections):
        x1, y1, x2, y2 = det["box"]
        color = (140, 214, 61) if index == 0 else (32, 176, 255)
        p1, p2 = (int(x1 * width), int(y1 * height)), (int(x2 * width), int(y2 * height))
        cv2.rectangle(image, p1, p2, color, 2)
        cv2.putText(image, "{} {:.0%}".format(det["label"], det["confidence"]), (p1[0], max(12, p1[1] - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
    return image


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="drone_detector", description=__doc__.split("\n\n")[0])
    parser.add_argument("--pi", default="http://respeaker.local:8765", help="tracker dashboard URL on the Pi")
    parser.add_argument("--weights", required=True, help="YOLO weights trained to detect drones (.pt)")
    parser.add_argument("--classes", nargs="+", help="only report these class names (default: all)")
    parser.add_argument("--conf", type=float, default=0.25, help="minimum confidence to report (default 0.25)")
    parser.add_argument("--imgsz", type=int, default=640, help="model input size (default 640)")
    parser.add_argument("--device", default=None, help="'cpu', '0' for the first CUDA GPU, ... (default: auto)")
    parser.add_argument("--show", action="store_true", help="also show the annotated stream in a window")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def main(argv=None, detector_factory=load_detector) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    import cv2
    import numpy as np
    import requests

    base = args.pi.rstrip("/")
    detect = detector_factory(args)
    model_name = Path(args.weights).name
    reader = StreamReader(base + "/camera.mjpg")
    reader.start()
    session = requests.Session()

    seq = None
    window_start, processed, found, busy_ms = time.monotonic(), 0, 0, 0.0
    post_error = None
    try:
        while True:
            frame = reader.newest(seq, 1.0)
            if frame is None:
                continue
            seq, jpeg = frame
            image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                continue
            started = time.perf_counter()
            detections = detect(image)
            inference_ms = (time.perf_counter() - started) * 1000.0
            payload = {"seq": seq, "model": model_name, "inference_ms": round(inference_ms, 1),
                       "width": image.shape[1], "height": image.shape[0], "detections": detections}
            try:
                reply = session.post(base + "/api/detections", json=payload, timeout=2.0)
                if reply.status_code != 200:
                    raise requests.RequestException("HTTP {}: {}".format(reply.status_code, reply.text[:120]))
                post_error = None
            except requests.RequestException as exc:
                if str(exc) != post_error:
                    log.warning("Could not send detections to the Pi: %s", exc)
                post_error = str(exc)

            processed += 1
            found += bool(detections)
            busy_ms += inference_ms
            if args.show:
                cv2.imshow("drone_detector", draw(image, detections))
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    break
            elapsed = time.monotonic() - window_start
            if elapsed >= 10.0:
                log.info("%.1f fps processed of %.1f fps received, %.0f ms per frame, drone in %d of %d frames",
                         processed / elapsed, reader.received / elapsed, busy_ms / max(1, processed), found, processed)
                window_start, processed, found, busy_ms = time.monotonic(), 0, 0, 0.0
                reader.received = 0
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop.set()
        if args.show:
            cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    sys.exit(main())
