"""
Demo target app — an honest frontend backed by an eventually-consistent API.

Every other demo app in this repo contains a UI bug: the frontend claims
something the backend didn't do. This one contains no frontend bug at all --
the frontend only shows "Deleted!" after the server returns 200. The catch is
in the backend: DELETE returns 200 immediately ("accepted"), but the record
isn't actually removed from the read path until a background worker applies
it DELAY_MS later, the way a queued write or an async replica lag would.

This exists to test Satya's own eventual-consistency handling
(`poll_timeout`/`poll_interval` in agent.loop.verify_action), not to find a
bug in the app:

  - With no polling (poll_timeout=0, the default), Satya reads backend state
    immediately after the action and sees the still-present record -- a
    FALSE POSITIVE (UI_LIED or NO_REQUEST) for a UI that told the truth.
  - With poll_timeout >= DELAY_MS, Satya polls until the backend catches up
    and correctly reports AGREE.

There is no ?bugs= toggle: this app has exactly one behavior, and the two
outcomes above come from how Satya is configured, not from the app.

Run: uvicorn app:app --app-dir src/delayed_demo
"""
from __future__ import annotations

import copy
import threading
import time

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="Satya eventual-consistency demo target")

_SEED = [
    {"id": 1, "title": "Archive last quarter's reports"},
    {"id": 2, "title": "Renew the TLS certificate"},
]
_db: list[dict] = copy.deepcopy(_SEED)
_lock = threading.Lock()

DELAY_MS = 900


def _reset():
    global _db
    with _lock:
        _db = copy.deepcopy(_SEED)


def _apply_delete_after_delay(task_id: int):
    time.sleep(DELAY_MS / 1000)
    with _lock:
        global _db
        _db = [t for t in _db if t["id"] != task_id]


@app.get("/api/tasks")
def list_tasks():
    with _lock:
        return list(_db)


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int):
    # accepted immediately; the actual removal lands DELAY_MS later
    threading.Thread(target=_apply_delete_after_delay, args=(task_id,), daemon=True).start()
    return {"status": "accepted", "id": task_id}


@app.post("/api/reset")
def reset():
    _reset()
    return {"status": "reset"}


FRONTEND = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Queued Tasks</title>
<style>
  body { font-family: Arial, sans-serif; max-width: 520px; margin: 40px auto; }
  h1 { font-size: 20px; }
  .task { display: flex; align-items: center; gap: 8px; padding: 10px; border: 1px solid #ddd; border-radius: 6px; margin-bottom: 8px; }
  .task span { flex: 1; }
  .task button { border: none; border-radius: 4px; padding: 6px 10px; cursor: pointer; background: #dc3545; color: white; }
  #toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: #222; color: white; padding: 10px 18px; border-radius: 6px; opacity: 0; transition: opacity .2s; }
  #toast.show { opacity: 1; }
</style>
</head>
<body>
<h1>Queued Tasks</h1>
<div id="list"></div>
<div id="toast"></div>
<script>
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
    div.innerHTML = `<span>${task.title}</span><button class="del" data-id="${task.id}">Delete</button>`;
    list.appendChild(div);
  }
}

document.addEventListener('click', async (e) => {
  const id = e.target.dataset.id;
  if (!id || !e.target.classList.contains('del')) return;
  // honest frontend: only update after the server actually responds
  const res = await fetch('/api/tasks/' + id, { method: 'DELETE' });
  if (res.ok) {
    e.target.closest('.task').remove();
    toast('Deleted!');
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
