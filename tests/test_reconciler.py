"""
Tests for Veritas — claim inference, generic reconciliation, VLM inference,
eventual consistency, audit middleware, and demo-app bugs.
Run with: pytest tests/ (from repo root)
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent.claim import (
    Claim,
    ClaimKind,
    HeuristicClaimInferrer,
    UISnapshot,
    VLMClaimInferrer,
    infer_claim,
)
from agent.loop import summarize, verify_action
from audit.middleware import VeritasAuditor
from models import ActionTrace, Finding, NetworkCall, Verdict
from reconcile.backend import BackendSnapshot, diff_backend
from reconcile.reconciler import check_data_leak, reconcile


def _trace(toast, calls):
    return ActionTrace("t", b"", b"", toast, 0, 0, network_calls=calls)


# --- claim inference: core patterns ------------------------------------------

def test_infer_removal_from_toast_and_dom():
    before = UISnapshot("", 3, {"1": "a", "2": "b", "3": "c"})
    after = UISnapshot("Deleted!", 2, {"2": "b", "3": "c"})
    claim = infer_claim(before, after)
    assert claim.kind == ClaimKind.REMOVAL
    assert claim.target_id == "1"
    assert claim.success_asserted


def test_infer_mutation_and_new_value():
    before = UISnapshot("", 2, {"1": "old", "2": "b"})
    after = UISnapshot("Saved!", 2, {"1": "new", "2": "b"})
    claim = infer_claim(before, after)
    assert claim.kind == ClaimKind.MUTATION
    assert claim.target_id == "1"
    assert claim.new_value == "new"


def test_infer_creation():
    before = UISnapshot("", 2, {"1": "a", "2": "b"})
    after = UISnapshot("Added!", 3, {"1": "a", "2": "b", "3": "c"})
    claim = infer_claim(before, after)
    assert claim.kind == ClaimKind.CREATION


def test_infer_failure_toast_is_no_claim():
    before = UISnapshot("", 2, {"1": "a", "2": "b"})
    after = UISnapshot("Error, try again", 2, {"1": "a", "2": "b"})
    claim = infer_claim(before, after)
    assert claim.success_asserted is False


# --- claim inference: expanded vocabulary & negations ------------------------

def test_infer_expanded_removal_vocabulary():
    inferrer = HeuristicClaimInferrer()
    for word in ("Archived task!", "Discarded!", "Trashed item", "Destroyed"):
        kind, success = inferrer.classify_toast(word)
        assert kind == ClaimKind.REMOVAL
        assert success is True


def test_infer_expanded_mutation_vocabulary():
    inferrer = HeuristicClaimInferrer()
    for word in ("Modified successfully", "Edited title", "Changes applied", "Synced with server"):
        kind, success = inferrer.classify_toast(word)
        assert kind == ClaimKind.MUTATION
        assert success is True


def test_infer_negation_detection():
    inferrer = HeuristicClaimInferrer()
    for phrase in ("not deleted", "failed to save", "could not update", "unable to add"):
        kind, success = inferrer.classify_toast(phrase)
        assert kind == ClaimKind.NONE
        assert success is False


# --- VLM claim inference & fallback -----------------------------------------

def test_vlm_claim_inferrer_fallback_when_no_api_key():
    vlm = VLMClaimInferrer(api_key=None)
    before = UISnapshot("", 2, {"1": "a", "2": "b"})
    after = UISnapshot("Archived!", 1, {"2": "b"})
    claim = vlm.infer(before, after)
    assert claim.kind == ClaimKind.REMOVAL
    assert claim.target_id == "1"


def test_vlm_claim_inferrer_with_mocked_response():
    vlm = VLMClaimInferrer(api_key="test-key-fake")
    mock_response_data = (
        b'{"candidates": [{"content": {"parts": [{"text": "'
        b'{\\"kind\\": \\"MUTATION\\", \\"success_asserted\\": true, \\"target_id\\": \\"2\\", '
        b'\\"new_value\\": \\"Updated Title\\", \\"evidence\\": \\"VLM detected title edit\\"} '
        b'"}]}}]}'
    )

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_cm = MagicMock()
        mock_cm.read.return_value = mock_response_data
        mock_urlopen.return_value.__enter__.return_value = mock_cm

        before = UISnapshot("", 2, {"1": "a", "2": "Old"})
        after = UISnapshot("Saved!", 2, {"1": "a", "2": "Updated Title"})
        claim = vlm.infer(before, after, action_description="Edit task 2")

        assert claim.kind == ClaimKind.MUTATION
        assert claim.target_id == "2"
        assert claim.new_value == "Updated Title"
        assert claim.success_asserted is True


# --- generic reconciliation --------------------------------------------------

def test_removal_no_request_caught():
    claim = Claim(ClaimKind.REMOVAL, True, "1", None, "e")
    diff = diff_backend(BackendSnapshot.from_list([{"id": 1}]), BackendSnapshot.from_list([{"id": 1}]))
    f = reconcile(claim, diff, _trace("Deleted!", []))
    assert f.verdict == Verdict.NO_REQUEST


def test_removal_ui_lied_when_still_present():
    claim = Claim(ClaimKind.REMOVAL, True, "1", None, "e")
    diff = diff_backend(BackendSnapshot.from_list([{"id": 1}]), BackendSnapshot.from_list([{"id": 1}]))
    f = reconcile(claim, diff, _trace("Deleted!", [NetworkCall("DELETE", "/api/tasks/1", None, 200, {})]))
    assert f.verdict == Verdict.UI_LIED


def test_removal_agree_when_gone():
    claim = Claim(ClaimKind.REMOVAL, True, "1", None, "e")
    diff = diff_backend(BackendSnapshot.from_list([{"id": 1}, {"id": 2}]), BackendSnapshot.from_list([{"id": 2}]))
    f = reconcile(claim, diff, _trace("Deleted!", [NetworkCall("DELETE", "/api/tasks/1", None, 200, {})]))
    assert f.verdict == Verdict.AGREE


def test_mutation_ui_lied_when_not_persisted():
    claim = Claim(ClaimKind.MUTATION, True, "2", "New Title", "e")
    diff = diff_backend(BackendSnapshot.from_list([{"id": 2, "title": "Old"}]),
                        BackendSnapshot.from_list([{"id": 2, "title": "Old"}]))
    f = reconcile(claim, diff, _trace("Saved!", [NetworkCall("PUT", "/api/tasks/2", {}, 200, {})]))
    assert f.verdict == Verdict.UI_LIED


def test_mutation_agree_when_persisted():
    claim = Claim(ClaimKind.MUTATION, True, "2", "New Title", "e")
    diff = diff_backend(BackendSnapshot.from_list([{"id": 2, "title": "Old"}]),
                        BackendSnapshot.from_list([{"id": 2, "title": "New Title"}]))
    f = reconcile(claim, diff, _trace("Saved!", [NetworkCall("PUT", "/api/tasks/2", {"title": "New Title"}, 200, {})]))
    assert f.verdict == Verdict.AGREE


def test_mutation_agree_across_bool_type_drift():
    """A checkbox toggle: UI claims the text "true", backend stores a real bool.
    Regression test for the old str(new) == str(claim.new_value) comparison,
    which would have compared "True" to "true" and wrongly reported UI_LIED."""
    claim = Claim(ClaimKind.MUTATION, True, "2", "true", "e")
    diff = diff_backend(BackendSnapshot.from_list([{"id": 2, "done": False}]),
                        BackendSnapshot.from_list([{"id": 2, "done": True}]))
    f = reconcile(claim, diff, _trace("Saved!", [NetworkCall("PUT", "/api/tasks/2", {"done": True}, 200, {})]))
    assert f.verdict == Verdict.AGREE


def test_mutation_agree_across_numeric_type_drift():
    """UI displays "3" (a string from the DOM), backend stores the float 3.0."""
    claim = Claim(ClaimKind.MUTATION, True, "2", "3", "e")
    diff = diff_backend(BackendSnapshot.from_list([{"id": 2, "qty": 1}]),
                        BackendSnapshot.from_list([{"id": 2, "qty": 3.0}]))
    f = reconcile(claim, diff, _trace("Saved!", [NetworkCall("PUT", "/api/tasks/2", {"qty": 3.0}, 200, {})]))
    assert f.verdict == Verdict.AGREE


def test_creation_flow_generalizes_with_no_dedicated_code():
    claim = Claim(ClaimKind.CREATION, True, "3", None, "e")
    diff_lie = diff_backend(BackendSnapshot.from_list([{"id": 1}]), BackendSnapshot.from_list([{"id": 1}]))
    assert reconcile(claim, diff_lie, _trace("Added!", [NetworkCall("POST", "/api/tasks", {}, 200, {})])).verdict == Verdict.UI_LIED
    diff_ok = diff_backend(BackendSnapshot.from_list([{"id": 1}]), BackendSnapshot.from_list([{"id": 1}, {"id": 3}]))
    assert reconcile(claim, diff_ok, _trace("Added!", [NetworkCall("POST", "/api/tasks", {}, 200, {})])).verdict == Verdict.AGREE


def test_backend_error_caught():
    claim = Claim(ClaimKind.MUTATION, True, "2", "X", "e")
    diff = diff_backend(BackendSnapshot.from_list([{"id": 2, "title": "Old"}]),
                        BackendSnapshot.from_list([{"id": 2, "title": "Old"}]))
    f = reconcile(claim, diff, _trace("Saved!", [NetworkCall("PUT", "/api/tasks/2", {"title": "X"}, 500, {})]))
    assert f.verdict == Verdict.BACKEND_ERROR


# --- data leak (including nested payloads) -----------------------------------

def test_no_claim_when_nothing_observed_at_all():
    """No toast, no DOM delta: Veritas should say it has no basis for a claim,
    not silently report AGREE as if it had verified something."""
    claim = Claim(ClaimKind.NONE, False, None, None, "toast=None; no identifiable DOM delta")
    before = UISnapshot(toast_text=None, row_count=2, row_values={"1": "a", "2": "b"})
    after = UISnapshot(toast_text=None, row_count=2, row_values={"1": "a", "2": "b"})
    trace = ActionTrace("no-op click", b"", b"", None, 2, 2, ui_before=before, ui_after=after)
    diff = diff_backend(BackendSnapshot.from_list([]), BackendSnapshot.from_list([]))
    f = reconcile(claim, diff, trace)
    assert f.verdict == Verdict.NO_CLAIM


def test_agree_not_no_claim_when_toast_explains_failure():
    """A toast that says the action failed IS a signal — Veritas understood the
    flow and correctly has nothing to verify. That's AGREE, not NO_CLAIM."""
    claim = Claim(ClaimKind.NONE, False, None, None, "toast='Error: could not save'")
    before = UISnapshot(toast_text=None, row_count=1, row_values={"1": "a"})
    after = UISnapshot(toast_text="Error: could not save", row_count=1, row_values={"1": "a"})
    trace = ActionTrace("edit and save", b"", b"", "Error: could not save", 1, 1,
                         ui_before=before, ui_after=after)
    diff = diff_backend(BackendSnapshot.from_list([]), BackendSnapshot.from_list([]))
    f = reconcile(claim, diff, trace)
    assert f.verdict == Verdict.AGREE


