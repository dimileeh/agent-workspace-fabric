"""Focused branch-coverage tests for the validation fix-cycle."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from awf.common.commands import CommandResult
from awf.common.compose_exec import ComposeExecCleanupError
from awf.control.executor import execution_validation as executor_execution_validation
from awf.control.executor.agent_service_recovery import AGENT_SERVICE_RECOVERY_ABORTED
from awf.control.executor.types import (
    _PlanningRunFailure,
    _PlanningValidationHandoff,
)
from awf.db.enums import FailureReason, OperationStatus, WorkspaceStatus
from awf.profiles.models import WorkspaceProfile
from awf.runtime.planning import (
    PlanConformanceReport,
    PlanConformanceStatus,
)
from awf.runtime.validation import (
    ValidationCommandResult,
    ValidationResult,
)
from awf.runtime.validation_worktree import ValidationWorktreeCheck, ValidationWorktreeCleanup


def _passing_command(tmp_path: Path) -> ValidationCommandResult:
    stdout = tmp_path / "ok.stdout"
    stderr = tmp_path / "ok.stderr"
    stdout.write_text("ok", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    return ValidationCommandResult(
        command="pytest -q",
        returncode=0,
        duration_seconds=0.1,
        stdout_path=stdout,
        stderr_path=stderr,
        phase="validate",
        reason_code=None,
        policy_failed=False,
    )


def _failing_command(tmp_path: Path) -> ValidationCommandResult:
    stdout = tmp_path / "fail.stdout"
    stderr = tmp_path / "fail.stderr"
    stdout.write_text("boom", encoding="utf-8")
    stderr.write_text("boom", encoding="utf-8")
    return ValidationCommandResult(
        command="pytest -q",
        returncode=1,
        duration_seconds=0.1,
        stdout_path=stdout,
        stderr_path=stderr,
        phase="validate",
        reason_code="PYTEST_TEST_FAILURE",
        policy_failed=True,
    )


def _workspace(workspace_id: str, *, pr_url: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        resolved_profile={"name": f"prof-{workspace_id}"},
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        task_class=None,
        operations=[],
        test_commands=[],
        task_title="A task",
        agent="codex",
        owned_paths=(),
        id=workspace_id,
        pr_url=pr_url,
        task_tag=None,
    )


def _handoff(
    tmp_path: Path, *, iteration: int = 1, max_iterations: int = 3
) -> _PlanningValidationHandoff:
    report = PlanConformanceReport(
        status=PlanConformanceStatus.satisfied,
        summary="ok",
        gaps=(),
    )
    return _PlanningValidationHandoff(
        report=report,
        plan_path=tmp_path / "plan.md",
        report_path=tmp_path / "report.md",
        iteration=iteration,
        max_iterations=max_iterations,
    )


def _patch_profile(monkeypatch: pytest.MonkeyPatch, profile: WorkspaceProfile) -> None:
    async def _sync_profile(*_args: object, **_kwargs: object) -> WorkspaceProfile:
        return profile

    monkeypatch.setattr(
        executor_execution_validation,
        "_profile_for_workspace",
        lambda *_args, **_kwargs: profile,
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "_sync_resolved_profile",
        _sync_profile,
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "profile_phase_command_plan",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "_validation_tier_for_workspace",
        lambda *_args, **_kwargs: 1,
    )


def _patch_clean_worktree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        executor_execution_validation,
        "check_validation_worktree_clean",
        AsyncMock(return_value=ValidationWorktreeCheck(clean=True)),
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "cleanup_validation_worktree_side_effects",
        AsyncMock(
            return_value=ValidationWorktreeCleanup(
                cleaned=True,
                check=ValidationWorktreeCheck(clean=True),
                restore_ref="c" * 40,
            )
        ),
    )


async def _run_cycle(
    executor: SimpleNamespace,
    *,
    workspace: SimpleNamespace,
    tmp_path: Path,
    adapter: Any,
    planning_validation_handoff: _PlanningValidationHandoff | None = None,
    recovery: dict[str, Any] | None = None,
    git_in_worktree: Any | None = None,
    resume_disable_fix_passes: bool = False,
) -> Any:
    if not hasattr(executor, "_capture_post_validation_conformance_scope_baseline"):
        executor._capture_post_validation_conformance_scope_baseline = AsyncMock(return_value=None)
    return await executor_execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id=workspace.id,
        ws=workspace,  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
        compose_project=f"awf_{workspace.id}",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch=f"awf/{workspace.id}",
        adapter=adapter,  # type: ignore[arg-type]
        default_model=None,
        baseline_coverage=None,
        planning_validation_handoff=planning_validation_handoff,
        recovery=recovery,
        rebase_recovery_result=None,
        git_in_worktree=git_in_worktree
        or AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
        resume_disable_fix_passes=resume_disable_fix_passes,
    )


@pytest.mark.unit
async def test_hosted_validation_missing_runner_fails_with_structured_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Hosted PR adoption must fail explicitly when no hosted validator is wired."""
    profile = WorkspaceProfile.model_validate({"name": "prof-hosted-missing-validator"})
    workspace = _workspace("ws_hosted_missing_validator", pr_url="https://github.com/x/y/pull/7")
    workspace.task_policy = {
        "pr_adoption": {
            "execution": {"mode": "hosted"},
            "base_ref": "main",
            "head_ref": "awf/hosted-missing-validator",
            "head_sha": "d" * 40,
            "pr_number": 7,
            "pr_url": "https://github.com/x/y/pull/7",
        }
    }
    workspace.repo_url = "git@github.com:x/y.git"
    workspace.remote_push_branch = "awf/hosted-missing-validator"
    workspace.pr_number = 7
    workspace.branch_base = "main"
    workspace.monitor_last_commit_sha = None
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    class _UnexpectedLocalValidation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            raise AssertionError("hosted validation must not fall back to local validation")

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-hosted-missing-validator"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _update_subphase=AsyncMock(),
        _validation=_UnexpectedLocalValidation(),
    )

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=SimpleNamespace(run=AsyncMock()),
    )

    assert result.stop
    executor._update_subphase.assert_not_awaited()
    executor._finish_validation_run.assert_awaited_once_with(
        "vr-hosted-missing-validator",
        status="failed",
        reason_code="VALIDATION_INFRASTRUCTURE_ERROR",
    )
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_kwargs["status"] == OperationStatus.failed
    assert finish_kwargs["reason_code"] == "VALIDATION_INFRASTRUCTURE_ERROR"
    assert (
        finish_kwargs["error_message"]
        == "hosted PR adoption validation failed: no hosted validation runner configured"
    )
    mark_kwargs = executor._mark_failed.await_args.kwargs
    assert mark_kwargs["failure_reason"] == FailureReason.infrastructure_failure
    assert mark_kwargs["reason_code"] == "VALIDATION_INFRASTRUCTURE_ERROR"
    assert mark_kwargs["message"] == finish_kwargs["error_message"]


