"""SQLite access layer and schema.

Keepstack uses a single SQLite database with WAL mode and FTS5 full-text
search. SQLite keeps the install dependency-free and makes the whole
repository a single portable file plus a blob directory: no external
database server to provision, back up, or secure.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from .config import config

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    email         TEXT,
    full_name     TEXT,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'viewer',
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    last_login    TEXT
);

CREATE TABLE IF NOT EXISTS assets (
    id                INTEGER PRIMARY KEY,
    uuid              TEXT UNIQUE NOT NULL,
    sha256            TEXT NOT NULL,
    filename          TEXT NOT NULL,
    original_filename TEXT,
    mime_type         TEXT,
    media_type        TEXT,
    ext               TEXT,
    size              INTEGER,
    width             INTEGER,
    height            INTEGER,
    duration          REAL,
    title             TEXT,
    description       TEXT,
    alt_text          TEXT,
    status            TEXT NOT NULL DEFAULT 'active',
    storage_key       TEXT NOT NULL,
    thumb_key         TEXT,
    license           TEXT,
    rights_statement  TEXT,
    credit            TEXT,
    usage_expiry      TEXT,
    retention_until   TEXT,
    dc_creator        TEXT,
    dc_subject        TEXT,
    dc_publisher      TEXT,
    dc_date           TEXT,
    uploaded_by       INTEGER,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    embedding         BLOB,
    FOREIGN KEY (uploaded_by) REFERENCES users (id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_assets_sha    ON assets (sha256);
CREATE INDEX IF NOT EXISTS idx_assets_status ON assets (status);
CREATE INDEX IF NOT EXISTS idx_assets_media  ON assets (media_type);
CREATE INDEX IF NOT EXISTS idx_assets_created ON assets (created_at);

CREATE TABLE IF NOT EXISTS asset_metadata (
    asset_id  INTEGER PRIMARY KEY,
    exif_json TEXT,
    iptc_json TEXT,
    xmp_json  TEXT,
    raw_json  TEXT,
    FOREIGN KEY (asset_id) REFERENCES assets (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS custom_fields (
    id          INTEGER PRIMARY KEY,
    key         TEXT UNIQUE NOT NULL,
    label       TEXT NOT NULL,
    field_type  TEXT NOT NULL DEFAULT 'text',
    options_json TEXT,
    required    INTEGER NOT NULL DEFAULT 0,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS asset_custom_values (
    asset_id INTEGER NOT NULL,
    field_id INTEGER NOT NULL,
    value    TEXT,
    PRIMARY KEY (asset_id, field_id),
    FOREIGN KEY (asset_id) REFERENCES assets (id) ON DELETE CASCADE,
    FOREIGN KEY (field_id) REFERENCES custom_fields (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tags (
    id       INTEGER PRIMARY KEY,
    name     TEXT UNIQUE NOT NULL,
    category TEXT
);

CREATE TABLE IF NOT EXISTS asset_tags (
    asset_id   INTEGER NOT NULL,
    tag_id     INTEGER NOT NULL,
    source     TEXT NOT NULL DEFAULT 'manual',
    confidence REAL,
    PRIMARY KEY (asset_id, tag_id),
    FOREIGN KEY (asset_id) REFERENCES assets (id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id)   REFERENCES tags (id)   ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS collections (
    id          INTEGER PRIMARY KEY,
    uuid        TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    description TEXT,
    parent_id   INTEGER,
    kind        TEXT NOT NULL DEFAULT 'folder',
    query_json  TEXT,
    created_by  INTEGER,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (parent_id) REFERENCES collections (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS collection_assets (
    collection_id INTEGER NOT NULL,
    asset_id      INTEGER NOT NULL,
    added_at      TEXT,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (collection_id, asset_id),
    FOREIGN KEY (collection_id) REFERENCES collections (id) ON DELETE CASCADE,
    FOREIGN KEY (asset_id)      REFERENCES assets (id)      ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS asset_versions (
    id          INTEGER PRIMARY KEY,
    asset_id    INTEGER NOT NULL,
    version_no  INTEGER NOT NULL,
    sha256      TEXT,
    storage_key TEXT,
    size        INTEGER,
    note        TEXT,
    created_by  INTEGER,
    created_at  TEXT,
    FOREIGN KEY (asset_id) REFERENCES assets (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS share_links (
    id             INTEGER PRIMARY KEY,
    token          TEXT UNIQUE NOT NULL,
    target_type    TEXT NOT NULL,
    target_id      INTEGER NOT NULL,
    permission     TEXT NOT NULL DEFAULT 'view',
    expires_at     TEXT,
    max_downloads  INTEGER,
    download_count INTEGER NOT NULL DEFAULT 0,
    password_hash  TEXT,
    created_by     INTEGER,
    created_at     TEXT,
    revoked        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    user_id     INTEGER,
    username    TEXT,
    action      TEXT NOT NULL,
    target_type TEXT,
    target_id   TEXT,
    detail      TEXT,
    ip          TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log (ts);

CREATE TABLE IF NOT EXISTS saved_searches (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER,
    name       TEXT,
    query_json TEXT,
    created_at TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS asset_fts USING fts5(
    title, description, alt_text, filename, tags, metadata_text, custom_text,
    tokenize = 'porter unicode61'
);
"""

