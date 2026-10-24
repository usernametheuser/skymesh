"""Badge photos: sent with an SOS (scene picture for responders) or on a responder's request."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from .. import services
from ..db import Tx, get_setting, new_id, now_iso, row, rows
from ..realtime import perm
from ..security import Principal, audit, client_ip, current_device, current_user, get_conn, rate_limit, require

router = APIRouter(prefix="/api/v1", tags=["photos"])

MAX_PHOTO_BYTES = 1_500_000


def _photo_dir(request: Request):
    d = request.app.state.settings.data_dir / "photos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _photo_out(p: dict) -> dict:
    return {**p, "url": f"/api/v1/photos/{p['id']}"}


@router.post("/device/photo", status_code=201, dependencies=[Depends(rate_limit("device_photo", 12, 60))])
async def upload_photo(request: Request, reason: str = "test", event_id: str | None = None, command_id: str | None = None,
                       dev: Principal = Depends(current_device)):
    """Raw JPEG body. The device identity comes from the token."""
    if reason not in ("sos", "request", "test"):
        raise HTTPException(422, "reason must be sos, request or test")
    data = await request.body()
    if len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(413, "Photo larger than 1.5 MB")
    if data[:3] != b"\xff\xd8\xff":
        raise HTTPException(422, "Body is not a JPEG image")
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(422, "JPEG could not be decoded")
    from ..db import connect
    conn = connect(request.app.state.settings.db_path)
    try:
        with Tx(conn):
            incident_id = None
            if reason == "sos" and event_id:
                incident_id = services.incident_for_key(conn, f"{dev.device_id}:{event_id[:40]}")
            if command_id:
                conn.execute("UPDATE device_commands SET done_at = ? WHERE id = ? AND device_id = ?",
                             (now_iso(), command_id[:40], dev.device_id))
            pid = new_id("pho")
            (_photo_dir(request) / f"{pid}.jpg").write_bytes(data)
            conn.execute("INSERT INTO device_photos (id, device_id, incident_id, command_id, reason, file_name, bytes, width, "
                         "height, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (pid, dev.device_id, incident_id, command_id[:40] if command_id else None, reason, f"{pid}.jpg",
                          len(data), img.shape[1], img.shape[0], now_iso()))
            if incident_id:
                services.add_incident_event(conn, incident_id, dev, "photo", None, None, "Badge sent a photo of the scene")
            audit(conn, dev, "device.photo", pid, {"reason": reason, "incident": incident_id, "bytes": len(data)})
            p = row(conn.execute("SELECT * FROM device_photos WHERE id = ?", (pid,)).fetchone())
            owner = conn.execute("SELECT owner_user_id FROM devices WHERE id = ?", (dev.device_id,)).fetchone()["owner_user_id"]
    finally:
        conn.close()
    request.app.state.hub.publish("device_photo", _photo_out(p), perm("incident.view_all", owner))
    return _photo_out(p)


def _device_visible(conn: sqlite3.Connection, p: Principal, device_id: str) -> dict:
    d = row(conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone())
    if not d or (d["owner_user_id"] != p.user_id and not p.can("device.view_all")):
        raise HTTPException(404, "Device not found")
    return d


@router.get("/devices/{device_id}/photos")
def device_photos(device_id: str, p: Principal = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    _device_visible(conn, p, device_id)
    return [_photo_out(x) for x in rows(conn.execute(
        "SELECT * FROM device_photos WHERE device_id = ? ORDER BY created_at DESC LIMIT 50", (device_id,)))]


@router.get("/incidents/{incident_id}/photos")
def incident_photos(incident_id: str, p: Principal = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    inc = services.get_incident(conn, incident_id)
    if not p.can("incident.view_all") and inc["reporter_user_id"] != p.user_id:
        raise HTTPException(404, "Incident not found")
    return [_photo_out(x) for x in rows(conn.execute(
        "SELECT * FROM device_photos WHERE incident_id = ? ORDER BY created_at", (incident_id,)))]


@router.get("/photos/{photo_id}")
def photo_file(photo_id: str, request: Request, p: Principal = Depends(current_user),
               conn: sqlite3.Connection = Depends(get_conn)):
    ph = row(conn.execute("SELECT * FROM device_photos WHERE id = ?", (photo_id,)).fetchone())
    if not ph:
        raise HTTPException(404, "Photo not found")
    d = _device_visible(conn, p, ph["device_id"])
    if d["owner_user_id"] != p.user_id:
        with Tx(conn):
            audit(conn, p, "photo.view", photo_id, {"device": ph["device_id"]}, client_ip(request))
    path = _photo_dir(request) / ph["file_name"]
    if not path.is_file():
        raise HTTPException(404, "Photo file was removed by the retention policy")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})


@router.post("/devices/{device_id}/photo-request", status_code=202)
def request_photo(device_id: str, request: Request, p: Principal = Depends(current_user),
                  conn: sqlite3.Connection = Depends(get_conn)):
    """Ask a badge for a photo. Delivered with its next heartbeat (within about 30 s). Audited.
    Responders and admins may ask any badge; everyone else only the badge they wear."""
    if not get_setting(conn, "badge.remote_photo_enabled"):
        raise HTTPException(403, "Photo requests are turned off (Administration → Settings → badge.remote_photo_enabled)")
    with Tx(conn):
        d = row(conn.execute("SELECT * FROM devices WHERE id = ? AND revoked_at IS NULL", (device_id,)).fetchone())
        if not d or d["type"] != "badge" or (not p.can("incident.manage") and d["owner_user_id"] != p.user_id):
            raise HTTPException(404, "Badge not found")
        cid = new_id("cmd")
        conn.execute("INSERT INTO device_commands (id, device_id, cmd, requested_by, created_at) VALUES (?,?,?,?,?)",
                     (cid, device_id, "photo", p.user_id, now_iso()))
        audit(conn, p, "photo.request", device_id, {"command": cid}, client_ip(request))
    return {"id": cid, "note": "The badge takes the photo at its next check-in (within about 30 seconds)."}


def pending_commands(conn: sqlite3.Connection, device_id: str) -> list[dict]:
    """Commands for the heartbeat response; each is handed out once."""
    cmds = rows(conn.execute("SELECT id, cmd FROM device_commands WHERE device_id = ? AND delivered_at IS NULL "
                             "ORDER BY created_at LIMIT 5", (device_id,)))
    for c in cmds:
        conn.execute("UPDATE device_commands SET delivered_at = ? WHERE id = ?", (now_iso(), c["id"]))
    return cmds
