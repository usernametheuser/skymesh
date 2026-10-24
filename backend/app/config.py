"""Runtime configuration, read from environment variables (prefix SKYMESH_)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(f"SKYMESH_{name}", default)


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", str(BACKEND_DIR / "data"))))
    db_path: Path | None = None
    frontend_dir: Path = field(default_factory=lambda: Path(_env("FRONTEND_DIR", str(REPO_DIR / "frontend"))))
    session_hours: int = int(_env("SESSION_HOURS", "12"))
    # Cookie "Secure" flag: on when serving over HTTPS (required for phone geolocation anyway).
    secure_cookies: bool = _env("SECURE_COOKIES", "0") == "1"
    allow_self_registration: bool = _env("ALLOW_SELF_REGISTRATION", "1") == "1"
    max_body_bytes: int = int(_env("MAX_BODY_BYTES", str(256 * 1024)))
    max_upload_bytes: int = int(_env("MAX_UPLOAD_BYTES", str(60 * 1024 * 1024)))
    # Location freshness thresholds (seconds since observation).
    live_s: int = int(_env("LIVE_S", "30"))
    recent_s: int = int(_env("RECENT_S", "120"))
    stale_s: int = int(_env("STALE_S", "900"))
    # Accuracy worse than this (metres) is LOW_CONFIDENCE.
    low_confidence_m: float = float(_env("LOW_CONFIDENCE_M", "25"))
    # Device considered offline when no heartbeat for this long.
    device_offline_s: int = int(_env("DEVICE_OFFLINE_S", "90"))
    # Accept client timestamps only within this skew of server time.
    max_clock_skew_s: int = int(_env("MAX_CLOCK_SKEW_S", "300"))
    max_backdate_s: int = int(_env("MAX_BACKDATE_S", "86400"))
    camera_fetch_timeout_s: float = float(_env("CAMERA_FETCH_TIMEOUT_S", "4"))
    rate_limits_enabled: bool = _env("RATE_LIMITS", "1") == "1"
    # Port devices use (plain HTTP on the LAN) and the UDP port for "where is the server?" broadcasts.
    http_port: int = int(_env("HTTP_PORT", "8000"))
    https_port: int = int(_env("HTTPS_PORT", "8443"))
    discovery_port: int = int(_env("DISCOVERY_PORT", "47808"))
    # Import the default floor plan etc. on startup when the database is empty.
    auto_bootstrap: bool = _env("AUTO_BOOTSTRAP", "1") == "1"

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        if self.db_path is None:
            self.db_path = Path(_env("DB_PATH", str(self.data_dir / "skymesh.db")))
        for sub in ("maps", "maps/originals", "maps/rendered", "videos", "snapshots"):
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)

    @property
    def maps_dir(self) -> Path:
        return self.data_dir / "maps"
