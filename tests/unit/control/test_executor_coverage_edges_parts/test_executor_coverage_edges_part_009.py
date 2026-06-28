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
from awf.control.executor.constants import (
    POST_VALIDATION_CONFORMANCE_REPORT_CLEANUP_FAILED_REASON_CODE,
)
from awf.control.executor.types import (
    _PlanningRunFailure,
    _PlanningValidationHandoff,
)
from awf.control.quality_gates import PLAN_ONLY_OUTPUT_REASON_CODE
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


@pytest.mark.unit
async def test_fix_pass_status_recheck_race_before_agent_run_stops(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A status race right before the fix-pass agent run stops the cycle cleanly."""
    profile = WorkspaceProfile.model_validate({"name": "prof-fix-recheck-agent"})
    workspace = _workspace("ws_fix_recheck_agent")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)
    deposit_calls: list[str] = []

    def _spy_deposit(*_args: object, **kwargs: object) -> None:
        deposit_calls.append(str(kwargs["workspace_id"]))

    monkeypatch.setattr(
        executor_execution_validation._planning_artifacts,
        "_deposit_planning_artifacts_best_effort",
        _spy_deposit,
    )

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_failing_command(tmp_path)])

    # First recheck (top of loop / "validate") passes; the recheck guarding
    # the fix-pass agent run returns False to simulate a mid-flight cancel.
    recheck = AsyncMock(side_effect=[True, False])
    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=recheck,
        _config=SimpleNamespace(
            max_validation_fix_passes=1,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-fix-recheck-agent"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
    )
    adapter = SimpleNamespace(run=AsyncMock())

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
    )

    assert result.stop
    assert result.successful_validation_run_id is None
    # The agent fix run never happened because the recheck failed first.
    adapter.run.assert_not_awaited()
    assert recheck.await_count == 2
    last_recheck_kwargs = recheck.await_args.kwargs
    assert last_recheck_kwargs["action"] == "validation_fix_agent_run"
    assert deposit_calls == [workspace.id]


@pytest.mark.unit
async def test_fix_pass_recovery_abort_marks_validating_workspace_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression PRRT_kwDOSJAM6s6MwuSg: fix-pass recovery abort is terminal."""
    profile = WorkspaceProfile.model_validate({"name": "prof-fix-recovery-abort"})
    workspace = _workspace("ws_fix_recovery_abort")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_failing_command(tmp_path)])

    async def _abort_recovery(*_args: object, **kwargs: object) -> tuple[bool, object | None]:
        assert kwargs["before_mark_failed_marks_workspace"] is True
        before_mark_failed = kwargs["before_mark_failed"]
        await before_mark_failed(reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED")
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
            max_validation_fix_passes=1,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-fix-recovery-abort"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _ensure_worktree_available=AsyncMock(return_value=True),
    )
    adapter = SimpleNamespace(run=AsyncMock())

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
    )

    assert result.stop
    adapter.run.assert_not_awaited()
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_kwargs["status"] == OperationStatus.failed
    assert finish_kwargs["reason_code"] == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
    mark_kwargs = executor._mark_failed.await_args.kwargs
    assert mark_kwargs["from_status"] is WorkspaceStatus.validating
    assert mark_kwargs["failure_reason"] is FailureReason.infrastructure_failure
    assert mark_kwargs["reason_code"] == "MIRROR_HOOKS_PATH_REPAIR_FAILED"


