# ADR-0007: Collection-aware uploads

**Status:** accepted

## Context

The original upload flow accepted files and filed them only in the general
asset library. Users who were browsing a collection had to remember to perform a
separate association step afterwards, and the API only exposed collection
association through a second call. The upload dialog also kept historical rows
between sessions and could leave a row looking unfinished after a file had
already been ingested.

## Decision

Extend `POST /api/assets` with an optional `collection_id` form field. Validate
the target before reading files. After each ingest, link the created or
deduplicated asset to the collection with an idempotent insert. Each file gets
its own `collection_added` or `collection_error` result; a link failure never
removes an uploaded asset. One `collection.add` audit entry summarises the
batch.

The upload dialog preselects the library's current collection filter, offers
"No collection" and the available collections, and applies one target to the
whole batch. It locks collection and AI choices during the batch, clears old
status rows, gives every file an explicit status element, and times out the
client wait instead of displaying an indefinite "uploading" state.

## Consequences

**Good.** The common content-ops flow, uploading into the collection being
curated, is one step. Duplicate uploads still produce one stored asset but can
be filed in the new collection. The batch reports partial successes instead of
losing the upload when the collection has been removed. The status display has
a terminal state for every file.

**The cost.** The API now has an extra form field and a four-state per-file
result. The UI supports one collection per batch, not multiple collections.
Closing the dialog during a running batch requires confirmation and uploads
continue in the background. The 5-minute browser timeout may stop waiting before
a very large server-side ingest completes, so a timeout asks the user to refresh
the library rather than claiming the upload failed. There is no upload progress
bar, because the browser uses `fetch` rather than an upload-progress API.
