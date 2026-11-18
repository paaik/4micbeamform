
from __future__ import annotations

import collections
import json
import logging
import mimetypes
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

ENV_FILE = Path(__file__).resolve().parent.parent / "cloud.env"
HTTP_TIMEOUT_S = 15.0
MAX_BACKOFF_S = 60.0
MAX_BATCH = 100


class CloudError(Exception):
    pass


def load_key(cloud: dict) -> str:
    """The API key from the environment, else from cloud.env (KEY=value lines, # comments)."""
    name = cloud["key_env"]
    if os.environ.get(name):
        return os.environ[name].strip()
    try:
        lines = ENV_FILE.read_text().splitlines()
    except OSError:
        return ""
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == name:
            return value.strip().strip('"').strip("'")
    return ""


def iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def rounded(value, digits=1):
    return None if value is None else round(float(value), digits)


class SupabaseClient:
    """The two REST calls this project needs: insert rows, upload a file."""

    def __init__(self, cloud: dict, key: str):
        self.base = cloud["url"].rstrip("/")
        self.table = cloud["table"]
        self.bucket = cloud["bucket"]
        self.key = key

    def _request(self, method: str, url: str, body: bytes | None, content_type: str | None, **extra) -> bytes:
        headers = {"apikey": self.key}
        if self.key.startswith("eyJ"):  # legacy anon key (a JWT); new sb_publishable_ keys go in apikey only
            headers["Authorization"] = "Bearer " + self.key
        if content_type:
            headers["Content-Type"] = content_type
        headers.update(extra)
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:200]
            raise CloudError("HTTP {} from {}: {}".format(exc.code, url.split("?")[0], detail)) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise CloudError("cannot reach {} ({})".format(self.base, getattr(exc, "reason", exc))) from exc

    def insert(self, rows: list[dict]) -> None:
        self._request("POST", "{}/rest/v1/{}".format(self.base, self.table),
                      json.dumps(rows).encode("utf-8"), "application/json", Prefer="return=minimal")

    def upload(self, path: Path, name: str) -> str:
        """Store a file in the bucket and return its public link."""
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._request("POST", "{}/storage/v1/object/{}/{}".format(self.base, self.bucket, name),
                      path.read_bytes(), content_type)
        return "{}/storage/v1/object/public/{}/{}".format(self.base, self.bucket, name)

    def probe(self) -> None:
        """Read at most one row: proves the URL, key and table name without writing anything."""
        self._request("GET", "{}/rest/v1/{}?select=id&limit=1".format(self.base, self.table), None, None)


def check_connection(cloud: dict) -> tuple[bool, str]:
    """For --check."""
    if not cloud["url"]:
        return False, "cloud.url is empty in doa_tracker.toml"
    key = load_key(cloud)
    if not key:
        return False, "no API key: set {} or put it in {}".format(cloud["key_env"], ENV_FILE)
    try:
        SupabaseClient(cloud, key).probe()
    except CloudError as exc:
        return False, str(exc)
    return True, "table {} reachable at {}".format(cloud["table"], cloud["url"])