@pytest.mark.unit
async def test_hosted_validation_run_startup_materialization_failure_fails_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Hosted command-plan materialization failures must fail the workspace and operation."""
    profile = WorkspaceProfile.model_validate({"name": "prof-hosted-materialization-fail"})
    workspace = _workspace("ws_hosted_materialization_fail", pr_url="https://github.com/x/y/pull/7")
    workspace.task_policy = {
        "pr_adoption": {
            "execution": {"mode": "hosted"},
            "base_ref": "main",
            "head_ref": "awf/hosted-materialization-fail",
            "head_sha": "d" * 40,
            "pr_number": 7,
            "pr_url": "https://github.com/x/y/pull/7",
        }
    }
    workspace.repo_url = "git@github.com:x/y.git"
    workspace.remote_push_branch = "awf/hosted-materialization-fail"
    workspace.pr_number = 7
    workspace.branch_base = "main"
    workspace.monitor_last_commit_sha = None
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    materialization_error = ValueError(
        "hosted profile payload contains secret-bearing fields: database.generated_setup[0].command"
    )
    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(side_effect=materialization_error),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _update_subphase=AsyncMock(),
        _validation=SimpleNamespace(run_profile_phases=AsyncMock()),
    )

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=SimpleNamespace(run=AsyncMock()),
    )

    assert result.stop
    assert result.successful_validation_run_id is None
    executor._update_subphase.assert_not_awaited()
    executor._validation.run_profile_phases.assert_not_awaited()
    executor._finish_validation_run.assert_not_awaited()
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_kwargs["status"] == OperationStatus.failed
    assert finish_kwargs["validation_run_id"] is None
    assert finish_kwargs["reason_code"] == "VALIDATION_INFRASTRUCTURE_ERROR"
    assert "validation run startup failed" in finish_kwargs["error_message"]
    mark_kwargs = executor._mark_failed.await_args.kwargs
    assert mark_kwargs["failure_reason"] == FailureReason.infrastructure_failure
    assert mark_kwargs["reason_code"] == "VALIDATION_INFRASTRUCTURE_ERROR"


class _ClassifiedValidationStartupError(ValueError):
    reason_code = "HOSTED_PROFILE_MATERIALIZATION_FAILED"


@pytest.mark.unit
async def test_hosted_validation_run_startup_failure_redacts_secrets_in_persisted_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Startup diagnostics with secret-shaped values must be redacted before persistence."""
    profile = WorkspaceProfile.model_validate({"name": "prof-hosted-startup-redact"})
    workspace = _workspace("ws_hosted_startup_redact", pr_url="https://github.com/x/y/pull/7")
    workspace.task_policy = {
        "pr_adoption": {
            "execution": {"mode": "hosted"},
            "base_ref": "main",
            "head_ref": "awf/hosted-startup-redact",
            "head_sha": "d" * 40,
            "pr_number": 7,
            "pr_url": "https://github.com/x/y/pull/7",
        }
    }
    workspace.repo_url = "git@github.com:x/y.git"
    workspace.remote_push_branch = "awf/hosted-startup-redact"
    workspace.pr_number = 7
    workspace.branch_base = "main"
    workspace.monitor_last_commit_sha = None
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    secret_token = "ghp_FAKESECRET0000000"
    materialization_error = ValueError(
        f"hosted profile payload contains secret-bearing fields: {secret_token}"
    )
    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(side_effect=materialization_error),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _update_subphase=AsyncMock(),
        _validation=SimpleNamespace(run_profile_phases=AsyncMock()),
    )

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=SimpleNamespace(run=AsyncMock()),
    )

    assert result.stop
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert secret_token not in finish_kwargs["error_message"]
    mark_kwargs = executor._mark_failed.await_args.kwargs
    assert secret_token not in mark_kwargs["message"]


