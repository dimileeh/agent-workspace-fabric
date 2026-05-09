"""Regression tests for the authoritative GitHub Actions coverage gate."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
DB_URL = "postgresql+asyncpg://awf:awf_ci@localhost:5432/awf"


def _workflow() -> dict[str, Any]:
    loaded = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _run_steps(job: dict[str, Any]) -> str:
    return "\n".join(
        str(step.get("run", "")) for step in job.get("steps", []) if isinstance(step, dict)
    )


@pytest.mark.unit
def test_ci_has_authoritative_python_full_coverage_job() -> None:
    jobs = _workflow()["jobs"]
    job = jobs["python-full-coverage"]

    assert job["runs-on"] == "ubuntu-latest-8-cores"

    postgres = job["services"]["postgres"]
    assert postgres["image"] == "postgres:16"
    assert postgres["env"] == {
        "POSTGRES_USER": "awf",
        "POSTGRES_PASSWORD": "awf_ci",
        "POSTGRES_DB": "awf",
    }

    commands = _run_steps(job)
    assert "docker version" in commands
    assert "docker compose version" in commands
    assert "docker build -t awf-agent-runtime:latest -f docker/agent-runtime.Dockerfile ." in commands
    assert "uv run --python 3.12 --extra dev pytest" in commands
    assert "-n 8" in commands
    assert "--dist=loadscope" in commands
    assert "--cov=awf" in commands
    assert "--cov-report=term-missing" in commands
    assert "--cov-report=xml" in commands
    assert "--cov-fail-under=99" in commands

    assert "--cov-fail-under=0" not in commands
    assert "pytest tests/unit" not in commands

    coverage_steps = [
        step for step in job["steps"] if isinstance(step, dict) and step.get("name") == "Full coverage"
    ]
    assert len(coverage_steps) == 1
    env = coverage_steps[0]["env"]
    assert env["CI"] == "true"
    assert env["AWF_DATABASE_URL"] == DB_URL
    assert env["AWF_TEST_DATABASE_URL"] == DB_URL
    assert env.get("AWF_SKIP_DOCKER_TESTS") not in ("1", 1)


@pytest.mark.unit
def test_unit_smoke_job_is_not_the_authoritative_coverage_gate() -> None:
    commands = _run_steps(_workflow()["jobs"]["lint-and-test"])

    assert "pytest tests/unit/" in commands
    assert "--cov=awf" not in commands
    assert "--cov-fail-under" not in commands
