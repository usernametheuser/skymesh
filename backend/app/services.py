"""Domain logic shared by routers: location ingestion/visibility and incident creation."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException

from . import fingerprint as fp, positioning as pos
from .config import Settings
from .db import get_setting, iso, new_id, now_iso, parse_iso, row, rows, utcnow
from .realtime import Hub, perm, users
from .security import Principal

ACTIVE_INCIDENT_STATES = ("NEW", "ACKNOWLEDGED", "RESPONDING")


# ---------------------------------------------------------------- time handling

def accept_timestamp(client_ts: str | None, cfg: Settings, now: datetime) -> tuple[str, list[str]]:
    """Use the client's observation time only when it is plausible; otherwise server time + flag."""
    if not client_ts:
        return iso(now), ["server_timestamp"]
    try:
        ts = parse_iso(client_ts)
    except ValueError:
        return iso(now), ["invalid_client_timestamp"]
    if ts > now + timedelta(seconds=cfg.max_clock_skew_s):
        return iso(now), ["client_clock_ahead"]
    if ts < now - timedelta(seconds=cfg.max_backdate_s):
        return iso(now), ["client_timestamp_too_old"]
    return iso(ts), []


# ---------------------------------------------------------------- map lookups

def floor_features(conn: sqlite3.Connection, floor_id: str) -> list[dict]:
    return rows(conn.execute("SELECT * FROM map_features WHERE floor_id = ?", (floor_id,)), ("geometry", "properties"))


def floor_scale(conn: sqlite3.Connection, floor_id: str) -> float | None:
    r = conn.execute("SELECT transform FROM map_images WHERE floor_id = ? AND active = 1", (floor_id,)).fetchone()
    if not r:
        return None
    return json.loads(r["transform"]).get("metres_per_unit")


def geotransform_for(conn: sqlite3.Connection, floor_id: str) -> pos.GeoTransform | None:
    pts = rows(conn.execute("SELECT lat, lon, x, y FROM georef_points WHERE floor_id = ?", (floor_id,)))
    return pos.fit_geotransform(pts)


def require_floor(conn: sqlite3.Connection, floor_id: str) -> dict:
    f = row(conn.execute("SELECT * FROM floors WHERE id = ?", (floor_id,)).fetchone())
    if not f:
        raise HTTPException(422, f"Unknown floor {floor_id}")
    return f


# ---------------------------------------------------------------- location ingestion

