-- SkyMesh initial schema (SQLite). All timestamps are server-side UTC ISO-8601 strings.

CREATE TABLE users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'responder', 'user')),
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);

CREATE TABLE sessions (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0,
    user_agent  TEXT
);
CREATE INDEX idx_sessions_user ON sessions(user_id);

CREATE TABLE devices (
    id               TEXT PRIMARY KEY,
    type             TEXT NOT NULL CHECK (type IN ('phone', 'badge', 'camera_node', 'anchor', 'gateway', 'laptop')),
    name             TEXT NOT NULL,
    owner_user_id    TEXT REFERENCES users(id) ON DELETE SET NULL,
    token_hash       TEXT UNIQUE,
    -- Camera nodes only: shared key the backend presents to the node's /capture endpoint.
    camera_key       TEXT,
    hardware_model   TEXT,
    firmware_version TEXT,
    enrolled_at      TEXT NOT NULL,
    enrolled_by      TEXT REFERENCES users(id) ON DELETE SET NULL,
    revoked_at       TEXT,
    last_seen_at     TEXT,
    last_ip          TEXT,
    last_rssi        INTEGER,
    last_bssid       TEXT,
    battery_mv       INTEGER,
    battery_pct      INTEGER,
    camera_ok        INTEGER,
    free_heap        INTEGER,
    uptime_s         INTEGER,
    last_sequence    INTEGER,
    capabilities     TEXT NOT NULL DEFAULT '{}',
    sharing_enabled  INTEGER NOT NULL DEFAULT 0,
    permission_state TEXT NOT NULL DEFAULT 'unknown'
        CHECK (permission_state IN ('unknown', 'granted', 'denied', 'unsupported', 'not_applicable'))
);
CREATE INDEX idx_devices_owner ON devices(owner_user_id);

CREATE TABLE device_heartbeats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    received_at TEXT NOT NULL,
    payload     TEXT NOT NULL
);
CREATE INDEX idx_heartbeats_device_time ON device_heartbeats(device_id, received_at);

