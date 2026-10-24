import numpy as np

from app.vision import FallDetector

W, H = 320, 240
FPS = 5


def scene():
    rng = np.random.default_rng(0)
    base = np.full((H, W, 3), 90, np.uint8)
    base += rng.integers(0, 6, base.shape, dtype=np.uint8)  # sensor noise
    return base


def frame_with(box):
    f = scene()
    if box:
        x, y, w, h = box
        f[y:y + h, x:x + w] = 220
    return f


def run(boxes, idle_s=0):
    d = FallDetector(still_s=8, idle_s=idle_s)
    alerts = []
    for i, box in enumerate(boxes):
        a = d.update(frame_with(box), i / FPS)
        if a:
            alerts.append((i / FPS, a))
    return alerts


def test_fall_then_lying_still_raises_one_alert():
    seq = [None] * 20                                                   # empty room (background learning)
    seq += [(40 + 6 * i, 60, 40, 120) for i in range(15)]               # person walks in, upright
    seq += [(130, 150, 120, 40)] * (FPS * 15)                           # falls: lying on the floor, still
    alerts = run(seq)
    assert len(alerts) == 1
    t, a = alerts[0]
    fall_t = (20 + 15) / FPS
    assert 7.5 <= t - fall_t <= 9.5 and a["confidence"] == 0.5


def test_person_who_stops_moving_raises_not_moving_alert():
    seq = [None] * 20
    seq += [(40 + 5 * i, 60, 40, 120) for i in range(20)]   # walks in
    seq += [(140, 60, 40, 120)] * (FPS * 40)                 # then does not move at all (e.g. slumped against a wall)
    alerts = run(seq, idle_s=30)
    assert [a["kind"] for _, a in alerts] == ["not_moving"]
    t = alerts[0][0]
    stopped = (20 + 20) / FPS
    assert 29 <= t - stopped <= 32


def test_object_that_never_moved_does_not_raise_not_moving():
    seq = [None] * 20 + [(140, 60, 40, 120)] * (FPS * 60)  # appears and stays: never seen moving
    assert run(seq, idle_s=30) == []


def test_fall_is_reported_once_not_twice():
    seq = [None] * 20 + [(40 + 6 * i, 60, 40, 120) for i in range(15)] + [(130, 150, 120, 40)] * (FPS * 60)
    alerts = run(seq, idle_s=20)
    assert [a["kind"] for _, a in alerts] == ["possible_fall"]


def test_walking_person_never_alerts():
    seq = [None] * 20 + [(10 + (3 * i) % 250, 60, 40, 120) for i in range(FPS * 30)]
    assert run(seq) == []


def test_horizontal_object_without_prior_upright_person_does_not_alert():
    seq = [None] * 20 + [(130, 150, 120, 40)] * (FPS * 20)
    assert run(seq) == []


def test_fall_alert_becomes_automatic_incident_with_evidence(login, cfg, app, make_client):
    from app.db import connect
    from app.liveanalysis import raise_fall_incident
    from .conftest import enroll
    admin = login("admin")
    cam = enroll(admin, "camera_node", "Corridor cam")["device"]
    conn = connect(cfg.db_path)
    try:
        inc = raise_fall_incident(cfg, app.state.hub, conn, cam["id"], frame_with((130, 150, 120, 40)),
                                  {"bbox": [130, 150, 120, 40], "still_s": 9.0, "confidence": 0.5, "reason": "x"})
    finally:
        conn.close()
    assert inc["source"] == "sensor" and inc["category"] == "possible_fall" and inc["state"] == "NEW"
    assert inc["message"].startswith("AUTOMATIC") and "Place this camera on the map" in inc["location_note"]
    r = login("resp").get(f"/api/v1/incidents/{inc['id']}/evidence")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert login("alice").get(f"/api/v1/incidents/{inc['id']}/evidence").status_code == 403


def test_mjpeg_parser_handles_split_chunks():
    from app.camrelay import parse_mjpeg
    part = lambda body: b"\r\n--skymeshframe\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(body) + body  # noqa: E731
    stream = part(b"\xff\xd8AAAA\xff\xd9") + part(b"\xff\xd8BBBBBB\xff\xd9")
    buf, got = bytearray(), []
    for i in range(0, len(stream), 7):  # arrives in small pieces
        buf += stream[i:i + 7]
        got += parse_mjpeg(buf)
    assert got == [b"\xff\xd8AAAA\xff\xd9", b"\xff\xd8BBBBBB\xff\xd9"]


def test_placed_camera_becomes_ble_anchor_for_badges(login, make_client, floor):
    from .conftest import enroll, heartbeat
    admin = login("admin")
    cam = enroll(admin, "camera_node", "Cam A")
    heartbeat(make_client(), cam["device_token"], capabilities={"beacon_id": "24:0A:C4:12:34:56"})
    a = admin.post(f"/api/v1/floors/{floor['floor']['id']}/anchors", json={
        "kind": "camera_node", "name": "Cam A", "x": 0.45, "y": 0.35, "device_id": cam["device"]["id"]}).json()
    assert a["identifier"] == "24:0a:c4:12:34:56" and a["rssi_at_1m"] == -45
    badge = enroll(admin, "badge", "B")
    r = make_client().post("/api/v1/device/location", json={"kind": "ble", "ble": [{"id": "24:0a:c4:12:34:56", "rssi": -62}]},
                           headers={"Authorization": f"Bearer {badge['device_token']}"}).json()
    assert r["kind"] == "room" and r["room_id"] == floor["room"]["id"]


def test_person_who_gets_up_quickly_does_not_alert():
    seq = [None] * 20 + [(60, 60, 40, 120)] * 10 + [(60, 150, 120, 40)] * (FPS * 4) + [(60, 60, 40, 120)] * 30
    assert run(seq) == []
