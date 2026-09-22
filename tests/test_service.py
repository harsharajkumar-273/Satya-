"""
Integration tests for the Satya service (src/service/app.py), exercised
through FastAPI's TestClient -- i.e. the actual HTTP layer the dashboard and
browser extension call, not the internal functions directly.

This file exists because of a real incident: POST /runs crashed with
NameError: name 'RUNS' is not defined on every single call (a leftover
reference from before the SQLite-backed RunStore replaced an in-memory
dict), and it shipped anyway because the existing test_planner_service.py
tests only called plan_run/inspect_repository/_url_smoke directly -- never
through the route. 77 tests were green while the dashboard and extension
were completely non-functional. These tests close that gap by asserting
on responses from TestClient(app), the same interface a real caller uses.
"""
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient
from storage import RunStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Fresh app + isolated SQLite store per test, so runs don't leak between tests."""
    monkeypatch.setenv("SATYA_DB_PATH", str(tmp_path / "runs.sqlite3"))
    import service.app as app_module
    app_module.STORE = RunStore(str(tmp_path / "runs.sqlite3"))
    return TestClient(app_module.app)


def _poll_until_terminal(client, run_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = client.get(f"/runs/{run_id}").json()
        if last["status"] in ("complete", "failed", "ready"):
            return last
        time.sleep(0.05)
    return last


def test_create_run_repository_mode_round_trip(client):
    """The regression case for the RUNS bug: POST /runs must actually return
    the created run, not 500."""
    r = client.post("/runs", json={"repository": "/tmp"})
    assert r.status_code == 202
    body = r.json()
    assert body["mode"] == "repository"
    assert "id" in body and body["id"]

    final = _poll_until_terminal(client, body["id"])
    assert final["status"] == "ready"
    assert final["repository_inspection"]["path"] == "/tmp"


def test_create_run_requires_url_or_repository(client):
    r = client.post("/runs", json={})
    assert r.status_code == 400


def test_get_run_not_found(client):
    r = client.get("/runs/does-not-exist")
    assert r.status_code == 404


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_dashboard_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Satya" in r.text


def test_create_run_url_mode_reaches_complete_via_mocked_browser(client):
    """Exercises the URL path end-to-end without needing a real Chromium
    install: _url_smoke is the one function that actually launches a
    browser, so it's mocked here the same way the rest of this suite avoids
    depending on Playwright being installed. Everything around it -- run
    creation, the background task, status transitions, the response the
    dashboard/extension actually parse -- is real."""
    fake_audit = {
        "url": "https://example.test", "http_status": 200, "title": "Example",
        "links": 3, "forms": 1, "console_errors": [], "failed_requests": [],
        "flows": [], "evidence": "ok",
    }
    with patch("service.app._url_smoke", return_value=fake_audit) as mock_smoke:
        r = client.post("/runs", json={"url": "https://example.test"})
        assert r.status_code == 202
        run_id = r.json()["id"]

    final = _poll_until_terminal(client, run_id)
    assert final["status"] == "complete"
    assert final["browser_audit"] == fake_audit
    mock_smoke.assert_called_once()
    assert mock_smoke.call_args[0][0] == "https://example.test"


def test_create_run_rejects_disallowed_domain_without_launching_browser(client):
    """allowed_domains is an opt-in restriction: when set, a mismatched
    domain must fail *before* a browser is ever launched."""
    with patch("service.app._url_smoke") as mock_smoke:
        r = client.post("/runs", json={
            "url": "https://evil.test/page",
            "allowed_domains": ["good.test"],
        })
        run_id = r.json()["id"]
        final = _poll_until_terminal(client, run_id)

    mock_smoke.assert_not_called()
    assert final["status"] == "failed"
    assert "evil.test" in final["message"]
