"""Health, overview, topology, events, audit, settings, reports, diagnostics and the WebSocket."""
from __future__ import annotations

import csv
import io
import json
import platform
import sqlite3
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import __version__, positioning as pos, services
from ..db import DEFAULT_SETTINGS, SETTING_CHOICES, Tx, all_settings, connect, new_id, now_iso, rows, utcnow
from ..netinfo import current_wifi, public_networks
from ..retention import run_retention
from ..security import (SESSION_COOKIE, Principal, audit, client_ip, current_user, get_conn, require, resolve_token)

router = APIRouter(prefix="/api/v1", tags=["system"])


class SettingsPatch(BaseModel):
    values: dict[str, Any]


class LinkIn(BaseModel):
    from_node: str = Field(max_length=60)
    to_node: str = Field(max_length=60)
    medium: str = Field(pattern="^(wifi|ethernet|ble|esp_mesh|usb|other)$")
    note: str | None = Field(default=None, max_length=200)


@router.get("/health")
def health(request: Request):
    """Unauthenticated liveness probe; reveals no data."""
    try:
        conn = connect(request.app.state.settings.db_path)
        conn.execute("SELECT 1").fetchone()
        conn.close()
        db = "ok"
    except sqlite3.Error:
        db = "error"
    return {"status": "ok" if db == "ok" else "degraded", "database": db, "server_time": now_iso(),
            "version": __version__}


@router.get("/config")
def public_config(request: Request, p: Principal = Depends(current_user)):
    from ..bootstrap import lan_ips
    cfg = request.app.state.settings
    ips = lan_ips()
    https = (cfg.data_dir / "tls" / "cert.pem").exists()
    host = ips[0] if ips else request.url.hostname
    return {"live_s": cfg.live_s, "recent_s": cfg.recent_s, "stale_s": cfg.stale_s,
            "low_confidence_m": cfg.low_confidence_m, "device_offline_s": cfg.device_offline_s, "version": __version__,            "lan_ips": ips, "https": https,
            # Addresses to hand to other devices: ESP32 boards use plain HTTP, phones the HTTPS port.
            "device_url": f"http://{host}:{cfg.http_port}",
            "phone_url": f"https://{host}:{cfg.https_port}" if https else f"http://{host}:{cfg.http_port}",
            # Only device installers need these (they run Windows tools, so other users skip them).
            "wifi": current_wifi() if p.can("device.enroll") else None,
            "public_networks": public_networks() if p.can("device.enroll") else []}