@pytest.mark.unit
async def test_unexpected_validation_cleanup_guard_deposits_planning_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When an unexpected validation exception is terminally handled by the
    cleanup guard, successful cleanup still deposits planning artifacts before
    returning."""
    profile = WorkspaceProfile.model_validate(
        {"name": "prof-unexpected-cleanup", "planning": {"required": True}}
    )
    workspace = _workspace("ws_unexpected_cleanup")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    guard_result = executor_execution_validation.ExecutionValidationResult(
        stop=True,
        successful_validation_run_id=None,
        successful_validation_workspace_head_sha=None,
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "_handle_validation_cleanup_guard",
        AsyncMock(return_value=guard_result),
    )

    deposit_calls: list[str] = []

    def _spy_deposit(*_args: object, **kwargs: object) -> None:
        deposit_calls.append(str(kwargs["workspace_id"]))

    monkeypatch.setattr(
        executor_execution_validation._planning_artifacts,
        "_deposit_planning_artifacts_best_effort",
        _spy_deposit,
    )

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            raise RuntimeError("validation runner exploded")

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-unexpected-cleanup"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
    )

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=SimpleNamespace(run=AsyncMock()),
    )

    assert result is guard_result
    assert deposit_calls == [workspace.id]
    executor._finish_validation_run.assert_not_awaited()
    executor._mark_failed.assert_not_awaited()


@pytest.mark.unit
async def test_fix_pass_status_recheck_race_before_commit_stops(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A status race right before the fix-pass commit recheck stops the cycle."""
    profile = WorkspaceProfile.model_validate({"name": "prof-fix-recheck-commit"})
    workspace = _workspace("ws_fix_recheck_commit")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)
    deposit_calls: list[str] = []

    def _spy_deposit(*_args: object, **kwargs: object) -> None:
        deposit_calls.append(str(kwargs["workspace_id"]))

    monkeypatch.setattr(
        executor_execution_validation._planning_artifacts,
        "_deposit_planning_artifacts_best_effort",
        _spy_deposit,
    )

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_failing_command(tmp_path)])

    # rechecks: validate(True) -> validation_fix_agent_run(True) ->
    # validation_fix_commit(False) triggers the early stop at line 1112.
    recheck = AsyncMock(side_effect=[True, True, False])
    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=recheck,
        _config=SimpleNamespace(
            max_validation_fix_passes=1,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-fix-recheck-commit"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _ensure_worktree_available=AsyncMock(return_value=True),
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(stdout="", stderr="")))

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
    )

    assert result.stop
    assert result.successful_validation_run_id is None
    # The agent fix run executed; the race happened on the post-run commit recheck.
    adapter.run.assert_awaited_once()
    assert recheck.await_count == 3
    assert recheck.await_args.kwargs["action"] == "validation_fix_commit"
    assert deposit_calls == [workspace.id]


@pytest.mark.unit
@pytest.mark.parametrize(
    "unavailable_action",
    [
        "validation_fix_agent_run",
        "validation_fix_git_add",
        "validation_fix_git_diff",
        "validation_fix_git_commit",
    ],
)
async def test_fix_pass_worktree_guard_stops_deposit_planning_artifacts(
    unavailable_action: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression PRRT_kwDOSJAM6s6KxQa0: fix-pass worktree guard stops must
    deposit planning artifacts before returning stop=True.
    """
    profile = WorkspaceProfile.model_validate({"name": f"prof-{unavailable_action}"})
    workspace = _workspace(f"ws_{unavailable_action}")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)
    monkeypatch.setattr(
        executor_execution_validation,
        "find_protected_quality_gate_changes",
        lambda **_kwargs: [],
    )

    deposit_calls: list[str] = []

    def _spy_deposit(*_args: object, **kwargs: object) -> None:
        deposit_calls.append(str(kwargs["workspace_id"]))

    monkeypatch.setattr(
        executor_execution_validation._planning_artifacts,
        "_deposit_planning_artifacts_best_effort",
        _spy_deposit,
    )

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_failing_command(tmp_path)])

    async def _ensure_worktree_available(**kwargs: object) -> bool:
        return kwargs.get("action") != unavailable_action

    git_in_worktree = AsyncMock(
        side_effect=[
            CommandResult(returncode=0, stdout="", stderr=""),  # git add -A
            CommandResult(
                returncode=0,
                stdout="src/foo.py\n",
                stderr="",
            ),  # git diff --cached --name-only
        ]
    )
    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=1,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value=f"vr-{unavailable_action}"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _ensure_worktree_available=AsyncMock(side_effect=_ensure_worktree_available),
        _repair_agent_git_ownership=AsyncMock(),
        _refresh_supply_chain_policy_for_workspace=AsyncMock(
            return_value=SimpleNamespace(policy_blocked=False),
        ),
        _committed_and_staged_output_is_plan_only=AsyncMock(return_value=False),
        _active_operator_grant_specs=AsyncMock(return_value=[]),
        _protected_file_diffs_for_staged_paths=AsyncMock(return_value=()),
        _runner=SimpleNamespace(run=AsyncMock(return_value=CommandResult(0, "", ""))),
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(stdout="", stderr="")))

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
        git_in_worktree=git_in_worktree,
    )

    assert result.stop
    assert deposit_calls == [workspace.id]


@pytest.mark.unit
async def test_fix_pass_git_add_failure_records_command_reason_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failing fix-pass ``git add`` with a reason_code records command_reason_code."""
    profile = WorkspaceProfile.model_validate({"name": "prof-fix-add-fail"})
    workspace = _workspace("ws_fix_add_fail")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_failing_command(tmp_path)])

    # The fix-pass ``git add -A`` fails and carries a command reason_code that
    # must be threaded into the failure details (line 1153).
    git_in_worktree = AsyncMock(
        return_value=CommandResult(
            returncode=1,
            stdout="",
            stderr="index lock",
            reason_code="GIT_ADD_LOCKED",
        )
    )
    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=1,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-fix-add-fail"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _ensure_worktree_available=AsyncMock(return_value=True),
        _repair_agent_git_ownership=AsyncMock(),
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(stdout="", stderr="")))

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
        git_in_worktree=git_in_worktree,
    )

    assert result.stop
    mark_kwargs = executor._mark_failed.await_args.kwargs
    assert mark_kwargs["reason_code"] == "VALIDATION_FIX_GIT_ADD_FAILED"
    assert mark_kwargs["details"]["operation"] == "git add -A"
    assert mark_kwargs["details"]["command_reason_code"] == "GIT_ADD_LOCKED"


