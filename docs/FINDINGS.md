# Findings: browser integration test suite

Every result below comes from `tests/browser/` — a pytest suite that drives real headless
Chromium against real demo apps and real third-party code, not the no-browser unit tests in
`tests/`. Each test module's own docstring states the exact app version it targets and the exact
command to reproduce it; this page collects the outcomes and sorts them into four categories, as
distinct things: a confirmed bug Satya caught, a false positive Satya produced on a correct app,
a case Satya's current design can't handle, and a result that came back genuinely inconclusive.

## Reproducing any result on this page

```bash
pip install -r requirements.txt
playwright install chromium
scripts/setup_todomvc.sh                    # only needed for the two TodoMVC-based tests
PYTHONPATH=src pytest tests/browser/ -q     # everything, ~2.5 minutes
```

Each test file can also be run on its own — see the command in its docstring. In CI
(`.github/workflows/tests.yml`), the `pytest` job explicitly excludes `tests/browser/`
(`--ignore=tests/browser`, since pytest otherwise recurses into it even when only `tests/` is
named) and stays fast with no browser installed; a separate `browser-tests` job installs Chromium
and `scripts/setup_todomvc.sh`, then runs everything under `tests/browser/` except
`test_inconsistent_reload_rendering.py`. That one test is excluded from CI specifically, not the
whole suite: it's a genuine ~1-in-6-reload race (see finding #7 below), so running it on every
push would make CI spuriously red about once every six runs for a reason that has nothing to do
with this repo's own code. Run it manually or on a schedule — see its own docstring.

Pinned app versions: every app in `src/*_demo/` is pinned by this repository's own commit — check
out the commit a given test file was last changed at and you have the exact app it was run
against. The TodoMVC-based tests are pinned to npm `todomvc@0.1.1`, fetched and extracted
verbatim by `scripts/setup_todomvc.sh` (same snapshot `docs/VALIDATION.md`'s findings use).

Last run: 13/13 browser tests passed (102/102 existing unit tests also still pass), total
~2.5 minutes, headless Chromium, this branch.

## Confirmed bugs

Satya correctly caught a real gap between what the UI claimed and what persisted. For each, the
"clean" counterpart (same app, bug toggled off, or the honest code path) is also tested and
produces `AGREE` — so these aren't cases where Satya just always says something's wrong.

| # | Scenario | App (pinned) | Verdict | Reproduce |
|---|---|---|---|---|
| 1 | Fake delete: row removed from DOM + "Deleted!" toast, no DELETE ever sent | `src/agent_demo` `?bugs=on` | `NO_REQUEST` | `pytest tests/browser/test_api_backed_persistence.py::test_task_manager_bugs_on_catches_fake_delete_and_fake_save` |
| 2 | Fake save: "Saved!" + new value shown, PUT body omits the field | `src/agent_demo` `?bugs=on` | `UI_LIED` | same test as #1 |
| 3 | Data leak: PUT response carries an internal field the UI never renders | `src/agent_demo` `?bugs=on` | `DATA_LEAK` | `pytest tests/browser/test_api_backed_persistence.py::test_task_manager_data_leak_caught_when_bugs_on` |
| 4 | Same two bug classes, different markup/toast mechanism (`.contact`/`.banner` instead of `.task`/`#toast`) | `src/contacts_demo` `?bugs=on` | `NO_REQUEST`, `UI_LIED` | `pytest tests/browser/test_api_backed_persistence.py::test_contacts_app_different_markup_same_verdicts` |
| 5 | **New bug class.** Fake ack over a WebSocket: server sends `{"status":"deleted"}` unconditionally; the record is only actually removed when bugs are off. The frontend is honest — it only updates after the ack — the lie is server-side. | `src/ws_demo` `?bugs=on` | `UI_LIED` | `pytest tests/browser/test_websocket_mutations.py::test_fake_ack_over_websocket_is_caught_as_ui_lied` |
| 6 | **New bug class.** Optimistic UI with no rollback: row added + "Added!" shown before the POST fires; server rejects with 422; UI never checks the response, so the fake row and toast stand uncorrected. | `src/optimistic_demo` `?bugs=on` | `BACKEND_ERROR` | `pytest tests/browser/test_optimistic_ui_failure.py::test_no_rollback_on_rejection_is_caught_as_backend_error` |
| 7 | **Real third-party bug**, unmodified code, reproduced fresh in this run (first found during earlier `docs/VALIDATION.md` work): Sammy.js TodoMVC's `#todo-list` sometimes renders empty after a reload because the item/list templates load asynchronously and the route can render before the list template arrives. | `todomvc@0.1.1` `examples/sammyjs` | `UNSTABLE_RENDER` (1/6 independent runs this time; 3/6 in `docs/VALIDATION.md`'s original measurement — a ~1-in-6-reload race, so the exact count varies run to run) | `pytest tests/browser/test_inconsistent_reload_rendering.py -s` |

