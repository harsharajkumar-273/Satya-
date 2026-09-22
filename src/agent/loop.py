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
import math
from dataclasses import replace
from typing import Any, Callable

from agent.claim import BaseClaimInferrer, infer_claim
from models import ActionTrace, FlowResult, Finding, Verdict
from reconcile.backend import BackendSnapshot, diff_backend
from reconcile.reconciler import check_data_leak, reconcile

# Re-exports for backward compatibility
__all__ = ["FlowResult", "verify_action", "safe_verify_action", "summarize",
           "HARD_FAILURE_VERDICTS", "INCONCLUSIVE_VERDICTS"]


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
    backend_id_key: str = "id",
    field_name: str | None = None,
    forbidden_keys: tuple[str, ...] = (),
    allowed_paths: tuple[str, ...] = (),
    ground_truth: str = "api",
    confirm_reloads: int = 2,
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
        backend_id_key: Backend record key corresponding to the UI row ID.
        field_name: Optional backend field to verify for mutations.
        ground_truth: "api" (default) reads backend_read_path / backend_fetch_fn
            before and after the action. "reload" needs no backend access at
            all: it reloads the page before the action (so every flow starts
            from persisted state, not whatever a previous flow left in the DOM)
            and again after, and treats the re-rendered rows as the truth.
    """
    if ground_truth not in ("api", "reload"):
        raise ValueError(f"ground_truth must be 'api' or 'reload', got {ground_truth!r}")

    def _fetch_records() -> list[dict]:
        if ground_truth == "reload":
            return agent.persisted_records()
        if backend_fetch_fn is not None:
            return backend_fetch_fn()
        return agent.api_get(backend_read_path)

    if ground_truth == "reload":
        backend_id_key = "id"
        field_name = field_name or "value"

    validate_polling(poll_timeout, poll_interval)
    pre_renders: list[list[dict]] = []
    if ground_truth == "reload":
        # Start every flow from a *trustworthy* render of persisted state. A page
        # that renders nondeterministically after a refresh would otherwise hand
        # the action a broken starting point (e.g. an empty list), and every
        # verdict after that is noise. See _stable_render.
        before_records, pre_renders = _stable_render(_fetch_records)
        before_snap = BackendSnapshot.from_list(before_records, id_key=backend_id_key)
    else:
        before_snap = BackendSnapshot.from_list(_fetch_records(), id_key=backend_id_key)
    trace: ActionTrace = agent.act(description, do)
    after_snap = BackendSnapshot.from_list(_fetch_records(), id_key=backend_id_key)

    claim = infer_claim(
        trace.ui_before,
        trace.ui_after,
        ui_before_png=trace.ui_before_png,
        ui_after_png=trace.ui_after_png,
        action_description=description,
        inferrer=inferrer,
    )

    if field_name is not None:
        claim = replace(claim, field_name=field_name)
    diff = diff_backend(before_snap, after_snap)
    finding = reconcile(claim, diff, trace, ground_truth=ground_truth)

    finding = poll_for_agreement(
        claim, trace, before_snap, lambda: BackendSnapshot.from_list(_fetch_records(), id_key=backend_id_key),
        finding, poll_timeout=poll_timeout, poll_interval=poll_interval, ground_truth=ground_truth,
    )

    if ground_truth == "reload" and finding.verdict in (Verdict.UI_LIED, Verdict.NO_REQUEST):
        finding = _confirm_across_reloads(claim, trace, before_snap, after_snap, finding,
                                          lambda: BackendSnapshot.from_list(_fetch_records()),
                                          confirm_reloads)

    findings = [finding]
    if len({_render_key(r) for r in pre_renders}) > 1 and finding.verdict != Verdict.UNSTABLE_RENDER:
        counts = [len(r) for r in pre_renders]
        findings.append(Finding(
            Verdict.UNSTABLE_RENDER,
            "Before this flow ran, the page rendered different persisted state on consecutive reloads.",
            f"Row counts across {len(pre_renders)} reloads: {counts}. Satya re-rendered until it got the "
            "majority state before acting, but a real user refreshing this page can see missing data.",
        ))
    leak = check_data_leak(trace, forbidden_keys=forbidden_keys, allowed_paths=allowed_paths)
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
    Verdict.UNSTABLE_RENDER,
}

# "Satya couldn't reach a conclusion" -- surfaced, but doesn't fail a build
# unless the caller opts in (CLI --fail-on-inconclusive).
INCONCLUSIVE_VERDICTS = {Verdict.NO_CLAIM, Verdict.ACTION_FAILED}


def summarize(results: list[FlowResult]) -> dict[str, Any]:
    all_findings = [f for r in results for f in r.findings]
    problems = [f for f in all_findings if f.verdict in HARD_FAILURE_VERDICTS]
    inconclusive = [f for f in all_findings if f.verdict in INCONCLUSIVE_VERDICTS]
    return {
        "flows_checked": len(results),
        "findings_total": len(all_findings),
        "problems_found": len(problems),
        "inconclusive_found": len(inconclusive),
        "verdicts": [f.verdict.value for f in all_findings],
    }


def validate_polling(timeout: float, interval: float) -> None:
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("poll_timeout must be finite and non-negative")
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("poll_interval must be finite and positive")


def poll_for_agreement(claim, trace, before_snap, fetch_snapshot, finding, *,
                       poll_timeout=0.0, poll_interval=0.1, ground_truth="api"):
    """Shared bounded polling for browser flows and passive audits, including WS sends."""
    validate_polling(poll_timeout, poll_interval)
    sent = any(c.method == "WS_SEND" or (
        c.method in ("POST", "PUT", "PATCH", "DELETE") and
        c.status is not None and 200 <= c.status < 300
    ) for c in trace.network_calls)
    if finding.verdict != Verdict.UI_LIED or not sent or poll_timeout == 0:
        return finding
    start = time.monotonic()
    deadline = start + poll_timeout
    while time.monotonic() < deadline:
        time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))
        diff = diff_backend(before_snap, fetch_snapshot())
        finding = reconcile(claim, diff, trace, ground_truth=ground_truth, corroboration_note=
                            f"confirmed after {time.monotonic() - start:.2f}s eventual consistency window")
        if finding.verdict == Verdict.AGREE:
            break
    return finding


def _confirm_across_reloads(claim, trace, before_snap, after_snap, finding, fetch_snapshot, extra):
    """
    Reload ground truth is only as good as a single render. Before reporting
    "your change didn't persist", reload `extra` more times:

    - every reload shows the same thing -> the failure is confirmed (and the
      detail says how many reloads agreed);
    - some reload shows something different -> the page does not render its
      persisted state deterministically. That's UNSTABLE_RENDER: a real bug
      (a user refreshes and their data may or may not be there), but not the
      same bug as "never persisted", so it's reported separately.

    Found necessary during third-party validation: TodoMVC's Sammy.js example
    has a template-loading race that renders an empty list on ~1 in 6
    refreshes even though every todo is in localStorage (docs/VALIDATION.md).
    """
    if extra <= 0:
        return finding
    renders = [after_snap.records]
    for _ in range(extra):
        renders.append(fetch_snapshot().records)
    if all(r == renders[0] for r in renders):
        return Finding(finding.verdict, finding.summary,
                       f"{finding.detail} Confirmed identical across {len(renders)} consecutive reloads.")
    counts = [len(r) for r in renders]
    agreeing = sum(
        1 for r in renders
        if reconcile(claim, diff_backend(before_snap, BackendSnapshot(r)), trace,
                     ground_truth="reload").verdict == Verdict.AGREE
    )
    return Finding(
        Verdict.UNSTABLE_RENDER,
        "The page rendered different persisted state on consecutive reloads — the claimed change "
        f"showed up in {agreeing} of {len(renders)} reloads.",
        f"{claim.evidence}. Row counts across {len(renders)} reloads: {counts}. The data may well be "
        "saved, but the page does not reliably display it after a refresh.",
    )


def safe_verify_action(agent: Any, description: str, do: Callable, **kwargs) -> FlowResult:
    """
    verify_action, but a flow whose own actions can't be performed (a selector
    that times out, an element that never appears) yields an ACTION_FAILED
    result instead of aborting every remaining flow in the run.
    """
    try:
        return verify_action(agent, description, do, **kwargs)
    except Exception as exc:  # noqa: BLE001 -- any failure here is reported, not raised
        msg = str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__
        return FlowResult(
            flow=description,
            trace=ActionTrace(description),
            claim_evidence="(flow did not complete)",
            findings=[Finding(Verdict.ACTION_FAILED,
                              "The flow's actions could not be performed, so nothing was verified.",
                              f"{type(exc).__name__}: {msg}")],
        )


def _render_key(records: list[dict]) -> str:
    import json
    return json.dumps(sorted(records, key=lambda r: str(r.get("id"))), sort_keys=True, default=str)


def _stable_render(fetch_records: Callable[[], list[dict]], max_extra: int = 3):
    """
    Reload until the page shows a render we can trust, and report every render seen.

    Two reloads that agree -> done (the common case: one extra reload per flow).
    If they disagree, take a third and treat the majority render as the
    persisted state, then keep reloading (up to `max_extra` times) until the
    page the action will run against actually shows that majority state.
    Returns (majority_records, all_renders_seen).
    """
    renders = [fetch_records(), fetch_records()]
    if _render_key(renders[0]) == _render_key(renders[1]):
        return renders[-1], renders
    renders.append(fetch_records())
    keys = [_render_key(r) for r in renders]
    majority_key = max(set(keys), key=keys.count)
    majority = renders[keys.index(majority_key)]
    for _ in range(max_extra):
        if _render_key(renders[-1]) == majority_key:
            break
        renders.append(fetch_records())
    return majority, renders
