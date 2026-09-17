"""Satya command-line entry point.

The legacy ``veritas_cli.py`` wrapper remains available for existing pipelines.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cli import main

if __name__ == "__main__":
    raise SystemExit(main())
