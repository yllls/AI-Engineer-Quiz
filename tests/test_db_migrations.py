"""Schema migrations must preserve existing catalog data."""
import sqlite3

import pytest

from keepstack.db import (
    CURRENT_SCHEMA_VERSION,
    SCHEMA,
    apply_migrations,
    schema_version,
)


def _v0_database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO assets "
        "(uuid, sha256, filename, media_type, status, storage_key, created_at, updated_at) "
        "VALUES ('asset-1', 'sha-1', 'harbor.jpg', 'image', 'active', 'blob-key', "
        "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
    )
    conn.execute("INSERT INTO tags (name) VALUES ('approved')")
    conn.execute("INSERT INTO asset_tags (asset_id, tag_id, source) VALUES (1, 1, 'manual')")
    conn.commit()
    return conn


def test_v0_database_upgrades_without_losing_data():
    conn = _v0_database()
    assert schema_version(conn) == 0

    assert apply_migrations(conn) == CURRENT_SCHEMA_VERSION
    assert schema_version(conn) == CURRENT_SCHEMA_VERSION

    row = conn.execute(
        "SELECT uuid, filename, status FROM assets WHERE uuid = 'asset-1'"
    ).fetchone()
    assert row == ("asset-1", "harbor.jpg", "active")

    indexes = {r[1] for r in conn.execute("PRAGMA index_list('assets')")}
    assert "idx_assets_status_created_id" in indexes
    assert "idx_assets_status_name_id" in indexes
    assert "idx_assets_status_size_id" in indexes
    tag_indexes = {r[1] for r in conn.execute("PRAGMA index_list('asset_tags')")}
    assert "idx_asset_tags_tag_asset" in tag_indexes


def test_migrations_are_idempotent():
    conn = _v0_database()
    apply_migrations(conn)
    apply_migrations(conn)
    assert schema_version(conn) == CURRENT_SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 1


def test_newer_schema_is_rejected():
    conn = _v0_database()
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
        (str(CURRENT_SCHEMA_VERSION + 1),),
    )
    conn.commit()
    with pytest.raises(RuntimeError, match="newer than this build"):
        apply_migrations(conn)
