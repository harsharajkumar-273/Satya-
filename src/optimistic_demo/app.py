"""
Demo target app — an optimistic-UI task list that doesn't roll back.

Optimistic UI (update the screen before the server confirms, for snappier
feel) is a legitimate, common pattern -- it's also a bug class purely-visual
testing can't see: if the server rejects the request, a correct app rolls
the optimistic change back; a buggy one leaves it standing and the user
walks away believing something succeeded that never did.

The bug (invisible to a purely-visual test — the UI looks correct):

  NO ROLLBACK: typing a title over 40 characters and clicking "Add" adds the
  row to the DOM and shows "Added!" immediately (optimistic), then POSTs.
  The server rejects titles over 40 characters with 422. When bugs are on,
  the frontend never checks the response -- the fake row and the "Added!"
  toast both stand uncorrected. When bugs are off, a 422 removes the
  optimistic row and shows an error toast instead.

A title of 40 characters or fewer succeeds in both modes: the server creates
it for real and the frontend reloads the list from the backend, so the
optimistic row's temporary id is replaced by the real one.

Toggle bugs off with ?bugs=off to show the agent passing a clean version.

Run: uvicorn app:app --app-dir src/optimistic_demo
"""
from __future__ import annotations

import copy

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="Satya optimistic-UI demo target")

_SEED = [
    {"id": 1, "title": "Draft the Q3 roadmap"},
    {"id": 2, "title": "Pair on the flaky test"},
]
_db: list[dict] = copy.deepcopy(_SEED)
_next_id = 3

MAX_TITLE_LEN = 40


def _reset():
    global _db, _next_id
    _db = copy.deepcopy(_SEED)
    _next_id = 3


@app.get("/api/tasks")
def list_tasks():
    return _db


@app.post("/api/tasks")
def create_task(payload: dict):
    global _next_id
    title = payload.get("title", "")
    if len(title) > MAX_TITLE_LEN:
        return JSONResponse(status_code=422, content={"error": "title too long"})
    task = {"id": _next_id, "title": title}
    _db.append(task)
    _next_id += 1
    return task


@app.post("/api/reset")
def reset():
    _reset()
    return {"status": "reset"}


FRONTEND = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Quick Tasks</title>
<style>
  body { font-family: Arial, sans-serif; max-width: 520px; margin: 40px auto; }
  h1 { font-size: 20px; }
  .task { padding: 10px; border: 1px solid #ddd; border-radius: 6px; margin-bottom: 8px; }
  .addrow { display: flex; gap: 8px; margin-bottom: 16px; }
  .addrow input { flex: 1; font-size: 15px; padding: 6px; }
  .addrow button { border: none; border-radius: 4px; padding: 6px 14px; cursor: pointer; background: #2b6fdc; color: white; }
  #toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: #222; color: white; padding: 10px 18px; border-radius: 6px; opacity: 0; transition: opacity .2s; }
  #toast.show { opacity: 1; }
</style>
</head>
<body>
<h1>Quick Tasks</h1>
<div class="addrow">
  <input type="text" id="new-title" placeholder="New task title">
  <button id="add-btn">Add</button>
</div>
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
  const res = await fetch('/api/tasks');
  const tasks = await res.json();
  const list = document.getElementById('list');
  list.innerHTML = '';
  for (const task of tasks) {
    const div = document.createElement('div');
    div.className = 'task';
    div.dataset.id = task.id;
    div.textContent = task.title;
    list.appendChild(div);
  }
}

document.getElementById('add-btn').addEventListener('click', async () => {
  const input = document.getElementById('new-title');
  const title = input.value;
  const tempId = 'temp-' + Date.now();

  // optimistic: show the row and the success toast before the server replies
  const div = document.createElement('div');
  div.className = 'task';
  div.dataset.id = tempId;
  div.textContent = title;
  document.getElementById('list').appendChild(div);
  toast('Added!');
  input.value = '';

  const res = await fetch('/api/tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title })
  });

  if (res.ok) {
    await load();  // pick up the real id from the backend
  } else if (BUGS === 'off') {
    // roll back the optimistic row -- the request failed, nothing was created
    div.remove();
    toast('Failed to add: title too long');
  }
  // BUG: when bugs are on, a failed request is never checked -- the
  // optimistic row and the "Added!" toast both stand uncorrected.
});

load();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return FRONTEND
