# Veritas

An agent that catches the class of frontend bug visual testing structurally cannot: where
the UI *looks* correct but **lies about what the backend actually did**.

A delete button that removes the row and flashes "Deleted!" — but never calls the API, so
the record is still there after a refresh. A save that shows "Saved!" — but drops the edited
field, so the backend keeps the old value. An API response that quietly carries an internal
email the page never displays. Every one of these renders perfectly on screen. A screenshot
diff, a pixel-comparison tool, a human glancing at the page — all pass them. Veritas catches
them by acting on the UI and then checking the UI's *claim* against the backend's *reality*.

> Veritas is a sibling project to PixelGuard. PixelGuard compares a UI against a visual
> baseline ("did this change from before?"). Veritas asks a different, harder question with
> no baseline at all: "is the UI telling the truth about what it just did?" They're
> deliberately separate — different problem, different architecture.

## Quickstart

```bash
pip install -r requirements.txt
playwright install chromium
python scripts/run_demo.py     # boots a buggy demo app, runs the agent against it
pytest tests/                  # 15 unit tests
```

`scripts/run_demo.py` starts a demo target app that contains three deliberate, hidden bugs,
then points the agent at it twice: once with the bugs on (the agent should catch the UI
lying) and once with them off (the agent should confirm everything agrees). Runs in well
under a minute.

## The core idea, in one picture

```
                    ┌─────────────────────────┐
                    │  agent loop: act on a    │
                    │  UI flow (click, type)   │
                    └────────────┬────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │  browser agent           │
                    │  Playwright + network    │
                    │  interception            │
                    └────────────┬────────────┘
                                 │  acts on
                    ┌────────────▼────────────┐
                    │  the app under test      │
                    └──────┬───────────┬───────┘
              observes     │           │    observes
         ┌─────────────────▼──┐    ┌───▼──────────────────┐
         │  VISUAL channel     │    │  BACKEND channel      │
         │  toast text + DOM   │    │  real HTTP traffic +  │
         │  delta = what the   │    │  API state = what     │
         │  UI *claims*        │    │  *actually* happened  │
         └─────────┬───────────┘    └───────────┬───────────┘
                   │                             │
          ┌────────▼─────────┐                   │
          │ claim inference  │                   │
          │ "UI says it did  │                   │
          │  a REMOVAL of #1"│                   │
          └────────┬─────────┘                   │
                   │                             │
                   └──────────┬──────────────────┘
                    ┌─────────▼──────────┐
                    │  reconciler         │
                    │  does the claim     │
                    │  match reality?     │
                    └─────────┬───────────┘
                              │
        verdict: AGREE / UI_LIED / NO_REQUEST / BACKEND_ERROR / DATA_LEAK
```

The whole design hinges on **two independent observation channels**. Purely-visual testing
only has the left one — so a UI that renders a convincing lie passes. Veritas adds the right
one (what the backend actually did) and makes *the gap between them* the thing it reports.

## What makes this an agent, not a test script

The important design decision: Veritas does **not** contain a `verify_delete` function with a
hand-written rule that says "a delete should remove the record from the backend." That would
just be a test script with network assertions, and it would only work for flows someone
pre-wrote.

Instead the agent **infers what the UI claimed** from generic, observable signals and checks
that inferred claim against a generic backend diff. There is no per-flow logic anywhere in
the pipeline:

1. **Claim inference** (`agent/claim.py`) reads the toast wording ("Deleted!" → a removal
   claim, "Saved!" → a mutation claim, "Added!" → a creation claim) and the DOM delta (which
   row disappeared, which value changed, which row is new) to build a structured `Claim`:
   *kind* (REMOVAL / MUTATION / CREATION / SUBMISSION), whether success was asserted, the
   target item, and — for edits — the new value the UI now shows.
2. **Backend diff** (`reconcile/backend.py`) snapshots the backend's records before and after
   the action and diffs them generically into removed / added / changed, tied to no
   particular schema.
3. **Reconciliation** (`reconcile/reconciler.py`) is a single `reconcile(claim, diff, trace)`
   function that checks any claim kind against the diff. A REMOVAL claim expects a record to
   have left; a MUTATION expects the named field to actually hold the claimed value; a
   CREATION expects a new record. No `if delete / if save` branches.

Because nothing is flow-specific, the pipeline handles flows it was never written for. The
test suite includes exactly this: a CREATION flow with no dedicated code path, which the
agent still forms a claim for, catches when the backend didn't actually gain the record, and
passes when it did.

### The seam for a smarter claim-inferrer

`infer_claim(before, after)` is a deliberate function boundary. The current implementation is
a heuristic engine over common UI patterns (toasts, list mutations, field edits). A
vision-language-model claim-inferrer — one that reads the before/after screenshots and the
toast and reasons about intent for *arbitrary* UIs — would implement the same signature and
drop in without touching the reconciler or the agent loop. That's the honest upgrade path
from "works on common patterns" to "reads any UI," and it's left as a clean seam rather than
pretended-away.

## The demo app and its bugs

`src/agent_demo/app.py` is a self-contained FastAPI app serving both a small task-manager
frontend and its JSON API, built to contain exactly the bugs Veritas targets. All three are
invisible on screen — the UI renders correctly in every case:

