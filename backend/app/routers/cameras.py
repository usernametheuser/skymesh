"""Camera nodes: snapshots, calibration, video analysis, observations and verified associations."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from .. import analysis, positioning as pos, services
from ..db import Tx, connect, iso, new_id, now_iso, row, rows, utcnow
from ..security import Principal, audit, client_ip, get_conn, require
from ..vision import decode_jpeg

router = APIRouter(prefix="/api/v1", tags=["cameras"])

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv"}


class CalibrationIn(BaseModel):
    floor_id: str = Field(max_length=40)
    image_points: list[list[float]] = Field(min_length=4, max_length=20)
    map_points: list[list[float]] = Field(min_length=4, max_length=20)
    frame_w: int = Field(ge=16, le=8000)
    frame_h: int = Field(ge=16, le=8000)


class AssociationIn(BaseModel):
    session_id: str = Field(max_length=40)
    track_label: str = Field(max_length=60)
    device_id: str = Field(max_length=40)
    note: str | None = Field(default=None, max_length=300)


def _camera(conn: sqlite3.Connection, camera_id: str) -> dict:
    d = row(conn.execute("SELECT * FROM devices WHERE id = ? AND type = 'camera_node'", (camera_id,)).fetchone())
    if not d:
        raise HTTPException(404, "Camera node not found")
    return d


@router.get("/cameras")
def cameras(request: Request, p: Principal = Depends(require("camera.view")), conn: sqlite3.Connection = Depends(get_conn)):
    cfg = request.app.state.settings
    live = request.app.state.live_analyzer.status
    out = []
    for d in rows(conn.execute("SELECT * FROM devices WHERE type = 'camera_node' ORDER BY name")):
        cal = row(conn.execute("SELECT floor_id, frame_w, frame_h, created_at FROM camera_calibrations WHERE device_id = ?",
                               (d["id"],)).fetchone())
        anchor = row(conn.execute("SELECT id, floor_id, x, y FROM anchors WHERE device_id = ?", (d["id"],)).fetchone())
        caps = json.loads(d["capabilities"] or "{}")
        out.append({"id": d["id"], "name": d["name"], "online": pos.device_online(d, cfg, utcnow()) if not d["revoked_at"] else False,
                    "revoked": bool(d["revoked_at"]), "camera_ok": d["camera_ok"], "last_seen_at": d["last_seen_at"],
                    "firmware_version": d["firmware_version"], "calibration": cal, "placement": anchor,
                    "live_analysis": live.get(d["id"]),
                    "sensor": caps.get("sensor"), "software_jpeg": caps.get("software_jpeg"),
                    "camera_error": caps.get("camera_error"), "beacon_id": caps.get("beacon_id"),
                    "hmirror": caps.get("hmirror"), "vflip": caps.get("vflip"), "picture": caps.get("picture") or {},
                    "relay": request.app.state.relays.status().get(d["id"])})
    return out


@router.get("/cameras/{camera_id}/live")
async def live(camera_id: str, request: Request, p: Principal = Depends(require("camera.view"))):
    """Low-latency MJPEG view (about 12-15 fps at 320x240) relayed through the backend.

    The camera key never reaches the browser, and any number of viewers share one camera connection.
    """
    conn = connect(request.app.state.settings.db_path)
    try:
        _camera(conn, camera_id)
        with Tx(conn):
            audit(conn, p, "camera.live_view", camera_id, {}, client_ip(request))
    finally:
        conn.close()
    relay = request.app.state.relays.get(camera_id)
    relay.touch()

    async def frames():
        seq = -1
        idle_since = time.monotonic()
        while not await request.is_disconnected():
            relay.touch()
            new_seq, jpg = await asyncio.to_thread(relay.wait_frame, seq, 1.0)
            if jpg is None or new_seq == seq:
                if time.monotonic() - idle_since > 20:
                    break  # camera stopped sending: let the browser show the error and retry
                continue
            seq, idle_since = new_seq, time.monotonic()
            yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n"

    return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


class ControlIn(BaseModel):
    cmd: str = Field(pattern="^(rotate180|hmirror|vflip|framesize|quality|flash|brightness|contrast|saturation|reset)$")
    val: str = Field(default="", max_length=8, pattern="^-?[a-z0-9]*$")


@router.post("/cameras/{camera_id}/control")
def control(camera_id: str, body: ControlIn, request: Request, p: Principal = Depends(require("camera.configure")),
            conn: sqlite3.Connection = Depends(get_conn)):
    """Rotate / mirror / resize the picture on the node itself (the node remembers orientation)."""
    import httpx
    cam = _camera(conn, camera_id)
    try:
        host = analysis.camera_host(cam)
        r = httpx.get(f"http://{host}/control", params={"cmd": body.cmd, "val": body.val},
                      headers={"X-SkyMesh-Key": cam["camera_key"]}, timeout=request.app.state.settings.camera_fetch_timeout_s)
    except analysis.CameraUnavailable as e:
        raise HTTPException(503, str(e))
    except httpx.HTTPError as e:
        raise HTTPException(503, f"camera not reachable ({type(e).__name__})")
    if r.status_code != 200:
        raise HTTPException(502, f"camera refused the command (HTTP {r.status_code})")
    with Tx(conn):
        # Show the new value at once instead of waiting for the next heartbeat to report it.
        caps = json.loads(cam["capabilities"] or "{}")
        pic = caps.setdefault("picture", {})
        if body.cmd in ("flash", "brightness", "contrast", "saturation", "quality"):
            pic[body.cmd] = int(body.val or 0)
        elif body.cmd == "framesize":
            pic["framesize"] = body.val
        elif body.cmd == "reset":
            caps["picture"] = {"flash": pic.get("flash", 0), "brightness": 0, "contrast": 0, "saturation": 0,
                               "quality": 35, "framesize": "qvga"}
        conn.execute("UPDATE devices SET capabilities = ? WHERE id = ?", (json.dumps(caps), camera_id))
        audit(conn, p, "camera.control", camera_id, body.model_dump(), client_ip(request))
    return {"ok": True}


@router.get("/cameras/{camera_id}/live-status")
def live_status(camera_id: str, request: Request, p: Principal = Depends(require("camera.view"))):
    r = request.app.state.relays.get(camera_id)
    return {"fps": r.fps, "error": r.error, "has_frame": r.frame is not None, "frame_bytes": len(r.frame) if r.frame else 0}


@router.get("/cameras/{camera_id}/snapshot")
def snapshot(camera_id: str, request: Request, p: Principal = Depends(require("camera.view")),
             conn: sqlite3.Connection = Depends(get_conn)):
    cam = _camera(conn, camera_id)
    data = request.app.state.relays.get(camera_id).fresh_frame()  # video already flowing: reuse its latest frame
    if data is None:
        try:
            data = analysis.fetch_snapshot(request.app.state.settings, cam)
        except analysis.CameraUnavailable as e:
            raise HTTPException(503, str(e))
    with Tx(conn):
        audit(conn, p, "camera.snapshot_view", camera_id, {}, client_ip(request))
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/cameras/{camera_id}/analyze-snapshot")
def analyze_snapshot(camera_id: str, request: Request, p: Principal = Depends(require("camera.configure")),
                     conn: sqlite3.Connection = Depends(get_conn)):
    cfg = request.app.state.settings
    cam = _camera(conn, camera_id)
    try:
        frame = decode_jpeg(analysis.fetch_snapshot(cfg, cam))
    except analysis.CameraUnavailable as e:
        raise HTTPException(503, str(e))
    if frame is None:
        raise HTTPException(502, "Snapshot could not be decoded")
    w = analysis.SessionWriter(cfg, request.app.state.hub, camera_id, "live", "manual snapshot",
                               request.app.state.live_analyzer.detector or analysis.default_detector(cfg.data_dir))
    with Tx(conn):
        obs = w.process(conn, frame, 0, now_iso())
        sess = w.finish(conn)
        audit(conn, p, "camera.analyze_snapshot", camera_id, {"people": len(obs)}, client_ip(request))
    return {"session": sess, "observations": obs}


@router.put("/cameras/{camera_id}/calibration")
def set_calibration(camera_id: str, body: CalibrationIn, request: Request, p: Principal = Depends(require("camera.configure")),
                    conn: sqlite3.Connection = Depends(get_conn)):
    """Pixel points on the camera image <-> normalized points on the floor plan (floor plane only)."""
    if len(body.image_points) != len(body.map_points):
        raise HTTPException(422, "image_points and map_points must pair up")
    for x, y in body.map_points:
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise HTTPException(422, "map_points must be normalized 0..1")
    try:
        h = pos.fit_homography(body.image_points, body.map_points)
    except ValueError as e:
        raise HTTPException(422, str(e))
    with Tx(conn):
        _camera(conn, camera_id)
        services.require_floor(conn, body.floor_id)
        conn.execute(
            "INSERT INTO camera_calibrations (device_id, floor_id, image_points, map_points, homography, frame_w, frame_h, "
            "created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET "
            "floor_id=excluded.floor_id, image_points=excluded.image_points, map_points=excluded.map_points, "
            "homography=excluded.homography, frame_w=excluded.frame_w, frame_h=excluded.frame_h, "
            "created_at=excluded.created_at, created_by=excluded.created_by",
            (camera_id, body.floor_id, json.dumps(body.image_points), json.dumps(body.map_points), json.dumps(h),
             body.frame_w, body.frame_h, now_iso(), p.user_id))
        audit(conn, p, "camera.calibrate", camera_id, {"floor_id": body.floor_id, "points": len(body.image_points)},
              client_ip(request))
    # Report reprojection error so the operator can judge the calibration.
    errs = []
    for (u, v), (mx, my) in zip(body.image_points, body.map_points):
        xy = pos.apply_homography(h, u, v)
        if xy:
            errs.append(((xy[0] - mx) ** 2 + (xy[1] - my) ** 2) ** 0.5)
    return {"ok": True, "max_reprojection_error": round(max(errs), 5) if errs else None}


@router.delete("/cameras/{camera_id}/calibration")
def clear_calibration(camera_id: str, request: Request, p: Principal = Depends(require("camera.configure")),
                      conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        conn.execute("DELETE FROM camera_calibrations WHERE device_id = ?", (camera_id,))
        audit(conn, p, "camera.calibration_clear", camera_id, {}, client_ip(request))
    return {"ok": True}


@router.post("/cameras/{camera_id}/videos", status_code=202)
async def upload_video(camera_id: str, request: Request, file: UploadFile = File(...), sample_fps: float = Form(2.0),
                       p: Principal = Depends(require("camera.configure")), conn: sqlite3.Connection = Depends(get_conn)):
    """Upload authorized footage for analysis. Runs in the background; poll /cameras/sessions."""
    cfg = request.app.state.settings
    _camera(conn, camera_id)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in VIDEO_EXTS:
        raise HTTPException(422, f"Video must be one of {sorted(VIDEO_EXTS)}")
    if not (0.2 <= sample_fps <= 10):
        raise HTTPException(422, "sample_fps must be 0.2..10")
    dst = cfg.data_dir / "videos" / f"{new_id('vid')}{ext}"
    size = 0
    with dst.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > cfg.max_upload_bytes:
                out.close()
                dst.unlink(missing_ok=True)
                raise HTTPException(413, "Video too large")
            out.write(chunk)
    with Tx(conn):
        audit(conn, p, "camera.video_upload", camera_id, {"bytes": size, "name": (file.filename or "")[:100]},
              client_ip(request))
    hub = request.app.state.hub
    threading.Thread(target=analysis.analyze_video, args=(cfg, hub, camera_id, dst, sample_fps), daemon=True).start()
    return {"accepted": True, "bytes": size}


@router.get("/cameras/sessions")
def sessions(camera_id: str | None = None, p: Principal = Depends(require("camera.view")),
             conn: sqlite3.Connection = Depends(get_conn)):
    if camera_id:
        return rows(conn.execute("SELECT * FROM analysis_sessions WHERE camera_device_id = ? ORDER BY started_at DESC "
                                 "LIMIT 100", (camera_id,)))
    return rows(conn.execute("SELECT * FROM analysis_sessions ORDER BY started_at DESC LIMIT 100"))


@router.get("/cameras/sessions/{session_id}/tracks")
def session_tracks(session_id: str, p: Principal = Depends(require("camera.view")),
                   conn: sqlite3.Connection = Depends(get_conn)):
    ts = rows(conn.execute(
        "SELECT track_label, COUNT(*) AS frames, MIN(observed_at) AS first_seen, MAX(observed_at) AS last_seen, "
        "MAX(confidence) AS max_confidence, SUM(projected) AS projected_frames FROM camera_observations "
        "WHERE session_id = ? GROUP BY track_label ORDER BY first_seen", (session_id,)))
    assoc = {a["track_label"]: a for a in rows(conn.execute(
        "SELECT a.*, d.name AS device_name FROM associations a JOIN devices d ON d.id = a.device_id "
        "WHERE a.session_id = ? AND a.revoked_at IS NULL", (session_id,)))}
    for t in ts:
        t["association"] = assoc.get(t["track_label"])
    return ts


@router.get("/observations/current")
def current_observations(request: Request, floor_id: str | None = None, window_s: int = 30,
                         p: Principal = Depends(require("camera.view")), conn: sqlite3.Connection = Depends(get_conn)):
    """Latest projected position per live track within the window. Anonymous unless an association was confirmed."""
    since = iso(utcnow() - timedelta(seconds=min(max(window_s, 5), 600)))
    args: list = [since]
    sql = ("SELECT o.* FROM camera_observations o JOIN (SELECT session_id, track_label, MAX(id) AS mid FROM "
           "camera_observations WHERE source = 'live' AND projected = 1 AND observed_at >= ? GROUP BY session_id, "
           "track_label) last ON last.mid = o.id")
    if floor_id:
        sql += " WHERE o.floor_id = ?"
        args.append(floor_id)
    obs = rows(conn.execute(sql, args), ("bbox",))
    for o in obs:
        a = row(conn.execute("SELECT a.device_id, d.name AS device_name FROM associations a JOIN devices d ON d.id = "
                             "a.device_id WHERE a.session_id = ? AND a.track_label = ? AND a.revoked_at IS NULL",
                             (o["session_id"], o["track_label"])).fetchone())
        o["association"] = a
    return obs


@router.post("/associations", status_code=201)
def create_association(body: AssociationIn, request: Request, p: Principal = Depends(require("association.confirm")),
                       conn: sqlite3.Connection = Depends(get_conn)):
    """A human operator confirms that an anonymous track is the person carrying a given device."""
    aid = new_id("asc")
    with Tx(conn):
        if not conn.execute("SELECT 1 FROM camera_observations WHERE session_id = ? AND track_label = ? LIMIT 1",
                            (body.session_id, body.track_label)).fetchone():
            raise HTTPException(404, "Track not found")
        if not conn.execute("SELECT 1 FROM devices WHERE id = ? AND type IN ('badge','phone')", (body.device_id,)).fetchone():
            raise HTTPException(422, "Associations link a track to a badge or phone")
        conn.execute("UPDATE associations SET revoked_at = ? WHERE session_id = ? AND track_label = ? AND revoked_at IS NULL",
                     (now_iso(), body.session_id, body.track_label))
        conn.execute("INSERT INTO associations (id, session_id, track_label, device_id, method, confirmed_by, note, "
                     "created_at) VALUES (?,?,?,?, 'operator_confirmed', ?,?,?)",
                     (aid, body.session_id, body.track_label, body.device_id, p.user_id, body.note, now_iso()))
        audit(conn, p, "association.confirm", aid, body.model_dump(), client_ip(request))
    return {"id": aid}


@router.delete("/associations/{association_id}")
def revoke_association(association_id: str, request: Request, p: Principal = Depends(require("association.confirm")),
                       conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        if conn.execute("UPDATE associations SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                        (now_iso(), association_id)).rowcount == 0:
            raise HTTPException(404, "Association not found")
        audit(conn, p, "association.revoke", association_id, {}, client_ip(request))
    return {"ok": True}
