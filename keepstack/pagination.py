"""Signed keyset pagination cursors.

Cursors are opaque to API clients. The payload carries the sort boundary, the
target page, page size, sort id, navigation direction, and a fingerprint of the
normalized filters. Any mismatch or signature failure is rejected rather than
silently producing a different result set.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass

from .auth import _secret

CURSOR_VERSION = 1
MAX_CURSOR_LENGTH = 4096
VALID_NAV = {"next", "prev"}


class InvalidCursorError(ValueError):
    """A cursor is malformed, tampered with, or used with different filters."""

    code = "invalid_cursor"


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


@dataclass(frozen=True)
class Cursor:
    page: int
    limit: int
    sort: str
    nav: str
    key: str | int
    id: int
    filter_fp: str


def encode_cursor(
    *,
    page: int,
    limit: int,
    sort: str,
    nav: str,
    key: str | int,
    asset_id: int,
    filter_fp: str,
) -> str:
    if nav not in VALID_NAV:
        raise ValueError("cursor navigation must be next or prev")
    if page < 1:
        raise ValueError("cursor page must be positive")
    if nav == "next" and page < 2:
        raise ValueError("next cursor must target page 2 or later")
    payload = {
        "v": CURSOR_VERSION,
        "page": page,
        "limit": limit,
        "sort": sort,
        "nav": nav,
        "key": key,
        "id": asset_id,
        "fp": filter_fp,
    }
    body = _b64encode(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )
    signature = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64encode(signature)}"


def decode_cursor(token: str, *, expected_sort: str, expected_limit: int, filter_fp: str) -> Cursor:
    if not token or len(token) > MAX_CURSOR_LENGTH:
        raise InvalidCursorError("Invalid pagination cursor")
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64decode(signature), expected):
            raise InvalidCursorError("Invalid pagination cursor signature")
        payload = json.loads(_b64decode(body).decode("utf-8"))
    except InvalidCursorError:
        raise
    except Exception as exc:
        raise InvalidCursorError("Invalid pagination cursor") from exc

    try:
        version = int(payload["v"])
        page = int(payload["page"])
        limit = int(payload["limit"])
        sort = str(payload["sort"])
        nav = str(payload["nav"])
        key = payload["key"]
        asset_id = int(payload["id"])
        cursor_fp = str(payload["fp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidCursorError("Invalid pagination cursor payload") from exc

    if version != CURSOR_VERSION:
        raise InvalidCursorError("Unsupported pagination cursor version")
    if nav not in VALID_NAV:
        raise InvalidCursorError("Invalid pagination cursor direction")
    if page < 1 or limit < 1 or asset_id < 1:
        raise InvalidCursorError("Invalid pagination cursor position")
    if nav == "next" and page < 2:
        raise InvalidCursorError("Invalid next-page cursor")
    if not isinstance(key, (str, int)):
        raise InvalidCursorError("Invalid pagination cursor boundary")
    if sort != expected_sort or limit != expected_limit:
        raise InvalidCursorError("Pagination cursor does not match the requested sort/page size")
    if not hmac.compare_digest(cursor_fp, filter_fp):
        raise InvalidCursorError("Pagination cursor does not match the requested filters")

    return Cursor(
        page=page,
        limit=limit,
        sort=sort,
        nav=nav,
        key=key,
        id=asset_id,
        filter_fp=cursor_fp,
    )
