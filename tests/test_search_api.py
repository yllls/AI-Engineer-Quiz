"""HTTP contract for combined filters, status compatibility, and cursors."""
import uuid

import pytest

from keepstack.db import init_db
from tests.fixtures_data import create_asset


def _token() -> str:
    return "a" + uuid.uuid4().hex[:10]


@pytest.fixture(scope="module", autouse=True)
def _schema():
    init_db()


def test_cursor_api_pagination_validation_and_auth(api_client, auth_headers):
    token = _token()
    stamp = "2026-04-01T00:00:00+00:00"
    assets = [
        create_asset(token, f"api-cursor-{index}", title=f"{token} cursor {index}", created_at=stamp)
        for index in range(5)
    ]

    unauthorized = api_client.get("/api/assets", params={"q": token})
    assert unauthorized.status_code == 401

    first = api_client.get(
        "/api/assets",
        params={"q": token, "sort": "oldest", "limit": 2},
        headers=auth_headers,
    )
    assert first.status_code == 200, first.text
    first_data = first.json()
    assert first_data["total"] == 5
    assert first_data["pagination"]["page"] == 1
    assert first_data["pagination"]["limit"] == 2
    assert first_data["pagination"]["prev_cursor"] is None
    assert first_data["pagination"]["next_cursor"]
    first_ids = [item["id"] for item in first_data["items"]]

    second = api_client.get(
        "/api/assets",
        params={
            "q": token,
            "sort": "oldest",
            "limit": 2,
            "cursor": first_data["pagination"]["next_cursor"],
        },
        headers=auth_headers,
    )
    assert second.status_code == 200, second.text
    second_data = second.json()
    assert second_data["pagination"]["page"] == 2
    assert second_data["pagination"]["prev_cursor"]

    back = api_client.get(
        "/api/assets",
        params={
            "q": token,
            "sort": "oldest",
            "limit": 2,
            "cursor": second_data["pagination"]["prev_cursor"],
        },
        headers=auth_headers,
    )
    assert back.status_code == 200, back.text
    assert [item["id"] for item in back.json()["items"]] == first_ids

    mismatched = api_client.get(
        "/api/assets",
        params={
            "q": token,
            "sort": "oldest",
            "limit": 2,
            "tags": "different-tag",
            "cursor": first_data["pagination"]["next_cursor"],
        },
        headers=auth_headers,
    )
    assert mismatched.status_code == 400
    assert mismatched.json()["detail"]["code"] == "invalid_cursor"

    conflict = api_client.get(
        "/api/assets",
        params={
            "q": token,
            "sort": "oldest",
            "limit": 2,
            "offset": 0,
            "cursor": first_data["pagination"]["next_cursor"],
        },
        headers=auth_headers,
    )
    assert conflict.status_code == 400
    assert conflict.json()["detail"]["code"] == "invalid_pagination"

    assert api_client.get(
        "/api/assets", params={"limit": 0}, headers=auth_headers
    ).status_code == 422
    assert api_client.get(
        "/api/assets", params={"sort": "relevance"}, headers=auth_headers
    ).status_code == 422
    assert api_client.get(
        "/api/assets", params={"collection_id": 0}, headers=auth_headers
    ).status_code == 422

    oldest = api_client.get(
        "/api/assets",
        params={"q": token, "sort": "oldest", "limit": 10},
        headers=auth_headers,
    ).json()
    assert [item["id"] for item in oldest["items"]] == [asset["id"] for asset in assets]


def test_status_compatibility_and_active_only_guards(api_client, auth_headers):
    token = _token()
    tag = f"status-{token}"
    active = create_asset(token, "active", title=f"{token} active", tags=[tag])
    deleted = create_asset(token, "deleted", title=f"{token} deleted", tags=[tag])
    from keepstack.db import get_conn

    get_conn().execute("UPDATE assets SET status = 'deleted' WHERE id = ?", (deleted["id"],))
    get_conn().commit()

    active_result = api_client.get(
        "/api/assets", params={"q": token}, headers=auth_headers
    )
    assert active_result.status_code == 200
    assert [item["uuid"] for item in active_result.json()["items"]] == [active["uuid"]]

    legacy = api_client.get(
        "/api/assets",
        params={"q": token, "status": "deleted"},
        headers=auth_headers,
    )
    assert legacy.status_code == 200, legacy.text
    assert [item["uuid"] for item in legacy.json()["items"]] == [deleted["uuid"]]

    forbidden = api_client.get(
        "/api/assets",
        params={"q": token, "status": "deleted", "tags": tag},
        headers=auth_headers,
    )
    assert forbidden.status_code == 400
    assert forbidden.json()["detail"]["code"] == "active_only"