@pytest.mark.unit
async def test_hosted_validation_run_startup_failure_preserves_classified_reason_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Classified startup failures must preserve their reason code end-to-end."""
    profile = WorkspaceProfile.model_validate({"name": "prof-hosted-startup-reason"})
    workspace = _workspace("ws_hosted_startup_reason", pr_url="https://github.com/x/y/pull/7")
    workspace.task_policy = {
        "pr_adoption": {
            "execution": {"mode": "hosted"},
            "base_ref": "main",
            "head_ref": "awf/hosted-startup-reason",
            "head_sha": "d" * 40,
            "pr_number": 7,
            "pr_url": "https://github.com/x/y/pull/7",
        }
    }
    workspace.repo_url = "git@github.com:x/y.git"
    workspace.remote_push_branch = "awf/hosted-startup-reason"
    workspace.pr_number = 7
    workspace.branch_base = "main"
    workspace.monitor_last_commit_sha = None
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(
            side_effect=_ClassifiedValidationStartupError("classified startup failure")
        ),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _update_subphase=AsyncMock(),
        _validation=SimpleNamespace(run_profile_phases=AsyncMock()),
    )

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=SimpleNamespace(run=AsyncMock()),
    )

    assert result.stop
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_kwargs["reason_code"] == "HOSTED_PROFILE_MATERIALIZATION_FAILED"
    mark_kwargs = executor._mark_failed.await_args.kwargs
    assert mark_kwargs["reason_code"] == "HOSTED_PROFILE_MATERIALIZATION_FAILED"


@pytest.mark.unit
async def test_hosted_validation_run_startup_unexpected_failure_propagates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Unexpected startup failures must not be swallowed by the classified path."""
    profile = WorkspaceProfile.model_validate({"name": "prof-hosted-startup-propagate"})
    workspace = _workspace("ws_hosted_startup_propagate", pr_url="https://github.com/x/y/pull/7")
    workspace.task_policy = {
        "pr_adoption": {
            "execution": {"mode": "hosted"},
            "base_ref": "main",
            "head_ref": "awf/hosted-startup-propagate",
            "head_sha": "d" * 40,
            "pr_number": 7,
            "pr_url": "https://github.com/x/y/pull/7",
        }
    }
    workspace.repo_url = "git@github.com:x/y.git"
    workspace.remote_push_branch = "awf/hosted-startup-propagate"
    workspace.pr_number = 7
    workspace.branch_base = "main"
    workspace.monitor_last_commit_sha = None
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(side_effect=OSError("disk full")),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _update_subphase=AsyncMock(),
        _validation=SimpleNamespace(run_profile_phases=AsyncMock()),
    )

    with pytest.raises(OSError, match="disk full"):
        await _run_cycle(
            executor,
            workspace=workspace,
            tmp_path=tmp_path,
            adapter=SimpleNamespace(run=AsyncMock()),
        )


