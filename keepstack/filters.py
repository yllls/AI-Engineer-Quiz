"""Normalized, validated asset filter conditions.

All search and export paths build a :class:`FilterSpec` first. That keeps the
list, counts, cursors, and CSV export on exactly the same semantics instead of
reimplementing tag intersections in each route.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable, Optional

MAX_QUERY_LENGTH = 512
MAX_TAG_FILTERS = 50


class FilterValidationError(ValueError):
    """Raised for malformed or unbounded filter input."""


def normalize_query(q: Optional[str]) -> str:
    text = (q or "").strip()
    if len(text) > MAX_QUERY_LENGTH:
        raise FilterValidationError(
            f"Keyword query is limited to {MAX_QUERY_LENGTH} characters"
        )
    return text


def fts_terms(q: str) -> tuple[str, ...]:
    """Preserve the original alphanumeric-only prefix-term tokenization."""
    terms = [
        term
        for term in "".join(
            char if char.isalnum() or char.isspace() else " " for char in q
        ).split()
        if term
    ]
    return tuple(terms)


def fts_query(terms: Iterable[str]) -> str:
    return " ".join(f'"{term}"*' for term in terms)


def normalize_media_type(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized or None


def normalize_tags(values: Optional[Iterable[str]], *, label: str) -> tuple[str, ...]:
    if not values:
        return ()
    unique: set[str] = set()
    for raw in values:
        if raw is None:
            continue
        name = str(raw).strip().lower()
        if name:
            unique.add(name)
    if len(unique) > MAX_TAG_FILTERS:
        raise FilterValidationError(
            f"At most {MAX_TAG_FILTERS} {label} filters are allowed"
        )
    return tuple(sorted(unique))


@dataclass(frozen=True)
class FilterSpec:
    q: str = ""
    media_type: Optional[str] = None
    collection_id: Optional[int] = None
    tags: tuple[str, ...] = ()
    exclude_tags: tuple[str, ...] = ()
    status: str = "active"

    @classmethod
    def build(
        cls,
        *,
        q: Optional[str] = "",
        media_type: Optional[str] = None,
        collection_id: Optional[int | str] = None,
        tag: Optional[str] = None,
        tags: Optional[Iterable[str]] = None,
        exclude_tags: Optional[Iterable[str]] = None,
        status: str = "active",
    ) -> "FilterSpec":
        included: list[str] = []
        if tag:
            included.append(tag)
        if tags:
            included.extend(tags)

        if collection_id is not None:
            try:
                collection_value = int(collection_id)
            except (TypeError, ValueError) as exc:
                raise FilterValidationError("collection_id must be an integer") from exc
            if collection_value < 1:
                raise FilterValidationError("collection_id must be positive")
        else:
            collection_value = None

        return cls(
            q=normalize_query(q),
            media_type=normalize_media_type(media_type),
            collection_id=collection_value,
            tags=normalize_tags(included, label="include tag"),
            exclude_tags=normalize_tags(exclude_tags, label="exclude tag"),
            status=(status or "active").strip().lower() or "active",
        )

    @property
    def search_terms(self) -> tuple[str, ...]:
        return fts_terms(self.q) if self.q else ()

    @property
    def has_conflicting_tags(self) -> bool:
        return bool(set(self.tags) & set(self.exclude_tags))

    @property
    def unsearchable_keyword(self) -> bool:
        return bool(self.q) and not self.search_terms

    @property
    def matches_nothing(self) -> bool:
        return self.has_conflicting_tags or self.unsearchable_keyword

    @property
    def fingerprint(self) -> str:
        payload = {
            "q": self.q,
            "media_type": self.media_type,
            "collection_id": self.collection_id,
            "tags": list(self.tags),
            "exclude_tags": list(self.exclude_tags),
            "status": self.status,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def summary(self) -> dict:
        return {
            "q": self.q,
            "media_type": self.media_type,
            "collection_id": self.collection_id,
            "tags": list(self.tags),
            "exclude_tags": list(self.exclude_tags),
            "status": self.status,
        }
