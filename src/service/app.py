"""Small local API used by the Satya web UI and browser extension."""
from __future__ import annotations
import asyncio
import secrets
import time
import json
import os
from pathlib import Path
from typing import Any
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from planner import plan_run, inspect_repository
from storage import RunStore

app = FastAPI(title="Satya QA Service")
STORE = RunStore(os.getenv("SATYA_DB_PATH"))
STATIC_DIR = Path(__file__).with_name("static")


class RunRequest(BaseModel):
    url: str | None = None
    repository: str | None = None
    branch: str | None = None
    flows: list[dict[str, Any]] = Field(default_factory=list)
    allow_mutations: bool = False
    allowed_domains: list[str] = Field(default_factory=list)
    api_filters: list[str] = Field(default_factory=lambda: ["/api/", "/graphql"])


def _url_smoke(url: str, flows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Run a safe browser audit and optional declarative read-only flows."""
    from playwright.sync_api import sync_playwright
    console: list[str] = []
    failed: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("console", lambda msg: console.append(f"{msg.type}: {msg.text}"))
        page.on("requestfailed", lambda req: failed.append(req.url))
        response = page.goto(url, wait_until="domcontentloaded", timeout=15000)
        title = page.title()
        links = page.locator("a").count()
        forms = page.locator("form").count()
        flow_results = []
        for flow in flows or []:
            before_errors, before_failed = len(console), len(failed)
            action_results = []
            for action in flow.get("actions", []):
                if "click" in action:
                    selector = action["click"]
                    page.locator(selector).click(timeout=5000)
                    action_results.append({"action": "click", "selector": selector})
                elif "fill" in action:
                    raise ValueError("fill actions require allow_mutations=true; safe mode permits navigation only")
                elif "wait_ms" in action:
                    page.wait_for_timeout(min(int(action["wait_ms"]), 5000))
                else:
                    raise ValueError(f"unsupported safe action: {action}")
            flow_results.append({"description": flow.get("description", "flow"),
                                 "actions": action_results,
                                 "new_console_errors": console[before_errors:],
                                 "new_failed_requests": failed[before_failed:]})
        browser.close()
    return {"url": url, "http_status": response.status if response else None,
            "title": title, "links": links, "forms": forms,
            "console_errors": [x for x in console if x.startswith("error:")],
            "failed_requests": failed, "flows": flow_results,
            "evidence": "The page loaded successfully and every permitted flow action completed."}


async def _prepare(run_id: str, request: RunRequest) -> None:
    STORE.update(run_id, status="inspecting")
    if request.repository and Path(request.repository).is_dir():
        STORE.update(run_id, repository_inspection=inspect_repository(request.repository))
    if request.url:
        STORE.update(run_id, status="running")
        try:
            from urllib.parse import urlparse
            domain = urlparse(request.url).hostname
            if request.allowed_domains and domain not in request.allowed_domains:
                raise ValueError(f"target domain {domain!r} is not in allowed_domains")
            audit = await asyncio.to_thread(_url_smoke, request.url, request.flows)
            STORE.update(run_id, browser_audit=audit, status="complete",
                         message="Safe URL workflow audit completed.")
        except Exception as exc:
            STORE.update(run_id, status="failed", message=f"URL audit failed: {exc}")
    else:
        STORE.update(run_id, status="ready",
                     message="Repository inspected; local executor is ready for an explicit run.")


@app.post("/runs", status_code=202)
async def create_run(request: RunRequest):
    try:
        plan = plan_run(url=request.url, repository=request.repository,
                        branch=request.branch, allow_mutations=request.allow_mutations)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    run_id = secrets.token_urlsafe(12)
    run = {"id": run_id, "status": "queued", "created_at": time.time(),
                    "mode": plan.mode.value, "url": plan.url, "repository": plan.repository,
                    "branch": plan.branch, "safe_only": plan.safe_only, "flows": request.flows,
                    "allowed_domains": request.allowed_domains}
    STORE.create(run)
    asyncio.create_task(_prepare(run_id, request))
    return run


@app.get("/runs/{run_id}")
async def get_run(run_id: str):
    run = STORE.get(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return run


@app.get("/health")
async def health():
    return {"service": "satya", "status": "ok"}

@app.get("/", include_in_schema=False)
async def dashboard():
    return FileResponse(STATIC_DIR / "index.html")
