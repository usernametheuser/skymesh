"""Device enrollment, revocation, listing and device-facing heartbeat API."""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from .. import positioning as pos, services
from ..db import Tx, new_id, now_iso, row, rows, utcnow
from ..realtime import perm
from ..security import (Principal, audit, client_ip, current_device, current_user, get_conn, new_device_token,
                        rate_limit, require, token_hash)

router = APIRouter(prefix="/api/v1", tags=["devices"])

ENROLLABLE_TYPES = "^(badge|camera_node|anchor|gateway|laptop|phone)$"
MAX_PHONES_PER_USER = 5


class EnrollIn(BaseModel):
    type: str = Field(pattern=ENROLLABLE_TYPES)
    name: str = Field(min_length=1, max_length=60)
    owner_user_id: str | None = None
    hardware_model: str | None = Field(default=None, max_length=80)


class PhoneRegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class DevicePatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    owner_user_id: str | None = None
    clear_owner: bool = False


class HeartbeatIn(BaseModel):
    schema_version: int = Field(ge=1, le=1)
    firmware_version: str = Field(max_length=40)
    hardware_model: str | None = Field(default=None, max_length=80)
    uptime_s: int = Field(ge=0)
    sequence: int = Field(ge=0)
    ip: str | None = Field(default=None, max_length=45)
    rssi: int | None = Field(default=None, ge=-127, le=0)
    bssid: str | None = Field(default=None, max_length=17)
    free_heap: int | None = Field(default=None, ge=0)
    psram_free: int | None = Field(default=None, ge=0)
    battery_mv: int | None = Field(default=None, ge=0, le=20000)
    battery_pct: int | None = Field(default=None, ge=0, le=100)
    camera_ok: bool | None = None
    # Text from SkyMesh_Server, a numeric esp_reset_reason_t from badge firmware 0.2.0: accept both.
    reset_reason: str | int | None = Field(default=None, union_mode="left_to_right")
    capabilities: dict = Field(default_factory=dict)
    pending_sos: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("reset_reason")
    @classmethod
    def _reason_text(cls, v):
        return None if v is None else str(v)[:40]


def device_public(conn: sqlite3.Connection, d: dict, request: Request, full: bool) -> dict:
    cfg = request.app.state.settings
    now = utcnow()
    owner = conn.execute("SELECT display_name FROM users WHERE id = ?", (d["owner_user_id"],)).fetchone() \
        if d["owner_user_id"] else None
    caps = d["capabilities"] if isinstance(d["capabilities"], dict) else json.loads(d["capabilities"] or "{}")
    out = {
        "id": d["id"], "type": d["type"], "name": d["name"], "owner_user_id": d["owner_user_id"],
        "owner_name": owner["display_name"] if owner else None, "hardware_model": d["hardware_model"],
        "firmware_version": d["firmware_version"], "enrolled_at": d["enrolled_at"], "revoked_at": d["revoked_at"],
        "last_seen_at": d["last_seen_at"], "online": pos.device_online(d, cfg, now) if not d["revoked_at"] else False,
        "battery_mv": d["battery_mv"], "battery_pct": d["battery_pct"], "camera_ok": d["camera_ok"],
        "rssi": d["last_rssi"], "capabilities": caps, "sharing_enabled": bool(d["sharing_enabled"]),
        "permission_state": d["permission_state"],
    }
    if full:
        out.update(last_ip=d["last_ip"], bssid=d["last_bssid"], free_heap=d["free_heap"], uptime_s=d["uptime_s"],
                   last_sequence=d["last_sequence"])
    return out


@router.get("/devices")
def list_devices(request: Request, p: Principal = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    if p.can("device.view_all"):
        ds = rows(conn.execute("SELECT * FROM devices ORDER BY type, name"))
    else:
        ds = rows(conn.execute("SELECT * FROM devices WHERE owner_user_id = ? ORDER BY type, name", (p.user_id,)))
    return [device_public(conn, d, request, p.can("device.view_all")) for d in ds]


@router.post("/devices", status_code=201)
def enroll_device(body: EnrollIn, request: Request, p: Principal = Depends(require("device.enroll")),
                  conn: sqlite3.Connection = Depends(get_conn)):
    """Create a device identity. The token (and camera key) are shown exactly once."""
    token = new_device_token()
    camera_key = secrets.token_urlsafe(24) if body.type == "camera_node" else None
    did = new_id({"badge": "badge", "camera_node": "cam", "phone": "phone"}.get(body.type, "dev"))
    with Tx(conn):
        if body.owner_user_id and not conn.execute("SELECT 1 FROM users WHERE id = ?", (body.owner_user_id,)).fetchone():
            raise HTTPException(422, "Unknown owner_user_id")
        conn.execute(
            "INSERT INTO devices (id, type, name, owner_user_id, token_hash, camera_key, hardware_model, enrolled_at, "
            "enrolled_by, permission_state) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (did, body.type, body.name, body.owner_user_id, token_hash(token), camera_key, body.hardware_model,
             now_iso(), p.user_id, "unknown" if body.type == "phone" else "not_applicable"))
        audit(conn, p, "device.enroll", did, {"type": body.type, "owner": body.owner_user_id}, client_ip(request))
        d = row(conn.execute("SELECT * FROM devices WHERE id = ?", (did,)).fetchone())
    out = {"device": device_public(conn, d, request, True), "device_token": token}
    if camera_key:
        out["camera_key"] = camera_key
    return out


