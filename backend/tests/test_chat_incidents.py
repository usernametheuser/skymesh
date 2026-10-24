import json
import uuid

from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import enroll, heartbeat


def _cid() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------- chat

def test_direct_message_persistence_and_receipts(login, seed):
    alice, bob = login("alice"), login("bob")
    conv = alice.post("/api/v1/chat/direct", json={"user_id": seed["bob"]["id"]}).json()
    assert bob.post("/api/v1/chat/direct", json={"user_id": seed["alice"]["id"]}).json()["id"] == conv["id"]
    m = alice.post(f"/api/v1/chat/conversations/{conv['id']}/messages", json={"client_msg_id": _cid(),
                                                                            "body": "hello bob"}).json()["message"]
    assert m["recipients"] == 1 and m["delivered"] == 0
    hist = bob.get(f"/api/v1/chat/conversations/{conv['id']}/messages").json()
    assert [x["body"] for x in hist] == ["hello bob"]
    assert bob.post("/api/v1/chat/receipts", json={"message_ids": [m["id"]], "status": "read"}).json()["updated"] == 1
    m2 = alice.get(f"/api/v1/chat/conversations/{conv['id']}/messages").json()[0]
    assert m2["delivered"] == 1 and m2["read"] == 1


def test_duplicate_send_after_reconnect_is_idempotent(login, seed):
    alice = login("alice")
    conv = alice.post("/api/v1/chat/direct", json={"user_id": seed["bob"]["id"]}).json()
    body = {"client_msg_id": _cid(), "body": "once"}
    r1 = alice.post(f"/api/v1/chat/conversations/{conv['id']}/messages", json=body).json()
    r2 = alice.post(f"/api/v1/chat/conversations/{conv['id']}/messages", json=body).json()
    assert r2["duplicate"] is True and r2["message"]["id"] == r1["message"]["id"]
    assert len(alice.get(f"/api/v1/chat/conversations/{conv['id']}/messages").json()) == 1


def test_non_members_cannot_read_or_post(login, seed):
    alice = login("alice")
    conv = alice.post("/api/v1/chat/direct", json={"user_id": seed["bob"]["id"]}).json()
    alice.post(f"/api/v1/chat/conversations/{conv['id']}/messages", json={"client_msg_id": _cid(), "body": "secret"})
    resp = login("resp")
    assert resp.get(f"/api/v1/chat/conversations/{conv['id']}/messages").status_code == 404
    assert resp.post(f"/api/v1/chat/conversations/{conv['id']}/messages",
                     json={"client_msg_id": _cid(), "body": "x"}).status_code == 404
    assert resp.get("/api/v1/chat/search", params={"q": "secret"}).json() == []
    # Receipts for messages you did not receive are ignored.
    msg_id = alice.get(f"/api/v1/chat/conversations/{conv['id']}/messages").json()[0]["id"]
    assert resp.post("/api/v1/chat/receipts", json={"message_ids": [msg_id], "status": "read"}).json()["updated"] == 0


def test_group_authorization(login, seed):
    alice, bob = login("alice"), login("bob")
    g = alice.post("/api/v1/chat/groups", json={"name": "Lab team", "member_ids": [seed["resp"]["id"]]}).json()
    assert bob.get(f"/api/v1/chat/conversations/{g['id']}/messages").status_code == 404
    assert bob.post(f"/api/v1/chat/groups/{g['id']}/members", json={"user_id": seed["bob"]["id"]}).status_code == 404
    assert alice.post(f"/api/v1/chat/groups/{g['id']}/members", json={"user_id": seed["bob"]["id"]}).status_code == 200
    assert bob.get(f"/api/v1/chat/conversations/{g['id']}/messages").status_code == 200


def test_announcements_admin_only(login):
    alice, admin = login("alice"), login("admin")
    ann = [c for c in alice.get("/api/v1/chat/conversations").json() if c["kind"] == "announcement"][0]
    assert alice.post(f"/api/v1/chat/conversations/{ann['id']}/messages",
                      json={"client_msg_id": _cid(), "body": "hi"}).status_code == 403
    assert admin.post(f"/api/v1/chat/conversations/{ann['id']}/messages",
                      json={"client_msg_id": _cid(), "body": "Fire drill at 3pm"}).status_code == 200


