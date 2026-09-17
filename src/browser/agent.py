"""
Browser agent with network interception.

This is the piece that makes the whole project possible: it drives the
page like a user (click, type) AND records every network request/response
the page makes while doing so. That second capability is the crux —
purely-visual testing can only see what the UI *renders*; by capturing the
actual HTTP traffic we can independently check what the frontend really
told (or didn't tell) the backend.

Playwright is the right tool here because it exposes both the DOM-driving
API and a request/response event stream from the same session.
"""
from __future__ import annotations

import json
from typing import Any

from models import ActionTrace, NetworkCall, UISnapshot

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None  # Playwright is optional for unit testing and models

# Re-exports for backward compatibility
__all__ = ["NetworkCall", "ActionTrace", "BrowserAgent"]


class BrowserAgent:
    """
    Wraps a Playwright page and records network traffic per action.

    Usage:
        with BrowserAgent(base_url) as agent:
            agent.goto("/")
            trace = agent.act("delete row 1", lambda p: p.click('.del[data-id="1"]'))
    """

    def __init__(
        self,
        base_url: str,
        *,
        headless: bool = True,
        width: int = 640,
        height: int = 720,
        row_selector: str = ".task",
        toast_selector: str = "#toast",
        id_attr: str = "data-id",
    ):
        if sync_playwright is None:
            raise ImportError(
                "playwright is required to use BrowserAgent. "
                "Install it with `pip install playwright && playwright install chromium`."
            )
        self.base_url = base_url.rstrip("/")
        self._headless = headless
        self._size = {"width": width, "height": height}
        self.row_selector = row_selector
        self.toast_selector = toast_selector
        self.id_attr = id_attr
        self._captured: list[NetworkCall] = []

    def __enter__(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self._headless)
        self._page = self._browser.new_page(viewport=self._size)
        self._wire_network_capture()
        return self

    def __exit__(self, *exc):
        self._browser.close()
        self._pw.stop()

    def _wire_network_capture(self):
        def on_response(response):
            req = response.request
            # only record calls to our own API, not static assets
            if "/api/" not in req.url:
                return
            body = None
            try:
                post = req.post_data
                body = json.loads(post) if post else None
            except Exception:
                body = req.post_data
            resp_body = None
            try:
                resp_body = response.json()
            except Exception:
                try:
                    resp_body = response.text()
                except Exception:
                    resp_body = None
            self._captured.append(
                NetworkCall(
                    method=req.method,
                    url=req.url,
                    request_body=body,
                    status=response.status,
                    response_body=resp_body,
                )
            )

        self._page.on("response", on_response)

    # --- navigation ---------------------------------------------------------

    def goto(self, path: str = "/"):
        self._page.goto(self.base_url + path)
        self._page.wait_for_timeout(300)
        self._captured.clear()  # drop the initial page-load traffic; we only want per-action calls

    def api_get(self, path: str):
        """Independent backend read — the 'ground truth' side of reconciliation."""
        resp = self._page.request.get(self.base_url + path)
        try:
            return resp.json()
        except Exception:
            return resp.text()

    # --- primitives ---------------------------------------------------------

    def _rows(self) -> int:
        return self._page.locator(self.row_selector).count()

    def _row_values(self) -> dict[str, str]:
        """Generic capture of every row's id -> visible content."""
        return self._page.eval_on_selector_all(
            self.row_selector,
            f"""els => Object.fromEntries(els.map(el => {{
                const id = el.getAttribute('{self.id_attr}') || el.id || '';
                const inp = el.querySelector('input[type=text], textarea');
                return [id, inp ? inp.value : el.textContent.trim()];
            }}).filter(([id]) => Boolean(id)))""",
        )

    def _toast(self) -> str | None:
        t = self._page.locator(self.toast_selector).first
        if t.count() == 0:
            return None
        txt = t.text_content() or ""
        return txt.strip() or None

    def _screenshot(self) -> bytes:
        return self._page.screenshot()

    def snapshot_ui(self) -> UISnapshot:
        """A generic UISnapshot of the current page state."""
        return UISnapshot(
            toast_text=self._toast(),
            row_count=self._rows(),
            row_values=self._row_values(),
        )

    # --- generic action -----------------------------------------------------

    def act(self, description: str, do) -> ActionTrace:
        """
        Perform an arbitrary UI interaction and capture everything observably.
        """
        before_png = self._screenshot()
        before = UISnapshot(self._toast(), self._rows(), self._row_values())
        self._captured.clear()
        do(self._page)
        self._page.wait_for_timeout(400)  # let requests fire + toast render
        after_png = self._screenshot()
        after = UISnapshot(self._toast(), self._rows(), self._row_values())
        return ActionTrace(
            action=description,
            ui_before_png=before_png,
            ui_after_png=after_png,
            toast_text=after.toast_text,
            dom_rows_before=before.row_count,
            dom_rows_after=after.row_count,
            network_calls=list(self._captured),
            ui_before=before,
            ui_after=after,
        )
