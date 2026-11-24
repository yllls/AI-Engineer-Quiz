"""User administration lifecycle, validation, and session invalidation."""
import io
import uuid

import pytest
from PIL import Image

from keepstack.db import get_conn, init_db


def _token() -> str:
    return "user" + uuid.uuid4().hex[:8]


def _login(api_client, username: str, password: str):
    return api_client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )


def _image() -> bytes:
    image = Image.new("RGB", (24, 24), (12, 90, 180))
    image.putpixel((3, 3), (240, 40, 40))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture(scope="module", autouse=True)
def _schema():
    init_db()


def test_user_edit_password_deactivation_and_session_revocation(api_client, auth_headers):
    username = _token()
    password = "start-pass-1"
    created = api_client.post(
        "/api/users",
        json={
            "username": username,
            "password": password,
            "role": "contributor",
            "email": f"{username}@example.org",
            "full_name": "Before Edit",
            "is_active": True,
        },
        headers=auth_headers,
    )
    assert created.status_code == 200, created.text
    uid = created.json()["id"]

    login = _login(api_client, username, password)
    assert login.status_code == 200, login.text
    user_token = login.json()["token"]
    user_headers = {"Authorization": f"Bearer {user_token}"}

    forbidden = api_client.get("/api/users", headers=user_headers)
    assert forbidden.status_code == 403

    invalid_role = api_client.patch(
        f"/api/users/{uid}", json={"role": "owner"}, headers=auth_headers
    )
    assert invalid_role.status_code == 422
    assert invalid_role.json()["detail"]["code"] == "invalid_role"

    immutable = api_client.patch(
        f"/api/users/{uid}", json={"username": "renamed"}, headers=auth_headers
    )
    assert immutable.status_code == 422
    assert immutable.json()["detail"]["code"] == "username_immutable"

    edited = api_client.patch(
        f"/api/users/{uid}",
        json={"email": "after@example.org", "full_name": "After Edit", "role": "editor"},
        headers=auth_headers,
    )
    assert edited.status_code == 200, edited.text
    users = api_client.get("/api/users", headers=auth_headers).json()["users"]
    row = next(item for item in users if item["id"] == uid)
    assert row["email"] == "after@example.org"
    assert row["full_name"] == "After Edit"
    assert row["role"] == "editor"

    reset = api_client.patch(
        f"/api/users/{uid}", json={"password": "new-pass-2"}, headers=auth_headers
    )
    assert reset.status_code == 200, reset.text
    assert api_client.get("/api/auth/me", headers=user_headers).status_code == 401
    assert _login(api_client, username, password).status_code == 401
    new_login = _login(api_client, username, "new-pass-2")
    assert new_login.status_code == 200, new_login.text
    new_headers = {"Authorization": f"Bearer {new_login.json()['token']}"}

    disabled = api_client.patch(
        f"/api/users/{uid}", json={"is_active": False}, headers=auth_headers
    )
    assert disabled.status_code == 200, disabled.text
    assert api_client.get("/api/auth/me", headers=new_headers).status_code == 401
    assert _login(api_client, username, "new-pass-2").status_code == 401

    enabled = api_client.patch(
        f"/api/users/{uid}", json={"is_active": True}, headers=auth_headers
    )
    assert enabled.status_code == 200, enabled.text
    # The token issued before deactivation stays dead after re-enabling.
    assert api_client.get("/api/auth/me", headers=new_headers).status_code == 401
    assert _login(api_client, username, "new-pass-2").status_code == 200

    active_delete = api_client.delete(f"/api/users/{uid}", headers=auth_headers)
    assert active_delete.status_code == 409
    assert active_delete.json()["detail"]["code"] == "deactivate_first"

    api_client.patch(f"/api/users/{uid}", json={"is_active": False}, headers=auth_headers)
    deleted = api_client.delete(f"/api/users/{uid}", headers=auth_headers)
    assert deleted.status_code == 200, deleted.text
    assert all(item["id"] != uid for item in api_client.get("/api/users", headers=auth_headers).json()["users"])


