"""SOS and incident lifecycle."""
from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import services
from ..db import Tx, get_setting, new_id, now_iso, rows
from ..security import Principal, audit, client_ip, current_device, current_user, get_conn, rate_limit, require

router = APIRouter(prefix="/api/v1", tags=["incidents"])

TRANSITIONS: dict[str, set[str]] = {
    "NEW": {"ACKNOWLEDGED", "FALSE_ALARM"},
    "ACKNOWLEDGED": {"RESPONDING", "RESOLVED", "FALSE_ALARM"},
    "RESPONDING": {"RESOLVED", "FALSE_ALARM"},
    "RESOLVED": set(),
    "FALSE_ALARM": set(),
}

Severity = Literal["low", "medium", "high", "critical"]


class PhoneSosIn(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_\-:.]+$")
    device_id: str | None = Field(default=None, max_length=40)
    category: str = Field(default="sos", max_length=40)
    message: str | None = Field(default=None, max_length=500)
    timestamp: str | None = Field(default=None, max_length=40)


class BadgeSosIn(BaseModel):
    schema_version: int = Field(ge=1, le=1)
    event_id: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_\-.]+$")
    timestamp: str | None = Field(default=None, max_length=40)
    uptime_ms: int | None = Field(default=None, ge=0)
    category: str = Field(default="sos", max_length=40)


class TestIn(BaseModel):
    message: str | None = Field(default=None, max_length=500)
    device_id: str | None = Field(default=None, max_length=40)
    severity: Severity = "medium"


class TransitionIn(BaseModel):
    to_state: Literal["ACKNOWLEDGED", "RESPONDING", "RESOLVED", "FALSE_ALARM"]
    note: str | None = Field(default=None, max_length=1000)


class AssignIn(BaseModel):
    user_id: str = Field(max_length=40)


class NoteIn(BaseModel):
    note: str = Field(min_length=1, max_length=1000)


@router.post("/incidents/sos", dependencies=[Depends(rate_limit("sos", 10, 60))])
def phone_sos(body: PhoneSosIn, request: Request, response: Response, p: Principal = Depends(current_user),
              conn: sqlite3.Connection = Depends(get_conn)):
    """SOS from a signed-in user (phone or browser). Retrying with the same key never duplicates."""
    cfg = request.app.state.settings
    with Tx(conn):
        own = [r["id"] for r in conn.execute(
            "SELECT id FROM devices WHERE owner_user_id = ? AND revoked_at IS NULL AND type IN ('phone','badge')",
            (p.user_id,))]
        if body.device_id and body.device_id not in own:
            raise HTTPException(404, "Device not found")
        dev_time, _ = services.accept_timestamp(body.timestamp, cfg, services.utcnow())
        inc, outcome = services.raise_sos(
            conn, request.app.state.hub, cfg, idempotency_key=f"user:{p.user_id}:{body.idempotency_key}", actor=p,
            open_incident_where=("reporter_user_id = ? AND source = 'phone'", (p.user_id,)),
            source="phone", category=body.category, severity="critical", reporter_user_id=p.user_id,
            device_id=body.device_id, location_device_ids=[body.device_id] if body.device_id else own,
            device_timestamp=dev_time, message=body.message, is_test=False)
        if outcome != "duplicate":
            audit(conn, p, "incident.sos", inc["id"], {"source": "phone", "outcome": outcome}, client_ip(request))
    response.status_code = 201 if outcome != "duplicate" else 200
    return {"incident": inc, "duplicate": outcome == "duplicate", "repeated": outcome == "repeated"}


@router.post("/device/sos", dependencies=[Depends(rate_limit("device_sos", 20, 60))])
def badge_sos(body: BadgeSosIn, request: Request, response: Response, dev: Principal = Depends(current_device),
              conn: sqlite3.Connection = Depends(get_conn)):
    if dev.device_type != "badge":
        raise HTTPException(403, "Only badges can raise device SOS events")
    cfg = request.app.state.settings
    with Tx(conn):
        dev_time, flags = services.accept_timestamp(body.timestamp, cfg, services.utcnow())
        inc, outcome = services.raise_sos(
            conn, request.app.state.hub, cfg, idempotency_key=f"{dev.device_id}:{body.event_id}", actor=dev,
            open_incident_where=("device_id = ? AND source = 'badge'", (dev.device_id,)),
            source="badge", category=body.category, severity="critical", reporter_user_id=dev.user_id,
            device_id=dev.device_id, location_device_ids=[dev.device_id],
            device_timestamp=None if flags else dev_time, message=None, is_test=False)
        if outcome != "duplicate":
            audit(conn, dev, "incident.sos", inc["id"], {"source": "badge", "event_id": body.event_id, "outcome": outcome})
    response.status_code = 201 if outcome != "duplicate" else 200
    return {"event_id": body.event_id, "incident_id": inc["id"], "state": inc["state"],
            "duplicate": outcome == "duplicate", "repeated": outcome == "repeated",
            "confirmed": inc["state"] in ("ACKNOWLEDGED", "RESPONDING", "RESOLVED", "FALSE_ALARM"),
            # A fresh scene photo with every press (badges without a camera simply ignore this).
            "photo_wanted": bool(outcome != "duplicate" and get_setting(conn, "badge.photo_on_sos")),
            **badge_sos_config(conn)}


def badge_sos_config(conn: sqlite3.Connection) -> dict:
    """Button timing the badge applies (sent with SOS replies and every heartbeat)."""
    return {"sos_hold_ms": int(get_setting(conn, "badge.sos_hold_ms")),
            "sos_lockout_s": float(get_setting(conn, "badge.sos_lockout_s"))}