@router.get("/overview")
def overview(request: Request, p: Principal = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    cfg = request.app.state.settings
    now = utcnow()
    if p.can("device.view_all"):
        devs = rows(conn.execute("SELECT * FROM devices WHERE revoked_at IS NULL"))
    else:
        devs = rows(conn.execute("SELECT * FROM devices WHERE revoked_at IS NULL AND owner_user_id = ?", (p.user_id,)))
    online = sum(1 for d in devs if pos.device_online(d, cfg, now) is True)
    offline = sum(1 for d in devs if pos.device_online(d, cfg, now) is False)
    by_type: dict[str, int] = {}
    for d in devs:
        by_type[d["type"]] = by_type.get(d["type"], 0) + 1
    low_batt = [{"id": d["id"], "name": d["name"], "battery_pct": d["battery_pct"]} for d in devs
                if d["battery_pct"] is not None and d["battery_pct"] <= 20]
    inc_where = "" if p.can("incident.view_all") else " AND reporter_user_id = ?"
    inc_args = () if p.can("incident.view_all") else (p.user_id,)
    active = conn.execute(f"SELECT COUNT(*) AS n FROM incidents WHERE state IN {services.ACTIVE_INCIDENT_STATES} "
                          f"AND is_test = 0{inc_where}", inc_args).fetchone()["n"]
    active_tests = conn.execute(f"SELECT COUNT(*) AS n FROM incidents WHERE state IN {services.ACTIVE_INCIDENT_STATES} "
                                f"AND is_test = 1{inc_where}", inc_args).fetchone()["n"]
    locs = services.visible_location_items(conn, cfg, p)
    latest = sorted([l for l in locs if l.get("observed_at")], key=lambda l: l["observed_at"], reverse=True)[:8]
    cams = [d for d in devs if d["type"] == "camera_node"]
    out = {
        "devices": {"registered": len(devs), "online": online, "offline": offline,
                    "no_heartbeat_expected": len(devs) - online - offline, "by_type": by_type},
        "incidents": {"active": active, "active_tests": active_tests},
        "locations": {"live": sum(1 for l in locs if l["is_live"]), "visible": len(locs), "latest": latest},
        "battery_warnings": low_batt,
        "cameras": [{"id": c["id"], "name": c["name"], "online": pos.device_online(c, cfg, now),
                     "camera_ok": c["camera_ok"]} for c in cams],
        "backend": {"status": "ok", "server_time": now_iso(), "ws_clients": len(request.app.state.hub.clients),
                    "version": __version__},
    }
    if p.can("events.view"):
        out["recent_events"] = _events(conn, 10)
    return out


def _events(conn: sqlite3.Connection, limit: int) -> list[dict]:
    """Operational events (no login IPs or chat content)."""
    ev = rows(conn.execute(
        "SELECT a.at, a.action, a.target, a.detail, u.display_name AS actor_name, d.name AS device_name "
        "FROM audit_log a LEFT JOIN users u ON u.id = a.actor_user_id LEFT JOIN devices d ON d.id = a.actor_device_id "
        "WHERE a.action LIKE 'device.%' OR a.action LIKE 'incident.%' OR a.action LIKE 'map.%' "
        "OR a.action LIKE 'camera.%' OR a.action LIKE 'association.%' ORDER BY a.id DESC LIMIT ?", (limit,)),
        ("detail",))
    return ev


@router.get("/setup/checklist")
def setup_checklist(request: Request, p: Principal = Depends(require("settings.manage")),
                    conn: sqlite3.Connection = Depends(get_conn)):
    """What is still needed before SkyMesh is fully operational, computed from real state."""
    cfg = request.app.state.settings
    now = utcnow()
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    floors = one("SELECT COUNT(*) FROM floors f JOIN map_images i ON i.floor_id = f.id AND i.active = 1")
    drafts = one("SELECT COUNT(*) FROM map_features WHERE verified = 0")
    verified = one("SELECT COUNT(*) FROM map_features WHERE verified = 1")
    scaled = one("SELECT COUNT(*) FROM map_images WHERE active = 1 AND json_extract(transform, '$.metres_per_unit') IS NOT NULL")
    georef_floors = [r[0] for r in conn.execute("SELECT floor_id FROM georef_points GROUP BY floor_id HAVING COUNT(*) >= 3")]
    responders = one("SELECT COUNT(*) FROM users WHERE role IN ('responder', 'admin') AND active = 1")
    users_n = one("SELECT COUNT(*) FROM users WHERE active = 1")
    devs = rows(conn.execute("SELECT * FROM devices WHERE revoked_at IS NULL"))
    badges = [d for d in devs if d["type"] == "badge"]
    cams = [d for d in devs if d["type"] == "camera_node"]
    online = lambda ds: sum(1 for d in ds if pos.device_online(d, cfg, now))  # noqa: E731
    anchors = one("SELECT COUNT(*) FROM anchors WHERE kind = 'ble_anchor' AND identifier IS NOT NULL")
    tls = (cfg.data_dir / "tls" / "cert.pem").exists()
    trained_spots = one("SELECT COUNT(*) FROM training_sessions WHERE samples > 0")
    items = [
        {"id": "admin", "title": "Administrator account", "done": True, "detail": "Created", "app": "admin"},
        {"id": "https", "title": "HTTPS for phones", "done": tls,
         "detail": "Certificate present: phones use https://<this computer>:8443" if tls
         else "Start with `skymesh.py serve` (HTTPS is on by default)", "app": "help"},
        {"id": "network", "title": "Network allows devices in", "done": not public_networks(),
         "detail": ("Windows treats " + ", ".join(public_networks()) + " as Public, so the firewall blocks badges and phones. "
                    "Set it to Private: Settings > Network & internet > Wi-Fi > (network) > Private network.")
         if public_networks() else "Network profile is Private", "app": "help"},
        {"id": "floor", "title": "Floor plan imported", "done": floors > 0, "detail": f"{floors} floor(s) with an image",
         "app": "map"},
        {"id": "verify", "title": "Rooms and exits verified", "done": drafts == 0 and verified > 0,
         "detail": f"{verified} verified, {drafts} drafts waiting for your confirmation", "app": "map"},
        {"id": "scale", "title": "Map scale set", "done": scaled > 0 and scaled >= floors,
         "detail": f"{scaled} of {floors} floor(s) have a scale", "app": "map"},
        {"id": "trained", "title": "Map trained for badges (Map Trainer)", "done": trained_spots >= 3,
         "detail": f"{trained_spots} spot(s) trained: walk with a badge and press I'm here in each room", "app": "trainer"},
        {"id": "georef", "title": "GPS control points (for phone GPS)", "done": len(georef_floors) > 0,
         "detail": f"{len(georef_floors)} floor(s) georeferenced; Map Trainer > With this phone, 3 points outdoors",
         "app": "trainer", "optional": True},
        {"id": "responders", "title": "Responders added", "done": responders > 1,
         "detail": f"{responders} admin/responder account(s), {users_n} users in total", "app": "admin"},
        {"id": "badges", "title": "Badges enrolled and online", "done": bool(badges) and online(badges) > 0,
         "detail": f"{len(badges)} enrolled, {online(badges)} online", "app": "adddevice"},
        {"id": "cameras", "title": "Camera nodes enrolled and online", "done": bool(cams) and online(cams) > 0,
         "detail": f"{len(cams)} enrolled, {online(cams)} online", "app": "adddevice", "optional": True},
        {"id": "anchors", "title": "BLE anchors placed (optional with a trained map)", "done": anchors >= 1,
         "detail": f"{anchors} anchor(s) with an identifier", "app": "map", "optional": True},
    ]
    return {"items": items, "done": sum(i["done"] for i in items), "total": len(items)}


@router.get("/events")
def events(limit: int = 100, p: Principal = Depends(require("events.view")), conn: sqlite3.Connection = Depends(get_conn)):
    return _events(conn, min(max(limit, 1), 500))


@router.get("/audit")
def audit_log(limit: int = 200, action: str | None = None, p: Principal = Depends(require("audit.view")),
              conn: sqlite3.Connection = Depends(get_conn)):
    sql = ("SELECT a.*, u.display_name AS actor_name FROM audit_log a LEFT JOIN users u ON u.id = a.actor_user_id")
    args: list = []
    if action:
        sql += " WHERE a.action LIKE ?"
        args.append(action.replace("%", "") + "%")
    sql += " ORDER BY a.id DESC LIMIT ?"
    args.append(min(max(limit, 1), 1000))
    return rows(conn.execute(sql, args), ("detail",))


@router.get("/settings")
def get_settings(p: Principal = Depends(require("settings.manage")), conn: sqlite3.Connection = Depends(get_conn)):
    return all_settings(conn)


@router.patch("/settings")
def patch_settings(body: SettingsPatch, request: Request, p: Principal = Depends(require("settings.manage")),
                   conn: sqlite3.Connection = Depends(get_conn)):
    for k, v in body.values.items():
        if k not in DEFAULT_SETTINGS:
            raise HTTPException(422, f"Unknown setting {k}")
        default = DEFAULT_SETTINGS[k]
        if isinstance(default, bool):
            if not isinstance(v, bool):
                raise HTTPException(422, f"{k} must be true/false")
        elif isinstance(default, (int, float)):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or v > 100000:
                raise HTTPException(422, f"{k} must be a number 0..100000")
        elif k in SETTING_CHOICES and v not in SETTING_CHOICES[k]:
            raise HTTPException(422, f"{k} must be one of: {', '.join(SETTING_CHOICES[k])}")
    with Tx(conn):
        for k, v in body.values.items():
            conn.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?,?,?,?) ON CONFLICT(key) "
                         "DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                         (k, json.dumps(v), now_iso(), p.user_id))
        audit(conn, p, "settings.update", None, body.values, client_ip(request))
    return all_settings(conn)


