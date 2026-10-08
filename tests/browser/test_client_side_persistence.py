"""
Client-side persistence: an app with no backend Satya can read at all. Target
is Vanilla JS TodoMVC, which persists to localStorage -- ground_truth="reload"
is the only option: Satya performs the action, reloads the page, and treats
the re-rendered rows as truth, same as it would on any site it doesn't own.

Reproduces docs/VALIDATION.md's "Vanilla JS: 5/5 AGREE" result as a pytest
assertion instead of a one-off script run.

Pinned app version: npm `todomvc@0.1.1` (an old, frozen snapshot -- see
docs/VALIDATION.md's Caveats). Reproduce:
    scripts/setup_todomvc.sh
    PYTHONPATH=src pytest tests/browser/test_client_side_persistence.py -q
"""
from __future__ import annotations

from agent.loop import safe_verify_action, summarize
from browser.agent import BrowserAgent
from cli import build_do
from reconcile.reconciler import Verdict

FLOWS = [
    {"description": "add todo 'Buy milk'", "actions": [
        {"fill": {"selector": "#new-todo", "value": "Buy milk"}},
        {"press": {"selector": "#new-todo", "key": "Enter"}}]},
    {"description": "add todo 'Walk dog'", "actions": [
        {"fill": {"selector": "#new-todo", "value": "Walk dog"}},
        {"press": {"selector": "#new-todo", "key": "Enter"}}]},
    {"description": "rename first todo to 'Buy oat milk'", "actions": [
        {"dblclick": "li[data-id] >> nth=0 >> label"},
        {"fill": {"selector": "li[data-id] >> nth=0 >> input.edit", "value": "Buy oat milk"}},
        {"press": {"selector": "li[data-id] >> nth=0 >> input.edit", "key": "Enter"}}]},
    {"description": "mark first todo complete", "actions": [
        {"check": "li[data-id] >> nth=0 >> input.toggle"}]},
    {"description": "delete last todo", "actions": [
        {"hover": "li[data-id] >> nth=-1"},
        {"click": "li[data-id] >> nth=-1 >> .destroy"}]},
]


def test_vanillajs_todomvc_all_flows_agree(todomvc_base_url):
    results = []
    with BrowserAgent(
        todomvc_base_url,
        row_selector="li[data-id]",
        toast_selector="#toast",
        id_attr="data-id",
        include_row_state=True,
    ) as agent:
        agent.goto("/examples/vanillajs/index.html")
        for flow in FLOWS:
            results.append(safe_verify_action(agent, flow["description"],
                                               build_do(flow["actions"]), ground_truth="reload"))

    verdicts = [f.verdict for r in results for f in r.findings]
    summary = summarize(results)
    assert verdicts == [Verdict.AGREE] * len(FLOWS), (
        f"expected all {len(FLOWS)} flows to AGREE on an unmodified, real "
        f"TodoMVC implementation; got {verdicts}. Summary: {summary}")
