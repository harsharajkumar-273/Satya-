"""
Optimistic UI that never rolls back a rejected request (src/optimistic_demo/app.py).
The UI adds the row and shows "Added!" before the POST even fires. When the
server rejects it (422, title too long), bugs=on never checks the response;
bugs=off removes the optimistic row and shows an error toast instead.

Confirmed empirically (not assumed) by running this against the demo app:
bugs=on -> BACKEND_ERROR (claim of success, a failed mutating call, no
backend change). bugs=off -> AGREE (the UI's own claim becomes "nothing
succeeded", which matches reality, so there's nothing to disagree about).

Pinned app version: this repository, same commit as this test file.
Reproduce: PYTHONPATH=src pytest tests/browser/test_optimistic_ui_failure.py -q
"""
from __future__ import annotations

from agent.loop import verify_action
from browser.agent import BrowserAgent
from reconcile.reconciler import Verdict

LONG_TITLE = "x" * 50  # over optimistic_demo.app.MAX_TITLE_LEN (40)
SHORT_TITLE = "Buy milk"


def _verdicts(result):
    return [f.verdict for f in result.findings]


def test_no_rollback_on_rejection_is_caught_as_backend_error(demo_app):
    base_url = demo_app("optimistic_demo")
    with BrowserAgent(base_url) as agent:
        agent.goto("/?bugs=on")
        agent._page.request.post(f"{base_url}/api/reset")
        agent.goto("/?bugs=on")
        result = verify_action(
            agent, "add a task with a title over the length limit",
            lambda p: (p.fill("#new-title", LONG_TITLE), p.click("#add-btn")))

    assert Verdict.BACKEND_ERROR in _verdicts(result), (
        f"the optimistic row and 'Added!' toast stand uncorrected after a 422; "
        f"expected BACKEND_ERROR, got {_verdicts(result)}")


def test_rollback_on_rejection_agrees(demo_app):
    base_url = demo_app("optimistic_demo")
    with BrowserAgent(base_url) as agent:
        agent.goto("/?bugs=off")
        agent._page.request.post(f"{base_url}/api/reset")
        agent.goto("/?bugs=off")
        result = verify_action(
            agent, "add a task with a title over the length limit",
            lambda p: (p.fill("#new-title", LONG_TITLE), p.click("#add-btn")))

    assert _verdicts(result) == [Verdict.AGREE], _verdicts(result)


def test_successful_optimistic_add_agrees_in_both_modes(demo_app):
    for bugs in ("on", "off"):
        base_url = demo_app("optimistic_demo")
        with BrowserAgent(base_url) as agent:
            agent.goto(f"/?bugs={bugs}")
            agent._page.request.post(f"{base_url}/api/reset")
            agent.goto(f"/?bugs={bugs}")
            result = verify_action(
                agent, f"add a short task title (bugs={bugs})",
                lambda p: (p.fill("#new-title", SHORT_TITLE), p.click("#add-btn")))
        assert _verdicts(result) == [Verdict.AGREE], (bugs, _verdicts(result))
