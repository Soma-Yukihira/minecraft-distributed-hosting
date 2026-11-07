-- Minecraft Distributed Hosting - control plane schema, version 1.
-- Ids are 32-char lowercase hex (uuid4().hex). Timestamps are UTC unix seconds (REAL).

CREATE TABLE users (
    id              TEXT PRIMARY KEY,
    username        TEXT NOT NULL,
    password_hash   TEXT NOT NULL,              -- argon2id encoded string
    created_at      REAL NOT NULL,
    disabled        INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1))
);
CREATE UNIQUE INDEX users_username_ci ON users (lower(username));

CREATE TABLE refresh_tokens (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    family_id       TEXT NOT NULL,              -- reuse of a rotated token revokes the family
    token_hash      TEXT NOT NULL UNIQUE,       -- sha256 of the opaque token, never the token
    created_at      REAL NOT NULL,
    expires_at      REAL NOT NULL,
    revoked_at      REAL,
    replaced_by     TEXT REFERENCES refresh_tokens (id)
);
CREATE INDEX refresh_tokens_family ON refresh_tokens (family_id);

CREATE TABLE access_tokens (
    token_hash      TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    refresh_id      TEXT REFERENCES refresh_tokens (id) ON DELETE CASCADE,
    expires_at      REAL NOT NULL
);

CREATE TABLE groups (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    owner_id            TEXT NOT NULL REFERENCES users (id),
    created_at          REAL NOT NULL,
    storage_quota_bytes INTEGER NOT NULL CHECK (storage_quota_bytes > 0)
);

