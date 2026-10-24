from datetime import timedelta

from fastapi.testclient import TestClient

from app.db import Tx, iso, utcnow
from app.main import create_app

from .conftest import PASSWORD, enroll


def test_health_is_public(make_client):
    r = make_client().get("/api/v1/health")
    assert r.status_code == 200 and r.json()["database"] == "ok"


def test_first_start_setup_flow(cfg):
    app = create_app(cfg, start_background=False)
    code = app.state.setup_code
    assert code and len(code) == 9
    with TestClient(app) as c:
        s = c.get("/api/v1/auth/session").json()
        assert s["needs_setup"] is True and s["authenticated"] is False
        body = {"username": "boss", "display_name": "Boss", "password": "long-enough-pw"}
        assert c.post("/api/v1/setup", json={**body, "setup_code": "WRONG-CODE"}).status_code == 403
        r = c.post("/api/v1/setup", json={**body, "setup_code": code.lower()})
        assert r.status_code == 201 and r.json()["user"]["role"] == "admin"
        s = c.get("/api/v1/auth/session").json()
        assert s["authenticated"] is True and s["needs_setup"] is False
        assert c.post("/api/v1/setup", json={**body, "username": "boss2", "setup_code": code}).status_code == 409
        checklist = c.get("/api/v1/setup/checklist").json()
        assert checklist["items"][0]["done"] is True and checklist["total"] >= 8
    # Once an admin exists, a restarted server issues no setup code.
    assert create_app(cfg, start_background=False).state.setup_code is None


def test_https_certificate_chain(cfg):
    from cryptography import x509
    from app.bootstrap import ensure_cert
    cert_p, key_p, made = ensure_cert(cfg, ["10.1.2.3"])
    assert made
    leaf = x509.load_pem_x509_certificate(cert_p.read_bytes())
    ca = x509.load_pem_x509_certificate((cfg.data_dir / "tls" / "ca.pem").read_bytes())
    leaf.verify_directly_issued_by(ca)
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "10.1.2.3" in [str(i) for i in san.get_values_for_type(x509.IPAddress)]
    assert all("_" not in n for n in san.get_values_for_type(x509.DNSName))
    assert leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    # Unchanged addresses: no re-issue, and the authority phones installed stays the same.
    assert ensure_cert(cfg, ["10.1.2.3"])[2] is False
    ensure_cert(cfg, ["10.9.9.9"])
    assert x509.load_pem_x509_certificate((cfg.data_dir / "tls" / "ca.pem").read_bytes()) == ca


def test_session_endpoint_never_401(make_client, seed):
    r = make_client().get("/api/v1/auth/session")
    assert r.status_code == 200 and r.json()["authenticated"] is False and r.json()["needs_setup"] is False


def test_bootstrap_imports_floor_plan(cfg):
    from app.bootstrap import bootstrap
    from app.db import connect
    bootstrap(cfg)
    bootstrap(cfg)  # idempotent
    conn = connect(cfg.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM floors").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM map_features WHERE verified = 0").fetchone()[0] >= 26
        assert conn.execute("SELECT COUNT(*) FROM conversations WHERE kind = 'announcement'").fetchone()[0] == 1
    finally:
        conn.close()


def test_protected_endpoints_require_auth(make_client, seed):
    c = make_client()
    for path in ("/api/v1/auth/me", "/api/v1/devices", "/api/v1/locations/current", "/api/v1/incidents",
                 "/api/v1/chat/conversations", "/api/v1/overview", "/api/v1/buildings"):
        r = c.get(path)
        assert r.status_code == 401, path
        assert r.json()["error"]["code"] == "unauthenticated"


def test_invalid_credentials(make_client, seed):
    c = make_client()
    assert c.post("/api/v1/auth/login", json={"username": "alice", "password": "wrong-password"}).status_code == 401
    assert c.post("/api/v1/auth/login", json={"username": "nobody", "password": "wrong-password"}).status_code == 401


def test_login_sets_httponly_cookie_and_hides_token(make_client, seed):
    c = make_client()
    r = c.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD})
    assert "token" not in r.json()
    assert "httponly" in r.headers["set-cookie"].lower()
    assert "samesite=strict" in r.headers["set-cookie"].lower()
    assert c.get("/api/v1/auth/me").json()["user"]["username"] == "alice"


def test_native_client_bearer_token(make_client, seed):
    r = make_client().post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD, "native_client": True})
    token = r.json()["token"]
    c2 = make_client()
    assert c2.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_expired_session_rejected(login, db):
    c = login("alice")
    with Tx(db):
        db.execute("UPDATE sessions SET expires_at = ?", (iso(utcnow() - timedelta(minutes=1)),))
    assert c.get("/api/v1/auth/me").status_code == 401


def test_logout_revokes_session(login):
    c = login("alice")
    assert c.post("/api/v1/auth/logout").status_code == 200
    assert c.get("/api/v1/auth/me").status_code == 401