# Schema changes after the original v0 schema live here. Each entry is applied
# once, in order, and recorded in schema_meta. Keep migrations additive and
# idempotent: existing installations must upgrade without losing data.
CURRENT_SCHEMA_VERSION = 1

MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        # Keyset order: (status + created_at + id) covers the required time
        # sorts, with id as the deterministic tie-breaker.
        "CREATE INDEX IF NOT EXISTS idx_assets_status_created_id "
        "ON assets (status, created_at, id)",
        # The other legacy sorts get the same deterministic shape. COALESCE
        # keeps NULL values out of the cursor comparison path.
        "CREATE INDEX IF NOT EXISTS idx_assets_status_name_id "
        "ON assets (status, COALESCE(title, ''), id)",
        "CREATE INDEX IF NOT EXISTS idx_assets_status_size_id "
        "ON assets (status, COALESCE(size, 0), id)",
        # Tag filtering uses correlated EXISTS queries by tag first, asset
        # second. The primary key has the opposite order.
        "CREATE INDEX IF NOT EXISTS idx_asset_tags_tag_asset "
        "ON asset_tags (tag_id, asset_id)",
    ),
}

_local = threading.local()


def new_connection(*, check_same_thread: bool = False) -> sqlite3.Connection:
    """Open an independent connection to the configured database.

    Long-running readers such as CSV exports need a connection with a stable
    read transaction, separate from the request thread's shared connection.
    """
    config.ensure_dirs()
    conn = sqlite3.connect(config.db_path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def get_conn() -> sqlite3.Connection:
    """One connection per thread (SQLite connections are not thread-safe)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = new_connection(check_same_thread=False)
        _local.conn = conn
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@contextmanager
def read_transaction(conn: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
    """Hold a consistent read snapshot for the duration of the block."""
    target = conn or get_conn()
    target.execute("BEGIN")
    try:
        yield target
    finally:
        target.rollback()


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    if not row:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Invalid schema_version in schema_meta") from exc


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Apply all pending schema migrations atomically and return the version.

    A database created by the original v0 code has no schema_version row, so it
    starts at 0. Re-running this function is safe.
    """
    current = schema_version(conn)
    if current > CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema v{current} is newer than this build "
            f"(v{CURRENT_SCHEMA_VERSION}); refusing to start"
        )
    for target in range(current + 1, CURRENT_SCHEMA_VERSION + 1):
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in MIGRATIONS[target]:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(target),),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return CURRENT_SCHEMA_VERSION


def init_db() -> None:
    """Create the base schema and apply pending migrations. Idempotent."""
    config.ensure_dirs()
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    apply_migrations(conn)
