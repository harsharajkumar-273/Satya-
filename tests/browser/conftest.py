"""
Shared fixtures for the browser-driven integration suite.

Unlike tests/, everything under tests/browser/ launches a real headless
Chromium via Playwright and a real demo server, so it's slower and requires
`playwright install chromium` -- intentionally excluded from the default CI
`pytest tests/` job (see .github/workflows/tests.yml) and run separately, the
same way scripts/run_demo.py already is. Run with:

    playwright install chromium   # once
    PYTHONPATH=src pytest tests/browser/ -q
"""
from __future__ import annotations

import functools
import http.server
import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

pytest.importorskip("playwright", reason="playwright is required for tests/browser/ "
                                          "(pip install playwright && playwright install chromium)")

import uvicorn  # noqa: E402

TODOMVC_DIR = Path(os.environ.get("SATYA_TODOMVC_DIR", REPO_ROOT / "validation" / "todomvc" / "package"))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_until_up(base_url: str, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    last_exc = None
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base_url, timeout=0.5)
            return
        except Exception as exc:  # noqa: BLE001 - polling, any failure just means "not up yet"
            last_exc = exc
            time.sleep(0.1)
    raise RuntimeError(f"server at {base_url} did not come up in {timeout}s") from last_exc


@pytest.fixture
def demo_app():
    """
    Factory fixture. demo_app("ws_demo") imports src/ws_demo/app.py's `app`
    FastAPI object, serves it on a fresh ephemeral port in a daemon thread,
    waits for it to answer, and returns its base URL. A fresh port and
    thread per call keeps parallel tests (and bugs=on/off runs within one
    test) from colliding on shared mutable in-memory state.
    """
    def _start(module_name: str) -> str:
        port = _free_port()
        module = __import__(f"{module_name}.app", fromlist=["app"])
        target_app = module.app

        def serve():
            uvicorn.run(target_app, host="127.0.0.1", port=port, log_level="error")

        threading.Thread(target=serve, daemon=True).start()
        base_url = f"http://127.0.0.1:{port}"
        _wait_until_up(base_url)
        return base_url

    return _start


@pytest.fixture(scope="session")
def todomvc_base_url():
    """
    Serves the pinned TodoMVC npm snapshot's examples/ directory over HTTP.

    Requires scripts/setup_todomvc.sh to have been run once (it pins
    todomvc@0.1.1, same version docs/VALIDATION.md's findings are reproduced
    against). Skips with instructions rather than fetching over the network
    mid-test-run, so a test run is deterministic once set up.
    """
    if not (TODOMVC_DIR / "examples").is_dir():
        pytest.skip(f"TodoMVC examples not found at {TODOMVC_DIR}. "
                     f"Run scripts/setup_todomvc.sh first (see docs/FINDINGS.md).")
    port = _free_port()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(TODOMVC_DIR))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{port}"
    _wait_until_up(base_url + "/examples/vanillajs/index.html")
    yield base_url
    httpd.shutdown()
