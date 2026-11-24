"""Upload-to-collection behavior and duplicate handling."""
import io
import uuid

import pytest
from PIL import Image

from keepstack.db import get_conn, init_db
from tests.fixtures_data import create_collection


def _token() -> str:
    return "u" + uuid.uuid4().hex[:10]


def _image(seed: int) -> bytes:
    image = Image.new("RGB", (32, 24), (20 + seed, 40 + seed, 80 + seed))
    image.putpixel((seed % 32, seed % 24), (200, 30, 30))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture(scope="module", autouse=True)
def _schema():
    init_db()


def test_upload_can_target_collection_and_dedupe(api_client, auth_headers):
    token = _token()
    collection_id = create_collection(f"Upload target {token}")
    image_a = _image(7)
    image_b = _image(19)

    first = api_client.post(
        "/api/assets",
        files=[("files", ("upload-a.jpg", image_a, "image/jpeg"))],
        data={"run_ai": "false", "collection_id": str(collection_id)},
        headers=auth_headers,
    )
    assert first.status_code == 200, first.text
    created = first.json()["created"][0]
    assert created["duplicate"] is False
    assert created["collection_id"] == collection_id
    assert created["collection_added"] is True
    assert created["collection_error"] is None

    duplicate = api_client.post(
        "/api/assets",
        files=[("files", ("upload-a-again.jpg", image_a, "image/jpeg"))],
        data={"run_ai": "false", "collection_id": str(collection_id)},
        headers=auth_headers,
    )
    assert duplicate.status_code == 200, duplicate.text
    duplicate_item = duplicate.json()["created"][0]
    assert duplicate_item["duplicate"] is True
    assert duplicate_item["uuid"] == created["uuid"]
    assert duplicate_item["collection_added"] is False

    second = api_client.post(
        "/api/assets",
        files=[("files", ("upload-b.jpg", image_b, "image/jpeg"))],
        data={"run_ai": "false", "collection_id": str(collection_id)},
        headers=auth_headers,
    )
    assert second.status_code == 200, second.text
    assert second.json()["created"][0]["collection_added"] is True

    rows = get_conn().execute(
        "SELECT COUNT(*) c FROM collection_assets WHERE collection_id = ?",
        (collection_id,),
    ).fetchone()
    assert rows["c"] == 2

    audit = get_conn().execute(
        "SELECT detail FROM audit_log WHERE action = 'collection.add' AND target_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (str(collection_id),),
    ).fetchone()
    assert audit is not None
    assert "uploaded assets" in audit["detail"]


def test_upload_without_collection_and_invalid_collection(api_client, auth_headers):
    token = _token()
    image = _image(31)
    plain = api_client.post(
        "/api/assets",
        files=[("files", ("plain.jpg", image, "image/jpeg"))],
        data={"run_ai": "false"},
        headers=auth_headers,
    )
    assert plain.status_code == 200, plain.text
    item = plain.json()["created"][0]
    assert item["collection_id"] is None
    assert item["collection_added"] is False

    missing = api_client.post(
        "/api/assets",
        files=[("files", (f"missing-{token}.jpg", _image(44), "image/jpeg"))],
        data={"run_ai": "false", "collection_id": "99999999"},
        headers=auth_headers,
    )
    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "collection_not_found"


def test_add_to_collection_counts_only_new_links(api_client, auth_headers):
    token = _token()
    collection_id = create_collection(f"Link count {token}")
    image = _image(55)
    uploaded = api_client.post(
        "/api/assets",
        files=[("files", ("link-count.jpg", image, "image/jpeg"))],
        data={"run_ai": "false"},
        headers=auth_headers,
    )
    uuid_value = uploaded.json()["created"][0]["uuid"]

    first = api_client.post(
        f"/api/collections/{collection_id}/assets",
        json={"uuids": [uuid_value]},
        headers=auth_headers,
    )
    assert first.status_code == 200
    assert first.json()["added"] == 1

    second = api_client.post(
        f"/api/collections/{collection_id}/assets",
        json={"uuids": [uuid_value]},
        headers=auth_headers,
    )
    assert second.status_code == 200
    assert second.json()["added"] == 0
