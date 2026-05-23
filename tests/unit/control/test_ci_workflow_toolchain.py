"""Regression checks for the GitHub Actions Python toolchain setup."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_WORKFLOW_PATH = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "ci.yml"
_SETUP_UV_REF_RE = re.compile(r"^astral-sh/setup-uv@v(?P<major>\d+)(?:\.\d+){0,2}$")
_CONCRETE_UV_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _workflow_jobs() -> dict[str, Any]:
    workflow = yaml.safe_load(_WORKFLOW_PATH.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    return jobs


@pytest.mark.unit
def test_python_ci_setup_uv_uses_current_action_and_concrete_uv_version() -> None:
    setup_uv_steps: list[tuple[str, dict[str, Any]]] = []

    for job_name, job in _workflow_jobs().items():
        steps = job.get("steps", [])
        assert isinstance(steps, list)
        for step in steps:
            if isinstance(step, dict) and str(step.get("uses", "")).startswith(
                "astral-sh/setup-uv@"
            ):
                setup_uv_steps.append((job_name, step))

    assert {job_name for job_name, _step in setup_uv_steps} == {
        "lint-and-type",
        "python-full-coverage",
        "release-artifacts",
    }

    for job_name, step in setup_uv_steps:
        uses = step["uses"]
        assert isinstance(uses, str)
        setup_uv_ref = _SETUP_UV_REF_RE.fullmatch(uses)
        assert setup_uv_ref is not None
        assert int(setup_uv_ref.group("major")) >= 8

        with_config = step.get("with", {})
        assert isinstance(with_config, dict)
        uv_version = with_config.get("version")
        assert isinstance(uv_version, str)
        assert _CONCRETE_UV_VERSION_RE.fullmatch(uv_version), (
            f"{job_name} setup-uv must pin a concrete uv version"
        )