def test_api_filter_limits_and_legacy_semantic_guards(api_client, auth_headers):
    token = _token()
    create_asset(token, "validation", title=f"{token} validation")

    too_many_tags = [("tags", f"tag-{index}") for index in range(51)]
    response = api_client.get(
        "/api/assets",
        params=[("q", token), *too_many_tags],
        headers=auth_headers,
    )
    assert response.status_code == 422

    response = api_client.get(
        "/api/assets", params={"q": "x" * 513}, headers=auth_headers
    )
    assert response.status_code == 422

    semantic_tags = api_client.get(
        "/api/assets",
        params={"q": token, "mode": "semantic", "tags": "approved"},
        headers=auth_headers,
    )
    assert semantic_tags.status_code == 400
    assert semantic_tags.json()["detail"]["code"] == "unsupported_semantic_filters"

    semantic_cursor = api_client.get(
        "/api/assets",
        params={"q": token, "mode": "semantic", "cursor": "anything"},
        headers=auth_headers,
    )
    assert semantic_cursor.status_code == 400
    assert semantic_cursor.json()["detail"]["code"] == "unsupported_semantic_filters"


def test_tag_search_endpoint_is_paginated_and_case_insensitive(api_client, auth_headers):
    token = _token()
    for index in range(4):
        create_asset(
            token,
            f"tags-{index}",
            title=f"{token} tag asset {index}",
            tags=[f"{token}-tag-{index}"],
        )

    response = api_client.get(
        "/api/tags",
        params={"q": token.upper(), "limit": 2, "offset": 0},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 4
    assert len(payload["tags"]) == 2
    assert all(token in tag["name"] for tag in payload["tags"])

    second = api_client.get(
        "/api/tags",
        params={"q": token, "limit": 2, "offset": 2},
        headers=auth_headers,
    )
    assert second.status_code == 200
    assert len(second.json()["tags"]) == 2
    assert {tag["name"] for tag in payload["tags"]}.isdisjoint(
        {tag["name"] for tag in second.json()["tags"]}
    )


def test_metadata_and_tag_edits_are_visible_to_search(api_client, auth_headers):
    token = _token()
    tag = f"edited-{token}"
    asset = create_asset(token, "editable", title=f"{token} before")

    new_title = f"{token} after-{uuid.uuid4().hex[:6]}"
    response = api_client.patch(
        f"/api/assets/{asset['uuid']}",
        json={"title": new_title, "description": f"{token} new description"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text

    found = api_client.get(
        "/api/assets", params={"q": new_title}, headers=auth_headers
    )
    assert found.status_code == 200
    assert [item["uuid"] for item in found.json()["items"]] == [asset["uuid"]]

    added = api_client.post(
        f"/api/assets/{asset['uuid']}/tags",
        json={"name": tag},
        headers=auth_headers,
    )
    assert added.status_code == 200, added.text
    tagged = api_client.get(
        "/api/assets", params={"q": token, "tags": tag, "limit": 100}, headers=auth_headers
    )
    assert tagged.status_code == 200
    assert asset["uuid"] in [item["uuid"] for item in tagged.json()["items"]]

    removed = api_client.delete(
        f"/api/assets/{asset['uuid']}/tags/{tag}", headers=auth_headers
    )
    assert removed.status_code == 200, removed.text
    untagged = api_client.get(
        "/api/assets", params={"q": token, "tags": tag, "limit": 100}, headers=auth_headers
    )
    assert untagged.status_code == 200
    assert asset["uuid"] not in [item["uuid"] for item in untagged.json()["items"]]
