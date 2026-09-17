import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from planner import RunMode, plan_run, inspect_repository


def test_plan_selects_three_input_modes(tmp_path):
    assert plan_run(url="https://example.test").mode == RunMode.URL
    assert plan_run(repository=str(tmp_path)).mode == RunMode.REPOSITORY
    assert plan_run(url="https://example.test", repository=str(tmp_path)).mode == RunMode.COMBINED


def test_plan_requires_an_input():
    import pytest
    with pytest.raises(ValueError): plan_run()


def test_repository_inspection_is_non_executing(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies":{"react":"1"}}')
    info = inspect_repository(tmp_path)
    assert info["framework"] == "react"
    assert info["has_tests"] is False
