"""Regression tests for the authoritative GitHub Actions coverage gate."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CONTRIBUTING_PATH = REPO_ROOT / "CONTRIBUTING.md"
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


def _workflow_triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    triggers = workflow.get("on", workflow.get(True, {}))
    assert isinstance(triggers, dict)
    return triggers


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps", [])
    assert isinstance(steps, list)
    return [step for step in steps if isinstance(step, dict)]


def _named_step(job: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [step for step in _steps(job) if step.get("name") == name]
    assert len(matches) == 1
    return matches[0]


def _step_run(job: dict[str, Any], name: str) -> str:
    run = _named_step(job, name).get("run")
    assert isinstance(run, str)
    return run


def _run_steps(job: dict[str, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in _steps(job))


def _job_needs(job: dict[str, Any]) -> set[str]:
    needs = job.get("needs", [])
    if isinstance(needs, str):
        return {needs}
    assert isinstance(needs, list)
    return {str(need) for need in needs}


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
def test_pull_request_ci_runs_for_every_target_branch() -> None:
    workflow = _workflow()
    triggers = _workflow_triggers(workflow)

    assert "pull_request" in triggers
    pull_request_trigger = triggers.get("pull_request")

    assert not (
        isinstance(pull_request_trigger, dict) and pull_request_trigger.get("branches")
    )


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

    full_coverage_run = _step_run(job, "Full coverage")
    assert "uv run --python 3.12 pytest" in full_coverage_run
    assert "-n 8" in full_coverage_run
    assert "--dist=loadscope" in full_coverage_run
    assert "--cov=awf" in full_coverage_run
    assert "--cov-report=term-missing" in full_coverage_run
    assert "--cov-report=xml" in full_coverage_run
    assert "--cov-fail-under=99" in full_coverage_run
    assert "--cov-fail-under=0" not in full_coverage_run
    assert "pytest tests/unit" not in full_coverage_run

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
        ("workflow", "True"),
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
    workflow = _workflow()
    job = _job(workflow, "lint-and-test")
    commands = _run_steps(job)
    unit_run = _step_run(job, "Unit tests")

    assert "pytest tests/unit/" in commands
    assert "-n 8" in unit_run
    assert "--dist=loadscope" in unit_run
    assert "--cov=awf" not in commands
    assert "--cov-fail-under" not in commands


@pytest.mark.unit
def test_unit_smoke_job_has_postgres_for_database_required_unit_tests() -> None:
    workflow = _workflow()
    job = _job(workflow, "lint-and-test")

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

    unit_step = _named_step(job, "Unit tests")
    env = _effective_env(workflow, job, unit_step)
    assert env.get("CI") == "true"
    assert env.get("AWF_DATABASE_URL") == DB_URL
    assert env.get("AWF_TEST_DATABASE_URL") == DB_URL
    _assert_docker_skip_env_disabled(workflow, job, unit_step)


@pytest.mark.unit
def test_release_artifacts_installs_wheel_with_uv_pip() -> None:
    run = _step_run(_job(_workflow(), "release-artifacts"), "Install wheel and verify entrypoints")

    assert "uv pip install --python .venv-install/bin/python dist/*.whl" in run
    assert ".venv-install/bin/pip install" not in run
    assert ".venv-install/bin/awf --help" in run


@pytest.mark.unit
def test_required_ci_gate_rolls_up_full_coverage_and_required_jobs() -> None:
    job = _job(_workflow(), "ci-required")

    assert job.get("if") == "${{ always() }}"
    assert _job_needs(job) == {
        "lint-and-test",
        "python-full-coverage",
        "console",
        "release-artifacts",
        "integration",
    }

    commands = _run_steps(job)
    assert "A required CI job did not pass." in commands
    assert '!= "success"' in commands


@pytest.mark.unit
def test_contributor_docs_require_ci_rollup_status_check() -> None:
    docs = CONTRIBUTING_PATH.read_text(encoding="utf-8")

    assert "branch protection" in docs.lower()
    assert "ci-required" in docs
    assert "python-full-coverage" in docs
