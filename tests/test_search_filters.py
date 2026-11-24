"""Combined filters, deterministic sorting, and signed cursor pagination."""
import uuid

import pytest

from keepstack import search
from keepstack.db import get_conn, init_db
from keepstack.pagination import InvalidCursorError
from tests.fixtures_data import create_asset, create_collection


def _token() -> str:
    return "t" + uuid.uuid4().hex[:10]


@pytest.fixture(scope="module", autouse=True)
def _schema():
    init_db()


def test_combined_filters_all_include_any_exclude_and_collection():
    token = _token()
    collection = create_collection(f"Harbor {token}")
    approved = f"approved-{token}"
    campaign = f"campaign-{token}"
    internal = f"internal-{token}"

    expected = create_asset(
        token,
        "match",
        title=f"{token} expected harbor",
        tags=[approved, campaign],
        collection_id=collection,
        created_at="2026-02-01T00:00:00+00:00",
    )
    create_asset(
        token,
        "excluded",
        title=f"{token} excluded harbor",
        tags=[approved, campaign, internal],
        collection_id=collection,
        created_at="2026-02-02T00:00:00+00:00",
    )
    create_asset(
        token,
        "missing-campaign",
        title=f"{token} missing campaign",
        tags=[approved],
        collection_id=collection,
    )
    create_asset(
        token,
        "other-collection",
        title=f"{token} other collection",
        tags=[approved, campaign],
    )

    result = search.search(
        q=token,
        collection_id=collection,
        tags=[approved, campaign],
        exclude_tags=[internal],
        sort="oldest",
    )
    assert result["total"] == 1
    assert [item["uuid"] for item in result["items"]] == [expected["uuid"]]
    assert result["mode"] == "keyword"


def test_duplicate_tags_and_include_exclude_conflict():
    token = _token()
    tag = f"duplicate-{token}"
    asset = create_asset(token, "one", title=f"{token} asset", tags=[tag])

    duplicate = search.search(q=token, tags=[tag, tag.upper()])
    assert duplicate["total"] == 1
    assert duplicate["items"][0]["uuid"] == asset["uuid"]

    conflict = search.search(q=token, tags=[tag], exclude_tags=[tag])
    assert conflict["total"] == 0
    assert conflict["items"] == []

    alias = search.search(q=token, tag=tag)
    assert alias["total"] == 1


def test_unsearchable_keyword_and_unknown_references_are_empty():
    token = _token()
    create_asset(token, "one", title=f"{token} asset", tags=[f"tag-{token}"])

    assert search.search(q="!!!***", tags=[])["total"] == 0
    assert search.search(q=token, tags=[f"missing-{token}"])["total"] == 0
    assert search.search(q=token, collection_id=987654321)["total"] == 0
    assert search.search(q=token, media_type="not-a-real-type")["total"] == 0


def test_keyword_honors_sort_and_stable_cursor_pages_with_equal_timestamps():
    token = _token()
    stamp = "2026-03-01T00:00:00+00:00"
    assets = [
        create_asset(token, f"stable-{index}", title=f"{token} stable {index}", created_at=stamp)
        for index in range(7)
    ]
    expected_ids = sorted((asset["id"] for asset in assets), reverse=True)

    seen_ids: list[int] = []
    cursor = None
    page = 0
    while True:
        page += 1
        result = search.search(q=token, sort="newest", limit=2, cursor=cursor)
        assert result["total"] == 7
        assert result["pagination"]["page"] == page
        seen_ids.extend(item["id"] for item in result["items"])
        cursor = result["pagination"]["next_cursor"]
        if not cursor:
            break
        assert page < 10

    assert seen_ids == expected_ids
    assert len(seen_ids) == len(set(seen_ids)) == 7


