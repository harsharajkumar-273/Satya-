"""
WebSocket mutations: an app whose mutations travel over a WebSocket instead
of REST (src/ws_demo/app.py). The frontend waits for a server-sent ack frame
before updating the DOM -- it's an honest *client*. The bug is server-side:
the ack is sent unconditionally, whether or not the record was actually
removed.

Pinned app version: this repository, same commit as this test file.
Reproduce: PYTHONPATH=src pytest tests/browser/test_websocket_mutations.py -q
"""
from __future__ import annotations

from agent.loop import verify_action
from browser.agent import BrowserAgent
from reconcile.reconciler import Verdict


def _verdicts(result):
    return [f.verdict for f in result.findings]


def test_fake_ack_over_websocket_is_caught_as_ui_lied(demo_app):
    base_url = demo_app("ws_demo")
    with BrowserAgent(base_url) as agent:
        agent.goto("/?bugs=on")
        agent._page.request.post(f"{base_url}/api/reset")
        agent.goto("/?bugs=on")
        result = verify_action(agent, "click Delete on task 1",
                                lambda p: p.click('.del[data-id="1"]'))

    ws_sends = [c for c in result.trace.network_calls if c.method == "WS_SEND"]
    assert ws_sends, "expected the delete click to send a WS_SEND frame"
    assert Verdict.UI_LIED in _verdicts(result), (
        f"server ack'd a delete it never applied; expected UI_LIED, got {_verdicts(result)}")


def test_honest_ack_over_websocket_agrees(demo_app):
    base_url = demo_app("ws_demo")
    with BrowserAgent(base_url) as agent:
        agent.goto("/?bugs=off")
        agent._page.request.post(f"{base_url}/api/reset")
        agent.goto("/?bugs=off")
        result = verify_action(agent, "click Delete on task 1",
                                lambda p: p.click('.del[data-id="1"]'))

    assert _verdicts(result) == [Verdict.AGREE], _verdicts(result)