@pytest.mark.unit
async def test_validation_failure_deposits_planning_artifacts_before_mark_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A terminal in-cycle validation failure deposits the plan/conformance
    artifacts BEFORE publishing the FAILED status.

    The console keys its artifact refetch on the workspace ``updated_at``
    (TaskArtifactsSection ``refreshKey``); ``_mark_failed`` bumps ``updated_at``
    when it publishes FAILED, but the filesystem deposit does not touch the row.
    Depositing after the mark would let a poll observe the terminal status in
    the window before the copy, record an empty artifact list, then never
    refetch — hiding the Plan/Validation controls. The cycle must therefore
    deposit before every in-cycle ``_mark_failed``.
    """
    profile = WorkspaceProfile.model_validate(
        {"name": "prof-validation-deposit", "planning": {"required": True}}
    )
    workspace = _workspace("ws_validation_deposit")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    order: list[str] = []
    real_deposit = (
        executor_execution_validation._planning_artifacts._deposit_planning_artifacts_best_effort
    )

    def _spy_deposit(*args: object, **kwargs: object) -> None:
        order.append("deposit")
        real_deposit(*args, **kwargs)

    monkeypatch.setattr(
        executor_execution_validation._planning_artifacts,
        "_deposit_planning_artifacts_best_effort",
        _spy_deposit,
    )

    async def _mark_failed(**_kwargs: object) -> None:
        order.append("mark_failed")

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_failing_command(tmp_path)])

    # A fix-pass ``git add -A`` failure drives a terminal in-cycle ``_mark_failed``
    # deterministically without the agent producing a real fix.
    git_in_worktree = AsyncMock(
        return_value=CommandResult(
            returncode=1, stdout="", stderr="index lock", reason_code="GIT_ADD_LOCKED"
        )
    )
    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=1,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-validation-deposit"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=_mark_failed,
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _ensure_worktree_available=AsyncMock(return_value=True),
        _repair_agent_git_ownership=AsyncMock(),
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(stdout="", stderr="")))

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
        git_in_worktree=git_in_worktree,
    )

    assert result.stop
    assert order.index("deposit") < order.index("mark_failed")


@pytest.mark.unit
async def test_fix_pass_plan_only_output_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A fix pass that stages only plan artifacts fails with PLAN_ONLY_OUTPUT."""
    profile = WorkspaceProfile.model_validate({"name": "prof-fix-plan-only"})
    workspace = _workspace("ws_fix_plan_only")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_failing_command(tmp_path)])

    # ``git add -A`` succeeds (ok); ``git diff --cached --name-only`` reports a
    # single staged plan artifact, which the plan-only guard rejects.
    git_in_worktree = AsyncMock(
        side_effect=[
            CommandResult(returncode=0, stdout="", stderr=""),  # git add -A
            CommandResult(
                returncode=0,
                stdout="docs/awf-plans/plan.md\n",
                stderr="",
            ),  # git diff --cached --name-only
        ]
    )
    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=1,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-fix-plan-only"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _ensure_worktree_available=AsyncMock(return_value=True),
        _repair_agent_git_ownership=AsyncMock(),
        _refresh_supply_chain_policy_for_workspace=AsyncMock(
            return_value=SimpleNamespace(policy_blocked=False),
        ),
        _committed_and_staged_output_is_plan_only=AsyncMock(return_value=True),
        _fail_if_plan_only_paths=AsyncMock(return_value=True),
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(stdout="", stderr="")))

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
        git_in_worktree=git_in_worktree,
    )

    assert result.stop
    executor._fail_if_plan_only_paths.assert_awaited_once()
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_kwargs["status"] == OperationStatus.failed
    assert finish_kwargs["reason_code"] == PLAN_ONLY_OUTPUT_REASON_CODE


