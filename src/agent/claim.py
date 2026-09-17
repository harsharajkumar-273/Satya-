"""
Claim inference — the piece that makes Veritas an agent rather than a test script.

Instead of a human pre-writing "a delete should remove a row from the backend",
this module reads what the UI *itself communicated* during an action and infers
a structured Claim from it: a success/failure signal (from toast/notification
text) plus an observed state-change signal (from how the DOM changed).

Supports pluggable inference engines:
- HeuristicClaimInferrer: Rule-based inference covering common UI patterns,
  expanded action vocabularies, and negation/failure detection.
- VLMClaimInferrer: Vision-Language Model inference that inspects screenshots
  and DOM state via multimodal models (e.g., Gemini) with automatic heuristic fallback.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any

from models import Claim, ClaimKind, UISnapshot

# Re-exports for backward compatibility
__all__ = ["ClaimKind", "Claim", "UISnapshot", "infer_claim", "BaseClaimInferrer",
           "HeuristicClaimInferrer", "VLMClaimInferrer"]


# Generic vocabulary for heuristic inference
_NEGATION_PREFIXES = ("not ", "no ", "failed to ", "unable to ", "couldn't ", "could not ", "never ")

_FAILURE_WORDS = (
    "error", "failed", "couldn't", "unable", "try again", "invalid",
    "rejected", "unsuccessful", "denied", "exception", "oops"
)

_REMOVAL_WORDS = (
    "deleted", "removed", "archived", "trashed", "discarded",
    "destroyed", "cleared", "erased"
)

_CREATION_WORDS = (
    "added", "created", "new", "inserted", "registered",
    "uploaded", "generated", "spawned"
)

_SUBMISSION_WORDS = (
    "sent", "submitted", "dispatched", "posted", "delivered"
)

_MUTATION_WORDS = (
    "saved", "updated", "modified", "edited", "applied",
    "changed", "renamed", "synced", "persisted", "saved changes"
)

_GENERIC_SUCCESS_WORDS = (
    "done", "success", "complete", "completed", "ok", "confirmed"
)


class BaseClaimInferrer(ABC):
    """Abstract interface for claim inference engines."""

    @abstractmethod
    def infer(
        self,
        before: UISnapshot,
        after: UISnapshot,
        *,
        ui_before_png: bytes | None = None,
        ui_after_png: bytes | None = None,
        action_description: str | None = None,
    ) -> Claim:
        """Infer a Claim from UI observations."""
        pass


class HeuristicClaimInferrer(BaseClaimInferrer):
    """Rule-based claim inferrer with expanded vocabulary and negation checks."""

    def _has_negated_word(self, text: str, word: str) -> bool:
        """Check if a word is immediately preceded by a negation."""
        for neg in _NEGATION_PREFIXES:
            if f"{neg}{word}" in text:
                return True
        return False

    def classify_toast(self, toast: str) -> tuple[ClaimKind, bool]:
        """Map free-text notification wording to a claim kind + success assertion."""
        if not toast:
            return ClaimKind.NONE, False

        t = toast.lower().strip()

        # Check for explicit failure words
        if any(w in t for w in _FAILURE_WORDS):
            return ClaimKind.NONE, False

        # Classify by intent categories while checking for negations
        matched_kind = ClaimKind.NONE
        is_negated = False

        for word in _REMOVAL_WORDS:
            if word in t:
                if self._has_negated_word(t, word):
                    is_negated = True
                else:
                    matched_kind = ClaimKind.REMOVAL
                break

        if matched_kind == ClaimKind.NONE and not is_negated:
            for word in _CREATION_WORDS:
                if word in t:
                    if self._has_negated_word(t, word):
                        is_negated = True
                    else:
                        matched_kind = ClaimKind.CREATION
                    break

        if matched_kind == ClaimKind.NONE and not is_negated:
            for word in _SUBMISSION_WORDS:
                if word in t:
                    if self._has_negated_word(t, word):
                        is_negated = True
                    else:
                        matched_kind = ClaimKind.SUBMISSION
                    break

        if matched_kind == ClaimKind.NONE and not is_negated:
            for word in _MUTATION_WORDS:
                if word in t:
                    if self._has_negated_word(t, word):
                        is_negated = True
                    else:
                        matched_kind = ClaimKind.MUTATION
                    break

        if is_negated:
            return ClaimKind.NONE, False

        success = (matched_kind != ClaimKind.NONE) or any(w in t for w in _GENERIC_SUCCESS_WORDS)
        return matched_kind, success

    def infer(
        self,
        before: UISnapshot,
        after: UISnapshot,
        *,
        ui_before_png: bytes | None = None,
        ui_after_png: bytes | None = None,
        action_description: str | None = None,
    ) -> Claim:
        toast_text = after.toast_text or ""
        toast_kind, success = self.classify_toast(toast_text)

        before_rows = before.row_values or {}
        after_rows = after.row_values or {}

        removed_ids = set(before_rows) - set(after_rows)
        added_ids = set(after_rows) - set(before_rows)
        changed_ids = {
            rid for rid in (set(before_rows) & set(after_rows))
            if before_rows[rid] != after_rows[rid]
        }

        # 1. Removal
        if toast_kind == ClaimKind.REMOVAL or (toast_kind == ClaimKind.NONE and removed_ids):
            target = next(iter(removed_ids), None) if len(removed_ids) == 1 else None
            return Claim(
                kind=ClaimKind.REMOVAL,
                success_asserted=success or bool(removed_ids),
                target_id=target,
                target_ids=sorted(removed_ids),
                operation="remove",
                new_value=None,
                evidence=(f"toast={after.toast_text!r}; DOM rows {before.row_count}->{after.row_count}; "
                          f"removed row id(s)={sorted(removed_ids) or 'none'}"),
            )

        # 2. Creation
        if toast_kind == ClaimKind.CREATION or (toast_kind == ClaimKind.NONE and added_ids):
            target = next(iter(added_ids), None) if len(added_ids) == 1 else None
            return Claim(
                kind=ClaimKind.CREATION,
                success_asserted=success or bool(added_ids),
                target_id=target,
                target_ids=sorted(added_ids),
                operation="create",
                new_value=None,
                evidence=f"toast={after.toast_text!r}; added row id(s)={sorted(added_ids) or 'none'}",
            )

        # 3. Mutation / Submission
        if toast_kind in (ClaimKind.MUTATION, ClaimKind.SUBMISSION) or changed_ids:
            target = next(iter(changed_ids), None) if len(changed_ids) == 1 else None
            new_val = after_rows.get(target) if target else None
            kind = toast_kind if toast_kind != ClaimKind.NONE else ClaimKind.MUTATION
            return Claim(
                kind=kind,
                success_asserted=success or bool(changed_ids),
                target_id=target,
                new_value=new_val,
                target_ids=sorted(changed_ids),
                operation="mutate",
                evidence=(f"toast={after.toast_text!r}; changed row id(s)={sorted(changed_ids) or 'none'}; "
                          f"new value={new_val!r}"),
            )

        # 4. No actionable state change
        return Claim(
            kind=ClaimKind.NONE,
            success_asserted=success,
            target_id=None,
            new_value=None,
            evidence=f"toast={after.toast_text!r}; no identifiable DOM delta",
        )


class VLMClaimInferrer(BaseClaimInferrer):
    """
    Vision-Language Model claim inferrer.
    
    Inspects action description, toast text, DOM deltas, and before/after screenshots
    to infer user intent and UI promises using multimodal models (Gemini API).
    Falls back gracefully to HeuristicClaimInferrer if unconfigured or unreachable.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gemini-2.5-flash",
        fallback: BaseClaimInferrer | None = None,
    ):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.model = model
        self.fallback = fallback or HeuristicClaimInferrer()

    def infer(
        self,
        before: UISnapshot,
        after: UISnapshot,
        *,
        ui_before_png: bytes | None = None,
        ui_after_png: bytes | None = None,
        action_description: str | None = None,
    ) -> Claim:
        if not self.api_key:
            return self.fallback.infer(
                before, after,
                ui_before_png=ui_before_png,
                ui_after_png=ui_after_png,
                action_description=action_description,
            )

        try:
            return self._call_vlm(
                before, after,
                ui_before_png=ui_before_png,
                ui_after_png=ui_after_png,
                action_description=action_description,
            )
        except Exception as exc:
            claim = self.fallback.infer(
                before, after,
                ui_before_png=ui_before_png,
                ui_after_png=ui_after_png,
                action_description=action_description,
            )
            claim.evidence += f" [VLM fallback triggered: {exc}]"
            return claim

    def _call_vlm(
        self,
        before: UISnapshot,
        after: UISnapshot,
        *,
        ui_before_png: bytes | None,
        ui_after_png: bytes | None,
        action_description: str | None,
    ) -> Claim:
        import base64

        prompt = (
            "You are an expert web UI verifier. Given the user's action and the observed UI delta, "
            "determine what the UI claimed happened.\n\n"
            f"Action performed: {action_description or 'Unknown interaction'}\n"
            f"Toast/Alert after action: {after.toast_text!r}\n"
            f"DOM row count change: {before.row_count} -> {after.row_count}\n"
            f"Row values before: {json.dumps(before.row_values)}\n"
            f"Row values after: {json.dumps(after.row_values)}\n\n"
            "Respond ONLY with a JSON object matching this schema:\n"
            "{\n"
            '  "kind": "REMOVAL" | "MUTATION" | "CREATION" | "SUBMISSION" | "NONE",\n'
            '  "success_asserted": boolean,\n'
            '  "target_id": string | null,\n'
            '  "new_value": string | null,\n'
            '  "evidence": string\n'
            "}"
        )

        parts: list[dict[str, Any]] = [{"text": prompt}]

        if ui_before_png:
            parts.append({
                "inline_data": {
                    "mime_type": "image/png",
                    "data": base64.b64encode(ui_before_png).decode("utf-8")
                }
            })
        if ui_after_png:
            parts.append({
                "inline_data": {
                    "mime_type": "image/png",
                    "data": base64.b64encode(ui_after_png).decode("utf-8")
                }
            })

        req_data = {
            "contents": [{"parts": parts}],
            "generationConfig": {"response_mime_type": "application/json"}
        }

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        req = urllib.request.Request(
            url,
            data=json.dumps(req_data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(raw_text)

        kind_str = parsed.get("kind", "NONE").upper()
        kind = ClaimKind(kind_str) if kind_str in ClaimKind.__members__ else ClaimKind.NONE

        return Claim(
            kind=kind,
            success_asserted=bool(parsed.get("success_asserted", False)),
            target_id=parsed.get("target_id"),
            new_value=parsed.get("new_value"),
            evidence=parsed.get("evidence", "VLM inferred claim"),
            confidence=0.95,
        )


_DEFAULT_INFERRER: BaseClaimInferrer = HeuristicClaimInferrer()


def set_default_inferrer(inferrer: BaseClaimInferrer) -> None:
    global _DEFAULT_INFERRER
    _DEFAULT_INFERRER = inferrer


def infer_claim(
    before: UISnapshot,
    after: UISnapshot,
    *,
    ui_before_png: bytes | None = None,
    ui_after_png: bytes | None = None,
    action_description: str | None = None,
    inferrer: BaseClaimInferrer | None = None,
) -> Claim:
    """
    Infer what the UI claims happened from observable signals.
    Delegates to the configured inferrer (defaults to HeuristicClaimInferrer).
    """
    engine = inferrer or _DEFAULT_INFERRER
    return engine.infer(
        before, after,
        ui_before_png=ui_before_png,
        ui_after_png=ui_after_png,
        action_description=action_description,
    )