@pytest.mark.unit
async def test_recovery_conformance_success_recaptures_post_conformance_head_sha(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Recovery + open PR + handoff re-captures HEAD after a passing conformance check."""
    profile = WorkspaceProfile.model_validate({"name": "prof-recovery-conf"})
    workspace = _workspace("ws_recovery_conf", pr_url="https://github.com/x/y/pull/7")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_passing_command(tmp_path)])

    pre_head = "a" * 40
    post_conformance_head = "f" * 40
    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        # First capture is the pre-validation HEAD; second is the
        # post-conformance HEAD that recovery re-captures.
        _capture_workspace_head_sha=AsyncMock(side_effect=[pre_head, post_conformance_head]),
        _start_validation_run=AsyncMock(return_value="vr-recovery-conf"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        # Conformance passes (returns None) under recovery.
        _run_post_validation_conformance_check=AsyncMock(return_value=None),
    )
    adapter = SimpleNamespace(run=AsyncMock())

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
        planning_validation_handoff=_handoff(tmp_path),
        recovery={"recovery_mode": "validate_only", "source": "pr_monitor"},
    )

    assert not result.stop
    assert result.successful_validation_run_id == "vr-recovery-conf"
    # The re-captured post-conformance HEAD wins over the pre-validation HEAD.
    assert result.successful_validation_workspace_head_sha == post_conformance_head
    assert executor._capture_workspace_head_sha.await_count == 2
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_kwargs["status"] == OperationStatus.succeeded
    assert finish_kwargs["reason_code"] == "VALIDATION_OK"


@pytest.mark.unit
async def test_recovery_conformance_success_keeps_prevalidation_head_when_recapture_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Recovery conformance success keeps the pre-validation HEAD when re-capture is empty.

    Exercises the false branch of the post-conformance HEAD re-capture: when the
    second ``_capture_workspace_head_sha`` returns a falsy value, the successful
    head SHA must remain the pre-validation HEAD rather than being overwritten.
    """
    profile = WorkspaceProfile.model_validate({"name": "prof-recovery-conf-empty"})
    workspace = _workspace("ws_recovery_conf_empty", pr_url="https://github.com/x/y/pull/8")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_passing_command(tmp_path)])

    pre_head = "a" * 40
    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        # Second capture (post-conformance) returns None → keep pre-validation HEAD.
        _capture_workspace_head_sha=AsyncMock(side_effect=[pre_head, None]),
        _start_validation_run=AsyncMock(return_value="vr-recovery-conf-empty"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _run_post_validation_conformance_check=AsyncMock(return_value=None),
    )
    adapter = SimpleNamespace(run=AsyncMock())

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
        planning_validation_handoff=_handoff(tmp_path),
        recovery={"recovery_mode": "validate_only", "source": "pr_monitor"},
    )

    assert not result.stop
    assert result.successful_validation_run_id == "vr-recovery-conf-empty"
    # The empty re-capture left the pre-validation HEAD intact.
    assert result.successful_validation_workspace_head_sha == pre_head
    assert executor._capture_workspace_head_sha.await_count == 2