def test_no_claim_falls_back_to_agree_without_ui_snapshots():
    """When no UISnapshot was captured at all (e.g. a caller using the lower-level
    ActionTrace directly, as several tests in this file do via _trace()), Veritas
    can't rule out a DOM delta it didn't see, so it stays conservative and reports
    AGREE rather than guessing NO_CLAIM."""
    claim = Claim(ClaimKind.NONE, False, None, None, "toast=None; no identifiable DOM delta")
    diff = diff_backend(BackendSnapshot.from_list([]), BackendSnapshot.from_list([]))
    f = reconcile(claim, diff, _trace(None, []))
    assert f.verdict == Verdict.AGREE


def test_data_leak_detected_flat():
    trace = _trace(None, [NetworkCall("GET", "/api/tasks", None, 200,
                                      [{"id": 1, "_internal_owner_email": "a@b.com"}])])
    assert check_data_leak(trace).verdict == Verdict.DATA_LEAK


def test_data_leak_detected_nested():
    trace = _trace(None, [NetworkCall("GET", "/api/user", None, 200, {
        "user": {
            "name": "Alice",
            "metadata": {"_private_key": "secret123"}
        }
    })])
    finding = check_data_leak(trace)
    assert finding is not None
    assert finding.verdict == Verdict.DATA_LEAK
    assert "_private_key" in finding.summary


