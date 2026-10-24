"""One upstream video connection per camera node, shared by every viewer and by the analysers.

An ESP32-CAM can serve only one MJPEG client and its web server is single-threaded, so the backend holds
the only connection, keeps the latest JPEG, and re-streams it to any number of authorised dashboard
viewers. The relay starts when someone needs frames and stops after 30 s without consumers.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field

import httpx

from .analysis import CameraUnavailable, camera_host
from .config import Settings
from .db import connect, row

log = logging.getLogger("skymesh.camrelay")

IDLE_STOP_S = 30
_LEN = re.compile(rb"Content-Length:\s*(\d+)", re.I)


def parse_mjpeg(buf: bytearray) -> list[bytes]:
    """Pull complete JPEG parts out of `buf` (modified in place). Parts carry a Content-Length header."""
    frames = []
    while True:
        m = _LEN.search(buf)
        if not m:
            if len(buf) > 65536:  # garbage without headers: keep memory bounded
                del buf[:-1024]
            return frames
        head_end = buf.find(b"\r\n\r\n", m.end())
        if head_end < 0:
            return frames
        n = int(m.group(1))
        start = head_end + 4
        if len(buf) < start + n:
            return frames
        frames.append(bytes(buf[start:start + n]))
        del buf[:start + n]


@dataclass
class CameraRelay:
    cfg: Settings
    camera_id: str
    frame: bytes | None = None
    frame_at: float = 0.0
    seq: int = 0
    fps: float = 0.0
    error: str | None = None
    last_used: float = field(default_factory=time.monotonic)
    _thread: threading.Thread | None = None
    _cond: threading.Condition = field(default_factory=threading.Condition)

    def touch(self) -> None:
        """A consumer wants frames: keep (or start) the upstream connection."""
        self.last_used = time.monotonic()
        if not self._thread or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name=f"relay-{self.camera_id}", daemon=True)
            self._thread.start()

    def wait_frame(self, after_seq: int, timeout: float) -> tuple[int, bytes | None]:
        with self._cond:
            self._cond.wait_for(lambda: self.seq != after_seq, timeout)
            return self.seq, self.frame

    def fresh_frame(self, max_age: float = 2.0) -> bytes | None:
        return self.frame if self.frame and time.monotonic() - self.frame_at < max_age else None

    def _device(self) -> dict | None:
        conn = connect(self.cfg.db_path)
        try:
            return row(conn.execute("SELECT * FROM devices WHERE id = ? AND type = 'camera_node' AND revoked_at IS NULL",
                                    (self.camera_id,)).fetchone())
        finally:
            conn.close()

    def _run(self) -> None:
        while time.monotonic() - self.last_used < IDLE_STOP_S:
            dev = self._device()
            try:
                if not dev:
                    raise CameraUnavailable("camera node not found or revoked")
                url = f"http://{camera_host(dev)}/stream"
                with httpx.stream("GET", url, headers={"X-SkyMesh-Key": dev["camera_key"]},
                                  timeout=httpx.Timeout(5.0, read=10.0)) as r:
                    if r.status_code == 409:
                        raise CameraUnavailable("the camera's video is in use by another program")
                    if r.status_code != 200:
                        raise CameraUnavailable(f"camera returned HTTP {r.status_code}")
                    self.error = None
                    buf = bytearray()
                    window_start, window_frames = time.monotonic(), 0
                    for chunk in r.iter_bytes():
                        buf += chunk
                        for jpg in parse_mjpeg(buf):
                            with self._cond:
                                self.frame, self.frame_at = jpg, time.monotonic()
                                self.seq += 1
                                self._cond.notify_all()
                            window_frames += 1
                        now = time.monotonic()
                        if now - window_start >= 2:
                            self.fps = round(window_frames / (now - window_start), 1)
                            window_start, window_frames = now, 0
                        if now - self.last_used > IDLE_STOP_S:
                            break
            except CameraUnavailable as e:
                self.error = str(e)
            except httpx.HTTPError as e:
                self.error = f"camera not reachable ({type(e).__name__})"
            except Exception as e:  # keep the relay thread alive and say what happened
                log.exception("relay %s failed", self.camera_id)
                self.error = f"relay error: {e}"
            self.fps = 0.0
            if time.monotonic() - self.last_used < IDLE_STOP_S:
                time.sleep(3)  # back off before reconnecting


class RelayManager:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self._relays: dict[str, CameraRelay] = {}
        self._lock = threading.Lock()

    def get(self, camera_id: str) -> CameraRelay:
        with self._lock:
            r = self._relays.get(camera_id)
            if r is None:
                r = self._relays[camera_id] = CameraRelay(self.cfg, camera_id)
            return r

    def status(self) -> dict[str, dict]:
        return {k: {"fps": r.fps, "error": r.error, "running": bool(r._thread and r._thread.is_alive()),
                    "frame_age_s": round(time.monotonic() - r.frame_at, 1) if r.frame else None}
                for k, r in self._relays.items()}
