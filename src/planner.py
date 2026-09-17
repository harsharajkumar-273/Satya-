"""Plan a Satya run from the inputs supplied by a UI or browser extension."""
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class RunMode(str, Enum):
    URL = "url"
    REPOSITORY = "repository"
    COMBINED = "combined"


@dataclass(frozen=True)
class RunPlan:
    mode: RunMode
    url: str | None = None
    repository: str | None = None
    branch: str | None = None
    safe_only: bool = True


def plan_run(*, url: str | None = None, repository: str | None = None,
             branch: str | None = None, allow_mutations: bool = False) -> RunPlan:
    if not url and not repository:
        raise ValueError("provide a website URL, a repository, or both")
    mode = RunMode.COMBINED if url and repository else RunMode.URL if url else RunMode.REPOSITORY
    return RunPlan(mode, url, repository, branch, not allow_mutations)


def inspect_repository(path: str | Path) -> dict[str, Any]:
    """Lightweight framework/package discovery; never executes repository code."""
    root = Path(path)
    if not root.is_dir():
        raise ValueError(f"repository path is not a directory: {path}")
    files = {p.name for p in root.iterdir()}
    package = root / "package.json"
    pyproject = root / "pyproject.toml"
    framework = "unknown"
    if package.exists():
        text = package.read_text(errors="ignore").lower()
        framework = "nextjs" if "next" in text else "react" if "react" in text else "node"
    elif pyproject.exists() or (root / "requirements.txt").exists():
        framework = "python"
    commands = []
    if (root / "package.json").exists(): commands.append("npm run dev")
    if (root / "manage.py").exists(): commands.append("python manage.py runserver")
    if (root / "pyproject.toml").exists(): commands.append("python -m uvicorn <module>:app")
    return {"path": str(root), "framework": framework, "top_level": sorted(files),
            "suggested_commands": commands, "has_tests": (root / "tests").exists()}