@router.post("/retention/run")
def retention(request: Request, p: Principal = Depends(require("settings.manage")),
              conn: sqlite3.Connection = Depends(get_conn)):
    res = run_retention(conn, request.app.state.settings.data_dir / "photos")
    with Tx(conn):
        audit(conn, p, "retention.run", None, res, client_ip(request))
    return res


@router.get("/topology")
def topology(request: Request, p: Principal = Depends(require("topology.view")), conn: sqlite3.Connection = Depends(get_conn)):
    """Nodes and links, each link labelled configured / observed / unknown. Nothing is inferred beyond telemetry."""
    cfg = request.app.state.settings
    now = utcnow()
    nodes = [{"id": "backend", "label": "SkyMesh backend", "kind": "server", "status": "online"}]
    links = []
    aps = rows(conn.execute("SELECT * FROM anchors WHERE kind = 'wifi_ap'"))
    ap_by_bssid = {a["identifier"].lower(): a for a in aps if a["identifier"]}
    for a in aps:
        nodes.append({"id": a["id"], "label": a["name"], "kind": "wifi_ap", "status": "configured"})
    for d in rows(conn.execute("SELECT * FROM devices WHERE revoked_at IS NULL")):
        online = pos.device_online(d, cfg, now)
        nodes.append({"id": d["id"], "label": d["name"], "kind": d["type"],
                      "status": "online" if online else ("offline" if online is False else "no_heartbeat"),
                      "rssi": d["last_rssi"], "last_seen_at": d["last_seen_at"]})
        if d["last_seen_at"]:
            ap = ap_by_bssid.get((d["last_bssid"] or "").lower())
            via = ap["id"] if ap else None
            if via:
                links.append({"from": d["id"], "to": via, "type": "observed" if online else "last_observed",
                              "medium": "wifi", "rssi": d["last_rssi"], "at": d["last_seen_at"]})
            else:
                # The device reached the backend, but the path (which AP / hops) is not known.
                links.append({"from": d["id"], "to": "backend", "type": "observed_path_unknown" if online
                              else "last_observed_path_unknown", "medium": "ip", "rssi": d["last_rssi"],
                              "at": d["last_seen_at"], "bssid": d["last_bssid"]})
        elif d["type"] == "phone":
            links.append({"from": d["id"], "to": "backend", "type": "unknown", "medium": "ip"})
    for a in aps:
        links.append({"from": a["id"], "to": "backend", "type": "configured_assumed", "medium": "lan"})
    for l in rows(conn.execute("SELECT * FROM network_links")):
        links.append({"from": l["from_node"], "to": l["to_node"], "type": "configured", "medium": l["medium"],
                      "note": l["note"], "id": l["id"]})
    return {"nodes": nodes, "links": links}


