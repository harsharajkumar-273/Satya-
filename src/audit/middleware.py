"""
Passive CI Audit Middleware for Veritas.

Wraps existing Playwright Page objects or test suites without taking over
browser lifecycle management. Allows development teams to easily add Veritas
truth-checking and data leak detection to existing Playwright tests.

Example:
    def test_task_flow(page):
        auditor = VeritasAuditor(page, backend_fetch_fn=lambda: api.get_tasks())
        with auditor.audit("Delete task 1"):
            page.click("#task-1 .delete")
        auditor.assert_truthful()
"""
from __future__ import annotations

import contextlib
import json
from typing import Any, Callable

from agent.claim import BaseClaimInferrer, infer_claim
from models import ActionTrace, FlowResult, Finding, NetworkCall, UISnapshot, Verdict
from reconcile.backend import BackendSnapshot, diff_backend
from reconcile.reconciler import check_data_leak, reconcile


class VeritasAuditor:
    """
    Attaches to an existing Playwright Page instance to audit user actions
    and network responses passively or actively during tests.
    """

    def __init__(
        self,
        page: Any,
        *,
        backend_fetch_fn: Callable[[], list[dict]] | None = None,
        row_selector: str = ".task, [role='listitem'], tr[data-id]",
        toast_selector: str = "#toast, [role='status'], [role='alert'], .toast, .notification",
        id_attr: str = "data-id",
        inferrer: BaseClaimInferrer | None = None,
        api_filter: str = "/api/",
        poll_timeout: float = 0.0,
    ):
        self.page = page
        self.backend_fetch_fn = backend_fetch_fn
        self.row_selector = row_selector
        self.toast_selector = toast_selector
        self.id_attr = id_attr
        self.inferrer = inferrer
        self.api_filter = api_filter
        self.poll_timeout = poll_timeout

        self.recorded_calls: list[NetworkCall] = []
        self._action_calls: list[NetworkCall] = []
        self.results: list[FlowResult] = []

        self._attach_listener()

    def _attach_listener(self) -> None:
        """Attach Playwright response event handler if available."""
        if not hasattr(self.page, "on"):
            return

        def on_response(response: Any) -> None:
            try:
                req = response.request
                if self.api_filter and self.api_filter not in req.url:
                    return

                post = getattr(req, "post_data", None)
                body = None
                if post:
                    try:
                        body = json.loads(post)
                    except Exception:
                        body = post

                resp_body = None
                try:
                    resp_body = response.json()
                except Exception:
                    try:
                        resp_body = response.text()
                    except Exception:
                        resp_body = None

                call = NetworkCall(
                    method=req.method,
                    url=req.url,
                    request_body=body,
                    status=response.status,
                    response_body=resp_body,
                )
                self.recorded_calls.append(call)
                self._action_calls.append(call)
            except Exception:
                pass

        self.page.on("response", on_response)

    def capture_ui_snapshot(self) -> UISnapshot:
        """Extract a flow-agnostic UISnapshot from the current page state."""
        if not hasattr(self.page, "locator"):
            return UISnapshot(toast_text=None, row_count=0, row_values={})

        # Toast extraction
        toast_text: str | None = None
        try:
            toast_loc = self.page.locator(self.toast_selector).first
            if toast_loc.count() > 0:
                txt = toast_loc.text_content() or ""
                toast_text = txt.strip() or None
        except Exception:
            pass

        # Row extraction
        row_values: dict[str, str] = {}
        try:
            row_values = self.page.eval_on_selector_all(
                self.row_selector,
                f"""els => Object.fromEntries(els.map(el => {{
                    const id = el.getAttribute('{self.id_attr}') || el.id || '';
                    const inp = el.querySelector('input[type=text], textarea');
                    return [id, inp ? inp.value : el.textContent.trim()];
                }}).filter(([id]) => Boolean(id)))""",
            )
        except Exception:
            pass

        row_count = len(row_values)
        if row_count == 0:
            try:
                row_count = self.page.locator(self.row_selector).count()
            except Exception:
                row_count = 0

        return UISnapshot(
            toast_text=toast_text,
            row_count=row_count,
            row_values=row_values,
        )

    def capture_screenshot(self) -> bytes:
        """Capture screenshot bytes if page supports it."""
        try:
            if hasattr(self.page, "screenshot"):
                return self.page.screenshot()
        except Exception:
            pass
        return b""

    @contextlib.contextmanager
    def audit(
        self,
        description: str,
        *,
        backend_fetch_fn: Callable[[], list[dict]] | None = None,
    ):
        """
        Context manager wrapping a UI action. Captures before/after UI and network
        traffic, infers what the UI promised, diffs the backend, and records findings.
        """
        fetch_fn = backend_fetch_fn or self.backend_fetch_fn
        before_records = fetch_fn() if fetch_fn else []
        before_snap = BackendSnapshot.from_list(before_records)
        before_ui = self.capture_ui_snapshot()
        before_png = self.capture_screenshot()

        self._action_calls.clear()

        yield

        # Brief settle time for DOM updates & network responses
        if hasattr(self.page, "wait_for_timeout"):
            try:
                self.page.wait_for_timeout(350)
            except Exception:
                pass

        after_ui = self.capture_ui_snapshot()
        after_png = self.capture_screenshot()
        after_records = fetch_fn() if fetch_fn else []
        after_snap = BackendSnapshot.from_list(after_records)

        action_calls = list(self._action_calls)
        trace = ActionTrace(
            action=description,
            ui_before_png=before_png,
            ui_after_png=after_png,
            toast_text=after_ui.toast_text,
            dom_rows_before=before_ui.row_count,
            dom_rows_after=after_ui.row_count,
            network_calls=action_calls,
            ui_before=before_ui,
            ui_after=after_ui,
        )

        claim = infer_claim(
            before_ui,
            after_ui,
            ui_before_png=before_png,
            ui_after_png=after_png,
            action_description=description,
            inferrer=self.inferrer,
        )

        diff = diff_backend(before_snap, after_snap)
        finding = reconcile(claim, diff, trace)

        findings = [finding]
        leak = check_data_leak(trace)
        if leak:
            findings.append(leak)

        result = FlowResult(
            flow=description,
            trace=trace,
            claim_evidence=claim.evidence,
            findings=findings,
        )
        self.results.append(result)

    def check_all_leaks(self) -> list[Finding]:
        """Inspect all intercepted network traffic across the test run for data leaks."""
        dummy_trace = ActionTrace(action="Session audit", network_calls=list(self.recorded_calls))
        leak = check_data_leak(dummy_trace)
        return [leak] if leak else []

    def get_problems(self) -> list[Finding]:
        """Return all non-AGREE findings encountered across audited actions."""
        problems: list[Finding] = []
        for res in self.results:
            for f in res.findings:
                if f.verdict != Verdict.AGREE:
                    problems.append(f)
        return problems

    def assert_truthful(self) -> None:
        """Raise an AssertionError if any action resulted in a UI lie or leak."""
        problems = self.get_problems()
        if problems:
            messages = [f"[{p.verdict.value}] {p.summary} ({p.detail})" for p in problems]
            raise AssertionError(
                f"Veritas detected {len(problems)} UI truthfulness problem(s):\n  "
                + "\n  ".join(messages)
            )