def test_no_leak_when_clean():
    trace = _trace(None, [NetworkCall("GET", "/api/tasks", None, 200, [{"id": 1, "title": "x"}])])
    assert check_data_leak(trace) is None


# --- eventual consistency ---------------------------------------------------

def test_eventual_consistency_retry_success():
    """Simulate a backend where change appears on subsequent poll within timeout."""
    poll_count = 0
    records = [{"id": 1, "title": "Old"}]

    def mock_fetch():
        nonlocal poll_count
        poll_count += 1
        if poll_count >= 3:
            return [{"id": 1, "title": "Updated Title"}]
        return [{"id": 1, "title": "Old"}]

    class MockAgent:
        def act(self, desc, do):
            before = UISnapshot("", 1, {"1": "Old"})
            after = UISnapshot("Saved!", 1, {"1": "Updated Title"})
            return ActionTrace(
                desc, b"", b"", "Saved!", 1, 1,
                network_calls=[NetworkCall("PUT", "/api/tasks/1", {"title": "Updated Title"}, 200, {})],
                ui_before=before,
                ui_after=after,
            )

    result = verify_action(
        MockAgent(),
        "edit task 1",
        lambda p: None,
        backend_fetch_fn=mock_fetch,
        poll_timeout=1.0,
        poll_interval=0.05,
    )

    assert result.findings[0].verdict == Verdict.AGREE
    assert "eventual consistency" in result.findings[0].summary


