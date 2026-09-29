"""Durable run storage for Satya's API and workers."""
from __future__ import annotations
import json
import os
import sqlite3
from pathlib import Path
from typing import Any


class RunStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else Path(os.getenv("SATYA_DB_PATH", "/tmp/satya-runs.sqlite3"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at REAL NOT NULL,
                payload TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}')""")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def create(self, run: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as db:
            db.execute("INSERT INTO runs(id,status,created_at,payload) VALUES(?,?,?,?)",
                       (run["id"], run["status"], run["created_at"], json.dumps(run)))
        return run

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT payload,result FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        data = json.loads(row["payload"])
        data.update(json.loads(row["result"]))
        return data

    def update(self, run_id: str, **changes: Any) -> dict[str, Any]:
        current = self.get(run_id)
        if current is None:
            raise KeyError(run_id)
        current.update(changes)
        base_keys = {"id", "status", "created_at", "mode", "url", "repository", "branch", "safe_only", "flows"}
        base = {k: current[k] for k in base_keys if k in current}
        result = {k: v for k, v in current.items() if k not in base_keys}
        with self._connect() as db:
            db.execute("UPDATE runs SET status=?, payload=?, result=? WHERE id=?",
                       (current["status"], json.dumps(base), json.dumps(result), run_id))
        return current
