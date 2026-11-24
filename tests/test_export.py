"""CSV export contract, safety, limits, and snapshot consistency."""
import codecs
import csv
import io
import json
import re
import uuid

import pytest

from keepstack import exports, search
from keepstack.config import config
from keepstack.db import get_conn, init_db
from keepstack.exports import CsvExporter, resolve_columns
from keepstack.filters import FilterSpec
from keepstack.query import resolve_sort
from tests.fixtures_data import create_asset


def _token() -> str:
    return "e" + uuid.uuid4().hex[:10]


def _csv_rows(response) -> tuple[list[list[str]], str]:
    raw = response.content
    assert raw.startswith(codecs.BOM_UTF8)
    text = raw.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    return rows, text


@pytest.fixture(scope="module", autouse=True)
def _schema():
    init_db()


def test_export_is_complete_sorted_and_matches_list_filters(api_client, auth_headers):
    token = _token()
    collection = None
    from tests.fixtures_data import create_collection

    collection = create_collection(f"Export {token}")
    keep = f"keep-{token}"
    drop = f"drop-{token}"
    expected = []
    for index in range(5):
        if index < 3:
            tags = [keep, f"z-{token}"]
        elif index == 4:
            tags = [keep]
        else:
            tags = [drop]
        asset = create_asset(
            token,
            f"export-{index}",
            title=f"{token} export {index}",
            tags=tags,
            collection_id=collection if index in (0, 1, 2, 3) else None,
            created_at=f"2026-05-0{index + 1}T00:00:00+00:00",
            description=(
                '中文, 逗号, "引号"\n下一行' if index == 0 else f"description {index}"
            ),
            alt_text=f"alt {index}" if index == 0 else None,
        )
        expected.append(asset)

    # Only index 0..2 have both keep and the collection; index 3/4 lack one.
    result = search.search(
        q=token,
        tags=[keep],
        collection_id=collection,
        sort="oldest",
        limit=2,
    )
    assert result["total"] == 3

    response = api_client.get(
        "/api/exports/assets.csv",
        params=[
            ("q", token),
            ("tags", keep),
            ("collection_id", collection),
            ("sort", "oldest"),
            ("columns", "size"),
            ("columns", "alt_text"),
        ],
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment;" in response.headers["content-disposition"]
    assert re.search(r'filename="keepstack-assets-\d{8}T\d{6}Z\.csv"', response.headers["content-disposition"])
    assert response.headers["cache-control"] == "no-store"

    rows, _ = _csv_rows(response)
    header = rows[0]
    assert header == [
        "uuid",
        "title",
        "description",
        "filename",
        "media_type",
        "tags",
        "created_at",
        "alt_text",
        "size",
    ]
    body = rows[1:]
    assert len(body) == 3
    assert [row[0] for row in body] == [expected[index]["uuid"] for index in (0, 1, 2)]
    assert body[0][2] == '中文, 逗号, "引号"\n下一行'
    tags = json.loads(body[0][5])
    assert tags == sorted(tags)
    assert set(tags) == {keep, f"z-{token}"}
    assert body[0][7] == "alt 0"
    assert str(expected[0]["size"]) == body[0][8]


def test_export_zero_rows_still_returns_header(api_client, auth_headers):
    response = api_client.get(
        "/api/exports/assets.csv",
        params={"q": _token()},
        headers=auth_headers,
    )
    assert response.status_code == 200
    rows, text = _csv_rows(response)
    assert rows == [
        [
            "uuid",
            "title",
            "description",
            "filename",
            "media_type",
            "tags",
            "created_at",
        ]
    ]
    assert text.endswith("\r\n")


def test_export_formula_protection_can_be_disabled(api_client, auth_headers):
    token = _token()
    create_asset(
        token,
        "formula",
        title=f"{token} formula",
        description="=SUM(A1:A2)",
    )

    protected = api_client.get(
        "/api/exports/assets.csv",
        params={"q": token},
        headers=auth_headers,
    )
    rows, _ = _csv_rows(protected)
    assert rows[1][2] == "'=SUM(A1:A2)"

    raw = api_client.get(
        "/api/exports/assets.csv",
        params={"q": token, "sanitize_formulas": "false"},
        headers=auth_headers,
    )
    rows, _ = _csv_rows(raw)
    assert rows[1][2] == "=SUM(A1:A2)"


def test_export_limit_audit_columns_and_auth(api_client, auth_headers, monkeypatch):
    token = _token()
    for index in range(3):
        create_asset(token, f"limit-{index}", title=f"{token} limit {index}")

    with monkeypatch.context() as patch:
        patch.setattr(config, "export_max_rows", 2)
        too_large = api_client.get(
            "/api/exports/assets.csv",
            params={"q": token},
            headers=auth_headers,
        )
    assert too_large.status_code == 413
    detail = too_large.json()["detail"]
    assert detail["code"] == "export_too_large"
    assert detail["matched"] == 3
    assert detail["limit"] == 2

    unknown = api_client.get(
        "/api/exports/assets.csv",
        params=[("q", token), ("columns", "password_hash")],
        headers=auth_headers,
    )
    assert unknown.status_code == 422

    inactive = api_client.get(
        "/api/exports/assets.csv",
        params={"q": token, "status": "deleted"},
        headers=auth_headers,
    )
    assert inactive.status_code == 400
    assert inactive.json()["detail"]["code"] == "active_only"

    assert api_client.get(
        "/api/exports/assets.csv", params={"q": token}
    ).status_code == 401

    before = get_conn().execute(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'asset.export'"
    ).fetchone()[0]
    success = api_client.get(
        "/api/exports/assets.csv",
        params={"q": token},
        headers=auth_headers,
    )
    assert success.status_code == 200
    after = get_conn().execute(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'asset.export'"
    ).fetchone()[0]
    assert after == before + 1
    entry = get_conn().execute(
        "SELECT detail FROM audit_log WHERE action = 'asset.export' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    detail = json.loads(entry["detail"])
    assert detail["matched"] == 3
    assert detail["sort"] == "newest"


def test_export_uses_one_read_snapshot(monkeypatch):
    token = _token()
    for index in range(3):
        create_asset(
            token,
            f"snapshot-{index}",
            title=f"{token} snapshot {index}",
            created_at=f"2026-06-0{index + 1}T00:00:00+00:00",
            description=f"old-{index}",
        )

    spec = FilterSpec.build(q=token, status="active")
    exporter = CsvExporter(
        spec=spec,
        sort=resolve_sort("oldest"),
        columns=resolve_columns(["description"]),
        sanitize_formulas=True,
    )
    monkeypatch.setattr(exports, "EXPORT_BATCH_SIZE", 1)
    exporter.open()
    chunks = exporter.iter_chunks()
    header_chunk = next(chunks)
    assert b"uuid" in header_chunk
    first_data_chunk = next(chunks)
    assert b"old-0" in first_data_chunk

    conn = get_conn()
    conn.execute(
        "UPDATE assets SET description = 'new-value' WHERE title LIKE ?",
        (f"{token}%",),
    )
    conn.commit()

    remaining = list(chunks)
    text = b"".join(remaining).decode("utf-8")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    assert [row[2] for row in rows if row] == ["old-1", "old-2"]
