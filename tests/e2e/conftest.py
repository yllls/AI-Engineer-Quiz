"""Browser-test fixtures for the Playwright suite."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_ENV = "KEEPSTACK_E2E"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_healthy(base_url: str, process: subprocess.Popen, log_path: Path) -> None:
    deadline = time.time() + 30
    while time.time() < deadline:
        if process.poll() is not None:
            break
        try:
            with urllib.request.urlopen(base_url + "/healthz", timeout=1) as response:
                if response.read() == b"ok":
                    return
        except Exception:
            time.sleep(0.1)
    tail = ""
    if log_path.exists():
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
    raise RuntimeError(f"E2E server failed to start\n{tail}")


@pytest.fixture(scope="session")
def e2e_server():
    if os.environ.get(E2E_ENV) != "1":
        pytest.skip(f"Set {E2E_ENV}=1 to run browser tests")

    data_dir = Path(tempfile.mkdtemp(prefix="keepstack-e2e-"))
    env = os.environ.copy()
    env.update(
        {
            "KEEPSTACK_DATA_DIR": str(data_dir),
            "KEEPSTACK_AI_ENABLED": "false",
            "KEEPSTACK_OCR_ENABLED": "false",
            "KEEPSTACK_ADMIN_USER": "admin",
            "KEEPSTACK_ADMIN_PASSWORD": "admin",
        }
    )
    subprocess.run(
        [sys.executable, "-m", "tests.fixtures_data"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
    )

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = data_dir / "server.log"
    log = open(log_path, "w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "keepstack.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
            "--no-access-log",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_healthy(base_url, process, log_path)
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        log.close()
        shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def chromium():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture()
def page(chromium):
    context = chromium.new_context(
        viewport={"width": 1440, "height": 1000},
        accept_downloads=True,
    )
    browser_page = context.new_page()
    browser_page.set_default_timeout(10000)
    errors: list[str] = []

    def _page_error(exc):
        errors.append(f"pageerror: {exc}")

    def _console_error(message):
        text = message.text
        allowed = [
            "net::ERR_ABORTED",
            "the server responded with a status of 500",
            "the server responded with a status of 413",
        ]
        if message.type == "error" and not any(fragment in text for fragment in allowed):
            errors.append(f"console error: {text}")

    browser_page.on("pageerror", _page_error)
    browser_page.on("console", _console_error)
    yield browser_page
    try:
        assert not errors, "\n".join(errors)
    finally:
        context.close()


@pytest.fixture()
def logged_in_page(page, e2e_server):
    from playwright.sync_api import expect

    page.goto(e2e_server, wait_until="networkidle")
    page.fill("#login-user", "admin")
    page.fill("#login-pass", "admin")
    page.click("#login-form button[type=submit]")
    expect(page.locator("#grid .card").first).to_be_visible()
    return page