@router.patch("/devices/{device_id}")
def patch_device(device_id: str, body: DevicePatchIn, request: Request, p: Principal = Depends(require("device.enroll")),
                 conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        d = row(conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone())
        if not d:
            raise HTTPException(404, "Device not found")
        if body.name:
            conn.execute("UPDATE devices SET name = ? WHERE id = ?", (body.name, device_id))
        if body.clear_owner:
            conn.execute("UPDATE devices SET owner_user_id = NULL WHERE id = ?", (device_id,))
        elif body.owner_user_id:
            if not conn.execute("SELECT 1 FROM users WHERE id = ?", (body.owner_user_id,)).fetchone():
                raise HTTPException(422, "Unknown owner_user_id")
            conn.execute("UPDATE devices SET owner_user_id = ? WHERE id = ?", (body.owner_user_id, device_id))
        audit(conn, p, "device.update", device_id, body.model_dump(exclude_none=True), client_ip(request))
        d = row(conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone())
    return device_public(conn, d, request, True)


@router.post("/devices/{device_id}/revoke")
def revoke_device(device_id: str, request: Request, p: Principal = Depends(require("device.enroll")),
                  conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        cur = conn.execute("UPDATE devices SET revoked_at = ?, token_hash = NULL, camera_key = NULL "
                           "WHERE id = ? AND revoked_at IS NULL", (now_iso(), device_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "Device not found or already revoked")
        audit(conn, p, "device.revoke", device_id, {}, client_ip(request))
    request.app.state.hub.publish("device", {"id": device_id, "revoked": True}, perm("device.view_all"))
    return {"ok": True}


@router.post("/devices/{device_id}/rotate-token")
def rotate_token(device_id: str, request: Request, p: Principal = Depends(require("device.enroll")),
                 conn: sqlite3.Connection = Depends(get_conn)):
    token = new_device_token()
    with Tx(conn):
        cur = conn.execute("UPDATE devices SET token_hash = ? WHERE id = ? AND revoked_at IS NULL",
                           (token_hash(token), device_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "Device not found or revoked")
        audit(conn, p, "device.rotate_token", device_id, {}, client_ip(request))
    return {"device_token": token}


FIRMWARE_DIR = Path(__file__).resolve().parents[2] / "firmware"


@router.get("/firmware")
def firmware_manifest(p: Principal = Depends(require("device.enroll"))):
    """Prebuilt images the Add-device wizard flashes over USB (built by scripts/build_firmware.py)."""
    m = FIRMWARE_DIR / "manifest.json"
    if not m.exists():
        raise HTTPException(404, "No prebuilt firmware: run backend/scripts/build_firmware.py")
    return json.loads(m.read_text(encoding="utf-8"))


@router.get("/firmware/{file_name}")
def firmware_file(file_name: str, p: Principal = Depends(require("device.enroll"))):
    if not re.fullmatch(r"[a-z0-9\-]+\.bin", file_name) or not (FIRMWARE_DIR / file_name).is_file():
        raise HTTPException(404, "Firmware image not found")
    return FileResponse(FIRMWARE_DIR / file_name, media_type="application/octet-stream",
                        headers={"Cache-Control": "no-cache"})


@router.get("/devices/{device_id}/heartbeats")
def device_heartbeats(device_id: str, limit: int = 50, p: Principal = Depends(require("device.view_all")),
                      conn: sqlite3.Connection = Depends(get_conn)):
    rs = rows(conn.execute("SELECT received_at, payload FROM device_heartbeats WHERE device_id = ? "
                           "ORDER BY id DESC LIMIT ?", (device_id, min(max(limit, 1), 500))), ("payload",))
    return rs


@router.post("/phones", status_code=201)
def register_phone(body: PhoneRegisterIn, request: Request, p: Principal = Depends(current_user),
                   conn: sqlite3.Connection = Depends(get_conn)):
    """A signed-in user registers the phone/browser they are using. Sharing starts OFF."""
    with Tx(conn):
        n = conn.execute("SELECT COUNT(*) AS n FROM devices WHERE owner_user_id = ? AND type = 'phone' "
                         "AND revoked_at IS NULL", (p.user_id,)).fetchone()["n"]
        if n >= MAX_PHONES_PER_USER:
            raise HTTPException(409, f"At most {MAX_PHONES_PER_USER} phones per user")
        did = new_id("phone")
        conn.execute("INSERT INTO devices (id, type, name, owner_user_id, enrolled_at, enrolled_by, permission_state) "
                     "VALUES (?, 'phone', ?, ?, ?, ?, 'unknown')", (did, body.name, p.user_id, now_iso(), p.user_id))
        audit(conn, p, "phone.register", did, {}, client_ip(request))
        d = row(conn.execute("SELECT * FROM devices WHERE id = ?", (did,)).fetchone())
    return device_public(conn, d, request, False)


# ---------------------------------------------------------------- device-facing API (device token)

@router.post("/device/heartbeat", dependencies=[Depends(rate_limit("heartbeat", 30, 60))])
def heartbeat(body: HeartbeatIn, request: Request, dev: Principal = Depends(current_device),
              conn: sqlite3.Connection = Depends(get_conn)):
    """The device ID comes from the token, never from the payload."""
    now = now_iso()
    with Tx(conn):
        before = row(conn.execute("SELECT * FROM devices WHERE id = ?", (dev.device_id,)).fetchone())
        was_online = pos.device_online(before, request.app.state.settings, utcnow())
        conn.execute(
            "UPDATE devices SET last_seen_at = ?, last_ip = ?, last_rssi = ?, last_bssid = ?, firmware_version = ?, "
            "hardware_model = COALESCE(?, hardware_model), free_heap = ?, uptime_s = ?, battery_mv = ?, "
            "battery_pct = ?, camera_ok = ?, last_sequence = ?, capabilities = ? WHERE id = ?",
            # The address the request actually came from, not the self-reported one: the backend
            # later connects to it (camera snapshots), so a device must not be able to redirect that.
            (now, client_ip(request), body.rssi, body.bssid, body.firmware_version, body.hardware_model,
             body.free_heap, body.uptime_s, body.battery_mv, body.battery_pct,
             None if body.camera_ok is None else int(body.camera_ok), body.sequence,
             json.dumps(body.capabilities)[:2000], dev.device_id))
        conn.execute("INSERT INTO device_heartbeats (device_id, received_at, payload) VALUES (?,?,?)",
                     (dev.device_id, now, body.model_dump_json()))
        if before["uptime_s"] is not None and body.uptime_s < before["uptime_s"]:
            audit(conn, dev, "device.rebooted", dev.device_id, {"reset_reason": body.reset_reason})
        beacon = str(body.capabilities.get("beacon_id") or body.capabilities.get("ble_mac") or "").lower()
        if dev.device_type == "camera_node" and re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", beacon):
            # A camera placed on the map becomes a positioning anchor (its Wi-Fi beacon) automatically.
            conn.execute("UPDATE anchors SET identifier = ? WHERE device_id = ? AND (identifier IS NULL OR identifier != ?)",
                         (beacon, dev.device_id, beacon))
        sos_status = sos_states(conn, dev.device_id, body.pending_sos)
        from .photos import pending_commands
        commands = pending_commands(conn, dev.device_id)
        d = row(conn.execute("SELECT * FROM devices WHERE id = ?", (dev.device_id,)).fetchone())
    pub = device_public(conn, d, request, True)
    request.app.state.hub.publish("device", pub, perm("device.view_all", d["owner_user_id"]))
    if not was_online:
        request.app.state.hub.publish("device_online", {"id": d["id"], "name": d["name"]}, perm("device.view_all"))
    from .incidents import badge_sos_config
    return {"server_time": now, "device_id": dev.device_id, "heartbeat_interval_s": 30, "sos": sos_status,
            "commands": commands, **badge_sos_config(conn)}


def sos_states(conn: sqlite3.Connection, device_id: str, event_ids: list[str]) -> list[dict]:
    """Delivery/acknowledgement state for SOS event ids a device is still tracking."""
    out = []
    for eid in event_ids[:20]:
        iid = services.incident_for_key(conn, f"{device_id}:{eid}")  # created by this press, or re-alerted by it
        r = conn.execute("SELECT id, state FROM incidents WHERE id = ? AND device_id = ?", (iid, device_id)).fetchone() if iid else None
        out.append({"event_id": eid, "incident_id": r["id"] if r else None, "state": r["state"] if r else "UNKNOWN",
                    "confirmed": bool(r and r["state"] in ("ACKNOWLEDGED", "RESPONDING", "RESOLVED", "FALSE_ALARM"))})
    return out


@router.get("/device/me")
def device_me(request: Request, dev: Principal = Depends(current_device), conn: sqlite3.Connection = Depends(get_conn)):
    d = row(conn.execute("SELECT * FROM devices WHERE id = ?", (dev.device_id,)).fetchone())
    return {"device": device_public(conn, d, request, True)}