def test_role_enforcement(login):
    alice = login("alice")
    assert alice.post("/api/v1/devices", json={"type": "badge", "name": "x"}).status_code == 403
    assert alice.patch("/api/v1/settings", json={"values": {"camera.analysis_enabled": True}}).status_code == 403
    assert alice.get("/api/v1/audit").status_code == 403
    assert alice.post("/api/v1/buildings", json={"name": "x"}).status_code == 403
    assert alice.post("/api/v1/incidents/test", json={}).status_code == 403
    resp = login("resp")
    assert resp.post("/api/v1/devices", json={"type": "badge", "name": "x"}).status_code == 403
    assert resp.post("/api/v1/incidents/test", json={}).status_code == 201


def test_registration_creates_plain_user(make_client, seed):
    c = make_client()
    r = c.post("/api/v1/auth/register", json={"username": "carol", "display_name": "Carol", "password": "long-enough-pw",
                                               "role": "admin"})
    assert r.status_code == 201
    assert r.json()["user"]["role"] == "user"  # extra "role" field is ignored
    assert c.post("/api/v1/auth/register", json={"username": "carol", "display_name": "C",
                                                  "password": "long-enough-pw"}).status_code == 409


def test_weak_password_rejected(make_client, seed):
    r = make_client().post("/api/v1/auth/register", json={"username": "dave", "display_name": "D", "password": "short"})
    assert r.status_code == 422


def test_rate_limit_login(cfg, seed):
    cfg.rate_limits_enabled = True
    app = create_app(cfg, start_background=False)
    with TestClient(app) as c:
        codes = [c.post("/api/v1/auth/login", json={"username": "alice", "password": "nope-nope-nope"}).status_code
                 for _ in range(12)]
    assert codes[:10] == [401] * 10
    assert codes[10] == 429


def test_rate_limit_counts_real_accounts_not_mistakes(cfg, seed):
    cfg.rate_limits_enabled = True
    app = create_app(cfg, start_background=False)
    with TestClient(app) as c:
        # Taken usernames and too-short passwords do not use up the allowance...
        for _ in range(15):
            assert c.post("/api/v1/auth/register", json={"username": "alice", "display_name": "A",
                                                         "password": "long-enough-pw-1"}).status_code in (409, 422)
            assert c.post("/api/v1/auth/register", json={"username": "newbie", "display_name": "N",
                                                         "password": "x"}).status_code == 422
        # ...and successful logins never lock anyone out.
        for _ in range(15):
            assert c.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 200
        assert c.post("/api/v1/auth/register", json={"username": "newbie", "display_name": "N",
                                                     "password": "long-enough-pw-1"}).status_code == 201


def test_malformed_and_oversized_payloads(login):
    alice = login("alice")
    r = alice.post("/api/v1/chat/direct", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 422
    big = "x" * (300 * 1024)
    r = alice.post("/api/v1/chat/direct", json={"user_id": big})
    assert r.status_code == 413


def test_security_headers(make_client):
    r = make_client().get("/api/v1/health")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


def test_device_token_cannot_call_user_api(login):
    admin = login("admin")
    dev = enroll(admin, "badge", "Badge A")
    c = login("bob")
    r = c.get("/api/v1/devices", headers={"Authorization": f"Bearer {dev['device_token']}"})
    assert r.status_code == 403


def test_user_cannot_use_device_api(login):
    alice = login("alice")
    r = alice.post("/api/v1/device/heartbeat", json={"schema_version": 1, "firmware_version": "x", "uptime_s": 1,
                                                     "sequence": 1})
    assert r.status_code == 403


def test_admin_cannot_demote_self(login, seed):
    admin = login("admin")
    r = admin.patch(f"/api/v1/users/{seed['admin']['id']}", json={"role": "user"})
    assert r.status_code == 409


def test_deactivated_user_loses_sessions(login, seed):
    alice = login("alice")
    admin = login("admin")
    assert admin.patch(f"/api/v1/users/{seed['alice']['id']}", json={"active": False}).status_code == 200
    assert alice.get("/api/v1/auth/me").status_code == 401


def test_audit_records_failed_login(make_client, login, seed):
    make_client().post("/api/v1/auth/login", json={"username": "alice", "password": "bad-password-x"})
    admin = login("admin")
    actions = [a["action"] for a in admin.get("/api/v1/audit").json()]
    assert "auth.login_failed" in actions


def test_websocket_requires_session(make_client, login):
    import pytest
    from starlette.websockets import WebSocketDisconnect
    anon = make_client()
    with pytest.raises(WebSocketDisconnect):
        with anon.websocket_connect("/api/v1/ws") as ws:
            ws.receive_text()
    alice = login("alice")
    with alice.websocket_connect("/api/v1/ws") as ws:
        assert '"hello"' in ws.receive_text()
    with pytest.raises(WebSocketDisconnect):
        with alice.websocket_connect("/api/v1/ws", headers={"origin": "http://evil.example"}) as ws:
            ws.receive_text()
