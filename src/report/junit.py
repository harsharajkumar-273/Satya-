"""
JUnit XML report for a Veritas run.

Most CI systems (GitHub Actions, GitLab, Jenkins) already know how to render
a JUnit XML file as pass/fail test results with annotations on the diff --
this lets a Veritas run slot into that same view instead of being just
stdout text nobody reads until something's already on fire.

Pure function of FlowResult data, using only the stdlib (xml.etree), so it
carries no new dependency and is unit-testable without a browser.
"""
from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

from agent.loop import HARD_FAILURE_VERDICTS
from models import FlowResult, Verdict


def render_junit_report(
    results: list[FlowResult],
    out_path: str | Path,
    *,
    suite_name: str = "veritas",
) -> Path:
    """Render `results` as a JUnit XML file at out_path. Returns the Path written."""
    failures = 0
    skipped = 0

    testsuite = ET.Element(
        "testsuite",
        {"name": suite_name, "tests": str(len(results))},
    )

    for result in results:
        hard = [f for f in result.findings if f.verdict in HARD_FAILURE_VERDICTS]
        inconclusive = [f for f in result.findings if f.verdict == Verdict.NO_CLAIM]

        testcase = ET.SubElement(
            testsuite,
            "testcase",
            {"classname": suite_name, "name": result.flow},
        )

        if hard:
            failures += 1
            message = "; ".join(f"[{f.verdict.value}] {f.summary}" for f in hard)
            text = "\n\n".join(f"[{f.verdict.value}] {f.summary}\n{f.detail}" for f in hard)
            failure_el = ET.SubElement(
                testcase, "failure", {"message": message, "type": hard[0].verdict.value}
            )
            failure_el.text = text
        elif inconclusive and not any(f.verdict == Verdict.AGREE for f in result.findings):
            # Nothing failed, but Veritas also never confirmed anything -- report
            # this as skipped rather than a silent pass, so coverage gaps show
            # up in the same CI view instead of only in the HTML report.
            skipped += 1
            skipped_el = ET.SubElement(
                testcase,
                "skipped",
                {"message": "Insufficient evidence -- Veritas could not verify this flow"},
            )
            skipped_el.text = inconclusive[0].detail

    testsuite.set("failures", str(failures))
    testsuite.set("skipped", str(skipped))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(testsuite).write(out_path, encoding="unicode", xml_declaration=True)
    return out_path