def test_message_html_is_stored_verbatim_not_interpreted(login, seed):
    alice = login("alice")
    conv = alice.post("/api/v1/chat/direct", json={"user_id": seed["bob"]["id"]}).json()
    payload = "<img src=x onerror=alert(1)>' OR 1=1 --"
    alice.post(f"/api/v1/chat/conversations/{conv['id']}/messages", json={"client_msg_id": _cid(), "body": payload})
    assert alice.get(f"/api/v1/chat/conversations/{conv['id']}/messages").json()[0]["body"] == payload


def test_report_and_moderate(login, seed):
    alice, bob, admin = login("alice"), login("bob"), login("admin")
    conv = alice.post("/api/v1/chat/direct", json={"user_id": seed["bob"]["id"]}).json()
    m = alice.post(f"/api/v1/chat/conversations/{conv['id']}/messages",
                   json={"client_msg_id": _cid(), "body": "rude"}).json()["message"]
    assert bob.post(f"/api/v1/chat/messages/{m['id']}/report", json={"reason": "abusive"}).status_code == 201
    assert len(admin.get("/api/v1/chat/reports").json()) == 1
    assert admin.post(f"/api/v1/chat/messages/{m['id']}/remove").json()["deleted"] is True
    assert bob.get(f"/api/v1/chat/conversations/{conv['id']}/messages").json()[0]["body"].startswith("[message removed")


def test_message_delivered_over_websocket(login, seed):
    alice, bob = login("alice"), login("bob")
    conv = alice.post("/api/v1/chat/direct", json={"user_id": seed["bob"]["id"]}).json()
    with bob.websocket_connect("/api/v1/ws") as ws:
        ws.receive_text()  # hello
        alice.post(f"/api/v1/chat/conversations/{conv['id']}/messages", json={"client_msg_id": _cid(), "body": "live"})
        for _ in range(5):
            ev = json.loads(ws.receive_text())
            if ev["type"] == "message":
                assert ev["data"]["body"] == "live"
                break
        else:
            raise AssertionError("message event not received")


# ---------------------------------------------------------------- incidents

def test_phone_sos_lifecycle(login, seed):
    alice, resp = login("alice"), login("resp")
    r = alice.post("/api/v1/incidents/sos", json={"idempotency_key": "sos-key-0001"})
    assert r.status_code == 201
    inc = r.json()["incident"]
    assert inc["state"] == "NEW" and inc["location_note"].startswith("Location unavailable") and inc["x"] is None
    # Retry with same key: same incident, no duplicate.
    r2 = alice.post("/api/v1/incidents/sos", json={"idempotency_key": "sos-key-0001"})
    assert r2.status_code == 200 and r2.json()["incident"]["id"] == inc["id"]
    assert alice.post(f"/api/v1/incidents/{inc['id']}/transition", json={"to_state": "ACKNOWLEDGED"}).status_code == 403
    assert resp.post(f"/api/v1/incidents/{inc['id']}/transition", json={"to_state": "RESOLVED"}).status_code == 409
    for st in ("ACKNOWLEDGED", "RESPONDING", "RESOLVED"):
        assert resp.post(f"/api/v1/incidents/{inc['id']}/transition", json={"to_state": st}).status_code == 200
    assert resp.post(f"/api/v1/incidents/{inc['id']}/transition", json={"to_state": "FALSE_ALARM"}).status_code == 409
    detail = alice.get(f"/api/v1/incidents/{inc['id']}").json()
    assert [e["to_state"] for e in detail["timeline"] if e["to_state"]] == ["NEW", "ACKNOWLEDGED", "RESPONDING", "RESOLVED"]
    assert detail["acknowledged_by"] == seed["resp"]["id"]


