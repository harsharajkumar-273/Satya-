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

PORT = 8077
BASE = f"http://127.0.0.1:{PORT}"


def _serve():
    from agent_demo.app import app
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="error")


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


def main():
    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    time.sleep(2.0)  # let uvicorn come up

    run_against("on")
    run_against("off")

    print("\n" + "=" * 70)
    print("Takeaway: every action above renders correctly on screen — a purely-visual")
    print("test would pass all of them. The agent catches the lies only by comparing")
    print("the UI's claim against the backend's actual state.")
    print("=" * 70)


if __name__ == "__main__":
    main()
