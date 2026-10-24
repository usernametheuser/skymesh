"""Buildings, floors, floor-plan images, annotations, infrastructure and calibration."""
from __future__ import annotations

import json
import math
import re
import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from .. import mapimage, positioning as pos, services
from ..db import Tx, new_id, now_iso, row, rows
from ..realtime import everyone
from ..security import Principal, audit, client_ip, current_user, get_conn, require

router = APIRouter(prefix="/api/v1", tags=["maps"])

Point = list[float]
FEATURE_KINDS = Literal["room", "corridor", "stair", "lift", "exit", "entrance", "restricted", "assembly_point",
                        "void", "toilet", "other"]
ANCHOR_KINDS = Literal["ble_anchor", "wifi_ap", "camera_node", "gateway", "reference", "server"]
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\- ()]+\.(jpg|jpeg|png)$", re.I)


def _unit(v: float) -> float:
    if not (0.0 <= v <= 1.0) or math.isnan(v):
        raise ValueError("coordinates must be normalized to 0..1")
    return v


class BuildingIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class FloorIn(BaseModel):
    building_id: str = Field(max_length=40)
    name: str = Field(min_length=1, max_length=80)
    level: int = Field(ge=-10, le=200)


class FeatureIn(BaseModel):
    kind: FEATURE_KINDS
    name: str = Field(min_length=1, max_length=80)
    geometry: list[Point] = Field(min_length=1, max_length=200)
    properties: dict = Field(default_factory=dict)

    @field_validator("geometry")
    @classmethod
    def _check(cls, g: list[Point]) -> list[Point]:
        return _check_geometry(g)


def _check_geometry(g: list[Point] | None) -> list[Point] | None:
    if g is None:
        return None
    if not 1 <= len(g) <= 200:
        raise ValueError("geometry needs 1 to 200 points")
    for p in g:
        if len(p) != 2:
            raise ValueError("each point is [x, y]")
        _unit(p[0])
        _unit(p[1])
    if len(g) == 2:
        raise ValueError("a polygon needs at least 3 points; use 1 point for a marker")
    return [[round(p[0], 5), round(p[1], 5)] for p in g]


class FeaturePatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    kind: FEATURE_KINDS | None = None
    geometry: list[Point] | None = None
    properties: dict | None = None

    @field_validator("geometry")
    @classmethod
    def _check(cls, g: list[Point] | None) -> list[Point] | None:
        return _check_geometry(g)


class AnchorIn(BaseModel):
    kind: ANCHOR_KINDS
    name: str = Field(min_length=1, max_length=80)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    device_id: str | None = Field(default=None, max_length=40)
    identifier: str | None = Field(default=None, max_length=40)
    rssi_at_1m: int | None = Field(default=None, ge=-100, le=0)
    properties: dict = Field(default_factory=dict)


class AnchorPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    x: float | None = Field(default=None, ge=0, le=1)
    y: float | None = Field(default=None, ge=0, le=1)
    identifier: str | None = Field(default=None, max_length=40)
    rssi_at_1m: int | None = Field(default=None, ge=-100, le=0)
    device_id: str | None = Field(default=None, max_length=40)


class RectifyIn(BaseModel):
    source_file: str = Field(max_length=120)
    corners: list[Point] | None = None
    rotate: Literal[0, 90, 180, 270] = 0
    output_width: int = Field(default=1800, ge=400, le=6000)
    note: str | None = Field(default=None, max_length=200)


class GeorefIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    note: str | None = Field(default=None, max_length=120)


class ScaleIn(BaseModel):
    a: Point
    b: Point
    distance_m: float = Field(gt=0, le=5000)


class TransitionIn(BaseModel):
    from_feature_id: str
    to_feature_id: str
    kind: Literal["stair", "lift", "ramp"]


# ---------------------------------------------------------------- helpers

