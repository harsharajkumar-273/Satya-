#!/usr/bin/env python3
"""
Thin executable wrapper for the Veritas CLI (see src/cli.py for the logic).

Run from the repo root, e.g.:
    python scripts/veritas_cli.py --config flows.example.yaml --html-report report.html
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
