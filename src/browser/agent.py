"""
Browser agent with network interception.

This is the piece that makes the whole project possible: it drives the
page like a user (click, type) AND records every network request/response
the page makes while doing so — HTTP *and* WebSocket. That second capability
is the crux — purely-visual testing can only see what the UI *renders*; by
capturing the actual traffic we can independently check what the frontend
really told (or didn't tell) the backend.

Playwright is the right tool here because it exposes both the DOM-driving
API and a request/response (and WebSocket frame) event stream from the same
session.

HTTP calls and WebSocket frames both end up as NetworkCall entries so the
reconciler doesn't need to know or care which transport carried a mutation:
a WS_SEND frame counts as "a mutating request was sent" the same way a POST
does (see reconcile.reconciler._MUTATING_METHODS), which matters for any app
that pushes state changes over a socket instead of a REST call — those used
to look identical to a dropped request (NO_REQUEST) even when the app was
behaving correctly.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from models import ActionTrace, NetworkCall, UISnapshot

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None  # Playwright is optional for unit testing and models

# Re-exports for backward compatibility
__all__ = ["NetworkCall", "ActionTrace", "BrowserAgent", "ws_frame_to_call"]


def ws_frame_to_call(url: str, direction: str, payload: Any) -> NetworkCall:
    """
    Convert one captured WebSocket frame into a NetworkCall, so the reconciler
    can reason about a WS-pushed mutation exactly like HTTP traffic — no
    separate code path needed.

    direction is "sent" (client -> server: evidence the client attempted a
    mutation, parallel to an HTTP POST/PUT/PATCH/DELETE) or "received"
    (server -> client: a pushed update, also scanned for data leaks like any
    other response body). Pure function, deliberately independent of
    Playwright's WebSocket object so it's unit-testable without a browser.
    """
    if isinstance(payload, (bytes, bytearray)):
        body: Any = payload  # binary frame; left opaque rather than guessed at
    else:
        try:
            body = json.loads(payload)
        except (TypeError, ValueError):
            body = payload

    if direction == "sent":
        return NetworkCall(method="WS_SEND", url=url, request_body=body, status=None, response_body=None)
    if direction == "received":
        return NetworkCall(method="WS_RECV", url=url, request_body=None, status=None, response_body=body)
    raise ValueError(f"direction must be 'sent' or 'received', got {direction!r}")


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
        storage_state: str | dict | None = None,
        extra_http_headers: dict[str, str] | None = None,
        api_filter: str | tuple[str, ...] = "/api/",
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
        self.storage_state = storage_state
        self.extra_http_headers = extra_http_headers or {}
        self.api_filter = (api_filter,) if isinstance(api_filter, str) else tuple(api_filter)
        self._active_action_id: str | None = None
        self._captured: list[NetworkCall] = []

    def __enter__(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self._headless)
        self._page = self._browser.new_page(viewport=self._size, storage_state=self.storage_state,
                                            extra_http_headers=self.extra_http_headers)
        self._wire_network_capture()
        self._wire_websocket_capture()
        return self

    def __exit__(self, *exc):
        self._browser.close()
        self._pw.stop()

    def _wire_network_capture(self):
        def on_response(response):
            started = time.perf_counter()
            req = response.request
            # only record calls to our own API, not static assets
            if self.api_filter and not any(token in req.url for token in self.api_filter):
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
                    timestamp=time.time(),
                    duration_ms=(time.perf_counter() - started) * 1000,
                    initiator=getattr(req, "resource_type", None),
                    correlation_id=self._active_action_id,
                )
            )

        self._page.on("response", on_response)

    def _wire_websocket_capture(self):
        """
        Mirror _wire_network_capture for WebSocket traffic. Apps that push
        mutations over a socket instead of a REST call would otherwise be
        invisible to the reconciler and look like a dropped request.
        """
        def on_websocket(ws):
            url = ws.url

            def on_frame_sent(payload):
                self._captured.append(ws_frame_to_call(url, "sent", payload))

            def on_frame_received(payload):
                self._captured.append(ws_frame_to_call(url, "received", payload))

            ws.on("framesent", on_frame_sent)
            ws.on("framereceived", on_frame_received)

        self._page.on("websocket", on_websocket)

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
        action_id = uuid.uuid4().hex
        self._active_action_id = action_id
        before_png = self._screenshot()
        before = UISnapshot(self._toast(), self._rows(), self._row_values())
        self._captured.clear()
        do(self._page)
        self._page.wait_for_timeout(400)  # let requests fire + toast render
        after_png = self._screenshot()
        after = UISnapshot(self._toast(), self._rows(), self._row_values())
        result = ActionTrace(
            action=description,
            ui_before_png=before_png,
            ui_after_png=after_png,
            toast_text=after.toast_text,
            dom_rows_before=before.row_count,
            dom_rows_after=after.row_count,
            network_calls=list(self._captured),
            ui_before=before,
            ui_after=after,
            action_id=action_id,
        )
        self._active_action_id = None
        return result
