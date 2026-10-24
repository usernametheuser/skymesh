"""Map trainer: radio fingerprinting ("I'm here") and k-nearest-neighbour positioning.

A trained spot is a known map position plus the signal strengths a badge heard there (Wi-Fi access
points by BSSID, SkyMesh beacons). A live scan is located by comparing it with every trained spot:
the closest few spots in signal space give the position. Accuracy depends on how densely the
floor was trained; with no training the backend falls back to the anchor estimate in positioning.py.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
from datetime import timedelta
from typing import Any

from .db import iso, now_iso, parse_iso, row, rows, utcnow

MAC = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
MISSING_DBM = -95.0         # value used for an access point one side heard and the other did not
MIN_SEEN_RATIO = 0.34       # an AP must appear in at least a third of a spot's samples to count
SESSION_TIMEOUT_S = 120     # an unfinished "I'm here" capture expires after this
SETTLE_S = 2.5              # ignore scans that started before the person stood still
K = 3


def vector(readings: list[dict] | None) -> dict[str, float]:
    """{id: rssi} from badge readings. Randomised (locally administered) MACs are dropped: those are
    phone hotspots and other moving devices, useless for positioning and personal."""
    out: dict[str, float] = {}
    for r in readings or []:
        rid = str(r.get("id", "")).strip().lower()
        rssi = r.get("rssi")
        if not isinstance(rssi, (int, float)) or not -120 <= rssi <= 0 or not rid:
            continue
        if MAC.match(rid) and int(rid[:2], 16) & 0x02:
            continue
        out[rid] = max(float(rssi), out.get(rid, -999.0))
    return out


# ---------------------------------------------------------------- training sessions

def active_session(conn: sqlite3.Connection, device_id: str) -> dict | None:
    s = row(conn.execute("SELECT * FROM training_sessions WHERE device_id = ? AND ended_at IS NULL "
                         "ORDER BY started_at DESC LIMIT 1", (device_id,)).fetchone())
    if s and (utcnow() - parse_iso(s["started_at"])).total_seconds() > SESSION_TIMEOUT_S:
        conn.execute("UPDATE training_sessions SET ended_at = ? WHERE id = ?", (now_iso(), s["id"]))
        return None
    return s


def add_sample(conn: sqlite3.Connection, session: dict, vec: dict[str, float]) -> dict:
    """Store one scan for the session. Returns the session with `accepted` and `done` set."""
    age = (utcnow() - parse_iso(session["started_at"])).total_seconds()
    out = dict(session, accepted=False, done=False, heard=len(vec))
    if age < SETTLE_S:
        out["reason"] = "settling"
        return out
    if not vec:
        out["reason"] = "nothing_heard"
        return out
    conn.execute("INSERT INTO fingerprints (session_id, floor_id, x, y, room_id, device_id, vector, created_at) "
                 "VALUES (?,?,?,?,?,?,?,?)", (session["id"], session["floor_id"], session["x"], session["y"],
                                               session["room_id"], session["device_id"], json.dumps(vec), now_iso()))
    n = session["samples"] + 1
    done = n >= session["target"]
    conn.execute("UPDATE training_sessions SET samples = ?, ended_at = ? WHERE id = ?",
                 (n, now_iso() if done else None, session["id"]))
    out.update(samples=n, accepted=True, done=done)
    _cache.invalidate()
    return out


# ---------------------------------------------------------------- trained spots (cached)

class _SpotCache:
    """Spots are rebuilt only when the fingerprint table changes, so matching costs no queries."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: tuple | None = None
        self._spots: list[dict] = []

    def invalidate(self) -> None:
        with self._lock:
            self._key = None

    def get(self, conn: sqlite3.Connection) -> list[dict]:
        k = (conn.execute("PRAGMA database_list").fetchone()[2],
             *conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM fingerprints").fetchone())
        with self._lock:
            if k == self._key:
                return self._spots
        spots = build_spots(conn)
        with self._lock:
            self._key, self._spots = k, spots
        return spots


_cache = _SpotCache()


def build_spots(conn: sqlite3.Connection, floor_id: str | None = None) -> list[dict]:
    """One averaged fingerprint per trained spot (training session)."""
    q = ("SELECT f.session_id, f.floor_id, f.x, f.y, f.room_id, f.vector, f.created_at FROM fingerprints f "
         + ("WHERE f.floor_id = ? " if floor_id else "") + "ORDER BY f.id")
    groups: dict[Any, dict] = {}
    for r in conn.execute(q, (floor_id,) if floor_id else ()):
        key = r["session_id"] or (r["floor_id"], round(r["x"], 4), round(r["y"], 4))
        g = groups.setdefault(key, {"id": r["session_id"], "floor_id": r["floor_id"], "x": r["x"], "y": r["y"],
                                    "room_id": r["room_id"], "n": 0, "sums": {}, "seen": {}, "at": r["created_at"]})
        g["n"] += 1
        for k, v in json.loads(r["vector"]).items():
            g["sums"][k] = g["sums"].get(k, 0.0) + v
            g["seen"][k] = g["seen"].get(k, 0) + 1
    spots = []
    for g in groups.values():
        vec = {k: g["sums"][k] / c for k, c in g["seen"].items() if c / g["n"] >= MIN_SEEN_RATIO}
        if vec:
            spots.append({"id": g["id"], "floor_id": g["floor_id"], "x": g["x"], "y": g["y"], "room_id": g["room_id"],
                          "samples": g["n"], "vector": vec, "created_at": g["at"]})
    return spots


def distance(a: dict[str, float], b: dict[str, float], known: set[str]) -> tuple[float, int]:
    """RMS signal difference in dB over the APs either side heard (unknown APs in the scan ignored)."""
    keys = set(b) | (set(a) & known)
    common = len(set(a) & set(b))
    if not keys or not common:
        return math.inf, 0
    s = sum((a.get(k, MISSING_DBM) - b.get(k, MISSING_DBM)) ** 2 for k in keys)
    return math.sqrt(s / len(keys)), common


def match(conn: sqlite3.Connection, vec: dict[str, float], metres_per_unit=None) -> dict | None:
    """Weighted k-NN over trained spots. None when nothing trained is comparable to this scan."""
    spots = _cache.get(conn)
    if not spots or not vec:
        return None
    known = {k for s in spots for k in s["vector"]}
    scored = []
    for s in spots:
        d, common = distance(vec, s["vector"], known)
        if math.isfinite(d):
            scored.append((d, common, s))
    if not scored:
        return None
    scored.sort(key=lambda t: t[0])
    best_d, best_common, best = scored[0]
    floor_id = best["floor_id"]
    # Neighbours must be on the same floor and not much worse than the best, so two distant
    # look-alike spots are not averaged into a point between them.
    near = [t for t in scored if t[2]["floor_id"] == floor_id and t[0] <= best_d * 1.4 + 2.0][:K]
    w = [1.0 / (d + 1.0) ** 2 for d, _, _ in near]
    tw = sum(w)
    x = sum(wi * t[2]["x"] for wi, t in zip(w, near)) / tw
    y = sum(wi * t[2]["y"] for wi, t in zip(w, near)) / tw
    # Confidence from signal distance (about 4 dB = same spot, 20 dB = poor match) and overlap.
    conf = max(0.05, min(0.9, 1.0 - (best_d - 4.0) / 18.0)) * min(1.0, best_common / 3.0)
    acc = None
    mpu = metres_per_unit(floor_id) if callable(metres_per_unit) else metres_per_unit
    if mpu:
        spread = math.sqrt(sum(wi * ((t[2]["x"] - x) ** 2 + (t[2]["y"] - y) ** 2) for wi, t in zip(w, near)) / tw)
        acc = round(max(spread * mpu, 2.5 if conf >= 0.5 else 5.0), 1)
    return {"floor_id": floor_id, "x": min(max(x, 0.0), 1.0), "y": min(max(y, 0.0), 1.0),
            "room_id": best["room_id"] if len(near) == 1 else None, "signal_distance_db": round(best_d, 1),
            "confidence": round(conf, 2), "accuracy_m": acc, "spots_used": len(near), "common_aps": best_common}


def spots_summary(conn: sqlite3.Connection, floor_id: str) -> list[dict]:
    out = []
    for s in build_spots(conn, floor_id):
        name = None
        if s["room_id"]:
            r = conn.execute("SELECT name FROM map_features WHERE id = ?", (s["room_id"],)).fetchone()
            name = r["name"] if r else None
        out.append({"id": s["id"], "x": s["x"], "y": s["y"], "room_id": s["room_id"], "room_name": name,
                    "samples": s["samples"], "signals": len(s["vector"]), "created_at": s["created_at"]})
    return out


def expire_old(conn: sqlite3.Connection) -> None:
    cutoff = iso(utcnow() - timedelta(seconds=SESSION_TIMEOUT_S))
    conn.execute("UPDATE training_sessions SET ended_at = ? WHERE ended_at IS NULL AND started_at < ?", (now_iso(), cutoff))
