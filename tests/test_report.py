"""
Tests for the HTML and JUnit report generators. Both are pure functions of
FlowResult data, so these build fixtures by hand rather than driving a real
browser -- consistent with how the rest of this suite keeps reconciliation
logic testable without Playwright.
"""
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from models import ActionTrace, Finding, FlowResult, NetworkCall, Verdict
from report.html import render_html_report
from report.junit import render_junit_report


def _result(flow: str, verdict: Verdict, *, toast="Deleted!", calls=None, png=b"") -> FlowResult:
    trace = ActionTrace(
        flow, png, png, toast, 3, 2,
        network_calls=calls or [NetworkCall("DELETE", "/api/tasks/1", None, 200, {"status": "ok"})],
    )
    return FlowResult(
        flow=flow,
        trace=trace,
        claim_evidence=f"toast={toast!r}",
        findings=[Finding(verdict, f"{verdict.value} summary", f"{verdict.value} detail")],
    )


# --- HTML report --------------------------------------------------------

def test_html_report_writes_file_and_embeds_expected_content(tmp_path):
    results = [
        _result("delete task 1", Verdict.AGREE),
        _result("edit task 2", Verdict.UI_LIED, toast="Saved!"),
    ]
    out = render_html_report(results, tmp_path / "report.html")
    text = out.read_text()

    assert out.exists()
    assert "delete task 1" in text
    assert "edit task 2" in text
    assert "UI_LIED" in text
    assert "AGREE" in text
    # summary stat block reflects summarize()
    assert "1</span><span class=\"l\">problems found" in text


def test_html_report_handles_missing_screenshots_gracefully(tmp_path):
    results = [_result("no-op flow", Verdict.NO_CLAIM, toast=None, png=b"")]
    out = render_html_report(results, tmp_path / "r.html")
    text = out.read_text()
    assert "no before screenshot captured" in text
    assert "no after screenshot captured" in text


def test_html_report_embeds_real_screenshot_as_base64(tmp_path):
    fake_png = b"\x89PNG\r\n\x1a\nfakebytes"
    results = [_result("with screenshot", Verdict.AGREE, png=fake_png)]
    out = render_html_report(results, tmp_path / "r.html")
    text = out.read_text()
    assert "data:image/png;base64," in text


# --- JUnit report --------------------------------------------------------

def test_junit_report_marks_hard_failures_as_failure(tmp_path):
    results = [_result("delete task 1", Verdict.NO_REQUEST)]
    out = render_junit_report(results, tmp_path / "junit.xml")

    root = ET.fromstring(out.read_text())
    assert root.tag == "testsuite"
    assert root.get("failures") == "1"
    testcase = root.find("testcase")
    assert testcase.get("name") == "delete task 1"
    assert testcase.find("failure") is not None


def test_junit_report_marks_no_claim_as_skipped_not_failure(tmp_path):
    results = [_result("no-op flow", Verdict.NO_CLAIM, toast=None)]
    out = render_junit_report(results, tmp_path / "junit.xml")

    root = ET.fromstring(out.read_text())
    assert root.get("failures") == "0"
    assert root.get("skipped") == "1"
    testcase = root.find("testcase")
    assert testcase.find("skipped") is not None
    assert testcase.find("failure") is None


def test_junit_report_clean_flow_has_no_failure_or_skip(tmp_path):
    results = [_result("clean flow", Verdict.AGREE)]
    out = render_junit_report(results, tmp_path / "junit.xml")

    root = ET.fromstring(out.read_text())
    assert root.get("failures") == "0"
    assert root.get("skipped") == "0"
    testcase = root.find("testcase")
    assert testcase.find("failure") is None
    assert testcase.find("skipped") is None
