"""
Demo target app — mutations travel over a WebSocket instead of REST.

Most of Satya's demo apps send a DELETE/PUT and the bug is in what the
frontend sends. Some real apps instead push mutations over a persistent
WebSocket (chat apps, live dashboards, collaborative editors) and the UI
waits for a server-sent acknowledgement before updating. The bug class is
different: the SERVER's ack message itself can lie.

The bug (invisible to a purely-visual test — the UI looks correct):

  WS FAKE-ACK DELETE: the client sends {"action": "delete", "id": N} over
  the socket. The server always replies {"status": "deleted", "id": N} --
  but when bugs are on, it never actually removes the record. The frontend
  trusts the ack, removes the row, and shows "Deleted!". Refresh (or GET
  /api/tasks) and the record is still there.

Toggle bugs off with ?bugs=off on both the page and the WebSocket URL to
show the agent passing a clean version, where the ack is only sent after
the record is actually removed.

Run: uvicorn app:app --app-dir src/ws_demo
"""
from __future__ import annotations

import copy
import json

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

app = FastAPI(title="Satya WebSocket demo target")

_SEED = [
    {"id": 1, "title": "Ship the release notes"},
    {"id": 2, "title": "Rotate the staging credentials"},
    {"id": 3, "title": "Reply to the audit email"},
]
_db: list[dict] = copy.deepcopy(_SEED)


def _reset():
    global _db
    _db = copy.deepcopy(_SEED)


@app.get("/api/tasks")
def list_tasks():
    return _db


@app.post("/api/reset")
def reset():
    _reset()
    return {"status": "reset"}


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    bugs = websocket.query_params.get("bugs", "on")
    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            if msg.get("action") == "delete":
                task_id = msg["id"]
                # BUG (fake ack): when bugs are on, the ack is sent unconditionally;
                # the record is only actually removed when bugs are off.
                if bugs == "off":
                    global _db
                    _db = [t for t in _db if t["id"] != task_id]
                await websocket.send_text(json.dumps({"status": "deleted", "id": task_id}))
    except WebSocketDisconnect:
        pass


FRONTEND = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Live Tasks</title>
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
<h1>Live Tasks</h1>
<div id="list"></div>
<div id="toast"></div>
<script>
const BUGS = new URLSearchParams(location.search).get('bugs') || 'on';
let socket;

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 1200);
}

function connect() {
  const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
  socket = new WebSocket(proto + location.host + '/ws?bugs=' + BUGS);
  socket.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.status === 'deleted') {
      const el = document.querySelector('.task[data-id="' + msg.id + '"]');
      if (el) el.remove();
      toast('Deleted!');
    }
  };
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

document.addEventListener('click', (e) => {
  const id = e.target.dataset.id;
  if (!id || !e.target.classList.contains('del')) return;
  socket.send(JSON.stringify({ action: 'delete', id: Number(id) }));
});

connect();
load();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return FRONTEND
