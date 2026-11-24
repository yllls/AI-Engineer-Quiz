"""Keepstack HTTP application (FastAPI).

Routes are grouped by concern: auth, assets, collections, tags, search,
sharing, administration, and open-standard endpoints (OAI-PMH, IIIF). The
single-page web client is served from the ``web/`` directory at the root.
"""
from __future__ import annotations

import io
import ipaddress
import json
import sqlite3
from pathlib import Path
from typing import Literal, Optional

from fastapi import (Body, Depends, FastAPI, File, Form, HTTPException, Query,
                     Request, UploadFile)
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from . import __version__, ingest, search, standards, storage, thumbnails
from .audit import log, now_iso
from .auth import (ROLES, authenticate, hash_password, login_locked, make_token,
                   note_login_failure, note_login_success, role_at_least,
                   verify_password, verify_token)
from .config import config
from .db import get_conn, init_db, transaction
from .exports import (CsvExporter, ExportTooLargeError, ExportValidationError,
                      export_filename, resolve_columns)
from .filters import (MAX_QUERY_LENGTH, FilterSpec, FilterValidationError)
from .pagination import InvalidCursorError
from .query import QueryError, resolve_sort

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="Keepstack DAM", version="0.1.0")


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
@app.on_event("startup")
def _startup() -> None:
    init_db()
    conn = get_conn()
    has_users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    if not has_users:
        conn.execute(
            "INSERT INTO users (username, email, full_name, password_hash, role, is_active, created_at) "
            "VALUES (?,?,?,?,?,1,?)",
            (config.admin_user, config.admin_email, "Administrator",
             hash_password(config.admin_password), "admin", now_iso()),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Auth dependencies
# ---------------------------------------------------------------------------
def optional_user(request: Request) -> Optional[dict]:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    payload = verify_token(auth[7:])
    if not payload:
        return None
    row = get_conn().execute(
        "SELECT * FROM users WHERE id = ? AND is_active = 1", (payload["sub"],)
    ).fetchone()
    if not row:
        return None
    if int(payload.get("ver", 0)) != int(row["token_version"]):
        return None
    return dict(row)


def require_user(request: Request) -> dict:
    user = optional_user(request)
    if not user:
        raise HTTPException(401, "Authentication required")
    return user


def require_role(minimum: str):
    def dep(request: Request) -> dict:
        user = require_user(request)
        if not role_at_least(user["role"], minimum):
            raise HTTPException(403, f"Requires {minimum} role")
        return user
    return dep


def _trusted_proxy_nets():
    nets = []
    for spec in config.trusted_proxies:
        try:
            nets.append(ipaddress.ip_network(spec, strict=False))
        except ValueError:
            continue
    return nets


def _ip_in(ip: str, nets) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in nets)


def client_ip(request: Request) -> str:
    """Resolve the client IP, trusting X-Forwarded-For only from a configured
    proxy. Without trusted proxies the direct peer address is authoritative, so
    a directly-connected client cannot spoof its IP to dodge rate limiting."""
    peer = request.client.host if request.client else ""
    nets = _trusted_proxy_nets()
    if nets and _ip_in(peer, nets):
        fwd = request.headers.get("X-Forwarded-For", "")
        for hop in reversed([h.strip() for h in fwd.split(",") if h.strip()]):
            if not _ip_in(hop, nets):
                return hop
    return peer


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.post("/api/auth/login")
def login(request: Request, body: dict = Body(...)):
    username = body.get("username", "")
    ip = client_ip(request)
    remaining = login_locked(username, ip)
    if remaining > 0:
        raise HTTPException(
            429, "Too many failed attempts, try again later",
            headers={"Retry-After": str(int(remaining) + 1)},
        )
    user = authenticate(username, body.get("password", ""))
    if not user:
        if note_login_failure(username, ip):
            log("login.lockout", detail=username, ip=ip)
        log("login.fail", detail=username, ip=ip)
        raise HTTPException(401, "Invalid credentials")
    note_login_success(username, ip)
    get_conn().execute("UPDATE users SET last_login = ? WHERE id = ?", (now_iso(), user["id"]))
    get_conn().commit()
    log("login.success", user=user, ip=ip)
    return {
        "token": make_token(user["id"], user["role"], user.get("token_version", 0)),
        "user": _user_public(user),
    }


@app.get("/api/auth/me")
def me(user: dict = Depends(require_user)):
    return _user_public(user)


