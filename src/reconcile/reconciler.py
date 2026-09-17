"""
Reconciliation — claim-driven, flow-agnostic verification.

Given (a) a Claim the agent inferred from the UI, (b) the backend state diff
that actually occurred, and (c) the network calls that fired, decide whether the
UI told the truth.

Also inspects network responses for inadvertent backend data leaks.
"""
from __future__ import annotations

from typing import Any

from models import ActionTrace, Claim, ClaimKind, Finding, Verdict
from reconcile.backend import BackendDiff

# Re-exports for backward compatibility
__all__ = ["Verdict", "Finding", "reconcile", "check_data_leak"]

_MUTATING_METHODS = ("POST", "PUT", "PATCH", "DELETE", "WS_SEND")
# WS_SEND (see browser.agent.ws_frame_to_call) is the WebSocket analogue of a
# state-changing HTTP verb: the client sent something over the socket. WS_RECV
# (a server->client push) is deliberately excluded here, same as an HTTP GET
# would be -- it's still scanned for data leaks by check_data_leak(), just not
# treated as evidence the client attempted a mutation.


def _values_match(claimed: Any, actual: Any) -> bool:
    """
    Compare a claimed value (from the UI, usually a string) against a backend
    field value, tolerant of the type drift that's common between a DOM text
    node and a JSON field: booleans and numbers round-trip through the UI as
    strings, so a naive str(a) == str(b) misses "True" vs "true" vs "on", or
    "1" vs "1.0". Falls back to a plain string comparison (case/whitespace
    normalized) when neither side looks like a bool or a number.
    """
    if claimed is None or actual is None:
        return claimed == actual

    def _as_bool(v: Any) -> bool | None:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            low = v.strip().lower()
            if low in ("true", "1", "yes", "on"):
                return True
            if low in ("false", "0", "no", "off"):
                return False
        return None

    claimed_bool, actual_bool = _as_bool(claimed), _as_bool(actual)
    if claimed_bool is not None and actual_bool is not None:
        return claimed_bool == actual_bool

    def _as_float(v: Any) -> float | None:
        if isinstance(v, bool):
            return None  # avoid bool/int aliasing (True == 1)
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    claimed_num, actual_num = _as_float(claimed), _as_float(actual)
    if claimed_num is not None and actual_num is not None:
        return claimed_num == actual_num

    return str(claimed).strip().lower() == str(actual).strip().lower()


def _no_observable_signal(claim: Claim, trace: ActionTrace) -> bool:
    """
    True when the heuristic engine had genuinely nothing to go on: no toast
    text at all, and no visible DOM delta between before/after snapshots.
    Distinguishes "we understood this and there was no success claim" (e.g.
    an explicit failure toast) from "we have no idea what this action did."
    """
    had_toast = bool(trace.toast_text)
    if had_toast:
        return False
    if trace.ui_before is None or trace.ui_after is None:
        # No UI snapshots captured at all — can't rule out a DOM delta, so
        # don't claim "no signal"; fall back to the ordinary AGREE path.
        return False
    return trace.ui_before.row_values == trace.ui_after.row_values


