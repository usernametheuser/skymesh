"""Authentication and user accounts."""
from __future__ import annotations

import secrets
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..db import Tx, new_id, now_iso, row, rows
from ..security import (SESSION_COOKIE, Principal, audit, client_ip, create_session, current_user, dummy_verify,
                        get_conn, hash_password, permissions_for, rate_limit, require, verify_password)

router = APIRouter(prefix="/api/v1", tags=["auth"])

USERNAME_RE = r"^[A-Za-z0-9_.\-]{3,32}$"


class RegisterIn(BaseModel):
    username: str = Field(pattern=USERNAME_RE)
    display_name: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=10, max_length=128)


class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=128)
    native_client: bool = False


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=128)
    new_password: str = Field(min_length=10, max_length=128)


class UserCreateIn(RegisterIn):
    role: str = Field(pattern="^(admin|responder|user)$")


class UserPatchIn(BaseModel):
    role: str | None = Field(default=None, pattern="^(admin|responder|user)$")
    active: bool | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=60)
    new_password: str | None = Field(default=None, min_length=10, max_length=128)


def public_user(u: dict) -> dict:
    return {"id": u["id"], "username": u["username"], "display_name": u["display_name"], "role": u["role"],
            "active": bool(u["active"]), "created_at": u["created_at"]}


def insert_user(conn: sqlite3.Connection, username: str, display_name: str, password: str, role: str) -> dict:
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        raise HTTPException(409, "Username already taken")
    uid = new_id("usr")
    now = now_iso()
    conn.execute("INSERT INTO users (id, username, display_name, password_hash, role, created_at) VALUES (?,?,?,?,?,?)",
                 (uid, username, display_name, hash_password(password), role, now))
    # Everyone receives campus announcements.
    for c in conn.execute("SELECT id FROM conversations WHERE kind = 'announcement'").fetchall():
        conn.execute("INSERT OR IGNORE INTO conversation_members (conversation_id, user_id, joined_at) VALUES (?,?,?)",
                     (c["id"], uid, now))
    return row(conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())


def _set_cookie(request: Request, response: Response, token: str) -> None:
    cfg = request.app.state.settings
    # The same server answers on HTTP (devices, localhost) and HTTPS (phones): mark the cookie
    # Secure whenever the sign-in itself arrived over HTTPS.
    secure = cfg.secure_cookies or request.url.scheme == "https"
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", secure=secure,
                        max_age=cfg.session_hours * 3600, path="/")


class SetupIn(RegisterIn):
    setup_code: str = Field(min_length=4, max_length=20)