def test_eventual_consistency_timeout_reports_ui_lied():
    """Simulate a backend that never reflects the edit, timing out to UI_LIED."""
    def mock_fetch():
        return [{"id": 1, "title": "Old"}]

    class MockAgent:
        def act(self, desc, do):
            before = UISnapshot("", 1, {"1": "Old"})
            after = UISnapshot("Saved!", 1, {"1": "Updated Title"})
            return ActionTrace(
                desc, b"", b"", "Saved!", 1, 1,
                network_calls=[NetworkCall("PUT", "/api/tasks/1", {"title": "Updated Title"}, 200, {})],
                ui_before=before,
                ui_after=after,
            )

    result = verify_action(
        MockAgent(),
        "edit task 1",
        lambda p: None,
        backend_fetch_fn=mock_fetch,
        poll_timeout=0.1,
        poll_interval=0.03,
    )

    assert result.findings[0].verdict == Verdict.UI_LIED


# --- VeritasAuditor middleware ----------------------------------------------

def test_veritas_auditor_middleware_flow():
    class MockPage:
        def __init__(self):
            self.handlers = []

        def on(self, event, handler):
            self.handlers.append(handler)

        def eval_on_selector_all(self, selector, script):
            return {"1": "task 1"}

        def locator(self, selector):
            m = MagicMock()
            m.first.count.return_value = 1
            m.first.text_content.return_value = "Deleted!"
            m.count.return_value = 1
            return m

    page = MockPage()
    db = [{"id": 1, "title": "task 1"}]

    auditor = VeritasAuditor(page, backend_fetch_fn=lambda: list(db))

    with auditor.audit("delete task"):
        # UI removes it locally but backend wasn't updated
        pass

    assert len(auditor.results) == 1
    problems = auditor.get_problems()
    assert len(problems) == 1
    assert problems[0].verdict == Verdict.NO_REQUEST

    with pytest.raises(AssertionError, match="Veritas detected 1 UI truthfulness problem"):
        auditor.assert_truthful()


# --- backend diff & snapshot extensions --------------------------------------

def test_backend_diff_detects_all_change_types():
    before = BackendSnapshot.from_list([{"id": 1, "t": "a"}, {"id": 2, "t": "b"}])
    after = BackendSnapshot.from_list([{"id": 2, "t": "B"}, {"id": 3, "t": "c"}])
    d = diff_backend(before, after)
    assert d.removed_ids == {"1"}
    assert d.added_ids == {"3"}
    assert "2" in d.changed
    assert d.field_changed_to("2", "B")


def test_backend_snapshot_custom_key_and_callable():
    rows = [{"uid": "u100", "val": 10}, {"uid": "u200", "val": 20}]
    s1 = BackendSnapshot.from_list(rows, id_key="uid")
    assert "u100" in s1.records

    s2 = BackendSnapshot.from_list(rows, id_key=lambda r: f"custom_{r['uid']}")
    assert "custom_u100" in s2.records


def test_demo_app_bugs_behave_as_designed():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from agent_demo.app import app
    c = TestClient(app)
    assert "_internal_owner_email" in c.get("/api/tasks?bugs=on").json()[0]
    assert "_internal_owner_email" not in c.get("/api/tasks?bugs=off").json()[0]
    c.post("/api/reset")
    c.put("/api/tasks/2", json={})
    title = [t for t in c.get("/api/tasks?bugs=off").json() if t["id"] == 2][0]["title"]
    assert title == "Review PR #42"


# --- WebSocket traffic: same reconciliation, different transport ------------

from browser.agent import ws_frame_to_call  # noqa: E402


