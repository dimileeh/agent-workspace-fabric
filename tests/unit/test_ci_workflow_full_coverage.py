"""Regression tests for the authoritative GitHub Actions coverage gate."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
DB_URL = "postgresql+asyncpg://awf:awf_ci@localhost:5432/awf"
DOCKER_SKIP_ENV = "AWF_SKIP_DOCKER_TESTS"
TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}


def _workflow() -> dict[str, Any]:
    loaded = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _job(workflow: dict[str, Any], name: str) -> dict[str, Any]:
    jobs = workflow.get("jobs", {})
    assert isinstance(jobs, dict)
    job = jobs.get(name)
    assert isinstance(job, dict)
    return job


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps", [])
    assert isinstance(steps, list)
    return [step for step in steps if isinstance(step, dict)]


def _named_step(job: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [step for step in _steps(job) if step.get("name") == name]
    assert len(matches) == 1
    return matches[0]


def _run_steps(job: dict[str, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in _steps(job))


def _env_mapping(scope: str, value: object) -> dict[str, Any]:
    assert isinstance(value, dict), f"{scope} env must be a mapping"
    return value


def _env_scopes(
    workflow: dict[str, Any],
    job: dict[str, Any],
    step: dict[str, Any],
) -> tuple[tuple[str, dict[str, Any]], ...]:
    return (
        ("workflow", _env_mapping("workflow", workflow.get("env", {}))),
        ("job", _env_mapping("job", job.get("env", {}))),
        ("step", _env_mapping("step", step.get("env", {}))),
    )


def _effective_env(
    workflow: dict[str, Any],
    job: dict[str, Any],
    step: dict[str, Any],
) -> dict[str, Any]:
    env: dict[str, Any] = {}
    for _scope, scoped_env in _env_scopes(workflow, job, step):
        env.update(scoped_env)
    return env


def _env_truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in TRUTHY_ENV_VALUES


def _assert_docker_skip_env_disabled(
    workflow: dict[str, Any],
    job: dict[str, Any],
    step: dict[str, Any],
) -> None:
    offenders = [
        scope
        for scope, scoped_env in _env_scopes(workflow, job, step)
        if _env_truthy(scoped_env.get(DOCKER_SKIP_ENV, ""))
    ]
    assert not offenders, f"{DOCKER_SKIP_ENV} must not be truthy in: {', '.join(offenders)}"


@pytest.mark.unit
def test_ci_has_authoritative_python_full_coverage_job() -> None:
    workflow = _workflow()
    job = _job(workflow, "python-full-coverage")

    assert job.get("runs-on") == "ubuntu-latest-8-cores"

    services = job.get("services", {})
    assert isinstance(services, dict)
    postgres = services.get("postgres")
    assert isinstance(postgres, dict)
    assert postgres.get("image") == "postgres:16"
    assert postgres.get("env") == {
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

    coverage_step = _named_step(job, "Full coverage")
    env = _effective_env(workflow, job, coverage_step)
    assert env.get("CI") == "true"
    assert env.get("AWF_DATABASE_URL") == DB_URL
    assert env.get("AWF_TEST_DATABASE_URL") == DB_URL
    _assert_docker_skip_env_disabled(workflow, job, coverage_step)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("scope", "value"),
    [
        ("workflow", "true"),
        ("job", "yes"),
        ("step", "on"),
        ("step", True),
    ],
)
def test_full_coverage_job_rejects_truthy_docker_skip_env_at_any_scope(
    scope: str,
    value: object,
) -> None:
    workflow = {"env": {}, "jobs": {"python-full-coverage": {"env": {}, "steps": []}}}
    job = workflow["jobs"]["python-full-coverage"]
    step: dict[str, object] = {"env": {}}

    if scope == "workflow":
        workflow["env"][DOCKER_SKIP_ENV] = value
    elif scope == "job":
        job["env"][DOCKER_SKIP_ENV] = value
    else:
        step["env"][DOCKER_SKIP_ENV] = value

    with pytest.raises(AssertionError, match=DOCKER_SKIP_ENV):
        _assert_docker_skip_env_disabled(workflow, job, step)


@pytest.mark.unit
def test_unit_smoke_job_is_not_the_authoritative_coverage_gate() -> None:
    commands = _run_steps(_job(_workflow(), "lint-and-test"))

    assert "pytest tests/unit/" in commands
    assert "--cov=awf" not in commands
    assert "--cov-fail-under" not in commands