def test_sos_captures_last_known_location(login, floor):
    alice = login("alice")
    ph = alice.post("/api/v1/phones", json={"name": "P"}).json()
    alice.post(f"/api/v1/phones/{ph['id']}/sharing", json={"enabled": True, "permission_state": "granted"})
    alice.post(f"/api/v1/phones/{ph['id']}/location", json={"kind": "floor", "floor_id": floor["floor"]["id"],
                                                           "x": 0.45, "y": 0.35, "accuracy_m": 4})
    inc = alice.post("/api/v1/incidents/sos", json={"idempotency_key": "sos-key-0002", "device_id": ph["id"]}).json()
    inc = inc["incident"]
    assert inc["x"] == 0.45 and inc["room_name"] == "Classroom 05" and inc["location_note"] == "Current location"


def test_users_see_only_own_incidents(login):
    alice, bob, resp = login("alice"), login("bob"), login("resp")
    inc = alice.post("/api/v1/incidents/sos", json={"idempotency_key": "sos-key-0003"}).json()["incident"]
    assert bob.get(f"/api/v1/incidents/{inc['id']}").status_code == 404
    assert bob.get("/api/v1/incidents").json() == []
    assert len(resp.get("/api/v1/incidents").json()) == 1


def test_badge_sos_idempotent_and_confirmation(login, make_client, seed):
    admin = login("admin")
    dev = enroll(admin, "badge", "Alice badge", seed["alice"]["id"])
    hdr = {"Authorization": f"Bearer {dev['device_token']}"}
    body = {"schema_version": 1, "event_id": "boot3-seq1", "uptime_ms": 12345}
    c = make_client()
    r1 = c.post("/api/v1/device/sos", json=body, headers=hdr)
    assert r1.status_code == 201 and r1.json()["confirmed"] is False
    r2 = c.post("/api/v1/device/sos", json=body, headers=hdr)
    assert r2.status_code == 200 and r2.json()["incident_id"] == r1.json()["incident_id"]
    hb = heartbeat(c, dev["device_token"], pending_sos=["boot3-seq1"]).json()
    assert hb["sos"][0]["state"] == "NEW" and hb["sos"][0]["confirmed"] is False
    login("resp").post(f"/api/v1/incidents/{r1.json()['incident_id']}/transition", json={"to_state": "ACKNOWLEDGED"})
    hb = heartbeat(c, dev["device_token"], pending_sos=["boot3-seq1"]).json()
    assert hb["sos"][0]["confirmed"] is True
    # The wearer sees the badge alert as their own incident.
    assert len(login("alice").get("/api/v1/incidents").json()) == 1


def test_badge_can_press_sos_again_and_again(login, make_client, seed):
    admin = login("admin")
    dev = enroll(admin, "badge", "Alice badge", seed["alice"]["id"])
    hdr = {"Authorization": f"Bearer {dev['device_token']}"}
    c = make_client()
    press = lambda eid: c.post("/api/v1/device/sos", json={"schema_version": 1, "event_id": eid}, headers=hdr).json()  # noqa: E731
    first = press("b1-s1")
    assert first["repeated"] is False and first["sos_lockout_s"] == 5 and first["sos_hold_ms"] == 1500
    # While it is open, every new press re-alerts the same incident (and asks for a fresh photo).
    for n, eid in enumerate(("b1-s2", "b1-s3"), start=1):
        r = press(eid)
        assert r["incident_id"] == first["incident_id"] and r["repeated"] is True and r["photo_wanted"] is True
        assert admin.get(f"/api/v1/incidents/{first['incident_id']}").json()["repeat_count"] == n
    assert press("b1-s3")["duplicate"] is True  # a retry of the same press is not counted twice
    assert admin.get(f"/api/v1/incidents/{first['incident_id']}").json()["repeat_count"] == 2
    # The badge learns acknowledgement for repeat presses too.
    login("resp").post(f"/api/v1/incidents/{first['incident_id']}/transition", json={"to_state": "ACKNOWLEDGED"})
    hb = heartbeat(c, dev["device_token"], pending_sos=["b1-s3"]).json()
    assert hb["sos"][0]["confirmed"] is True and hb["sos_lockout_s"] == 5
    # Once resolved, the next press is a new incident.
    for st in ("RESPONDING", "RESOLVED"):
        login("resp").post(f"/api/v1/incidents/{first['incident_id']}/transition", json={"to_state": st})
    assert press("b1-s4")["incident_id"] != first["incident_id"]
    # Setting: always open a new incident.
    admin.patch("/api/v1/settings", json={"values": {"sos.repeat_mode": "new_incident", "badge.sos_lockout_s": 2}})
    r = press("b1-s5")
    assert r["repeated"] is False and r["sos_lockout_s"] == 2
    assert admin.patch("/api/v1/settings", json={"values": {"sos.repeat_mode": "whatever"}}).status_code == 422


