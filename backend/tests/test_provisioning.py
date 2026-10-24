import hashlib
import hmac
import json
import socket
import time

import pytest

from app.discovery import discovery_reply

from .conftest import enroll


def _device_side_check(token: str, nonce: str, reply: dict) -> bool:
    """What the firmware does: HMAC key is the hex SHA-256 of its own token."""
    key = hashlib.sha256(token.encode()).hexdigest().encode()
    return hmac.compare_digest(reply["sig"], hmac.new(key, f"{nonce}|{reply['url']}".encode(), hashlib.sha256).hexdigest())


def test_discovery_reply_is_signed_for_that_device(login, cfg, monkeypatch):
    monkeypatch.setattr("app.discovery.lan_ips", lambda: ["10.0.0.5", "192.168.1.6"])
    admin = login("admin")
    dev = enroll(admin, "badge", "B1")
    nonce = "a1b2c3d4e5f6a7b8c9d0"
    r = discovery_reply(cfg, {"skymesh": "discover", "id": dev["device"]["id"], "nonce": nonce}, "192.168.1.40", 8000)
    assert r["url"] == "http://192.168.1.6:8000"  # the address on the device's own subnet
    assert _device_side_check(dev["device_token"], nonce, r)
    assert not _device_side_check("smd_someone-else", nonce, r)


def test_discovery_ignores_unknown_revoked_and_malformed(login, cfg, monkeypatch):
    monkeypatch.setattr("app.discovery.lan_ips", lambda: ["192.168.1.6"])
    admin = login("admin")
    dev = enroll(admin, "badge", "B1")
    nonce = "0123456789abcdef0123"
    assert discovery_reply(cfg, {"skymesh": "discover", "id": "badge_nope", "nonce": nonce}, "192.168.1.9", 8000) is None
    assert discovery_reply(cfg, {"skymesh": "discover", "id": dev["device"]["id"], "nonce": "short"}, "192.168.1.9", 8000) is None
    assert discovery_reply(cfg, {"hello": 1}, "192.168.1.9", 8000) is None
    admin.post(f"/api/v1/devices/{dev['device']['id']}/revoke")
    assert discovery_reply(cfg, {"skymesh": "discover", "id": dev["device"]["id"], "nonce": nonce}, "192.168.1.9", 8000) is None


def test_discovery_udp_roundtrip(cfg, login, monkeypatch):
    """The real responder on a UDP socket (loopback)."""
    import asyncio
    from app.discovery import start_discovery
    monkeypatch.setattr("app.discovery.lan_ips", lambda: ["127.0.0.1"])
    admin = login("admin")
    dev = enroll(admin, "camera_node", "C1")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        cfg.discovery_port = probe.getsockname()[1]
    nonce = "fedcba9876543210fedc"

    async def run():
        transport = await start_discovery(cfg, 8000)
        assert transport is not None
        loop = asyncio.get_running_loop()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setblocking(False)
        s.sendto(json.dumps({"skymesh": "discover", "id": dev["device"]["id"], "nonce": nonce}).encode(),
                 ("127.0.0.1", cfg.discovery_port))
        data = await asyncio.wait_for(loop.sock_recv(s, 1024), 3)
        s.close()
        transport.close()
        return json.loads(data)

    reply = asyncio.run(run())
    assert reply["url"] == "http://127.0.0.1:8000" and _device_side_check(dev["device_token"], nonce, reply)


def test_firmware_endpoints_admin_only(login):
    from app.routers.devices import FIRMWARE_DIR
    if not (FIRMWARE_DIR / "manifest.json").exists():
        pytest.skip("firmware not built")
    admin, alice = login("admin"), login("alice")
    assert alice.get("/api/v1/firmware").status_code == 403
    m = admin.get("/api/v1/firmware").json()
    roles = {i["role"] for i in m["images"]}
    assert roles == {"badge", "camera_node"}
    img = m["images"][0]
    r = admin.get(f"/api/v1/firmware/{img['file']}")
    assert r.status_code == 200 and len(r.content) == img["size"]
    assert hashlib.sha256(r.content).hexdigest() == img["sha256"]
    assert admin.get("/api/v1/firmware/..%2Fmanifest.json").status_code == 404


def test_config_gives_device_and_phone_addresses(login, monkeypatch):
    monkeypatch.setattr("app.bootstrap.lan_ips", lambda: ["192.168.1.6"])
    c = login("alice").get("/api/v1/config").json()
    assert c["device_url"] == "http://192.168.1.6:8000"
    assert c["phone_url"].startswith(("https://192.168.1.6:", "http://192.168.1.6:"))
