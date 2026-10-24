"""SQLite access and migrations.

SQLite is the prototype store (single local backend). Each request gets its own
connection; WAL mode allows concurrent readers alongside one writer.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def now_iso() -> str:
    return iso(utcnow())


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=15, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def migrate(db_path: Path) -> list[str]:
    conn = connect(db_path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
        applied = []
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.stem in done:
                continue
            conn.execute("BEGIN")
            for stmt in _split_sql(path.read_text(encoding="utf-8")):
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations VALUES (?, ?)", (path.stem, now_iso()))
            conn.execute("COMMIT")
            applied.append(path.stem)
        return applied
    finally:
        conn.close()


def _split_sql(script: str) -> list[str]:
    lines = [ln for ln in script.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


class Tx:
    """`with Tx(conn):` runs the block in an IMMEDIATE transaction."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")


def row(r: sqlite3.Row | None, json_fields: Iterable[str] = ()) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    for f in json_fields:
        if f in d and isinstance(d[f], str):
            d[f] = json.loads(d[f])
    return d


def rows(rs: Iterable[sqlite3.Row], json_fields: Iterable[str] = ()) -> list[dict[str, Any]]:
    jf = tuple(json_fields)
    return [row(r, jf) for r in rs]  # type: ignore[misc]


DEFAULT_SETTINGS: dict[str, Any] = {
    "retention.location_days": 7,
    "retention.heartbeat_days": 3,
    "retention.message_days": 365,
    "retention.camera_observation_days": 7,
    "retention.audit_days": 365,
    "camera.analysis_enabled": False,
    "camera.analysis_interval_s": 5,
    "camera.fall_detection_enabled": False,
    "camera.fall_still_seconds": 8,
    "camera.no_movement_seconds": 120,  # 0 turns the "person not moving" alert off
    "badge.photo_on_sos": True,          # badge sends a photo of the scene right after its SOS
    "badge.remote_photo_enabled": True,   # responders: any badge; users: their own badge (all audited)
    "retention.photo_days": 7,
    "chat.max_message_chars": 4000,
    "sos.alert_display_s": 10,            # how long a new SOS stays on every desktop before fading
    "sos.repeat_mode": "same_incident",   # same_incident: a press while open re-alerts it; new_incident: always a new one
    "badge.sos_hold_ms": 1500,            # how long the badge button must be held
    "badge.sos_lockout_s": 5,             # minimum gap between two SOS presses on a badge
}

# Settings that take one of a fixed set of text values.
SETTING_CHOICES: dict[str, tuple[str, ...]] = {
    "sos.repeat_mode": ("same_incident", "new_incident"),
}


def get_setting(conn: sqlite3.Connection, key: str) -> Any:
    r = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(r["value"]) if r else DEFAULT_SETTINGS.get(key)


def all_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    out = dict(DEFAULT_SETTINGS)
    for r in conn.execute("SELECT key, value FROM settings"):
        out[r["key"]] = json.loads(r["value"])
    return out