class CloudUploader:
    def __init__(self, cloud: dict, state):
        self.cfg = cloud
        self.state = state
        self.device_id = cloud["device_id"]
        self.key = load_key(cloud)
        self.client = SupabaseClient(cloud, self.key)
        self.queue = collections.deque()
        self.lock = threading.Lock()
        self.last_sample = -float("inf")
        self.last_flush = time.monotonic()
        self.backoff = 0.0
        self.retry_at = 0.0
        self.sent = 0
        self.photos = 0
        self.dropped = 0
        self.error = None

    def submit(self, drone_type: str, azimuth_deg: float | None, elevation_deg: float | None = None,
               photo_path: str | Path | None = None, t: float | None = None) -> None:
        """Queue one detection. Safe to call from any thread; returns at once."""
        row = {
            "recorded_at": iso_utc(time.time() if t is None else t),
            "drone_type": drone_type or "unknown",
            "azimuth_deg": rounded(azimuth_deg),
            "elevation_deg": rounded(elevation_deg),
            "photo_url": None,
            "device_id": self.device_id,
        }
        self._queue({"row": row, "photo": None if photo_path is None else Path(photo_path)})

    def run(self, stop: threading.Event) -> None:
        if not self.cfg["url"] or not self.key:
            self.error = "set cloud.url, and {} in the environment or cloud.env".format(self.cfg["key_env"])
            log.warning("Cloud upload off: %s", self.error)
            self._report()
            return
        log.info("Uploading detections to %s (table %s, bucket %s) as %s",
                 self.cfg["url"], self.cfg["table"], self.cfg["bucket"], self.device_id)
        seq = -1
        try:
            while not stop.is_set():
                snapshot = self.state.wait_newer(seq, 1.0)
                if snapshot["seq"] != seq:
                    seq = snapshot["seq"]
                    if self.cfg["auto_rows"]:
                        self._sample(snapshot)
                now = time.monotonic()
                if self.queue and now - self.last_flush >= self.cfg["upload_interval_s"] and now >= self.retry_at:
                    self.flush()
                self._report()
        finally:
            if self.queue:
                self.flush()  # one last try so a clean shutdown does not lose rows
            self._report()

    def flush(self) -> bool:
        """Upload pending photos, then send the queued rows in one batch. True if the queue emptied."""
        self.last_flush = time.monotonic()
        with self.lock:
            batch = list(self.queue)[:MAX_BATCH]
        try:
            for item in batch:
                self._upload_photo(item)
            self.client.insert([item["row"] for item in batch])
        except CloudError as exc:
            self._failed(str(exc))
            return False
        with self.lock:
            for _ in batch:
                self.queue.popleft()
        self.sent += len(batch)
        self.backoff = 0.0
        if self.error:
            log.info("Cloud upload working again")
        self.error = None
        return not self.queue

    def _sample(self, snapshot: dict) -> None:
        if not snapshot.get("ok") or not snapshot.get("tracking_live"):
            return
        now = time.monotonic()
        if now - self.last_sample < self.cfg["sample_interval_s"]:
            return
        self.last_sample = now
        self.submit("unknown", snapshot.get("tracked_deg"), t=snapshot["t"])

    def _queue(self, item: dict) -> None:
        with self.lock:
            if len(self.queue) >= self.cfg["max_queue"]:
                self.queue.popleft()
                self.dropped += 1
            self.queue.append(item)

    def _upload_photo(self, item: dict) -> None:
        """Upload once; a retried batch reuses the link it already has."""
        path = item["photo"]
        if path is None or item["row"]["photo_url"]:
            return
        if not path.is_file():
            log.warning("Photo %s is missing; sending the row without it", path)
            item["photo"] = None
            return
        stamp = datetime.now(timezone.utc)
        name = "{}/{}/{}_{}{}".format(self.device_id, stamp.strftime("%Y-%m-%d"), stamp.strftime("%H%M%S"),
                                      uuid.uuid4().hex[:8], path.suffix.lower() or ".jpg")
        item["row"]["photo_url"] = self.client.upload(path, name)
        self.photos += 1

    def _failed(self, message: str) -> None:
        if message != self.error:
            log.warning("Cloud upload failed (%d rows kept for retry): %s", len(self.queue), message)
        self.error = message
        self.backoff = min(MAX_BACKOFF_S, max(2.0, self.backoff * 2))
        self.retry_at = time.monotonic() + self.backoff

    def _report(self) -> None:
        self.state.set_module("cloud", {
            "enabled": True,
            "sent": self.sent,
            "photos": self.photos,
            "queued": len(self.queue),
            "dropped": self.dropped,
            "error": self.error,
        })

    # Here is the fake data made up for test the uploading of data to the Supabase.
    # NO need to remove it as long as it only get called when type in < python -m doa_tracker --cloud-test> in the Terminal


def send_test(cloud: dict) -> int:
    """`--cloud-test`: upload one fake detection with a generated picture, then exit."""
    ok, detail = check_connection(cloud)
    if not ok:
        log.error("Cloud test failed: %s", detail)
        return 1

    class NoState:
        def set_module(self, name, status):
            pass

    uploader = CloudUploader(cloud, NoState())
    with tempfile.TemporaryDirectory() as folder:
        photo = None
        try:
            from PIL import Image, ImageDraw

            photo = Path(folder) / "cloud_test.png"
            image = Image.new("RGB", (320, 240), "#87ceeb")
            ImageDraw.Draw(image).text((112, 112), "CLOUD TEST", fill="black")
            image.save(photo)
        except ImportError:
            log.warning("Pillow is not installed, so the test row has no photo")
        uploader.submit("test", azimuth_deg=135.0, elevation_deg=30.0, photo_path=photo)
        if not uploader.flush():
            log.error("Cloud test failed: %s", uploader.error)
            return 1
    log.info("Test detection sent%s. Check Table Editor -> %s and Storage -> %s.",
             " with a photo" if uploader.photos else "", cloud["table"], cloud["bucket"])
    return 0