@pytest.mark.unit
async def test_post_validation_conformance_fix_pass_loop_falls_through_to_continue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A conformance fix pass that exhausts attempts re-validates then exits the loop.

    Drives a planning-validation-handoff flow where validation passes but the
    post-validation conformance check reports a gap with a remaining iteration
    budget. The conformance fix-pass synthesises a failing result, re-runs the
    agent, re-commits nothing, and loops back. The handoff is exhausted on the
    next attempt so the loop completes without ``break`` (the 260->1418 fall
    through), returning ``stop=False`` with no successful run id.
    """
    profile = WorkspaceProfile.model_validate({"name": "prof-conf-fix-loop"})
    workspace = _workspace("ws_conf_fix_loop")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_passing_command(tmp_path)])

    # First conformance check reports a gap (remaining budget > 0 → fix pass);
    # second conformance check passes (returns None) on the next iteration.
    conformance_failure = _PlanningRunFailure(
        message="conformance gap",
        reason_code="PLAN_CONFORMANCE_UNSATISFIED",
        details={"attempt": 1},
    )
    conformance_check = AsyncMock(side_effect=[conformance_failure, None])

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(side_effect=["vr-1", "vr-2"]),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _run_post_validation_conformance_check=conformance_check,
        _ensure_worktree_available=AsyncMock(return_value=True),
        _repair_agent_git_ownership=AsyncMock(),
        _refresh_supply_chain_policy_for_workspace=AsyncMock(
            return_value=SimpleNamespace(policy_blocked=False),
        ),
        _fail_if_plan_only_paths=AsyncMock(return_value=False),
        _protected_file_diffs_for_staged_paths=AsyncMock(return_value=()),
        _runner=SimpleNamespace(run=AsyncMock(return_value=CommandResult(0, "", ""))),
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(stdout="", stderr="")))

    # handoff: iteration 1 of 2 → exactly one remaining conformance iteration.
    handoff = _handoff(tmp_path, iteration=1, max_iterations=2)

    # The fix-pass produces no staged changes (empty diff) so no commit happens;
    # the loop re-validates and conformance passes on attempt 2.
    git_in_worktree = AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr=""))

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
        planning_validation_handoff=handoff,
        git_in_worktree=git_in_worktree,
    )

    # Conformance passed on the second pass → successful run recorded, break.
    assert not result.stop
    assert result.successful_validation_run_id == "vr-2"
    assert conformance_check.await_count == 2
    # The conformance fix pass re-invoked the agent exactly once between checks.
    assert adapter.run.await_count == 1


@pytest.mark.unit
async def test_post_validation_conformance_report_cleanup_failure_skips_fix_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Report cleanup residue is AWF/git failure, not an agent-correctable gap."""
    profile = WorkspaceProfile.model_validate({"name": "prof-conf-cleanup"})
    workspace = _workspace("ws_conf_cleanup")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)
    deposit_calls: list[str] = []

    def _spy_deposit(*_args: object, **_kwargs: object) -> None:
        deposit_calls.append("deposit")

    monkeypatch.setattr(
        executor_execution_validation._planning_artifacts,
        "_deposit_planning_artifacts_best_effort",
        _spy_deposit,
    )

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_passing_command(tmp_path)])

    conformance_failure = _PlanningRunFailure(
        message="post-validation conformance report cleanup left report path dirty: report.json",
        reason_code=POST_VALIDATION_CONFORMANCE_REPORT_CLEANUP_FAILED_REASON_CODE,
        details={"conformance_report_cleanup": {"report_path": "report.json"}},
    )
    conformance_check = AsyncMock(return_value=conformance_failure)

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=2,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-1"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _run_post_validation_conformance_check=conformance_check,
        _ensure_worktree_available=AsyncMock(return_value=True),
        _repair_agent_git_ownership=AsyncMock(),
        _refresh_supply_chain_policy_for_workspace=AsyncMock(
            return_value=SimpleNamespace(policy_blocked=False),
        ),
        _fail_if_plan_only_paths=AsyncMock(return_value=False),
        _protected_file_diffs_for_staged_paths=AsyncMock(return_value=()),
        _runner=SimpleNamespace(run=AsyncMock(return_value=CommandResult(0, "", ""))),
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(stdout="", stderr="")))

    # Handoff has remaining iteration budget, but cleanup residue must not trigger
    # a post-validation conformance fix pass.
    handoff = _handoff(tmp_path, iteration=1, max_iterations=3)

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
        planning_validation_handoff=handoff,
    )

    assert result.stop
    assert conformance_check.await_count == 1
    assert adapter.run.await_count == 0
    assert deposit_calls == []
    executor._mark_failed.assert_awaited_once()
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_kwargs["status"] == OperationStatus.failed
    assert (
        finish_kwargs["reason_code"]
        == POST_VALIDATION_CONFORMANCE_REPORT_CLEANUP_FAILED_REASON_CODE
    )
    mark_failed_kwargs = executor._mark_failed.await_args.kwargs
    assert mark_failed_kwargs["failure_reason"] == FailureReason.infrastructure_failure


