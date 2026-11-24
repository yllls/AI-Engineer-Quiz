"""Shared SQL construction for filtered lists, cursor pages, and CSV export."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from .filters import FilterSpec, fts_query
from .pagination import Cursor, encode_cursor


class QueryError(ValueError):
    """The requested sort or pagination shape is not supported."""


@dataclass(frozen=True)
class SortSpec:
    name: str
    key_sql: str
    direction: str

    @property
    def id_direction(self) -> str:
        # Keeping the id tie-breaker in the same direction as the primary key
        # gives newly-created records the intuitive position on both newest and
        # oldest pages, and removes all ambiguity for equal sort values.
        return self.direction


SORTS = {
    "newest": SortSpec("newest", "a.created_at", "DESC"),
    "oldest": SortSpec("oldest", "a.created_at", "ASC"),
    "name": SortSpec("name", "COALESCE(a.title, '')", "ASC"),
    "largest": SortSpec("largest", "COALESCE(a.size, 0)", "DESC"),
}
DEFAULT_SORT = "newest"


def resolve_sort(name: str) -> SortSpec:
    try:
        return SORTS[name]
    except KeyError as exc:
        raise QueryError(f"Unsupported sort: {name}") from exc


def sort_value(row: sqlite3.Row, sort: SortSpec) -> str | int:
    if sort.name in ("newest", "oldest"):
        return row["created_at"]
    if sort.name == "name":
        return row["title"] or ""
    if sort.name == "largest":
        return row["size"] if row["size"] is not None else 0
    raise QueryError(f"Unsupported sort: {sort.name}")


@dataclass(frozen=True)
class BaseQuery:
    joins: str
    where: str
    params: tuple
    no_match: bool = False


@dataclass(frozen=True)
class PageRows:
    rows: list[sqlite3.Row]
    page: int
    limit: int
    prev_cursor: Optional[str]
    next_cursor: Optional[str]


def build_base(spec: FilterSpec) -> BaseQuery:
    if spec.matches_nothing:
        return BaseQuery("", "", (), no_match=True)

    joins: list[str] = []
    where: list[str] = ["a.status = ?"]
    params: list = [spec.status]

    if spec.search_terms:
        joins.append("JOIN asset_fts f ON f.rowid = a.id")
        where.append("asset_fts MATCH ?")
        params.append(fts_query(spec.search_terms))

    if spec.media_type:
        where.append("a.media_type = ?")
        params.append(spec.media_type)

    if spec.collection_id is not None:
        where.append(
            "EXISTS (SELECT 1 FROM collection_assets ca "
            "WHERE ca.asset_id = a.id AND ca.collection_id = ?)"
        )
        params.append(spec.collection_id)

    for tag in spec.tags:
        where.append(
            "EXISTS (SELECT 1 FROM asset_tags at_i JOIN tags t_i "
            "ON t_i.id = at_i.tag_id "
            "WHERE at_i.asset_id = a.id AND t_i.name = ?)"
        )
        params.append(tag)

    if spec.exclude_tags:
        placeholders = ",".join("?" for _ in spec.exclude_tags)
        where.append(
            "NOT EXISTS (SELECT 1 FROM asset_tags at_e JOIN tags t_e "
            "ON t_e.id = at_e.tag_id "
            f"WHERE at_e.asset_id = a.id AND t_e.name IN ({placeholders}))"
        )
        params.extend(spec.exclude_tags)

    return BaseQuery(" ".join(joins), " AND ".join(where), tuple(params))


def count_matches(conn: sqlite3.Connection, spec: FilterSpec) -> int:
    base = build_base(spec)
    if base.no_match:
        return 0
    sql = f"SELECT COUNT(*) AS c FROM assets a {base.joins} WHERE {base.where}"
    return int(conn.execute(sql, base.params).fetchone()[0])


def _order_sql(sort: SortSpec, *, reverse: bool = False) -> str:
    direction = sort.direction
    if reverse:
        direction = "ASC" if direction == "DESC" else "DESC"
    return f"{sort.key_sql} {direction}, a.id {direction}"


def _boundary_clause(
    sort: SortSpec, row: sqlite3.Row, *, after: bool
) -> tuple[str, list]:
    key = sort_value(row, sort)
    if after:
        op = "<" if sort.direction == "DESC" else ">"
    else:
        op = ">" if sort.direction == "DESC" else "<"
    clause = (
        f"({sort.key_sql} {op} ? "
        f"OR ({sort.key_sql} = ? AND a.id {op} ?))"
    )
    return clause, [key, key, row["id"]]


def _has_any(
    conn: sqlite3.Connection,
    base: BaseQuery,
    sort: SortSpec,
    row: sqlite3.Row,
    *,
    after: bool,
) -> bool:
    boundary, boundary_params = _boundary_clause(sort, row, after=after)
    sql = (
        f"SELECT 1 FROM assets a {base.joins} "
        f"WHERE {base.where} AND {boundary} LIMIT 1"
    )
    return conn.execute(sql, (*base.params, *boundary_params)).fetchone() is not None


def fetch_cursor_page(
    conn: sqlite3.Connection,
    spec: FilterSpec,
    sort: SortSpec,
    limit: int,
    cursor: Optional[Cursor],
) -> PageRows:
    base = build_base(spec)
    if base.no_match:
        return PageRows([], cursor.page if cursor else 1, limit, None, None)

    page = cursor.page if cursor else 1
    params: list = list(base.params)
    where = base.where
    reverse = False
    if cursor is not None:
        after = cursor.nav == "next"
        boundary, boundary_params = _boundary_clause_for_cursor(sort, cursor, after=after)
        where = f"{where} AND {boundary}"
        params.extend(boundary_params)
        reverse = cursor.nav == "prev"

    order = _order_sql(sort, reverse=reverse)
    sql = (
        f"SELECT a.* FROM assets a {base.joins} WHERE {where} "
        f"ORDER BY {order} LIMIT ?"
    )
    rows = list(conn.execute(sql, (*params, limit)).fetchall())
    if reverse:
        rows.reverse()

    if not rows:
        return PageRows([], page, limit, None, None)

    has_next = _has_any(conn, base, sort, rows[-1], after=True)
    has_prev = page > 1 and _has_any(conn, base, sort, rows[0], after=False)

    next_cursor = None
    if has_next:
        next_cursor = encode_cursor(
            page=page + 1,
            limit=limit,
            sort=sort.name,
            nav="next",
            key=sort_value(rows[-1], sort),
            asset_id=rows[-1]["id"],
            filter_fp=spec.fingerprint,
        )
    prev_cursor = None
    if has_prev:
        prev_cursor = encode_cursor(
            page=page - 1,
            limit=limit,
            sort=sort.name,
            nav="prev",
            key=sort_value(rows[0], sort),
            asset_id=rows[0]["id"],
            filter_fp=spec.fingerprint,
        )
    return PageRows(rows, page, limit, prev_cursor, next_cursor)


def _boundary_clause_for_cursor(
    sort: SortSpec, cursor: Cursor, *, after: bool
) -> tuple[str, list]:
    if after:
        op = "<" if sort.direction == "DESC" else ">"
    else:
        op = ">" if sort.direction == "DESC" else "<"
    clause = (
        f"({sort.key_sql} {op} ? "
        f"OR ({sort.key_sql} = ? AND a.id {op} ?))"
    )
    return clause, [cursor.key, cursor.key, cursor.id]


def fetch_offset_page(
    conn: sqlite3.Connection,
    spec: FilterSpec,
    sort: SortSpec,
    limit: int,
    offset: int,
) -> PageRows:
    base = build_base(spec)
    page = offset // limit + 1
    if base.no_match:
        return PageRows([], page, limit, None, None)
    sql = (
        f"SELECT a.* FROM assets a {base.joins} WHERE {base.where} "
        f"ORDER BY {_order_sql(sort)} LIMIT ? OFFSET ?"
    )
    rows = list(conn.execute(sql, (*base.params, limit, offset)).fetchall())
    return PageRows(rows, page, limit, None, None)


def export_rows(
    conn: sqlite3.Connection, spec: FilterSpec, sort: SortSpec
):
    """Return the full ordered result iterator for a CSV snapshot."""
    base = build_base(spec)
    if base.no_match:
        return iter(())
    sql = (
        f"SELECT a.* FROM assets a {base.joins} WHERE {base.where} "
        f"ORDER BY {_order_sql(sort)}"
    )
    return conn.execute(sql, base.params)
