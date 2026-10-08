"""
Delayed backend responses / eventual consistency (src/delayed_demo/app.py).
The frontend here has no bug: it only shows "Deleted!" after the server
returns 200. The server itself is eventually consistent -- DELETE is
accepted immediately but the record isn't actually removed from the read
path until DELAY_MS (900ms) later, on a background thread.

Confirmed empirically (not assumed): with no polling, Satya snapshots the
backend immediately after the action and sees the still-present record --
a FALSE POSITIVE, UI_LIED, for a UI that told the truth. With
poll_timeout >= DELAY_MS, Satya polls until the backend catches up and
correctly reports AGREE. This is the one scenario in this suite that
isn't a bug in an app -- it's a demonstration of a known Satya limitation
and its existing mitigation (agent.loop.verify_action's poll_timeout).

Pinned app version: this repository, same commit as this test file.
Reproduce: PYTHONPATH=src pytest tests/browser/test_delayed_responses.py -q
"""
from __future__ import annotations

from agent.loop import verify_action
from browser.agent import BrowserAgent
from reconcile.reconciler import Verdict


def _verdicts(result):
    return [f.verdict for f in result.findings]


def test_no_polling_misreports_an_honest_delayed_delete(demo_app):
    """Documents the false positive, it doesn't celebrate it."""
    base_url = demo_app("delayed_demo")
    with BrowserAgent(base_url) as agent:
        agent.goto("/")
        agent._page.request.post(f"{base_url}/api/reset")
        agent.goto("/")
        result = verify_action(agent, "click Delete on task 1, no poll_timeout",
                                lambda p: p.click('.del[data-id="1"]'))

    assert Verdict.UI_LIED in _verdicts(result), (
        f"expected the known false positive (UI_LIED on an honest, merely-delayed "
        f"delete) with no poll_timeout configured, got {_verdicts(result)}")


def test_polling_past_the_delay_agrees(demo_app):
    base_url = demo_app("delayed_demo")
    with BrowserAgent(base_url) as agent:
        agent.goto("/")
        agent._page.request.post(f"{base_url}/api/reset")
        agent.goto("/")
        # src/delayed_demo/app.py's DELAY_MS is 900; 1.5s/0.1s comfortably covers it.
        result = verify_action(agent, "click Delete on task 1, poll_timeout=1.5s",
                                lambda p: p.click('.del[data-id="1"]'),
                                poll_timeout=1.5, poll_interval=0.1)

    assert _verdicts(result) == [Verdict.AGREE], _verdicts(result)
