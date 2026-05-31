"""Focused branch-coverage tests for executor helper behavior (part 6)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.control.executor import helpers as executor_helpers
from awf.control.executor.helpers import (
    _validation_command_count,
    _validation_run_command_records,
    _validation_tier_for_workspace,
)
from awf.db.enums import (
    OperationStatus,
    OperationType,
    TaskClass,
)
from awf.db.repositories import (
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.profiles.models import WorkspaceProfile
from awf.runtime.planning import (
    PLAN_CONFORMANCE_UNSATISFIED,
)
from awf.runtime.validation import (
    ValidationCommandResult,
    ValidationCoverageResult,
)
from awf.runtime.validation_identity import (
    environment_identity_digest,
    resolved_profile_digest,
)
from tests.postgres import create_postgres_test_engine


def _command_result(tmp_path: Path, *, returncode: int = 1) -> ValidationCommandResult:
    stdout = tmp_path / "cmd.stdout"
    stderr = tmp_path / "cmd.stderr"
    stdout.write_text("stdout", encoding="utf-8")
    stderr.write_text("stderr", encoding="utf-8")
    return ValidationCommandResult(
        command="pytest --cov",
        returncode=returncode,
        duration_seconds=0.1,
        stdout_path=stdout,
        stderr_path=stderr,
        phase="coverage",
        reason_code="COVERAGE_BELOW_THRESHOLD",
        policy_failed=returncode != 0,
    )


def _coverage(
    tmp_path: Path,
    *,
    percent: float | None,
    minimum: float = 99,
    reason_code: str = "COVERAGE_BELOW_THRESHOLD",
    status: str = "failed",
    command_result: ValidationCommandResult | None = None,
) -> ValidationCoverageResult:
    return ValidationCoverageResult(
        provider="python",
        percent=percent,
        minimum_percent=minimum,
        enforce=True,
        status=status,
        reason_code=reason_code,
        command_result=command_result if command_result is not None else _command_result(tmp_path),
    )


class _CoverageValidation:
    def __init__(self, coverage: ValidationCoverageResult | None) -> None:
        self.coverage = coverage
        self.calls: list[str] = []
        self.kwargs: list[dict[str, object]] = []

    async def run_profile_coverage(
        self, *, phase: str, **_kwargs: object
    ) -> ValidationCoverageResult | None:
        self.calls.append(phase)
        self.kwargs.append(dict(_kwargs))
        return self.coverage


def _executor_with_runner(
    runner: FakeCommandRunner,
    tmp_path: Path,
    *,
    validation: object | None = None,
) -> WorkspaceExecutor:
    executor = WorkspaceExecutor(
        session_factory=object(),  # type: ignore[arg-type]
        runner=runner,
        compose=object(),  # type: ignore[arg-type]
        validation=validation or object(),  # type: ignore[arg-type]
        pr_creator=object(),  # type: ignore[arg-type]
        config=ExecutorConfig(
            worktrees_root=tmp_path / "worktrees",
            compose_projects_root=tmp_path / "compose",
        ),
    )
    executor._update_subphase = AsyncMock()  # type: ignore[method-assign]
    return executor


@pytest.mark.unit
def test_validation_command_count_includes_database_refresh_hooks_and_coverage() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "count-db-refresh",
            "phases": {
                "post_agent": ["ruff format --check"],
                "validate": ["pytest -q"],
            },
            "database": {"pre_validation_refresh": ["python scripts/db_refresh.py"]},
            "validation": {
                "strategy": {"final_gate": "coverage"},
                "coverage": {"command": "pytest --cov=awf"},
            },
        }
    )

    workspace = SimpleNamespace(
        resolved_profile=profile.model_dump(mode="json", by_alias=True),
        test_commands=[],
    )

    assert _validation_command_count(workspace) == 4


@pytest.mark.unit
def test_validation_command_count_ignores_coverage_without_local_final_gate() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "count-targeted-with-coverage-command",
            "phases": {"validate": ["pytest -q"]},
            "validation": {
                "strategy": {"edit_gate": "targeted", "final_gate": "none"},
                "coverage": {"command": "pytest --cov=awf"},
            },
        }
    )

    workspace = SimpleNamespace(
        resolved_profile=profile.model_dump(mode="json", by_alias=True),
        test_commands=[],
    )

    assert _validation_command_count(workspace) == 1


@pytest.mark.unit
def test_validation_run_command_records_include_http_healthcheck_display() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "records-http-healthcheck",
            "phases": {"validate": ["pytest -q"]},
            "validation": {
                "healthchecks": [
                    {
                        "name": "api",
                        "url": "http://api:8080/healthz",
                        "expected_status": 204,
                    }
                ]
            },
        }
    )

    records = _validation_run_command_records(
        profile=profile,
        phase_names=("validate",),
        run_healthchecks=True,
    )

    assert records[0] == {
        "phase": "healthcheck",
        "command_index": 1,
        "command": "GET http://api:8080/healthz expected 204",
        "healthcheck_name": "api",
        "healthcheck_kind": "http",
        "target": "http://api:8080/healthz",
        "stream_ids": {
            "stdout": "validation.01_healthcheck.stdout",
            "stderr": "validation.01_healthcheck.stderr",
        },
    }


@pytest.mark.unit
def test_validation_run_command_records_include_alembic_policy_before_healthchecks() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "records-alembic-policy",
            "phases": {"validate": ["pytest -q"]},
            "validation": {
                "alembic": {"enabled": True},
                "healthchecks": [{"name": "api", "command": "curl -fsS localhost/health"}],
            },
        }
    )

    records = _validation_run_command_records(
        profile=profile,
        phase_names=("validate",),
        run_healthchecks=True,
    )

    assert [(record["phase"], record["command_index"]) for record in records] == [
        ("migration_policy", 1),
        ("healthcheck", 1),
        ("validate", 1),
    ]
    assert records[0]["command"] == "awf validate alembic migration chain"
    assert records[0]["stream_ids"] == {
        "stdout": "validation.01_migration_policy.stdout",
        "stderr": "validation.01_migration_policy.stderr",
    }


def test_validation_run_command_records_skips_alembic_policy_if_validation_alembic_is_none() -> (
    None
):
    profile = WorkspaceProfile.model_validate(
        {
            "name": "records-alembic-none",
            "phases": {"validate": ["pytest -q"]},
            "validation": {
                "healthchecks": [{"name": "api", "command": "curl -fsS localhost/health"}],
            },
        }
    )
    profile.validation.alembic = None  # type: ignore[assignment]

    records = _validation_run_command_records(
        profile=profile,
        phase_names=("validate",),
        run_healthchecks=False,
    )

    assert [(record["phase"], record["command_index"]) for record in records] == [
        ("validate", 1),
    ]


@pytest.mark.unit
def test_validation_run_command_records_can_skip_healthchecks_and_coverage() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "records-no-healthchecks",
            "phases": {
                "setup": ["uv sync"],
                "validate": ["pytest -q"],
            },
            "validation": {
                "healthchecks": [{"name": "api", "command": "curl -fsS localhost/health"}],
                "coverage": {"command": "pytest --cov=awf --cov-report=term"},
            },
        }
    )

    records = _validation_run_command_records(
        profile=profile,
        phase_names=("setup",),
        run_healthchecks=False,
    )

    assert [(record["phase"], record["command"]) for record in records] == [("setup", "uv sync")]


@pytest.mark.unit
def test_validation_tier_for_workspace_uses_task_class_floor() -> None:
    profile = WorkspaceProfile.model_validate({"name": "tier", "validation": {"requested_tier": 1}})

    assert (
        _validation_tier_for_workspace(
            SimpleNamespace(task_class=TaskClass.migration_task.value),  # type: ignore[arg-type]
            profile,
        )
        == 3
    )
    assert (
        _validation_tier_for_workspace(
            SimpleNamespace(task_class=TaskClass.refactor_task.value),  # type: ignore[arg-type]
            profile,
        )
        == 2
    )
    assert (
        _validation_tier_for_workspace(
            SimpleNamespace(task_class=None),  # type: ignore[arg-type]
            profile,
        )
        == 1
    )


@pytest.mark.unit
def test_validation_tier_for_workspace_uses_successful_validate_operation_tier() -> None:
    profile = WorkspaceProfile.model_validate({"name": "tier", "validation": {"requested_tier": 1}})
    workspace = SimpleNamespace(
        task_class=None,
        operations=[
            SimpleNamespace(
                type=OperationType.validate.value,
                status=OperationStatus.failed.value,
                payload={"requested_tier": 3},
                result={"requested_tier": 3},
            ),
            SimpleNamespace(
                type=OperationType.refresh.value,
                status=OperationStatus.succeeded.value,
                payload={"requested_tier": 3},
                result={"requested_tier": 3},
            ),
            SimpleNamespace(
                type=OperationType.validate.value,
                status=OperationStatus.succeeded.value,
                payload={"requested_tier": "3"},
                result={"requested_tier": "3"},
            ),
            SimpleNamespace(
                type=OperationType.validate.value,
                status=OperationStatus.succeeded.value,
                payload={"requested_tier": 2},
                result={"validation": {"requested_tier": 3}},
            ),
        ],
    )

    assert _validation_tier_for_workspace(workspace, profile) == 3  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.parametrize(
    "active_status",
    [OperationStatus.pending.value, OperationStatus.running.value],
)
def test_validation_tier_for_workspace_uses_active_validate_operation_payload_tier(
    active_status: str,
) -> None:
    profile = WorkspaceProfile.model_validate({"name": "tier", "validation": {"requested_tier": 1}})
    workspace = SimpleNamespace(
        task_class=None,
        operations=[
            SimpleNamespace(
                type=OperationType.validate.value,
                status=OperationStatus.failed.value,
                payload={"requested_tier": 3},
                result={"requested_tier": 3},
            ),
            SimpleNamespace(
                type=OperationType.validate.value,
                status=OperationStatus.cancelled.value,
                payload={"requested_tier": 3},
                result={"requested_tier": 3},
            ),
            SimpleNamespace(
                type=OperationType.refresh.value,
                status=active_status,
                payload={"requested_tier": 3},
            ),
            SimpleNamespace(
                type=OperationType.validate.value,
                status=active_status,
                payload={"requested_tier": 3},
            ),
        ],
    )

    assert _validation_tier_for_workspace(workspace, profile) == 3  # type: ignore[arg-type]


@pytest.mark.unit
async def test_baseline_coverage_preflight_returns_logged_policy_result(
    tmp_path: Path,
) -> None:
    baseline = _coverage(tmp_path, percent=88, minimum=99)
    validation = _CoverageValidation(baseline)
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path, validation=validation)
    profile = WorkspaceProfile.model_validate(
        {
            "name": "coverage-preflight",
            "validation": {
                "coverage": {
                    "minimum_percent": 99,
                    "enforce": True,
                    "command": "pytest --cov=awf",
                }
            },
        }
    )

    result = await executor._run_baseline_coverage_preflight(
        workspace_id="ws_preflight",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        profile=profile,
    )

    assert result is baseline
    assert validation.calls == ["baseline_coverage"]


@pytest.mark.unit
async def test_baseline_coverage_preflight_returns_passing_policy_result(
    tmp_path: Path,
) -> None:
    baseline = _coverage(tmp_path, percent=100, minimum=99, status="passed")

    validation = _CoverageValidation(baseline)
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path, validation=validation)
    profile = WorkspaceProfile.model_validate(
        {
            "name": "coverage-preflight-passing",
            "validation": {
                "coverage": {
                    "minimum_percent": 99,
                    "enforce": True,
                    "command": "pytest --cov=awf",
                }
            },
        }
    )

    result = await executor._run_baseline_coverage_preflight(
        workspace_id="ws_preflight_ok",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        profile=profile,
    )

    assert result is baseline
    assert validation.calls == ["baseline_coverage"]


@pytest.mark.unit
async def test_baseline_coverage_preflight_returns_successful_result(tmp_path: Path) -> None:
    baseline = _coverage(
        tmp_path,
        percent=99,
        minimum=99,
        status="passed",
        reason_code="COVERAGE_OK",
    )
    validation = _CoverageValidation(baseline)
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path, validation=validation)
    profile = WorkspaceProfile.model_validate(
        {
            "name": "coverage-preflight-success",
            "validation": {
                "coverage": {
                    "minimum_percent": 99,
                    "enforce": True,
                    "command": "pytest --cov=awf",
                }
            },
        }
    )

    result = await executor._run_baseline_coverage_preflight(
        workspace_id="ws_preflight_success",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        profile=profile,
    )

    assert result is baseline
    assert validation.calls == ["baseline_coverage"]


@pytest.mark.unit
async def test_baseline_coverage_preflight_skips_when_strategy_disables_it(
    tmp_path: Path,
) -> None:
    baseline = _coverage(tmp_path, percent=99, minimum=99, status="passed")
    validation = _CoverageValidation(baseline)
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path, validation=validation)
    profile = WorkspaceProfile.model_validate(
        {
            "name": "coverage-preflight-skip",
            "validation": {
                "strategy": {"baseline_coverage": "skip"},
                "coverage": {
                    "minimum_percent": 99,
                    "enforce": True,
                    "command": "pytest --cov=awf",
                },
            },
        }
    )

    result = await executor._run_baseline_coverage_preflight(
        workspace_id="ws_preflight_skip",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        profile=profile,
    )

    assert result is None
    assert validation.calls == []


@pytest.mark.unit
async def test_final_coverage_gate_skips_when_coverage_command_is_absent(
    tmp_path: Path,
) -> None:
    validation = _CoverageValidation(_coverage(tmp_path, percent=100, status="passed"))
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path, validation=validation)
    profile = WorkspaceProfile.model_validate(
        {
            "name": "final-gate-no-command",
            "validation": {"strategy": {"final_gate": "coverage"}},
        }
    )

    result = await executor._run_final_coverage_gate(
        workspace_id="ws_no_coverage_command",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        profile=profile,
        validation_tier=1,
        workspace_head_sha="head",
    )

    assert result.coverage is None
    assert validation.calls == []


@pytest.mark.unit
async def test_final_coverage_gate_reuses_exact_fresh_evidence(
    tmp_path: Path,
) -> None:
    engine = await create_postgres_test_engine()
    factory = make_session_factory(engine)
    profile = WorkspaceProfile.model_validate(
        {
            "name": "final-gate",
            "validation": {
                "strategy": {
                    "final_gate": "coverage",
                    "reuse_evidence": True,
                    "freshness_max_age_seconds": 3600,
                },
                "coverage": {
                    "minimum_percent": 99,
                    "command": "pytest --cov=awf",
                },
            },
        }
    )
    commands = _validation_run_command_records(
        profile=profile,
        phase_names=("post_agent", "validate"),
        run_healthchecks=True,
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/awf.git",
            branch_base="main",
            task_title="reuse final coverage",
            task_prompt="reuse final coverage",
            agent="codex",
            test_commands=[],
        )
        run = await ValidationRunRepository(session).start(
            workspace_id=workspace.id,
            attempt_id=None,
            tier=1,
            commands=commands,
            base_commit="base",
            target_branch="main",
            target_head_sha=None,
            workspace_head_sha="head",
            resolved_profile_digest=resolved_profile_digest(profile),
            environment_identity_digest=environment_identity_digest(profile),
            log_stream_refs={},
        )
        await ValidationRunRepository(session).finish(
            run.id,
            status="succeeded",
            reason_code="VALIDATION_OK",
            coverage={"status": "passed", "reason_code": "COVERAGE_OK", "percent": 99.5},
        )
        await session.commit()
        workspace_id = workspace.id
        source_run_id = run.id

    validation = _CoverageValidation(_coverage(tmp_path, percent=100, status="passed"))
    executor = WorkspaceExecutor(
        session_factory=factory,
        runner=FakeCommandRunner(),
        compose=object(),  # type: ignore[arg-type]
        validation=validation,  # type: ignore[arg-type]
        pr_creator=object(),  # type: ignore[arg-type]
        config=ExecutorConfig(
            worktrees_root=tmp_path / "worktrees",
            compose_projects_root=tmp_path / "compose",
        ),
    )

    result = await executor._run_final_coverage_gate(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        profile=profile,
        validation_tier=1,
        workspace_head_sha="head",
    )

    assert result.coverage is not None
    assert result.coverage.percent == 99.5
    assert result.evidence_status == "reused"
    assert result.source_run_id == source_run_id
    assert validation.calls == []
    await engine.dispose()


@pytest.mark.unit
async def test_final_coverage_gate_caps_parallel_workers_to_active_reservation(
    tmp_path: Path,
) -> None:
    engine = await create_postgres_test_engine()
    try:
        factory = make_session_factory(engine)
        profile = WorkspaceProfile.model_validate(
            {
                "name": "final-gate-parallel",
                "validation": {
                    "strategy": {"final_gate": "coverage"},
                    "coverage": {
                        "minimum_percent": 99,
                        "command": "pytest --cov=awf",
                        "parallel_workers": 20,
                    },
                },
            }
        )
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/awf.git",
                branch_base="main",
                task_title="parallel final coverage",
                task_prompt="parallel final coverage",
                agent="codex",
                test_commands=[],
            )
            task = await TaskRepository(session).create_or_get(
                repo_url=workspace.repo_url,
                base_branch=workspace.branch_base,
                title=workspace.task_title,
                prompt=workspace.task_prompt,
                external_id=None,
                idempotency_key=None,
                task_class=None,
                owned_paths=[],
            )
            attempt = await TaskAttemptRepository(session).create_for_workspace(
                task=task,
                workspace=workspace,
            )
            await ResourceReservationRepository(session).create(
                workspace_id=workspace.id,
                attempt_id=attempt.id,
                node_id="local",
                steady_cpu=3.0,
                steady_memory_gb=10.0,
                peak_cpu=6.0,
                peak_memory_gb=16.0,
                disk_mb=None,
                phase="execution",
            )
            await session.commit()
            workspace_id = workspace.id

        coverage = _coverage(tmp_path, percent=100, status="passed", reason_code="COVERAGE_OK")
        validation = _CoverageValidation(coverage)
        executor = WorkspaceExecutor(
            session_factory=factory,
            runner=FakeCommandRunner(),
            compose=object(),  # type: ignore[arg-type]
            validation=validation,  # type: ignore[arg-type]
            pr_creator=object(),  # type: ignore[arg-type]
            config=ExecutorConfig(
                worktrees_root=tmp_path / "worktrees",
                compose_projects_root=tmp_path / "compose",
            ),
        )

        result = await executor._run_final_coverage_gate(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            profile=profile,
            validation_tier=1,
            workspace_head_sha="head",
        )

        assert result.coverage is coverage
        assert validation.calls == ["coverage"]
        assert validation.kwargs[0]["parallel_worker_cpu_limit"] == 3
    finally:
        await engine.dispose()


@pytest.mark.unit
async def test_validation_run_evidence_for_conformance_reports_missing_run(
    tmp_path: Path,
) -> None:
    engine = await create_postgres_test_engine()
    try:
        executor = WorkspaceExecutor(
            session_factory=make_session_factory(engine),
            runner=FakeCommandRunner(),
            compose=object(),  # type: ignore[arg-type]
            validation=object(),  # type: ignore[arg-type]
            pr_creator=object(),  # type: ignore[arg-type]
            config=ExecutorConfig(
                worktrees_root=tmp_path / "worktrees",
                compose_projects_root=tmp_path / "compose",
            ),
        )

        evidence = await executor._validation_run_evidence_for_conformance("missing-run")

        assert "AWF persisted validation run evidence" in evidence
        assert '"status": "missing"' in evidence
        assert '"reason_code": "VALIDATION_RUN_NOT_FOUND"' in evidence
    finally:
        await engine.dispose()


@pytest.mark.unit
async def test_auto_retry_planning_scope_failure_ignores_other_reason_codes(
    tmp_path: Path,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)

    await executor._auto_retry_planning_scope_failure(
        workspace_id="ws_plan",
        failure=executor_helpers._PlanningRunFailure(
            message="ordinary conformance failure",
            reason_code=PLAN_CONFORMANCE_UNSATISFIED,
        ),
    )


@pytest.mark.unit
async def test_git_commit_count_since_handles_failed_and_invalid_output(
    tmp_path: Path,
) -> None:
    failed_runner = FakeCommandRunner()
    failed_runner.queue_result(returncode=1, stderr="bad revision")
    failed_executor = _executor_with_runner(failed_runner, tmp_path)
    assert await failed_executor._git_commit_count_since(tmp_path / "worktree", "base") == 0

    invalid_runner = FakeCommandRunner()
    invalid_runner.queue_result(returncode=0, stdout="not-an-int\n")
    invalid_executor = _executor_with_runner(invalid_runner, tmp_path)
    assert await invalid_executor._git_commit_count_since(tmp_path / "worktree", "base") == 0
