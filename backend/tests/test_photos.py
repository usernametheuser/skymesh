import cv2
import numpy as np

from app.db import Tx

from .conftest import enroll, heartbeat


def jpeg(w=640, h=480) -> bytes:
    img = np.full((h, w, 3), 120, np.uint8)
    cv2.rectangle(img, (100, 100), (300, 400), (0, 200, 255), -1)
    return cv2.imencode(".jpg", img)[1].tobytes()


def post_photo(client, token, body, **params):
    return client.post("/api/v1/device/photo", params=params, content=body,
                       headers={"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"})


def test_sos_photo_is_linked_to_the_incident(login, make_client, seed):
    admin = login("admin")
    badge = enroll(admin, "badge", "Badge A", seed["alice"]["id"])
    tok = badge["device_token"]
    c = make_client()
    sos = c.post("/api/v1/device/sos", json={"schema_version": 1, "event_id": "b1-s1"},
                 headers={"Authorization": f"Bearer {tok}"}).json()
    assert sos["photo_wanted"] is True  # default: a scene photo follows every new badge SOS
    r = post_photo(c, tok, jpeg(), reason="sos", event_id="b1-s1")
    assert r.status_code == 201, r.text
    assert r.json()["incident_id"] == sos["incident_id"] and r.json()["width"] == 640
    resp = login("resp")
    photos = resp.get(f"/api/v1/incidents/{sos['incident_id']}/photos").json()
    assert len(photos) == 1
    img = resp.get(photos[0]["url"])
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    assert "photo.view" in [a["action"] for a in admin.get("/api/v1/audit").json()]
    # The wearer sees their own badge's photos; other users do not.
    assert login("alice").get(f"/api/v1/devices/{badge['device']['id']}/photos").json()[0]["id"] == photos[0]["id"]
    assert login("bob").get(photos[0]["url"]).status_code == 404


def test_photo_upload_rejects_non_jpeg_and_users(login, make_client):
    admin = login("admin")
    tok = enroll(admin, "badge", "B")["device_token"]
    assert post_photo(make_client(), tok, b"not an image").status_code == 422
    assert post_photo(make_client(), tok, b"\xff\xd8\xff" + b"0" * 100).status_code == 422
    assert login("alice").post("/api/v1/device/photo", content=jpeg(), headers={"Content-Type": "image/jpeg"}).status_code == 403


def test_remote_photo_request_on_by_default_then_delivered_once(login, make_client, db, seed):
    admin = login("admin")
    badge = enroll(admin, "badge", "B")
    alices = enroll(admin, "badge", "Alice's", seed["alice"]["id"])
    resp = login("resp")
    alice = login("alice")
    # Users may ask only their own badge; responders any badge.
    assert alice.post(f"/api/v1/devices/{badge['device']['id']}/photo-request").status_code == 404
    assert alice.post(f"/api/v1/devices/{alices['device']['id']}/photo-request").status_code == 202
    # An administrator can still switch the feature off.
    admin.patch("/api/v1/settings", json={"values": {"badge.remote_photo_enabled": False}})
    assert resp.post(f"/api/v1/devices/{badge['device']['id']}/photo-request").status_code == 403
    admin.patch("/api/v1/settings", json={"values": {"badge.remote_photo_enabled": True}})
    cmd = resp.post(f"/api/v1/devices/{badge['device']['id']}/photo-request").json()
    c = make_client()
    hb = heartbeat(c, badge["device_token"]).json()
    assert hb["commands"] == [{"id": cmd["id"], "cmd": "photo"}]
    assert heartbeat(c, badge["device_token"]).json()["commands"] == []  # handed out once
    assert post_photo(c, badge["device_token"], jpeg(), reason="request", command_id=cmd["id"]).status_code == 201
    assert db.execute("SELECT done_at FROM device_commands WHERE id = ?", (cmd["id"],)).fetchone()["done_at"]


def test_retention_deletes_old_photos_and_files(login, make_client, cfg, db):
    from app.retention import run_retention
    admin = login("admin")
    tok = enroll(admin, "badge", "B")["device_token"]
    p = post_photo(make_client(), tok, jpeg(), reason="test").json()
    path = cfg.data_dir / "photos" / f"{p['id']}.jpg"
    assert path.exists()
    with Tx(db):
        db.execute("UPDATE device_photos SET created_at = '2020-01-01T00:00:00Z'")
    assert run_retention(db, cfg.data_dir / "photos")["device_photos"] == 1
    assert not path.exists()


def test_camera_picture_controls_are_validated(login):
    admin = login("admin")
    cam = enroll(admin, "camera_node", "Cam")["device"]
    # Camera has never reported in, so the command cannot be sent — but the value is validated first.
    assert admin.post(f"/api/v1/cameras/{cam['id']}/control", json={"cmd": "flash", "val": "40"}).status_code == 503
    assert admin.post(f"/api/v1/cameras/{cam['id']}/control", json={"cmd": "brightness", "val": "-1"}).status_code == 503
    assert admin.post(f"/api/v1/cameras/{cam['id']}/control", json={"cmd": "format_disk", "val": ""}).status_code == 422
    assert login("resp").post(f"/api/v1/cameras/{cam['id']}/control", json={"cmd": "flash", "val": "40"}).status_code == 403
