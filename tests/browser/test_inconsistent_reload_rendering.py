"""
Inconsistent reload rendering: the Sammy.js TodoMVC build has a real,
pre-existing render race (confirmed by reading its source -- see
docs/VALIDATION.md). After a refresh, `#todo-list` sometimes renders empty
even though every todo is still in localStorage, because the item template
and the list template are fetched asynchronously and the route handler can
render before the list template arrives. Satya's reload-mode ground truth
reloads the page to check persistence, so it is exactly the kind of check
this race can fool -- which is why docs/VALIDATION.md's false-positive
fixes (confirm_reloads, _stable_render) exist. This test proves the race is
still genuinely reproducible against the pinned snapshot, not merely
described in prose.

Slow and *intentionally* flaky in a specific, bounded way: the race fires on
roughly 1 in 6 reloads (docs/VALIDATION.md), so this runs the same 5-flow
sequence 6 independent times (fresh browser context each time, matching the
"6 full runs" the docs findings were measured against) and asserts the race
shows up at least once -- not that it shows up on every run, which would be
asserting away the nondeterminism that IS the finding. Expect ~90-120s.

Pinned app version: npm `todomvc@0.1.1` (sammyjs example). Reproduce:
    scripts/setup_todomvc.sh
    PYTHONPATH=src pytest tests/browser/test_inconsistent_reload_rendering.py -q -s
"""
from __future__ import annotations

from agent.loop import safe_verify_action
from browser.agent import BrowserAgent
from cli import build_do
from reconcile.reconciler import Verdict

TRIALS = 6

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


def _run_one_trial(todomvc_base_url) -> list[Verdict]:
    with BrowserAgent(
        todomvc_base_url,
        row_selector="li[data-id]",
        toast_selector="#toast",
        id_attr="data-id",
        include_row_state=True,
    ) as agent:
        agent.goto("/examples/sammyjs/index.html")
        results = [safe_verify_action(agent, flow["description"], build_do(flow["actions"]),
                                       ground_truth="reload") for flow in FLOWS]
    return [f.verdict for r in results for f in r.findings]


def test_sammyjs_render_race_is_reproducible(todomvc_base_url):
    unstable_runs = 0
    per_trial = []
    for trial in range(TRIALS):
        verdicts = _run_one_trial(todomvc_base_url)
        hit = Verdict.UNSTABLE_RENDER in verdicts
        unstable_runs += hit
        per_trial.append(verdicts)
        print(f"  trial {trial}: {'UNSTABLE_RENDER' if hit else 'clean'} -> {verdicts}")

    print(f"UNSTABLE_RENDER in {unstable_runs}/{TRIALS} full runs "
          f"(docs/VALIDATION.md's own measurement: 3/6)")
    assert unstable_runs >= 1, (
        f"expected the documented Sammy.js render race to reproduce at least once "
        f"across {TRIALS} independent runs; got 0/{TRIALS}. This doesn't necessarily "
        f"mean the bug is gone -- it's a ~1-in-6-reload race, so 0/6 happens by chance "
        f"too; re-run before concluding it regressed. Trials: {per_trial}")
