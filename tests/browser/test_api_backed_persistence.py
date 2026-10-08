"""
API-backed persistence: Satya reads ground truth from the app's own REST API
(ground_truth="api", the default). Targets: src/agent_demo/app.py (task
manager) and src/contacts_demo/app.py (contacts) -- both pinned in this repo,
so "the app" is whatever commit this test file is checked out at.

Pinned app version: this repository, same commit as this test file.
Reproduce: PYTHONPATH=src pytest tests/browser/test_api_backed_persistence.py -q
"""
from __future__ import annotations

from agent.loop import verify_action
from browser.agent import BrowserAgent
from reconcile.reconciler import Verdict


def _verdicts(result):
    return [f.verdict for f in result.findings]


def test_task_manager_bugs_on_catches_fake_delete_and_fake_save(demo_app):
    base_url = demo_app("agent_demo")
    with BrowserAgent(base_url) as agent:
        agent.goto("/?bugs=on")
        agent._page.request.post(f"{base_url}/api/reset")
        agent.goto("/?bugs=on")

        delete_result = verify_action(agent, "click Delete on task 1",
                                       lambda p: p.click('.del[data-id="1"]'))
        save_result = verify_action(
            agent, 'edit task 2 title to "Review PR #42 (updated)" and Save',
            lambda p: (p.fill('input[data-id="2"]', "Review PR #42 (updated)"),
                       p.click('.save[data-id="2"]')))

    assert Verdict.NO_REQUEST in _verdicts(delete_result), (
        f"expected the fake delete to be caught as NO_REQUEST, got {_verdicts(delete_result)}")
    assert Verdict.UI_LIED in _verdicts(save_result), (
        f"expected the fake save to be caught as UI_LIED, got {_verdicts(save_result)}")


def test_task_manager_bugs_off_agrees(demo_app):
    base_url = demo_app("agent_demo")
    with BrowserAgent(base_url) as agent:
        agent.goto("/?bugs=off")
        agent._page.request.post(f"{base_url}/api/reset")
        agent.goto("/?bugs=off")

        delete_result = verify_action(agent, "click Delete on task 1",
                                       lambda p: p.click('.del[data-id="1"]'),
                                       backend_read_path="/api/tasks?bugs=off")
        save_result = verify_action(
            agent, 'edit task 2 title to "Review PR #42 (updated)" and Save',
            lambda p: (p.fill('input[data-id="2"]', "Review PR #42 (updated)"),
                       p.click('.save[data-id="2"]')),
            backend_read_path="/api/tasks?bugs=off")

    assert _verdicts(delete_result) == [Verdict.AGREE], _verdicts(delete_result)
    assert _verdicts(save_result) == [Verdict.AGREE], _verdicts(save_result)


def test_task_manager_data_leak_caught_when_bugs_on(demo_app):
    """
    The leak (BUG 3) rides the PUT response, not the DELETE -- a delete with
    bugs=on never sends a request at all (that's BUG 1, NO_REQUEST), so the
    edit+save flow is the one that actually exercises the leaky response.
    """
    base_url = demo_app("agent_demo")
    with BrowserAgent(base_url) as agent:
        agent.goto("/?bugs=on")
        agent._page.request.post(f"{base_url}/api/reset")
        agent.goto("/?bugs=on")
        result = verify_action(
            agent, 'edit task 2 title to "Review PR #42 (updated)" and Save',
            lambda p: (p.fill('input[data-id="2"]', "Review PR #42 (updated)"),
                       p.click('.save[data-id="2"]')))
    assert Verdict.DATA_LEAK in _verdicts(result), _verdicts(result)


def test_contacts_app_different_markup_same_verdicts(demo_app):
    """Same engine, a second app with genuinely different selectors/toast
    mechanism -- the generalization claim made concrete, not just asserted."""
    base_url = demo_app("contacts_demo")
    with BrowserAgent(
        base_url,
        row_selector=".contact",
        toast_selector=".banner",
        id_attr="data-contact-id",
    ) as agent:
        agent.goto("/?bugs=on")
        agent._page.request.post(f"{base_url}/api/reset")
        agent.goto("/?bugs=on")

        archive_result = verify_action(
            agent, "click Archive on contact 1",
            lambda p: p.click('.archive-btn[data-contact-id="1"]'),
            backend_read_path="/api/contacts?bugs=off")
        save_result = verify_action(
            agent, 'edit contact 2 phone to "555-9999" and Save',
            lambda p: (p.fill('input[data-contact-id="2"]', "555-9999"),
                       p.click('.save-btn[data-contact-id="2"]')),
            backend_read_path="/api/contacts?bugs=off")

    assert Verdict.NO_REQUEST in _verdicts(archive_result), _verdicts(archive_result)
    assert Verdict.UI_LIED in _verdicts(save_result), _verdicts(save_result)
