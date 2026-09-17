"""
The verification agent loop — flow-agnostic, with eventual consistency tolerance.

For any UI interaction the agent:
1. Snapshots backend state.
2. Performs the action (capturing UI before/after + network traffic).
3. Snapshots backend state again (with optional polling for eventually consistent systems).
4. Infers what the UI claimed from observable signals (via Heuristic or VLM inferrer).
5. Diffs the backend and reconciles the claim against reality.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from agent.claim import BaseClaimInferrer, infer_claim
from models import ActionTrace, FlowResult, Finding, Verdict
from reconcile.backend import BackendSnapshot, diff_backend
from reconcile.reconciler import check_data_leak, reconcile

# Re-exports for backward compatibility
__all__ = ["FlowResult", "verify_action", "summarize", "HARD_FAILURE_VERDICTS"]


def verify_action(
    agent: Any,  # BrowserAgent or any adapter with .api_get() and .act()
    description: str,
    do: Callable,
    *,
    backend_read_path: str = "/api/tasks?bugs=off",
    backend_fetch_fn: Callable[[], list[dict]] | None = None,
    poll_timeout: float = 0.0,
    poll_interval: float = 0.1,
    inferrer: BaseClaimInferrer | None = None,
) -> FlowResult:
    """
    Run one UI action end-to-end through the verification pipeline.

    Args:
        agent: BrowserAgent or compatible interface.
        description: Human-readable action description.
        do: Callable(page) that interacts with the UI.
        backend_read_path: Path to poll via agent.api_get().
        backend_fetch_fn: Alternative direct function returning backend records.
        poll_timeout: Max seconds to wait for backend state to converge (eventual consistency).
        poll_interval: Seconds between polls during eventual consistency window.
        inferrer: Optional custom claim inferrer (e.g. VLMClaimInferrer).
    """
    def _fetch_records() -> list[dict]:
        if backend_fetch_fn is not None:
            return backend_fetch_fn()
        return agent.api_get(backend_read_path)

    before_snap = BackendSnapshot.from_list(_fetch_records())
    trace: ActionTrace = agent.act(description, do)
    after_snap = BackendSnapshot.from_list(_fetch_records())

    claim = infer_claim(
        trace.ui_before,
        trace.ui_after,
        ui_before_png=trace.ui_before_png,
        ui_after_png=trace.ui_after_png,
        action_description=description,
        inferrer=inferrer,
    )

    diff = diff_backend(before_snap, after_snap)
    finding = reconcile(claim, diff, trace)

    # Eventual consistency retry loop:
    # If the UI asserted a change and a mutating request succeeded (HTTP 2xx),
    # but the backend hasn't reflected the change yet, poll until it does or timeout expires.
    mutating_success = any(
        c.status and 200 <= c.status < 300
        for c in trace.network_calls
        if c.method in ("POST", "PUT", "PATCH", "DELETE")
    )

    if finding.verdict == Verdict.UI_LIED and mutating_success and poll_timeout > 0:
        start_time = time.monotonic()
        while (time.monotonic() - start_time) < poll_timeout:
            time.sleep(poll_interval)
            after_snap = BackendSnapshot.from_list(_fetch_records())
            diff = diff_backend(before_snap, after_snap)
            elapsed = time.monotonic() - start_time
            retry_finding = reconcile(
                claim,
                diff,
                trace,
                corroboration_note=f"confirmed after {elapsed:.2f}s eventual consistency window",
            )
            if retry_finding.verdict == Verdict.AGREE:
                finding = retry_finding
                break

    findings = [finding]
    leak = check_data_leak(trace)
    if leak:
        findings.append(leak)

    return FlowResult(
        flow=description,
        trace=trace,
        claim_evidence=claim.evidence,
        findings=findings,
    )


# Verdicts that mean "Veritas checked this and found a real discrepancy."
# NO_CLAIM is deliberately excluded: it means "no signal to check," which is
# a coverage gap worth surfacing, not proof of a bug -- callers (like the CLI)
# should be able to fail a build on problems_found without failing it every
# time a flow has no toast and no DOM delta to reason about.
HARD_FAILURE_VERDICTS = {
    Verdict.UI_LIED,
    Verdict.NO_REQUEST,
    Verdict.BACKEND_ERROR,
    Verdict.DATA_LEAK,
}


def summarize(results: list[FlowResult]) -> dict[str, Any]:
    all_findings = [f for r in results for f in r.findings]
    problems = [f for f in all_findings if f.verdict in HARD_FAILURE_VERDICTS]
    inconclusive = [f for f in all_findings if f.verdict == Verdict.NO_CLAIM]
    return {
        "flows_checked": len(results),
        "findings_total": len(all_findings),
        "problems_found": len(problems),
        "inconclusive_found": len(inconclusive),
        "verdicts": [f.verdict.value for f in all_findings],
    }
