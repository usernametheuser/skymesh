"""Location freshness, georeferencing, room lookup and indoor (BLE) estimation.

Everything here works in normalized floor-plan coordinates: x and y in [0, 1]
relative to the active, calibrated floor-plan image.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

import numpy as np

from .config import Settings
from .db import parse_iso

# Location states (see docs/indoor-positioning.md).
LIVE = "LIVE"
RECENT = "RECENT"
STALE = "STALE"
LAST_KNOWN = "LAST_KNOWN"
OFFLINE = "OFFLINE"
PERMISSION_DENIED = "PERMISSION_DENIED"
SHARING_DISABLED = "SHARING_DISABLED"
POSITION_UNAVAILABLE = "POSITION_UNAVAILABLE"
LOW_CONFIDENCE = "LOW_CONFIDENCE"


def location_state(device: dict, loc: dict | None, cfg: Settings, now: datetime) -> dict[str, Any]:
    """Classify a device's latest location. Old positions are never reported as live."""
    out: dict[str, Any] = {"state": POSITION_UNAVAILABLE, "age_s": None, "is_live": False}
    if device["type"] == "phone":
        if device["permission_state"] == "denied":
            out["state"] = PERMISSION_DENIED
            return out
        if not device["sharing_enabled"]:
            out["state"] = SHARING_DISABLED
            return out
    if loc is None or loc["kind"] in ("unavailable", "denied"):
        if loc is not None and loc["kind"] == "denied":
            out["state"] = PERMISSION_DENIED
        return out
    age = (now - parse_iso(loc["observed_at"])).total_seconds()
    out["age_s"] = round(max(age, 0.0), 1)
    if age <= cfg.live_s:
        low = (loc["accuracy_m"] is not None and loc["accuracy_m"] > cfg.low_confidence_m) or \
              (loc["confidence"] is not None and loc["confidence"] < 0.35)
        out["state"] = LOW_CONFIDENCE if low else LIVE
        out["is_live"] = True
    elif age <= cfg.recent_s:
        out["state"] = RECENT
    elif age <= cfg.stale_s:
        out["state"] = STALE
    else:
        out["state"] = LAST_KNOWN
    if device_online(device, cfg, now) is False and out["state"] in (LIVE, LOW_CONFIDENCE, RECENT):
        # Heartbeat-bearing devices that dropped off cannot be live, whatever their last fix said.
        out["state"], out["is_live"] = OFFLINE, False
    return out


def device_online(device: dict, cfg: Settings, now: datetime) -> bool | None:
    """True/False from heartbeats; None for devices that never heartbeat (e.g. browser phones)."""
    if not device.get("last_seen_at"):
        return None if device["type"] == "phone" else False
    return (now - parse_iso(device["last_seen_at"])).total_seconds() <= cfg.device_offline_s


# ---------------------------------------------------------------- geometry

def point_in_polygon(x: float, y: float, poly: Sequence[Sequence[float]]) -> bool:
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def polygon_centroid(poly: Sequence[Sequence[float]]) -> tuple[float, float]:
    a = cx = cy = 0.0
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(a) < 1e-12:
        return (sum(p[0] for p in poly) / n, sum(p[1] for p in poly) / n)
    a *= 0.5
    return (cx / (6 * a), cy / (6 * a))


def find_area(features: list[dict], x: float, y: float) -> dict | None:
    """Smallest polygon feature containing the point (rooms win over corridors by area)."""
    best, best_area = None, math.inf
    for f in features:
        if f["geometry_type"] != "polygon":
            continue
        poly = f["geometry"] if isinstance(f["geometry"], list) else json.loads(f["geometry"])
        if len(poly) >= 3 and point_in_polygon(x, y, poly):
            area = abs(_area(poly))
            if area < best_area:
                best, best_area = f, area
    return best


def _area(poly: Sequence[Sequence[float]]) -> float:
    return 0.5 * sum(poly[i][0] * poly[(i + 1) % len(poly)][1] - poly[(i + 1) % len(poly)][0] * poly[i][1]
                     for i in range(len(poly)))


# ---------------------------------------------------------------- georeferencing (GPS -> floor plan)

@dataclass
class GeoTransform:
    origin: np.ndarray   # mean (lon, lat) of the control points, for numerical conditioning
    matrix: np.ndarray   # 2x3 affine on micro-degree offsets: [x, y] = M @ [dlon, dlat, 1]
    rms_residual: float  # in normalized units
    points: int

    def apply(self, lat: float, lon: float) -> tuple[float, float]:
        d = (np.array([lon, lat]) - self.origin) * 1e6
        x, y = self.matrix @ np.array([d[0], d[1], 1.0])
        return float(x), float(y)