def test_previous_cursor_reverses_order_back_to_first_page():
    token = _token()
    stamp = "2026-03-02T00:00:00+00:00"
    for index in range(5):
        create_asset(token, f"prev-{index}", title=f"{token} prev {index}", created_at=stamp)

    first = search.search(q=token, sort="oldest", limit=2)
    second = search.search(
        q=token,
        sort="oldest",
        limit=2,
        cursor=first["pagination"]["next_cursor"],
    )
    assert second["pagination"]["page"] == 2
    back = search.search(
        q=token,
        sort="oldest",
        limit=2,
        cursor=second["pagination"]["prev_cursor"],
    )
    assert back["pagination"]["page"] == 1
    assert [item["id"] for item in back["items"]] == [item["id"] for item in first["items"]]


def test_cursor_rejects_tampering_filter_changes_and_conflicting_offset():
    token = _token()
    stamp = "2026-03-03T00:00:00+00:00"
    tag = f"cursor-{token}"
    for index in range(4):
        create_asset(
            token,
            f"cursor-{index}",
            title=f"{token} cursor {index}",
            tags=[tag],
            created_at=stamp,
        )

    first = search.search(q=token, sort="oldest", limit=2, tags=[tag])
    cursor = first["pagination"]["next_cursor"]
    assert cursor

    with pytest.raises(InvalidCursorError):
        search.search(q=token, sort="oldest", limit=2, tags=[tag], cursor=cursor + "x")
    with pytest.raises(InvalidCursorError):
        search.search(q=token, sort="oldest", limit=2, tags=[f"other-{token}"], cursor=cursor)
    with pytest.raises(InvalidCursorError):
        search.search(q=token, sort="oldest", limit=3, tags=[tag], cursor=cursor)
    with pytest.raises(search.PaginationConflictError):
        search.search(q=token, sort="oldest", limit=2, tags=[tag], cursor=cursor, offset=0)


def test_offset_mode_remains_available_for_legacy_callers():
    token = _token()
    stamp = "2026-03-04T00:00:00+00:00"
    assets = [
        create_asset(token, f"offset-{index}", title=f"{token} offset {index}", created_at=stamp)
        for index in range(5)
    ]
    ordered = sorted(assets, key=lambda asset: asset["id"])
    result = search.search(q=token, sort="oldest", limit=2, offset=2)
    assert result["pagination"]["page"] == 2
    assert result["pagination"]["next_cursor"] is None
    assert [item["id"] for item in result["items"]] == [a["id"] for a in ordered[2:4]]


def test_name_and_largest_orders_are_deterministic():
    token = _token()
    first = create_asset(token, "name-1", title=f"{token} Bravo", created_at="2026-03-05T00:00:00+00:00")
    second = create_asset(token, "name-2", title=f"{token} Alpha", created_at="2026-03-05T00:00:00+00:00")
    third = create_asset(token, "name-3", title=f"{token} Charlie", created_at="2026-03-05T00:00:00+00:00")
    conn = get_conn()
    conn.execute("UPDATE assets SET size = ? WHERE id = ?", (300, first["id"]))
    conn.execute("UPDATE assets SET size = ? WHERE id = ?", (100, second["id"]))
    conn.execute("UPDATE assets SET size = ? WHERE id = ?", (500, third["id"]))
    conn.commit()

    by_name = search.search(q=token, sort="name", limit=10)
    assert [item["title"] for item in by_name["items"]] == [
        f"{token} Alpha",
        f"{token} Bravo",
        f"{token} Charlie",
    ]

    by_size = search.search(q=token, sort="largest", limit=10)
    assert [item["size"] for item in by_size["items"]] == [500, 300, 100]


def test_status_filter_default_is_active_only():
    token = _token()
    active = create_asset(token, "active", title=f"{token} active")
    deleted = create_asset(token, "deleted", title=f"{token} deleted")
    conn = get_conn()
    conn.execute("UPDATE assets SET status = 'deleted' WHERE id = ?", (deleted["id"],))
    conn.commit()

    active_only = search.search(q=token)
    assert [item["uuid"] for item in active_only["items"]] == [active["uuid"]]

    legacy = search.search(q=token, status="deleted", offset=0)
    assert [item["uuid"] for item in legacy["items"]] == [deleted["uuid"]]
