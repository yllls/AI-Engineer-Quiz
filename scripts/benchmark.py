"""Reproducible local benchmark for list queries and full CSV export.

Usage:
    python scripts/benchmark.py --rows 100000

The script creates a throwaway catalog by default. Pass ``--data-dir`` with a
fresh directory to keep the generated data for inspection, and ``--keep`` to
retain the default temporary directory.

This is intentionally not part of CI; it is a release note tool for recording
real measurements on the target host.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, math.ceil(pct / 100 * len(ordered)) - 1))
    return ordered[index]


def _fresh_data_dir(requested: str | None) -> Path:
    if requested:
        path = Path(requested).resolve()
        path.mkdir(parents=True, exist_ok=True)
        db_path = path / "keepstack.db"
        if db_path.exists() and db_path.stat().st_size > 0:
            raise SystemExit(f"Refusing to reuse a non-empty database: {db_path}")
        return path
    return Path(tempfile.mkdtemp(prefix="keepstack-bench-"))


def _seed(rows: int) -> tuple[int, float]:
    from keepstack.db import get_conn, init_db

    init_db()
    conn = get_conn()
    collection_id = conn.execute(
        "INSERT INTO collections (uuid, name, description, kind, created_at) "
        "VALUES ('bench-collection', 'Benchmark Collection', '', 'folder', ?)",
        ("2025-01-01T00:00:00+00:00",),
    ).lastrowid
    conn.commit()

    tag_names = ["approved", "campaign", "internal"] + [f"bucket-{i:02d}" for i in range(20)]
    tag_ids: dict[str, int] = {}
    for name in tag_names:
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
        tag_ids[name] = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()[0]
    conn.commit()

    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    started = time.perf_counter()
    batch_size = 1000
    conn.execute("BEGIN")
    try:
        for start in range(0, rows, batch_size):
            end = min(rows, start + batch_size)
            assets = []
            fts_rows = []
            asset_tags = []
            collection_rows = []
            for index in range(start, end):
                asset_id = index + 1
                created = (base + timedelta(seconds=index)).isoformat()
                sha = f"{index + 1:064x}"
                title = f"Harbor archive asset {index:06d}"
                description = f"Benchmark harbor estuary asset {index:06d}"
                selected_tags = [f"bucket-{index % 20:02d}"]
                if index % 2 == 0:
                    selected_tags.append("approved")
                if index % 3 == 0:
                    selected_tags.append("campaign")
                if index % 10 == 0:
                    selected_tags.append("internal")
                tag_text = " ".join(selected_tags)
                assets.append(
                    (
                        asset_id,
                        f"bench-{asset_id:09d}",
                        sha,
                        f"harbor-{index:06d}.jpg",
                        "image/jpeg",
                        "image",
                        "jpg",
                        100_000 + (index % 900_000),
                        1200,
                        800,
                        title,
                        description,
                        "active",
                        f"blobs/{sha[:2]}/{sha}",
                        None,
                        created,
                        created,
                    )
                )
                fts_rows.append(
                    (asset_id, title, description, "", f"harbor-{index:06d}.jpg", tag_text, "", "")
                )
                for tag in selected_tags:
                    asset_tags.append((asset_id, tag_ids[tag]))
                if index < rows * 0.6:
                    collection_rows.append((collection_id, asset_id))
            conn.executemany(
                "INSERT INTO assets "
                "(id, uuid, sha256, filename, mime_type, media_type, ext, size, width, height, "
                "title, description, status, storage_key, thumb_key, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                assets,
            )
            conn.executemany(
                "INSERT INTO asset_fts "
                "(rowid, title, description, alt_text, filename, tags, metadata_text, custom_text) "
                "VALUES (?,?,?,?,?,?,?,?)",
                fts_rows,
            )
            conn.executemany(
                "INSERT OR IGNORE INTO asset_tags (asset_id, tag_id, source) VALUES (?,?,'manual')",
                asset_tags,
            )
            conn.executemany(
                "INSERT OR IGNORE INTO collection_assets (collection_id, asset_id, added_at) "
                "VALUES (?,?, '2025-01-01T00:00:00+00:00')",
                collection_rows,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return int(collection_id), time.perf_counter() - started


def _bench_list(iterations: int, collection_id: int) -> list[dict]:
    from keepstack import search

    scenarios = [
        (
            "keyword",
            dict(q="harbor", sort="newest", limit=10),
        ),
        (
            "combined",
            dict(
                q="harbor",
                collection_id=collection_id,
                tags=["approved", "campaign"],
                exclude_tags=["internal"],
                sort="newest",
                limit=10,
            ),
        ),
    ]
    results = []
    for name, kwargs in scenarios:
        warmup = search.search(**kwargs)
        times = []
        for _ in range(iterations):
            started = time.perf_counter()
            search.search(**kwargs)
            times.append((time.perf_counter() - started) * 1000)
        results.append(
            {
                "scenario": name,
                "total": warmup["total"],
                "items": len(warmup["items"]),
                "p50_ms": round(_percentile(times, 50), 2),
                "p95_ms": round(_percentile(times, 95), 2),
                "min_ms": round(min(times), 2),
                "max_ms": round(max(times), 2),
            }
        )
    return results


def _bench_export() -> dict:
    from keepstack.exports import CsvExporter, REQUIRED_COLUMNS
    from keepstack.filters import FilterSpec
    from keepstack.query import resolve_sort

    exporter = CsvExporter(
        spec=FilterSpec.build(status="active"),
        sort=resolve_sort("newest"),
        columns=REQUIRED_COLUMNS,
        sanitize_formulas=True,
    )
    started = time.perf_counter()
    exporter.open()
    byte_count = 0
    chunks = 0
    for chunk in exporter.iter_chunks():
        byte_count += len(chunk)
        chunks += 1
    elapsed = time.perf_counter() - started
    exported_rows = exporter.total
    return {
        "rows": exported_rows,
        "seconds": round(elapsed, 2),
        "bytes": byte_count,
        "chunks": chunks,
        "rows_per_second": int(exported_rows / elapsed) if elapsed else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--data-dir")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--skip-export", action="store_true")
    args = parser.parse_args()
    if args.rows < 1 or args.iterations < 1:
        raise SystemExit("--rows and --iterations must be positive")

    data_dir = _fresh_data_dir(args.data_dir)
    os.environ["KEEPSTACK_DATA_DIR"] = str(data_dir)
    os.environ["KEEPSTACK_AI_ENABLED"] = "false"
    os.environ["KEEPSTACK_OCR_ENABLED"] = "false"
    os.environ["KEEPSTACK_EXPORT_MAX_ROWS"] = str(max(args.rows, 1))

    from keepstack.db import get_conn

    collection_id, seed_seconds = _seed(args.rows)
    list_results = _bench_list(args.iterations, collection_id=collection_id)
    export_result = None if args.skip_export else _bench_export()
    sqlite_version = get_conn().execute("SELECT sqlite_version()").fetchone()[0]
    get_conn().close()

    report = {
        "environment": {
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "sqlite": sqlite_version,
            "rows": args.rows,
            "iterations": args.iterations,
            "data_dir": str(data_dir),
        },
        "seed_seconds": round(seed_seconds, 2),
        "list": list_results,
        "export": export_result,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if not args.keep and not args.data_dir:
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