def test_self_protection_validation_and_audit(api_client, auth_headers):
    me = api_client.get("/api/auth/me", headers=auth_headers).json()
    admin_id = me["id"]

    deactivate_self = api_client.patch(
        f"/api/users/{admin_id}", json={"is_active": False}, headers=auth_headers
    )
    assert deactivate_self.status_code == 409
    assert deactivate_self.json()["detail"]["code"] == "cannot_deactivate_self"

    demote_self = api_client.patch(
        f"/api/users/{admin_id}", json={"role": "viewer"}, headers=auth_headers
    )
    assert demote_self.status_code == 409
    assert demote_self.json()["detail"]["code"] == "cannot_change_own_role"

    delete_self = api_client.delete(f"/api/users/{admin_id}", headers=auth_headers)
    assert delete_self.status_code == 409
    assert delete_self.json()["detail"]["code"] == "cannot_delete_self"

    unknown = api_client.patch(
        f"/api/users/{admin_id}", json={"nickname": "x"}, headers=auth_headers
    )
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["code"] == "unsupported_user_fields"

    invalid_create = api_client.post(
        "/api/users",
        json={"username": _token(), "password": "x", "role": "owner"},
        headers=auth_headers,
    )
    assert invalid_create.status_code == 422
    assert invalid_create.json()["detail"]["code"] == "invalid_role"


def test_permanent_delete_clears_user_references(api_client, auth_headers):
    username = _token()
    created = api_client.post(
        "/api/users",
        json={
            "username": username,
            "password": "delete-pass-1",
            "role": "contributor",
            "is_active": True,
        },
        headers=auth_headers,
    )
    assert created.status_code == 200, created.text
    uid = created.json()["id"]

    login = _login(api_client, username, "delete-pass-1")
    user_headers = {"Authorization": f"Bearer {login.json()['token']}"}
    uploaded = api_client.post(
        "/api/assets",
        files=[("files", ("owned-by-user.jpg", _image(), "image/jpeg"))],
        data={"run_ai": "false"},
        headers=user_headers,
    )
    assert uploaded.status_code == 200, uploaded.text
    asset_uuid = uploaded.json()["created"][0]["uuid"]
    collection = api_client.post(
        "/api/collections",
        json={"name": f"Owned by {username}"},
        headers=user_headers,
    )
    assert collection.status_code == 200, collection.text
    collection_id = collection.json()["id"]

    conn = get_conn()
    asset_id = conn.execute("SELECT id FROM assets WHERE uuid = ?", (asset_uuid,)).fetchone()["id"]
    conn.execute(
        "INSERT INTO saved_searches (user_id, name, query_json, created_at) "
        "VALUES (?, 'temp search', '{}', '2026-01-01T00:00:00Z')",
        (uid,),
    )
    conn.commit()

    assert api_client.patch(
        f"/api/users/{uid}", json={"is_active": False}, headers=auth_headers
    ).status_code == 200
    assert api_client.delete(f"/api/users/{uid}", headers=auth_headers).status_code == 200

    assert conn.execute(
        "SELECT uploaded_by FROM assets WHERE id = ?", (asset_id,)
    ).fetchone()["uploaded_by"] is None
    assert conn.execute(
        "SELECT created_by FROM collections WHERE id = ?", (collection_id,)
    ).fetchone()["created_by"] is None
    assert conn.execute(
        "SELECT COUNT(*) c FROM saved_searches WHERE user_id = ?", (uid,)
    ).fetchone()["c"] == 0
    audit_entries = conn.execute(
        "SELECT username, action, detail FROM audit_log "
        "WHERE action = 'user.delete' AND target_id = ? ORDER BY id DESC LIMIT 1",
        (str(uid),),
    ).fetchone()
    assert audit_entries is not None
    assert audit_entries["detail"] == username
