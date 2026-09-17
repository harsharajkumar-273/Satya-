"""
Command-line entry point for Veritas.

Turns the library into something a CI pipeline can call directly, with no
Python glue: point it at a running app and a YAML/JSON file describing which
flows to verify, and it prints a summary, optionally writes an HTML and/or
JUnit report, and exits non-zero if any flow's UI claim didn't match backend
reality (NO_CLAIM findings do not fail the run -- see agent.loop.summarize).

    python -m cli --config flows.yaml --html-report report.html

See flows.example.yaml (repo root) for the config format. This module is
split from scripts/veritas_cli.py so its pieces (config loading, the action
interpreter, exit-code logic) are importable and unit-testable without a
live browser.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from agent.loop import summarize, verify_action
from browser.agent import BrowserAgent
from models import FlowResult, Verdict
from report.html import render_html_report
from report.junit import render_junit_report

__all__ = ["build_arg_parser", "load_config", "build_do", "run_flows", "main"]


def build_do(actions: list[dict[str, Any]]) -> Callable:
    """
    Turn a flow's declarative action list (from the YAML/JSON config) into
    the `do(page)` callable verify_action expects. Kept separate from
    run_flows so the interpreter itself -- the part most likely to need a
    new action kind someday -- is unit-testable against a fake `page`
    object instead of a real browser.
    """
    def do(page: Any) -> None:
        for i, step in enumerate(actions):
            if "click" in step:
                page.click(step["click"])
            elif "fill" in step:
                spec = step["fill"]
                page.fill(spec["selector"], spec["value"])
            elif "press" in step:
                spec = step["press"]
                page.press(spec["selector"], spec["key"])
            elif "check" in step:
                page.check(step["check"])
            elif "wait_ms" in step:
                page.wait_for_timeout(step["wait_ms"])
            else:
                raise ValueError(f"action[{i}]: unrecognized step {step!r} "
                                  f"(expected one of: click, fill, press, check, wait_ms)")
    return do


def load_config(path: str) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    if path.endswith((".yaml", ".yml")):
        import yaml  # optional dependency; only needed for YAML configs
        return yaml.safe_load(text)
    return json.loads(text)


def run_flows(config: dict[str, Any]) -> list[FlowResult]:
    base_url = config["base_url"]
    selectors = config.get("selectors", {})
    entry_path = config.get("entry_path", "/")
    default_backend_read_path = config.get("backend_read_path", "/api/tasks")
    default_poll_timeout = config.get("poll_timeout", 0.0)
    flows = config.get("flows", [])

    if not flows:
        raise ValueError("config has no 'flows' to run")

    results: list[FlowResult] = []
    with BrowserAgent(
        base_url,
        headless=config.get("headless", True),
        row_selector=selectors.get("row_selector", ".task"),
        toast_selector=selectors.get("toast_selector", "#toast"),
        id_attr=selectors.get("id_attr", "data-id"),
    ) as agent:
        agent.goto(entry_path)
        for flow in flows:
            results.append(
                verify_action(
                    agent,
                    flow["description"],
                    build_do(flow["actions"]),
                    backend_read_path=flow.get("backend_read_path", default_backend_read_path),
                    poll_timeout=flow.get("poll_timeout", default_poll_timeout),
                )
            )
    return results


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="veritas",
        description="Run Veritas verification flows against a running app and "
                     "report whether the UI told the truth about what the backend did.",
    )
    p.add_argument("--config", required=True, help="Path to a flows YAML or JSON file")
    p.add_argument("--html-report", metavar="PATH", help="Write a self-contained HTML report here")
    p.add_argument("--junit-report", metavar="PATH", help="Write a JUnit XML report here (for CI)")
    p.add_argument("--quiet", action="store_true", help="Only print the final summary line")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except Exception as exc:
        print(f"veritas: could not load config {args.config!r}: {exc}", file=sys.stderr)
        return 2

    try:
        results = run_flows(config)
    except Exception as exc:
        print(f"veritas: run failed: {exc}", file=sys.stderr)
        return 2

    if not args.quiet:
        for r in results:
            print(f"\nFlow: {r.flow}")
            for f in r.findings:
                mark = "OK " if f.verdict == Verdict.AGREE else "!! "
                print(f"  {mark}[{f.verdict.value}] {f.summary}")

    summary = summarize(results)
    print(
        f"\n{summary['problems_found']} problem(s), "
        f"{summary['inconclusive_found']} inconclusive, "
        f"across {summary['flows_checked']} flow(s) -> verdicts: {summary['verdicts']}"
    )

    if args.html_report:
        path = render_html_report(results, args.html_report)
        print(f"HTML report written to {path}")
    if args.junit_report:
        path = render_junit_report(results, args.junit_report)
        print(f"JUnit report written to {path}")

    return 1 if summary["problems_found"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
