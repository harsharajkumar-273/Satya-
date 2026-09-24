# Architecture

Satya drives a web app, records two independent accounts of what happened, and reports the
gap between them. This document walks through the pipeline stage by stage and points at the
module that implements each one.

## The pipeline

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

### 1. Browser agent — `src/browser/agent.py`

Drives the app under test with Playwright and wraps a single UI action (`agent.act(description, do)`)
in an `ActionTrace`: the toast/DOM snapshot before and after the action, every network call and
WebSocket frame observed during it, and before/after screenshots. This is the only module that
touches Playwright; everything downstream operates on the plain-data `ActionTrace`, which is why
the reconciliation logic has no browser dependency and its tests don't need one.

### 2. Claim inference — `src/agent/claim.py`

Reads the `ActionTrace`'s visual channel — toast wording, DOM row-count delta — and infers a
`Claim`: a `ClaimKind` (`CREATION`, `REMOVAL`, `MUTATION`, `NONE`) plus the record id and field
the UI appears to be claiming to have changed. This is deliberately generic: there is no
`verify_delete()` with a hand-written rule. A default `HeuristicClaimInferrer` covers common toast
phrasing; `BaseClaimInferrer` is the extension point for a VLM-based inferrer that reads the
screenshots directly instead of relying on toast text.

### 3. Backend snapshot & diff — `src/reconcile/backend.py`

`BackendSnapshot` captures backend state (a list of records, keyed by id) before and after the
action. `diff_backend()` produces a generic `BackendDiff` (added/removed/changed records) with no
knowledge of what flow produced it. In API mode this comes from reading your backend directly; in
reload mode (`ground_truth: reload`, for apps you don't own) it comes from reloading the page and
re-scraping the DOM, so "backend" here really means "ground truth," not necessarily a server.

### 4. Reconciler — `src/reconcile/reconciler.py`

`reconcile(claim, diff, trace)` is the decision function: does the inferred claim match the
backend diff and the network calls actually fired? It's a pure function over three plain-data
inputs, which is why it's exhaustively unit-tested (`tests/test_reconciler.py`) without a browser.
`check_data_leak()` separately scans network responses for fields the UI never renders — a claim
can be entirely truthful and still leak data over the wire.

Verdicts:

| Verdict | Meaning |
|---|---|
| `AGREE` | The claim matches the backend diff. |
| `UI_LIED` | The UI claimed a change that didn't happen (or happened differently). |
| `NO_REQUEST` | The UI claimed a change but never sent the request for it. |
| `BACKEND_ERROR` | The request was sent and the backend rejected it, but the UI claimed success. |
| `DATA_LEAK` | A network response carried data the page never displays. |
| `UNSTABLE_RENDER` | Reload ground truth disagreed with itself across repeated reloads — a real rendering bug, not a false positive (see `docs/VALIDATION.md`'s Sammy.js finding). |
| `NO_CLAIM` / `ACTION_FAILED` | Inconclusive: no claim could be inferred, or the step itself failed (selector timeout, etc.). Coverage gaps, not passing verdicts. |

### 5. Agent loop — `src/agent/loop.py`

Wires 1–4 together per flow: snapshot backend → act → snapshot backend again (with optional
polling for eventually-consistent backends) → infer claim → diff → reconcile. This is also where
reload-mode's extra robustness lives: a pre-action reload that repeats until two renders agree
(falling back to the majority render and flagging instability, see `_stable_render`), and a
post-action confirmation that reloads `confirm_reloads` times before reporting a failure, so a
one-off flaky render doesn't get reported as `UI_LIED`.

### 6. Auditor / orchestration — `src/audit/middleware.py`

`VeritasAuditor` runs a list of flows against an agent and collects `FlowResult`s, applying
`ground_truth`, `backend_id_key`, and the hard-failure/inconclusive verdict sets used to decide a
process exit code.

### Interfaces on top of the same engine

Everything above is a plain Python library (`agent`, `reconcile`, `audit`, `browser`) with no
knowledge of any particular front end. Four separate interfaces sit on top of it, all calling the
same `verify_action` / `VeritasAuditor` core:

- **CLI** (`src/cli.py`) — `python -m cli` for scripted/CI use; emits HTML, JUnit XML, and JSON reports (`src/report/`).
- **FastAPI service + dashboard** (`src/service/app.py`, `src/service/static/`) — runs flows on demand, persists run history to SQLite (`src/storage.py`), and serves the smoke-check / truthfulness-check URL modes described in the README.
- **Playwright middleware** — the `audit` module can be dropped directly into an existing Playwright test suite.
- **Browser extension** (`extension/`) — a read-only smoke check (status/console/failed-requests only; it does not perform the mutating actions a truthfulness check needs).

### Why the split matters

Every module above `browser/agent.py` operates on plain dataclasses (`ActionTrace`, `Claim`,
`BackendDiff`, `Finding`) defined in `src/models.py`, not on Playwright objects. That's what makes
102 of the test suite's tests runnable with no browser at all (`tests/test_reconciler.py`,
`tests/test_improvements.py`, `tests/test_reload_ground_truth.py`, etc.) — they construct traces
and snapshots by hand and assert on the verdict. Only `scripts/run_demo.py`'s live demo actually
launches Chromium.
