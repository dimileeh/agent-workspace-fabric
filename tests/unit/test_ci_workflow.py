"""Regression tests for GitHub Actions workflow contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _ci_workflow() -> dict[str, Any]:
    workflow = yaml.safe_load((_repo_root() / ".github" / "workflows" / "ci.yml").read_text())
    assert isinstance(workflow, dict)
    return workflow


def _job_step(job_name: str, step_name: str) -> dict[str, Any]:
    job = _ci_workflow()["jobs"][job_name]
    assert isinstance(job, dict)
    steps = job["steps"]
    assert isinstance(steps, list)
    for step in steps:
        assert isinstance(step, dict)
        if step.get("name") == step_name:
            return step
    raise AssertionError(f"{job_name!r} does not define step {step_name!r}")


@pytest.mark.unit
def test_lint_job_installs_locked_dev_dependencies() -> None:
    install_step = _job_step("lint-and-type", "Install dependencies")
    run = install_step["run"]

    assert isinstance(run, str)
    assert "uv sync --python 3.12 --locked --extra dev" in run
    assert 'uv pip install -e ".[dev]"' not in run