def reconcile(
    claim: Claim,
    diff: BackendDiff,
    trace: ActionTrace,
    *,
    corroboration_note: str | None = None,
) -> Finding:
    """The generic reconciliation entry point. No flow-specific branches."""
    if claim.kind == ClaimKind.NONE and claim.success_asserted:
        return Finding(Verdict.NO_CLAIM, "UI asserted success without an identifiable effect.", claim.evidence)
    if claim.kind == ClaimKind.NONE or not claim.success_asserted:
        if _no_observable_signal(claim, trace):
            # Distinct from AGREE: this is not "verified and clean," it's
            # "Veritas had nothing to reason about" (no toast, no DOM delta).
            # Reporting it as AGREE would let a flow the tool never understood
            # look identical to one it actually checked.
            return Finding(
                Verdict.NO_CLAIM,
                "No toast and no DOM change were observed — Veritas could not "
                "form a claim about what this action was supposed to do.",
                claim.evidence,
            )
        return Finding(
            Verdict.AGREE,
            "UI asserted no successful change; nothing to verify.",
            claim.evidence,
        )

    mutating_calls = [c for c in trace.network_calls if c.method in _MUTATING_METHODS]
    failed_calls = [c for c in mutating_calls if c.status and c.status >= 400]

    # 1. The UI claimed success but no mutating request was sent at all.
    if not mutating_calls:
        return Finding(
            Verdict.NO_REQUEST,
            f"UI asserted a {claim.kind.value.lower()} succeeded, but no state-changing request was sent.",
            f"{claim.evidence}. No POST/PUT/PATCH/DELETE fired — the change exists only in the browser and "
            f"would vanish on refresh.",
        )

    # 2. A request fired but the backend rejected it with HTTP error, while the UI showed success.
    if failed_calls and not diff.any_change:
        codes = ", ".join(str(c.status) for c in failed_calls)
        return Finding(
            Verdict.BACKEND_ERROR,
            f"UI showed success, but the backend rejected the request ({codes}).",
            f"{claim.evidence}. Failing call(s): {codes}, and backend state did not change.",
        )

    if claim.target_id is None and claim.kind in (ClaimKind.REMOVAL, ClaimKind.CREATION):
        return Finding(Verdict.NO_CLAIM, "The affected record could not be identified.", claim.evidence)

    # 3. Does the backend diff actually contain the claimed effect?
    if claim.kind == ClaimKind.REMOVAL:
        expected_removed = set(claim.target_ids) if claim.target_ids else ({claim.target_id} if claim.target_id else set())
        if expected_removed and not expected_removed.issubset(diff.removed_ids):
            return Finding(
                Verdict.UI_LIED,
                "UI claimed records were removed, but some are still on the backend.",
                f"{claim.evidence}. Expected removed ids: {sorted(expected_removed)}; actual: {sorted(diff.removed_ids)}.",
            )
        if claim.target_id and claim.target_id not in diff.removed_ids:
            return Finding(
                Verdict.UI_LIED,
                f"UI claimed item {claim.target_id} was removed, but it is still on the backend.",
                f"{claim.evidence}. Backend removed ids this action: {sorted(diff.removed_ids) or 'none'}.",
            )
        if not claim.target_id and not diff.removed_ids:
            return Finding(
                Verdict.UI_LIED,
                "UI claimed a removal, but no record left the backend.",
                f"{claim.evidence}. Backend diff shows no removals.",
            )

    elif claim.kind == ClaimKind.CREATION:
        expected_added = set(claim.target_ids) if claim.target_ids else ({claim.target_id} if claim.target_id else set())
        if expected_added and not expected_added.issubset(diff.added_ids):
            return Finding(Verdict.UI_LIED, "UI claimed records were created, but some did not appear on the backend.", claim.evidence)
        if not diff.added_ids or (claim.target_id is not None and claim.target_id not in diff.added_ids):
            return Finding(
                Verdict.UI_LIED,
                "UI claimed a creation, but the claimed record did not appear on the backend.",
                f"{claim.evidence}. Backend diff shows no additions.",
            )

    elif claim.kind in (ClaimKind.MUTATION, ClaimKind.SUBMISSION):
        if claim.target_id is None or claim.new_value is None:
            return Finding(
                Verdict.NO_CLAIM,
                "UI asserted success, but the target or expected value could not be identified.",
                claim.evidence,
            )
        changed_here = diff.changed.get(claim.target_id, {})
        if claim.field_name is not None:
            changed_here = {k: v for k, v in changed_here.items() if k == claim.field_name}
        persisted_to_claim = any(
            _values_match(claim.new_value, new) for (_old, new) in changed_here.values()
        )
        if claim.new_value is not None and not persisted_to_claim:
            sent_values = [c.request_body for c in mutating_calls if isinstance(c.request_body, dict)]
            carried = any(
                any(_values_match(claim.new_value, v) for v in body.values())
                for body in sent_values if body
            )
            reason = (
                "the request body carried the new value but it didn't persist"
                if carried
                else "the request body never included the edited value"
            )
            return Finding(
                Verdict.UI_LIED,
                f"UI claimed item {claim.target_id} now reads {claim.new_value!r}, but the backend disagrees.",
                f"{claim.evidence}. Backend field changes for this item: {changed_here or 'none'} — {reason}.",
            )

    note = f" ({corroboration_note})" if corroboration_note else ""
    return Finding(
        Verdict.AGREE,
        f"UI's {claim.kind.value.lower()} claim is confirmed by the backend{note}.",
        f"{claim.evidence}. Backend diff corroborates the claim.",
    )


def _find_leaked_keys(data: Any, forbidden_prefixes: tuple[str, ...], path: str = "") -> list[str]:
    """Recursively search for leaked internal keys in nested JSON payloads."""
    leaked: list[str] = []
    if isinstance(data, dict):
        for k, v in data.items():
            if any(k.startswith(p) for p in forbidden_prefixes):
                leaked.append(f"{path}.{k}" if path else k)
            leaked.extend(_find_leaked_keys(v, forbidden_prefixes, f"{path}.{k}" if path else k))
    elif isinstance(data, list):
        for index, item in enumerate(data):
            leaked.extend(_find_leaked_keys(item, forbidden_prefixes, f"{path}[{index}]"))
    return sorted(set(leaked))


def check_data_leak(
    trace: ActionTrace,
    forbidden_prefixes: tuple[str, ...] = ("_internal", "_private"),
    forbidden_keys: tuple[str, ...] = (),
    allowed_paths: tuple[str, ...] = (),
) -> Finding | None:
    """Independent of any claim: did any API response carry internal-only fields?"""
    for call in trace.network_calls:
        body = call.response_body
        leaked = _find_leaked_keys(body, forbidden_prefixes)
        if forbidden_keys:
            leaked.extend(_find_leaked_keys(body, tuple(),))
            def walk(value: Any, path: str = "") -> None:
                if isinstance(value, dict):
                    for key, child in value.items():
                        current = f"{path}.{key}" if path else key
                        if key in forbidden_keys and current not in allowed_paths:
                            leaked.append(current)
                        walk(child, current)
                elif isinstance(value, list):
                    for index, child in enumerate(value):
                        walk(child, f"{path}[{index}]")
            walk(body)
        leaked = sorted(set(leaked))
        if leaked:
            return Finding(
                Verdict.DATA_LEAK,
                f"API response leaked internal field(s): {', '.join(leaked)}.",
                f"{call.method} {call.url} returned {leaked} to the client — not shown on screen, "
                f"but present in the payload.",
            )
    return None