def ingest_location(conn: sqlite3.Connection, hub: Hub, cfg: Settings, device: dict, p: dict[str, Any]) -> dict:
    """Validate, normalise and store one location report. Returns the stored update.

    `p` keys: kind (floor|geo|room|unavailable|denied), timestamp, sequence, floor_id, x, y,
    lat, lon, accuracy_m, room_id, source, confidence, ble (list of {id, rssi}).
    """
    now = utcnow()
    observed_at, flags = accept_timestamp(p.get("timestamp"), cfg, now)
    seq = p.get("sequence")
    if seq is not None:
        dup = conn.execute(
            "SELECT id FROM location_updates WHERE device_id = ? AND sequence = ? AND received_at > ?",
            (device["id"], seq, iso(now - timedelta(hours=6)))).fetchone()
        if dup:
            return {"id": dup["id"], "duplicate": True}

    kind = p["kind"]
    rec: dict[str, Any] = {"floor_id": None, "room_id": None, "x": None, "y": None, "lat": None, "lon": None,
                           "accuracy_m": p.get("accuracy_m"), "source": p.get("source") or kind,
                           "confidence": p.get("confidence")}

    training = None
    if kind == "ble":
        readings = (p.get("ble") or []) + (p.get("wifi") or [])
        vec = fp.vector(readings)
        session = fp.active_session(conn, device["id"])
        fpm = None if session else fp.match(conn, vec, lambda f: floor_scale(conn, f))
        if session:
            # Map trainer: the person said "I'm here", so this scan is stored as a fingerprint of that spot
            # and the badge is shown where they said they are.
            training = fp.add_sample(conn, session, vec)
            rec.update(floor_id=session["floor_id"], x=session["x"], y=session["y"], room_id=session["room_id"],
                       source="map_trainer", accuracy_m=None, confidence=None)
            flags.append("confirmed_by_person")
            kind = "floor"
            hub.publish("training", {k: training[k] for k in ("id", "device_id", "floor_id", "samples", "target",
                                                              "accepted", "done", "heard")}
                        | {"reason": training.get("reason")}, perm("map.train"))
            if training["done"]:
                hub.publish("map_changed", {"floor_id": session["floor_id"], "what": "training"}, perm("map.train"))
        elif fpm is not None and fpm["confidence"] >= 0.15:
            rec.update(floor_id=fpm["floor_id"], x=fpm["x"], y=fpm["y"], room_id=fpm["room_id"],
                       accuracy_m=fpm["accuracy_m"], confidence=fpm["confidence"], source="wifi_fingerprint")
            flags += ["estimate", f"spots_used:{fpm['spots_used']}", f"signal_diff_db:{fpm['signal_distance_db']}"]
            kind = "floor"
    if kind == "ble":
        # Camera nodes advertise a beacon too, so a placed camera doubles as a positioning anchor.
        anchors = rows(conn.execute("SELECT * FROM anchors WHERE kind IN ('ble_anchor', 'camera_node') AND identifier IS NOT NULL"))
        est = pos.estimate_from_ble(p.get("ble") or [], anchors,
                                    floor_scale(conn, anchors[0]["floor_id"]) if anchors else None)
        if est["kind"] == "unavailable":
            kind = "unavailable"
            rec["source"] = "ble"
            flags.append(est["reason"])
        else:
            rec.update(floor_id=est["floor_id"], x=est["x"], y=est["y"], accuracy_m=est["accuracy_m"],
                       confidence=est["confidence"], source=est["source"])
            flags.append(f"anchors_used:{est['anchors_used']}")
            kind = "floor"
            if est["kind"] == "nearest":
                flags.append("nearest_node_only")
                area = pos.find_area(floor_features(conn, est["floor_id"]), est["x"], est["y"])
                if area and area["kind"] == "room":
                    # Only room-level is supportable: report the room, not a point.
                    kind = "room"
                    rec["room_id"] = area["id"]
                    rec["x"], rec["y"] = pos.polygon_centroid(area["geometry"])
            else:
                flags.append("estimate")

    elif p["kind"] == "floor":
        require_floor(conn, p["floor_id"])
        rec.update(floor_id=p["floor_id"], x=p["x"], y=p["y"])

    elif kind == "geo":
        rec.update(lat=p["lat"], lon=p["lon"], source=p.get("source") or "gps")
        floor_id = p.get("floor_id")
        if not floor_id:
            # Phones rarely know their floor. With exactly one GPS-calibrated floor, that is the one.
            ready = [r["floor_id"] for r in conn.execute("SELECT DISTINCT floor_id FROM georef_points")
                     if geotransform_for(conn, r["floor_id"]) is not None]
            if len(ready) == 1:
                floor_id = ready[0]
                flags.append("only_calibrated_floor")
        if floor_id:
            require_floor(conn, floor_id)
            rec["floor_id"] = floor_id
            gt = geotransform_for(conn, floor_id)
            if gt is None:
                flags.append("floor_not_georeferenced")
            else:
                x, y = gt.apply(p["lat"], p["lon"])
                if 0 <= x <= 1 and 0 <= y <= 1:
                    rec["x"], rec["y"] = x, y
                    kind = "floor"
                    flags.append("projected_from_gps")
                else:
                    flags.append("outside_floor_plan")
        else:
            flags.append("no_floor_selected")

    elif kind == "room":
        f = row(conn.execute("SELECT * FROM map_features WHERE id = ? AND geometry_type = 'polygon'",
                             (p["room_id"],)).fetchone(), ("geometry",))
        if not f:
            raise HTTPException(422, "Unknown room/area")
        rec.update(floor_id=f["floor_id"], room_id=f["id"], source=p.get("source") or "manual_room")
        rec["x"], rec["y"] = pos.polygon_centroid(f["geometry"])
        rec["accuracy_m"] = None
        flags.append("room_level_only")

    if kind == "floor" and rec["x"] is not None and rec["room_id"] is None:
        area = pos.find_area(floor_features(conn, rec["floor_id"]), rec["x"], rec["y"])
        if area:
            rec["room_id"] = area["id"]
            if not area["verified"]:
                flags.append("area_unverified")

    cur = conn.execute(
        "INSERT INTO location_updates (device_id, received_at, observed_at, kind, floor_id, room_id, x, y, lat, lon, "
        "accuracy_m, source, confidence, sequence, flags) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (device["id"], iso(now), observed_at, kind, rec["floor_id"], rec["room_id"], rec["x"], rec["y"], rec["lat"],
         rec["lon"], rec["accuracy_m"], rec["source"], rec["confidence"], seq, json.dumps(flags)))
    upd_id = cur.lastrowid

    current = conn.execute("SELECT observed_at FROM current_locations WHERE device_id = ?", (device["id"],)).fetchone()
    if current and parse_iso(current["observed_at"]) > parse_iso(observed_at):
        flags.append("out_of_order_not_current")
        conn.execute("UPDATE location_updates SET flags = ? WHERE id = ?", (json.dumps(flags), upd_id))
    else:
        conn.execute(
            "INSERT INTO current_locations (device_id, update_id, received_at, observed_at, kind, floor_id, room_id, x, y, "
            "lat, lon, accuracy_m, source, confidence, flags) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(device_id) DO UPDATE SET update_id=excluded.update_id, received_at=excluded.received_at, "
            "observed_at=excluded.observed_at, kind=excluded.kind, floor_id=excluded.floor_id, room_id=excluded.room_id, "
            "x=excluded.x, y=excluded.y, lat=excluded.lat, lon=excluded.lon, accuracy_m=excluded.accuracy_m, "
            "source=excluded.source, confidence=excluded.confidence, flags=excluded.flags",
            (device["id"], upd_id, iso(now), observed_at, kind, rec["floor_id"], rec["room_id"], rec["x"], rec["y"],
             rec["lat"], rec["lon"], rec["accuracy_m"], rec["source"], rec["confidence"], json.dumps(flags)))
        item = describe_location(conn, cfg, device_row(conn, device["id"]))
        hub.publish("location", item, perm("location.view_all", device["owner_user_id"]))
    out = {"id": upd_id, "duplicate": False, "kind": kind, "observed_at": observed_at, "flags": flags,
           "floor_id": rec["floor_id"], "room_id": rec["room_id"], "x": rec["x"], "y": rec["y"],
           "source": rec["source"], "accuracy_m": rec["accuracy_m"], "confidence": rec["confidence"]}
    if training is not None:
        out["training"] = {"samples": training["samples"], "target": training["target"], "done": training["done"]}
    if p["kind"] == "ble":
        # Badges scan fast while someone is training a spot with them, slowly otherwise (battery).
        out["scan_interval_s"] = 3 if training is not None and not training["done"] else 10
    return out


