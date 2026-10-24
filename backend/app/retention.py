"""Configurable deletion of sensitive history (locations, heartbeats, messages, camera data, audit)."""
from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

from .db import Tx, get_setting, iso, utcnow


def run_retention(conn: sqlite3.Connection, photo_dir: Path | None = None) -> dict[str, int]:
    now = utcnow()

    def cutoff(key: str) -> str:
        return iso(now - timedelta(days=float(get_setting(conn, key))))

    out: dict[str, int] = {}
    with Tx(conn):
        # current_locations keeps each device's latest row, so the map is unaffected by history purges.
        out["location_updates"] = conn.execute(
            "DELETE FROM location_updates WHERE received_at < ? AND id NOT IN (SELECT update_id FROM current_locations)",
            (cutoff("retention.location_days"),)).rowcount
        out["device_heartbeats"] = conn.execute("DELETE FROM device_heartbeats WHERE received_at < ?",
                                                (cutoff("retention.heartbeat_days"),)).rowcount
        out["messages"] = conn.execute("DELETE FROM messages WHERE created_at < ?",
                                       (cutoff("retention.message_days"),)).rowcount
        out["camera_observations"] = conn.execute("DELETE FROM camera_observations WHERE created_at < ?",
                                                  (cutoff("retention.camera_observation_days"),)).rowcount
        out["audit_log"] = conn.execute("DELETE FROM audit_log WHERE at < ?", (cutoff("retention.audit_days"),)).rowcount
        out["sessions"] = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (iso(now),)).rowcount
        old = conn.execute("SELECT file_name FROM device_photos WHERE created_at < ?", (cutoff("retention.photo_days"),)).fetchall()
        out["device_photos"] = conn.execute("DELETE FROM device_photos WHERE created_at < ?",
                                            (cutoff("retention.photo_days"),)).rowcount
    if photo_dir is not None:  # remove the image files after the rows are gone
        for r in old:
            (photo_dir / r["file_name"]).unlink(missing_ok=True)
    return out
