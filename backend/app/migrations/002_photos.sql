-- Photos sent by badges (with an SOS, or when a responder asks) and queued device commands.

CREATE TABLE device_photos (
    id          TEXT PRIMARY KEY,
    device_id   TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    incident_id TEXT REFERENCES incidents(id) ON DELETE SET NULL,
    command_id  TEXT,
    reason      TEXT NOT NULL CHECK (reason IN ('sos', 'request', 'test')),
    file_name   TEXT NOT NULL,
    bytes       INTEGER NOT NULL,
    width       INTEGER,
    height      INTEGER,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_photos_device ON device_photos(device_id, created_at);
CREATE INDEX idx_photos_incident ON device_photos(incident_id);

CREATE TABLE device_commands (
    id           TEXT PRIMARY KEY,
    device_id    TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    cmd          TEXT NOT NULL CHECK (cmd IN ('photo')),
    requested_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at   TEXT NOT NULL,
    delivered_at TEXT,
    done_at      TEXT
);
CREATE INDEX idx_commands_device ON device_commands(device_id, delivered_at);
