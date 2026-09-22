"""
Tests for reload-persistence ground truth (verify_action(ground_truth="reload")).

This is the mode that lets Satya check an app it has no backend access to:
reload the page before and after the action and treat the re-rendered rows as
the truth. A fake agent stands in for BrowserAgent so these run without a
browser; the live-browser proof lives in docs/VALIDATION.md.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent.loop import verify_action
from models import ActionTrace, NetworkCall, UISnapshot, Verdict


class FakeAgent:
    """Minimal BrowserAgent stand-in: scripted persisted states and one action trace."""

    def __init__(self, persisted_states, before_rows, after_rows, toast, calls=()):
        self._persisted = list(persisted_states)
        self.reloads = 0
        self._before = UISnapshot(None, len(before_rows), dict(before_rows))
        self._after = UISnapshot(toast, len(after_rows), dict(after_rows))
        self._toast = toast
        self._calls = list(calls)

    def persisted_records(self):
        self.reloads += 1
        # Script runs out -> keep rendering the last state (a deterministic page).
        rows = self._persisted.pop(0) if len(self._persisted) > 1 else self._persisted[0]
        return [{"id": k, "value": v} for k, v in rows.items()]

    def api_get(self, path):  # must never be used in reload mode
        raise AssertionError("reload mode must not read a backend API")

    def act(self, description, do):
        return ActionTrace(description, b"", b"", self._toast,
                           self._before.row_count, self._after.row_count,
                           network_calls=self._calls,
                           ui_before=self._before, ui_after=self._after)


ROWS = {"1": "Write proposal", "2": "Review PR"}


def _run(agent):
    return verify_action(agent, "flow", lambda p: None, ground_truth="reload")


def test_fake_delete_reappears_after_reload_is_no_request():
    agent = FakeAgent([ROWS, ROWS, ROWS], ROWS, {"2": "Review PR"}, "Deleted!")
    result = _run(agent)
    assert result.findings[0].verdict == Verdict.NO_REQUEST
    assert "reload" in result.findings[0].summary
    # twice before (a stable starting render), once after (the truth), plus 2 confirmations
    assert agent.reloads == 5
    assert "Confirmed identical across 3 consecutive reloads" in result.findings[0].detail


def test_client_side_delete_that_persists_is_agree_not_no_request():
    """Apps can legitimately persist to localStorage -- no request is not a lie
    if the change survives a reload."""
    after = {"2": "Review PR"}
    agent = FakeAgent([ROWS, ROWS, after], ROWS, after, "Deleted!")
    finding = _run(agent).findings[0]
    assert finding.verdict == Verdict.AGREE
    assert "client-side" in finding.detail


def test_save_that_sends_request_but_does_not_persist_is_ui_lied():
    edited = {"1": "Write proposal", "2": "Review PR (updated)"}
    put = NetworkCall("PUT", "http://x/api/tasks/2", {}, 200, {})
    agent = FakeAgent([ROWS, ROWS, ROWS], ROWS, edited, "Saved!", calls=[put])
    finding = _run(agent).findings[0]
    assert finding.verdict == Verdict.UI_LIED
    assert "reload" in finding.detail


def test_save_that_persists_is_agree():
    edited = {"1": "Write proposal", "2": "Review PR (updated)"}
    put = NetworkCall("PUT", "http://x/api/tasks/2", {"title": "Review PR (updated)"}, 200, {})
    agent = FakeAgent([ROWS, ROWS, edited], ROWS, edited, "Saved!", calls=[put])
    finding = _run(agent).findings[0]
    assert finding.verdict == Verdict.AGREE
    assert "survived a page reload" in finding.summary


def test_creation_that_vanishes_on_reload_is_caught():
    created = dict(ROWS, **{"3": "New task"})
    agent = FakeAgent([ROWS, ROWS, ROWS], ROWS, created, "Added!")
    assert _run(agent).findings[0].verdict == Verdict.NO_REQUEST


def test_invalid_ground_truth_rejected():
    agent = FakeAgent([ROWS, ROWS, ROWS], ROWS, ROWS, None)
    with pytest.raises(ValueError):
        verify_action(agent, "flow", lambda p: None, ground_truth="vibes")


def test_flaky_render_is_unstable_render_not_no_request():
    """The TodoMVC/Sammy.js case from docs/VALIDATION.md: the change *was*
    persisted, but the page sometimes renders an empty list after a refresh."""
    created = dict(ROWS, **{"3": "New task"})
    # before-reload, after-reload (empty: the race), then two confirmations that render fine
    agent = FakeAgent([ROWS, ROWS, {}, created, created], ROWS, created, "Added!")
    finding = _run(agent).findings[0]
    assert finding.verdict == Verdict.UNSTABLE_RENDER
    assert "2 of 3 reloads" in finding.summary
    assert "[0, 3, 3]" in finding.detail


def test_confirm_reloads_can_be_disabled():
    agent = FakeAgent([ROWS, ROWS, ROWS], ROWS, {"2": "Review PR"}, "Deleted!")
    result = verify_action(agent, "flow", lambda p: None, ground_truth="reload", confirm_reloads=0)
    assert result.findings[0].verdict == Verdict.NO_REQUEST
    assert agent.reloads == 3


def test_safe_verify_action_turns_a_broken_step_into_action_failed():
    from agent.loop import safe_verify_action, summarize

    class Broken(FakeAgent):
        def act(self, description, do):
            raise TimeoutError('Page.click: Timeout 10000ms exceeded.\nCall log: ...')

    agent = Broken([ROWS, ROWS], ROWS, ROWS, None)
    result = safe_verify_action(agent, "click a row that isn't there", lambda p: None,
                                ground_truth="reload")
    assert result.findings[0].verdict == Verdict.ACTION_FAILED
    assert "Timeout 10000ms" in result.findings[0].detail
    assert "Call log" not in result.findings[0].detail  # first line only
    s = summarize([result])
    assert s["problems_found"] == 0 and s["inconclusive_found"] == 1


def test_unstable_pre_render_uses_majority_and_is_reported():
    """If the page renders differently before the action, Satya reloads until it
    gets the majority render, acts on that, and flags the instability."""
    after = {"2": "Review PR"}
    # pre-render: ROWS, {} (glitch), ROWS -> majority ROWS; after: deleted; confirmations deleted
    agent = FakeAgent([ROWS, {}, ROWS, after], ROWS, after, "Deleted!")
    result = _run(agent)
    verdicts = [f.verdict for f in result.findings]
    assert verdicts[0] == Verdict.AGREE          # the delete itself is verified against the good render
    assert Verdict.UNSTABLE_RENDER in verdicts   # ...and the flaky refresh is still a reported bug
    assert "[2, 0, 2]" in result.findings[-1].detail