def device_row(conn: sqlite3.Connection, device_id: str) -> dict:
    d = row(conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone(), ("capabilities",))
    if not d:
        raise HTTPException(404, "Device not found")
    return d


def describe_location(conn: sqlite3.Connection, cfg: Settings, device: dict, now: datetime | None = None,
                      reveal: bool = True) -> dict:
    now = now or utcnow()
    loc = row(conn.execute("SELECT * FROM current_locations WHERE device_id = ?", (device["id"],)).fetchone(),
              ("flags",))
    st = pos.location_state(device, loc, cfg, now)
    owner = None
    if device["owner_user_id"]:
        o = conn.execute("SELECT display_name FROM users WHERE id = ?", (device["owner_user_id"],)).fetchone()
        owner = o["display_name"] if o else None
    sos = conn.execute(
        f"SELECT id FROM incidents WHERE device_id = ? AND state IN {ACTIVE_INCIDENT_STATES} AND is_test = 0 LIMIT 1",
        (device["id"],)).fetchone()
    item: dict[str, Any] = {
        "device_id": device["id"], "device_name": device["name"], "device_type": device["type"],
        "owner_user_id": device["owner_user_id"], "owner_name": owner,
        "online": pos.device_online(device, cfg, now), "battery_pct": device["battery_pct"],
        "battery_mv": device["battery_mv"], "sos_incident_id": sos["id"] if sos else None, **st,
    }
    hide = st["state"] in (pos.SHARING_DISABLED, pos.PERMISSION_DENIED) or not reveal
    if loc and not hide:
        room_name = None
        if loc["room_id"]:
            r = conn.execute("SELECT name FROM map_features WHERE id = ?", (loc["room_id"],)).fetchone()
            room_name = r["name"] if r else None
        item.update({k: loc[k] for k in ("kind", "floor_id", "room_id", "x", "y", "lat", "lon", "accuracy_m", "source",
                                         "confidence", "observed_at", "received_at", "flags")})
        item["room_name"] = room_name
    return item


