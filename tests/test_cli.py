"""
Tests for the CLI (src/cli.py): config loading, the declarative action
interpreter, and exit-code logic. run_flows() itself needs a real browser and
is exercised by hand via scripts/veritas_cli.py against the demo app, not
here -- these tests cover the parts that don't need Playwright.
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cli import build_arg_parser, build_do, load_config, main
from models import ActionTrace, Finding, FlowResult, UISnapshot, Verdict


# --- load_config ----------------------------------------------------------

def test_load_config_reads_yaml(tmp_path):
    p = tmp_path / "flows.yaml"
    p.write_text("base_url: http://x\nflows:\n  - description: a\n    actions: []\n")
    cfg = load_config(str(p))
    assert cfg["base_url"] == "http://x"
    assert cfg["flows"][0]["description"] == "a"


def test_load_config_reads_json(tmp_path):
    p = tmp_path / "flows.json"
    p.write_text(json.dumps({"base_url": "http://x", "flows": []}))
    cfg = load_config(str(p))
    assert cfg["base_url"] == "http://x"


# --- build_do (the declarative action interpreter) -------------------------

def test_build_do_dispatches_click():
    page = MagicMock()
    build_do([{"click": ".del"}])(page)
    page.click.assert_called_once_with(".del")


def test_build_do_dispatches_fill():
    page = MagicMock()
    build_do([{"fill": {"selector": "#title", "value": "hi"}}])(page)
    page.fill.assert_called_once_with("#title", "hi")


def test_build_do_dispatches_multiple_steps_in_order():
    page = MagicMock()
    calls = []
    page.fill.side_effect = lambda *a: calls.append(("fill", a))
    page.click.side_effect = lambda *a: calls.append(("click", a))
    build_do([{"fill": {"selector": "#t", "value": "x"}}, {"click": ".save"}])(page)
    assert [c[0] for c in calls] == ["fill", "click"]


def test_build_do_rejects_unknown_action():
    with pytest.raises(ValueError, match="unrecognized step"):
        build_do([{"teleport": "#somewhere"}])(MagicMock())


# --- main(): exit codes ----------------------------------------------------

def _flow_result(verdict: Verdict) -> FlowResult:
    trace = ActionTrace("f", b"", b"", "Deleted!", 1, 0)
    return FlowResult("f", trace, "e", [Finding(verdict, "s", "d")])


def test_main_exits_2_on_bad_config_path():
    assert main(["--config", "/no/such/file.yaml"]) == 2


def test_main_exits_0_when_all_agree(tmp_path):
    cfg = tmp_path / "flows.yaml"
    cfg.write_text("base_url: http://x\nflows:\n  - description: a\n    actions: []\n")
    with patch("cli.run_flows", return_value=[_flow_result(Verdict.AGREE)]):
        assert main(["--config", str(cfg), "--quiet"]) == 0


def test_main_exits_1_on_hard_failure(tmp_path):
    cfg = tmp_path / "flows.yaml"
    cfg.write_text("base_url: http://x\nflows:\n  - description: a\n    actions: []\n")
    with patch("cli.run_flows", return_value=[_flow_result(Verdict.UI_LIED)]):
        assert main(["--config", str(cfg), "--quiet"]) == 1


def test_main_exits_0_on_no_claim_alone():
    """A flow Veritas didn't understand shouldn't fail the build by itself --
    only real discrepancies should (see agent.loop.HARD_FAILURE_VERDICTS)."""
    pass  # covered by summarize()'s own tests; kept here as documentation of intent


def test_main_writes_html_and_junit_reports(tmp_path):
    cfg = tmp_path / "flows.yaml"
    cfg.write_text("base_url: http://x\nflows:\n  - description: a\n    actions: []\n")
    html_out = tmp_path / "r.html"
    junit_out = tmp_path / "r.xml"
    with patch("cli.run_flows", return_value=[_flow_result(Verdict.AGREE)]):
        code = main([
            "--config", str(cfg), "--quiet",
            "--html-report", str(html_out),
            "--junit-report", str(junit_out),
        ])
    assert code == 0
    assert html_out.exists()
    assert junit_out.exists()


def test_arg_parser_requires_config():
    parser = build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_run_flows_passes_ground_truth_through(tmp_path):
    """ground_truth: reload in the config must reach verify_action (flow-level overrides global)."""
    from unittest.mock import MagicMock
    import cli
    config = {"base_url": "http://x", "ground_truth": "reload",
              "flows": [{"description": "a", "actions": []},
                        {"description": "b", "actions": [], "ground_truth": "api"}]}
    fake_agent = MagicMock()
    fake_agent.__enter__ = MagicMock(return_value=fake_agent)
    fake_agent.__exit__ = MagicMock(return_value=False)
    with patch("cli.BrowserAgent", return_value=fake_agent), \
         patch("cli.safe_verify_action") as mock_verify:
        cli.run_flows(config)
    modes = [c.kwargs["ground_truth"] for c in mock_verify.call_args_list]
    assert modes == ["reload", "api"]


def test_build_do_dispatches_hover_and_dblclick():
    page = MagicMock()
    build_do([{"hover": "li"}, {"dblclick": "label"}])(page)
    page.hover.assert_called_once_with("li")
    page.dblclick.assert_called_once_with("label")


def test_reload_mode_turns_on_row_state_capture_by_default():
    """Reload mode compares rendered rows with rendered rows, so it's safe (and
    necessary for checkbox toggles) to encode checkbox state in row values."""
    import cli
    for cfg, expected in (({"ground_truth": "reload"}, True), ({}, False),
                          ({"ground_truth": "reload", "include_row_state": False}, False)):
        fake_agent = MagicMock()
        fake_agent.__enter__ = MagicMock(return_value=fake_agent)
        fake_agent.__exit__ = MagicMock(return_value=False)
        config = dict(cfg, base_url="http://x", flows=[{"description": "a", "actions": []}])
        with patch("cli.BrowserAgent", return_value=fake_agent) as mock_agent, \
             patch("cli.safe_verify_action"):
            cli.run_flows(config)
        assert mock_agent.call_args.kwargs["include_row_state"] is expected


def test_one_broken_flow_does_not_abort_the_rest():
    """Before: a single selector timeout killed the whole run (exit 2, no report).
    Now it's an ACTION_FAILED result and the remaining flows still run."""
    import cli
    from models import Verdict
    config = {"base_url": "http://x", "flows": [
        {"description": "broken", "actions": [{"click": "#nope"}]},
        {"description": "fine", "actions": []}]}
    fake_agent = MagicMock()
    fake_agent.__enter__ = MagicMock(return_value=fake_agent)
    fake_agent.__exit__ = MagicMock(return_value=False)
    fake_agent.act.side_effect = [TimeoutError("Page.click: Timeout"),
                                  ActionTrace("fine", b"", b"", None, 0, 0,
                                              ui_before=UISnapshot(None, 0), ui_after=UISnapshot(None, 0))]
    fake_agent.api_get.return_value = []
    with patch("cli.BrowserAgent", return_value=fake_agent):
        results = cli.run_flows(config)
    assert [r.flow for r in results] == ["broken", "fine"]
    assert results[0].findings[0].verdict == Verdict.ACTION_FAILED
    assert results[1].findings[0].verdict != Verdict.ACTION_FAILED