def _needs_setup(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM users WHERE role = 'admin' AND active = 1").fetchone() is None


@router.get("/auth/session")
def session(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Always 200: tells the client whether it is signed in, and whether first-start setup is pending."""
    from ..security import principal_from_request
    p = principal_from_request(request, conn)
    cfg = request.app.state.settings
    out = {"authenticated": False, "needs_setup": _needs_setup(conn),
           "allow_registration": cfg.allow_self_registration, "secure": request.url.scheme == "https"}
    if p and p.kind == "user":
        u = row(conn.execute("SELECT * FROM users WHERE id = ?", (p.user_id,)).fetchone())
        out.update(authenticated=True, user=public_user(u), permissions=permissions_for(u["role"]))
    return out


@router.post("/setup", status_code=201, dependencies=[Depends(rate_limit("setup", 10, 600, count="failure"))])
def first_setup(body: SetupIn, request: Request, response: Response, conn: sqlite3.Connection = Depends(get_conn)):
    """Create the first administrator, proven by the code printed on the server console."""
    code = request.app.state.setup_code
    with Tx(conn):
        if not _needs_setup(conn):
            raise HTTPException(409, "Setup is already complete")
        given = body.setup_code.strip().upper().replace(" ", "")
        if not code or not secrets.compare_digest(given.encode(), code.encode()):
            audit(conn, None, "setup.code_rejected", None, {}, client_ip(request))
            raise HTTPException(403, "Wrong setup code. Use the code shown in the SkyMesh server window.")
        u = insert_user(conn, body.username, body.display_name, body.password, "admin")
        token = create_session(conn, u["id"], request.app.state.settings.session_hours,
                               request.headers.get("user-agent"))
        audit(conn, Principal("user", user_id=u["id"]), "setup.admin_created", u["id"], {}, client_ip(request))
    request.app.state.setup_code = None
    _set_cookie(request, response, token)
    return {"user": public_user(u), "permissions": permissions_for(u["role"])}


# Counts accounts actually created (a taken username or short password costs nothing): 20 per device per hour.
@router.post("/auth/register", status_code=201, dependencies=[Depends(rate_limit("register", 20, 3600, count="success"))])
def register(body: RegisterIn, request: Request, response: Response, conn: sqlite3.Connection = Depends(get_conn)):
    if not request.app.state.settings.allow_self_registration:
        raise HTTPException(403, "Self-registration is disabled; ask an administrator")
    with Tx(conn):
        u = insert_user(conn, body.username, body.display_name, body.password, "user")
        token = create_session(conn, u["id"], request.app.state.settings.session_hours,
                               request.headers.get("user-agent"))
        audit(conn, None, "user.register", u["id"], {"username": u["username"]}, client_ip(request))
    _set_cookie(request, response, token)
    return {"user": public_user(u), "permissions": permissions_for(u["role"])}


@router.post("/auth/login", dependencies=[Depends(rate_limit("login", 10, 300, count="failure"))])
def login(body: LoginIn, request: Request, response: Response, conn: sqlite3.Connection = Depends(get_conn)):
    u = row(conn.execute("SELECT * FROM users WHERE username = ?", (body.username,)).fetchone())
    if not u:
        dummy_verify(body.password)
    if not u or not u["active"] or not verify_password(body.password, u["password_hash"]):
        with Tx(conn):
            audit(conn, None, "auth.login_failed", None, {"username": body.username[:64]}, client_ip(request))
        raise HTTPException(401, "Invalid username or password")
    with Tx(conn):
        token = create_session(conn, u["id"], request.app.state.settings.session_hours,
                               request.headers.get("user-agent"))
        audit(conn, Principal("user", user_id=u["id"]), "auth.login", u["id"], {}, client_ip(request))
    _set_cookie(request, response, token)
    out = {"user": public_user(u), "permissions": permissions_for(u["role"])}
    if body.native_client:
        out["token"] = token  # native apps store it in platform secure storage; browsers use the HttpOnly cookie
    return out


@router.post("/auth/logout")
def logout(request: Request, response: Response, p: Principal = Depends(current_user),
           conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        conn.execute("UPDATE sessions SET revoked = 1 WHERE id = ?", (p.session_id,))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me")
def me(p: Principal = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    u = row(conn.execute("SELECT * FROM users WHERE id = ?", (p.user_id,)).fetchone())
    return {"user": public_user(u), "permissions": permissions_for(u["role"])}


@router.post("/auth/password")
def change_password(body: PasswordIn, request: Request, p: Principal = Depends(current_user),
                    conn: sqlite3.Connection = Depends(get_conn)):
    u = row(conn.execute("SELECT * FROM users WHERE id = ?", (p.user_id,)).fetchone())
    if not verify_password(body.current_password, u["password_hash"]):
        raise HTTPException(403, "Current password is incorrect")
    with Tx(conn):
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(body.new_password), p.user_id))
        conn.execute("UPDATE sessions SET revoked = 1 WHERE user_id = ? AND id != ?", (p.user_id, p.session_id))
        audit(conn, p, "auth.password_changed", p.user_id, {}, client_ip(request))
    return {"ok": True}


# ---------------------------------------------------------------- user directory / admin

@router.get("/users")
def list_users(request: Request, p: Principal = Depends(require("user.directory")),
               conn: sqlite3.Connection = Depends(get_conn)):
    online = request.app.state.hub.online_user_ids()
    us = rows(conn.execute("SELECT * FROM users WHERE active = 1 OR ? ORDER BY display_name",
                           (1 if p.can("user.manage") else 0,)))
    out = []
    for u in us:
        d = {"id": u["id"], "display_name": u["display_name"], "username": u["username"], "online": u["id"] in online,
             "role": u["role"]}
        if p.can("user.manage"):
            d.update(active=bool(u["active"]), created_at=u["created_at"])
        out.append(d)
    return out


@router.post("/users", status_code=201)
def create_user(body: UserCreateIn, request: Request, p: Principal = Depends(require("user.manage")),
                conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        u = insert_user(conn, body.username, body.display_name, body.password, body.role)
        audit(conn, p, "user.create", u["id"], {"role": body.role}, client_ip(request))
    return public_user(u)


@router.patch("/users/{user_id}")
def patch_user(user_id: str, body: UserPatchIn, request: Request, p: Principal = Depends(require("user.manage")),
               conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        u = row(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())
        if not u:
            raise HTTPException(404, "User not found")
        if user_id == p.user_id and (body.role not in (None, "admin") or body.active is False):
            raise HTTPException(409, "You cannot demote or deactivate your own account")
        if body.role is not None:
            conn.execute("UPDATE users SET role = ? WHERE id = ?", (body.role, user_id))
        if body.active is not None:
            conn.execute("UPDATE users SET active = ? WHERE id = ?", (int(body.active), user_id))
            if not body.active:
                conn.execute("UPDATE sessions SET revoked = 1 WHERE user_id = ?", (user_id,))
        if body.display_name is not None:
            conn.execute("UPDATE users SET display_name = ? WHERE id = ?", (body.display_name, user_id))
        if body.new_password is not None:
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(body.new_password), user_id))
            conn.execute("UPDATE sessions SET revoked = 1 WHERE user_id = ?", (user_id,))
        audit(conn, p, "user.update", user_id,
              {k: v for k, v in body.model_dump().items() if v is not None and k != "new_password"}
              | ({"password_reset": True} if body.new_password else {}), client_ip(request))
        u = row(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())
    return public_user(u)
