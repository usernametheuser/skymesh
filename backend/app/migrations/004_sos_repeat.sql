-- Repeated SOS presses: a press while the person's alert is still open re-alerts that incident.

ALTER TABLE incidents ADD COLUMN repeat_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE incidents ADD COLUMN last_alert_at TEXT;

CREATE TABLE sos_presses (
    idempotency_key TEXT PRIMARY KEY,           -- "<device>:<event>" or "user:<id>:<key>"
    incident_id     TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_sos_presses_incident ON sos_presses(incident_id);
