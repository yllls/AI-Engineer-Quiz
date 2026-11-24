"""Search and discovery.

Three complementary modes over one catalog:

  * keyword  - FTS5 full-text across title, description, tags, metadata
  * filter   - faceted structured filters (media type, tag, status)
  * semantic - embedding cosine ranking ("natural language" / more-like-this)

The keyword/filter path delegates to the shared filter and query modules so the
list, counts, cursors, and CSV export cannot drift apart. Semantic search is a
legacy compatibility mode; the web client no longer exposes it.
"""
from __future__ import annotations

from typing import Optional, Sequence

from . import ai
from .db import get_conn, read_transaction
from .filters import FilterSpec, fts_query, fts_terms
from .pagination import decode_cursor
from .query import (
    QueryError,
    count_matches,
    fetch_cursor_page,
    fetch_offset_page,
    resolve_sort,
)


class PaginationConflictError(ValueError):
    """Both cursor and offset were supplied."""


class UnsupportedSearchModeError(ValueError):
    """Semantic mode was combined with new-only filters or pagination."""


def _fts_query(q: str) -> str:
    """Compatibility helper preserving the original safe FTS prefix query."""
    return fts_query(fts_terms(q))


def _validate_limit(limit: int) -> None:
    if limit < 1:
        raise QueryError("limit must be positive")


def search(
    *,
    q: str = "",
    media_type: Optional[str] = None,
    tag: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
    exclude_tags: Optional[Sequence[str]] = None,
    collection_id: Optional[int] = None,
    status: str = "active",
    sort: str = "newest",
    mode: str = "keyword",
    limit: int = 60,
    offset: Optional[int] = None,
    cursor: Optional[str] = None,
) -> dict:
    _validate_limit(limit)

    if mode == "semantic" and q.strip():
        # Preserve the old semantic API, but reject combinations whose
        # semantics this legacy ranking path cannot honor.
        if tags or exclude_tags or cursor is not None:
            raise UnsupportedSearchModeError(
                "Semantic search does not support tag filters or cursors"
            )
        effective_offset = 0 if offset is None else offset
        if effective_offset < 0:
            raise QueryError("offset must be non-negative")
        result = _semantic_search(q, media_type, status, limit, effective_offset)
        result["pagination"] = {
            "page": effective_offset // limit + 1,
            "limit": limit,
            "prev_cursor": None,
            "next_cursor": None,
        }
        return result

    if tags or exclude_tags or cursor is not None:
        if mode == "semantic":
            raise UnsupportedSearchModeError(
                "Semantic search does not support tag filters or cursors"
            )

    spec = FilterSpec.build(
        q=q,
        media_type=media_type,
        collection_id=collection_id,
        tag=tag,
        tags=tags,
        exclude_tags=exclude_tags,
        status=status,
    )
    sort_spec = resolve_sort(sort)

    if cursor is not None and offset is not None:
        raise PaginationConflictError("Use either cursor or offset, not both")
    if offset is not None and offset < 0:
        raise QueryError("offset must be non-negative")

    parsed_cursor = None
    if cursor is not None:
        parsed_cursor = decode_cursor(
            cursor,
            expected_sort=sort_spec.name,
            expected_limit=limit,
            filter_fp=spec.fingerprint,
        )

    with read_transaction() as conn:
        total = count_matches(conn, spec)
        if offset is not None:
            page = fetch_offset_page(conn, spec, sort_spec, limit, offset)
        else:
            page = fetch_cursor_page(conn, spec, sort_spec, limit, parsed_cursor)
        facets = _facets(spec.status, conn=conn)

    return {
        "total": total,
        "items": [_card(dict(row)) for row in page.rows],
        "facets": facets,
        "mode": mode,
        "pagination": {
            "page": page.page,
            "limit": page.limit,
            "prev_cursor": page.prev_cursor,
            "next_cursor": page.next_cursor,
        },
    }


def _semantic_search(q, media_type, status, limit, offset) -> dict:
    conn = get_conn()
    qvec = ai.embed(q)
    where = ["status = ?", "embedding IS NOT NULL"]
    params: list = [status]
    if media_type:
        where.append("media_type = ?")
        params.append(media_type)
    rows = conn.execute(
        f"SELECT * FROM assets WHERE {' AND '.join(where)}", params
    ).fetchall()
    scored = []
    for r in rows:
        score = ai.cosine(qvec, ai.unpack_embedding(r["embedding"]))
        scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    page = scored[offset:offset + limit]
    items = []
    for score, r in page:
        card = _card(dict(r))
        card["score"] = round(score, 4)
        items.append(card)
    return {
        "total": len(scored),
        "items": items,
        "facets": _facets(status),
        "mode": "semantic",
    }


def more_like(asset_id: int, limit: int = 12) -> list[dict]:
    conn = get_conn()
    base = conn.execute("SELECT embedding FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not base or not base["embedding"]:
        return []
    bvec = ai.unpack_embedding(base["embedding"])
    rows = conn.execute(
        "SELECT * FROM assets WHERE id != ? AND status = 'active' AND embedding IS NOT NULL",
        (asset_id,),
    ).fetchall()
    scored = [(ai.cosine(bvec, ai.unpack_embedding(r["embedding"])), r) for r in rows]
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for score, r in scored[:limit]:
        card = _card(dict(r))
        card["score"] = round(score, 4)
        out.append(card)
    return out


def _facets(status: str, conn=None) -> dict:
    conn = conn or get_conn()
    media = conn.execute(
        "SELECT media_type, COUNT(*) c FROM assets WHERE status = ? GROUP BY media_type ORDER BY c DESC",
        (status,),
    ).fetchall()
    tags = conn.execute(
        """SELECT t.name, COUNT(*) c FROM tags t
           JOIN asset_tags at ON at.tag_id = t.id
           JOIN assets a ON a.id = at.asset_id AND a.status = ?
           GROUP BY t.name ORDER BY c DESC LIMIT 25""",
        (status,),
    ).fetchall()
    return {
        "media_types": [{"value": r["media_type"], "count": r["c"]} for r in media],
        "tags": [{"value": r["name"], "count": r["c"]} for r in tags],
    }


def _card(a: dict) -> dict:
    """Compact asset representation for grid/list views."""
    return {
        "id": a["id"],
        "uuid": a["uuid"],
        "title": a["title"],
        "filename": a["filename"],
        "media_type": a["media_type"],
        "mime_type": a["mime_type"],
        "ext": a["ext"],
        "size": a["size"],
        "width": a["width"],
        "height": a["height"],
        "alt_text": a["alt_text"],
        "status": a["status"],
        "created_at": a["created_at"],
    }
