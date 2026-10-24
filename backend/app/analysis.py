"""Camera frame analysis: video files and live ESP32-CAM snapshots -> anonymous person observations."""
from __future__ import annotations

import ipaddress
import json
import logging
import threading
import time
from datetime import timedelta
from pathlib import Path

import httpx
import numpy as np

from . import positioning as pos
from .config import Settings
from .db import Tx, connect, get_setting, iso, new_id, now_iso, parse_iso, row, utcnow
from .realtime import Hub, perm
from .vision import Detector, IouTracker, decode_jpeg, default_detector, iter_video_frames

log = logging.getLogger("skymesh.analysis")


class CameraUnavailable(Exception):
    pass


def camera_host(device: dict) -> str:
    """The node's address for outgoing requests. Only private LAN addresses are ever contacted."""
    if not device.get("camera_key"):
        raise CameraUnavailable("camera node has no access key (revoked or not a camera)")
    ip = device.get("last_ip")
    if not ip:
        raise CameraUnavailable("camera node has never sent a heartbeat")
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        raise CameraUnavailable("invalid camera address")
    if not (addr.is_private or addr.is_loopback) or addr.is_unspecified:
        raise CameraUnavailable("camera address is not on the local network")
    return f"[{ip}]" if addr.version == 6 else ip


def fetch_snapshot(cfg: Settings, device: dict) -> bytes:
    """GET http://<node>/capture with the node's key."""
    host = camera_host(device)
    try:
        r = httpx.get(f"http://{host}/capture", headers={"X-SkyMesh-Key": device["camera_key"]},
                      timeout=cfg.camera_fetch_timeout_s)
    except httpx.HTTPError as e:
        raise CameraUnavailable(f"camera not reachable: {type(e).__name__}")
    if r.status_code != 200 or r.headers.get("content-type", "").split(";")[0] != "image/jpeg":
        raise CameraUnavailable(f"camera returned HTTP {r.status_code}")
    return r.content


def _calibration(conn, camera_id: str) -> dict | None:
    return row(conn.execute("SELECT * FROM camera_calibrations WHERE device_id = ?", (camera_id,)).fetchone(),
               ("homography",))


def _project(cal: dict | None, foot: tuple[float, float], frame_w: int, frame_h: int) -> tuple[float, float] | None:
    if not cal:
        return None
    # Calibration points are in the calibration frame's pixels; rescale if the stream size differs.
    u = foot[0] * cal["frame_w"] / frame_w
    v = foot[1] * cal["frame_h"] / frame_h
    xy = pos.apply_homography(cal["homography"], u, v)
    if xy is None or not (0 <= xy[0] <= 1 and 0 <= xy[1] <= 1):
        return None
    return xy


class SessionWriter:
    """Writes the detections of one analysis session (one video file, or one live run of a camera)."""

    def __init__(self, cfg: Settings, hub: Hub | None, camera_id: str, source: str, source_name: str | None,
                 detector: Detector):
        self.cfg, self.hub, self.camera_id, self.source = cfg, hub, camera_id, source
        self.detector = detector
        self.tracker = IouTracker()
        self.session_id = new_id("ana")
        self.frames = self.detections = self.projected = 0
        self.lock = threading.Lock()
        conn = connect(cfg.db_path)
        try:
            with Tx(conn):
                conn.execute("INSERT INTO analysis_sessions (id, camera_device_id, source, source_name, started_at, "
                             "detector) VALUES (?,?,?,?,?,?)",
                             (self.session_id, camera_id, source, source_name, now_iso(), detector.name))
        finally:
            conn.close()

    def process(self, conn, frame: np.ndarray, frame_index: int, observed_at: str) -> list[dict]:
        h, w = frame.shape[:2]
        dets = self.detector.detect(frame)
        cal = _calibration(conn, self.camera_id)
        out = []
        with self.lock:
            pairs = self.tracker.update(dets, frame_index)
            self.frames += 1
            self.detections += len(dets)
        for track, d in pairs:
            xy = _project(cal, d.foot, w, h)
            if xy:
                self.projected += 1
            conn.execute(
                "INSERT INTO camera_observations (camera_device_id, session_id, track_label, observed_at, frame_index, "
                "bbox, confidence, frame_w, frame_h, floor_id, x, y, projected, source, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.camera_id, self.session_id, track.label, observed_at, frame_index,
                 json.dumps([round(d.x), round(d.y), round(d.w), round(d.h)]), d.confidence, w, h,
                 cal["floor_id"] if (cal and xy) else None, xy[0] if xy else None, xy[1] if xy else None,
                 int(bool(xy)), self.source, now_iso()))
            out.append({"track_label": track.label, "confidence": d.confidence, "projected": bool(xy),
                        "x": xy[0] if xy else None, "y": xy[1] if xy else None,
                        "floor_id": cal["floor_id"] if (cal and xy) else None})
        return out

    def finish(self, conn, note: str | None = None) -> dict:
        tracks = self.tracker.next_number - 1
        conn.execute("UPDATE analysis_sessions SET finished_at = ?, frames_processed = ?, detections = ?, tracks = ?, "
                     "projected = ?, note = ? WHERE id = ?",
                     (now_iso(), self.frames, self.detections, tracks, self.projected, note, self.session_id))
        return row(conn.execute("SELECT * FROM analysis_sessions WHERE id = ?", (self.session_id,)).fetchone())


def analyze_video(cfg: Settings, hub: Hub | None, camera_id: str, path: Path, sample_fps: float = 2.0,
                  detector: Detector | None = None) -> dict:
    """Analyse a recorded video. Timestamps are offsets from the analysis start, flagged as video_file."""
    detector = detector or default_detector(cfg.data_dir)
    w = SessionWriter(cfg, hub, camera_id, "video_file", path.name, detector)
    start = utcnow()
    conn = connect(cfg.db_path)
    note = None
    try:
        try:
            for idx, t, frame in iter_video_frames(path, sample_fps):
                with Tx(conn):
                    w.process(conn, frame, idx, iso(start + timedelta(seconds=t)))
        except ValueError as e:
            note = str(e)
        if _calibration(conn, camera_id) is None:
            note = (note + "; " if note else "") + "camera not calibrated: observations are not projected onto the map"
        with Tx(conn):
            result = w.finish(conn, note)
    finally:
        conn.close()
    if hub:
        hub.publish("analysis_session", result, perm("camera.view"))
    return result


