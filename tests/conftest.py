"""Shared test environment.

Set the data-directory override before any test module imports ``keepstack``.
The suite intentionally shares one throwaway catalog; individual tests use
unique names/prefixes so they do not depend on a clean database.
"""
import os
import tempfile

import pytest

os.environ.setdefault(
    "KEEPSTACK_DATA_DIR", tempfile.mkdtemp(prefix="keepstack-test-")
)
os.environ.setdefault("KEEPSTACK_AI_ENABLED", "false")


@pytest.fixture(scope="session")
def api_client():
    from fastapi.testclient import TestClient

    from keepstack.app import app

    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="session")
def auth_headers(api_client):
    from keepstack.config import config

    response = api_client.post(
        "/api/auth/login",
        json={"username": config.admin_user, "password": config.admin_password},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}
