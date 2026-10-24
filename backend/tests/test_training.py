"""Map trainer: "I'm here" fingerprint capture and k-NN positioning."""
from datetime import timedelta

from app import fingerprint as fp
from app.db import Tx, iso, utcnow

from .conftest import enroll

AP = ["00:11:22:33:44:01", "00:11:22:33:44:02", "00:11:22:33:44:03", "00:11:22:33:44:04"]
SPOT_A = {AP[0]: -45, AP[1]: -70, AP[2]: -80, AP[3]: -88}   # near AP 1
SPOT_B = {AP[0]: -85, AP[1]: -72, AP[2]: -50, AP[3]: -60}   # near AP 3/4


def _scan(vec, jitter=0):
    return [{"id": k, "rssi": v + jitter} for k, v in vec.items()]


def _post(client, token, vec, jitter=0, seq=None):
    body = {"kind": "ble", "ble": [], "wifi": _scan(vec, jitter)}
    if seq is not None:
        body["sequence"] = seq
    r = client.post("/api/v1/device/location", json=body, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.json()


def _train(admin, badge_client, token, db, fid, x, y, vec, samples=3):
    s = admin.post("/api/v1/training/sessions", json={"device_id": token["device"]["id"], "floor_id": fid,
                                                      "x": x, "y": y, "samples": samples})
    assert s.status_code == 201, s.text
    s = s.json()
    with Tx(db):  # skip the settle delay (the person has been standing still)
        db.execute("UPDATE training_sessions SET started_at = ? WHERE id = ?", (iso(utcnow() - timedelta(seconds=5)), s["id"]))
    last = None
    for i in range(samples):
        last = _post(badge_client, token["device_token"], vec, jitter=(-1) ** i)
        assert last["source"] == "map_trainer" and "confirmed_by_person" in last["flags"]
        assert abs(last["x"] - x) < 1e-9 and last["scan_interval_s"] == (10 if i == samples - 1 else 3)
    assert last["training"] == {"samples": samples, "target": samples, "done": True}
    return s


def test_train_two_spots_then_locate(login, make_client, floor, db):
    admin = login("admin")
    fid = floor["floor"]["id"]
    dev = enroll(admin, "badge", "Trainer badge")
    bc = make_client()
    a = _train(admin, bc, dev, db, fid, 0.45, 0.35, SPOT_A)      # inside Classroom 05
    _train(admin, bc, dev, db, fid, 0.80, 0.70, SPOT_B)

    summary = admin.get(f"/api/v1/floors/{fid}/training").json()
    assert len(summary["spots"]) == 2 and summary["signals"] == 4 and summary["rooms_trained"] == 1
    assert any(s["id"] == a["id"] and s["room_name"] == "Classroom 05" for s in summary["spots"])

    # A different badge, standing near spot A, is located there (not trained, so it is matched).
    other = enroll(admin, "badge", "Wearer badge")
    r = _post(make_client(), other["device_token"], SPOT_A, jitter=2)
    assert r["source"] == "wifi_fingerprint" and "estimate" in r["flags"]
    assert abs(r["x"] - 0.45) < 0.05 and abs(r["y"] - 0.35) < 0.05
    assert r["room_id"] == floor["room"]["id"] and r["confidence"] >= 0.5
    r = _post(make_client(), other["device_token"], SPOT_B, jitter=-3)
    assert abs(r["x"] - 0.80) < 0.05 and abs(r["y"] - 0.70) < 0.05

    # Deleting a spot forgets it.
    assert admin.delete(f"/api/v1/training/sessions/{a['id']}").status_code == 200
    assert len(admin.get(f"/api/v1/floors/{fid}/training").json()["spots"]) == 1


def test_unrelated_scan_falls_back_to_anchor_logic(login, make_client, floor, db):
    admin = login("admin")
    fid = floor["floor"]["id"]
    dev = enroll(admin, "badge", "B")
    _train(admin, make_client(), dev, db, fid, 0.45, 0.35, SPOT_A)
    r = _post(make_client(), dev["device_token"], {"00:99:99:99:99:99": -60})
    assert r["kind"] == "unavailable" and r["x"] is None


def test_settling_and_empty_scans_are_not_stored(login, make_client, floor, db):
    admin = login("admin")
    dev = enroll(admin, "badge", "B")
    s = admin.post("/api/v1/training/sessions", json={"device_id": dev["device"]["id"], "floor_id": floor["floor"]["id"],
                                                      "x": 0.5, "y": 0.5}).json()
    r = _post(make_client(), dev["device_token"], SPOT_A)  # arrives immediately: scan began while walking
    assert r["training"]["samples"] == 0
    assert admin.get(f"/api/v1/training/sessions/{s['id']}").json()["samples"] == 0
    assert admin.post(f"/api/v1/training/sessions/{s['id']}/stop").json() == {"ok": True, "kept": False}


def test_randomised_macs_are_ignored():
    v = fp.vector([{"id": "02:11:22:33:44:55", "rssi": -50}, {"id": "DA:A1:19:00:00:01", "rssi": -40},
                   {"id": "00:11:22:33:44:55", "rssi": -60}, {"id": "00:11:22:33:44:55", "rssi": -55}])
    assert v == {"00:11:22:33:44:55": -55.0}


def test_only_badges_and_trainers(login, floor):
    admin = login("admin")
    fid = floor["floor"]["id"]
    cam = enroll(admin, "camera_node", "Cam")
    r = admin.post("/api/v1/training/sessions", json={"device_id": cam["device"]["id"], "floor_id": fid, "x": 0.5, "y": 0.5})
    assert r.status_code == 422
    badge = enroll(admin, "badge", "B")
    alice = login("alice")
    assert alice.post("/api/v1/training/sessions", json={"device_id": badge["device"]["id"], "floor_id": fid,
                                                         "x": 0.5, "y": 0.5}).status_code == 403
    assert login("resp").post("/api/v1/training/sessions", json={"device_id": badge["device"]["id"], "floor_id": fid,
                                                                 "x": 0.5, "y": 0.5}).status_code == 201


def test_phone_gps_uses_the_only_calibrated_floor(login, floor):
    admin = login("admin")
    fid = floor["floor"]["id"]
    for lat, lon, x, y in ((28.601, 77.200, 0.0, 0.0), (28.601, 77.201, 1.0, 0.0), (28.600, 77.200, 0.0, 1.0)):
        admin.post(f"/api/v1/floors/{fid}/georef-points", json={"lat": lat, "lon": lon, "x": x, "y": y})
    alice = login("alice")
    ph = alice.post("/api/v1/phones", json={"name": "Phone"}).json()
    alice.post(f"/api/v1/phones/{ph['id']}/sharing", json={"enabled": True, "permission_state": "granted"})
    r = alice.post(f"/api/v1/phones/{ph['id']}/location", json={"kind": "geo", "lat": 28.6005, "lon": 77.2005,
                                                               "accuracy_m": 8}).json()
    assert r["kind"] == "floor" and r["floor_id"] == fid and "only_calibrated_floor" in r["flags"]
