"""Deterministic catalog fixtures shared by integration and browser tests."""
from __future__ import annotations

import io
import random
import uuid
import zlib
from typing import Iterable, Optional

from PIL import Image

from keepstack import ingest
from keepstack.db import get_conn, init_db


def _image_bytes(seed: str) -> bytes:
    rng = random.Random(zlib.crc32(seed.encode("utf-8")))
    color = (rng.randrange(1, 256), rng.randrange(1, 256), rng.randrange(1, 256))
    accent = (rng.randrange(0, 256), rng.randrange(0, 256), rng.randrange(0, 256))
    image = Image.new("RGB", (40, 30), color)
    for x in range(8):
        for y in range(6):
            image.putpixel((x + len(seed) % 30, y + (x * 3) % 22), accent)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def add_tags(asset_id: int, tags: Iterable[str], *, source: str = "manual") -> None:
    conn = get_conn()
    for raw in tags:
        name = str(raw).strip().lower()
        if not name:
            continue
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
        tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO asset_tags (asset_id, tag_id, source) VALUES (?,?,?)",
            (asset_id, tag_id, source),
        )
    conn.commit()


def create_collection(name: str, description: str = "") -> int:
    conn = get_conn()
    collection_uuid = str(uuid.uuid4())
    cur = conn.execute(
        "INSERT INTO collections (uuid, name, description, kind, created_at) "
        "VALUES (?,?,?,'folder',?)",
        (collection_uuid, name, description, "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    return int(cur.lastrowid)


def create_asset(
    namespace: str,
    slug: str,
    *,
    title: Optional[str] = None,
    tags: Iterable[str] = (),
    collection_id: Optional[int] = None,
    created_at: Optional[str] = None,
    status: str = "active",
    description: Optional[str] = None,
    alt_text: Optional[str] = None,
) -> dict:
    filename = f"{namespace}-{slug}.jpg"
    asset = ingest.ingest_stream(
        io.BytesIO(_image_bytes(f"{namespace}:{slug}")),
        filename,
        title=title or f"{namespace} {slug}",
        run_ai=False,
    )
    conn = get_conn()
    updates = []
    values: list = []
    if created_at is not None:
        updates.extend(["created_at = ?", "updated_at = ?"])
        values.extend([created_at, created_at])
    if status != "active":
        updates.append("status = ?")
        values.append(status)
    if description is not None:
        updates.append("description = ?")
        values.append(description)
    if alt_text is not None:
        updates.append("alt_text = ?")
        values.append(alt_text)
    if updates:
        values.append(asset["id"])
        conn.execute(f"UPDATE assets SET {', '.join(updates)} WHERE id = ?", values)
    if collection_id is not None:
        conn.execute(
            "INSERT OR IGNORE INTO collection_assets (collection_id, asset_id, added_at) "
            "VALUES (?,?,?)",
            (collection_id, asset["id"], "2026-01-01T00:00:00+00:00"),
        )
    conn.commit()
    add_tags(asset["id"], tags)
    ingest.reindex_fts(asset["id"])
    return dict(conn.execute("SELECT * FROM assets WHERE id = ?", (asset["id"],)).fetchone())


def seed_e2e_catalog() -> None:
    """Build the deterministic dataset used by Playwright."""
    init_db()
    harbor_collection = create_collection("Harbor Campaign", "Browser tests")

    for index in range(30):
        tags = []
        if index < 18:
            tags.append("approved")
        if index < 15:
            tags.append("campaign")
        if index % 9 == 3:
            tags.append("internal")
        description = "Harbor catalog asset"
        alt_text = "Harbor imagery"
        if index == 0:
            description = '中文说明, 含逗号, "双引号"\n以及换行'
            alt_text = "港口归档图片"
        if index == 5:
            description = "=SUM(A1:A2)"
        create_asset(
            "e2e",
            f"harbor-{index:02d}",
            title=f"Harbor campaign asset {index:02d}",
            tags=tags,
            collection_id=harbor_collection if index < 20 else None,
            created_at="2026-01-02T03:04:05+00:00",
            description=description,
            alt_text=alt_text,
        )

    for index in range(3):
        create_asset(
            "e2e",
            f"mountain-{index:02d}",
            title=f"Mountain archive asset {index:02d}",
            tags=["archive"],
            created_at=f"2025-12-0{index + 1}T00:00:00+00:00",
        )


if __name__ == "__main__":
    seed_e2e_catalog()
