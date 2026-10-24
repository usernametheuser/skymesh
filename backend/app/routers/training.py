"""Map trainer: "I'm here" captures that teach the map what each spot sounds like to a badge."""
from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import fingerprint as fp, positioning as pos, services
from ..db import Tx, new_id, now_iso, row
from ..realtime import perm
from ..security import Principal, audit, client_ip, get_conn, require

router = APIRouter(prefix="/api/v1", tags=["training"])


class SessionIn(BaseModel):
    device_id: str = Field(max_length=40)
    floor_id: str = Field(max_length=40)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    samples: int = Field(default=4, ge=2, le=20)


def _session_out(conn: sqlite3.Connection, s: dict) -> dict:
    out = dict(s)
    if s["room_id"]:
        r = conn.execute("SELECT name FROM map_features WHERE id = ?", (s["room_id"],)).fetchone()
        out["room_name"] = r["name"] if r else None
    else:
        out["room_name"] = None
    return out


@router.post("/training/sessions", status_code=201)
def start_session(body: SessionIn, request: Request, p: Principal = Depends(require("map.train")),
                  conn: sqlite3.Connection = Depends(get_conn)):
    """Start capturing the spot where the person holding the badge says they are."""
    cfg = request.app.state.settings
    with Tx(conn):
        services.require_floor(conn, body.floor_id)
        d = services.device_row(conn, body.device_id)
        if d["type"] != "badge" or d["revoked_at"]:
            raise HTTPException(422, "Choose an active badge. Phones cannot scan Wi-Fi from a browser: use GPS calibration.")
        conn.execute("UPDATE training_sessions SET ended_at = ? WHERE device_id = ? AND ended_at IS NULL",
                     (now_iso(), body.device_id))
        area = pos.find_area(services.floor_features(conn, body.floor_id), body.x, body.y)
        sid = new_id("trn")
        conn.execute("INSERT INTO training_sessions (id, device_id, floor_id, x, y, room_id, target, started_by, started_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", (sid, body.device_id, body.floor_id, body.x, body.y,
                                                    area["id"] if area else None, body.samples, p.user_id, now_iso()))
        audit(conn, p, "map.train_spot", sid, {"device_id": body.device_id, "floor_id": body.floor_id}, client_ip(request))
        s = _session_out(conn, row(conn.execute("SELECT * FROM training_sessions WHERE id = ?", (sid,)).fetchone()))
    s["badge_online"] = bool(pos.device_online(d, cfg, services.utcnow()))
    s["badge_scans_wifi"] = bool((d["capabilities"] or {}).get("wifi_scan"))
    return s


@router.get("/training/sessions/{session_id}")
def get_session(session_id: str, p: Principal = Depends(require("map.train")),
                conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        fp.expire_old(conn)
        s = row(conn.execute("SELECT * FROM training_sessions WHERE id = ?", (session_id,)).fetchone())
    if not s:
        raise HTTPException(404, "Training spot not found")
    return _session_out(conn, s)


@router.post("/training/sessions/{session_id}/stop")
def stop_session(session_id: str, p: Principal = Depends(require("map.train")),
                 conn: sqlite3.Connection = Depends(get_conn)):
    """Finish early, keeping whatever samples were captured (a spot with none is removed)."""
    with Tx(conn):
        s = row(conn.execute("SELECT * FROM training_sessions WHERE id = ?", (session_id,)).fetchone())
        if not s:
            raise HTTPException(404, "Training spot not found")
        if s["samples"] == 0:
            conn.execute("DELETE FROM training_sessions WHERE id = ?", (session_id,))
            return {"ok": True, "kept": False}
        conn.execute("UPDATE training_sessions SET ended_at = COALESCE(ended_at, ?) WHERE id = ?", (now_iso(), session_id))
    return {"ok": True, "kept": True}


@router.delete("/training/sessions/{session_id}")
def delete_session(session_id: str, request: Request, p: Principal = Depends(require("map.train")),
                   conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        s = row(conn.execute("SELECT * FROM training_sessions WHERE id = ?", (session_id,)).fetchone())
        if not s:
            raise HTTPException(404, "Training spot not found")
        conn.execute("DELETE FROM fingerprints WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM training_sessions WHERE id = ?", (session_id,))
        audit(conn, p, "map.train_spot_delete", session_id, {}, client_ip(request))
    fp._cache.invalidate()
    request.app.state.hub.publish("map_changed", {"floor_id": s["floor_id"], "what": "training"}, perm("map.train"))
    return {"ok": True}


@router.get("/floors/{floor_id}/training")
def floor_training(floor_id: str, p: Principal = Depends(require("map.train")),
                   conn: sqlite3.Connection = Depends(get_conn)):
    services.require_floor(conn, floor_id)
    spots = fp.spots_summary(conn, floor_id)
    signals = set()
    for r in conn.execute("SELECT vector FROM fingerprints WHERE floor_id = ?", (floor_id,)):
        signals.update(json.loads(r["vector"]).keys())
    rooms = {s["room_id"] for s in spots if s["room_id"]}
    n_rooms = conn.execute("SELECT COUNT(*) AS n FROM map_features WHERE floor_id = ? AND kind = 'room'",
                           (floor_id,)).fetchone()["n"]
    return {"spots": spots, "signals": len(signals), "rooms_trained": len(rooms), "rooms_total": n_rooms,
            "scale_m": services.floor_scale(conn, floor_id)}


@router.delete("/floors/{floor_id}/training")
def clear_floor_training(floor_id: str, request: Request, p: Principal = Depends(require("map.edit")),
                         conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        services.require_floor(conn, floor_id)
        n = conn.execute("DELETE FROM fingerprints WHERE floor_id = ?", (floor_id,)).rowcount
        conn.execute("DELETE FROM training_sessions WHERE floor_id = ?", (floor_id,))
        audit(conn, p, "map.train_clear", floor_id, {"samples": n}, client_ip(request))
    fp._cache.invalidate()
    request.app.state.hub.publish("map_changed", {"floor_id": floor_id, "what": "training"}, perm("map.train"))
    return {"ok": True, "deleted_samples": n}