@router.post("/network-links", status_code=201)
def add_link(body: LinkIn, request: Request, p: Principal = Depends(require("settings.manage")),
             conn: sqlite3.Connection = Depends(get_conn)):
    lid = new_id("lnk")
    with Tx(conn):
        conn.execute("INSERT INTO network_links (id, from_node, to_node, medium, note, created_at) VALUES (?,?,?,?,?,?)",
                     (lid, body.from_node, body.to_node, body.medium, body.note, now_iso()))
        audit(conn, p, "network.link_add", lid, body.model_dump(), client_ip(request))
    return {"id": lid}


@router.delete("/network-links/{link_id}")
def delete_link(link_id: str, request: Request, p: Principal = Depends(require("settings.manage")),
                conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        if conn.execute("DELETE FROM network_links WHERE id = ?", (link_id,)).rowcount == 0:
            raise HTTPException(404, "Not found")
        audit(conn, p, "network.link_delete", link_id, {}, client_ip(request))
    return {"ok": True}


@router.get("/reports/summary")
def report_summary(p: Principal = Depends(require("reports.view")), conn: sqlite3.Connection = Depends(get_conn)):
    by_state = {r["state"]: r["n"] for r in conn.execute(
        "SELECT state, COUNT(*) AS n FROM incidents WHERE is_test = 0 GROUP BY state")}
    by_source = {r["source"]: r["n"] for r in conn.execute(
        "SELECT source, COUNT(*) AS n FROM incidents WHERE is_test = 0 GROUP BY source")}
    ack = conn.execute(
        "SELECT AVG((julianday(acknowledged_at) - julianday(created_at)) * 86400) AS s, COUNT(*) AS n "
        "FROM incidents WHERE acknowledged_at IS NOT NULL AND is_test = 0").fetchone()
    return {"incidents_by_state": by_state, "incidents_by_source": by_source,
            "mean_seconds_to_acknowledge": round(ack["s"], 1) if ack["s"] is not None else None,
            "acknowledged_count": ack["n"],
            "tests": conn.execute("SELECT COUNT(*) AS n FROM incidents WHERE is_test = 1").fetchone()["n"]}


@router.get("/reports/incidents.csv")
def incidents_csv(request: Request, p: Principal = Depends(require("reports.view")),
                  conn: sqlite3.Connection = Depends(get_conn)):
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["id", "created_at", "source", "category", "severity", "state", "is_test", "location_note",
            "acknowledged_at", "resolved_at"]
    w.writerow(cols)
    for r in conn.execute(f"SELECT {', '.join(cols)} FROM incidents ORDER BY created_at DESC"):
        # Neutralise spreadsheet formula injection.
        w.writerow(["'" + str(v) if isinstance(v, str) and v[:1] in "=+-@" else v for v in r])
    with Tx(conn):
        audit(conn, p, "report.export", "incidents.csv", {}, client_ip(request))
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=skymesh-incidents.csv"})


@router.get("/diagnostics")
def diagnostics(request: Request, p: Principal = Depends(require("diagnostics.view")),
                conn: sqlite3.Connection = Depends(get_conn)):
    cfg = request.app.state.settings
    counts = {t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in (
        "users", "devices", "device_heartbeats", "location_updates", "messages", "incidents", "camera_observations",
        "audit_log", "map_features", "anchors")}
    return {"version": __version__, "python": platform.python_version(), "platform": platform.platform(),
            "database": {"path": str(cfg.db_path), "size_bytes": cfg.db_path.stat().st_size if cfg.db_path.exists() else 0,
                         "rows": counts,
                         "migrations": [r["version"] for r in conn.execute("SELECT version FROM schema_migrations")]},
            "thresholds": {"live_s": cfg.live_s, "recent_s": cfg.recent_s, "stale_s": cfg.stale_s,
                           "device_offline_s": cfg.device_offline_s, "low_confidence_m": cfg.low_confidence_m},
            "realtime": {"clients": len(request.app.state.hub.clients)},
            "live_analysis": request.app.state.live_analyzer.status,
            "detector": getattr(request.app.state.live_analyzer.detector, "name", "not loaded yet"),
            "secure_cookies": cfg.secure_cookies, "rate_limits": request.app.state.limiter.enabled}


@router.websocket("/ws")
async def ws(websocket: WebSocket):
    """Live updates. Authenticated by the session cookie, or ?token= for native clients."""
    app = websocket.app
    origin = websocket.headers.get("origin")
    if origin and urlparse(origin).netloc != websocket.headers.get("host", ""):
        await websocket.close(code=4403)  # cross-site WebSocket hijacking guard
        return
    token =websocket.cookies.get(SESSION_COOKIE) or websocket.query_params.get("token")
    conn = connect(app.state.settings.db_path)
    try:
        principal = resolve_token(conn, token)
    finally:
        conn.close()
    if principal is None or principal.kind != "user":
        await websocket.close(code=4401)
        return
    await websocket.accept()
    await app.state.hub.serve(websocket, principal)
