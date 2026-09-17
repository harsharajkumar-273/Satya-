"""
Demo target app — a tiny task manager with DELIBERATE, HIDDEN bugs.

The whole project is meaningless without a target where the UI *lies* about
what the backend did, so this app is built to contain exactly those bugs.
It is a single self-contained FastAPI app serving both an HTML/JS frontend
and a JSON API, so the demo needs no external services.

The bugs (all invisible to a purely-visual test — the UI looks correct):

  1. FAKE DELETE: clicking "Delete" removes the row from the DOM and shows a
     "Deleted!" toast, but the frontend never calls DELETE /api/tasks/{id}.
     The task is still on the server. Refresh and it reappears.

  2. FAKE SAVE (silent failure): editing a task's title and clicking "Save"
     shows "Saved!" and updates the DOM, but the PUT request body omits the
     title field, so the server keeps the old value. UI and backend diverge.

  3. LEAK: the task list endpoint returns an internal field
     (`_internal_owner_email`) that the frontend doesn't display but is
     present in the network response — a data-leak bug that's invisible
     on screen but catchable by inspecting the actual API payload.

Toggle bugs off with ?bugs=off to show the agent passing a clean version.

Run: uvicorn app:app --app-dir src/agent_demo  (or via the demo script)
"""
from __future__ import annotations

import copy

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="Veritas demo target")

# in-memory "database"
_SEED = [
    {"id": 1, "title": "Write proposal", "done": False, "_internal_owner_email": "harsha@example.com"},
    {"id": 2, "title": "Review PR #42", "done": False, "_internal_owner_email": "harsha@example.com"},
    {"id": 3, "title": "Deploy to staging", "done": True, "_internal_owner_email": "harsha@example.com"},
]
_db: list[dict] = copy.deepcopy(_SEED)


def _reset():
    global _db
    _db = copy.deepcopy(_SEED)


@app.get("/api/tasks")
def list_tasks(bugs: str = "on"):
    # BUG 3 (leak): when bugs are on, the internal field is left in the payload
    if bugs == "off":
        return [{k: v for k, v in t.items() if not k.startswith("_")} for t in _db]
    return _db  # leaks _internal_owner_email


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int):
    global _db
    _db = [t for t in _db if t["id"] != task_id]
    return {"status": "deleted", "id": task_id}


@app.put("/api/tasks/{task_id}")
def update_task(task_id: int, payload: dict, bugs: str = "on"):
    for t in _db:
        if t["id"] == task_id:
            if "title" in payload:
                t["title"] = payload["title"]
            if "done" in payload:
                t["done"] = payload["done"]
            # BUG 3 (leak) also applies to this response unless bugs are off
            if bugs == "off":
                return {k: v for k, v in t.items() if not k.startswith("_")}
            return t
    return JSONResponse(status_code=404, content={"error": "not found"})


@app.post("/api/reset")
def reset():
    _reset()
    return {"status": "reset"}


FRONTEND = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Tasks</title>
<style>
  body { font-family: Arial, sans-serif; max-width: 520px; margin: 40px auto; }
  h1 { font-size: 20px; }
  .task { display: flex; align-items: center; gap: 8px; padding: 10px; border: 1px solid #ddd; border-radius: 6px; margin-bottom: 8px; }
  .task input[type=text] { flex: 1; border: none; font-size: 15px; }
  .task button { border: none; border-radius: 4px; padding: 6px 10px; cursor: pointer; }
  .save { background: #2b6fdc; color: white; }
  .del { background: #dc3545; color: white; }
  #toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: #222; color: white; padding: 10px 18px; border-radius: 6px; opacity: 0; transition: opacity .2s; }
  #toast.show { opacity: 1; }
</style>
</head>
<body>
<h1>My Tasks</h1>
<div id="list"></div>
<div id="toast"></div>
<script>
const BUGS = new URLSearchParams(location.search).get('bugs') || 'on';

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 1200);
}

async function load() {
  const res = await fetch('/api/tasks?bugs=' + BUGS);
  const tasks = await res.json();
  const list = document.getElementById('list');
  list.innerHTML = '';
  for (const task of tasks) {
    const div = document.createElement('div');
    div.className = 'task';
    div.dataset.id = task.id;
    div.innerHTML = `
      <input type="text" value="${task.title}" data-id="${task.id}">
      <button class="save" data-id="${task.id}">Save</button>
      <button class="del" data-id="${task.id}">Delete</button>`;
    list.appendChild(div);
  }
}

document.addEventListener('click', async (e) => {
  const id = e.target.dataset.id;
  if (!id) return;

  if (e.target.classList.contains('del')) {
    // BUG 1 (fake delete): remove from DOM + toast, but DON'T call the API when bugs are on
    e.target.closest('.task').remove();
    toast('Deleted!');
    if (BUGS === 'off') {
      await fetch('/api/tasks/' + id, { method: 'DELETE' });
    }
    return;
  }

  if (e.target.classList.contains('save')) {
    const input = document.querySelector('input[data-id="' + id + '"]');
    // BUG 2 (fake save): when bugs are on, send an empty body so the title never persists
    const body = (BUGS === 'off') ? { title: input.value } : {};
    await fetch('/api/tasks/' + id + '?bugs=' + BUGS, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    toast('Saved!');
  }
});

load();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return FRONTEND
