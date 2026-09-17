"""
Second demo target -- a contacts app, deliberately built with different
markup, selectors, and toast mechanism than agent_demo/app.py.

The point of this app is NOT to showcase new bug types (see agent_demo for
the canonical three). It's to prove that BrowserAgent/verify_action/reconcile
generalize via *configuration*, not because they're quietly hardcoded to
agent_demo's specific DOM: this app uses <li class="contact" data-contact-id>
instead of <div class="task" data-id>, and a `.banner[role=status]` toast
instead of `#toast`. Nothing in src/agent, src/reconcile, or src/browser
changes to support it -- only the BrowserAgent(row_selector=..., ...) call
site does (see scripts/run_demo.py's run_contacts_demo()).

Two bugs, same classes as agent_demo, different flows:
  1. Fake archive (NO_REQUEST) -- clicking Archive hides the contact and
     shows "Archived!", but never calls the API when bugs are on.
  2. Fake update (UI_LIED) -- editing a phone number and clicking Save shows
     "Updated!" (note: a different, still-generic toast word than
     agent_demo's "Saved!" -- proving the heuristic vocab isn't tuned to one
     app's copy), but the request body omits the field, so the backend keeps
     the old value.
  3. Leak -- the contact list response carries `_private_notes`, never
     rendered but present in the payload.
"""
from __future__ import annotations

import copy

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="Veritas contacts demo target")

_SEED = [
    {"id": 1, "name": "Alice Cooper", "phone": "555-0101", "_private_notes": "VIP, handle personally"},
    {"id": 2, "name": "Bilal Khan", "phone": "555-0102", "_private_notes": "past due invoice"},
]
_db: list[dict] = copy.deepcopy(_SEED)


def _reset():
    global _db
    _db = copy.deepcopy(_SEED)


@app.get("/api/contacts")
def list_contacts(bugs: str = "on"):
    if bugs == "off":
        return [{k: v for k, v in c.items() if not k.startswith("_")} for c in _db]
    return _db  # leaks _private_notes


@app.delete("/api/contacts/{contact_id}")
def archive_contact(contact_id: int):
    global _db
    _db = [c for c in _db if c["id"] != contact_id]
    return {"status": "archived", "id": contact_id}


@app.put("/api/contacts/{contact_id}")
def update_contact(contact_id: int, payload: dict, bugs: str = "on"):
    for c in _db:
        if c["id"] == contact_id:
            if "phone" in payload:
                c["phone"] = payload["phone"]
            if bugs == "off":
                return {k: v for k, v in c.items() if not k.startswith("_")}
            return c
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
<title>Contacts</title>
<style>
  body { font-family: Arial, sans-serif; max-width: 520px; margin: 40px auto; }
  h1 { font-size: 20px; }
  ul#contacts { list-style: none; padding: 0; }
  li.contact { display: flex; align-items: center; gap: 8px; padding: 10px;
               border: 1px solid #ddd; border-radius: 6px; margin-bottom: 8px; }
  li.contact .name { font-weight: 600; min-width: 120px; }
  li.contact input[type=text] { flex: 1; border: none; font-size: 15px; }
  li.contact button { border: none; border-radius: 4px; padding: 6px 10px; cursor: pointer; }
  .save-btn { background: #2b6fdc; color: white; }
  .archive-btn { background: #6c757d; color: white; }
  .banner { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
            background: #222; color: white; padding: 10px 18px; border-radius: 6px;
            opacity: 0; transition: opacity .2s; }
  .banner.show { opacity: 1; }
</style>
</head>
<body>
<h1>Contacts</h1>
<ul id="contacts"></ul>
<div class="banner" role="status"></div>
<script>
const BUGS = new URLSearchParams(location.search).get('bugs') || 'on';

function banner(msg) {
  const b = document.querySelector('.banner');
  b.textContent = msg; b.classList.add('show');
  setTimeout(() => b.classList.remove('show'), 1200);
}

async function load() {
  const res = await fetch('/api/contacts?bugs=' + BUGS);
  const contacts = await res.json();
  const ul = document.getElementById('contacts');
  ul.innerHTML = '';
  for (const c of contacts) {
    const li = document.createElement('li');
    li.className = 'contact';
    li.dataset.contactId = c.id;
    li.innerHTML = `
      <span class="name">${c.name}</span>
      <input type="text" value="${c.phone}" data-contact-id="${c.id}">
      <button class="save-btn" data-contact-id="${c.id}">Save</button>
      <button class="archive-btn" data-contact-id="${c.id}">Archive</button>`;
    ul.appendChild(li);
  }
}

document.addEventListener('click', async (e) => {
  const id = e.target.dataset.contactId;
  if (!id) return;

  if (e.target.classList.contains('archive-btn')) {
    // BUG 1 (fake archive): remove from view + banner, but skip the API call when bugs are on
    e.target.closest('li.contact').remove();
    banner('Archived!');
    if (BUGS === 'off') {
      await fetch('/api/contacts/' + id, { method: 'DELETE' });
    }
    return;
  }

  if (e.target.classList.contains('save-btn')) {
    const input = document.querySelector('input[data-contact-id="' + id + '"]');
    // BUG 2 (fake update): empty body when bugs are on, so phone never persists
    const body = (BUGS === 'off') ? { phone: input.value } : {};
    await fetch('/api/contacts/' + id + '?bugs=' + BUGS, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    banner('Updated!');
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
