"""LAN discovery: devices that lost the server broadcast on UDP 47808 and get its current address back.

The reply is signed with HMAC-SHA256 keyed by the device's own token hash (which both sides know), so a
device only follows a reply from the real SkyMesh server, never from another machine on the network.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import time

from .bootstrap import lan_ips
from .config import Settings
from .db import connect

log = logging.getLogger("skymesh.discovery")


def server_url_for(sender_ip: str, http_port: int) -> str | None:
    """The address of this computer on the same network as the device."""
    ips = lan_ips()
    if not ips:
        return None
    try:
        sender = ipaddress.ip_address(sender_ip)
        same = [ip for ip in ips if ipaddress.ip_network(f"{ip}/24", strict=False).network_address
                == ipaddress.ip_network(f"{sender}/24", strict=False).network_address]
    except ValueError:
        same = []
    return f"http://{(same or ips)[0]}:{http_port}"


def discovery_reply(cfg: Settings, payload: dict, sender_ip: str, http_port: int) -> dict | None:
    if payload.get("skymesh") != "discover":
        return None
    device_id, nonce = str(payload.get("id", ""))[:40], str(payload.get("nonce", ""))[:64]
    if not device_id or len(nonce) < 16:
        return None
    conn = connect(cfg.db_path)
    try:
        row = conn.execute("SELECT token_hash FROM devices WHERE id = ? AND revoked_at IS NULL AND token_hash IS NOT NULL",
                           (device_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None  # unknown or revoked devices learn nothing
    url = server_url_for(sender_ip, http_port)
    if not url:
        return None
    sig = hmac.new(row["token_hash"].encode(), f"{nonce}|{url}".encode(), hashlib.sha256).hexdigest()
    return {"skymesh": "here", "url": url, "sig": sig}


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, cfg: Settings, http_port: int):
        self.cfg, self.http_port = cfg, http_port
        self.recent: dict[str, float] = {}

    def connection_made(self, transport):  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        ip = addr[0]
        now = time.monotonic()
        if now - self.recent.get(ip, 0) < 1.0 or len(data) > 512:  # at most one answer per second per sender
            return
        self.recent[ip] = now
        try:
            payload = json.loads(data.decode("utf-8"))
            reply = discovery_reply(self.cfg, payload, ip, self.http_port) if isinstance(payload, dict) else None
        except (ValueError, UnicodeDecodeError):
            return
        if reply:
            self.transport.sendto(json.dumps(reply).encode(), addr)
            log.info("discovery: told %s where the server is", ip)


async def start_discovery(cfg: Settings, http_port: int) -> asyncio.BaseTransport | None:
    loop = asyncio.get_running_loop()
    try:
        transport, _ = await loop.create_datagram_endpoint(lambda: _Protocol(cfg, http_port),
                                                           local_addr=("0.0.0.0", cfg.discovery_port))
        return transport
    except OSError as e:  # port busy: everything else still works, devices just cannot auto-find a moved server
        log.warning("discovery responder not started on UDP %s: %s", cfg.discovery_port, e)
        # Printed to the SkyMesh window too: without it, devices cannot follow this computer to a new address.
        print(f"\n  WARNING: UDP port {cfg.discovery_port} is in use (is another copy of SkyMesh running?).\n"
              "           Badges and cameras will not find this server after a network change.\n"
              "           Close the other copy and restart SkyMesh.\n", flush=True)
        return None