def _image_out(img: dict | None) -> dict | None:
    if not img:
        return None
    return {"id": img["id"], "version": img["version"], "width": img["width"], "height": img["height"],
            "url": f"/api/v1/map-images/{img['id']}/file", "transform": json.loads(img["transform"]),
            "source_file": img["source_file"], "active": bool(img["active"]), "created_at": img["created_at"]}


def _feature_out(f: dict, active_image_id: str | None) -> dict:
    f = dict(f)
    f["geometry"] = json.loads(f["geometry"]) if isinstance(f["geometry"], str) else f["geometry"]
    f["properties"] = json.loads(f["properties"]) if isinstance(f["properties"], str) else f["properties"]
    f["verified"] = bool(f["verified"])
    # Annotations drawn on an older image version must be re-checked against the new image.
    f["needs_recheck"] = bool(active_image_id and f["map_image_id"] and f["map_image_id"] != active_image_id)
    return f


def _active_image(conn: sqlite3.Connection, floor_id: str) -> dict | None:
    return row(conn.execute("SELECT * FROM map_images WHERE floor_id = ? AND active = 1", (floor_id,)).fetchone())


def _next_version(conn: sqlite3.Connection, floor_id: str) -> int:
    return (conn.execute("SELECT MAX(version) AS v FROM map_images WHERE floor_id = ?", (floor_id,)).fetchone()["v"] or 0) + 1


def _publish_map_change(request: Request, floor_id: str, what: str) -> None:
    request.app.state.hub.publish("map_changed", {"floor_id": floor_id, "what": what}, everyone)


def add_map_image(conn: sqlite3.Connection, maps_dir: Path, floor_id: str, source_file: str,
                  corners: list[Point] | None, rotate: int, output_width: int, user_id: str | None,
                  note: str | None = None) -> dict:
    src = maps_dir / "originals" / source_file
    if not SAFE_NAME.match(source_file) or not src.is_file():
        raise HTTPException(404, "Source photo not found")
    version = _next_version(conn, floor_id)
    img_id = new_id("img")
    file_name = f"{floor_id}_v{version}.jpg"
    try:
        w, h = mapimage.rectify(src, maps_dir / "rendered" / file_name, corners, rotate, output_width)
    except ValueError as e:
        raise HTTPException(422, str(e))
    prev = _active_image(conn, floor_id)
    transform = {"corners": corners, "rotate": rotate, "note": note,
                 "metres_per_unit": json.loads(prev["transform"]).get("metres_per_unit") if prev else None}
    conn.execute("UPDATE map_images SET active = 0 WHERE floor_id = ?", (floor_id,))
    conn.execute("INSERT INTO map_images (id, floor_id, version, file_name, width, height, source_file, transform, "
                 "active, created_at, created_by) VALUES (?,?,?,?,?,?,?,?,1,?,?)",
                 (img_id, floor_id, version, file_name, w, h, source_file, json.dumps(transform), now_iso(), user_id))
    return row(conn.execute("SELECT * FROM map_images WHERE id = ?", (img_id,)).fetchone())


# ---------------------------------------------------------------- buildings & floors

