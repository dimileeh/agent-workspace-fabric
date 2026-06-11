"""Regression tests for the authoritative GitHub Actions coverage gate."""

from __future__ import annotations

import re
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
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _assert_sha_pinned(uses: object, expected_action: str) -> None:
    """Assert a ``uses:`` ref pins ``expected_action`` to a full 40-char commit SHA."""
    assert isinstance(uses, str), (
        f"expected 'uses' to be a string, but got {type(uses).__name__} (action: {expected_action})"
    )
    action, sep, ref = uses.partition("@")
    assert sep == "@" and action == expected_action, (
        f"expected action '{expected_action}', but got '{uses}'"
    )
    assert _SHA_RE.fullmatch(ref), f"{expected_action} must be pinned to a 40-char SHA: {uses}"


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
    _assert_sha_pinned(step.get("uses"), "astral-sh/setup-uv")
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
def test_ci_has_parallel_python_coverage_shards() -> None:
    """Coverage execution runs in balanced GitHub Actions matrix shards."""
    workflow = _workflow()
    job = _job(workflow, "python-coverage-shards")

    assert job.get("runs-on") == GITHUB_HOSTED_RUNNER
    assert job.get("timeout-minutes") == 45
    assert job.get("strategy") == {
        "fail-fast": False,
        "matrix": {"shard": [1, 2, 3, 4, 5, 6, 7, 8]},
    }

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
    assert "docker build -t awf-agent-runtime:latest" not in commands

    shard_run = _step_run(job, "Run coverage shard")
    assert "uv run --python 3.12 coverage run --parallel-mode -m pytest" in shard_run
    assert "shopt -s nullglob" in shard_run
    assert "--splits 8" in shard_run
    assert '--group "${{ matrix.shard }}"' in shard_run
    assert "--timeout=300" in shard_run
    assert "--timeout=120" not in shard_run
    assert "-n 8" not in shard_run
    assert "--dist=loadscope" not in shard_run
    assert "--cov=awf" not in shard_run
    assert "--cov-fail-under" not in shard_run
    assert "pytest tests/unit" not in shard_run

    upload_step = _named_step(job, "Upload coverage shard")
    assert upload_step.get("if") == "${{ always() }}"
    _assert_sha_pinned(upload_step.get("uses"), "actions/upload-artifact")
    assert upload_step.get("with") == {
        "name": "python-coverage-shard-${{ matrix.shard }}",
        "path": "coverage-artifacts/.coverage.shard-${{ matrix.shard }}",
        "include-hidden-files": True,
        "retention-days": 14,
    }

    coverage_step = _named_step(job, "Run coverage shard")
    env = _effective_env(workflow, job, coverage_step)
    assert env.get("CI") == "true"
    assert env.get("AWF_DATABASE_URL") == DB_URL
    assert env.get("AWF_TEST_DATABASE_URL") == DB_URL
    _assert_docker_skip_env_disabled(workflow, job, coverage_step)


@pytest.mark.unit
def test_ci_has_authoritative_python_full_coverage_gate() -> None:
    """The aggregate coverage gate combines shard artifacts and enforces 99%."""
    workflow = _workflow()
    job = _job(workflow, "python-full-coverage")

    assert job.get("runs-on") == GITHUB_HOSTED_RUNNER
    assert job.get("timeout-minutes") == 15
    assert job.get("if") == "${{ always() }}"
    assert _job_needs(job) == {"python-coverage-shards"}
    assert "services" not in job
    assert "strategy" not in job

    commands = _run_steps(job)
    assert "pytest" not in commands
    assert "--cov-fail-under=99" not in commands
    assert "coverage combine" in commands
    assert "coverage xml" in commands
    assert "coverage report --show-missing --fail-under=99" in commands
    assert "shopt -s nullglob" in _step_run(job, "Combine coverage and enforce threshold")
    assert (
        "uv run --python 3.12 python scripts/ci/check_coverage_threshold.py "
        "coverage.xml --minimum-percent 99"
    ) in commands

    verify_run = _step_run(job, "Verify coverage shards passed")
    assert "needs.python-coverage-shards.result" in verify_run
    assert "exit 1" in verify_run

    download_step = _named_step(job, "Download coverage shards")
    _assert_sha_pinned(download_step.get("uses"), "actions/download-artifact")
    assert download_step.get("with") == {
        "pattern": "python-coverage-shard-*",
        "path": "coverage-artifacts",
        "merge-multiple": True,
    }

    step_names = [str(step.get("name")) for step in _steps(job)]
    assert step_names.index("Verify coverage shards passed") < step_names.index(
        "Download coverage shards"
    )
    assert step_names.index("Combine coverage and enforce threshold") > step_names.index(
        "Download coverage shards"
    )
    assert step_names.index("Upload full coverage artifact") > step_names.index(
        "Combine coverage and enforce threshold"
    )


