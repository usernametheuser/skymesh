"""Persistent direct, group and announcement messaging with delivery receipts."""
from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..db import Tx, get_setting, new_id, now_iso, row, rows
from ..realtime import users
from ..security import Principal, audit, client_ip, current_user, get_conn, rate_limit, require

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


class DirectIn(BaseModel):
    user_id: str = Field(max_length=40)


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    member_ids: list[str] = Field(default_factory=list, max_length=200)


class MemberIn(BaseModel):
    user_id: str = Field(max_length=40)


class MessageIn(BaseModel):
    client_msg_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    body: str = Field(min_length=1, max_length=4000)


class ReceiptIn(BaseModel):
    message_ids: list[int] = Field(min_length=1, max_length=200)
    status: Literal["delivered", "read"]


class ReportIn(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


def _member_ids(conn: sqlite3.Connection, conv_id: str) -> list[str]:
    return [r["user_id"] for r in conn.execute("SELECT user_id FROM conversation_members WHERE conversation_id = ?",
                                               (conv_id,))]


def _require_member(conn: sqlite3.Connection, p: Principal, conv_id: str) -> dict:
    c = row(conn.execute("SELECT c.*, m.member_role FROM conversations c JOIN conversation_members m "
                         "ON m.conversation_id = c.id AND m.user_id = ? WHERE c.id = ?", (p.user_id, conv_id)).fetchone())
    if not c:
        raise HTTPException(404, "Conversation not found")
    return c


def _message_out(conn: sqlite3.Connection, m: dict) -> dict:
    stats = conn.execute("SELECT COUNT(*) AS n, COUNT(delivered_at) AS d, COUNT(read_at) AS r FROM message_receipts "
                         "WHERE message_id = ?", (m["id"],)).fetchone()
    sender = conn.execute("SELECT display_name FROM users WHERE id = ?", (m["sender_id"],)).fetchone()
    deleted = m["deleted_at"] is not None
    return {"id": m["id"], "conversation_id": m["conversation_id"], "sender_id": m["sender_id"],
            "sender_name": sender["display_name"] if sender else "(deleted user)", "client_msg_id": m["client_msg_id"],
            "body": "[message removed by a moderator]" if deleted else m["body"], "deleted": deleted,
            "created_at": m["created_at"], "recipients": stats["n"], "delivered": stats["d"], "read": stats["r"]}


def _conversation_out(conn: sqlite3.Connection, c: dict, me: str) -> dict:
    members = rows(conn.execute("SELECT u.id, u.display_name FROM conversation_members m JOIN users u "
                                "ON u.id = m.user_id WHERE m.conversation_id = ?", (c["id"],)))
    name = c["name"]
    if c["kind"] == "direct":
        other = [m for m in members if m["id"] != me]
        name = other[0]["display_name"] if other else "(just you)"
    last = conn.execute("SELECT * FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT 1", (c["id"],)).fetchone()
    unread = conn.execute("SELECT COUNT(*) AS n FROM message_receipts r JOIN messages m ON m.id = r.message_id "
                          "WHERE m.conversation_id = ? AND r.user_id = ? AND r.read_at IS NULL",
                          (c["id"], me)).fetchone()["n"]
    return {"id": c["id"], "kind": c["kind"], "name": name, "members": members, "unread": unread,
            "last_message": _message_out(conn, dict(last)) if last else None, "created_at": c["created_at"]}


@router.get("/conversations")
def conversations(p: Principal = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    cs = rows(conn.execute("SELECT c.* FROM conversations c JOIN conversation_members m ON m.conversation_id = c.id "
                           "WHERE m.user_id = ?", (p.user_id,)))
    out = [_conversation_out(conn, c, p.user_id) for c in cs]
    out.sort(key=lambda c: (c["last_message"] or {}).get("created_at") or c["created_at"], reverse=True)
    return out


@router.post("/direct")
def open_direct(body: DirectIn, p: Principal = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    if body.user_id == p.user_id:
        raise HTTPException(422, "Cannot open a direct conversation with yourself")
    with Tx(conn):
        if not conn.execute("SELECT 1 FROM users WHERE id = ? AND active = 1", (body.user_id,)).fetchone():
            raise HTTPException(404, "User not found")
        key = ":".join(sorted([p.user_id, body.user_id]))
        c = row(conn.execute("SELECT * FROM conversations WHERE direct_key = ?", (key,)).fetchone())
        if not c:
            cid, now = new_id("conv"), now_iso()
            conn.execute("INSERT INTO conversations (id, kind, direct_key, created_by, created_at) VALUES (?,?,?,?,?)",
                         (cid, "direct", key, p.user_id, now))
            for uid in (p.user_id, body.user_id):
                conn.execute("INSERT INTO conversation_members (conversation_id, user_id, joined_at) VALUES (?,?,?)",
                             (cid, uid, now))
            c = row(conn.execute("SELECT * FROM conversations WHERE id = ?", (cid,)).fetchone())
    return _conversation_out(conn, c, p.user_id)


@router.post("/groups", status_code=201)
def create_group(body: GroupIn, request: Request, p: Principal = Depends(current_user),
                 conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        ids = set(body.member_ids) | {p.user_id}
        found = {r["id"] for r in conn.execute(
            f"SELECT id FROM users WHERE active = 1 AND id IN ({','.join('?' * len(ids))})", tuple(ids))}
        if found != ids:
            raise HTTPException(422, "Unknown member id(s)")
        cid, now = new_id("conv"), now_iso()
        conn.execute("INSERT INTO conversations (id, kind, name, created_by, created_at) VALUES (?,?,?,?,?)",
                     (cid, "group", body.name, p.user_id, now))
        for uid in ids:
            conn.execute("INSERT INTO conversation_members (conversation_id, user_id, member_role, joined_at) "
                         "VALUES (?,?,?,?)", (cid, uid, "owner" if uid == p.user_id else "member", now))
        c = row(conn.execute("SELECT * FROM conversations WHERE id = ?", (cid,)).fetchone())
    out = _conversation_out(conn, c, p.user_id)
    request.app.state.hub.publish("conversation", {"id": cid}, users(*ids))
    return out


@router.post("/groups/{conv_id}/members")
def add_member(conv_id: str, body: MemberIn, request: Request, p: Principal = Depends(current_user),
               conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        c = _require_member(conn, p, conv_id)
        if c["kind"] != "group" or (c["member_role"] != "owner" and not p.can("chat.moderate")):
            raise HTTPException(403, "Only the group owner can add members")
        if not conn.execute("SELECT 1 FROM users WHERE id = ? AND active = 1", (body.user_id,)).fetchone():
            raise HTTPException(404, "User not found")
        conn.execute("INSERT OR IGNORE INTO conversation_members (conversation_id, user_id, joined_at) VALUES (?,?,?)",
                     (conv_id, body.user_id, now_iso()))
    request.app.state.hub.publish("conversation", {"id": conv_id}, users(*_member_ids(conn, conv_id)))
    return {"ok": True}


@router.delete("/groups/{conv_id}/members/{user_id}")
def remove_member(conv_id: str, user_id: str, p: Principal = Depends(current_user),
                  conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        c = _require_member(conn, p, conv_id)
        if c["kind"] != "group":
            raise HTTPException(403, "Not a group")
        if user_id != p.user_id and c["member_role"] != "owner" and not p.can("chat.moderate"):
            raise HTTPException(403, "Only the owner can remove other members")
        conn.execute("DELETE FROM conversation_members WHERE conversation_id = ? AND user_id = ?", (conv_id, user_id))
    return {"ok": True}


@router.get("/conversations/{conv_id}/messages")
def messages(conv_id: str, before: int | None = None, limit: int = 50, p: Principal = Depends(current_user),
             conn: sqlite3.Connection = Depends(get_conn)):
    _require_member(conn, p, conv_id)
    limit = min(max(limit, 1), 200)
    if before:
        ms = rows(conn.execute("SELECT * FROM messages WHERE conversation_id = ? AND id < ? ORDER BY id DESC LIMIT ?",
                               (conv_id, before, limit)))
    else:
        ms = rows(conn.execute("SELECT * FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
                               (conv_id, limit)))
    return [_message_out(conn, m) for m in reversed(ms)]


@router.post("/conversations/{conv_id}/messages", dependencies=[Depends(rate_limit("chat_send", 60, 60))])
def send(conv_id: str, body: MessageIn, request: Request, p: Principal = Depends(current_user),
         conn: sqlite3.Connection = Depends(get_conn)):
    """Idempotent on (sender, client_msg_id): a client retrying after a reconnect never duplicates."""
    hub = request.app.state.hub
    with Tx(conn):
        c = _require_member(conn, p, conv_id)
        if c["kind"] == "announcement" and not p.can("chat.announce"):
            raise HTTPException(403, "Only administrators can post announcements")
        if len(body.body) > int(get_setting(conn, "chat.max_message_chars")):
            raise HTTPException(422, "Message too long")
        existing = row(conn.execute("SELECT * FROM messages WHERE sender_id = ? AND client_msg_id = ?",
                                    (p.user_id, body.client_msg_id)).fetchone())
        if existing:
            if existing["conversation_id"] != conv_id:
                raise HTTPException(409, "client_msg_id already used in another conversation")
            return {"message": _message_out(conn, existing), "duplicate": True}
        cur = conn.execute("INSERT INTO messages (conversation_id, sender_id, client_msg_id, body, created_at) "
                           "VALUES (?,?,?,?,?)", (conv_id, p.user_id, body.client_msg_id, body.body, now_iso()))
        mid = cur.lastrowid
        members = _member_ids(conn, conv_id)
        for uid in members:
            if uid != p.user_id:
                conn.execute("INSERT INTO message_receipts (message_id, user_id) VALUES (?,?)", (mid, uid))
        m = _message_out(conn, row(conn.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()))
    hub.publish("message", m, users(*members))
    return {"message": m, "duplicate": False}


@router.post("/receipts")
def receipts(body: ReceiptIn, request: Request, p: Principal = Depends(current_user),
             conn: sqlite3.Connection = Depends(get_conn)):
    now = now_iso()
    touched: dict[str, list[int]] = {}
    with Tx(conn):
        for mid in body.message_ids:
            r = conn.execute("SELECT m.sender_id, m.conversation_id FROM message_receipts r JOIN messages m "
                             "ON m.id = r.message_id WHERE r.message_id = ? AND r.user_id = ?", (mid, p.user_id)).fetchone()
            if not r:
                continue  # not a recipient: silently ignore rather than reveal message existence
            conn.execute("UPDATE message_receipts SET delivered_at = COALESCE(delivered_at, ?) WHERE message_id = ? "
                         "AND user_id = ?", (now, mid, p.user_id))
            if body.status == "read":
                conn.execute("UPDATE message_receipts SET read_at = COALESCE(read_at, ?) WHERE message_id = ? "
                             "AND user_id = ?", (now, mid, p.user_id))
            touched.setdefault(r["sender_id"], []).append(mid)
    for sender, mids in touched.items():
        for mid in mids:
            m = _message_out(conn, row(conn.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()))
            request.app.state.hub.publish("message_status", m, users(sender, p.user_id))
    return {"updated": sum(len(v) for v in touched.values())}


@router.get("/search")
def search(q: str, p: Principal = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    q = q.strip()
    if len(q) < 2 or len(q) > 100:
        raise HTTPException(422, "Query must be 2-100 characters")
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    ms = rows(conn.execute(
        "SELECT m.* FROM messages m JOIN conversation_members cm ON cm.conversation_id = m.conversation_id "
        "AND cm.user_id = ? WHERE m.deleted_at IS NULL AND m.body LIKE ? ESCAPE '\\' ORDER BY m.id DESC LIMIT 50",
        (p.user_id, like)))
    return [_message_out(conn, m) for m in ms]


@router.post("/messages/{message_id}/report", status_code=201)
def report(message_id: int, body: ReportIn, request: Request, p: Principal = Depends(current_user),
           conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        m = conn.execute("SELECT conversation_id FROM messages WHERE id = ?", (message_id,)).fetchone()
        if not m:
            raise HTTPException(404, "Message not found")
        _require_member(conn, p, m["conversation_id"])
        rid = new_id("rep")
        conn.execute("INSERT INTO message_reports (id, message_id, reporter_id, reason, created_at) VALUES (?,?,?,?,?)",
                     (rid, message_id, p.user_id, body.reason, now_iso()))
        audit(conn, p, "chat.report", str(message_id), {"report_id": rid}, client_ip(request))
    return {"id": rid}


@router.get("/reports")
def list_reports(p: Principal = Depends(require("chat.moderate")), conn: sqlite3.Connection = Depends(get_conn)):
    rs = rows(conn.execute(
        "SELECT r.*, u.display_name AS reporter_name, m.body, m.sender_id, m.deleted_at FROM message_reports r "
        "JOIN users u ON u.id = r.reporter_id JOIN messages m ON m.id = r.message_id ORDER BY r.created_at DESC LIMIT 200"))
    return rs


@router.post("/messages/{message_id}/remove")
def remove_message(message_id: int, request: Request, p: Principal = Depends(require("chat.moderate")),
                   conn: sqlite3.Connection = Depends(get_conn)):
    with Tx(conn):
        m = row(conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone())
        if not m:
            raise HTTPException(404, "Message not found")
        now = now_iso()
        conn.execute("UPDATE messages SET deleted_at = ?, deleted_by = ? WHERE id = ?", (now, p.user_id, message_id))
        conn.execute("UPDATE message_reports SET resolved_at = ?, resolved_by = ? WHERE message_id = ? "
                     "AND resolved_at IS NULL", (now, p.user_id, message_id))
        audit(conn, p, "chat.remove_message", str(message_id), {"conversation": m["conversation_id"]}, client_ip(request))
        out = _message_out(conn, row(conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()))
    request.app.state.hub.publish("message_status", out, users(*_member_ids(conn, m["conversation_id"])))
    return out
