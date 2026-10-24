from datetime import timedelta

from app import positioning as pos
from app.db import Tx, iso, utcnow

from .conftest import enroll, heartbeat


def _phone(client, name="Pixel"):
    r = client.post("/api/v1/phones", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- devices

def test_enroll_heartbeat_and_status(login, make_client):
    admin = login("admin")
    dev = enroll(admin, "badge", "Badge 1")
    assert dev["device_token"].startswith("smd_")
    anon = make_client()
    r = heartbeat(anon, dev["device_token"], rssi=-61, battery_mv=3900, ip="1.2.3.4")
    assert r.status_code == 200, r.text
    assert r.json()["device_id"] == dev["device"]["id"]
    d = [x for x in admin.get("/api/v1/devices").json() if x["id"] == dev["device"]["id"]][0]
    assert d["online"] is True and d["rssi"] == -61 and d["battery_mv"] == 3900
    assert d["last_ip"] == "testclient"  # the observed source, not the self-reported address


def test_heartbeat_exactly_as_badge_firmware_sends_it(login, make_client):
    """Regression: firmware 0.2.0 sends reset_reason as a number and pending_sos as a list (found on real hardware)."""
    admin = login("admin")
    dev = enroll(admin, "badge", "Real badge")
    body = {"schema_version": 1, "firmware_version": "skymesh-badge-0.2.0", "hardware_model": "ESP32-S3-CAM",
            "uptime_s": 12, "sequence": 1, "ip": "192.168.1.8", "rssi": -56, "bssid": "20:0C:86:FB:2B:A1",
            "free_heap": 210000, "reset_reason": 1,
            "capabilities": {"sos_button": True, "ble_scan": True, "battery_sense": False, "camera": False,
                             "led": False, "clock_synced": False}, "pending_sos": []}
    r = make_client().post("/api/v1/device/heartbeat", json=body, headers={"Authorization": f"Bearer {dev['device_token']}"})
    assert r.status_code == 200, r.text
    assert [d for d in admin.get("/api/v1/devices").json() if d["id"] == dev["device"]["id"]][0]["online"] is True


def test_offline_after_timeout(login, make_client, db):
    admin = login("admin")
    dev = enroll(admin, "badge", "Badge 1")
    heartbeat(make_client(), dev["device_token"])
    with Tx(db):
        db.execute("UPDATE devices SET last_seen_at = ?", (iso(utcnow() - timedelta(seconds=600)),))
    d = [x for x in admin.get("/api/v1/devices").json() if x["id"] == dev["device"]["id"]][0]
    assert d["online"] is False


def test_never_seen_device_is_offline_not_online(login):
    admin = login("admin")
    dev = enroll(admin, "camera_node", "Cam")
    assert "camera_key" in dev
    d = [x for x in admin.get("/api/v1/devices").json() if x["id"] == dev["device"]["id"]][0]
    assert d["online"] is False


def test_revoked_device_rejected(login, make_client):
    admin = login("admin")
    dev = enroll(admin, "badge", "Badge 1")
    assert admin.post(f"/api/v1/devices/{dev['device']['id']}/revoke").status_code == 200
    assert heartbeat(make_client(), dev["device_token"]).status_code == 401


def test_device_identity_comes_from_token(login, make_client):
    """A badge cannot report on behalf of another badge: payload device ids are ignored."""
    admin = login("admin")
    a = enroll(admin, "badge", "A")
    b = enroll(admin, "badge", "B")
    r = heartbeat(make_client(), a["device_token"], device_id=b["device"]["id"])
    assert r.status_code == 200 and r.json()["device_id"] == a["device"]["id"]
    devs = {d["id"]: d for d in admin.get("/api/v1/devices").json()}
    assert devs[b["device"]["id"]]["last_seen_at"] is None


def test_forged_token_rejected(make_client, seed):
    assert heartbeat(make_client(), "smd_" + "A" * 43).status_code == 401


def test_rotate_token(login, make_client):
    admin = login("admin")
    dev = enroll(admin, "badge", "A")
    new = admin.post(f"/api/v1/devices/{dev['device']['id']}/rotate-token").json()["device_token"]
    assert heartbeat(make_client(), dev["device_token"]).status_code == 401
    assert heartbeat(make_client(), new).status_code == 200


def test_users_only_see_own_devices(login, seed):
    admin = login("admin")
    enroll(admin, "badge", "Alice badge", seed["alice"]["id"])
    enroll(admin, "badge", "Bob badge", seed["bob"]["id"])
    alice = login("alice")
    names = [d["name"] for d in alice.get("/api/v1/devices").json()]
    assert names == ["Alice badge"]


# ---------------------------------------------------------------- phone location

def test_sharing_off_by_default_and_rejects_updates(login, floor):
    alice = login("alice")
    ph = _phone(alice)
    assert ph["sharing_enabled"] is False
    r = alice.post(f"/api/v1/phones/{ph['id']}/location", json={"kind": "geo", "lat": 28.6, "lon": 77.2})
    assert r.status_code == 409


def test_live_location_flow_and_states(login, floor, db):
    alice = login("alice")
    ph = _phone(alice)
    alice.post(f"/api/v1/phones/{ph['id']}/sharing", json={"enabled": True, "permission_state": "granted"})
    fid = floor["floor"]["id"]
    r = alice.post(f"/api/v1/phones/{ph['id']}/location",
                   json={"kind": "floor", "floor_id": fid, "x": 0.45, "y": 0.35, "accuracy_m": 3, "source": "manual_test"})
    assert r.status_code == 200, r.text
    assert r.json()["room_id"] == floor["room"]["id"]
    item = alice.get("/api/v1/locations/current").json()[0]
    assert item["state"] == "LIVE" and item["is_live"] and item["room_name"] == "Classroom 05"
    # Age the observation: it must never stay LIVE.
    for age, state in ((60, "RECENT"), (300, "STALE"), (5000, "LAST_KNOWN")):
        with Tx(db):
            db.execute("UPDATE current_locations SET observed_at = ?", (iso(utcnow() - timedelta(seconds=age)),))
        item = alice.get("/api/v1/locations/current").json()[0]
        assert item["state"] == state and item["is_live"] is False
        assert item["x"] == 0.45  # still shown, but labelled historical


def test_low_accuracy_flagged(login, floor):
    alice = login("alice")
    ph = _phone(alice)
    alice.post(f"/api/v1/phones/{ph['id']}/sharing", json={"enabled": True, "permission_state": "granted"})
    alice.post(f"/api/v1/phones/{ph['id']}/location", json={"kind": "floor", "floor_id": floor["floor"]["id"],
                                                           "x": 0.1, "y": 0.1, "accuracy_m": 80})
    assert alice.get("/api/v1/locations/current").json()[0]["state"] == "LOW_CONFIDENCE"


def test_permission_denied(login, floor):
    alice = login("alice")
    ph = _phone(alice)
    alice.post(f"/api/v1/phones/{ph['id']}/sharing", json={"enabled": True, "permission_state": "granted"})
    r = alice.post(f"/api/v1/phones/{ph['id']}/location", json={"kind": "denied", "reason": "PERMISSION_DENIED"})
    assert r.status_code == 200
    item = alice.get("/api/v1/locations/current").json()[0]
    assert item["state"] == "PERMISSION_DENIED" and item.get("x") is None


def test_stop_sharing_hides_coordinates_from_responders(login, floor):
    alice = login("alice")
    ph = _phone(alice)
    alice.post(f"/api/v1/phones/{ph['id']}/sharing", json={"enabled": True, "permission_state": "granted"})
    alice.post(f"/api/v1/phones/{ph['id']}/location", json={"kind": "floor", "floor_id": floor["floor"]["id"],
                                                           "x": 0.2, "y": 0.2})
    resp = login("resp")
    assert resp.get("/api/v1/locations/current").json()[0]["x"] == 0.2
    alice.post(f"/api/v1/phones/{ph['id']}/sharing", json={"enabled": False})
    item = resp.get("/api/v1/locations/current").json()[0]
    assert item["state"] == "SHARING_DISABLED" and "x" not in item
    assert resp.get(f"/api/v1/locations/history/{ph['id']}").status_code == 403


def test_ordinary_users_cannot_see_others(login, floor):
    alice = login("alice")
    ph = _phone(alice)
    alice.post(f"/api/v1/phones/{ph['id']}/sharing", json={"enabled": True, "permission_state": "granted"})
    alice.post(f"/api/v1/phones/{ph['id']}/location", json={"kind": "floor", "floor_id": floor["floor"]["id"],
                                                           "x": 0.2, "y": 0.2})
    bob = login("bob")
    assert bob.get("/api/v1/locations/current").json() == []
    assert bob.get(f"/api/v1/locations/history/{ph['id']}").status_code == 404
    assert bob.post(f"/api/v1/phones/{ph['id']}/sharing", json={"enabled": False}).status_code == 404


def test_privileged_location_access_is_audited(login, floor):
    alice = login("alice")
    ph = _phone(alice)
    alice.post(f"/api/v1/phones/{ph['id']}/sharing", json={"enabled": True, "permission_state": "granted"})
    alice.post(f"/api/v1/phones/{ph['id']}/location", json={"kind": "floor", "floor_id": floor["floor"]["id"],
                                                           "x": 0.2, "y": 0.2})
    login("resp").get("/api/v1/locations/current")
    actions = [a["action"] for a in login("admin").get("/api/v1/audit").json()]
    assert "location.view_all" in actions


def test_gps_not_projected_without_georeference(login, floor):
    alice = login("alice")
    ph = _phone(alice)
    alice.post(f"/api/v1/phones/{ph['id']}/sharing", json={"enabled": True, "permission_state": "granted"})
    r = alice.post(f"/api/v1/phones/{ph['id']}/location", json={"kind": "geo", "lat": 28.6, "lon": 77.2,
                                                               "accuracy_m": 12, "floor_id": floor["floor"]["id"]}).json()
    assert r["kind"] == "geo" and r["x"] is None and "floor_not_georeferenced" in r["flags"]


def test_gps_projected_with_georeference(login, floor):
    admin = login("admin")
    fid = floor["floor"]["id"]
    # Synthetic control points: a north-up plan 0.001 deg wide.
    for lat, lon, x, y in ((28.601, 77.200, 0.0, 0.0), (28.601, 77.201, 1.0, 0.0), (28.600, 77.200, 0.0, 1.0)):
        admin.post(f"/api/v1/floors/{fid}/georef-points", json={"lat": lat, "lon": lon, "x": x, "y": y})
    alice = login("alice")
    ph = _phone(alice)
    alice.post(f"/api/v1/phones/{ph['id']}/sharing", json={"enabled": True, "permission_state": "granted"})
    r = alice.post(f"/api/v1/phones/{ph['id']}/location", json={"kind": "geo", "lat": 28.6005, "lon": 77.2005,
                                                               "accuracy_m": 8, "floor_id": fid}).json()
    assert r["kind"] == "floor" and abs(r["x"] - 0.5) < 1e-6 and abs(r["y"] - 0.5) < 1e-6
    out = alice.post(f"/api/v1/phones/{ph['id']}/location", json={"kind": "geo", "lat": 28.7, "lon": 77.3,
                                                                 "floor_id": fid}).json()
    assert "outside_floor_plan" in out["flags"] and out["x"] is None


def test_invalid_coordinates_rejected(login, floor):
    alice = login("alice")
    ph = _phone(alice)
    alice.post(f"/api/v1/phones/{ph['id']}/sharing", json={"enabled": True, "permission_state": "granted"})
    for body in ({"kind": "geo", "lat": 123, "lon": 0}, {"kind": "floor", "floor_id": floor["floor"]["id"], "x": 1.5,
                                                         "y": 0.2}, {"kind": "floor", "x": 0.2, "y": 0.2},
                 {"kind": "floor", "floor_id": "flr_nope", "x": 0.2, "y": 0.2}):
        assert alice.post(f"/api/v1/phones/{ph['id']}/location", json=body).status_code == 422, body


def test_future_timestamp_replaced_and_duplicate_sequence(login, floor):
    alice = login("alice")
    ph = _phone(alice)
    alice.post(f"/api/v1/phones/{ph['id']}/sharing", json={"enabled": True, "permission_state": "granted"})
    future = iso(utcnow() + timedelta(hours=2))
    body = {"kind": "floor", "floor_id": floor["floor"]["id"], "x": 0.2, "y": 0.2, "timestamp": future, "sequence": 7}
    r1 = alice.post(f"/api/v1/phones/{ph['id']}/location", json=body).json()
    assert "client_clock_ahead" in r1["flags"]
    r2 = alice.post(f"/api/v1/phones/{ph['id']}/location", json=body).json()
    assert r2["duplicate"] is True and r2["id"] == r1["id"]


def test_delayed_update_does_not_replace_current(login, floor):
    alice = login("alice")
    ph = _phone(alice)
    alice.post(f"/api/v1/phones/{ph['id']}/sharing", json={"enabled": True, "permission_state": "granted"})
    fid = floor["floor"]["id"]
    alice.post(f"/api/v1/phones/{ph['id']}/location", json={"kind": "floor", "floor_id": fid, "x": 0.6, "y": 0.6})
    old = iso(utcnow() - timedelta(seconds=60))
    r = alice.post(f"/api/v1/phones/{ph['id']}/location",
                   json={"kind": "floor", "floor_id": fid, "x": 0.1, "y": 0.1, "timestamp": old}).json()
    assert "out_of_order_not_current" in r["flags"]
    assert alice.get("/api/v1/locations/current").json()[0]["x"] == 0.6


def test_manual_room_is_room_level(login, floor):
    alice = login("alice")
    ph = _phone(alice)
    alice.post(f"/api/v1/phones/{ph['id']}/sharing", json={"enabled": True, "permission_state": "granted"})
    r = alice.post(f"/api/v1/phones/{ph['id']}/location", json={"kind": "room", "room_id": floor["room"]["id"]}).json()
    assert r["kind"] == "room" and "room_level_only" in r["flags"]


# ---------------------------------------------------------------- badge BLE positioning

def test_badge_ble_no_anchors_is_unavailable(login, make_client, floor):
    admin = login("admin")
    dev = enroll(admin, "badge", "B")
    r = make_client().post("/api/v1/device/location", json={"kind": "ble", "ble": [{"id": "aa:bb", "rssi": -60}]},
                           headers={"Authorization": f"Bearer {dev['device_token']}"}).json()
    assert r["kind"] == "unavailable" and r["x"] is None
    item = [i for i in admin.get("/api/v1/locations/current").json() if i["device_id"] == dev["device"]["id"]][0]
    assert item["state"] == "POSITION_UNAVAILABLE"


def test_badge_ble_single_anchor_is_room_level(login, make_client, floor):
    admin = login("admin")
    fid = floor["floor"]["id"]
    admin.post(f"/api/v1/floors/{fid}/anchors", json={"kind": "ble_anchor", "name": "A1", "x": 0.45, "y": 0.35,
                                                      "identifier": "AA:BB:CC:00:00:01", "rssi_at_1m": -59})
    dev = enroll(admin, "badge", "B")
    r = make_client().post("/api/v1/device/location", json={"kind": "ble", "ble": [{"id": "aa:bb:cc:00:00:01",
                                                                                  "rssi": -70}]},
                           headers={"Authorization": f"Bearer {dev['device_token']}"}).json()
    assert r["kind"] == "room" and r["room_id"] == floor["room"]["id"]
    assert "nearest_node_only" in r["flags"]


def test_badge_ble_three_anchors_is_estimate(login, make_client, floor):
    admin = login("admin")
    fid = floor["floor"]["id"]
    for i, (x, y) in enumerate(((0.1, 0.1), (0.3, 0.1), (0.2, 0.3))):
        admin.post(f"/api/v1/floors/{fid}/anchors", json={"kind": "ble_anchor", "name": f"A{i}", "x": x, "y": y,
                                                          "identifier": f"aa:00:00:00:00:0{i}"})
    dev = enroll(admin, "badge", "B")
    r = make_client().post("/api/v1/device/location", json={"kind": "ble", "ble": [
        {"id": "aa:00:00:00:00:00", "rssi": -60}, {"id": "aa:00:00:00:00:01", "rssi": -60},
        {"id": "aa:00:00:00:00:02", "rssi": -60}]}, headers={"Authorization": f"Bearer {dev['device_token']}"}).json()
    assert r["kind"] == "floor" and "estimate" in r["flags"]
    assert abs(r["x"] - 0.2) < 1e-6 and abs(r["y"] - 0.1667) < 1e-3


def test_offline_badge_location_not_live(login, make_client, floor, db):
    admin = login("admin")
    dev = enroll(admin, "badge", "B")
    heartbeat(make_client(), dev["device_token"])
    make_client().post("/api/v1/device/location", json={"kind": "floor", "floor_id": floor["floor"]["id"], "x": 0.3,
                                                        "y": 0.3}, headers={"Authorization": f"Bearer {dev['device_token']}"})
    with Tx(db):
        db.execute("UPDATE devices SET last_seen_at = ?", (iso(utcnow() - timedelta(seconds=600)),))
    item = [i for i in admin.get("/api/v1/locations/current").json() if i["device_id"] == dev["device"]["id"]][0]
    assert item["state"] == "OFFLINE" and not item["is_live"]


def test_geotransform_rejects_collinear():
    pts = [{"lat": 1, "lon": 1, "x": 0, "y": 0}, {"lat": 2, "lon": 2, "x": 0.5, "y": 0.5},
           {"lat": 3, "lon": 3, "x": 1, "y": 1}]
    assert pos.fit_geotransform(pts) is None
