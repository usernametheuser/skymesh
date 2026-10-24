"""Phone location sharing, device location reports and the live location view."""
from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from .. import services
from ..db import Tx, rows
from ..realtime import perm
from ..security import Principal, audit, client_ip, current_device, current_user, get_conn, rate_limit

router = APIRouter(prefix="/api/v1", tags=["locations"])


class SharingIn(BaseModel):
    enabled: bool
    permission_state: Literal["unknown", "granted", "denied", "unsupported"] | None = None


class BleReading(BaseModel):
    id: str = Field(max_length=40)
    rssi: int = Field(ge=-120, le=0)


class LocationIn(BaseModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    kind: Literal["geo", "floor", "room", "ble", "unavailable", "denied"]
    timestamp: str | None = Field(default=None, max_length=40)
    sequence: int | None = Field(default=None, ge=0)
    floor_id: str | None = Field(default=None, max_length=40)
    room_id: str | None = Field(default=None, max_length=40)
    x: float | None = Field(default=None, ge=0, le=1)
    y: float | None = Field(default=None, ge=0, le=1)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0, le=100000)
    source: str | None = Field(default=None, max_length=40)
    confidence: float | None = Field(default=None, ge=0, le=1)
    ble: list[BleReading] | None = Field(default=None, max_length=32)
    wifi: list[BleReading] | None = Field(default=None, max_length=40)  # access points heard: BSSID + RSSI
    reason: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def _required_fields(self):
        need = {"geo": ("lat", "lon"), "floor": ("floor_id", "x", "y"), "room": ("room_id",)}
        missing = [f for f in need.get(self.kind, ()) if getattr(self, f) is None]
        if self.kind == "ble" and self.ble is None and self.wifi is None:
            missing.append("ble or wifi")
        if missing:
            raise ValueError(f"kind={self.kind} requires {', '.join(missing)}")
        return self


def _owned_phone(conn: sqlite3.Connection, p: Principal, device_id: str) -> dict:
    d = services.device_row(conn, device_id)
    if d["owner_user_id"] != p.user_id or d["type"] != "phone" or d["revoked_at"]:
        raise HTTPException(404, "Phone not found")
    return d


@router.post("/phones/{device_id}/sharing")
def set_sharing(device_id: str, body: SharingIn, request: Request, p: Principal = Depends(current_user),
                conn: sqlite3.Connection = Depends(get_conn)):
    """Only the phone's owner can turn sharing on or off. No administrator override exists."""
    cfg = request.app.state.settings
    with Tx(conn):
        _owned_phone(conn, p, device_id)
        if body.permission_state:
            conn.execute("UPDATE devices SET permission_state = ? WHERE id = ?", (body.permission_state, device_id))
        enabled = body.enabled and body.permission_state != "denied"
        conn.execute("UPDATE devices SET sharing_enabled = ? WHERE id = ?", (int(enabled), device_id))
        audit(conn, p, "location.sharing_" + ("on" if enabled else "off"), device_id,
              {"permission_state": body.permission_state}, client_ip(request))
        d = services.device_row(conn, device_id)
        item = services.describe_location(conn, cfg, d)
    request.app.state.hub.publish("location", item, perm("location.view_all", p.user_id))
    return item


@router.post("/phones/{device_id}/location", dependencies=[Depends(rate_limit("phone_location", 120, 60))])
def phone_location(device_id: str, body: LocationIn, request: Request, p: Principal = Depends(current_user),
                   conn: sqlite3.Connection = Depends(get_conn)):
    if body.kind == "ble":
        raise HTTPException(422, "BLE estimates from phones are not supported yet")
    with Tx(conn):
        d = _owned_phone(conn, p, device_id)
        if body.kind == "denied":
            conn.execute("UPDATE devices SET permission_state = 'denied', sharing_enabled = 0 WHERE id = ?", (device_id,))
        elif not d["sharing_enabled"]:
            raise HTTPException(409, "Location sharing is off for this phone")
        res = services.ingest_location(conn, request.app.state.hub, request.app.state.settings, d,
                                       body.model_dump())
    return res


@router.post("/device/location", dependencies=[Depends(rate_limit("device_location", 120, 60))])
def device_location(body: LocationIn, request: Request, dev: Principal = Depends(current_device),
                    conn: sqlite3.Connection = Depends(get_conn)):
    if body.kind in ("geo",) and dev.device_type != "badge":
        raise HTTPException(422, "Unsupported location kind for this device")
    with Tx(conn):
        d = services.device_row(conn, dev.device_id)
        return services.ingest_location(conn, request.app.state.hub, request.app.state.settings, d, body.model_dump())


@router.get("/locations/current")
def current_locations(request: Request, floor_id: str | None = None, p: Principal = Depends(current_user),
                      conn: sqlite3.Connection = Depends(get_conn)):
    items = services.visible_location_items(conn, request.app.state.settings, p, floor_id)
    if p.can("location.view_all"):
        others = [i["device_id"] for i in items if i["owner_user_id"] != p.user_id and i.get("x") is not None]
        if others:
            with Tx(conn):
                audit(conn, p, "location.view_all", floor_id, {"devices": len(others)}, client_ip(request))
    return items


@router.get("/locations/history/{device_id}")
def location_history(device_id: str, request: Request, limit: int = 200, p: Principal = Depends(current_user),
                     conn: sqlite3.Connection = Depends(get_conn)):
    d = services.device_row(conn, device_id)
    if d["owner_user_id"] != p.user_id and not p.can("location.view_all"):
        raise HTTPException(404, "Device not found")
    if d["owner_user_id"] != p.user_id:
        if d["type"] == "phone" and not d["sharing_enabled"]:
            raise HTTPException(403, "The owner is not sharing this phone's location")
        with Tx(conn):
            audit(conn, p, "location.history_view", device_id, {}, client_ip(request))
    return rows(conn.execute(
        "SELECT id, received_at, observed_at, kind, floor_id, room_id, x, y, lat, lon, accuracy_m, source, confidence, "
        "flags FROM location_updates WHERE device_id = ? ORDER BY observed_at DESC LIMIT ?",
        (device_id, min(max(limit, 1), 1000))), ("flags",))