- **Fake delete (`NO_REQUEST`)** — clicking Delete removes the row from the DOM and shows
  "Deleted!", but the frontend never sends `DELETE /api/tasks/{id}`. The task is still on the
  server; a refresh brings it back.
- **Fake save (`UI_LIED`)** — editing a title and clicking Save shows "Saved!" and updates
  the input, but the `PUT` request body omits the title field, so the server keeps the old
  value. UI and backend silently diverge.
- **Data leak (`DATA_LEAK`)** — the task list response carries an internal
  `_internal_owner_email` field the frontend never displays but that is present in the
  network payload, readable by anyone inspecting traffic.

Add `?bugs=off` and the same app behaves correctly, so the demo shows the agent both catching
the lies and — importantly — staying silent when the app is actually working (no false
alarms).

## What the demo prints

With bugs **on**, three problems caught, each with a plain-English explanation:

```
Flow: click Delete on task 1
  UI claimed: toast='Deleted!', DOM rows 3->2
  Network:    (no API calls)
  !! [NO_REQUEST] UI asserted a removal succeeded, but no state-changing request was sent.

Flow: edit task 2 title ... and Save
  UI claimed: toast='Saved!', DOM rows 2->2
  Network:    PUT /tasks/2->200
  !! [UI_LIED] UI claimed item 2 now reads 'Review PR #42 (updated)', but the backend disagrees.
  !! [DATA_LEAK] API response leaked internal field(s): _internal_owner_email.

Summary: 3 problem(s) across 2 flow(s)
```

With bugs **off**, the same two flows both come back `AGREE`, 0 problems.

## Scope and current limitations

Worth being precise about, since the claims above are easy to over-read:

- **Not zero-config.** "Flow-agnostic" describes the claim-inference and reconciliation
  logic — there's no per-flow `if delete / if save` branch. It does not mean *app-agnostic*:
  `BrowserAgent` still needs `row_selector`, `toast_selector`, and `id_attr` configured for
  the app under test, the same way any Playwright-based tool needs to know an app's DOM.
- **The heuristic inferrer leans on toast text first.** A DOM-delta fallback catches
  list-level changes (a row added/removed/edited) even with no toast, but a flow with
  *neither* a toast *nor* a visible list change (e.g. "send an email," "trigger a webhook")
  gives the heuristic engine nothing to form a claim from. That's precisely the gap the
  `VLMClaimInferrer` seam exists to close, but it's an honest gap today, not a solved one.
- **Validated against one demo app.** The three injected bugs and the 25-test suite prove the
  reconciliation logic is sound, but the project has only been run end-to-end against the
  purpose-built demo target — it hasn't yet been pointed at a real, pre-existing production
  frontend to see how the selector/toast assumptions hold up against messier markup.

## Repo layout

```
src/
  models.py     pure decoupled data models (ActionTrace, NetworkCall, Claim, Verdict)
  audit/        VeritasAuditor middleware for passive CI & existing Playwright tests
  agent_demo/   the demo target app (FastAPI frontend + API, with hidden bugs)
  browser/      Playwright agent with network interception; captures UI + traffic per action
  agent/        claim inference (claim.py with Heuristic + VLM) + loop (loop.py)
  reconcile/    backend snapshot/diff (backend.py) + claim-driven reconciler (reconciler.py)
tests/          pytest suite (25 tests covering claims, VLM, reconciler, auditor, eventual consistency)
scripts/        run_demo.py — one-command end-to-end demo
```

## How to use Veritas in existing Playwright tests

You can wrap existing Playwright tests with `VeritasAuditor`:

```python
from audit.middleware import VeritasAuditor

def test_delete_action(page):
    auditor = VeritasAuditor(page, backend_fetch_fn=lambda: fetch_db_records())
    
    with auditor.audit("delete task"):
        page.click(".delete-btn")
        
    # Raises AssertionError if UI lied, dropped API request, or leaked data
    auditor.assert_truthful()
```

## Advanced Capabilities

- **VLM Claim Inference**: Set `GEMINI_API_KEY` to enable `VLMClaimInferrer`, allowing multimodal models to inspect before/after screenshots and DOM state to understand subtle or unconventional UI affirmations, with automatic fallback to heuristics.
- **Eventual Consistency Support**: Set `poll_timeout` (e.g. `2.0`s) in `verify_action` or `VeritasAuditor` to handle asynchronous backends, read-replicas, and background queues without false positives.
- **Deep Data Leak Auditing**: Scans nested response payloads for sensitive or internal keys (`_internal*`, `_private*`) delivered over the wire.
- **Decoupled Architecture**: `models.py` has zero framework dependencies, allowing unit tests and reconciler logic to run in milliseconds without browser dependencies.

## How it maps to real work

The three bugs are not arbitrary — they're the same classes of defect caught by hand during a
real UI rewrite: deletes and saves that never reached the backend, and a share flow leaking
internal fields to recipients. Veritas is that manual QA work turned into an autonomous agent:
it performs the flow, reads what the UI claims, independently verifies against the backend,
and reports the mismatch.

