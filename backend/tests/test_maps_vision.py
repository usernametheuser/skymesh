from pathlib import Path

import cv2
import numpy as np

from app import positioning as pos
from app.analysis import analyze_video
from app.vision import Detection, IouTracker

from .conftest import enroll


def test_floor_detail_and_image(login, floor):
    alice = login("alice")
    d = alice.get(f"/api/v1/floors/{floor['floor']['id']}").json()
    assert d["image"]["width"] == 1800 and d["image"]["height"] > 1000
    assert alice.get(d["image"]["url"]).headers["content-type"] == "image/jpeg"
    assert [f["name"] for f in d["features"]] == ["Classroom 05"]


def test_features_validate_normalized_coordinates(login, floor):
    admin = login("admin")
    fid = floor["floor"]["id"]
    bad = admin.post(f"/api/v1/floors/{fid}/features", json={"kind": "room", "name": "x",
                                                             "geometry": [[0.1, 0.1], [1.4, 0.1], [0.2, 0.3]]})
    assert bad.status_code == 422
    two = admin.post(f"/api/v1/floors/{fid}/features", json={"kind": "room", "name": "x",
                                                             "geometry": [[0.1, 0.1], [0.2, 0.2]]})
    assert two.status_code == 422


def test_new_image_version_flags_annotations_for_recheck(login, floor):
    admin = login("admin")
    fid = floor["floor"]["id"]
    r = admin.post(f"/api/v1/floors/{fid}/images", json={"source_file": "floorplan_photo_1.jpg", "rotate": 0})
    assert r.status_code == 201 and r.json()["version"] == 2
    f = admin.get(f"/api/v1/floors/{fid}").json()["features"][0]
    assert f["needs_recheck"] is True
    admin.post(f"/api/v1/features/{f['id']}/verify")
    assert admin.get(f"/api/v1/floors/{fid}").json()["features"][0]["needs_recheck"] is False


def test_rectify_rejects_bad_input(login, floor):
    admin = login("admin")
    fid = floor["floor"]["id"]
    assert admin.post(f"/api/v1/floors/{fid}/images", json={"source_file": "../secrets.jpg"}).status_code == 404
    assert admin.post(f"/api/v1/floors/{fid}/images", json={
        "source_file": "floorplan_photo_1.jpg", "corners": [[0, 0], [5000, 0], [5000, 5000], [0, 5000]]}).status_code == 422


def test_assist_proposes_unverified_exit_drafts(login, floor):
    admin = login("admin")
    fid = floor["floor"]["id"]
    r = admin.post(f"/api/v1/floors/{fid}/assist").json()
    assert r["proposed"] >= 3
    drafts = [f for f in admin.get(f"/api/v1/floors/{fid}").json()["features"] if f["source"] == "assist_draft"]
    assert drafts and all(not f["verified"] for f in drafts)
    labels = [f for f in drafts if f["name"].startswith("EXIT label")]
    assert len(labels) == 3  # the three EXIT boxes printed on the supplied plan


def test_scale_from_known_distance(login, floor):
    admin = login("admin")
    t = admin.post(f"/api/v1/floors/{floor['floor']['id']}/scale",
                   json={"a": [0.0, 0.5], "b": [0.5, 0.5], "distance_m": 30}).json()
    assert t["metres_per_unit"] == 60.0


def test_anchor_identifiers_hidden_from_users(login, floor):
    admin = login("admin")
    fid = floor["floor"]["id"]
    admin.post(f"/api/v1/floors/{fid}/anchors", json={"kind": "ble_anchor", "name": "A", "x": 0.5, "y": 0.5,
                                                      "identifier": "AA:BB:CC:DD:EE:FF"})
    a = login("alice").get(f"/api/v1/floors/{fid}").json()["anchors"][0]
    assert "identifier" not in a


# ---------------------------------------------------------------- vision