@pytest.mark.unit
async def test_grant_resume_conformance_failure_skips_fix_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A grant-bearing resume never fires a conformance fix pass (PRRT_kwDOSJAM6s6KADN4).

    For a combined ``--directive ... --grant ...`` resume, ``resume_disable_fix_passes``
    is true. Even though the planning handoff has a remaining conformance iteration
    budget, a post-validation conformance miss must mark the workspace FAILED for
    operator triage rather than re-invoking the coding agent — re-running while
    operator grants are active could rewrite a granted protected file and have the
    new violation suppressed by the same single-use grant.
    """
    profile = WorkspaceProfile.model_validate({"name": "prof-grant-conf"})
    workspace = _workspace("ws_grant_conf")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_passing_command(tmp_path)])

    conformance_failure = _PlanningRunFailure(
        message="conformance gap",
        reason_code="PLAN_CONFORMANCE_UNSATISFIED",
        details={"attempt": 1},
    )
    conformance_check = AsyncMock(return_value=conformance_failure)

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=2,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-1"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _run_post_validation_conformance_check=conformance_check,
        _ensure_worktree_available=AsyncMock(return_value=True),
        _repair_agent_git_ownership=AsyncMock(),
        _refresh_supply_chain_policy_for_workspace=AsyncMock(
            return_value=SimpleNamespace(policy_blocked=False),
        ),
        _fail_if_plan_only_paths=AsyncMock(return_value=False),
        _protected_file_diffs_for_staged_paths=AsyncMock(return_value=()),
        _runner=SimpleNamespace(run=AsyncMock(return_value=CommandResult(0, "", ""))),
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(stdout="", stderr="")))

    # Handoff has a remaining iteration budget (would normally allow a fix pass).
    handoff = _handoff(tmp_path, iteration=1, max_iterations=3)

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
        planning_validation_handoff=handoff,
        resume_disable_fix_passes=True,
    )

    # Conformance checked once, then marked FAILED — no fix-pass agent re-invocation.
    assert result.stop
    assert conformance_check.await_count == 1
    assert adapter.run.await_count == 0
    executor._mark_failed.assert_awaited_once()
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_kwargs["status"] == OperationStatus.failed
    assert finish_kwargs["reason_code"] == "PLAN_CONFORMANCE_UNSATISFIED"
