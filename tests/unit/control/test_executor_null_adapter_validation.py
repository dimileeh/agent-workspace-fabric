"""Unit tests for executor validation behavior when adapter is None (e.g. during recovery)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.common.commands import CommandResult
from awf.control.executor import execution_validation as executor_execution_validation
from awf.control.executor.constants import POST_VALIDATION_CONFORMANCE_FAILED_REASON_CODE
from awf.db.enums import (
    FailureReason,
    OperationStatus,
    WorkspaceStatus,
)
from awf.profiles.models import WorkspaceProfile
from awf.runtime.validation import (
    ValidationCommandResult,
    ValidationResult,
)
from awf.runtime.validation_worktree import ValidationWorktreeCheck


@pytest.mark.unit
async def test_execution_validation_fails_cleanly_when_adapter_is_none_and_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When adapter is None and validation fails, fix pass is skipped and workspace fails cleanly."""
    profile = WorkspaceProfile.model_validate(
        {"name": "test-null-adapter-fail", "planning": {"required": True}}
    )

    failing_cmd = ValidationCommandResult(
        command="pytest",
        returncode=1,
        duration_seconds=1.0,
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
        phase="validate",
    )
    (tmp_path / "stdout.log").write_text("1 failed", encoding="utf-8")
    (tmp_path / "stderr.log").write_text("", encoding="utf-8")

    failing_val_result = ValidationResult(
        commands=[failing_cmd],
    )

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _ensure_worktree_available=AsyncMock(return_value=True),
        _start_validation_run=AsyncMock(return_value="vr-null-adapter-1"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=SimpleNamespace(run_profile_phases=AsyncMock(return_value=failing_val_result)),
        _config=SimpleNamespace(
            max_validation_fix_passes=2,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _session_factory=AsyncMock(),
    )

    workspace = SimpleNamespace(
        id="ws_null_adapter_fail",
        head_sha="a" * 40,
        task_tag=None,
        resolved_profile=profile.model_dump(),
        pr_url=None,
    )

    monkeypatch.setattr(
        executor_execution_validation,
        "_profile_for_workspace",
        lambda *_args, **_kwargs: profile,
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "_sync_resolved_profile",
        AsyncMock(return_value=profile),
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "check_validation_worktree_clean",
        AsyncMock(return_value=ValidationWorktreeCheck(clean=True)),
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "cleanup_validation_worktree_side_effects",
        AsyncMock(
            return_value=SimpleNamespace(
                ok=True, side_effect_paths=(), check=ValidationWorktreeCheck(clean=True)
            )
        ),
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "profile_phase_command_plan",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "_validation_tier_for_workspace",
        lambda *_args, **_kwargs: 1,
    )

    result = await executor_execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id="ws_null_adapter_fail",
        ws=workspace,  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
        compose_project="awf_ws_null_adapter_fail",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch="awf/ws_null_adapter_fail",
        adapter=None,  # Null adapter during recovery
        default_model=None,
        baseline_coverage=None,
        planning_validation_handoff=None,
        recovery={"recovery_mode": "validate_only", "source": "pr_monitor"},
        rebase_recovery_result=None,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert result.stop
    assert result.successful_validation_run_id is None

    # Operations should be finished as failed
    executor._finish_pending_validate_operations.assert_awaited_once()
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_kwargs["validation_run_id"] == "vr-null-adapter-1"
    assert finish_kwargs["status"] == OperationStatus.failed

    # Workspace should be marked failed cleanly
    executor._mark_failed.assert_awaited_once()
    mark_kwargs = executor._mark_failed.await_args.kwargs
    assert mark_kwargs["from_status"] == WorkspaceStatus.validating
    assert mark_kwargs["failure_reason"] == FailureReason.validation_failure


@pytest.mark.unit
async def test_execution_validation_fails_when_adapter_is_none_and_conformance_is_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When adapter is None and planning_validation_handoff is present, validation fails because conformance cannot be checked."""
    profile = WorkspaceProfile.model_validate(
        {"name": "test-null-adapter-pass", "planning": {"required": True}}
    )

    passing_val_result = ValidationResult(
        commands=[],
    )

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _ensure_worktree_available=AsyncMock(return_value=True),
        _start_validation_run=AsyncMock(return_value="vr-null-adapter-2"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=SimpleNamespace(run_profile_phases=AsyncMock(return_value=passing_val_result)),
        _config=SimpleNamespace(
            max_validation_fix_passes=2,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _session_factory=AsyncMock(),
    )

    workspace = SimpleNamespace(
        id="ws_null_adapter_pass",
        head_sha="a" * 40,
        task_tag=None,
        resolved_profile=profile.model_dump(),
        pr_url="https://github.com/org/repo/pull/1",
    )

    fake_handoff = SimpleNamespace(
        report_path="docs/awf-plans/ws_null_adapter_pass.conformance.json",
        max_iterations=3,
        iteration=1,
    )

    monkeypatch.setattr(
        executor_execution_validation,
        "_profile_for_workspace",
        lambda *_args, **_kwargs: profile,
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "_sync_resolved_profile",
        AsyncMock(return_value=profile),
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "check_validation_worktree_clean",
        AsyncMock(return_value=ValidationWorktreeCheck(clean=True)),
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "cleanup_validation_worktree_side_effects",
        AsyncMock(
            return_value=SimpleNamespace(
                ok=True, side_effect_paths=(), check=ValidationWorktreeCheck(clean=True)
            )
        ),
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "profile_phase_command_plan",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "_validation_tier_for_workspace",
        lambda *_args, **_kwargs: 1,
    )

    result = await executor_execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id="ws_null_adapter_pass",
        ws=workspace,  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
        compose_project="awf_ws_null_adapter_pass",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch="awf/ws_null_adapter_pass",
        adapter=None,  # Null adapter during recovery
        default_model=None,
        baseline_coverage=None,
        planning_validation_handoff=fake_handoff,  # Handoff present from previous planning
        recovery={"recovery_mode": "validate_only", "source": "pr_monitor"},
        rebase_recovery_result=None,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert result.stop
    assert result.successful_validation_run_id is None

    # Operations should be finished as failed
    executor._finish_pending_validate_operations.assert_awaited_once()
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_kwargs["validation_run_id"] == "vr-null-adapter-2"
    assert finish_kwargs["status"] == OperationStatus.failed
    assert finish_kwargs["reason_code"] == POST_VALIDATION_CONFORMANCE_FAILED_REASON_CODE
    assert "agent adapter is unavailable" in finish_kwargs["error_message"]

    # Workspace should be marked failed cleanly
    executor._mark_failed.assert_awaited_once()
    mark_kwargs = executor._mark_failed.await_args.kwargs
    assert mark_kwargs["from_status"] == WorkspaceStatus.validating
    assert mark_kwargs["failure_reason"] == FailureReason.infrastructure_failure