def test_tracker_keeps_identity_across_frames_and_labels_anonymously():
    t = IouTracker()
    a = t.update([Detection(10, 10, 50, 100, 0.9)], 0)
    b = t.update([Detection(14, 12, 50, 100, 0.9), Detection(300, 10, 50, 100, 0.8)], 1)
    assert a[0][0].label == "Person observation 001"
    labels = sorted(tr.label for tr, _ in b)
    assert labels == ["Person observation 001", "Person observation 002"]


def test_homography_projection():
    img = [[0, 0], [640, 0], [640, 480], [0, 480]]
    mp = [[0.2, 0.2], [0.4, 0.2], [0.4, 0.4], [0.2, 0.4]]
    h = pos.fit_homography(img, mp)
    x, y = pos.apply_homography(h, 320, 240)
    assert abs(x - 0.3) < 1e-6 and abs(y - 0.3) < 1e-6


class FakeDetector:
    name = "fake"

    def detect(self, frame):
        return [Detection(100, 50, 40, 120, 0.9)]


def _video(path: Path, frames=10):
    w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (320, 240))
    for _ in range(frames):
        w.write(np.zeros((240, 320, 3), np.uint8))
    w.release()


def test_video_analysis_unprojected_without_calibration(login, cfg, tmp_path):
    admin = login("admin")
    cam = enroll(admin, "camera_node", "Cam")["device"]
    vid = tmp_path / "v.mp4"
    _video(vid)
    res = analyze_video(cfg, None, cam["id"], vid, 5, FakeDetector())
    assert res["frames_processed"] >= 4 and res["tracks"] == 1 and res["projected"] == 0
    assert "not calibrated" in res["note"]
    tracks = admin.get(f"/api/v1/cameras/sessions/{res['id']}/tracks").json()
    assert tracks[0]["track_label"] == "Person observation 001" and tracks[0]["association"] is None


def test_video_analysis_projects_with_calibration_and_association_is_manual(login, cfg, tmp_path, floor):
    admin = login("admin")
    cam = enroll(admin, "camera_node", "Cam")["device"]
    badge = enroll(admin, "badge", "Badge")["device"]
    r = admin.put(f"/api/v1/cameras/{cam['id']}/calibration", json={
        "floor_id": floor["floor"]["id"], "frame_w": 320, "frame_h": 240,
        "image_points": [[0, 0], [320, 0], [320, 240], [0, 240]],
        "map_points": [[0.2, 0.2], [0.4, 0.2], [0.4, 0.4], [0.2, 0.4]]})
    assert r.status_code == 200 and r.json()["max_reprojection_error"] < 1e-6
    vid = tmp_path / "v.mp4"
    _video(vid)
    res = analyze_video(cfg, None, cam["id"], vid, 5, FakeDetector())
    assert res["projected"] == res["detections"] > 0
    # Projection never moves the badge: the badge still has no location.
    item = [i for i in admin.get("/api/v1/locations/current").json() if i["device_id"] == badge["id"]][0]
    assert item["state"] == "POSITION_UNAVAILABLE"
    alice = login("alice")
    body = {"session_id": res["id"], "track_label": "Person observation 001", "device_id": badge["id"]}
    assert alice.post("/api/v1/associations", json=body).status_code == 403
    assert admin.post("/api/v1/associations", json=body).status_code == 201
    tracks = admin.get(f"/api/v1/cameras/sessions/{res['id']}/tracks").json()
    assert tracks[0]["association"]["device_id"] == badge["id"]


def test_snapshot_unreachable_camera_reports_error(login):
    admin = login("admin")
    cam = enroll(admin, "camera_node", "Cam")["device"]
    r = admin.get(f"/api/v1/cameras/{cam['id']}/snapshot")
    assert r.status_code == 503 and "never sent a heartbeat" in r.json()["error"]["message"]


def test_camera_routes_require_permission(login):
    alice = login("alice")
    assert alice.get("/api/v1/cameras").status_code == 403
    assert alice.get("/api/v1/observations/current").status_code == 403