def visible_location_items(conn: sqlite3.Connection, cfg: Settings, viewer: Principal,
                           floor_id: str | None = None) -> list[dict]:
    if viewer.can("location.view_all"):
        devs = rows(conn.execute(
            "SELECT * FROM devices WHERE revoked_at IS NULL AND type IN ('phone', 'badge')"), ("capabilities",))
    else:
        devs = rows(conn.execute(
            "SELECT * FROM devices WHERE revoked_at IS NULL AND type IN ('phone', 'badge') AND owner_user_id = ?",
            (viewer.user_id,)), ("capabilities",))
    now = utcnow()
    items = [describe_location(conn, cfg, d, now) for d in devs]
    if floor_id:
        items = [i for i in items if i.get("floor_id") in (None, floor_id)]
    return items


def best_location_for_incident(conn: sqlite3.Connection, cfg: Settings, device_ids: list[str]) -> dict:
    """The freshest stored location among the given devices, labelled honestly. Never invented."""
    best = None
    for did in device_ids:
        loc = row(conn.execute("SELECT * FROM current_locations WHERE device_id = ?", (did,)).fetchone())
        if loc and loc["kind"] not in ("unavailable", "denied") and (best is None or loc["observed_at"] > best["observed_at"]):
            best = loc
    if best is None:
        return {"location_note": "Location unavailable: no position has been reported for this device."}
    age = (utcnow() - parse_iso(best["observed_at"])).total_seconds()
    note = "Current location" if age <= cfg.live_s else f"Last known location, {int(age)} s old at time of alert"
    if best["kind"] == "room":
        note += " (room-level only)"
    elif best["kind"] == "geo" and best["x"] is None:
        note += " (GPS only, not on floor plan)"
    return {"floor_id": best["floor_id"], "room_id": best["room_id"], "x": best["x"], "y": best["y"],
            "lat": best["lat"], "lon": best["lon"], "location_source": best["source"],
            "location_observed_at": best["observed_at"], "accuracy_m": best["accuracy_m"], "location_note": note}


# ---------------------------------------------------------------- incidents

def incident_audience(inc: dict):
    return perm("incident.view_all", inc.get("reporter_user_id"))


def create_incident(conn: sqlite3.Connection, hub: Hub, cfg: Settings, *, source: str, idempotency_key: str,
                    category: str, severity: str, reporter_user_id: str | None, device_id: str | None,
                    location_device_ids: list[str], device_timestamp: str | None, message: str | None,
                    is_test: bool, actor: Principal, location_override: dict | None = None) -> tuple[dict, bool]:
    existing = row(conn.execute("SELECT * FROM incidents WHERE idempotency_key = ?", (idempotency_key,)).fetchone())
    if existing:
        return existing, False
    now = now_iso()
    loc = location_override or best_location_for_incident(conn, cfg, location_device_ids)
    inc_id = new_id("inc")
    conn.execute(
        "INSERT INTO incidents (id, source, category, severity, state, is_test, reporter_user_id, device_id, "
        "idempotency_key, created_at, device_timestamp, floor_id, room_id, x, y, lat, lon, location_source, "
        "location_observed_at, accuracy_m, location_note, message, updated_at) "
        "VALUES (?,?,?,?,'NEW',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (inc_id, source, category, severity, int(is_test), reporter_user_id, device_id, idempotency_key, now,
         device_timestamp, loc.get("floor_id"), loc.get("room_id"), loc.get("x"), loc.get("y"), loc.get("lat"),
         loc.get("lon"), loc.get("location_source"), loc.get("location_observed_at"), loc.get("accuracy_m"),
         loc["location_note"], message, now))
    add_incident_event(conn, inc_id, actor, "created", None, "NEW", f"{source} alert received by backend")
    conn.execute("UPDATE incidents SET last_alert_at = ? WHERE id = ?", (now, inc_id))
    inc = get_incident(conn, inc_id)
    hub.publish("incident", inc, incident_audience(inc))
    if not is_test:
        publish_sos_alert(conn, hub, inc)
    return inc, True