@router.get("/buildings")
def buildings(p: Principal = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    out = []
    for b in rows(conn.execute("SELECT * FROM buildings ORDER BY name")):
        floors = rows(conn.execute("SELECT * FROM floors WHERE building_id = ? ORDER BY level", (b["id"],)))
        for f in floors:
            f["image"] = _image_out(_active_image(conn, f["id"]))
        b["floors"] = floors
        out.append(b)
    return out


@router.post("/buildings", status_code=201)
def create_building(body: BuildingIn, request: Request, p: Principal = Depends(require("map.edit")),
                    conn: sqlite3.Connection = Depends(get_conn)):
    bid = new_id("bld")
    with Tx(conn):
        conn.execute("INSERT INTO buildings (id, name, created_at) VALUES (?,?,?)", (bid, body.name, now_iso()))
        audit(conn, p, "map.building_create", bid, {"name": body.name}, client_ip(request))
    return {"id": bid, "name": body.name}


@router.post("/floors", status_code=201)
def create_floor(body: FloorIn, request: Request, p: Principal = Depends(require("map.edit")),
                 conn: sqlite3.Connection = Depends(get_conn)):
    fid = new_id("flr")
    with Tx(conn):
        if not conn.execute("SELECT 1 FROM buildings WHERE id = ?", (body.building_id,)).fetchone():
            raise HTTPException(422, "Unknown building")
        if conn.execute("SELECT 1 FROM floors WHERE building_id = ? AND level = ?", (body.building_id, body.level)).fetchone():
            raise HTTPException(409, "That level already exists in this building")
        conn.execute("INSERT INTO floors (id, building_id, name, level, created_at) VALUES (?,?,?,?,?)",
                     (fid, body.building_id, body.name, body.level, now_iso()))
        audit(conn, p, "map.floor_create", fid, {"name": body.name}, client_ip(request))
    return {"id": fid, "building_id": body.building_id, "name": body.name, "level": body.level}


@router.get("/floors/{floor_id}")
def floor_detail(floor_id: str, p: Principal = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    f = services.require_floor(conn, floor_id)
    img = _active_image(conn, floor_id)
    feats = [_feature_out(x, img["id"] if img else None)
             for x in rows(conn.execute("SELECT * FROM map_features WHERE floor_id = ? ORDER BY kind, name", (floor_id,)))]
    anchors = rows(conn.execute("SELECT * FROM anchors WHERE floor_id = ? ORDER BY kind, name", (floor_id,)), ("properties",))
    if not p.can("map.edit"):
        for a in anchors:
            a.pop("identifier", None)  # BLE MACs / BSSIDs are infrastructure details for admins only
    gpts = rows(conn.execute("SELECT * FROM georef_points WHERE floor_id = ? ORDER BY created_at", (floor_id,)))
    gt = pos.fit_geotransform(gpts)
    trans = rows(conn.execute(
        "SELECT t.*, fa.name AS from_name, fb.name AS to_name, fb.floor_id AS to_floor_id, fa.floor_id AS from_floor_id "
        "FROM floor_transitions t JOIN map_features fa ON fa.id = t.from_feature_id "
        "JOIN map_features fb ON fb.id = t.to_feature_id WHERE fa.floor_id = ? OR fb.floor_id = ?", (floor_id, floor_id)))
    return {**f, "image": _image_out(img), "features": feats, "anchors": anchors,
            "georef": {"points": gpts if p.can("map.edit") else [], "count": len(gpts), "ready": gt is not None,
                       "rms_residual": round(gt.rms_residual, 4) if gt else None},
            "transitions": trans}


@router.get("/floors/{floor_id}/images")
def floor_images(floor_id: str, p: Principal = Depends(require("map.edit")), conn: sqlite3.Connection = Depends(get_conn)):
    return [_image_out(i) for i in rows(conn.execute("SELECT * FROM map_images WHERE floor_id = ? ORDER BY version DESC",
                                                     (floor_id,)))]


@router.get("/map-images/{image_id}/file")
def map_image_file(image_id: str, request: Request, p: Principal = Depends(current_user),
                   conn: sqlite3.Connection = Depends(get_conn)):
    img = row(conn.execute("SELECT * FROM map_images WHERE id = ?", (image_id,)).fetchone())
    if not img:
        raise HTTPException(404, "Image not found")
    path = request.app.state.settings.maps_dir / "rendered" / img["file_name"]
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.post("/map-images/{image_id}/activate")
def activate_image(image_id: str, request: Request, p: Principal = Depends(require("map.edit")),
                   conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        img = row(conn.execute("SELECT * FROM map_images WHERE id = ?", (image_id,)).fetchone())
        if not img:
            raise HTTPException(404, "Image not found")
        conn.execute("UPDATE map_images SET active = 0 WHERE floor_id = ?", (img["floor_id"],))
        conn.execute("UPDATE map_images SET active = 1 WHERE id = ?", (image_id,))
        audit(conn, p, "map.image_activate", image_id, {"version": img["version"]}, client_ip(request))
    _publish_map_change(request, img["floor_id"], "image")
    return {"ok": True}


@router.get("/map-sources")
def map_sources(request: Request, p: Principal = Depends(require("map.edit"))):
    d = request.app.state.settings.maps_dir / "originals"
    return sorted(f.name for f in d.iterdir() if f.is_file() and SAFE_NAME.match(f.name))


@router.get("/map-sources/{name}")
def map_source_file(name: str, request: Request, p: Principal = Depends(require("map.edit"))):
    path = request.app.state.settings.maps_dir / "originals" / name
    if not SAFE_NAME.match(name) or not path.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(path)


@router.post("/map-sources", status_code=201)
async def upload_source(request: Request, file: UploadFile = File(...), p: Principal = Depends(require("map.edit"))):
    name = Path(file.filename or "").name
    if not SAFE_NAME.match(name):
        raise HTTPException(422, "File name must end in .jpg/.jpeg/.png and use simple characters")
    data = await file.read(15 * 1024 * 1024 + 1)
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(413, "Image larger than 15 MB")
    if not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(422, "Not a JPEG or PNG image")
    dst = request.app.state.settings.maps_dir / "originals" / name
    if dst.exists():
        raise HTTPException(409, "A source photo with that name already exists")
    dst.write_bytes(data)
    return {"name": name}


@router.post("/floors/{floor_id}/images", status_code=201)
def rectify_image(floor_id: str, body: RectifyIn, request: Request, p: Principal = Depends(require("map.edit")),
                  conn: sqlite3.Connection = Depends(get_conn)):
    """Create a new map image version from a source photo (optionally perspective-corrected)."""
    with Tx(conn):
        services.require_floor(conn, floor_id)
        img = add_map_image(conn, request.app.state.settings.maps_dir, floor_id, body.source_file, body.corners,
                            body.rotate, body.output_width, p.user_id, body.note)
        audit(conn, p, "map.image_create", img["id"], {"version": img["version"], "source": body.source_file},
              client_ip(request))
    _publish_map_change(request, floor_id, "image")
    return _image_out(img)


@router.post("/floors/{floor_id}/scale")
def set_scale(floor_id: str, body: ScaleIn, request: Request, p: Principal = Depends(require("map.edit")),
              conn: sqlite3.Connection = Depends(get_conn)):
    """Metres per normalized unit, from two points a known distance apart (e.g. a room's labelled width)."""
    with Tx(conn):
        img = _active_image(conn, floor_id)
        if not img:
            raise HTTPException(409, "Floor has no active image")
        # Normalized y is relative to image height: convert to a common pixel space first.
        dx = (body.a[0] - body.b[0]) * img["width"]
        dy = (body.a[1] - body.b[1]) * img["height"]
        d_px = math.hypot(dx, dy)
        if d_px < 5:
            raise HTTPException(422, "Points are too close together")
        t = json.loads(img["transform"])
        t["metres_per_unit"] = round(body.distance_m / (d_px / img["width"]), 3)
        t["metres_per_pixel"] = round(body.distance_m / d_px, 5)
        conn.execute("UPDATE map_images SET transform = ? WHERE id = ?", (json.dumps(t), img["id"]))
        audit(conn, p, "map.scale_set", floor_id, t, client_ip(request))
    return t


# ---------------------------------------------------------------- features

@router.post("/floors/{floor_id}/features", status_code=201)
def create_feature(floor_id: str, body: FeatureIn, request: Request, p: Principal = Depends(require("map.edit")),
                   conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        services.require_floor(conn, floor_id)
        img = _active_image(conn, floor_id)
        fid, now = new_id("feat"), now_iso()
        conn.execute("INSERT INTO map_features (id, floor_id, map_image_id, kind, geometry_type, name, geometry, "
                     "properties, source, verified, verified_by, verified_at, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?, 'manual', 1, ?, ?, ?, ?)",
                     (fid, floor_id, img["id"] if img else None, body.kind,
                      "point" if len(body.geometry) == 1 else "polygon", body.name, json.dumps(body.geometry),
                      json.dumps(body.properties), p.user_id, now, now, now))
        audit(conn, p, "map.feature_create", fid, {"kind": body.kind, "name": body.name}, client_ip(request))
        f = row(conn.execute("SELECT * FROM map_features WHERE id = ?", (fid,)).fetchone())
    _publish_map_change(request, floor_id, "features")
    return _feature_out(f, img["id"] if img else None)


@router.patch("/features/{feature_id}")
def patch_feature(feature_id: str, body: FeaturePatchIn, request: Request, p: Principal = Depends(require("map.edit")),
                  conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        f = row(conn.execute("SELECT * FROM map_features WHERE id = ?", (feature_id,)).fetchone())
        if not f:
            raise HTTPException(404, "Feature not found")
        img = _active_image(conn, f["floor_id"])
        sets: dict = {"updated_at": now_iso()}
        if body.name is not None:
            sets["name"] = body.name
        if body.kind is not None:
            sets["kind"] = body.kind
        if body.properties is not None:
            sets["properties"] = json.dumps(body.properties)
        if body.geometry is not None:
            sets.update(geometry=json.dumps(body.geometry), map_image_id=img["id"] if img else None,
                        geometry_type="point" if len(body.geometry) == 1 else "polygon")
        conn.execute(f"UPDATE map_features SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                     (*sets.values(), feature_id))
        audit(conn, p, "map.feature_update", feature_id, {k: v for k, v in sets.items() if k != "geometry"},
              client_ip(request))
        f = row(conn.execute("SELECT * FROM map_features WHERE id = ?", (feature_id,)).fetchone())
    _publish_map_change(request, f["floor_id"], "features")
    return _feature_out(f, img["id"] if img else None)


@router.post("/features/{feature_id}/verify")
def verify_feature(feature_id: str, request: Request, verified: bool = True,
                   p: Principal = Depends(require("map.edit")), conn: sqlite3.Connection = Depends(get_conn)):
    """An administrator confirms a draft (or re-checks after a new image version)."""
    with Tx(conn):
        f = row(conn.execute("SELECT * FROM map_features WHERE id = ?", (feature_id,)).fetchone())
        if not f:
            raise HTTPException(404, "Feature not found")
        img = _active_image(conn, f["floor_id"])
        now = now_iso()
        conn.execute("UPDATE map_features SET verified = ?, verified_by = ?, verified_at = ?, map_image_id = ?, "
                     "updated_at = ? WHERE id = ?",
                     (int(verified), p.user_id if verified else None, now if verified else None,
                      img["id"] if img else f["map_image_id"], now, feature_id))
        audit(conn, p, "map.feature_verify", feature_id, {"verified": verified}, client_ip(request))
        f = row(conn.execute("SELECT * FROM map_features WHERE id = ?", (feature_id,)).fetchone())
    _publish_map_change(request, f["floor_id"], "features")
    return _feature_out(f, img["id"] if img else None)


@router.delete("/features/{feature_id}")
def delete_feature(feature_id: str, request: Request, p: Principal = Depends(require("map.edit")),
                   conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        f = row(conn.execute("SELECT * FROM map_features WHERE id = ?", (feature_id,)).fetchone())
        if not f:
            raise HTTPException(404, "Feature not found")
        conn.execute("DELETE FROM map_features WHERE id = ?", (feature_id,))
        audit(conn, p, "map.feature_delete", feature_id, {"name": f["name"]}, client_ip(request))
    _publish_map_change(request, f["floor_id"], "features")
    return {"ok": True}


@router.post("/floors/{floor_id}/assist")
def assist(floor_id: str, request: Request, p: Principal = Depends(require("map.edit")),
           conn: sqlite3.Connection = Depends(get_conn)):
    """Run the exit-sign detector on the active image and store results as UNVERIFIED drafts."""
    with Tx(conn):
        img = _active_image(conn, floor_id)
        if not img:
            raise HTTPException(409, "Floor has no active image")
        found = mapimage.detect_exit_signs(request.app.state.settings.maps_dir / "rendered" / img["file_name"])
        existing = [json.loads(r["geometry"])[0] for r in conn.execute(
            "SELECT geometry FROM map_features WHERE floor_id = ? AND kind = 'exit' AND geometry_type = 'point'",
            (floor_id,))]
        created = []
        now = now_iso()
        for d in found:
            if any(abs(e[0] - d["x"]) < 0.02 and abs(e[1] - d["y"]) < 0.02 for e in existing):
                continue
            fid = new_id("feat")
            conn.execute("INSERT INTO map_features (id, floor_id, map_image_id, kind, geometry_type, name, geometry, "
                         "properties, source, verified, created_at, updated_at) "
                         "VALUES (?,?,?,'exit','point',?,?,?, 'assist_draft', 0, ?, ?)",
                         (fid, floor_id, img["id"], f"{d['label']} (detected)",
                          json.dumps([[round(d["x"], 5), round(d["y"], 5)]]),
                          json.dumps({"detector": "green-sign-colour-shape", "score": d["score"], "bbox": d["bbox"]}),
                          now, now))
            created.append(fid)
        audit(conn, p, "map.assist_run", floor_id, {"proposals": len(created)}, client_ip(request))
    _publish_map_change(request, floor_id, "features")
    return {"proposed": len(created), "detections": len(found)}


# ---------------------------------------------------------------- infrastructure anchors

@router.post("/floors/{floor_id}/anchors", status_code=201)
def create_anchor(floor_id: str, body: AnchorIn, request: Request, p: Principal = Depends(require("map.edit")),
                  conn: sqlite3.Connection = Depends(get_conn)):
    aid = new_id("anc")
    with Tx(conn):
        services.require_floor(conn, floor_id)
        ident = body.identifier.lower() if body.identifier else None
        if body.device_id:
            dev = conn.execute("SELECT capabilities FROM devices WHERE id = ?", (body.device_id,)).fetchone()
            if not dev:
                raise HTTPException(422, "Unknown device_id")
            # Camera nodes report their beacon address: use it so badges can locate themselves nearby.
            caps = json.loads(dev["capabilities"] or "{}")
            ident = ident or (caps.get("beacon_id") or caps.get("ble_mac") or "").lower() or None
        # A camera's Wi-Fi beacon is louder than a BLE tag: about -45 dBm at 1 m.
        rssi_1m = body.rssi_at_1m if body.rssi_at_1m is not None else (-45 if body.kind == "camera_node" else None)
        conn.execute("INSERT INTO anchors (id, floor_id, kind, name, x, y, device_id, identifier, rssi_at_1m, properties, "
                     "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (aid, floor_id, body.kind, body.name, body.x, body.y, body.device_id,
                      ident, rssi_1m, json.dumps(body.properties), now_iso()))
        audit(conn, p, "map.anchor_create", aid, {"kind": body.kind}, client_ip(request))
    _publish_map_change(request, floor_id, "anchors")
    return row(conn.execute("SELECT * FROM anchors WHERE id = ?", (aid,)).fetchone(), ("properties",))


@router.patch("/anchors/{anchor_id}")
def patch_anchor(anchor_id: str, body: AnchorPatchIn, request: Request, p: Principal = Depends(require("map.edit")),
                 conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        a = row(conn.execute("SELECT * FROM anchors WHERE id = ?", (anchor_id,)).fetchone())
        if not a:
            raise HTTPException(404, "Anchor not found")
        sets = body.model_dump(exclude_none=True)
        if "identifier" in sets:
            sets["identifier"] = sets["identifier"].lower()
        if sets:
            conn.execute(f"UPDATE anchors SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                         (*sets.values(), anchor_id))
        audit(conn, p, "map.anchor_update", anchor_id, sets, client_ip(request))
    _publish_map_change(request, a["floor_id"], "anchors")
    return row(conn.execute("SELECT * FROM anchors WHERE id = ?", (anchor_id,)).fetchone(), ("properties",))


@router.delete("/anchors/{anchor_id}")
def delete_anchor(anchor_id: str, request: Request, p: Principal = Depends(require("map.edit")),
                  conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        a = row(conn.execute("SELECT * FROM anchors WHERE id = ?", (anchor_id,)).fetchone())
        if not a:
            raise HTTPException(404, "Anchor not found")
        conn.execute("DELETE FROM anchors WHERE id = ?", (anchor_id,))
        audit(conn, p, "map.anchor_delete", anchor_id, {"name": a["name"]}, client_ip(request))
    _publish_map_change(request, a["floor_id"], "anchors")
    return {"ok": True}


# ---------------------------------------------------------------- georeferencing & transitions

@router.post("/floors/{floor_id}/georef-points", status_code=201)
def add_georef(floor_id: str, body: GeorefIn, request: Request, p: Principal = Depends(require("map.edit")),
               conn: sqlite3.Connection = Depends(get_conn)):
    gid = new_id("geo")
    with Tx(conn):
        services.require_floor(conn, floor_id)
        conn.execute("INSERT INTO georef_points (id, floor_id, lat, lon, x, y, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
                     (gid, floor_id, body.lat, body.lon, body.x, body.y, body.note, now_iso()))
        audit(conn, p, "map.georef_add", floor_id, {"id": gid}, client_ip(request))
        gt = services.geotransform_for(conn, floor_id)
    return {"id": gid, "ready": gt is not None, "rms_residual": round(gt.rms_residual, 4) if gt else None}


@router.delete("/georef-points/{point_id}")
def delete_georef(point_id: str, request: Request, p: Principal = Depends(require("map.edit")),
                  conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        cur = conn.execute("DELETE FROM georef_points WHERE id = ?", (point_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "Not found")
        audit(conn, p, "map.georef_delete", point_id, {}, client_ip(request))
    return {"ok": True}


@router.post("/transitions", status_code=201)
def create_transition(body: TransitionIn, request: Request, p: Principal = Depends(require("map.edit")),
                      conn: sqlite3.Connection = Depends(get_conn)):
    tid = new_id("trn")
    with Tx(conn):
        a = conn.execute("SELECT floor_id FROM map_features WHERE id = ?", (body.from_feature_id,)).fetchone()
        b = conn.execute("SELECT floor_id FROM map_features WHERE id = ?", (body.to_feature_id,)).fetchone()
        if not a or not b:
            raise HTTPException(422, "Unknown feature")
        if a["floor_id"] == b["floor_id"]:
            raise HTTPException(422, "A transition must connect two different floors")
        conn.execute("INSERT INTO floor_transitions (id, from_feature_id, to_feature_id, kind, created_at) VALUES (?,?,?,?,?)",
                     (tid, body.from_feature_id, body.to_feature_id, body.kind, now_iso()))
        audit(conn, p, "map.transition_create", tid, body.model_dump(), client_ip(request))
    return {"id": tid}


@router.delete("/transitions/{transition_id}")
def delete_transition(transition_id: str, request: Request, p: Principal = Depends(require("map.edit")),
                      conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        if conn.execute("DELETE FROM floor_transitions WHERE id = ?", (transition_id,)).rowcount == 0:
            raise HTTPException(404, "Not found")
        audit(conn, p, "map.transition_delete", transition_id, {}, client_ip(request))
    return {"ok": True}