CREATE TABLE buildings (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE floors (
    id          TEXT PRIMARY KEY,
    building_id TEXT NOT NULL REFERENCES buildings(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    level       INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (building_id, level)
);

CREATE TABLE map_images (
    id          TEXT PRIMARY KEY,
    floor_id    TEXT NOT NULL REFERENCES floors(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    file_name   TEXT NOT NULL,
    width       INTEGER NOT NULL,
    height      INTEGER NOT NULL,
    source_file TEXT,
    transform   TEXT NOT NULL DEFAULT '{}',
    active      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    created_by  TEXT REFERENCES users(id) ON DELETE SET NULL,
    UNIQUE (floor_id, version)
);

-- Rooms, corridors, exits, ... drawn on a floor in normalized image coordinates (0..1).
CREATE TABLE map_features (
    id           TEXT PRIMARY KEY,
    floor_id     TEXT NOT NULL REFERENCES floors(id) ON DELETE CASCADE,
    map_image_id TEXT REFERENCES map_images(id) ON DELETE SET NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('room', 'corridor', 'stair', 'lift', 'exit', 'entrance',
                                               'restricted', 'assembly_point', 'void', 'toilet', 'other')),
    geometry_type TEXT NOT NULL CHECK (geometry_type IN ('point', 'polygon')),
    name         TEXT NOT NULL,
    geometry     TEXT NOT NULL,
    properties   TEXT NOT NULL DEFAULT '{}',
    source       TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'assist_draft')),
    verified     INTEGER NOT NULL DEFAULT 0,
    verified_by  TEXT REFERENCES users(id) ON DELETE SET NULL,
    verified_at  TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX idx_features_floor ON map_features(floor_id);

CREATE TABLE floor_transitions (
    id              TEXT PRIMARY KEY,
    from_feature_id TEXT NOT NULL REFERENCES map_features(id) ON DELETE CASCADE,
    to_feature_id   TEXT NOT NULL REFERENCES map_features(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CHECK (kind IN ('stair', 'lift', 'ramp')),
    created_at      TEXT NOT NULL
);

-- Installed infrastructure placed on the map (BLE anchors, APs, camera nodes, reference points).
CREATE TABLE anchors (
    id          TEXT PRIMARY KEY,
    floor_id    TEXT NOT NULL REFERENCES floors(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('ble_anchor', 'wifi_ap', 'camera_node', 'gateway', 'reference', 'server')),
    name        TEXT NOT NULL,
    x           REAL NOT NULL CHECK (x >= 0 AND x <= 1),
    y           REAL NOT NULL CHECK (y >= 0 AND y <= 1),
    device_id   TEXT REFERENCES devices(id) ON DELETE SET NULL,
    identifier  TEXT,
    rssi_at_1m  INTEGER,
    properties  TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_anchors_floor ON anchors(floor_id);
CREATE INDEX idx_anchors_identifier ON anchors(identifier);

-- Ground control points relating WGS84 lat/lon to a floor's normalized coordinates.
CREATE TABLE georef_points (
    id         TEXT PRIMARY KEY,
    floor_id   TEXT NOT NULL REFERENCES floors(id) ON DELETE CASCADE,
    lat        REAL NOT NULL CHECK (lat BETWEEN -90 AND 90),
    lon        REAL NOT NULL CHECK (lon BETWEEN -180 AND 180),
    x          REAL NOT NULL CHECK (x >= 0 AND x <= 1),
    y          REAL NOT NULL CHECK (y >= 0 AND y <= 1),
    note       TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE camera_calibrations (
    device_id    TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
    floor_id     TEXT NOT NULL REFERENCES floors(id) ON DELETE CASCADE,
    image_points TEXT NOT NULL,
    map_points   TEXT NOT NULL,
    homography   TEXT NOT NULL,
    frame_w      INTEGER NOT NULL,
    frame_h      INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    created_by   TEXT REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE location_updates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    received_at TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('floor', 'geo', 'room', 'unavailable', 'denied')),
    floor_id    TEXT REFERENCES floors(id) ON DELETE SET NULL,
    room_id     TEXT REFERENCES map_features(id) ON DELETE SET NULL,
    x           REAL CHECK (x IS NULL OR (x >= 0 AND x <= 1)),
    y           REAL CHECK (y IS NULL OR (y >= 0 AND y <= 1)),
    lat         REAL,
    lon         REAL,
    accuracy_m  REAL,
    source      TEXT NOT NULL,
    confidence  REAL,
    sequence    INTEGER,
    flags       TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX idx_locupd_device_time ON location_updates(device_id, observed_at);
CREATE INDEX idx_locupd_received ON location_updates(received_at);

-- One row per device: the latest accepted location, so the map never scans history.
CREATE TABLE current_locations (
    device_id   TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
    update_id   INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    kind        TEXT NOT NULL,
    floor_id    TEXT,
    room_id     TEXT,
    x           REAL,
    y           REAL,
    lat         REAL,
    lon         REAL,
    accuracy_m  REAL,
    source      TEXT NOT NULL,
    confidence  REAL,
    flags       TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE conversations (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('direct', 'group', 'announcement')),
    name       TEXT,
    direct_key TEXT UNIQUE,
    created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE conversation_members (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    member_role     TEXT NOT NULL DEFAULT 'member' CHECK (member_role IN ('owner', 'member')),
    joined_at       TEXT NOT NULL,
    PRIMARY KEY (conversation_id, user_id)
);
CREATE INDEX idx_members_user ON conversation_members(user_id);

CREATE TABLE messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    sender_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_msg_id   TEXT NOT NULL,
    body            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    deleted_at      TEXT,
    deleted_by      TEXT REFERENCES users(id) ON DELETE SET NULL,
    UNIQUE (sender_id, client_msg_id)
);
CREATE INDEX idx_messages_conv ON messages(conversation_id, id);

CREATE TABLE message_receipts (
    message_id   INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    delivered_at TEXT,
    read_at      TEXT,
    PRIMARY KEY (message_id, user_id)
);

CREATE TABLE message_reports (
    id          TEXT PRIMARY KEY,
    message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    reporter_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE incidents (
    id                TEXT PRIMARY KEY,
    source            TEXT NOT NULL CHECK (source IN ('phone', 'badge', 'dashboard_test', 'sensor')),
    category          TEXT NOT NULL,
    severity          TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    state             TEXT NOT NULL CHECK (state IN ('NEW', 'ACKNOWLEDGED', 'RESPONDING', 'RESOLVED', 'FALSE_ALARM')),
    is_test           INTEGER NOT NULL DEFAULT 0,
    reporter_user_id  TEXT REFERENCES users(id) ON DELETE SET NULL,
    device_id         TEXT REFERENCES devices(id) ON DELETE SET NULL,
    idempotency_key   TEXT NOT NULL UNIQUE,
    created_at        TEXT NOT NULL,
    device_timestamp  TEXT,
    floor_id          TEXT,
    room_id           TEXT,
    x                 REAL,
    y                 REAL,
    lat               REAL,
    lon               REAL,
    location_source   TEXT,
    location_observed_at TEXT,
    accuracy_m        REAL,
    location_note     TEXT NOT NULL,
    message           TEXT,
    assigned_to       TEXT REFERENCES users(id) ON DELETE SET NULL,
    acknowledged_by   TEXT REFERENCES users(id) ON DELETE SET NULL,
    acknowledged_at   TEXT,
    resolved_by       TEXT REFERENCES users(id) ON DELETE SET NULL,
    resolved_at       TEXT,
    updated_at        TEXT NOT NULL
);
CREATE INDEX idx_incidents_state ON incidents(state);

CREATE TABLE incident_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id     TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    at              TEXT NOT NULL,
    actor_user_id   TEXT REFERENCES users(id) ON DELETE SET NULL,
    actor_device_id TEXT REFERENCES devices(id) ON DELETE SET NULL,
    kind            TEXT NOT NULL,
    from_state      TEXT,
    to_state        TEXT,
    note            TEXT
);
CREATE INDEX idx_incident_events ON incident_events(incident_id, id);

CREATE TABLE camera_observations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    session_id       TEXT NOT NULL,
    track_label      TEXT NOT NULL,
    observed_at      TEXT NOT NULL,
    frame_index      INTEGER,
    bbox             TEXT NOT NULL,
    confidence       REAL NOT NULL,
    frame_w          INTEGER NOT NULL,
    frame_h          INTEGER NOT NULL,
    floor_id         TEXT,
    x                REAL,
    y                REAL,
    projected        INTEGER NOT NULL DEFAULT 0,
    source           TEXT NOT NULL CHECK (source IN ('live', 'video_file')),
    created_at       TEXT NOT NULL
);
CREATE INDEX idx_camobs_cam_time ON camera_observations(camera_device_id, observed_at);
CREATE INDEX idx_camobs_track ON camera_observations(session_id, track_label);

CREATE TABLE analysis_sessions (
    id               TEXT PRIMARY KEY,
    camera_device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    source           TEXT NOT NULL,
    source_name      TEXT,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    frames_processed INTEGER NOT NULL DEFAULT 0,
    detections       INTEGER NOT NULL DEFAULT 0,
    tracks           INTEGER NOT NULL DEFAULT 0,
    projected        INTEGER NOT NULL DEFAULT 0,
    detector         TEXT NOT NULL,
    note             TEXT
);

-- Person observation <-> device associations exist only when a human confirms them.
CREATE TABLE associations (
    id               TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL,
    track_label      TEXT NOT NULL,
    device_id        TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    method           TEXT NOT NULL CHECK (method IN ('operator_confirmed', 'user_confirmed')),
    confirmed_by     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    note             TEXT,
    created_at       TEXT NOT NULL,
    revoked_at       TEXT
);

CREATE TABLE network_links (
    id         TEXT PRIMARY KEY,
    from_node  TEXT NOT NULL,
    to_node    TEXT NOT NULL,
    medium     TEXT NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              TEXT NOT NULL,
    actor_user_id   TEXT,
    actor_device_id TEXT,
    action          TEXT NOT NULL,
    target          TEXT,
    detail          TEXT NOT NULL DEFAULT '{}',
    ip              TEXT
);
CREATE INDEX idx_audit_at ON audit_log(at);

CREATE TABLE settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