def test_ws_frame_to_call_sent_parses_json():
    call = ws_frame_to_call("ws://x/socket", "sent", '{"op": "delete", "id": 1}')
    assert call.method == "WS_SEND"
    assert call.request_body == {"op": "delete", "id": 1}
    assert call.response_body is None


def test_ws_frame_to_call_received_parses_json():
    call = ws_frame_to_call("ws://x/socket", "received", '{"id": 1, "status": "deleted"}')
    assert call.method == "WS_RECV"
    assert call.response_body == {"id": 1, "status": "deleted"}
    assert call.request_body is None


def test_ws_frame_to_call_falls_back_to_raw_string_on_bad_json():
    call = ws_frame_to_call("ws://x/socket", "sent", "not json")
    assert call.request_body == "not json"


def test_ws_frame_to_call_binary_payload_left_opaque():
    call = ws_frame_to_call("ws://x/socket", "received", b"\x00\x01binary")
    assert call.response_body == b"\x00\x01binary"


def test_ws_frame_to_call_rejects_bad_direction():
    with pytest.raises(ValueError):
        ws_frame_to_call("ws://x/socket", "sideways", "{}")


def test_removal_agree_via_websocket_send():
    """A delete pushed over a WebSocket instead of an HTTP DELETE should be
    treated exactly like the HTTP case -- this used to be indistinguishable
    from a dropped request (NO_REQUEST) before WS frames counted as evidence
    of a mutation attempt."""
    claim = Claim(ClaimKind.REMOVAL, True, "1", None, "toast='Deleted!'; removed row id(s)=['1']")
    diff = diff_backend(BackendSnapshot.from_list([{"id": 1}]), BackendSnapshot.from_list([]))
    ws_call = ws_frame_to_call("ws://x/socket", "sent", '{"op": "delete", "id": 1}')
    f = reconcile(claim, diff, _trace("Deleted!", [ws_call]))
    assert f.verdict == Verdict.AGREE


def test_removal_no_request_when_only_a_push_was_received():
    """Receiving a WS push is not, by itself, evidence the client asked for a
    mutation -- only WS_SEND counts, the same way only certain HTTP verbs do."""
    claim = Claim(ClaimKind.REMOVAL, True, "1", None, "toast='Deleted!'; removed row id(s)=['1']")
    diff = diff_backend(BackendSnapshot.from_list([{"id": 1}]), BackendSnapshot.from_list([{"id": 1}]))
    ws_call = ws_frame_to_call("ws://x/socket", "received", '{"unrelated": true}')
    f = reconcile(claim, diff, _trace("Deleted!", [ws_call]))
    assert f.verdict == Verdict.NO_REQUEST


def test_summarize_excludes_no_claim_from_problems_but_counts_it_separately():
    from models import FlowResult

    hard_failure = FlowResult(
        flow="delete", trace=_trace("Deleted!", []),
        claim_evidence="e",
        findings=[Finding(Verdict.NO_REQUEST, "s", "d")],
    )
    inconclusive = FlowResult(
        flow="no-op", trace=_trace(None, []),
        claim_evidence="e",
        findings=[Finding(Verdict.NO_CLAIM, "s", "d")],
    )
    clean = FlowResult(
        flow="save", trace=_trace("Saved!", []),
        claim_evidence="e",
        findings=[Finding(Verdict.AGREE, "s", "d")],
    )

    summary = summarize([hard_failure, inconclusive, clean])
    assert summary["problems_found"] == 1
    assert summary["inconclusive_found"] == 1
    assert summary["findings_total"] == 3


def test_contacts_demo_app_bugs_behave_as_designed():
    """Sanity check for the second, independently-styled demo target (different
    markup/selectors/toast than agent_demo) -- same bug *classes*, different app,
    proving the fixture itself is sound before BrowserAgent ever points at it."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from contacts_demo.app import app
    c = TestClient(app)
    assert "_private_notes" in c.get("/api/contacts?bugs=on").json()[0]
    assert "_private_notes" not in c.get("/api/contacts?bugs=off").json()[0]
    c.post("/api/reset")
    c.put("/api/contacts/2?bugs=on", json={})
    phone = [x for x in c.get("/api/contacts?bugs=off").json() if x["id"] == 2][0]["phone"]
    assert phone == "555-0102"  # unchanged: bug 2 dropped the field
    c.put("/api/contacts/2?bugs=off", json={"phone": "555-9999"})
    phone = [x for x in c.get("/api/contacts?bugs=off").json() if x["id"] == 2][0]["phone"]
    assert phone == "555-9999"  # bugs off: it persists correctly
