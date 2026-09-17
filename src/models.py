"""
Core data models and enums for Veritas.

These data structures are completely decoupled from browser automation (Playwright),
FastAPI, or external services, allowing pure unit testing and headless claim/reconciliation
logic to run in any Python environment.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ClaimKind(str, Enum):
    REMOVAL = "REMOVAL"          # UI indicates something was removed/deleted/archived
    MUTATION = "MUTATION"        # UI indicates an existing item was edited/updated
    CREATION = "CREATION"        # UI indicates something new was added
    SUBMISSION = "SUBMISSION"    # UI indicates data was sent/submitted (e.g. "Sent!")
    NONE = "NONE"                # UI communicated nothing actionable


class Verdict(str, Enum):
    AGREE = "AGREE"                    # UI claim matches backend reality
    UI_LIED = "UI_LIED"               # UI claimed success; backend disagrees
    NO_REQUEST = "NO_REQUEST"         # UI claimed an action but no API call fired at all
    BACKEND_ERROR = "BACKEND_ERROR"   # a call fired but the backend rejected it while UI showed success
    DATA_LEAK = "DATA_LEAK"           # backend returned fields the UI shouldn't have received
    NO_CLAIM = "NO_CLAIM"             # no toast and no DOM delta at all — nothing to reason about,
                                       # NOT the same as a verified-clean AGREE (see reconcile())


@dataclass
class NetworkCall:
    """Represents an intercepted HTTP request and its response."""
    method: str
    url: str
    request_body: Any = None
    status: int | None = None
    response_body: Any = None
    timestamp: float | None = None
    initiator: str | None = None
    duration_ms: float | None = None
    correlation_id: str | None = None


@dataclass
class UISnapshot:
    """A flow-agnostic capture of observable UI state at one moment."""
    toast_text: str | None
    row_count: int
    row_values: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionTrace:
    """Everything observed while performing one UI action."""
    action: str
    ui_before_png: bytes = b""
    ui_after_png: bytes = b""
    toast_text: str | None = None
    dom_rows_before: int = 0
    dom_rows_after: int = 0
    network_calls: list[NetworkCall] = field(default_factory=list)
    ui_before: UISnapshot | None = None
    ui_after: UISnapshot | None = None
    action_id: str | None = None


@dataclass
class Claim:
    """A structured assertion inferred from the UI."""
    kind: ClaimKind
    success_asserted: bool
    target_id: str | None
    new_value: str | None
    evidence: str
    confidence: float = 1.0
    field_name: str | None = None
    target_ids: list[str] = field(default_factory=list)
    operation: str | None = None


@dataclass
class Finding:
    """Verdict and explanatory detail produced by the reconciler."""
    verdict: Verdict
    summary: str
    detail: str


@dataclass
class FlowResult:
    """Full outcome of a verified UI action flow."""
    flow: str
    trace: ActionTrace
    claim_evidence: str
    findings: list[Finding]
