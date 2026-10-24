"""WebSocket hub: pushes live events to authorized dashboard and phone clients.

Endpoints run in a threadpool, so publishing is thread-safe: events are handed to
the event loop captured at startup. Each event carries an audience filter that is
evaluated per connection, so a client only receives what its principal may see.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Callable

from fastapi import WebSocket

from .security import Principal

log = logging.getLogger("skymesh.realtime")

Audience = Callable[[Principal], bool]


def everyone(_: Principal) -> bool:
    return True


def users(*user_ids: str | None) -> Audience:
    ids = {u for u in user_ids if u}
    return lambda p: p.user_id in ids


def perm(permission: str, *also_users: str | None) -> Audience:
    ids = {u for u in also_users if u}
    return lambda p: p.can(permission) or p.user_id in ids


@dataclass(eq=False)
class Client:
    ws: WebSocket
    principal: Principal
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=500))


class Hub:
    def __init__(self) -> None:
        self.clients: set[Client] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self) -> None:
        self.loop = asyncio.get_running_loop()

    def online_user_ids(self) -> set[str]:
        return {c.principal.user_id for c in self.clients if c.principal.user_id}

    async def serve(self, ws: WebSocket, principal: Principal) -> None:
        client = Client(ws, principal)
        self.clients.add(client)
        sender = asyncio.create_task(self._sender(client))
        if principal.user_id:
            self.publish("presence", {"user_id": principal.user_id, "online": True}, everyone)
        try:
            await ws.send_text(json.dumps({"type": "hello", "data": {"user_id": principal.user_id}}))
            while True:
                msg = await ws.receive_text()
                if msg == "ping":
                    await client.queue.put(json.dumps({"type": "pong"}))
        except Exception:  # disconnect or socket error
            pass
        finally:
            sender.cancel()
            self.clients.discard(client)
            if principal.user_id and principal.user_id not in self.online_user_ids():
                self.publish("presence", {"user_id": principal.user_id, "online": False}, everyone)

    async def _sender(self, client: Client) -> None:
        while True:
            text = await client.queue.get()
            await client.ws.send_text(text)

    def _dispatch(self, text: str, audience: Audience) -> None:
        for c in list(self.clients):
            try:
                if audience(c.principal):
                    c.queue.put_nowait(text)
            except asyncio.QueueFull:
                log.warning("dropping event for slow client %s", c.principal.user_id)

    def publish(self, event_type: str, data: dict, audience: Audience) -> None:
        if self.loop is None or self.loop.is_closed():
            return
        text = json.dumps({"type": event_type, "data": data}, default=str)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            self._dispatch(text, audience)
        else:
            self.loop.call_soon_threadsafe(self._dispatch, text, audience)
