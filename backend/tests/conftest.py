import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Tx, connect, new_id, now_iso
from app.main import create_app
from app.routers.auth import insert_user

ORIGINALS = Path(__file__).resolve().parent.parent / "data" / "maps" / "originals"
PASSWORD = "correct-horse-42"


@pytest.fixture
def cfg(tmp_path) -> Settings:
    s = Settings(data_dir=tmp_path / "data")
    s.rate_limits_enabled = False
    s.auto_bootstrap = False
    src = ORIGINALS / "floorplan_photo_1.jpg"
    if src.exists():
        shutil.copy(src, s.maps_dir / "originals" / src.name)
    return s


@pytest.fixture
def app(cfg):
    return create_app(cfg, start_background=False)


@pytest.fixture
def make_client(app):
    clients = []

    def _make() -> TestClient:
        c = TestClient(app)
        c.__enter__()
        clients.append(c)
        return c
    yield _make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def db(cfg, app):  # app first: it runs the migrations
    conn = connect(cfg.db_path)
    yield conn
    conn.close()


@pytest.fixture
def seed(db):
    """Users of each role plus the announcement channel."""
    with Tx(db):
        db.execute("INSERT INTO conversations (id, kind, name, created_at) VALUES (?,?,?,?)",
                   (new_id("conv"), "announcement", "Campus announcements", now_iso()))
        users = {r: insert_user(db, r, r.title(), PASSWORD, role)
                 for r, role in (("admin", "admin"), ("resp", "responder"), ("alice", "user"), ("bob", "user"))}
    return users


@pytest.fixture
def login(make_client, seed):
    def _login(username: str) -> TestClient:
        c = make_client()
        r = c.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})
        assert r.status_code == 200, r.text
        return c
    return _login


@pytest.fixture
def floor(login):
    """A building + floor with a map image and one verified room polygon."""
    admin = login("admin")
    b = admin.post("/api/v1/buildings", json={"name": "Test block"}).json()
    f = admin.post("/api/v1/floors", json={"building_id": b["id"], "name": "Level 1", "level": 1}).json()
    r = admin.post(f"/api/v1/floors/{f['id']}/images", json={"source_file": "floorplan_photo_1.jpg",
                                                             "corners": [[205, 0], [1490, 0], [1490, 1150], [205, 1150]]})
    assert r.status_code == 201, r.text
    room = admin.post(f"/api/v1/floors/{f['id']}/features", json={
        "kind": "room", "name": "Classroom 05", "geometry": [[0.4, 0.3], [0.5, 0.3], [0.5, 0.4], [0.4, 0.4]]}).json()
    return {"floor": f, "building": b, "room": room}


def enroll(admin: TestClient, type_: str, name: str, owner: str | None = None) -> dict:
    r = admin.post("/api/v1/devices", json={"type": type_, "name": name, "owner_user_id": owner})
    assert r.status_code == 201, r.text
    return r.json()


def heartbeat(client: TestClient, token: str, **extra) -> object:
    body = {"schema_version": 1, "firmware_version": "test-1", "uptime_s": 10, "sequence": 1, **extra}
    return client.post("/api/v1/device/heartbeat", json=body, headers={"Authorization": f"Bearer {token}"})