@pytest.mark.unit
def test_setup_uv_steps_avoid_github_token_release_lookup() -> None:
    workflow = _workflow()

    for job_name in (
        "lint-and-type",
        "python-coverage-shards",
        "python-full-coverage",
        "release-artifacts",
    ):
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
    assert "python scripts/ci/check_j2_tojson.py" in commands
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
    assert python_test_jobs == ["python-coverage-shards"]


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
    # publish gates on both build (artifacts) and installer-smoke (verifies the
    # released wheel) so a failed/cancelled smoke blocks the release.
    assert publish_job.get("needs") == ["build", "installer-smoke"]
    assert publish_job.get("permissions", {}).get("id-token") == "write"

    commands = _run_steps(_job(workflow, "build"))
    assert "python -m build" in commands
    assert "sha256sum dist/*" in commands

    publish_steps = _steps(publish_job)
    assert any(
        str(step.get("uses", "")).split("@", 1)[0] == "pypa/gh-action-pypi-publish"
        for step in publish_steps
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
@pytest.mark.parametrize(
    ("action", "ref"),
    [
        ("actions/checkout", "v4"),
        ("astral-sh/setup-uv", "v7"),
        ("actions/upload-artifact", "v7"),
        ("actions/download-artifact", "v4"),
        ("actions/setup-node", "v6"),
        ("docker/setup-buildx-action", "v4"),
        ("docker/build-push-action", "v7"),
    ],
)
def test_ci_workflow_actions_pinned_to_sha_with_version_comment(action: str, ref: str) -> None:
    """Every ``uses:`` ref is pinned to a 40-char SHA with the Dependabot ``# <ref>`` comment."""
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    # No floating-tag refs remain for this action.
    assert not re.search(rf"{re.escape(action)}@{re.escape(ref)}(?=\s|$)", text), action
    # Every ``uses:`` occurrence is pinned to a 40-char SHA with the version comment, so a
    # single mis-pinned duplicate cannot hide behind a correctly pinned sibling.
    uses_lines = re.findall(rf"^\s*(?:-\s*)?uses:\s*{re.escape(action)}@\S+.*", text, re.MULTILINE)
    assert uses_lines, f"No occurrences of {action} found"
    for line in uses_lines:
        assert re.search(
            rf"uses:\s*{re.escape(action)}@[0-9a-f]{{40}}\s+# {re.escape(ref)}\b", line
        ), f"Action {action} in line '{line}' is not pinned to a 40-char SHA with comment '# {ref}'"


@pytest.mark.unit
def test_ci_workflow_all_external_actions_are_sha_pinned() -> None:
    """Every non-local ``uses:`` is SHA-pinned with a ``# <ref>`` comment, exhaustively.

    The parametrized guard above only checks the actions it names, so a newly
    added external ``uses:`` entry could regress to a floating tag and still pass.
    This pass walks *every* non-local ``uses:`` line in ci.yml and enforces
    ``@<40-char SHA>  # <ref>`` so the hardening cannot silently regress. Mirrors
    ``test_publish_workflow_all_external_actions_are_sha_pinned``.
    """
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    # Collect every uses: line that is not a local (``./``) action reference.
    uses_lines = re.findall(r"^\s*(?:-\s*)?uses:\s*(?!\./)\S+.*", text, re.MULTILINE)
    assert uses_lines, "No external uses: entries found in ci.yml"
    for line in uses_lines:
        assert re.search(r"uses:\s*[^@\s]+@[0-9a-f]{40}\s+#\s+\S+\b", line), (
            f"External action in line '{line.strip()}' is not pinned to a "
            "40-char SHA with a '# <ref>' comment"
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "job_name",
    [
        "lint-and-type",
        "python-coverage-shards",
        "python-full-coverage",
        "console",
        "release-artifacts",
    ],
)
def test_ci_workflow_checkouts_disable_persisted_credentials(job_name: str) -> None:
    """Every checkout in ci.yml sets ``persist-credentials: false``.

    Mirrors ``test_publish_workflow_checkouts_disable_persisted_credentials``
    so the two workflows are symmetrically enforced: a future edit that drops
    the flag from any ci.yml checkout fails the suite instead of silently
    re-persisting the GITHUB_TOKEN in ``.git/config``.
    """
    job = _job(_workflow(), job_name)
    checkouts = [
        step
        for step in _steps(job)
        if str(step.get("uses", "")).split("@", 1)[0] == "actions/checkout"
    ]
    assert checkouts, f"{job_name} job has no checkout step"
    for checkout in checkouts:
        with_config = checkout.get("with", {})
        assert isinstance(with_config, dict)
        assert with_config.get("persist-credentials") is False, (
            f"checkout in {job_name} must set persist-credentials: false"
        )


_EXPECTED_CI_JOBS = frozenset(
    {
        "lint-and-type",
        "python-coverage-shards",
        "python-full-coverage",
        "console",
        "release-artifacts",
        "ci-required",
    }
)
_VALID_PERMISSION_VALUES = frozenset({"read", "write", "none"})
# ``(job, scope)`` pairs that may be granted ``write`` (currently none — the entire
# workflow is least-privilege read-only). A future job that genuinely needs a broader
# scope must add the *specific* ``(job, scope)`` pair here deliberately, which is the
# point of the gate. Keyed by ``(job, scope)`` rather than job name alone so a
# whitelisted job is approved only for the exact scope it needs and cannot silently
# acquire write on additional scopes (``packages``, ``id-token``, ``pull-requests``,
# etc.) without a separate, deliberate entry.
_WRITE_PERMISSION_WHITELIST: frozenset[tuple[str, str]] = frozenset()


@pytest.mark.unit
def test_ci_workflow_jobs_have_least_privilege_permissions() -> None:
    """Every ci.yml job is covered by an explicit least-privilege ``permissions:`` block.

    Resolves CodeQL ``actions/missing-workflow-permissions`` (alerts #1–#6): with
    no ``permissions:`` block the default GITHUB_TOKEN scope is broad. The fix is a
    single workflow-level ``contents: read`` default; this guard asserts that
    default exists and that no job silently broadens it. Assertion #1 is the gate
    that covers every job — including any future job — because the workflow-level
    default applies to all jobs that lack their own block. Assertion #2 then encodes
    the coverage invariant explicitly (job-level block *or* workflow default) so the
    check still holds if coverage is ever moved from the default to per-job blocks.
    """
    workflow = _workflow()

    # 1. Workflow-level default is read-only (PyYAML loads ``read`` as a string).
    assert workflow.get("permissions") == {"contents": "read"}

    jobs = workflow.get("jobs", {})
    assert isinstance(jobs, dict)

    # The known six jobs must all still be present so the covered set can't shrink
    # unnoticed, and drive coverage off the *live* job keys so a newly added job is
    # also checked.
    assert _EXPECTED_CI_JOBS.issubset(jobs.keys())

    for name, job in jobs.items():
        assert isinstance(job, dict), name
        job_permissions = job.get("permissions")

        # 2. Every job is covered by an explicit permissions block: either its own
        #    job-level mapping or the workflow-level default. Given assertion #1 pins
        #    that default, the RHS holds today and #1 is the real gate; this encodes
        #    the coverage invariant so it still bites if coverage ever moves to
        #    per-job blocks (default dropped + a job missing its own block).
        assert job_permissions is not None or "permissions" in workflow, (
            f"job {name!r} has no permissions coverage (no job-level block and no "
            "workflow-level default)"
        )

        # 3. No job over-grants. Any job-level block must be a scope->level mapping
        #    and may not grant write on any scope unless explicitly whitelisted.
        if job_permissions is not None:
            assert isinstance(job_permissions, dict), (
                f"job {name!r} permissions must be a dictionary, got "
                f"{type(job_permissions).__name__}"
            )
            for scope, level in job_permissions.items():
                assert level in _VALID_PERMISSION_VALUES, (
                    f"job {name!r} grants invalid permission {scope}={level!r}"
                )
                if level == "write":
                    assert (name, scope) in _WRITE_PERMISSION_WHITELIST, (
                        f"job {name!r} over-grants {scope}: write"
                    )


@pytest.mark.unit
def test_contributor_docs_require_ci_rollup_status_check() -> None:
    docs = CONTRIBUTING_PATH.read_text(encoding="utf-8")

    assert "branch protection" in docs.lower()
    assert "ci-required" in docs
    assert "lint-and-type" in docs
    assert "python-coverage-shards" in docs
    assert "python-full-coverage" in docs
    assert "release-artifacts" in docs
    assert "lint-and-test" not in docs
