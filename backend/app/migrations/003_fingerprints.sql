-- Map trainer: radio fingerprints recorded at known spots ("I'm here"), used to locate badges.

CREATE TABLE training_sessions (
    id          TEXT PRIMARY KEY,
    device_id   TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    floor_id    TEXT NOT NULL REFERENCES floors(id) ON DELETE CASCADE,
    x           REAL NOT NULL CHECK (x >= 0 AND x <= 1),
    y           REAL NOT NULL CHECK (y >= 0 AND y <= 1),
    room_id     TEXT REFERENCES map_features(id) ON DELETE SET NULL,
    target      INTEGER NOT NULL DEFAULT 4,
    samples     INTEGER NOT NULL DEFAULT 0,
    started_by  TEXT REFERENCES users(id) ON DELETE SET NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT
);
CREATE INDEX idx_training_device ON training_sessions(device_id, ended_at);

CREATE TABLE fingerprints (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT REFERENCES training_sessions(id) ON DELETE CASCADE,
    floor_id    TEXT NOT NULL REFERENCES floors(id) ON DELETE CASCADE,
    x           REAL NOT NULL,
    y           REAL NOT NULL,
    room_id     TEXT REFERENCES map_features(id) ON DELETE SET NULL,
    device_id   TEXT REFERENCES devices(id) ON DELETE SET NULL,
    vector      TEXT NOT NULL,          -- {"aa:bb:..": -67, ...}  (Wi-Fi BSSIDs and SkyMesh beacons)
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_fp_floor ON fingerprints(floor_id);
