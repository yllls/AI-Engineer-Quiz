"""Bounded, consistent CSV export for the asset catalog."""
from __future__ import annotations

import codecs
import csv
import io
import json
import sqlite3
from datetime import datetime, timezone
from typing import Iterator, Sequence

from .config import config
from .db import new_connection
from .filters import FilterSpec
from .query import SortSpec, count_matches, export_rows

REQUIRED_COLUMNS = (
    "uuid",
    "title",
    "description",
    "filename",
    "media_type",
    "tags",
    "created_at",
)
OPTIONAL_COLUMNS = (
    "alt_text",
    "mime_type",
    "size",
    "width",
    "height",
    "updated_at",
    "dc_creator",
    "dc_subject",
    "dc_publisher",
    "dc_date",
    "license",
    "rights_statement",
    "credit",
    "usage_expiry",
    "retention_until",
)
ALL_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS
EXPORT_BATCH_SIZE = 500
DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class ExportValidationError(ValueError):
    """The requested export options are not in the allowlist."""


class ExportTooLargeError(ValueError):
    def __init__(self, matched: int, limit: int) -> None:
        self.matched = matched
        self.limit = limit
        super().__init__(f"Export matched {matched} rows, exceeding the limit of {limit}")


def resolve_columns(requested: Sequence[str]) -> tuple[str, ...]:
    selected: set[str] = set()
    unknown: list[str] = []
    for raw in requested:
        name = str(raw).strip()
        if name not in ALL_COLUMNS:
            unknown.append(str(raw))
        else:
            selected.add(name)
    if unknown:
        raise ExportValidationError(
            "Unknown export column(s): " + ", ".join(sorted(set(unknown)))
        )
    return REQUIRED_COLUMNS + tuple(
        column for column in OPTIONAL_COLUMNS if column in selected
    )


def sanitize_cell(value, enabled: bool):
    if value is None:
        return ""
    if enabled and isinstance(value, str) and value.startswith(DANGEROUS_PREFIXES):
        return "'" + value
    return value


def export_filename() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"keepstack-assets-{stamp}.csv"


class CsvExporter:
    """A snapshot-backed, row-at-a-time CSV writer.

    ``open`` does the preflight count inside the same read transaction that the
    row iterator later uses. A caller therefore rejects oversized exports before
    response headers, while concurrent edits cannot make the body disagree with
    the count.
    """

    def __init__(
        self,
        *,
        spec: FilterSpec,
        sort: SortSpec,
        columns: Sequence[str],
        sanitize_formulas: bool = True,
    ) -> None:
        self.spec = spec
        self.sort = sort
        self.columns = tuple(columns)
        self.sanitize_formulas = sanitize_formulas
        self.conn: sqlite3.Connection | None = None
        self.total = 0
        self._opened = False

    def open(self) -> None:
        if self._opened:
            return
        conn = new_connection(check_same_thread=False)
        try:
            conn.execute("BEGIN")
            total = count_matches(conn, self.spec)
            limit = config.export_max_rows
            if total > limit:
                raise ExportTooLargeError(total, limit)
        except Exception:
            try:
                conn.rollback()
            finally:
                conn.close()
            raise
        self.conn = conn
        self.total = total
        self._opened = True

    def close(self) -> None:
        conn = self.conn
        self.conn = None
        self._opened = False
        if conn is not None:
            try:
                conn.rollback()
            finally:
                conn.close()

    def __enter__(self) -> "CsvExporter":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _tags_for(self, asset_ids: Sequence[int]) -> dict[int, list[str]]:
        if not asset_ids:
            return {}
        placeholders = ",".join("?" for _ in asset_ids)
        rows = self.conn.execute(
            "SELECT at.asset_id, t.name FROM asset_tags at "
            "JOIN tags t ON t.id = at.tag_id "
            f"WHERE at.asset_id IN ({placeholders}) "
            "ORDER BY t.name",
            tuple(asset_ids),
        ).fetchall()
        result: dict[int, list[str]] = {}
        for row in rows:
            result.setdefault(row["asset_id"], []).append(row["name"])
        return result

    def _row_values(self, row: sqlite3.Row, tags: dict[int, list[str]]) -> list:
        values: list = []
        for column in self.columns:
            if column == "tags":
                raw = json.dumps(
                    tags.get(row["id"], []),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            else:
                raw = row[column]
            values.append(sanitize_cell(raw, self.sanitize_formulas))
        return values

    def iter_chunks(self) -> Iterator[bytes]:
        if self.conn is None:
            raise RuntimeError("CSV exporter must be opened before iterating")
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\r\n")
        try:
            writer.writerow(self.columns)
            yield codecs.BOM_UTF8 + buffer.getvalue().encode("utf-8")
            buffer.seek(0)
            buffer.truncate(0)

            if self.total:
                rows = export_rows(self.conn, self.spec, self.sort)
                while True:
                    batch = rows.fetchmany(EXPORT_BATCH_SIZE)
                    if not batch:
                        break
                    ids = [row["id"] for row in batch]
                    tag_map = self._tags_for(ids)
                    for row in batch:
                        writer.writerow(self._row_values(row, tag_map))
                    yield buffer.getvalue().encode("utf-8")
                    buffer.seek(0)
                    buffer.truncate(0)
        finally:
            self.close()
