# Validation: Satya against apps it wasn't written for

Every result here comes from a live run with headless Chromium. Configs are in
`validation/todomvc/` and every run can be reproduced (see the end of this page). The goal was
to answer the one question the demo apps can't: does Satya hold up on code
that wasn't built to contain the bugs it looks for?

## What was tested

The target is [TodoMVC](https://todomvc.com), the same todo app built in dozens of
frameworks, taken from the published npm package `todomvc@0.1.1` (an older snapshot of
the project; see "Caveats"). None of these apps have a backend. They persist to
`localStorage`, so Satya can't read their data directly. Every run used **reload ground
truth** (`ground_truth: reload`). Satya performs the action, reloads the page, and checks
whether the change it was shown is still there. It never inspects `localStorage` itself.

Each app ran the same five flows: add two todos, rename one, mark one complete, delete one.

## Results

| App | Result |
|---|---|
| Vanilla JS | 5/5 `AGREE` |
| Stapes | 5/5 `AGREE` |
| Dijon | 5/5 `AGREE` |
| Component.js | 5/5 `AGREE` (needed its own new-todo selector, `.todo__new`) |
| **Sammy.js** | **Real bug found:** `UNSTABLE_RENDER`, see below |
| Vanilla JS with 2 seeded bugs | 3/3 affected flows caught (`NO_REQUEST`), 2 untouched flows `AGREE` |
| Kendo UI | Not supported: its rows have no stable id attribute, so there is no way to tell which record changed |
| jQuery | Not runnable: the npm snapshot is missing its `bower_components` |

The task-manager and contacts demo apps were also re-run in both ground-truth modes. Reload
mode and API mode gave the same verdicts (3 problems with bugs on, all `AGREE` with bugs off).

### The Sammy.js finding

After a refresh, the Sammy.js TodoMVC sometimes shows an **empty list** even though every
todo is still saved. A stress test reloaded a page holding 5 saved todos 18 times, and 3 of
those reloads rendered nothing and stayed empty. `localStorage` held all 5 todos every time.

**Root cause** (confirmed by reading the source): `#todo-list` is defined inside
`templates/todos.template`, and that template is fetched asynchronously when the app
launches. At the same time, the `#/` route triggers `fetchTodos`, which renders the items
into `$('#todo-list')`. When the item template arrives before the list template, that
selector matches nothing. The items are silently dropped and nothing ever re-renders them.
A user who refreshes simply sees their todos vanish.

Satya reports this as `UNSTABLE_RENDER`, a hard failure, because data that doesn't
reliably show up after a refresh is a real bug. Across 6 full runs, it was flagged in 3.
That's expected for a race that hits about 1 in 6 reloads.

### False positives found and fixed along the way

The first Sammy.js runs were wrong, and three engine changes came directly from them:

1. **A false `NO_REQUEST`**. A reload that happened to render empty looked like "the new
   todo didn't persist". It had persisted. **Fix:** before reporting that a change didn't
   survive, Satya reloads twice more. If every reload agrees, the failure is confirmed (and
   the report says so). If they disagree, the verdict is `UNSTABLE_RENDER` instead.
2. **Corrupted starting state**. If the reload *before* an action rendered empty, the action
   ran against a broken page and every verdict after that was noise. **Fix:** the pre-action
   reload repeats until two renders agree, falls back to the majority render if they
   don't, and flags the instability.
3. **One broken step aborted the whole run**. A selector that timed out (30s) killed every
   remaining flow and produced no report. **Fix:** a failing step now yields
   `ACTION_FAILED` for that flow only (inconclusive, not a verdict on the app), the step
   timeout is 10s, and the rest of the run continues.

Page loads also now wait for network idle plus a stable row snapshot, instead of a fixed 300ms.

## Caveats

- `todomvc@0.1.1` is an old snapshot. The upstream repo wasn't reachable from the test
  environment, so it's unconfirmed whether the current Sammy.js example still has this race.
  The finding holds for the code that was tested.
- Some Sammy.js runs still produce an inconclusive `NO_CLAIM`. In a page load hit by the race,
  a rename can have no visible effect at all. That's reported as "couldn't verify", which is
  the correct outcome.
- The seeded bugs were written by us. They show that reload mode catches the bug class on a
  third-party codebase, not that Satya found them independently. The Sammy.js race is the
  independent finding.
- Reload ground truth performs real actions. Only point it at test or staging environments.
  The service refuses mutation mode unless `allowed_domains` is set explicitly.

## Reproducing

```bash
npm pack todomvc@0.1.1 && tar -xzf todomvc-0.1.1.tgz
(cd package && python3 -m http.server 8090 --bind 127.0.0.1) &
# optional: seeded variant
cp -r package/examples/vanillajs package/examples/vanillajs_seeded
patch -d package/examples -p0 < validation/todomvc/vanillajs_seeded.patch
python scripts/satya.py --config validation/todomvc/sammyjs.yaml --html-report sammy.html
```