def _user_public(u: dict) -> dict:
    return {"id": u["id"], "username": u["username"], "email": u["email"],
            "full_name": u["full_name"], "role": u["role"]}


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------
@app.post("/api/assets")
async def upload_assets(
    request: Request,
    files: list[UploadFile] = File(...),
    run_ai: bool = Form(True),
    collection_id: Optional[int] = Form(None, ge=1),
    user: dict = Depends(require_role("contributor")),
):
    target_collection = None
    if collection_id is not None:
        row = get_conn().execute(
            "SELECT id, name FROM collections WHERE id = ?", (collection_id,)
        ).fetchone()
        if not row:
            raise HTTPException(
                422,
                detail={
                    "code": "collection_not_found",
                    "message": "The selected collection does not exist",
                },
            )
        target_collection = dict(row)

    results = []
    added_count = 0
    for f in files:
        data = await f.read()
        asset = ingest.ingest_stream(io.BytesIO(data), f.filename or "upload.bin",
                                     user=user, run_ai=run_ai)
        if not asset.get("duplicate"):
            log("asset.create", user=user, target_type="asset", target_id=asset["uuid"],
                detail=asset["filename"], ip=client_ip(request))

        collection_added = False
        collection_error = None
        if target_collection is not None:
            try:
                with transaction() as conn:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO collection_assets "
                        "(collection_id, asset_id, added_at) VALUES (?,?,?)",
                        (target_collection["id"], asset["id"], now_iso()),
                    )
                collection_added = bool(cur.rowcount)
                if collection_added:
                    added_count += 1
            except sqlite3.IntegrityError:
                # The target can disappear between validation and association.
                # The upload itself stays successful; report only the link error.
                collection_error = "Collection no longer exists"
            except Exception:
                collection_error = "Could not add the asset to the collection"

        results.append({
            "uuid": asset["uuid"],
            "title": asset["title"],
            "duplicate": asset.get("duplicate", False),
            "media_type": asset["media_type"],
            "collection_id": target_collection["id"] if target_collection else None,
            "collection_added": collection_added,
            "collection_error": collection_error,
        })

    if target_collection is not None and added_count:
        log("collection.add", user=user, target_type="collection",
            target_id=target_collection["id"],
            detail=f"{added_count} uploaded assets", ip=client_ip(request))
    return {"created": results}


