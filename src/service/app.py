"""Small local API used by the Satya web UI and browser extension."""
from __future__ import annotations
import asyncio
import secrets
import time
from pathlib import Path
from typing import Any
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from planner import plan_run, inspect_repository

app = FastAPI(title="Satya QA Service")
RUNS: dict[str, dict[str, Any]] = {}
STATIC_DIR = Path(__file__).with_name("static")


class RunRequest(BaseModel):
    url: str | None = None
    repository: str | None = None
    branch: str | None = None
    flows: list[dict[str, Any]] = Field(default_factory=list)
    allow_mutations: bool = False


def _url_smoke(url: str) -> dict[str, Any]:
    """Safe, read-only URL audit used when no explicit flows are supplied."""
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
        browser.close()
    return {"url": url, "http_status": response.status if response else None,
            "title": title, "links": links, "forms": forms,
            "console_errors": [x for x in console if x.startswith("error:")],
            "failed_requests": failed}


async def _prepare(run_id: str, request: RunRequest) -> None:
    run = RUNS[run_id]
    run["status"] = "inspecting"
    if request.repository and Path(request.repository).is_dir():
        run["repository_inspection"] = inspect_repository(request.repository)
    if request.url:
        run["status"] = "running"
        try:
            run["browser_audit"] = await asyncio.to_thread(_url_smoke, request.url)
            run["status"] = "complete"
            run["message"] = "Safe URL smoke audit completed."
        except Exception as exc:
            run["status"] = "failed"
            run["message"] = f"URL audit failed: {exc}"
    else:
        run["status"] = "ready"
        run["message"] = "Repository inspected; local executor is ready for an explicit run."


@app.post("/runs", status_code=202)
async def create_run(request: RunRequest):
    try:
        plan = plan_run(url=request.url, repository=request.repository,
                        branch=request.branch, allow_mutations=request.allow_mutations)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    run_id = secrets.token_urlsafe(12)
    RUNS[run_id] = {"id": run_id, "status": "queued", "created_at": time.time(),
                    "mode": plan.mode.value, "url": plan.url, "repository": plan.repository,
                    "branch": plan.branch, "safe_only": plan.safe_only, "flows": request.flows}
    asyncio.create_task(_prepare(run_id, request))
    return RUNS[run_id]


@app.get("/runs/{run_id}")
async def get_run(run_id: str):
    if run_id not in RUNS:
        raise HTTPException(404, "run not found")
    return RUNS[run_id]


@app.get("/health")
async def health():
    return {"service": "satya", "status": "ok"}

@app.get("/", include_in_schema=False)
async def dashboard():
    return FileResponse(STATIC_DIR / "index.html")