def publish_sos_alert(conn: sqlite3.Connection, hub: Hub, inc: dict) -> None:
    """The on-screen alarm every desktop shows for `sos.alert_display_s` seconds (new alert or a repeat press)."""
    hub.publish("sos_alert", {"incident": inc, "repeat": inc.get("repeat_count") or 0,
                              "display_s": get_setting(conn, "sos.alert_display_s")}, incident_audience(inc))


def incident_for_key(conn: sqlite3.Connection, idempotency_key: str) -> str | None:
    """Incident an SOS press belongs to: the one it created, or the open one it re-alerted."""
    r = conn.execute("SELECT id FROM incidents WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
    if r:
        return r["id"]
    r = conn.execute("SELECT incident_id FROM sos_presses WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
    return r["incident_id"] if r else None


def raise_sos(conn: sqlite3.Connection, hub: Hub, cfg: Settings, *, idempotency_key: str,
              open_incident_where: tuple[str, tuple], actor: Principal, **create_kwargs) -> tuple[dict, str]:
    """SOS press. Returns (incident, outcome) with outcome "created", "repeated" or "duplicate".

    The same press sent twice (retries) is a duplicate. A new press while this person's previous alert is
    still open re-alerts that incident (sos.repeat_mode = same_incident) instead of opening another one.
    """
    known = incident_for_key(conn, idempotency_key)
    if known:
        return get_incident(conn, known), "duplicate"
    if get_setting(conn, "sos.repeat_mode") == "same_incident":
        where, args = open_incident_where
        open_inc = conn.execute(
            f"SELECT id FROM incidents WHERE {where} AND is_test = 0 AND state IN {ACTIVE_INCIDENT_STATES} "
            "ORDER BY created_at DESC LIMIT 1", args).fetchone()
        if open_inc:
            now = now_iso()
            conn.execute("INSERT INTO sos_presses (idempotency_key, incident_id, created_at) VALUES (?,?,?)",
                         (idempotency_key, open_inc["id"], now))
            conn.execute("UPDATE incidents SET repeat_count = repeat_count + 1, last_alert_at = ?, updated_at = ? WHERE id = ?",
                         (now, now, open_inc["id"]))
            inc = get_incident(conn, open_inc["id"])
            add_incident_event(conn, inc["id"], actor, "sos_repeat", None, None,
                               f"SOS pressed again ({inc['repeat_count'] + 1} presses in total)")
            hub.publish("incident", inc, incident_audience(inc))
            publish_sos_alert(conn, hub, inc)
            return inc, "repeated"
    inc, created = create_incident(conn, hub, cfg, idempotency_key=idempotency_key, actor=actor, **create_kwargs)
    return inc, "created" if created else "duplicate"


def add_incident_event(conn: sqlite3.Connection, incident_id: str, actor: Principal | None, kind: str,
                       from_state: str | None, to_state: str | None, note: str | None) -> None:
    conn.execute(
        "INSERT INTO incident_events (incident_id, at, actor_user_id, actor_device_id, kind, from_state, to_state, note) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (incident_id, now_iso(), actor.user_id if actor and actor.kind == "user" else None,
         actor.device_id if actor and actor.kind == "device" else None, kind, from_state, to_state, note))


def get_incident(conn: sqlite3.Connection, incident_id: str) -> dict:
    inc = row(conn.execute(
        "SELECT i.*, ru.display_name AS reporter_name, au.display_name AS assigned_name, d.name AS device_name, "
        "f.name AS room_name FROM incidents i LEFT JOIN users ru ON ru.id = i.reporter_user_id "
        "LEFT JOIN users au ON au.id = i.assigned_to LEFT JOIN devices d ON d.id = i.device_id "
        "LEFT JOIN map_features f ON f.id = i.room_id WHERE i.id = ?", (incident_id,)).fetchone())
    if not inc:
        raise HTTPException(404, "Incident not found")
    inc["is_test"] = bool(inc["is_test"])
    return inc


def notify_users(hub: Hub, event_type: str, data: dict, *user_ids: str | None) -> None:
    hub.publish(event_type, data, users(*user_ids))
