"""Live camera analysis: person observations and automatic "possible fall" SOS alerts.

Frames come from the shared camera relay (camrelay.py). Person detection runs every
`camera.analysis_interval_s`; fall detection looks at up to 5 frames per second per camera.
"""
from __future__ import annotations

import json
import logging
import threading
import time

import cv2

from . import positioning as pos, services
from .analysis import SessionWriter
from .camrelay import RelayManager
from .config import Settings
from .db import Tx, connect, get_setting, now_iso, parse_iso, row, utcnow
from .realtime import Hub, perm
from .security import Principal
from .vision import Detector, FallDetector, decode_jpeg, default_detector

log = logging.getLogger("skymesh.live")

FALL_FPS = 5.0


def fall_location(conn, camera_id: str, bbox: list[float], frame_w: int, frame_h: int) -> dict:
    """Best honest location for a fall seen by a camera."""
    cal = row(conn.execute("SELECT * FROM camera_calibrations WHERE device_id = ?", (camera_id,)).fetchone(), ("homography",))
    name = conn.execute("SELECT name FROM devices WHERE id = ?", (camera_id,)).fetchone()["name"]
    if cal:
        u = (bbox[0] + bbox[2] / 2) * cal["frame_w"] / frame_w
        v = (bbox[1] + bbox[3]) * cal["frame_h"] / frame_h
        xy = pos.apply_homography(cal["homography"], u, v)
        if xy and 0 <= xy[0] <= 1 and 0 <= xy[1] <= 1:
            area = pos.find_area(services.floor_features(conn, cal["floor_id"]), *xy)
            return {"floor_id": cal["floor_id"], "room_id": area["id"] if area else None, "x": xy[0], "y": xy[1],
                    "location_source": "camera_projection", "location_observed_at": now_iso(),
                    "location_note": f"Seen by {name} (position projected from the calibrated camera view)"}
    anc = row(conn.execute("SELECT floor_id, x, y FROM anchors WHERE device_id = ? LIMIT 1", (camera_id,)).fetchone())
    if anc:
        area = pos.find_area(services.floor_features(conn, anc["floor_id"]), anc["x"], anc["y"])
        return {"floor_id": anc["floor_id"], "room_id": area["id"] if area else None, "x": anc["x"], "y": anc["y"],
                "location_source": "camera_position", "location_observed_at": now_iso(),
                "location_note": f"In view of {name} (marker shows the camera's position, not the person's)"}
    return {"location_note": f"In view of {name}. Place this camera on the map to show where that is."}


def raise_fall_incident(cfg: Settings, hub: Hub, conn, camera_id: str, frame, alert: dict) -> dict:
    h, w = frame.shape[:2]
    loc = fall_location(conn, camera_id, alert["bbox"], w, h)
    cam = conn.execute("SELECT name, owner_user_id FROM devices WHERE id = ?", (camera_id,)).fetchone()
    kind = alert.get("kind", "possible_fall")
    what = ("a person collapsed and has not moved for" if kind == "possible_fall"
            else "a person in view has not moved at all for")
    with Tx(conn):
        inc, _ = services.create_incident(
            conn, hub, cfg, source="sensor", idempotency_key=f"{kind}:{camera_id}:{int(time.time())}",
            category=kind, severity="high", reporter_user_id=None, device_id=camera_id,
            location_device_ids=[], device_timestamp=None, is_test=False,
            message=(f"AUTOMATIC: {cam['name']} saw that {what} {alert['still_s']:.0f} s. "
                     "Not verified by a human: check the snapshot and the scene."),
            actor=Principal("device", device_id=camera_id), location_override=loc)
    # Evidence for the responder: the frame with the detected shape outlined.
    x, y, bw, bh = alert["bbox"]
    marked = frame.copy()
    cv2.rectangle(marked, (int(x), int(y)), (int(x + bw), int(y + bh)), (0, 0, 255), 2)
    ev = cfg.data_dir / "evidence"
    ev.mkdir(exist_ok=True)
    cv2.imwrite(str(ev / f"{inc['id']}.jpg"), marked, [cv2.IMWRITE_JPEG_QUALITY, 85])
    log.warning("possible fall on %s -> incident %s", camera_id, inc["id"])
    return inc


