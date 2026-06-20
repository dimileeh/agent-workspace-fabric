"""Focused branch-coverage tests for executor execution-validation behavior.

Split out of ``test_executor_coverage_edges_part_003.py`` to keep first-party
files under the maintainability line limit.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.common.commands import CommandResult
from awf.control.executor import execution_validation as executor_execution_validation
from awf.db.enums import (
    FailureReason,
    OperationStatus,
)
from awf.profiles.models import WorkspaceProfile
from awf.runtime.validation_worktree import ValidationWorktreeCheck
from awf.runtime.validation_worktree_constants import (
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
)
from awf.service import artifacts as executor_service_artifacts


@pytest.mark.unit
async def test_execution_validation_returns_stop_when_start_transition_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = WorkspaceProfile.model_validate(
        {"name": "validation-stale-start", "planning": {"required": True}}
    )
    calls: list[str] = []
    real_deposit = (
        executor_execution_validation._planning_artifacts._deposit_planning_artifacts_best_effort
    )

    def _spy_deposit(*_args: object, **_kwargs: object) -> None:
        calls.append("deposit")
        real_deposit(*_args, **_kwargs)

    monkeypatch.setattr(
        executor_execution_validation._planning_artifacts,
        "_deposit_planning_artifacts_best_effort",
        _spy_deposit,
    )

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=False),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _session_factory=AsyncMock,
    )

    async def _sync_resolved_profile(*_args: object, **_kwargs: object) -> WorkspaceProfile:
        return profile

    monkeypatch.setattr(
        executor_execution_validation,
        "_profile_for_workspace",
        lambda *_args, **_kwargs: profile,
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "_sync_resolved_profile",
        _sync_resolved_profile,
    )

    worktree_path = tmp_path / "worktree"
    plan_path = worktree_path / "docs" / "awf-plans" / "ws_stale_validation.md"
    report_path = worktree_path / "docs" / "awf-plans" / "ws_stale_validation.conformance.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# plan\n", encoding="utf-8")
    report_path.write_text('{"status":"satisfied"}', encoding="utf-8")

    result = await executor_execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id="ws_stale_validation",
        ws=SimpleNamespace(resolved_profile=profile.model_dump()),
        worktree_path=worktree_path,
        compose_project="awf_ws_stale_validation",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch="awf/ws_stale_validation",
        adapter=object(),  # type: ignore[arg-type]
        default_model=None,
        baseline_coverage=None,
        planning_validation_handoff=None,
        recovery=None,
        rebase_recovery_result=None,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert result.stop
    assert calls == ["deposit"]
    artifact_dir = executor_service_artifacts.workspace_artifact_dir(
        (tmp_path / "artifacts").parent, "ws_stale_validation"
    )
    assert (artifact_dir / "plan.md").read_text(encoding="utf-8") == "# plan\n"
    deposited_report = json.loads((artifact_dir / "conformance.json").read_text(encoding="utf-8"))
    assert deposited_report["status"] == "satisfied"


@pytest.mark.unit
async def test_execution_validation_returns_stop_when_validate_recheck_is_stale(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile = WorkspaceProfile.model_validate(
        {"name": "validation-stale", "planning": {"required": True}}
    )
    calls: list[str] = []
    real_deposit = (
        executor_execution_validation._planning_artifacts._deposit_planning_artifacts_best_effort
    )

    def _spy_deposit(*_args: object, **_kwargs: object) -> None:
        calls.append("deposit")
        real_deposit(*_args, **_kwargs)

    monkeypatch.setattr(
        executor_execution_validation._planning_artifacts,
        "_deposit_planning_artifacts_best_effort",
        _spy_deposit,
    )

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=False),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
    )
    workspace = SimpleNamespace(
        resolved_profile=profile.model_dump(),
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        test_commands=[],
        task_class=None,
        operations=[],
    )
    worktree_path = tmp_path / "worktree"
    plan_path = worktree_path / "docs" / "awf-plans" / "ws_recheck_stale.md"
    report_path = worktree_path / "docs" / "awf-plans" / "ws_recheck_stale.conformance.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# plan\n", encoding="utf-8")
    report_path.write_text('{"status":"satisfied"}', encoding="utf-8")

    async def _sync_profile_passthrough(*_args: object, **kwargs: object) -> WorkspaceProfile:
        passthrough = kwargs["profile"]
        assert isinstance(passthrough, WorkspaceProfile)
        return passthrough

    monkeypatch.setattr(
        executor_execution_validation,
        "_profile_for_workspace",
        lambda *_args, **_kwargs: profile,
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "_sync_resolved_profile",
        _sync_profile_passthrough,
    )

    result = await executor_execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id="ws_recheck_stale",
        ws=workspace,  # type: ignore[arg-type]
        worktree_path=worktree_path,
        compose_project="awf_ws_recheck_stale",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch="awf/ws_recheck_stale",
        adapter=object(),  # type: ignore[arg-type]
        default_model=None,
        baseline_coverage=None,
        planning_validation_handoff=None,
        recovery=None,
        rebase_recovery_result=None,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert result.stop
    assert calls == ["deposit"]
    artifact_dir = executor_service_artifacts.workspace_artifact_dir(
        (tmp_path / "artifacts").parent, "ws_recheck_stale"
    )
    assert (artifact_dir / "plan.md").read_text(encoding="utf-8") == "# plan\n"
    deposited_report = json.loads((artifact_dir / "conformance.json").read_text(encoding="utf-8"))
    assert deposited_report["status"] == "satisfied"


@pytest.mark.unit
async def test_execution_validation_fails_when_workspace_head_sha_cannot_be_captured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Ensure validation short-circuits when workspace head SHA cannot be captured."""
    profile = WorkspaceProfile.model_validate({"name": "validation-missing-head"})
    workspace = SimpleNamespace(
        resolved_profile={"name": "validation-missing-head"},
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        test_commands=[],
        task_class=None,
        operations=[],
    )
    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(max_validation_fix_passes=0, planning_max_iterations_default=3),
        _capture_workspace_head_sha=AsyncMock(return_value=None),
        _start_validation_run=AsyncMock(return_value="vr-missing-head"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
    )

    async def _sync_resolved_profile(*_args: object, **_kwargs: object) -> WorkspaceProfile:
        """Stub resolved profile lookup for head-capture failure path."""
        return profile

    monkeypatch.setattr(
        executor_execution_validation,
        "_profile_for_workspace",
        lambda *_args, **_kwargs: profile,
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "_sync_resolved_profile",
        _sync_resolved_profile,
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
        workspace_id="ws_missing_head",
        ws=workspace,  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
        compose_project="awf_ws_missing_head",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch="awf/ws_missing_head",
        adapter=object(),  # type: ignore[arg-type]
        default_model=None,
        baseline_coverage=None,
        planning_validation_handoff=None,
        recovery=None,
        rebase_recovery_result=None,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert result.stop
    assert result.successful_validation_run_id is None
    executor._start_validation_run.assert_awaited_once_with(
        workspace_id="ws_missing_head",
        profile=profile,
        base_commit="b" * 40,
        workspace_head_sha=None,
        target_branch="awf/ws_missing_head",
        target_head_sha=None,
        tier=1,
    )
    executor._finish_validation_run.assert_awaited_once()
    finish_kwargs = executor._finish_validation_run.await_args.kwargs
    assert finish_kwargs["status"] == "failed"
    assert finish_kwargs["reason_code"] == "VALIDATION_INFRASTRUCTURE_ERROR"
    executor._finish_pending_validate_operations.assert_awaited_once()
    assert executor._finish_pending_validate_operations.await_args.kwargs["status"] == "failed"
    executor._mark_failed.assert_awaited_once()
    mark_kwargs = executor._mark_failed.await_args.kwargs
    assert getattr(mark_kwargs["from_status"], "value", mark_kwargs["from_status"]) == "validating"
    assert mark_kwargs["failure_reason"] == FailureReason.infrastructure_failure
    assert mark_kwargs["reason_code"] == "VALIDATION_INFRASTRUCTURE_ERROR"


@pytest.mark.unit
async def test_execution_validation_reports_dirty_worktree_when_head_capture_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Ensure pre-validation dirty worktrees are not hidden by HEAD capture failures."""
    profile = WorkspaceProfile.model_validate({"name": "validation-dirty-missing-head"})
    workspace = SimpleNamespace(
        resolved_profile={"name": "validation-dirty-missing-head"},
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        test_commands=[],
        task_class=None,
        operations=[],
    )
    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(max_validation_fix_passes=0, planning_max_iterations_default=3),
        _capture_workspace_head_sha=AsyncMock(return_value=None),
        _start_validation_run=AsyncMock(return_value="vr-dirty-missing-head"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
    )
    check_worktree_clean = AsyncMock(
        return_value=ValidationWorktreeCheck(
            clean=False,
            reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
            message="dirty file prevents validation",
        )
    )

    async def _sync_resolved_profile(*_args: object, **_kwargs: object) -> WorkspaceProfile:
        """Stub resolved profile lookup for dirty-before-missing-head test."""
        return profile

    monkeypatch.setattr(
        executor_execution_validation,
        "_profile_for_workspace",
        lambda *_args, **_kwargs: profile,
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "_sync_resolved_profile",
        _sync_resolved_profile,
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "check_validation_worktree_clean",
        check_worktree_clean,
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
        workspace_id="ws_dirty_missing_head",
        ws=workspace,  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
        compose_project="awf_ws_dirty_missing_head",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch="awf/ws_dirty_missing_head",
        adapter=object(),  # type: ignore[arg-type]
        default_model=None,
        baseline_coverage=None,
        planning_validation_handoff=None,
        recovery=None,
        rebase_recovery_result=None,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert result.stop
    check_worktree_clean.assert_awaited_once()
    executor._start_validation_run.assert_awaited_once_with(
        workspace_id="ws_dirty_missing_head",
        profile=profile,
        base_commit="b" * 40,
        workspace_head_sha=None,
        target_branch="awf/ws_dirty_missing_head",
        target_head_sha=None,
        tier=1,
    )
    executor._finish_validation_run.assert_awaited_once()
    assert (
        executor._finish_validation_run.await_args.kwargs["reason_code"]
        == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    )
    executor._finish_pending_validate_operations.assert_awaited_once()
    assert (
        executor._finish_pending_validate_operations.await_args.kwargs["reason_code"]
        == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    )
    executor._mark_failed.assert_awaited_once()
    mark_kwargs = executor._mark_failed.await_args.kwargs
    assert mark_kwargs["failure_reason"] == FailureReason.infrastructure_failure
    assert mark_kwargs["reason_code"] == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY


@pytest.mark.unit
async def test_execution_validation_fails_when_worktree_is_dirty_before_starting_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Ensure a dirty worktree finalizes its started validation run."""
    profile = WorkspaceProfile.model_validate({"name": "validation-dirty-worktree"})
    workspace = SimpleNamespace(
        resolved_profile={"name": "validation-dirty-worktree"},
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        test_commands=[],
        task_class=None,
        operations=[],
    )
    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(max_validation_fix_passes=0, planning_max_iterations_default=3),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-dirty-worktree"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
    )

    async def _sync_resolved_profile(*_args: object, **_kwargs: object) -> WorkspaceProfile:
        """Stub resolved profile lookup for dirty-worktree preflight test."""
        return profile

    async def _check_worktree_clean(*_args: object, **_kwargs: object) -> ValidationWorktreeCheck:
        """Return a pre-existing dirty-worktree result for this test case."""
        return ValidationWorktreeCheck(
            clean=False,
            reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
            message="dirty file prevents validation",
        )

    monkeypatch.setattr(
        executor_execution_validation,
        "_profile_for_workspace",
        lambda *_args, **_kwargs: profile,
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "_sync_resolved_profile",
        _sync_resolved_profile,
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "check_validation_worktree_clean",
        _check_worktree_clean,
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
        workspace_id="ws_dirty_validation",
        ws=workspace,  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
        compose_project="awf_ws_dirty_validation",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch="awf/ws_dirty_validation",
        adapter=object(),  # type: ignore[arg-type]
        default_model=None,
        baseline_coverage=None,
        planning_validation_handoff=None,
        recovery=None,
        rebase_recovery_result=None,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert result.stop
    assert result.successful_validation_run_id is None
    executor._start_validation_run.assert_awaited_once_with(
        workspace_id="ws_dirty_validation",
        profile=profile,
        base_commit="b" * 40,
        workspace_head_sha="c" * 40,
        target_branch="awf/ws_dirty_validation",
        target_head_sha=None,
        tier=1,
    )
    executor._finish_validation_run.assert_awaited_once()
    finish_run_kwargs = executor._finish_validation_run.await_args.kwargs
    assert finish_run_kwargs["status"] == "failed"
    assert finish_run_kwargs["reason_code"] == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    executor._finish_pending_validate_operations.assert_awaited_once()
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_kwargs["validation_run_id"] == "vr-dirty-worktree"
    assert finish_kwargs["status"] == OperationStatus.failed
    assert finish_kwargs["reason_code"] == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    executor._mark_failed.assert_awaited_once()
    mark_kwargs = executor._mark_failed.await_args.kwargs
    assert getattr(mark_kwargs["from_status"], "value", mark_kwargs["from_status"]) == "validating"
    assert mark_kwargs["failure_reason"] == FailureReason.infrastructure_failure
    assert mark_kwargs["reason_code"] == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