def test_sos_alert_event_carries_display_time(login):
    login("admin").patch("/api/v1/settings", json={"values": {"sos.alert_display_s": 7}})
    alice, resp = login("alice"), login("resp")  # resp last: the live hub follows the newest test client's loop
    with resp.websocket_connect("/api/v1/ws") as ws:
        ws.receive_text()
        alice.post("/api/v1/incidents/sos", json={"idempotency_key": "sos-key-0101"})
        alice.post("/api/v1/incidents/sos", json={"idempotency_key": "sos-key-0102"})  # pressed again
        alerts = []
        while len(alerts) < 2:
            ev = json.loads(ws.receive_text())
            if ev["type"] == "sos_alert":
                alerts.append(ev["data"])
    assert [a["repeat"] for a in alerts] == [0, 1] and alerts[0]["display_s"] == 7
    assert alerts[0]["incident"]["id"] == alerts[1]["incident"]["id"]


def test_only_badges_raise_device_sos(login, make_client):
    admin = login("admin")
    cam = enroll(admin, "camera_node", "Cam")
    r = make_client().post("/api/v1/device/sos", json={"schema_version": 1, "event_id": "x1"},
                           headers={"Authorization": f"Bearer {cam['device_token']}"})
    assert r.status_code == 403


def test_test_incidents_isolated(login):
    resp = login("resp")
    resp.post("/api/v1/incidents/test", json={"message": "drill"})
    ov = resp.get("/api/v1/overview").json()
    assert ov["incidents"]["active"] == 0 and ov["incidents"]["active_tests"] == 1
    assert resp.get("/api/v1/incidents", params={"include_tests": False}).json() == []


def test_assignment_requires_responder(login, seed):
    alice, resp = login("alice"), login("resp")
    inc = alice.post("/api/v1/incidents/sos", json={"idempotency_key": "sos-key-0004"}).json()["incident"]
    assert resp.post(f"/api/v1/incidents/{inc['id']}/assign", json={"user_id": seed["bob"]["id"]}).status_code == 422
    r = resp.post(f"/api/v1/incidents/{inc['id']}/assign", json={"user_id": seed["resp"]["id"]})
    assert r.status_code == 200 and r.json()["assigned_name"] == "Resp"


def test_incidents_survive_backend_restart(cfg, login):
    alice = login("alice")
    inc = alice.post("/api/v1/incidents/sos", json={"idempotency_key": "sos-key-0005"}).json()["incident"]
    app2 = create_app(cfg, start_background=False)  # new process state, same database
    with TestClient(app2) as c:
        c.post("/api/v1/auth/login", json={"username": "resp", "password": "correct-horse-42"})
        active = c.get("/api/v1/incidents", params={"active": True}).json()
        assert [i["id"] for i in active] == [inc["id"]]


def test_sos_pushed_to_responders_live(login):
    alice, resp = login("alice"), login("resp")
    with resp.websocket_connect("/api/v1/ws") as ws:
        ws.receive_text()
        alice.post("/api/v1/incidents/sos", json={"idempotency_key": "sos-key-0006"})
        for _ in range(5):
            ev = json.loads(ws.receive_text())
            if ev["type"] == "incident":
                assert ev["data"]["state"] == "NEW"
                break
        else:
            raise AssertionError("incident event not received")