class LiveAnalyzer:
    """Runs while person analysis or fall detection is enabled in Administration → Settings."""

    def __init__(self, cfg: Settings, hub: Hub, relays: RelayManager):
        self.cfg, self.hub, self.relays = cfg, hub, relays
        self.stop = threading.Event()
        self.writers: dict[str, SessionWriter] = {}
        self.falls: dict[str, FallDetector] = {}
        self.status: dict[str, dict] = {}
        self.detector: Detector | None = None
        self._seen: dict[str, int] = {}
        self._last_fall_frame: dict[str, float] = {}
        self._last_people: dict[str, float] = {}
        self.thread = threading.Thread(target=self._run, name="skymesh-live-analysis", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        cams, settings_at = [], 0.0
        people_on = falls_on = False
        interval, still_s, idle_s = 5.0, 8.0, 120.0
        while not self.stop.is_set():
            try:
                conn = connect(self.cfg.db_path)
                try:
                    if time.monotonic() - settings_at > 3:
                        settings_at = time.monotonic()
                        people_on = bool(get_setting(conn, "camera.analysis_enabled"))
                        falls_on = bool(get_setting(conn, "camera.fall_detection_enabled"))
                        interval = float(get_setting(conn, "camera.analysis_interval_s"))
                        still_s = float(get_setting(conn, "camera.fall_still_seconds"))
                        idle_s = float(get_setting(conn, "camera.no_movement_seconds"))
                        cams = [dict(r) for r in conn.execute(
                            "SELECT * FROM devices WHERE type = 'camera_node' AND revoked_at IS NULL AND camera_key IS NOT NULL")]
                    if people_on or falls_on:
                        for cam in cams:
                            self._camera(conn, cam, people_on, falls_on, interval, still_s, idle_s)
                finally:
                    conn.close()
            except Exception:  # keep the loop alive; errors show in Diagnostics
                log.exception("live analysis tick failed")
            self.stop.wait(0.2 if (people_on or falls_on) else 2.0)

    def _camera(self, conn, cam: dict, people_on: bool, falls_on: bool, interval: float, still_s: float,
                idle_s: float) -> None:
        cid = cam["id"]
        now = utcnow()
        if not cam["last_seen_at"] or (now - parse_iso(cam["last_seen_at"])).total_seconds() > self.cfg.device_offline_s:
            self.status[cid] = {"state": "offline", "at": now_iso()}
            return
        relay = self.relays.get(cid)
        relay.touch()
        if relay.frame is None or relay.seq == self._seen.get(cid):
            self.status[cid] = {"state": "error" if relay.error else "waiting", "error": relay.error, "at": now_iso()}
            return
        t = time.monotonic()
        want_fall = falls_on and t - self._last_fall_frame.get(cid, 0) >= 1 / FALL_FPS
        want_people = people_on and t - self._last_people.get(cid, 0) >= interval
        if not (want_fall or want_people):
            return
        self._seen[cid] = relay.seq
        frame = decode_jpeg(relay.frame)
        if frame is None:
            return
        st = {"state": "ok", "fps": relay.fps, "at": now_iso(), "fall_detection": falls_on}
        if want_fall:
            self._last_fall_frame[cid] = t
            fd = self.falls.get(cid)
            if fd is None or fd.still_s != still_s or fd.idle_s != idle_s:
                fd = self.falls[cid] = FallDetector(still_s=still_s, idle_s=idle_s)
            alert = fd.update(frame, t)
            st["fall_state"] = fd.state
            if alert:
                inc = raise_fall_incident(self.cfg, self.hub, conn, cid, frame, alert)
                st["last_fall_incident"] = inc["id"]
        if want_people:
            self._last_people[cid] = t
            if self.detector is None:
                self.detector = default_detector(self.cfg.data_dir)
            w = self.writers.get(cid)
            if w is None:
                w = self.writers[cid] = SessionWriter(self.cfg, self.hub, cid, "live", "live", self.detector)
            with Tx(conn):
                obs = w.process(conn, frame, w.frames, now_iso())
                w.finish(conn)
            st["people"] = len(obs)
            projected = [o for o in obs if o["projected"]]
            if projected:
                self.hub.publish("camera_observations", {"camera_id": cid, "observations": projected}, perm("camera.view"))
        self.status[cid] = {**self.status.get(cid, {}), **st}