@app.get("/api/assets")
def list_assets(
    q: str = "",
    media_type: Optional[str] = None,
    tag: Optional[str] = None,
    tags: list[str] = Query(default=[]),
    exclude_tags: list[str] = Query(default=[]),
    collection_id: Optional[int] = Query(None, ge=1),
    status: str = "active",
    sort: Literal["newest", "oldest", "name", "largest"] = "newest",
    mode: Literal["keyword", "semantic"] = "keyword",
    limit: int = Query(60, ge=1, le=200),
    offset: Optional[int] = Query(None, ge=0),
    cursor: Optional[str] = None,
    user: dict = Depends(require_user),
):
    normalized_status = (status or "active").strip().lower() or "active"
    uses_new_filters = bool(tags or exclude_tags) or cursor is not None
    if cursor is not None and offset is not None:
        raise HTTPException(
            400,
            detail={
                "code": "invalid_pagination",
                "message": "Use either cursor or offset, not both",
            },
        )
    if mode == "semantic" and (tags or exclude_tags or cursor is not None):
        raise HTTPException(
            400,
            detail={
                "code": "unsupported_semantic_filters",
                "message": "Semantic search does not support tag filters or cursors",
            },
        )
    if normalized_status != "active":
        if uses_new_filters:
            raise HTTPException(
                400,
                detail={
                    "code": "active_only",
                    "message": "Combined filters, cursor pagination and export require active assets",
                },
            )
        # Preserve the pre-keyset API's status queries by keeping them on the
        # legacy offset path even when the old client omitted offset.
        if offset is None:
            offset = 0

    try:
        return search.search(
            q=q,
            media_type=media_type,
            tag=tag,
            tags=tags,
            exclude_tags=exclude_tags,
            collection_id=collection_id,
            status=normalized_status,
            sort=sort,
            mode=mode,
            limit=limit,
            offset=offset,
            cursor=cursor,
        )
    except InvalidCursorError as exc:
        raise HTTPException(
            400,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except search.PaginationConflictError as exc:
        raise HTTPException(
            400,
            detail={"code": "invalid_pagination", "message": str(exc)},
        ) from exc
    except search.UnsupportedSearchModeError as exc:
        raise HTTPException(
            400,
            detail={"code": "unsupported_semantic_filters", "message": str(exc)},
        ) from exc
    except (FilterValidationError, QueryError) as exc:
        raise HTTPException(422, str(exc)) from exc


def _get_asset_or_404(uuid: str) -> dict:
    row = get_conn().execute("SELECT * FROM assets WHERE uuid = ?", (uuid,)).fetchone()
    if not row:
        raise HTTPException(404, "Asset not found")
    return dict(row)


@app.get("/api/assets/{uuid}")
def asset_detail(uuid: str, user: dict = Depends(require_user)):
    a = _get_asset_or_404(uuid)
    conn = get_conn()
    tags = [dict(r) for r in conn.execute(
        "SELECT t.name, at.source, at.confidence FROM tags t "
        "JOIN asset_tags at ON at.tag_id = t.id WHERE at.asset_id = ? ORDER BY t.name",
        (a["id"],))]
    versions = [dict(r) for r in conn.execute(
        "SELECT version_no, sha256, size, note, created_at FROM asset_versions "
        "WHERE asset_id = ? ORDER BY version_no DESC", (a["id"],))]
    collections = [dict(r) for r in conn.execute(
        "SELECT c.id, c.name FROM collections c JOIN collection_assets ca ON ca.collection_id = c.id "
        "WHERE ca.asset_id = ?", (a["id"],))]
    custom = [dict(r) for r in conn.execute(
        "SELECT cf.key, cf.label, acv.value FROM asset_custom_values acv "
        "JOIN custom_fields cf ON cf.id = acv.field_id WHERE acv.asset_id = ?", (a["id"],))]
    a.pop("embedding", None)
    a["dublin_core"] = standards.dublin_core(a)
    return {"asset": a, "tags": tags, "versions": versions,
            "collections": collections, "custom": custom}


@app.patch("/api/assets/{uuid}")
def update_asset(uuid: str, request: Request, body: dict = Body(...),
                 user: dict = Depends(require_role("contributor"))):
    a = _get_asset_or_404(uuid)
    editable = {"title", "description", "alt_text", "license", "rights_statement",
                "credit", "usage_expiry", "retention_until", "dc_creator",
                "dc_subject", "dc_publisher", "dc_date", "status"}
    sets, vals = [], []
    for k, v in body.items():
        if k in editable:
            sets.append(f"{k} = ?")
            vals.append(v)
    if sets:
        sets.append("updated_at = ?")
        vals.extend([now_iso(), a["id"]])
        with transaction() as conn:
            conn.execute(f"UPDATE assets SET {', '.join(sets)} WHERE id = ?", vals)
        ingest.reindex_fts(a["id"])
        ingest.reindex_embedding(a["id"])
        log("asset.update", user=user, target_type="asset", target_id=uuid,
            detail=",".join(body.keys()), ip=client_ip(request))
    return asset_detail(uuid, user)


@app.delete("/api/assets/{uuid}")
def delete_asset(uuid: str, request: Request, user: dict = Depends(require_role("editor"))):
    a = _get_asset_or_404(uuid)
    with transaction() as conn:
        conn.execute("UPDATE assets SET status = 'deleted', updated_at = ? WHERE id = ?",
                     (now_iso(), a["id"]))
    log("asset.delete", user=user, target_type="asset", target_id=uuid, ip=client_ip(request))
    return {"ok": True}


@app.get("/api/assets/{uuid}/file")
def asset_file(uuid: str, user: dict = Depends(require_user)):
    a = _get_asset_or_404(uuid)
    path = storage.blob_path(a["storage_key"])
    if not path.exists():
        raise HTTPException(410, "Blob missing")
    return FileResponse(path, media_type=a["mime_type"], filename=a["filename"])


@app.get("/api/assets/{uuid}/thumb")
def asset_thumb(uuid: str):
    a = _get_asset_or_404(uuid)
    key = a["thumb_key"] or thumbnails.ensure_thumbnail(
        a["sha256"], a["storage_key"], a["media_type"], a["ext"])
    path = storage.thumb_path(key)
    if not path.exists():
        thumbnails.ensure_thumbnail(a["sha256"], a["storage_key"], a["media_type"], a["ext"])
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/assets/{uuid}/preview")
def asset_preview(uuid: str, size: int = Query(1600, le=4000)):
    a = _get_asset_or_404(uuid)
    data = thumbnails.rendition(a["sha256"], a["storage_key"], a["media_type"], a["ext"], size)
    if data is None:
        return asset_thumb(uuid)
    return Response(content=data, media_type="image/jpeg")


@app.get("/api/assets/{uuid}/similar")
def asset_similar(uuid: str, user: dict = Depends(require_user)):
    a = _get_asset_or_404(uuid)
    return {"items": search.more_like(a["id"])}


@app.post("/api/assets/{uuid}/tags")
def add_tag(uuid: str, request: Request, body: dict = Body(...),
            user: dict = Depends(require_role("contributor"))):
    a = _get_asset_or_404(uuid)
    name = (body.get("name") or "").strip().lower()
    if not name:
        raise HTTPException(400, "Tag name required")
    with transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
        tid = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()["id"]
        conn.execute("INSERT OR IGNORE INTO asset_tags (asset_id, tag_id, source) VALUES (?,?,?)",
                     (a["id"], tid, "manual"))
    ingest.reindex_fts(a["id"])
    ingest.reindex_embedding(a["id"])
    log("tag.add", user=user, target_type="asset", target_id=uuid, detail=name)
    return {"ok": True}


@app.delete("/api/assets/{uuid}/tags/{name}")
def remove_tag(uuid: str, name: str, user: dict = Depends(require_role("contributor"))):
    a = _get_asset_or_404(uuid)
    with transaction() as conn:
        conn.execute(
            "DELETE FROM asset_tags WHERE asset_id = ? AND tag_id = "
            "(SELECT id FROM tags WHERE name = ?)", (a["id"], name.lower()))
    ingest.reindex_fts(a["id"])
    return {"ok": True}


@app.post("/api/assets/{uuid}/versions")
async def add_version(uuid: str, request: Request, file: UploadFile = File(...),
                      note: str = Form(""), user: dict = Depends(require_role("editor"))):
    a = _get_asset_or_404(uuid)
    data = await file.read()
    ext = (file.filename or "").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else a["ext"]
    sha, key, size = storage.store_bytes(data, ext)
    conn = get_conn()
    last = conn.execute("SELECT COALESCE(MAX(version_no),0) m FROM asset_versions WHERE asset_id = ?",
                        (a["id"],)).fetchone()["m"]
    with transaction() as conn:
        # snapshot the current version, then promote the new bytes to current
        conn.execute(
            "INSERT INTO asset_versions (asset_id, version_no, sha256, storage_key, size, note, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (a["id"], last + 1, a["sha256"], a["storage_key"], a["size"],
             "previous current", user["id"], now_iso()))
        conn.execute(
            "INSERT INTO asset_versions (asset_id, version_no, sha256, storage_key, size, note, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (a["id"], last + 2, sha, key, size, note or "new version", user["id"], now_iso()))
        conn.execute("UPDATE assets SET sha256 = ?, storage_key = ?, size = ?, updated_at = ? WHERE id = ?",
                     (sha, key, size, now_iso(), a["id"]))
    thumbnails.ensure_thumbnail(sha, key, a["media_type"], ext)
    log("asset.version", user=user, target_type="asset", target_id=uuid, detail=note)
    return {"ok": True, "version": last + 2}


@app.get("/api/assets/{uuid}/metadata")
def asset_metadata(uuid: str, user: dict = Depends(require_user)):
    a = _get_asset_or_404(uuid)
    row = get_conn().execute("SELECT * FROM asset_metadata WHERE asset_id = ?", (a["id"],)).fetchone()
    if not row:
        return {"exif": {}, "iptc": {}, "xmp": {}}
    return {"exif": json.loads(row["exif_json"] or "{}"),
            "iptc": json.loads(row["iptc_json"] or "{}"),
            "xmp": json.loads(row["xmp_json"] or "{}")}


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------
@app.get("/api/tags")
def list_tags(
    q: str = "",
    limit: Optional[int] = Query(None, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_user),
):
    term = q.strip()
    if len(term) > MAX_QUERY_LENGTH:
        raise HTTPException(
            422, f"Tag search is limited to {MAX_QUERY_LENGTH} characters"
        )
    where = ""
    params: list = []
    if term:
        escaped = (
            term.lower()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        where = "WHERE LOWER(t.name) LIKE ? ESCAPE '\\'"
        params.append(f"%{escaped}%")

    conn = get_conn()
    total = conn.execute(f"SELECT COUNT(*) c FROM tags t {where}", params).fetchone()["c"]
    sql = (
        "SELECT t.name, COUNT(at.asset_id) c FROM tags t "
        "LEFT JOIN asset_tags at ON at.tag_id = t.id "
        f"{where} GROUP BY t.name ORDER BY c DESC, t.name"
    )
    query_params = list(params)
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        query_params.extend([limit, offset])
    rows = conn.execute(sql, query_params).fetchall()
    return {"tags": [dict(r) for r in rows], "total": total}


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------
@app.get("/api/exports/assets.csv")
def export_assets_csv(
    request: Request,
    q: str = "",
    media_type: Optional[str] = None,
    tag: Optional[str] = None,
    tags: list[str] = Query(default=[]),
    exclude_tags: list[str] = Query(default=[]),
    collection_id: Optional[int] = Query(None, ge=1),
    status: str = "active",
    sort: Literal["newest", "oldest", "name", "largest"] = "newest",
    columns: list[str] = Query(default=[]),
    sanitize_formulas: bool = True,
    user: dict = Depends(require_user),
):
    normalized_status = (status or "active").strip().lower() or "active"
    if normalized_status != "active":
        raise HTTPException(
            400,
            detail={
                "code": "active_only",
                "message": "CSV export is limited to active assets",
            },
        )
    try:
        spec = FilterSpec.build(
            q=q,
            media_type=media_type,
            collection_id=collection_id,
            tag=tag,
            tags=tags,
            exclude_tags=exclude_tags,
            status="active",
        )
        sort_spec = resolve_sort(sort)
        selected_columns = resolve_columns(columns)
    except (FilterValidationError, ExportValidationError, QueryError) as exc:
        raise HTTPException(422, str(exc)) from exc

    exporter = CsvExporter(
        spec=spec,
        sort=sort_spec,
        columns=selected_columns,
        sanitize_formulas=sanitize_formulas,
    )
    try:
        exporter.open()
    except ExportTooLargeError as exc:
        raise HTTPException(
            413,
            detail={
                "code": "export_too_large",
                "matched": exc.matched,
                "limit": exc.limit,
                "message": str(exc),
            },
        ) from exc

    try:
        log(
            "asset.export",
            user=user,
            target_type="export",
            detail=json.dumps(
                {
                    "matched": exporter.total,
                    "sort": sort_spec.name,
                    "columns": list(selected_columns),
                    "sanitize_formulas": sanitize_formulas,
                    **spec.summary(),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            ip=client_ip(request),
        )
    except Exception:
        exporter.close()
        raise

    headers = {
        "Content-Disposition": f'attachment; filename="{export_filename()}"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    return StreamingResponse(
        exporter.iter_chunks(),
        media_type="text/csv; charset=utf-8",
        headers=headers,
        background=BackgroundTask(exporter.close),
    )


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------
@app.get("/api/collections")
def list_collections(user: dict = Depends(require_user)):
    rows = get_conn().execute(
        "SELECT c.*, (SELECT COUNT(*) FROM collection_assets ca WHERE ca.collection_id = c.id) AS asset_count "
        "FROM collections c ORDER BY c.name").fetchall()
    return {"collections": [dict(r) for r in rows]}


@app.post("/api/collections")
def create_collection(request: Request, body: dict = Body(...),
                      user: dict = Depends(require_role("contributor"))):
    import uuid as uuidlib
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Name required")
    cid = str(uuidlib.uuid4())
    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO collections (uuid, name, description, parent_id, kind, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, name, body.get("description"), body.get("parent_id"),
             body.get("kind", "folder"), user["id"], now_iso()))
    log("collection.create", user=user, target_type="collection", target_id=cid, detail=name)
    return {"id": cur.lastrowid, "uuid": cid, "name": name}


@app.delete("/api/collections/{cid}")
def delete_collection(cid: int, user: dict = Depends(require_role("editor"))):
    with transaction() as conn:
        conn.execute("DELETE FROM collections WHERE id = ?", (cid,))
    log("collection.delete", user=user, target_type="collection", target_id=cid)
    return {"ok": True}


@app.post("/api/collections/{cid}/assets")
def add_to_collection(cid: int, body: dict = Body(...),
                      user: dict = Depends(require_role("contributor"))):
    uuids = body.get("uuids", [])
    conn = get_conn()
    if not conn.execute("SELECT 1 FROM collections WHERE id = ?", (cid,)).fetchone():
        raise HTTPException(404, "Collection not found")
    added = 0
    with transaction() as conn:
        for u in uuids:
            row = conn.execute("SELECT id FROM assets WHERE uuid = ?", (u,)).fetchone()
            if row:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO collection_assets (collection_id, asset_id, added_at) VALUES (?,?,?)",
                    (cid, row["id"], now_iso()))
                if cur.rowcount:
                    added += 1
    log("collection.add", user=user, target_type="collection", target_id=cid, detail=f"{added} assets")
    return {"ok": True, "added": added}


@app.delete("/api/collections/{cid}/assets/{uuid}")
def remove_from_collection(cid: int, uuid: str, user: dict = Depends(require_role("contributor"))):
    with transaction() as conn:
        conn.execute(
            "DELETE FROM collection_assets WHERE collection_id = ? AND asset_id = "
            "(SELECT id FROM assets WHERE uuid = ?)", (cid, uuid))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Sharing (public, tokenised, expiring links)
# ---------------------------------------------------------------------------
@app.post("/api/shares")
def create_share(request: Request, body: dict = Body(...),
                 user: dict = Depends(require_role("contributor"))):
    import secrets
    token = secrets.token_urlsafe(16)
    with transaction() as conn:
        conn.execute(
            "INSERT INTO share_links (token, target_type, target_id, permission, expires_at, "
            "max_downloads, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (token, body.get("target_type", "asset"), body["target_id"],
             body.get("permission", "view"), body.get("expires_at"),
             body.get("max_downloads"), user["id"], now_iso()))
    log("share.create", user=user, target_type=body.get("target_type"), target_id=body["target_id"])
    return {"token": token, "url": f"{config.base_url}/s/{token}"}


@app.get("/api/shares")
def list_shares(user: dict = Depends(require_role("contributor"))):
    rows = get_conn().execute(
        "SELECT id, token, target_type, target_id, permission, expires_at, "
        "download_count, max_downloads, revoked, created_at FROM share_links "
        "WHERE revoked = 0 ORDER BY created_at DESC").fetchall()
    return {"shares": [dict(r) for r in rows]}


@app.delete("/api/shares/{sid}")
def revoke_share(sid: int, user: dict = Depends(require_role("contributor"))):
    with transaction() as conn:
        conn.execute("UPDATE share_links SET revoked = 1 WHERE id = ?", (sid,))
    log("share.revoke", user=user, target_type="share", target_id=sid)
    return {"ok": True}


def _valid_share(token: str) -> dict:
    row = get_conn().execute("SELECT * FROM share_links WHERE token = ? AND revoked = 0",
                             (token,)).fetchone()
    if not row:
        raise HTTPException(404, "Invalid or revoked link")
    s = dict(row)
    if s["expires_at"] and s["expires_at"] < now_iso():
        raise HTTPException(410, "Link expired")
    if s["max_downloads"] and s["download_count"] >= s["max_downloads"]:
        raise HTTPException(410, "Download limit reached")
    return s


@app.get("/s/{token}", response_class=HTMLResponse)
def public_share(token: str):
    s = _valid_share(token)
    a = get_conn().execute("SELECT * FROM assets WHERE id = ?", (s["target_id"],)).fetchone()
    if not a:
        raise HTTPException(404, "Asset unavailable")
    a = dict(a)
    can_dl = s["permission"] == "download"
    dl = (f'<a class="btn" href="/s/{token}/file">Download original</a>' if can_dl else "")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{a['title']} - Shared via Keepstack</title>
<style>body{{margin:0;font-family:system-ui,sans-serif;background:#f4f6fa;color:#1d2433;
display:flex;flex-direction:column;align-items:center;gap:24px;padding:48px 16px}}
img{{max-width:min(900px,92vw);border-radius:12px;border:1px solid #dde3ec}}
.btn{{background:#4d68d6;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none}}
.muted{{color:#64708a}}</style></head>
<body><h2>{a['title']}</h2>
<img src="/s/{token}/preview" alt="{a.get('alt_text') or a['title']}">
<p class="muted">{a.get('description') or ''}</p>{dl}
<p class="muted">Shared securely via Keepstack</p></body></html>"""


@app.get("/s/{token}/preview")
def public_share_preview(token: str):
    s = _valid_share(token)
    a = dict(get_conn().execute("SELECT * FROM assets WHERE id = ?", (s["target_id"],)).fetchone())
    data = thumbnails.rendition(a["sha256"], a["storage_key"], a["media_type"], a["ext"], 1600)
    if data is None:
        key = a["thumb_key"]
        return FileResponse(storage.thumb_path(key), media_type="image/jpeg")
    return Response(content=data, media_type="image/jpeg")


@app.get("/s/{token}/file")
def public_share_file(token: str, request: Request):
    s = _valid_share(token)
    if s["permission"] != "download":
        raise HTTPException(403, "Download not permitted")
    a = dict(get_conn().execute("SELECT * FROM assets WHERE id = ?", (s["target_id"],)).fetchone())
    with transaction() as conn:
        conn.execute("UPDATE share_links SET download_count = download_count + 1 WHERE id = ?", (s["id"],))
    log("share.download", target_type="asset", target_id=a["uuid"], detail=token, ip=client_ip(request))
    return FileResponse(storage.blob_path(a["storage_key"]), media_type=a["mime_type"],
                        filename=a["filename"])


# ---------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------
@app.get("/api/stats")
def stats(user: dict = Depends(require_user)):
    conn = get_conn()
    by_type = {r["media_type"]: r["c"] for r in conn.execute(
        "SELECT media_type, COUNT(*) c FROM assets WHERE status='active' GROUP BY media_type")}
    total = conn.execute("SELECT COUNT(*) c FROM assets WHERE status='active'").fetchone()["c"]
    size = conn.execute("SELECT COALESCE(SUM(size),0) s FROM assets WHERE status='active'").fetchone()["s"]
    tags = conn.execute("SELECT COUNT(*) c FROM tags").fetchone()["c"]
    cols = conn.execute("SELECT COUNT(*) c FROM collections").fetchone()["c"]
    # storage saved through dedup: bytes that would exist without content-addressing
    dup = conn.execute(
        "SELECT COALESCE(SUM(size),0) s FROM (SELECT sha256, size, COUNT(*) n "
        "FROM assets GROUP BY sha256 HAVING n > 1)").fetchone()["s"]
    return {"total_assets": total, "total_bytes": size, "by_type": by_type,
            "tags": tags, "collections": cols, "dedup_saved_bytes": dup}


CREATE_USER_FIELDS = {"username", "email", "full_name", "role", "password", "is_active"}
UPDATE_USER_FIELDS = {"email", "full_name", "role", "is_active", "password"}


def _user_http_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})


def _unknown_fields_error(body: dict, allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise _user_http_error(
            422,
            "unsupported_user_fields",
            "Unsupported fields: " + ", ".join(unknown),
        )


def _coerce_active(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise _user_http_error(422, "invalid_is_active", "is_active must be a boolean")


def _optional_text(value, field: str):
    if value is None:
        return None
    if not isinstance(value, str):
        raise _user_http_error(422, "invalid_user_field", f"{field} must be a string")
    stripped = value.strip()
    return stripped or None


def _validated_role(value: str) -> str:
    if value not in ROLES:
        raise _user_http_error(422, "invalid_role", f"Role must be one of: {', '.join(ROLES)}")
    return value


def _get_user_row(uid: int) -> dict:
    row = get_conn().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not row:
        raise HTTPException(404, "User not found")
    return dict(row)


def _public_admin_user(row) -> dict:
    data = dict(row)
    return {
        "id": data["id"],
        "username": data["username"],
        "email": data.get("email"),
        "full_name": data.get("full_name"),
        "role": data["role"],
        "is_active": bool(data["is_active"]),
        "created_at": data.get("created_at"),
        "last_login": data.get("last_login"),
    }


@app.get("/api/users")
def list_users(user: dict = Depends(require_role("admin"))):
    rows = get_conn().execute(
        "SELECT id, username, email, full_name, role, is_active, created_at, last_login FROM users "
        "ORDER BY username").fetchall()
    return {"users": [dict(r) for r in rows]}


@app.post("/api/users")
def create_user(request: Request, body: dict = Body(...), user: dict = Depends(require_role("admin"))):
    _unknown_fields_error(body, CREATE_USER_FIELDS)
    username = body.get("username")
    if not isinstance(username, str) or not username.strip():
        raise _user_http_error(422, "invalid_username", "Username is required")
    username = username.strip()
    password = body.get("password")
    if not isinstance(password, str) or not password:
        raise _user_http_error(422, "invalid_password", "Initial password is required")
    role = _validated_role(body.get("role", "viewer"))
    is_active = _coerce_active(body.get("is_active", True))
    email = _optional_text(body.get("email"), "email")
    full_name = _optional_text(body.get("full_name"), "full_name")

    try:
        with transaction() as conn:
            cur = conn.execute(
                "INSERT INTO users "
                "(username, email, full_name, password_hash, role, is_active, created_at, token_version) "
                "VALUES (?,?,?,?,?,?,?,0)",
                (username, email, full_name, hash_password(password), role,
                 1 if is_active else 0, now_iso()))
    except sqlite3.IntegrityError as exc:
        raise _user_http_error(409, "username_exists", "Username already exists") from exc

    log("user.create", user=user, target_type="user", target_id=username,
        detail=f"role={role}, active={int(is_active)}")
    created = _get_user_row(int(cur.lastrowid))
    return {"id": cur.lastrowid, "user": _public_admin_user(created)}


@app.patch("/api/users/{uid}")
def update_user(uid: int, body: dict = Body(...), user: dict = Depends(require_role("admin"))):
    target = _get_user_row(uid)
    if "username" in body:
        raise _user_http_error(422, "username_immutable", "Username cannot be changed")
    _unknown_fields_error(body, UPDATE_USER_FIELDS)

    changes: dict = {}
    changed_fields: list[str] = []
    password_changed = False

    if "email" in body:
        email = _optional_text(body.get("email"), "email")
        if email != target.get("email"):
            changes["email"] = email
            changed_fields.append("email")
    if "full_name" in body:
        full_name = _optional_text(body.get("full_name"), "full_name")
        if full_name != target.get("full_name"):
            changes["full_name"] = full_name
            changed_fields.append("full_name")
    if "role" in body:
        role = _validated_role(body["role"])
        if uid == user["id"] and role != target["role"]:
            raise _user_http_error(409, "cannot_change_own_role", "You cannot change your own role")
        if role != target["role"]:
            changes["role"] = role
            changed_fields.append("role")
    if "is_active" in body:
        active = _coerce_active(body["is_active"])
        if uid == user["id"] and not active:
            raise _user_http_error(409, "cannot_deactivate_self", "You cannot deactivate yourself")
        if bool(target["is_active"]) != active:
            changes["is_active"] = 1 if active else 0
            changed_fields.append("is_active")
    if "password" in body:
        password = body.get("password")
        if password not in (None, ""):
            if not isinstance(password, str):
                raise _user_http_error(422, "invalid_password", "Password must be a string")
            changes["password_hash"] = hash_password(password)
            password_changed = True
            changed_fields.append("password")

    new_role = changes.get("role", target["role"])
    new_active = bool(changes["is_active"]) if "is_active" in changes else bool(target["is_active"])
    if target["role"] == "admin" and target["is_active"] and (new_role != "admin" or not new_active):
        other_admins = get_conn().execute(
            "SELECT COUNT(*) c FROM users WHERE role = 'admin' AND is_active = 1 AND id != ?",
            (uid,),
        ).fetchone()["c"]
        if not other_admins:
            raise _user_http_error(
                409,
                "last_active_admin",
                "At least one active administrator must remain",
            )

    invalidate_tokens = password_changed or (target["is_active"] and not new_active)
    if invalidate_tokens:
        changes["token_version"] = "token_version + 1"
        changed_fields.append("token_version")

    if changes:
        assignments = []
        values = []
        for key, value in changes.items():
            if key == "token_version":
                assignments.append("token_version = token_version + 1")
            else:
                assignments.append(f"{key} = ?")
                values.append(value)
        values.append(uid)
        with transaction() as conn:
            conn.execute(
                f"UPDATE users SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
        log("user.update", user=user, target_type="user", target_id=uid,
            detail=",".join(changed_fields))
        if "is_active" in changes:
            action = "user.activate" if changes["is_active"] else "user.deactivate"
            log(action, user=user, target_type="user", target_id=uid,
                detail=target["username"])

    return {"ok": True, "user": _public_admin_user(_get_user_row(uid))}


@app.delete("/api/users/{uid}")
def delete_user(uid: int, user: dict = Depends(require_role("admin"))):
    target = _get_user_row(uid)
    if uid == user["id"]:
        raise _user_http_error(409, "cannot_delete_self", "You cannot delete yourself")
    if target["is_active"]:
        raise _user_http_error(
            409,
            "deactivate_first",
            "Deactivate the user before deleting them permanently",
        )
    with transaction() as conn:
        # Most of these columns predate foreign keys. Clear them explicitly so
        # a permanent deletion does not leave dangling user ids.
        conn.execute("UPDATE assets SET uploaded_by = NULL WHERE uploaded_by = ?", (uid,))
        conn.execute("UPDATE asset_versions SET created_by = NULL WHERE created_by = ?", (uid,))
        conn.execute("UPDATE collections SET created_by = NULL WHERE created_by = ?", (uid,))
        conn.execute("UPDATE share_links SET created_by = NULL WHERE created_by = ?", (uid,))
        conn.execute("DELETE FROM saved_searches WHERE user_id = ?", (uid,))
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    log("user.delete", user=user, target_type="user", target_id=uid,
        detail=target["username"])
    return {"ok": True}


@app.get("/api/audit")
def audit_log(limit: int = Query(100, le=1000), user: dict = Depends(require_role("admin"))):
    rows = get_conn().execute(
        "SELECT ts, username, action, target_type, target_id, detail, ip FROM audit_log "
        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return {"entries": [dict(r) for r in rows]}


@app.get("/api/custom-fields")
def list_custom_fields(user: dict = Depends(require_user)):
    rows = get_conn().execute("SELECT * FROM custom_fields ORDER BY sort_order, label").fetchall()
    return {"fields": [dict(r) for r in rows]}


@app.post("/api/custom-fields")
def create_custom_field(body: dict = Body(...), user: dict = Depends(require_role("admin"))):
    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO custom_fields (key, label, field_type, options_json, required, sort_order) "
            "VALUES (?,?,?,?,?,?)",
            (body["key"], body["label"], body.get("field_type", "text"),
             json.dumps(body.get("options")) if body.get("options") else None,
             1 if body.get("required") else 0, body.get("sort_order", 0)))
    return {"id": cur.lastrowid}


@app.put("/api/assets/{uuid}/custom/{field_id}")
def set_custom_value(uuid: str, field_id: int, body: dict = Body(...),
                     user: dict = Depends(require_role("contributor"))):
    a = _get_asset_or_404(uuid)
    with transaction() as conn:
        conn.execute(
            "INSERT INTO asset_custom_values (asset_id, field_id, value) VALUES (?,?,?) "
            "ON CONFLICT(asset_id, field_id) DO UPDATE SET value = excluded.value",
            (a["id"], field_id, body.get("value")))
    ingest.reindex_fts(a["id"])
    return {"ok": True}


@app.post("/api/admin/fixity")
def fixity_check(user: dict = Depends(require_role("admin"))):
    """Re-hash every blob and report integrity failures (digital preservation)."""
    conn = get_conn()
    rows = conn.execute("SELECT uuid, sha256, storage_key FROM assets WHERE status != 'deleted'").fetchall()
    failures = []
    for r in rows:
        if not storage.verify_fixity(r["storage_key"], r["sha256"]):
            failures.append(r["uuid"])
    log("admin.fixity", user=user, detail=f"{len(failures)} failures of {len(rows)}")
    return {"checked": len(rows), "failures": failures, "ok": not failures}


# ---------------------------------------------------------------------------
# Open standards: OAI-PMH + IIIF
# ---------------------------------------------------------------------------
@app.get("/oai")
def oai(request: Request):
    body, status = standards.oai_response(dict(request.query_params))
    return Response(content=body, media_type="application/xml", status_code=status)


@app.get("/iiif/3/{uuid}/info.json")
def iiif_info(uuid: str):
    a = _get_asset_or_404(uuid)
    return JSONResponse(standards.iiif_info(a))


@app.get("/iiif/3/{uuid}/manifest.json")
def iiif_manifest(uuid: str):
    a = _get_asset_or_404(uuid)
    if a["media_type"] != "image":
        raise HTTPException(415, "IIIF manifests are available for images only")
    return JSONResponse(standards.iiif_manifest(a))


@app.get("/iiif/3/{uuid}/{region}/{size}/{rotation}/{quality}.{fmt}")
def iiif_image(uuid: str, region: str, size: str, rotation: str, quality: str, fmt: str):
    a = _get_asset_or_404(uuid)
    if fmt.lower() not in ("jpg", "jpeg"):
        raise HTTPException(400, "Only JPEG IIIF output is supported")
    try:
        data = thumbnails.iiif_rendition(
            a["sha256"], a["storage_key"], a["media_type"], region, size, rotation
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if data is None:
        raise HTTPException(415, "IIIF available for images only")
    return Response(content=data, media_type="image/jpeg")


@app.get("/api/version")
def version():
    """Report the running version (unauthenticated, for monitoring)."""
    return {"name": "keepstack", "version": __version__}


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


# ---------------------------------------------------------------------------
# Static SPA (registered last so API routes win)
# ---------------------------------------------------------------------------
if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
