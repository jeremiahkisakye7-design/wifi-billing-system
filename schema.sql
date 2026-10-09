-- Voucher lifecycle additions used by app.init_db().
-- Existing client columns are added idempotently by the application's migration code.

CREATE TABLE IF NOT EXISTS voucher_devices (
    id TEXT PRIMARY KEY,
    voucher_code TEXT NOT NULL,
    device_mac TEXT NOT NULL,
    created_at TEXT NOT NULL,
    device_slot_id TEXT,
    UNIQUE (voucher_code, device_mac)
);
CREATE INDEX IF NOT EXISTS idx_voucher_devices_voucher
    ON voucher_devices(voucher_code);
CREATE INDEX IF NOT EXISTS idx_voucher_devices_slot
    ON voucher_devices(voucher_code, device_slot_id);

CREATE TABLE IF NOT EXISTS voucher_device_tokens (
    token_hash TEXT PRIMARY KEY,
    voucher_code TEXT NOT NULL,
    device_slot_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_voucher_tokens_code
    ON voucher_device_tokens(voucher_code);

CREATE TABLE IF NOT EXISTS voucher_login_failures (
    id TEXT PRIMARY KEY,
    ip_hash TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_voucher_failures_ip_created
    ON voucher_login_failures(ip_hash, created_at);

CREATE TABLE IF NOT EXISTS active_sessions (
    id TEXT PRIMARY KEY,
    voucher_code TEXT NOT NULL,
    device_mac TEXT NOT NULL,
    device_type TEXT NOT NULL DEFAULT '',
    connected_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    disconnected_at TEXT,
    bytes_used INTEGER NOT NULL DEFAULT 0,
    UNIQUE (voucher_code, device_mac)
);
CREATE INDEX IF NOT EXISTS idx_active_sessions_status
    ON active_sessions(status);
CREATE INDEX IF NOT EXISTS idx_active_sessions_voucher
    ON active_sessions(voucher_code);

CREATE TABLE IF NOT EXISTS session_usage_events (
    event_id TEXT PRIMARY KEY,
    voucher_code TEXT NOT NULL,
    device_mac TEXT NOT NULL,
    bytes_delta INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_usage_voucher
    ON session_usage_events(voucher_code);

-- clients lifecycle columns migrated by app.init_db():
-- duration_seconds, activated_at, activation_deadline, max_devices,
-- disabled_at, paused_at, paused_seconds, data_limit_mb, speed_limit_mbps.
-- Existing voucher_devices rows receive device_slot_id = device_mac during migration.
