"""Validation fix-pass git and policy helper functions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from awf.common.commands import CommandResult
from awf.control.executor import planning_artifacts as _planning_artifacts
from awf.control.executor.helpers import _validation_run_coverage_metadata
from awf.control.executor.supply_chain_messages import _supply_chain_block_message
from awf.control.executor.validation_cleanup_guards import (
    ExecutionValidationResult,
)
from awf.control.executor.validation_cleanup_guards import (
    fail_validation_worktree_guard as _fail_validation_worktree_guard,
)
from awf.control.quality_gates import (
    PLAN_ONLY_OUTPUT_REASON_CODE,
    plan_only_output_message,
    quality_gate_violation_message,
)
from awf.db.enums import FailureReason, OperationStatus, WorkspaceStatus
from awf.db.models import Workspace
from awf.profiles.models import WorkspaceProfile
from awf.runtime.validation import ValidationCoverageResult, ValidationResult
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    VALIDATION_WORKTREE_STATUS_FAILED,
    check_validation_worktree_clean,
    validation_worktree_preexisting_dirty_message,
)


async def fail_fix_pass_git_command(
    self: Any,
    *,
    workspace_id: str,
    validation_tier: int,
    mark_failed_preserving_planning_artifacts: Callable[..., Awaitable[None]],
    current_validation_run_id: str,
    current_validation_coverage: dict[str, object] | None,
    reason_code: str,
    operation: str,
    result: CommandResult,
) -> None:
    """Record a validation fix-pass git failure and mark workspace failed."""
    command_output = (result.stderr or result.stdout).strip()
    message = (
        f"validation fix pass {operation} failed with exit {result.returncode}"
        + (f": {command_output}" if command_output else "")
    )[:2000]
    details: dict[str, Any] = {
        "validation_run_id": current_validation_run_id,
        "operation": operation,
        "returncode": result.returncode,
    }
    if result.reason_code is not None:
        details["command_reason_code"] = result.reason_code
    await self._finish_pending_validate_operations(
        workspace_id=workspace_id,
        status=OperationStatus.failed,
        validation_run_id=current_validation_run_id,
        requested_tier=validation_tier,
        reason_code=reason_code,
        coverage=current_validation_coverage,
        error_message=message,
    )
    await mark_failed_preserving_planning_artifacts(
        workspace_id=workspace_id,
        from_status=WorkspaceStatus.validating,
        failure_reason=FailureReason.infrastructure_failure,
        message=message,
        reason_code=reason_code,
        details=details,
    )


async def check_post_fix_worktree_clean(
    self: Any,
    *,
    workspace_id: str,
    validation_tier: int,
    git_in_worktree: Callable[[list[str]], Awaitable[CommandResult]],
    worktree_path: Path,
    profile: WorkspaceProfile,
) -> ExecutionValidationResult | None:
    fix_pass_ignored_check = await check_validation_worktree_clean(
        run_git=git_in_worktree,
        worktree_path=worktree_path,
        ignore_all_ignored=True,
        remove_empty_untracked_dirs=True,
    )
    if fix_pass_ignored_check.clean:
        return None
    reason_code = fix_pass_ignored_check.reason_code or VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    message = (
        fix_pass_ignored_check.message
        if reason_code == VALIDATION_WORKTREE_STATUS_FAILED
        else validation_worktree_preexisting_dirty_message(fix_pass_ignored_check)
    )
    return await _fail_validation_worktree_guard(
        self,
        workspace_id=workspace_id,
        validation_run_id=None,
        validation_tier=validation_tier,
        reason_code=reason_code,
        message=message,
        profile=profile,
        worktree_path=worktree_path,
    )


async def gate_fix_changed_paths(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    ws: Workspace,
    profile: WorkspaceProfile,
    changed_paths: list[str],
    diff_base_ref: str,
    fix_command_evidence: list[str],
    validation_run_id: str,
    val_result: ValidationResult,
    validation_tier: int,
    baseline_coverage: ValidationCoverageResult | None,
    successful_validation_run_id: str | None,
    successful_validation_workspace_head_sha: str | None,
    find_protected_quality_gate_changes_fn: Callable[..., Any],
    mark_failed_preserving_planning_artifacts: Callable[..., Awaitable[None]],
    enter_blocked_preserving_planning_artifacts: Callable[..., Awaitable[None]],
    execution_owner_id: str | None,
) -> ExecutionValidationResult | None:
    supply_chain_result = await self._refresh_supply_chain_policy_for_workspace(
        workspace_id=workspace_id,
        command_evidence=fix_command_evidence,
        changed_paths=changed_paths,
    )
    if supply_chain_result.policy_blocked:
        message = _supply_chain_block_message(supply_chain_result.findings)
        await self._finish_pending_validate_operations(
            workspace_id=workspace_id,
            status=OperationStatus.failed,
            validation_run_id=validation_run_id,
            requested_tier=validation_tier,
            reason_code="SUPPLY_CHAIN_POLICY_BLOCKED",
            coverage=_validation_run_coverage_metadata(
                val_result,
                baseline_coverage=baseline_coverage,
            ),
            error_message=message,
        )
        await mark_failed_preserving_planning_artifacts(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.validating,
            failure_reason=FailureReason.policy_failure,
            reason_code="SUPPLY_CHAIN_POLICY_BLOCKED",
            message=message[:2000],
        )
        return ExecutionValidationResult(
            stop=True,
            successful_validation_run_id=successful_validation_run_id,
            successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
        )
    if not changed_paths:
        return None
    if await self._committed_and_staged_output_is_plan_only(
        worktree_path=worktree_path,
        base_commit=diff_base_ref,
        staged_paths=changed_paths,
    ):
        _planning_artifacts._deposit_planning_artifacts_best_effort(
            self,
            profile=profile,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
        )
        await self._fail_if_plan_only_paths(
            workspace_id=workspace_id,
            changed_paths=changed_paths,
            expected_status=WorkspaceStatus.validating,
        )
        await self._finish_pending_validate_operations(
            workspace_id=workspace_id,
            status=OperationStatus.failed,
            validation_run_id=validation_run_id,
            requested_tier=validation_tier,
            reason_code=PLAN_ONLY_OUTPUT_REASON_CODE,
            coverage=_validation_run_coverage_metadata(
                val_result,
                baseline_coverage=baseline_coverage,
            ),
            error_message=plan_only_output_message(changed_paths),
        )
        return ExecutionValidationResult(
            stop=True,
            successful_validation_run_id=successful_validation_run_id,
            successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
        )
    protected_file_diffs = await self._protected_file_diffs_for_staged_paths(
        worktree_path=worktree_path,
        base_ref=diff_base_ref,
        changed_paths=changed_paths,
        owned_paths=list(ws.owned_paths),
    )
    violations = find_protected_quality_gate_changes_fn(
        changed_paths=changed_paths,
        owned_paths=list(ws.owned_paths),
        protected_file_diffs=protected_file_diffs,
        operator_granted_paths=await self._active_operator_grant_specs(workspace_id),
    )
    if violations:
        message = quality_gate_violation_message(violations)
        await self._finish_pending_validate_operations(
            workspace_id=workspace_id,
            status=OperationStatus.failed,
            validation_run_id=validation_run_id,
            requested_tier=validation_tier,
            reason_code="QUALITY_GATE_POLICY_CHANGED",
            coverage=_validation_run_coverage_metadata(
                val_result,
                baseline_coverage=baseline_coverage,
            ),
            error_message=message,
        )
        await enter_blocked_preserving_planning_artifacts(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.validating,
            violations=violations,
            resume_phase="validation_fix_cycle",
            execution_owner_id=execution_owner_id,
        )
        return ExecutionValidationResult(
            stop=True,
            successful_validation_run_id=successful_validation_run_id,
            successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
        )
    return None
