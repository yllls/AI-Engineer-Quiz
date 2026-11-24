# Changelog

All notable changes to Keepstack are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
semantic versioning.

## [Unreleased]

### Added

- Combined asset search with keyword, media type, collection, all-of included
  tags, and any-of excluded tags, while still honoring the selected sort.
- Signed, bidirectional keyset pagination with a deterministic id tie-breaker,
  a filter/sort/page-size fingerprint, and legacy `limit`/`offset` compatibility.
- Bounded streaming CSV export with a single read snapshot, required and optional
  columns, UTF-8 BOM/CRLF output, JSON tag arrays, and spreadsheet formula
  protection that can be disabled.
- A search toolbar with removable filter chips, searchable paginated tag
  candidates, include/exclude tag modes, and URL-restored filter/sort state.
- Versioned SQLite migrations, search indexes, and a trigger-maintained
  `tag_usage` table for fast active-tag facets.
- Playwright browser tests for filtering, pagination, export downloads, list
  failures, request races, and authenticated asset downloads.
- A reproducible 100k-asset benchmark script for list latency and full export.
- Optional collection-aware uploads: the upload dialog preselects the current
  library collection, lets the batch be filed or left unfiled, attaches duplicate
  assets to the target, and reports per-file collection status.
- User lifecycle management: create/edit forms, optional password reset,
  disable/enable, permanent deletion for inactive users, self and last-admin
  safeguards, and `token_version` session invalidation.
- Upload progress rows now have explicit terminal states, clear stale rows
  between batches, and time out instead of displaying an endless uploading
  state.

- Parse common XMP RDF fields into structured metadata, catalog fields, and
  full-text search.
- OCR images at ingest through the Tesseract binary when it is installed, so
  text inside scans and photos becomes searchable. No new Python dependencies;
  disable with `KEEPSTACK_OCR_ENABLED=false`.
- On-device CLIP vision model (optional `pip install onnxruntime`): real
  semantic search over image content, visual similarity, and zero-shot
  auto-tagging with no API keys, plus a `python -m keepstack reindex` command
  to re-embed an existing catalog.
- Advertise IIIF Image API level 2 and support region cropping, percent
  regions, best-fit sizing, and rotation for IIIF image requests.
- Added `python -m keepstack gc` to remove blob files no longer referenced by
  active assets or their version history.

- IIIF Presentation API 3.0 manifest at `GET /iiif/3/{uuid}/manifest.json`, so
  viewers like Mirador and Universal Viewer open assets directly with their
  label and metadata.
- Login rate limiting: repeated failed attempts for a username and IP are
  locked out with a 429 and `Retry-After`, and the lockout is audit-logged.
  Tunable with `KEEPSTACK_LOGIN_MAX_ATTEMPTS` and `KEEPSTACK_LOGIN_LOCKOUT_SECONDS`.

### Changed

- The library now uses a top filter toolbar and cursor pagination instead of
  the old sidebar facets and single-tag filter. Semantic search remains in the
  legacy API but is hidden from the web interface.
- The asset library defaults to 10 items per page, offers 10/20/50, and uses a
  five-column desktop grid so 10-item pages form two complete rows.
- Asset tag editing now has an explicit Add button beside the tag input; Enter
  continues to submit the same form.
- The upload flow locks its collection and AI options while a batch is running,
  clears stale status rows, and confirms before closing during active uploads.
- User editing now validates roles, protects the current and last active admin,
  and writes user lifecycle audit events. Permanent deletion requires the user
  to be inactive first.
- Keyword searches honor the selected time or metadata sort instead of always
  ordering by FTS relevance.
- Tag and media facets use active assets and are no longer presented as counts
  for the current result set.
- The asset detail Download action now carries the Bearer token through a
  protected fetch, and JSON requests default to POST when no method is supplied.
- FTS filtering uses a rowid subquery, avoiding per-asset virtual-table probes
  on large catalogs.

- Switched the web interface to a light theme: white and soft-gray surfaces
  with an indigo accent, applied across the app, placeholders, and share pages.
- Centered the captions in generated seed images so square thumbnails no
  longer crop them.
- Tidied the chrome: background scrolling now locks while the drawer or upload
  modal is open (one scrollbar instead of two), scrollbars are slim and themed,
  and the dropdown controls got a custom chevron with roomier padding.

## [0.1.0] - 2026-06-28

The first working release. A complete DAM core, built to sit in the gap between
standards-rich heritage tools and modern AI photo libraries.

### Added

- Content-addressable blob storage with automatic deduplication and SHA-256 fixity
- Ingest pipeline with EXIF / IPTC / XMP extraction and thumbnail generation
- Search: SQLite FTS5 full-text, faceted filtering, and embedding-based semantic
  search with a "more like this" mode
- Collections, tags (manual and AI-suggested), and per-asset version history
- Four-role access control (admin, editor, contributor, viewer), an audit log on
  every state change, and tokenised expiring share links
- Open standards: Dublin Core mapping, an OAI-PMH 2.0 endpoint, and an IIIF
  Image API endpoint for every asset
- A repository-wide fixity check for digital-preservation integrity
- Optional AI enrichment (Groq vision, Cohere embeddings) with deterministic
  offline fallbacks, so the product is fully useful with zero API keys
- A zero-build two-tone web client and a REST API covering every resource
- A passing test suite, synthetic seed data, and a Docker deployment

### Known limitations

Tracked in [ROADMAP.md](ROADMAP.md) and graded honestly in
[COMPARISON.md](COMPARISON.md): AI is heuristic without a provider key,
preservation is fixity rather than format migration, IIIF is level 1, and there
is no OCR, SSO, or published accessibility conformance report yet.
