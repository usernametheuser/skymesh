"""Authentication, role-based authorization, rate limiting and audit logging."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterator

import bcrypt
from fastapi import Depends, HTTPException, Request, status

from .db import connect, iso, now_iso, parse_iso, utcnow

SESSION_COOKIE = "skymesh_session"
USER_TOKEN_PREFIX = "sms_"
DEVICE_TOKEN_PREFIX = "smd_"

ROLES = ("admin", "responder", "user")

# permission -> roles that hold it. Ordinary users get only what is listed for "user".
PERMISSIONS: dict[str, set[str]] = {
    "location.view_all": {"admin", "responder"},
    "incident.manage": {"admin", "responder"},
    "incident.test": {"admin", "responder"},
    "incident.view_all": {"admin", "responder"},
    "device.view_all": {"admin", "responder"},
    "device.enroll": {"admin"},
    "map.edit": {"admin"},
    "map.train": {"admin", "responder"},
    "camera.view": {"admin", "responder"},
    "camera.configure": {"admin"},
    "association.confirm": {"admin", "responder"},
    "user.manage": {"admin"},
    "user.directory": {"admin", "responder", "user"},
    "settings.manage": {"admin"},
    "audit.view": {"admin"},
    "chat.announce": {"admin"},
    "chat.moderate": {"admin"},
    "topology.view": {"admin", "responder"},
    "events.view": {"admin", "responder"},
    "reports.view": {"admin", "responder"},
    "diagnostics.view": {"admin"},
}


def permissions_for(role: str) -> list[str]:
    return sorted(p for p, roles in PERMISSIONS.items() if role in roles)


@dataclass
class Principal:
    kind: str  # "user" | "device"
    user_id: str | None = None
    username: str | None = None
    display_name: str | None = None
    role: str | None = None
    device_id: str | None = None
    device_type: str | None = None
    session_id: str | None = None

    def can(self, perm: str) -> bool:
        return self.kind == "user" and self.role in PERMISSIONS.get(perm, set())


# ---------------------------------------------------------------- passwords / tokens

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode())
    except ValueError:
        return False


# Constant-time dummy hash so unknown usernames cost the same as wrong passwords.
_DUMMY_HASH = bcrypt.hashpw(b"skymesh-dummy", bcrypt.gensalt(rounds=12)).decode()


def dummy_verify(password: str) -> None:
    verify_password(password, _DUMMY_HASH)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_user_token() -> str:
    return USER_TOKEN_PREFIX + secrets.token_urlsafe(32)


def new_device_token() -> str:
    return DEVICE_TOKEN_PREFIX + secrets.token_urlsafe(32)


def create_session(conn: sqlite3.Connection, user_id: str, hours: int, user_agent: str | None) -> str:
    token = new_user_token()
    now = utcnow()
    conn.execute(
        "INSERT INTO sessions (id, user_id, token_hash, created_at, expires_at, user_agent) VALUES (?,?,?,?,?,?)",
        (secrets.token_hex(8), user_id, token_hash(token), iso(now), iso(now + timedelta(hours=hours)),
         (user_agent or "")[:200]),
    )
    return token


# ---------------------------------------------------------------- request dependencies

def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def resolve_token(conn: sqlite3.Connection, token: str | None) -> Principal | None:
    if not token:
        return None
    th = token_hash(token)
    if token.startswith(USER_TOKEN_PREFIX):
        r = conn.execute(
            "SELECT s.id AS sid, s.expires_at, s.revoked, u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ?", (th,)).fetchone()
        if not r or r["revoked"] or not r["active"] or parse_iso(r["expires_at"]) < utcnow():
            return None
        return Principal("user", user_id=r["id"], username=r["username"], display_name=r["display_name"],
                         role=r["role"], session_id=r["sid"])
    if token.startswith(DEVICE_TOKEN_PREFIX):
        r = conn.execute("SELECT * FROM devices WHERE token_hash = ?", (th,)).fetchone()
        if not r or r["revoked_at"]:
            return None
        return Principal("device", device_id=r["id"], device_type=r["type"], user_id=r["owner_user_id"])
    return None


def principal_from_request(request: Request, conn: sqlite3.Connection) -> Principal | None:
    token = _bearer(request) or request.cookies.get(SESSION_COOKIE)
    return resolve_token(conn, token)


def current_principal(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Principal:
    p = principal_from_request(request, conn)
    if p is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required")
    return p


def current_user(p: Principal = Depends(current_principal)) -> Principal:
    if p.kind != "user":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "User credentials required")
    return p


def current_device(p: Principal = Depends(current_principal)) -> Principal:
    if p.kind != "device":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Device credentials required")
    return p


def require(perm: str):
    def dep(p: Principal = Depends(current_user)) -> Principal:
        if not p.can(perm):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Missing permission: {perm}")
        return p
    return dep


# ---------------------------------------------------------------- rate limiting

class RateLimiter:
    """Sliding-window limiter kept in memory (single backend process)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self.enabled = True

    def check(self, key: str, limit: int, window_s: float, record: bool = True) -> None:
        """Raise 429 when `limit` hits are already in the window; otherwise count this one (if `record`)."""
        if not self.enabled:
            return
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - window_s:
                q.popleft()
            if len(q) >= limit:
                retry = max(1, int(window_s - (now - q[0])))
                wait = f"{-(-retry // 60)} min" if retry >= 60 else f"{retry} s"
                raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                                    f"Too many attempts from this device. Try again in {wait}.",
                                    headers={"Retry-After": str(retry)})
            if record:
                q.append(now)

    def add(self, key: str) -> None:
        if self.enabled:
            with self._lock:
                self._hits[key].append(time.monotonic())

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limit(name: str, limit: int, window_s: float, count: str = "all"):
    """count: "all" every request, "success" only requests that succeed (e.g. accounts actually created),
    "failure" only requests that fail (e.g. wrong passwords), so honest typos and retries are not punished."""
    if count == "all":
        def dep(request: Request) -> None:
            request.app.state.limiter.check(f"{name}:{client_ip(request)}", limit, window_s)
        return dep

    def counted(request: Request):
        key = f"{name}:{client_ip(request)}"
        limiter = request.app.state.limiter
        limiter.check(key, limit, window_s, record=False)
        try:
            yield
        except HTTPException as e:
            if count == "failure" and e.status_code in (401, 403):
                limiter.add(key)
            raise
        else:
            if count == "success":
                limiter.add(key)
    return counted


# ---------------------------------------------------------------- audit

def audit(conn: sqlite3.Connection, actor: Principal | None, action: str, target: str | None = None,
          detail: dict | None = None, ip: str | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_log (at, actor_user_id, actor_device_id, action, target, detail, ip) VALUES (?,?,?,?,?,?,?)",
        (now_iso(), actor.user_id if actor and actor.kind == "user" else None,
         actor.device_id if actor and actor.kind == "device" else None,
         action, target, json.dumps(detail or {}), ip),
    )
