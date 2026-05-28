"""Regression tests for the authoritative GitHub Actions coverage gate."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PUBLISH_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "publish.yml"
CONTRIBUTING_PATH = REPO_ROOT / "CONTRIBUTING.md"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
DB_URL = "postgresql+asyncpg://awf:awf_ci@localhost:5432/awf"
DOCKER_SKIP_ENV = "AWF_SKIP_DOCKER_TESTS"
TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
GITHUB_HOSTED_RUNNER = "ubuntu-latest"
SETUP_UV_VERSION = "0.5.31"


def _workflow() -> dict[str, Any]:
    loaded = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _publish_workflow() -> dict[str, Any]:
    loaded = yaml.safe_load(PUBLISH_WORKFLOW_PATH.read_text(encoding="utf-8"))
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


def _setup_uv_step(workflow: dict[str, Any], job_name: str) -> dict[str, Any]:
    step = _named_step(_job(workflow, job_name), "Install uv")
    assert step.get("uses") == "astral-sh/setup-uv@v4"
    return step


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

    assert not (isinstance(pull_request_trigger, dict) and pull_request_trigger.get("branches"))


@pytest.mark.unit
def test_ci_cancels_superseded_branch_and_pr_runs() -> None:
    workflow = _workflow()

    assert workflow.get("concurrency") == {
        "group": "${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}",
        "cancel-in-progress": True,
    }


@pytest.mark.unit
def test_ci_has_authoritative_python_full_coverage_job() -> None:
    """The coverage job produces XML and enforces the exact threshold."""
    workflow = _workflow()
    job = _job(workflow, "python-full-coverage")

    assert job.get("runs-on") == GITHUB_HOSTED_RUNNER

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
    assert (
        "docker build -t awf-agent-runtime:latest -f docker/agent-runtime.Dockerfile ." in commands
    )

    full_coverage_run = _step_run(job, "Full coverage")
    assert "uv run --python 3.12 pytest" in full_coverage_run
    assert "-n 8" in full_coverage_run
    assert "--dist=loadscope" in full_coverage_run
    assert "--timeout=300" in full_coverage_run
    assert "--timeout=120" not in full_coverage_run
    assert "--cov=awf" in full_coverage_run
    assert "--cov-report=term-missing" in full_coverage_run
    assert "--cov-report=xml" in full_coverage_run
    assert "--cov-fail-under=99" in full_coverage_run
    assert "--cov-fail-under=0" not in full_coverage_run
    assert "pytest tests/unit" not in full_coverage_run

    exact_threshold_run = _step_run(job, "Enforce exact coverage threshold")
    assert exact_threshold_run == (
        "uv run --python 3.12 python scripts/check_coverage_threshold.py "
        "coverage.xml --minimum-percent 99"
    )

    step_names = [str(step.get("name")) for step in _steps(job)]
    assert step_names.index("Enforce exact coverage threshold") == (
        step_names.index("Full coverage") + 1
    )
    assert step_names.index("Upload full coverage artifact") > step_names.index(
        "Enforce exact coverage threshold"
    )

    coverage_step = _named_step(job, "Full coverage")
    env = _effective_env(workflow, job, coverage_step)
    assert env.get("CI") == "true"
    assert env.get("AWF_DATABASE_URL") == DB_URL
    assert env.get("AWF_TEST_DATABASE_URL") == DB_URL
    _assert_docker_skip_env_disabled(workflow, job, coverage_step)


@pytest.mark.unit
def test_setup_uv_steps_avoid_github_token_release_lookup() -> None:
    workflow = _workflow()

    for job_name in ("lint-and-type", "python-full-coverage", "release-artifacts"):
        step = _setup_uv_step(workflow, job_name)
        with_config = step.get("with")

        assert isinstance(with_config, dict)
        assert with_config.get("version") == SETUP_UV_VERSION
        assert with_config.get("github-token") == ""


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
def test_lint_and_type_job_does_not_duplicate_python_test_execution() -> None:
    workflow = _workflow()
    job = _job(workflow, "lint-and-type")

    assert job.get("runs-on") == GITHUB_HOSTED_RUNNER
    assert job.get("timeout-minutes") == 15
    assert "services" not in job

    commands = _run_steps(job)
    assert "ruff check ." in commands
    assert "ruff format --check ." in commands
    assert ".venv/bin/mypy" in commands
    assert "pytest" not in commands
    assert "--cov=awf" not in commands
    assert "--cov-fail-under" not in commands


@pytest.mark.unit
def test_full_coverage_is_the_only_python_test_job() -> None:
    workflow = _workflow()
    jobs = workflow.get("jobs", {})
    assert isinstance(jobs, dict)

    assert "integration" not in jobs

    python_test_jobs = [
        name for name, job in jobs.items() if isinstance(job, dict) and "pytest" in _run_steps(job)
    ]
    assert python_test_jobs == ["python-full-coverage"]


@pytest.mark.unit
def test_coverage_report_precision_exposes_below_threshold_decimals() -> None:
    """Coverage report precision exposes values just below the 99% gate."""
    pyproject = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    coverage_report = pyproject["tool"]["coverage"]["report"]

    assert coverage_report["precision"] == 2
    assert coverage_report["fail_under"] == 99


@pytest.mark.unit
def test_release_artifacts_installs_wheel_with_uv_pip() -> None:
    run = _step_run(_job(_workflow(), "release-artifacts"), "Install wheel and verify entrypoints")

    assert "cd /tmp" in run
    assert (
        'uv pip install --python /tmp/awf-wheel-smoke/bin/python "$GITHUB_WORKSPACE"/dist/*.whl'
        in run
    )
    assert "bin/pip install" not in run
    assert "/tmp/awf-wheel-smoke/bin/awf --help" in run
    assert "/tmp/awf-wheel-smoke/bin/awf init --help" in run
    assert "/tmp/awf-wheel-smoke/bin/awf service bootstrap --help" in run
    assert "get_bootstrap_asset_root" in run
    assert "docker/agent-runtime.Dockerfile" in run


@pytest.mark.unit
def test_publish_workflow_builds_on_tags_and_uses_trusted_publishing() -> None:
    workflow = _publish_workflow()
    triggers = _workflow_triggers(workflow)

    assert "workflow_dispatch" in triggers
    dispatch = triggers.get("workflow_dispatch")
    assert isinstance(dispatch, dict)
    inputs = dispatch.get("inputs")
    assert isinstance(inputs, dict)
    assert "publish_target" in inputs
    push = triggers.get("push")
    assert isinstance(push, dict)
    assert push.get("tags") == ["v*"]

    jobs = workflow.get("jobs", {})
    assert isinstance(jobs, dict)
    assert {"build", "publish"}.issubset(jobs)

    publish_job = _job(workflow, "publish")
    assert publish_job.get("needs") == "build"
    assert publish_job.get("permissions", {}).get("id-token") == "write"

    commands = _run_steps(_job(workflow, "build"))
    assert "python -m build" in commands
    assert "sha256sum dist/*" in commands

    publish_steps = _steps(publish_job)
    assert any(
        step.get("uses") == "pypa/gh-action-pypi-publish@release/v1" for step in publish_steps
    )
    assert "secrets.PYPI_API_TOKEN" not in PUBLISH_WORKFLOW_PATH.read_text(encoding="utf-8")


@pytest.mark.unit
def test_required_ci_gate_rolls_up_full_coverage_and_required_jobs() -> None:
    job = _job(_workflow(), "ci-required")

    assert job.get("if") == "${{ always() }}"
    assert _job_needs(job) == {
        "lint-and-type",
        "python-full-coverage",
        "console",
        "release-artifacts",
    }

    commands = _run_steps(job)
    assert "A required CI job did not pass." in commands
    assert '!= "success"' in commands


@pytest.mark.unit
def test_contributor_docs_require_ci_rollup_status_check() -> None:
    docs = CONTRIBUTING_PATH.read_text(encoding="utf-8")

    assert "branch protection" in docs.lower()
    assert "ci-required" in docs
    assert "lint-and-type" in docs
    assert "python-full-coverage" in docs
    assert "release-artifacts" in docs
    assert "lint-and-test" not in docs