def fit_geotransform(points: list[dict]) -> GeoTransform | None:
    """Least-squares affine from >= 3 non-collinear control points, or None."""
    if len(points) < 3:
        return None
    ll = np.array([[p["lon"], p["lat"]] for p in points], dtype=np.float64)
    origin = ll.mean(axis=0)
    d = (ll - origin) * 1e6
    s = np.linalg.svd(d, compute_uv=False)
    if s[0] == 0 or s[1] / s[0] < 1e-3:  # collinear or coincident points
        return None
    a = np.hstack([d, np.ones((len(points), 1))])
    b = np.array([[p["x"], p["y"]] for p in points])
    sol, *_ = np.linalg.lstsq(a, b, rcond=None)
    resid = b - (a @ sol)
    rms = float(np.sqrt((resid ** 2).sum(axis=1).mean()))
    return GeoTransform(origin, sol.T, rms, len(points))


# ---------------------------------------------------------------- BLE estimation

PATH_LOSS_EXPONENT = 2.5
DEFAULT_RSSI_AT_1M = -59


def rssi_to_distance_m(rssi: float, rssi_at_1m: float | None) -> float:
    ref = rssi_at_1m if rssi_at_1m is not None else DEFAULT_RSSI_AT_1M
    return 10 ** ((ref - rssi) / (10 * PATH_LOSS_EXPONENT))


def estimate_from_ble(readings: list[dict], anchors: list[dict], metres_per_unit: float | None) -> dict:
    """Turn BLE RSSI readings into an honest estimate.

    - no known anchor heard           -> unavailable
    - 1-2 anchors                     -> nearest-anchor (room-level when the anchor sits in a room)
    - >= 3 anchors on the same floor  -> weighted centroid estimate, flagged as an estimate
    """
    by_id = {a["identifier"].lower(): a for a in anchors if a.get("identifier")}
    heard = []
    for r in readings:
        a = by_id.get(str(r.get("id", "")).lower())
        if a is not None and isinstance(r.get("rssi"), (int, float)) and -120 <= r["rssi"] <= 0:
            heard.append((a, float(r["rssi"])))
    if not heard:
        return {"kind": "unavailable", "source": "ble", "reason": "no_known_anchor_heard"}
    heard.sort(key=lambda t: t[1], reverse=True)
    floor_id = heard[0][0]["floor_id"]
    same_floor = [(a, rssi) for a, rssi in heard if a["floor_id"] == floor_id]
    nearest, rssi = same_floor[0]
    if len(same_floor) < 3:
        d = rssi_to_distance_m(rssi, nearest.get("rssi_at_1m"))
        return {"kind": "nearest", "source": "ble_nearest_anchor", "floor_id": floor_id,
                "x": nearest["x"], "y": nearest["y"], "anchor_id": nearest["id"],
                "accuracy_m": round(max(d, 3.0), 1), "confidence": 0.3, "anchors_used": len(same_floor)}
    weights, xs, ys, dists = [], [], [], []
    for a, r in same_floor[:6]:
        d = max(rssi_to_distance_m(r, a.get("rssi_at_1m")), 0.5)
        weights.append(1.0 / d ** 2)
        xs.append(a["x"])
        ys.append(a["y"])
        dists.append(d)
    w = np.array(weights) / sum(weights)
    x, y = float(np.dot(w, xs)), float(np.dot(w, ys))
    acc = None
    if metres_per_unit:
        # Spread of the anchors used is a floor on what a centroid can resolve.
        spread = float(np.sqrt(np.average((np.array(xs) - x) ** 2 + (np.array(ys) - y) ** 2, weights=w)))
        acc = round(max(spread * metres_per_unit, min(dists), 2.0), 1)
    return {"kind": "estimate", "source": "ble_weighted_centroid", "floor_id": floor_id,
            "x": min(max(x, 0.0), 1.0), "y": min(max(y, 0.0), 1.0), "accuracy_m": acc,
            "confidence": 0.5, "anchors_used": len(dists)}


# ---------------------------------------------------------------- camera homography

def fit_homography(image_points: list[list[float]], map_points: list[list[float]]) -> list[list[float]]:
    import cv2
    if len(image_points) < 4 or len(image_points) != len(map_points):
        raise ValueError("need >= 4 matching point pairs")
    h, _ = cv2.findHomography(np.array(image_points, dtype=np.float64), np.array(map_points, dtype=np.float64), 0)
    if h is None:
        raise ValueError("degenerate point configuration")
    return h.tolist()


def apply_homography(h: list[list[float]], u: float, v: float) -> tuple[float, float] | None:
    m = np.array(h)
    p = m @ np.array([u, v, 1.0])
    if abs(p[2]) < 1e-9:
        return None
    return float(p[0] / p[2]), float(p[1] / p[2])