@router.post("/incidents/test", status_code=201)
def test_incident(body: TestIn, request: Request, p: Principal = Depends(require("incident.test")),
                  conn: sqlite3.Connection = Depends(get_conn)):
    """Clearly flagged test incident; excluded from SOS markers and active-incident counts."""
    with Tx(conn):
        inc, _ = services.create_incident(
            conn, request.app.state.hub, request.app.state.settings, source="dashboard_test",
            idempotency_key=new_id("test"), category="test", severity=body.severity, reporter_user_id=p.user_id,
            device_id=body.device_id, location_device_ids=[body.device_id] if body.device_id else [],
            device_timestamp=None, message=body.message, is_test=True, actor=p)
        audit(conn, p, "incident.test", inc["id"], {}, client_ip(request))
    return {"incident": inc}


@router.get("/incidents")
def list_incidents(active: bool = False, include_tests: bool = True, limit: int = 100,
                   p: Principal = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    where, args = [], []
    if active:
        where.append(f"state IN {services.ACTIVE_INCIDENT_STATES}")
    if not include_tests:
        where.append("is_test = 0")
    if not p.can("incident.view_all"):
        where.append("reporter_user_id = ?")
        args.append(p.user_id)
    sql = "SELECT id FROM incidents" + (" WHERE " + " AND ".join(where) if where else "") + \
          " ORDER BY created_at DESC LIMIT ?"
    ids = [r["id"] for r in conn.execute(sql, (*args, min(max(limit, 1), 500)))]
    return [services.get_incident(conn, i) for i in ids]


def _visible_incident(conn: sqlite3.Connection, p: Principal, incident_id: str) -> dict:
    inc = services.get_incident(conn, incident_id)
    if not p.can("incident.view_all") and inc["reporter_user_id"] != p.user_id:
        raise HTTPException(404, "Incident not found")
    return inc


@router.get("/incidents/{incident_id}")
def get_incident(incident_id: str, p: Principal = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    inc = _visible_incident(conn, p, incident_id)
    inc["timeline"] = rows(conn.execute(
        "SELECT e.*, u.display_name AS actor_name FROM incident_events e LEFT JOIN users u ON u.id = e.actor_user_id "
        "WHERE incident_id = ? ORDER BY e.id", (incident_id,)))
    return inc


@router.get("/incidents/{incident_id}/evidence")
def evidence(incident_id: str, request: Request, p: Principal = Depends(require("incident.view_all")),
             conn: sqlite3.Connection = Depends(get_conn)):
    """Snapshot saved when a camera raised an automatic alert (viewing is audited)."""
    services.get_incident(conn, incident_id)
    path = request.app.state.settings.data_dir / "evidence" / f"{incident_id}.jpg"
    if not path.is_file():
        raise HTTPException(404, "No snapshot for this incident")
    with Tx(conn):
        audit(conn, p, "incident.evidence_view", incident_id, {}, client_ip(request))
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/incidents/{incident_id}/transition")
def transition(incident_id: str, body: TransitionIn, request: Request, p: Principal = Depends(require("incident.manage")),
               conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        inc = services.get_incident(conn, incident_id)
        if body.to_state not in TRANSITIONS[inc["state"]]:
            raise HTTPException(409, f"Cannot move incident from {inc['state']} to {body.to_state}")
        now = now_iso()
        sets = {"state": body.to_state, "updated_at": now}
        if body.to_state == "ACKNOWLEDGED":
            sets.update(acknowledged_by=p.user_id, acknowledged_at=now)
        if body.to_state in ("RESOLVED", "FALSE_ALARM"):
            sets.update(resolved_by=p.user_id, resolved_at=now)
        conn.execute(f"UPDATE incidents SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                     (*sets.values(), incident_id))
        services.add_incident_event(conn, incident_id, p, "state_change", inc["state"], body.to_state, body.note)
        audit(conn, p, "incident.transition", incident_id, {"to": body.to_state}, client_ip(request))
        inc = services.get_incident(conn, incident_id)
    request.app.state.hub.publish("incident", inc, services.incident_audience(inc))
    return inc


@router.post("/incidents/{incident_id}/assign")
def assign(incident_id: str, body: AssignIn, request: Request, p: Principal = Depends(require("incident.manage")),
           conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        inc = services.get_incident(conn, incident_id)
        if inc["state"] in ("RESOLVED", "FALSE_ALARM"):
            raise HTTPException(409, "Incident is closed")
        u = conn.execute("SELECT display_name, role FROM users WHERE id = ? AND active = 1", (body.user_id,)).fetchone()
        if not u or u["role"] not in ("admin", "responder"):
            raise HTTPException(422, "Assignee must be an active responder or administrator")
        conn.execute("UPDATE incidents SET assigned_to = ?, updated_at = ? WHERE id = ?",
                     (body.user_id, now_iso(), incident_id))
        services.add_incident_event(conn, incident_id, p, "assigned", None, None, f"Assigned to {u['display_name']}")
        audit(conn, p, "incident.assign", incident_id, {"assignee": body.user_id}, client_ip(request))
        inc = services.get_incident(conn, incident_id)
    request.app.state.hub.publish("incident", inc, services.incident_audience(inc))
    return inc


@router.post("/incidents/{incident_id}/notes")
def add_note(incident_id: str, body: NoteIn, request: Request, p: Principal = Depends(current_user),
             conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        inc = _visible_incident(conn, p, incident_id)
        services.add_incident_event(conn, incident_id, p, "note", None, None, body.note)
        conn.execute("UPDATE incidents SET updated_at = ? WHERE id = ?", (now_iso(), incident_id))
    request.app.state.hub.publish("incident", services.get_incident(conn, incident_id), services.incident_audience(inc))
    return {"ok": True}