CREATE TABLE memberships (
    group_id        TEXT NOT NULL REFERENCES groups (id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
    can_host        INTEGER NOT NULL DEFAULT 1 CHECK (can_host IN (0, 1)),
    joined_at       REAL NOT NULL,
    PRIMARY KEY (group_id, user_id)
);

CREATE TABLE invites (
    code_hash       TEXT PRIMARY KEY,
    group_id        TEXT NOT NULL REFERENCES groups (id) ON DELETE CASCADE,
    created_by      TEXT NOT NULL REFERENCES users (id),
    max_uses        INTEGER NOT NULL CHECK (max_uses > 0),
    uses            INTEGER NOT NULL DEFAULT 0,
    expires_at      REAL NOT NULL,
    CHECK (uses <= max_uses)
);

CREATE TABLE hosts (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    display_name        TEXT NOT NULL,
    os                  TEXT NOT NULL,
    cpu_cores           INTEGER NOT NULL,
    total_ram_mb        INTEGER NOT NULL,
    hosting_allowed     INTEGER NOT NULL DEFAULT 1 CHECK (hosting_allowed IN (0, 1)),
    auto_accept_offers  INTEGER NOT NULL DEFAULT 0 CHECK (auto_accept_offers IN (0, 1)),
    max_ram_mb          INTEGER,
    created_at          REAL NOT NULL,
    last_seen_at        REAL
);

CREATE TABLE servers (
    id                      TEXT PRIMARY KEY,
    group_id                TEXT NOT NULL REFERENCES groups (id) ON DELETE RESTRICT,
    name                    TEXT NOT NULL,
    mc_version              TEXT NOT NULL,
    loader                  TEXT NOT NULL CHECK (loader IN ('vanilla', 'paper', 'fabric')),
    memory_mb               INTEGER NOT NULL CHECK (memory_mb >= 1024),
    properties_json         TEXT NOT NULL DEFAULT '{}',
    preemption              TEXT NOT NULL DEFAULT 'when_empty'
                                CHECK (preemption IN ('never', 'when_empty', 'countdown')),
    auto_failover_after_s   INTEGER,             -- NULL: dirty failover needs an admin
    hot_backup_interval_s   INTEGER NOT NULL DEFAULT 900,
    state                   TEXT NOT NULL DEFAULT 'offline' CHECK (state IN (
                                'offline', 'electing', 'assigned', 'starting', 'running',
                                'migrating', 'stopping', 'unreachable', 'orphaned')),
    -- 1 when the group wants the server up: the control plane keeps looking for a host.
    desired_running         INTEGER NOT NULL DEFAULT 0 CHECK (desired_running IN (0, 1)),
    head_version_id         TEXT REFERENCES world_versions (id),
    config_revision         INTEGER NOT NULL DEFAULT 1,
    created_at              REAL NOT NULL
);

CREATE TABLE server_host_prefs (
    server_id       TEXT NOT NULL REFERENCES servers (id) ON DELETE CASCADE,
    host_id         TEXT NOT NULL REFERENCES hosts (id) ON DELETE CASCADE,
    priority        INTEGER NOT NULL DEFAULT 50 CHECK (priority BETWEEN 0 AND 100),
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    PRIMARY KEY (server_id, host_id)
);

-- Exactly one lease row per server. The epoch is the fencing token.
CREATE TABLE leases (
    server_id       TEXT PRIMARY KEY REFERENCES servers (id) ON DELETE CASCADE,
    epoch           INTEGER NOT NULL DEFAULT 0 CHECK (epoch >= 0),
    holder_host_id  TEXT REFERENCES hosts (id),
    granted_at      REAL,
    expires_at      REAL,
    CHECK ((holder_host_id IS NULL) = (expires_at IS NULL))
);

CREATE TRIGGER leases_epoch_never_decreases
BEFORE UPDATE OF epoch ON leases
WHEN NEW.epoch < OLD.epoch
BEGIN
    SELECT RAISE(ABORT, 'lease epoch cannot decrease');
END;

CREATE TABLE world_versions (
    id                  TEXT PRIMARY KEY,
    server_id           TEXT NOT NULL REFERENCES servers (id) ON DELETE RESTRICT,
    parent_id           TEXT REFERENCES world_versions (id),
    epoch               INTEGER NOT NULL,
    host_id             TEXT REFERENCES hosts (id),
    kind                TEXT NOT NULL CHECK (kind IN ('final', 'hot', 'manual', 'restore')),
    manifest_sha256     TEXT NOT NULL,
    total_size          INTEGER NOT NULL CHECK (total_size >= 0),
    created_at          REAL NOT NULL,
    pinned              INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    label               TEXT
);
CREATE INDEX world_versions_server ON world_versions (server_id, created_at);

-- Retention may delete old versions, but never the head nor a pinned version.
CREATE TRIGGER world_versions_protect_head
BEFORE DELETE ON world_versions
WHEN OLD.pinned = 1 OR EXISTS (SELECT 1 FROM servers WHERE head_version_id = OLD.id)
BEGIN
    SELECT RAISE(ABORT, 'cannot delete the head or a pinned world version');
END;

-- Versions are immutable once committed (only pin/label may change).
CREATE TRIGGER world_versions_immutable
BEFORE UPDATE OF server_id, parent_id, epoch, host_id, kind, manifest_sha256, total_size, created_at
ON world_versions
BEGIN
    SELECT RAISE(ABORT, 'world versions are immutable');
END;

CREATE TABLE blobs (
    group_id        TEXT NOT NULL REFERENCES groups (id) ON DELETE CASCADE,
    sha256          TEXT NOT NULL,
    size            INTEGER NOT NULL CHECK (size >= 0),
    stored_size     INTEGER NOT NULL CHECK (stored_size >= 0),
    created_at      REAL NOT NULL,
    PRIMARY KEY (group_id, sha256)
);

CREATE TABLE version_blobs (
    version_id      TEXT NOT NULL REFERENCES world_versions (id) ON DELETE CASCADE,
    sha256          TEXT NOT NULL,
    PRIMARY KEY (version_id, sha256)
);
CREATE INDEX version_blobs_sha ON version_blobs (sha256);

CREATE TABLE offers (
    id              TEXT PRIMARY KEY,
    server_id       TEXT NOT NULL REFERENCES servers (id) ON DELETE CASCADE,
    host_id         TEXT NOT NULL REFERENCES hosts (id) ON DELETE CASCADE,
    created_at      REAL NOT NULL,
    expires_at      REAL NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'declined', 'expired')),
    reason          TEXT NOT NULL DEFAULT 'election'   -- 'election' | 'resume'
);
CREATE INDEX offers_pending ON offers (status, expires_at);

CREATE TABLE events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id        TEXT NOT NULL REFERENCES groups (id) ON DELETE CASCADE,
    server_id       TEXT REFERENCES servers (id) ON DELETE CASCADE,
    actor_user_id   TEXT REFERENCES users (id) ON DELETE SET NULL,
    host_id         TEXT REFERENCES hosts (id) ON DELETE SET NULL,
    type            TEXT NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}',
    created_at      REAL NOT NULL
);
CREATE INDEX events_server ON events (server_id, id);

CREATE TABLE idempotency_keys (
    key             TEXT NOT NULL,
    user_id         TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    request_hash    TEXT NOT NULL,
    response_json   TEXT NOT NULL,
    created_at      REAL NOT NULL,
    PRIMARY KEY (user_id, key)
);
