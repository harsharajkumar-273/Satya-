"""
End-to-end demo.

Boots the demo target app (with its hidden bugs) on a background thread,
points the verification agent at it, and prints what the agent found —
first with bugs ON (the agent should catch the UI lying), then with bugs
OFF (the agent should confirm everything agrees).

Run from the repo root:
    python scripts/run_demo.py
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import uvicorn

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent.loop import summarize, verify_action  # noqa: E402
from browser.agent import BrowserAgent  # noqa: E402
from reconcile.reconciler import Verdict  # noqa: E402
from report.html import render_html_report  # noqa: E402

PORT = 8077
BASE = f"http://127.0.0.1:{PORT}"

CONTACTS_PORT = 8078
CONTACTS_BASE = f"http://127.0.0.1:{CONTACTS_PORT}"


def _serve():
    from agent_demo.app import app
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="error")


def _serve_contacts():
    from contacts_demo.app import app
    uvicorn.run(app, host="127.0.0.1", port=CONTACTS_PORT, log_level="error")


def _print_result(r):
    print(f"\n  Flow: {r.flow}")
    print(f"    UI claimed: toast={r.trace.toast_text!r}, DOM rows {r.trace.dom_rows_before}->{r.trace.dom_rows_after}")
    net = ", ".join(f"{c.method} {c.url.split('/api')[-1]}->{c.status}" for c in r.trace.network_calls) or "(no API calls)"
    print(f"    Network:    {net}")
    for f in r.findings:
        mark = "OK " if f.verdict == Verdict.AGREE else "!! "
        print(f"    {mark}[{f.verdict.value}] {f.summary}")
        if f.detail:
            print(f"         {f.detail}")


def run_against(bugs: str):
    print("\n" + "=" * 70)
    print(f"Running verification agent against the app with bugs={bugs.upper()}")
    print("=" * 70)
    with BrowserAgent(BASE) as agent:
        agent.goto(f"/?bugs={bugs}")
        # reset backend to a known seed before each run
        agent._page.request.post(f"{BASE}/api/reset")
        agent.goto(f"/?bugs={bugs}")

        results = [
            verify_action(agent, "click Delete on task 1",
                          lambda p: p.click('.del[data-id="1"]')),
            verify_action(agent, 'edit task 2 title to "Review PR #42 (updated)" and Save',
                          lambda p: (p.fill('input[data-id="2"]', "Review PR #42 (updated)"),
                                     p.click('.save[data-id="2"]'))),
        ]
    for r in results:
        _print_result(r)

    s = summarize(results)
    print(f"\n  Summary: {s['problems_found']} problem(s) across {s['flows_checked']} flow(s) "
          f"-> verdicts: {s['verdicts']}")
    return results


def run_contacts_demo(bugs: str):
    """
    Same pipeline (BrowserAgent -> verify_action -> reconcile), pointed at a
    second app with genuinely different markup: <li class="contact"
    data-contact-id> instead of <div class="task" data-id>, and a
    `.banner[role=status]` toast instead of `#toast`. Nothing in src/agent,
    src/reconcile, or src/browser changes -- only these selector kwargs do.
    That's the generalization claim made concrete rather than just asserted.
    """
    print("\n" + "=" * 70)
    print(f"Running verification agent against the CONTACTS app (different markup) bugs={bugs.upper()}")
    print("=" * 70)
    with BrowserAgent(
        CONTACTS_BASE,
        row_selector=".contact",
        toast_selector=".banner",
        id_attr="data-contact-id",
    ) as agent:
        agent.goto(f"/?bugs={bugs}")
        agent._page.request.post(f"{CONTACTS_BASE}/api/reset")
        agent.goto(f"/?bugs={bugs}")

        results = [
            verify_action(agent, "click Archive on contact 1",
                          lambda p: p.click('.archive-btn[data-contact-id="1"]'),
                          backend_read_path="/api/contacts?bugs=off"),
            verify_action(agent, 'edit contact 2 phone to "555-9999" and Save',
                          lambda p: (p.fill('input[data-contact-id="2"]', "555-9999"),
                                     p.click('.save-btn[data-contact-id="2"]')),
                          backend_read_path="/api/contacts?bugs=off"),
        ]
    for r in results:
        _print_result(r)

    s = summarize(results)
    print(f"\n  Summary: {s['problems_found']} problem(s) across {s['flows_checked']} flow(s) "
          f"-> verdicts: {s['verdicts']}")
    return results


def main():
    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    t2 = threading.Thread(target=_serve_contacts, daemon=True)
    t2.start()
    time.sleep(2.0)  # let both uvicorn instances come up

    all_results = []
    all_results += run_against("on")
    all_results += run_against("off")
    all_results += run_contacts_demo("on")
    all_results += run_contacts_demo("off")

    print("\n" + "=" * 70)
    print("Takeaway: every action above renders correctly on screen — a purely-visual")
    print("test would pass all of them. The agent catches the lies only by comparing")
    print("the UI's claim against the backend's actual state. The second (contacts)")
    print("app proves this isn't specific to the first app's markup: same code, new")
    print("selectors, same verdicts.")
    print("=" * 70)

    out_dir = Path(__file__).resolve().parents[1] / "demo_output"
    report_path = render_html_report(all_results, out_dir / "demo_report.html",
                                      title="Veritas Demo Run")
    print(f"\nHTML report written to {report_path}")


if __name__ == "__main__":
    main()