@pytest.mark.unit
async def test_conformance_recovery_abort_marks_validating_workspace_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression PRRT_kwDOSJAM6s6MwuSg: conformance recovery abort is terminal."""
    profile = WorkspaceProfile.model_validate({"name": "prof-conf-recovery-abort"})
    workspace = _workspace("ws_conf_recovery_abort")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_passing_command(tmp_path)])

    async def _abort_recovery(*_args: object, **kwargs: object) -> tuple[bool, object | None]:
        assert kwargs["before_mark_failed_marks_workspace"] is True
        before_mark_failed = kwargs["before_mark_failed"]
        await before_mark_failed(reason_code="GIT_AGENT_WRITABILITY_FAILED")
        return False, None

    monkeypatch.setattr(
        executor_execution_validation,
        "_run_agent_callable_with_service_recovery",
        _abort_recovery,
    )

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-conf-recovery-abort"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _run_post_validation_conformance_check=AsyncMock(),
    )

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=SimpleNamespace(run=AsyncMock()),
        planning_validation_handoff=_handoff(tmp_path),
    )

    assert result.stop
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_kwargs["status"] == OperationStatus.failed
    assert finish_kwargs["reason_code"] == "GIT_AGENT_WRITABILITY_FAILED"
    mark_kwargs = executor._mark_failed.await_args.kwargs
    assert mark_kwargs["from_status"] is WorkspaceStatus.validating
    assert mark_kwargs["failure_reason"] is FailureReason.infrastructure_failure
    assert mark_kwargs["reason_code"] == "GIT_AGENT_WRITABILITY_FAILED"


@pytest.mark.unit
async def test_conformance_recovery_abort_without_reason_is_not_marked_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile = WorkspaceProfile.model_validate({"name": "prof-conf-recovery-abort-no-reason"})
    workspace = _workspace("ws_conf_recovery_abort_no_reason")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_passing_command(tmp_path)])

    async def _abort_recovery(*_args: object, **kwargs: object) -> tuple[bool, object | None]:
        assert kwargs["before_mark_failed_marks_workspace"] is True
        before_mark_failed = kwargs["before_mark_failed"]
        await before_mark_failed()
        return False, None

    monkeypatch.setattr(
        executor_execution_validation,
        "_run_agent_callable_with_service_recovery",
        _abort_recovery,
    )

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-conf-recovery-abort-no-reason"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _run_post_validation_conformance_check=AsyncMock(),
    )

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=SimpleNamespace(run=AsyncMock()),
        planning_validation_handoff=_handoff(tmp_path),
    )

    assert result.stop
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_kwargs["reason_code"] == AGENT_SERVICE_RECOVERY_ABORTED
    mark_kwargs = executor._mark_failed.await_args.kwargs
    assert mark_kwargs["reason_code"] == AGENT_SERVICE_RECOVERY_ABORTED


@pytest.mark.unit
async def test_post_validation_conformance_cleanup_retry_reuses_original_scope_baseline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile = WorkspaceProfile.model_validate({"name": "prof-conf-retry-scope"})
    workspace = _workspace("ws_conf_retry_scope")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)
    baseline = object()
    seen_baselines: list[object] = []

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_passing_command(tmp_path)])

    async def _retry_after_cleanup_failure(*_args: object, **kwargs: object) -> tuple[bool, Any]:
        run_agent = kwargs["run_agent"]
        try:
            await run_agent(False)
        except ComposeExecCleanupError:
            return True, await run_agent(True)
        raise AssertionError("first conformance attempt should fail cleanup")

    async def _conformance_check(**kwargs: object) -> _PlanningRunFailure | None:
        seen_baselines.append(kwargs["conformance_scope_baseline"])
        if len(seen_baselines) == 1:
            raise ComposeExecCleanupError(
                invocation_id="conf-1",
                source="agent",
                label="post-validation conformance",
                message='service "agent" is not running',
                cleanup_result=CommandResult(
                    returncode=1,
                    stdout="",
                    stderr='service "agent" is not running',
                ),
            )
        return _PlanningRunFailure(
            message="post-validation conformance phase changed files outside `report.md`",
            reason_code="PLAN_CONFORMANCE_SCOPE_VIOLATION",
            details={"offending_paths": ["src/app.py"]},
        )

    monkeypatch.setattr(
        executor_execution_validation,
        "_run_agent_callable_with_service_recovery",
        _retry_after_cleanup_failure,
    )

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=2,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-conf-retry-scope"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _capture_post_validation_conformance_scope_baseline=AsyncMock(return_value=baseline),
        _run_post_validation_conformance_check=_conformance_check,
    )

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=SimpleNamespace(run=AsyncMock()),
        planning_validation_handoff=_handoff(tmp_path, iteration=2, max_iterations=2),
    )

    assert result.stop
    assert seen_baselines == [baseline, baseline]
    executor._capture_post_validation_conformance_scope_baseline.assert_awaited_once_with(
        tmp_path / "worktree",
        (tmp_path / "report.md"),
    )
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_kwargs["reason_code"] == "PLAN_CONFORMANCE_SCOPE_VIOLATION"
    executor._mark_failed.assert_awaited_once()
