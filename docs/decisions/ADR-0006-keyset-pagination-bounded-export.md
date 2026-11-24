# ADR-0006: Keyset pagination and bounded export

**Status:** accepted

## Context

The catalog now needs combined keyword, media, collection, included-tag, and
excluded-tag filters; deterministic ordering by creation time, name, or size;
previous/next navigation; a matching total; and a CSV export that covers the
entire filtered result. Equal creation timestamps must not create duplicate or
missing assets across pages. The original list API used `limit` and `offset`,
which remains useful for existing callers but is not a reliable page model when
rows tie or change.

The catalog is expected to reach roughly 100000 assets on a single SQLite
deployment. A naïve CSV export could hold the whole result in memory, and
aggregating all tag links on every list request would dominate the response
time.

## Decision

Use signed keyset cursors for the web client. Each cursor is opaque, carries the
sort boundary, target page, page size, navigation direction, and a fingerprint
of the normalized filters, and is HMAC-signed with the existing server secret.
The id column is a deterministic tie-breaker. The frontend maintains a cursor
stack for page display and uses the server's reverse query for previous pages.
If the filters, sort, or page size change, the old cursor is rejected and the
client starts at page one. Legacy `limit`/`offset` requests remain available but
cannot be combined with a cursor.

Build one SQL query core for counts, pages, boundary probes, and export rows.
Express FTS matches as a rowid subquery and tag conditions as `EXISTS` /
`NOT EXISTS`. Maintain `tag_usage` with database triggers for global active-tag
facets, and add a media index for the media facet.

Generate CSV synchronously from a dedicated read transaction. Count first, reject
results above a configurable limit, and stream rows in batches with `csv.writer`.
The download is authenticated with the existing Bearer token and is delivered to
the browser as a Blob. Required columns are always emitted; optional columns are
allowlisted. Tags are JSON arrays. Formula-injection protection is on by
default, with an explicit opt-out. Successful export initiation is audited.

## Consequences

**Good.** Cursor navigation is stable when timestamps tie, page changes cannot
silently mix filter contexts, and the list and export share one definition of
filtering and ordering. The 100000-asset benchmark stays under the 500ms list
P95 target on the reference host, and a 100k-row export completes in a few
seconds with bounded server memory.

**The cost.** There is no arbitrary jump to page N, because the cursor only
moves between neighbouring pages. Cursor tokens are intentionally not
shareable, and signing means a secret rotation invalidates pages already open.
The export holds a read transaction while streaming; this delays WAL
checkpointing and is why the result cap exists. Browser Blob downloads buffer
the file in the browser, and synchronous export is the wrong shape for jobs
larger than the configured cap. Disabling formula protection exports the raw
cell text. The UI hides semantic search in this phase; the legacy API remains
available without the new cursor and tag combinations.