## False positives

Satya flagged a UI that told the truth.

| # | Scenario | App (pinned) | Verdict produced | Why | Reproduce |
|---|---|---|---|---|---|
| 1 | Delete is accepted (200) immediately but the backend only actually removes the record ~900ms later on a background thread (simulated eventually-consistent write). The frontend only shows "Deleted!" *after* the 200 — it is not lying about anything. With no `poll_timeout` configured (the default), Satya reads backend state immediately and sees the still-present record. | `src/delayed_demo` | `UI_LIED` | Satya's immediate post-action backend read races the backend's own async write. The mitigation already exists (`poll_timeout`/`poll_interval` in `agent.loop.verify_action`) and resolves it — see the paired test — but the *default* (`poll_timeout=0`) produces a false positive against any genuinely eventually-consistent backend. | `pytest tests/browser/test_delayed_responses.py::test_no_polling_misreports_an_honest_delayed_delete` |

**Confirmed fix, same scenario:** `poll_timeout=1.5, poll_interval=0.1` against the identical app
produces `AGREE` (`test_polling_past_the_delay_agrees`). This isn't a new finding about Satya's
engine — `poll_timeout` already existed — it's the first real-browser proof that it actually
closes this specific false positive, not just the mocked version in `tests/test_reconciler.py`'s
`test_eventual_consistency_retry_success`.

## Unsupported cases

Carried over from `docs/VALIDATION.md` (not re-tested here, no new information) for completeness
— cases Satya's current id-tracking design cannot handle, listed separately from bugs and false
positives because they are neither:

| App | Why unsupported |
|---|---|
| Kendo UI (TodoMVC) | Rows have no stable id attribute anywhere — no way to tell which record changed. |
| Lavaca / RequireJS (TodoMVC) | The id lives on a `<div data-id>` nested inside the `<li>` row Satya reads it from, not on the row itself. |
| jQuery (TodoMVC) | The pinned npm snapshot is missing `bower_components`; not runnable at all. |

## Inconclusive results

Results where Satya correctly declined to form an opinion rather than guessing — these are
coverage gaps, not passes or failures.

| # | Scenario | App (pinned) | Verdict | Why inconclusive |
|---|---|---|---|---|
| 1 | Same Sammy.js render race as confirmed bug #7, different flows, same trial run: `rename first todo` produced `NO_CLAIM`, `mark first todo complete` produced `NO_REQUEST` instead of `UNSTABLE_RENDER` or `AGREE`. | `todomvc@0.1.1` `examples/sammyjs` | `NO_CLAIM`, `NO_REQUEST` | The race can land at different points in a flow and produce different observable symptoms — sometimes a confirmed `UNSTABLE_RENDER`, sometimes a reload that happens to render empty mid-flow with no later disagreement to catch it, which looks like "nothing happened" rather than "something unstable happened." `docs/VALIDATION.md` documents this same ambiguity ("some Sammy.js runs still produce an inconclusive NO_CLAIM"); this run reproduced it directly rather than just citing it. See the full per-trial output from `pytest tests/browser/test_inconsistent_reload_rendering.py -s`. |

No `NO_CLAIM`/`ACTION_FAILED` results were observed from `ws_demo`, `optimistic_demo`, or
`delayed_demo` in this run — every trial against those three landed as a clean confirmed result
(bug caught, false positive, or `AGREE`), so this section isn't padded with results that didn't
happen.

## What's genuinely new here versus `docs/VALIDATION.md`

`docs/VALIDATION.md` covers client-side-only TodoMVC apps (reload ground truth, no backend) and
this repo's two REST demo apps. This suite adds three scenario classes that had no coverage
before: a WebSocket-mutation bug class (#5), an optimistic-UI-without-rollback bug class (#6),
and a direct demonstration — not just a unit-level mock — of Satya's own eventual-consistency
false positive and its fix (false positive #1). The Sammy.js finding (#7) is the same underlying
bug `docs/VALIDATION.md` already reported, re-run here as an automated, repeatable pytest
assertion instead of a one-off manual script.
