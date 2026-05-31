"""Validation and fix-cycle phase for WorkspaceExecutor."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from awf.adapters.base import AgentAdapter, AgentRunError
from awf.common.command_evidence import append_command_evidence
from awf.common.commands import CommandResult
from awf.common.compose_exec import (
    EXEC_PROCESS_CLEANUP_FAILED,
    ComposeExecCleanupError,
    cleanup_failure_message,
)
from awf.common.git_identity import (
    git_identity_config_args,
    git_safe_directory_config_args,
)
from awf.control.executor.constants import (
    PLAN_CONFORMANCE_UNSATISFIED,
    POST_VALIDATION_CONFORMANCE_FAILED_REASON_CODE,
    POST_VALIDATION_CONFORMANCE_REPORT_GIT_FAILED_REASON_CODE,
    POST_VALIDATION_CONFORMANCE_REPORT_WRITE_FAILED_REASON_CODE,
)
from awf.control.executor.git_ops import _git_name_lines
from awf.control.executor.helpers import (
    _apply_baseline_coverage_ratchet,
    _failure_reason_for_phase,
    _post_validation_conformance_fix_result,
    _profile_for_workspace,
    _should_run_local_coverage,
    _validation_failure_message,
    _validation_run_coverage_metadata,
    _validation_run_reason_code,
    _validation_tier_for_workspace,
)
from awf.control.executor.quality_gates import (
    _log,
    _post_validation_conformance_agent_failure_details,
    _post_validation_conformance_agent_failure_message,
)
from awf.control.executor.state_ops import _sync_resolved_profile
from awf.control.executor.supply_chain_messages import _supply_chain_block_message
from awf.control.executor.types import (
    _CoverageEvidenceResult,
    _PlanningRunFailure,
    _PlanningValidationHandoff,
    _PostValidationConformanceReportGitError,
    _PostValidationConformanceReportWriteError,
    _RebaseRecoveryResult,
)
from awf.control.quality_gates import (
    PLAN_ONLY_OUTPUT_REASON_CODE,
    find_protected_quality_gate_changes,
    plan_only_output_message,
    quality_gate_violation_message,
)
from awf.control.validation_fix_cycle import (
    ValidationFixContext,
    build_fix_prompt,
    read_output_tail,
)
from awf.db.enums import FailureReason, OperationStatus, WorkspaceStatus
from awf.db.models import Workspace
from awf.runtime.validation import ValidationCoverageResult, profile_phase_command_plan
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    VALIDATION_WORKTREE_STATUS_FAILED,
    ValidationWorktreeCleanup,
    check_validation_worktree_clean,
    cleanup_validation_worktree_side_effects,
    validation_worktree_cleanup_failure_message,
    validation_worktree_preexisting_dirty_message,
)


@dataclass(frozen=True)
class ExecutionValidationResult:
    """Result for the execution validation loop."""

    stop: bool
    successful_validation_run_id: str | None
    successful_validation_workspace_head_sha: str | None
    has_known_non_plan_output: bool


async def _fail_validation_worktree_guard(
    self: Any,
    *,
    workspace_id: str,
    validation_run_id: str | None,
    validation_tier: int,
    reason_code: str,
    message: str,
) -> ExecutionValidationResult:
    """Record and surface a fatal validation-worktree guard failure."""
    failure_message = f"{reason_code}: {message}"
    if validation_run_id is not None:
        await self._finish_validation_run(
            validation_run_id,
            status="failed",
            reason_code=reason_code,
        )
    await self._finish_pending_validate_operations(
        workspace_id=workspace_id,
        status=OperationStatus.failed,
        validation_run_id=validation_run_id,
        requested_tier=validation_tier,
        reason_code=reason_code,
        error_message=failure_message,
    )
    await self._mark_failed(
        workspace_id=workspace_id,
        from_status=WorkspaceStatus.validating,
        failure_reason=FailureReason.infrastructure_failure,
        message=failure_message[:2000],
        reason_code=reason_code,
    )
    return ExecutionValidationResult(
        stop=True,
        successful_validation_run_id=None,
        successful_validation_workspace_head_sha=None,
        has_known_non_plan_output=False,
    )


async def _handle_validation_cleanup_guard(
    self: Any,
    *,
    workspace_id: str,
    validation_run_id: str,
    validation_tier: int,
    successful_validation_run_id: str | None,
    successful_validation_workspace_head_sha: str | None,
    has_known_non_plan_output: bool,
    callback_ignored: bool,
    cleanup_result: ValidationWorktreeCleanup,
) -> ExecutionValidationResult | None:
    """Handle post-validation cleanup failures consistently across handler types."""
    if cleanup_result.ok:
        if callback_ignored:
            return ExecutionValidationResult(
                stop=True,
                successful_validation_run_id=successful_validation_run_id,
                successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                has_known_non_plan_output=has_known_non_plan_output,
            )
        return None

    if callback_ignored:
        _log.warning(
            "executor.validation_cleanup_failed_after_stale_validation_callback",
            workspace_id=workspace_id,
            validation_run_id=validation_run_id,
            reason_code=cleanup_result.reason_code or VALIDATION_WORKTREE_CLEANUP_FAILED,
        )
        return ExecutionValidationResult(
            stop=True,
            successful_validation_run_id=successful_validation_run_id,
            successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
            has_known_non_plan_output=has_known_non_plan_output,
        )

    reason_code = cleanup_result.reason_code or VALIDATION_WORKTREE_CLEANUP_FAILED
    cleanup_message = validation_worktree_cleanup_failure_message(cleanup_result)
    return await _fail_validation_worktree_guard(
        self,
        workspace_id=workspace_id,
        validation_run_id=validation_run_id,
        validation_tier=validation_tier,
        reason_code=reason_code,
        message=cleanup_message,
    )


async def run_validation_and_fix_cycle(
    self: Any,
    *,
    workspace_id: str,
    ws: Workspace,
    worktree_path: Path,
    compose_project: str,
    compose_file: Path,
    base_commit: str,
    expected_branch: str,
    adapter: AgentAdapter,
    run_model: str | None = None,
    default_model: str | None = None,
    baseline_coverage: ValidationCoverageResult | None,
    planning_validation_handoff: _PlanningValidationHandoff | None,
    recovery: Mapping[str, Any] | None,
    rebase_recovery_result: _RebaseRecoveryResult | None,
    has_known_non_plan_output: bool,
    git_in_worktree: Callable[[list[str]], Awaitable[CommandResult]],
) -> ExecutionValidationResult:
    """Run validate/fix attempts and emit the terminal validation state."""
    if run_model is None:
        run_model = default_model

    successful_validation_run_id: str | None = None
    successful_validation_workspace_head_sha: str | None = None

    # ── Step 2: validation (tests + optional Alembic), with fix-cycle ──
    if not await self._transition_if_current(
        workspace_id,
        from_status=WorkspaceStatus.running,
        to=WorkspaceStatus.validating,
        reason="AGENT_RUN_OK",
        action="start_validation",
    ):
        return ExecutionValidationResult(
            stop=True,
            successful_validation_run_id=successful_validation_run_id,
            successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
            has_known_non_plan_output=has_known_non_plan_output,
        )

    max_fix_passes = self._config.max_validation_fix_passes
    profile = _profile_for_workspace(
        ws,
        worktree_path=worktree_path,
        planning_max_iterations_default=self._config.planning_max_iterations_default,
    )
    profile = await _sync_resolved_profile(
        self,
        ws=ws,
        workspace_id=workspace_id,
        profile=profile,
        planning_max_iterations_default=self._config.planning_max_iterations_default,
    )
    validation_commands = [
        step.command.command
        for step in profile_phase_command_plan(profile, ("post_agent", "validate"))
    ]
    test_commands_tuple = tuple(validation_commands)
    validation_tier = _validation_tier_for_workspace(ws, profile)
    if rebase_recovery_result is not None:
        validation_tier = max(validation_tier, 2)
    last_failure_message: str | None = None
    validation_fix_passes_used = 0
    post_validation_conformance_fix_attempts = 0
    post_validation_conformance_fix_pass_budget = (
        max(
            0,
            planning_validation_handoff.max_iterations - planning_validation_handoff.iteration,
        )
        if planning_validation_handoff is not None and recovery is None
        else 0
    )
    max_validation_attempts = max_fix_passes + post_validation_conformance_fix_pass_budget + 1
    for pass_number in range(max_validation_attempts):
        # This loop covers the initial validation plus any validation or
        # post-validation conformance fix prompts. The per-category
        # counters below enforce their separate budgets.
        if not await self._recheck_status(
            workspace_id,
            expected=WorkspaceStatus.validating,
            action="validate",
        ):
            return ExecutionValidationResult(
                stop=True,
                successful_validation_run_id=successful_validation_run_id,
                successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                has_known_non_plan_output=has_known_non_plan_output,
            )
        validation_workspace_head_sha = await self._capture_workspace_head_sha(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
        )
        if validation_workspace_head_sha is None:
            validation_run_id = await self._start_validation_run(
                workspace_id=workspace_id,
                profile=profile,
                base_commit=base_commit,
                workspace_head_sha=validation_workspace_head_sha,
                target_branch=expected_branch,
                target_head_sha=None,
                tier=validation_tier,
            )
            return await _fail_validation_worktree_guard(
                self,
                workspace_id=workspace_id,
                validation_run_id=validation_run_id,
                validation_tier=validation_tier,
                reason_code="VALIDATION_INFRASTRUCTURE_ERROR",
                message="could not capture workspace HEAD before AWF validation",
            )
        validation_run_id = await self._start_validation_run(
            workspace_id=workspace_id,
            profile=profile,
            base_commit=base_commit,
            workspace_head_sha=validation_workspace_head_sha,
            target_branch=expected_branch,
            target_head_sha=None,
            tier=validation_tier,
        )
        pre_validation_check = await check_validation_worktree_clean(
            run_git=git_in_worktree,
            worktree_path=worktree_path,
        )
        if not pre_validation_check.clean:
            reason_code = pre_validation_check.reason_code or VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
            message = (
                pre_validation_check.message
                if reason_code == VALIDATION_WORKTREE_STATUS_FAILED
                else validation_worktree_preexisting_dirty_message(pre_validation_check)
            )
            return await _fail_validation_worktree_guard(
                self,
                workspace_id=workspace_id,
                validation_run_id=None,
                validation_tier=validation_tier,
                reason_code=reason_code,
                message=message,
            )
        run_local_coverage = _should_run_local_coverage(profile)
        coverage_evidence = _CoverageEvidenceResult(coverage=None)
        try:
            await self._update_subphase(workspace_id, "validation")
            val_result = await self._validation.run_profile_phases(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                profile=profile,
                phase_names=("post_agent", "validate"),
                run_healthchecks=True,
                worktree_path=worktree_path,
                include_coverage=False,
            )
            if run_local_coverage and val_result.all_passed:
                coverage_evidence = await self._run_final_coverage_gate(
                    workspace_id=workspace_id,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    profile=profile,
                    validation_tier=validation_tier,
                    workspace_head_sha=validation_workspace_head_sha,
                )
                val_result = replace(val_result, coverage=coverage_evidence.coverage)
        except ComposeExecCleanupError as exc:
            message = cleanup_failure_message(exc)
            _log.error(
                "executor.validation_cleanup_failed",
                workspace_id=workspace_id,
                validation_run_id=validation_run_id,
                source=exc.source,
                label=exc.label,
                invocation_id=exc.invocation_id,
                reason_code=exc.reason_code,
            )
            callback_ignored = await self._finish_validation_callback_if_terminal(
                workspace_id=workspace_id,
                validation_run_id=validation_run_id,
                requested_tier=validation_tier,
            )
            cleanup_result = await cleanup_validation_worktree_side_effects(
                run_git=git_in_worktree,
                worktree_path=worktree_path,
                restore_ref=validation_workspace_head_sha,
            )
            if (
                cleanup_guard_result := await _handle_validation_cleanup_guard(
                    self,
                    workspace_id=workspace_id,
                    validation_run_id=validation_run_id,
                    validation_tier=validation_tier,
                    successful_validation_run_id=successful_validation_run_id,
                    successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                    has_known_non_plan_output=has_known_non_plan_output,
                    callback_ignored=callback_ignored,
                    cleanup_result=cleanup_result,
                )
            ) is not None:
                return cleanup_guard_result
            await self._finish_validation_run(
                validation_run_id,
                status="failed",
                reason_code=EXEC_PROCESS_CLEANUP_FAILED,
            )
            await self._finish_pending_validate_operations(
                workspace_id=workspace_id,
                status=OperationStatus.failed,
                validation_run_id=validation_run_id,
                requested_tier=validation_tier,
                reason_code=EXEC_PROCESS_CLEANUP_FAILED,
                error_message=message,
            )
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.validating,
                failure_reason=FailureReason.infrastructure_failure,
                message=message,
                reason_code=EXEC_PROCESS_CLEANUP_FAILED,
            )
            return ExecutionValidationResult(
                stop=True,
                successful_validation_run_id=successful_validation_run_id,
                successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                has_known_non_plan_output=has_known_non_plan_output,
            )
        except Exception as exc:
            message = f"unexpected error during validation run: {exc!r}"[:2000]
            _log.exception(
                "executor.validation_run_unexpected_failed",
                workspace_id=workspace_id,
                validation_run_id=validation_run_id,
            )
            callback_ignored = await self._finish_validation_callback_if_terminal(
                workspace_id=workspace_id,
                validation_run_id=validation_run_id,
                requested_tier=validation_tier,
            )
            cleanup_result = await cleanup_validation_worktree_side_effects(
                run_git=git_in_worktree,
                worktree_path=worktree_path,
                restore_ref=validation_workspace_head_sha,
            )
            if (
                cleanup_guard_result := await _handle_validation_cleanup_guard(
                    self,
                    workspace_id=workspace_id,
                    validation_run_id=validation_run_id,
                    validation_tier=validation_tier,
                    successful_validation_run_id=successful_validation_run_id,
                    successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                    has_known_non_plan_output=has_known_non_plan_output,
                    callback_ignored=callback_ignored,
                    cleanup_result=cleanup_result,
                )
            ) is not None:
                return cleanup_guard_result
            await self._finish_validation_run(
                validation_run_id,
                status="failed",
                reason_code="VALIDATION_INFRASTRUCTURE_ERROR",
            )
            await self._finish_pending_validate_operations(
                workspace_id=workspace_id,
                status=OperationStatus.failed,
                validation_run_id=validation_run_id,
                requested_tier=validation_tier,
                reason_code="VALIDATION_INFRASTRUCTURE_ERROR",
                error_message=message,
            )
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.validating,
                failure_reason=FailureReason.infrastructure_failure,
                message=message,
                reason_code="VALIDATION_INFRASTRUCTURE_ERROR",
            )
            return ExecutionValidationResult(
                stop=True,
                successful_validation_run_id=successful_validation_run_id,
                successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                has_known_non_plan_output=has_known_non_plan_output,
            )
        cleanup_result = await cleanup_validation_worktree_side_effects(
            run_git=git_in_worktree,
            worktree_path=worktree_path,
            restore_ref=validation_workspace_head_sha,
        )
        if not cleanup_result.ok:
            if await self._finish_validation_callback_if_terminal(
                workspace_id=workspace_id,
                validation_run_id=validation_run_id,
                requested_tier=validation_tier,
            ):
                _log.warning(
                    "executor.validation_cleanup_failed_after_stale_validation_callback",
                    workspace_id=workspace_id,
                    validation_run_id=validation_run_id,
                    reason_code=cleanup_result.reason_code or VALIDATION_WORKTREE_CLEANUP_FAILED,
                )
                return ExecutionValidationResult(
                    stop=True,
                    successful_validation_run_id=successful_validation_run_id,
                    successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                    has_known_non_plan_output=has_known_non_plan_output,
                )
            reason_code = cleanup_result.reason_code or VALIDATION_WORKTREE_CLEANUP_FAILED
            message = validation_worktree_cleanup_failure_message(cleanup_result)
            return await _fail_validation_worktree_guard(
                self,
                workspace_id=workspace_id,
                validation_run_id=validation_run_id,
                validation_tier=validation_tier,
                reason_code=reason_code,
                message=message,
            )
        if await self._finish_validation_callback_if_terminal(
            workspace_id=workspace_id,
            validation_run_id=validation_run_id,
            requested_tier=validation_tier,
        ):
            return ExecutionValidationResult(
                stop=True,
                successful_validation_run_id=successful_validation_run_id,
                successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                has_known_non_plan_output=has_known_non_plan_output,
            )
        val_result = _apply_baseline_coverage_ratchet(
            val_result,
            baseline_coverage=baseline_coverage,
        )
        validation_coverage = _validation_run_coverage_metadata(
            val_result,
            baseline_coverage=baseline_coverage,
        )
        await self._finish_validation_run(
            validation_run_id,
            status="succeeded" if val_result.all_passed else "failed",
            reason_code=_validation_run_reason_code(val_result),
            retry_count=val_result.total_retries,
            coverage=validation_coverage,
            command_retries=[c.retry_count for c in val_result.commands],
            coverage_evidence_status=coverage_evidence.evidence_status,
            coverage_evidence_reason_code=coverage_evidence.reason_code,
            coverage_evidence_source_run_id=coverage_evidence.source_run_id,
        )
        if val_result.all_passed:
            conformance_failure: _PlanningRunFailure | None = None
            if planning_validation_handoff is not None:
                conformance_handoff = planning_validation_handoff
                try:
                    if post_validation_conformance_fix_attempts:
                        conformance_handoff = replace(
                            planning_validation_handoff,
                            iteration=(
                                planning_validation_handoff.iteration
                                + post_validation_conformance_fix_attempts
                            ),
                        )
                    if recovery is not None:
                        _log.info(
                            "executor.post_validation_conformance_recovery_single_attempt",
                            workspace_id=workspace_id,
                            validation_run_id=validation_run_id,
                            recovery_mode=recovery.get("recovery_mode"),
                            source=recovery.get("source"),
                            max_fix_passes=post_validation_conformance_fix_pass_budget,
                            will_retry=False,
                        )
                    conformance_failure = await self._run_post_validation_conformance_check(
                        adapter=adapter,
                        workspace=ws,
                        profile=profile,
                        compose_project=compose_project,
                        compose_file=compose_file,
                        worktree_path=worktree_path,
                        model=run_model,
                        handoff=conformance_handoff,
                        validation_run_id=validation_run_id,
                    )
                except ComposeExecCleanupError as exc:
                    message = cleanup_failure_message(exc)
                    _log.error(
                        "executor.post_validation_conformance_cleanup_failed",
                        workspace_id=workspace_id,
                        validation_run_id=validation_run_id,
                        source=exc.source,
                        label=exc.label,
                        invocation_id=exc.invocation_id,
                        reason_code=exc.reason_code,
                    )
                    await self._finish_pending_validate_operations(
                        workspace_id=workspace_id,
                        status=OperationStatus.failed,
                        validation_run_id=validation_run_id,
                        requested_tier=validation_tier,
                        reason_code=EXEC_PROCESS_CLEANUP_FAILED,
                        coverage=validation_coverage,
                        error_message=message,
                    )
                    await self._mark_failed(
                        workspace_id=workspace_id,
                        from_status=WorkspaceStatus.validating,
                        failure_reason=FailureReason.infrastructure_failure,
                        message=message,
                        reason_code=EXEC_PROCESS_CLEANUP_FAILED,
                    )
                    return ExecutionValidationResult(
                        stop=True,
                        successful_validation_run_id=successful_validation_run_id,
                        successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                        has_known_non_plan_output=has_known_non_plan_output,
                    )
                except AgentRunError as exc:
                    reason_code = exc.reason_code or "AGENT_CLI_FAILED"
                    message = _post_validation_conformance_agent_failure_message(exc)
                    _log.warning(
                        "executor.post_validation_conformance_agent_failed",
                        workspace_id=workspace_id,
                        validation_run_id=validation_run_id,
                        returncode=exc.result.returncode,
                        reason_code=reason_code,
                    )
                    await self._finish_pending_validate_operations(
                        workspace_id=workspace_id,
                        status=OperationStatus.failed,
                        validation_run_id=validation_run_id,
                        requested_tier=validation_tier,
                        reason_code=reason_code,
                        coverage=validation_coverage,
                        error_message=message,
                    )
                    await self._mark_failed(
                        workspace_id=workspace_id,
                        from_status=WorkspaceStatus.validating,
                        failure_reason=FailureReason.agent_failure,
                        message=message,
                        reason_code=reason_code,
                        details=_post_validation_conformance_agent_failure_details(
                            exc,
                            validation_run_id=validation_run_id,
                        ),
                    )
                    return ExecutionValidationResult(
                        stop=True,
                        successful_validation_run_id=successful_validation_run_id,
                        successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                        has_known_non_plan_output=has_known_non_plan_output,
                    )
                except _PostValidationConformanceReportGitError as exc:
                    reason_code = POST_VALIDATION_CONFORMANCE_REPORT_GIT_FAILED_REASON_CODE
                    message = str(exc)
                    _log.error(
                        "executor.post_validation_conformance_report_git_failed",
                        workspace_id=workspace_id,
                        validation_run_id=validation_run_id,
                        operation=exc.operation,
                        returncode=exc.returncode,
                        command_reason_code=exc.command_reason_code,
                        reason_code=reason_code,
                    )
                    await self._finish_pending_validate_operations(
                        workspace_id=workspace_id,
                        status=OperationStatus.failed,
                        validation_run_id=validation_run_id,
                        requested_tier=validation_tier,
                        reason_code=reason_code,
                        coverage=validation_coverage,
                        error_message=message,
                    )
                    failure_details: dict[str, Any] = {
                        "validation_run_id": validation_run_id,
                        "report_path": conformance_handoff.report_path.as_posix(),
                        "operation": exc.operation,
                        "returncode": exc.returncode,
                    }
                    if exc.command_reason_code is not None:
                        failure_details["command_reason_code"] = exc.command_reason_code
                    if exc.cleanup_operation is not None:
                        failure_details["cleanup_operation"] = exc.cleanup_operation
                        failure_details["cleanup_returncode"] = exc.cleanup_returncode
                        failure_details["report_left_staged"] = True
                    if exc.cleanup_command_reason_code is not None:
                        failure_details["cleanup_command_reason_code"] = (
                            exc.cleanup_command_reason_code
                        )
                    await self._mark_failed(
                        workspace_id=workspace_id,
                        from_status=WorkspaceStatus.validating,
                        failure_reason=FailureReason.infrastructure_failure,
                        message=message,
                        reason_code=reason_code,
                        details=failure_details,
                    )
                    return ExecutionValidationResult(
                        stop=True,
                        successful_validation_run_id=successful_validation_run_id,
                        successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                        has_known_non_plan_output=has_known_non_plan_output,
                    )
                except _PostValidationConformanceReportWriteError as exc:
                    reason_code = POST_VALIDATION_CONFORMANCE_REPORT_WRITE_FAILED_REASON_CODE
                    message = str(exc)
                    _log.error(
                        "executor.post_validation_conformance_report_write_failed",
                        workspace_id=workspace_id,
                        validation_run_id=validation_run_id,
                        report_path=exc.report_path.as_posix(),
                        error_type=exc.error_type,
                        errno=exc.errno,
                        reason_code=reason_code,
                    )
                    await self._finish_pending_validate_operations(
                        workspace_id=workspace_id,
                        status=OperationStatus.failed,
                        validation_run_id=validation_run_id,
                        requested_tier=validation_tier,
                        reason_code=reason_code,
                        coverage=validation_coverage,
                        error_message=message,
                    )
                    write_failure_details: dict[str, Any] = {
                        "validation_run_id": validation_run_id,
                        "report_path": exc.report_path.as_posix(),
                        "operation": "write",
                        "error_type": exc.error_type,
                    }
                    if exc.errno is not None:
                        write_failure_details["errno"] = exc.errno
                    await self._mark_failed(
                        workspace_id=workspace_id,
                        from_status=WorkspaceStatus.validating,
                        failure_reason=FailureReason.infrastructure_failure,
                        message=message,
                        reason_code=reason_code,
                        details=write_failure_details,
                    )
                    return ExecutionValidationResult(
                        stop=True,
                        successful_validation_run_id=successful_validation_run_id,
                        successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                        has_known_non_plan_output=has_known_non_plan_output,
                    )
                except Exception as exc:
                    reason_code = POST_VALIDATION_CONFORMANCE_FAILED_REASON_CODE
                    message = (f"post-validation conformance check failed: {exc!r}")[:2000]
                    _log.exception(
                        "executor.post_validation_conformance_unexpected_failed",
                        workspace_id=workspace_id,
                        validation_run_id=validation_run_id,
                        reason_code=reason_code,
                    )
                    await self._finish_pending_validate_operations(
                        workspace_id=workspace_id,
                        status=OperationStatus.failed,
                        validation_run_id=validation_run_id,
                        requested_tier=validation_tier,
                        reason_code=reason_code,
                        coverage=validation_coverage,
                        error_message=message,
                    )
                    await self._mark_failed(
                        workspace_id=workspace_id,
                        from_status=WorkspaceStatus.validating,
                        failure_reason=FailureReason.infrastructure_failure,
                        message=message,
                        reason_code=reason_code,
                    )
                    return ExecutionValidationResult(
                        stop=True,
                        successful_validation_run_id=successful_validation_run_id,
                        successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                        has_known_non_plan_output=has_known_non_plan_output,
                    )
                if conformance_failure is not None:
                    remaining_conformance_iterations = max(
                        0,
                        conformance_handoff.max_iterations - conformance_handoff.iteration,
                    )
                    # Recovery skips feature execution; retrying this
                    # conformance miss would only rerun validation.
                    if recovery is not None or remaining_conformance_iterations <= 0:
                        await self._finish_pending_validate_operations(
                            workspace_id=workspace_id,
                            status=OperationStatus.failed,
                            validation_run_id=validation_run_id,
                            requested_tier=validation_tier,
                            reason_code=conformance_failure.reason_code
                            or PLAN_CONFORMANCE_UNSATISFIED,
                            coverage=validation_coverage,
                            error_message=conformance_failure.message,
                        )
                        await self._mark_failed(
                            workspace_id=workspace_id,
                            from_status=WorkspaceStatus.validating,
                            failure_reason=FailureReason.agent_failure,
                            message=conformance_failure.message[:2000],
                            reason_code=conformance_failure.reason_code,
                            details=conformance_failure.details,
                        )
                        return ExecutionValidationResult(
                            stop=True,
                            successful_validation_run_id=successful_validation_run_id,
                            successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                            has_known_non_plan_output=has_known_non_plan_output,
                        )
                    _log.info(
                        "executor.post_validation_conformance_needs_fix_pass",
                        workspace_id=workspace_id,
                        validation_run_id=validation_run_id,
                        fix_pass=post_validation_conformance_fix_attempts + 1,
                        max_fix_passes=post_validation_conformance_fix_pass_budget,
                        validation_fix_passes_used=validation_fix_passes_used,
                        remaining_conformance_iterations=remaining_conformance_iterations,
                        reason_code=(
                            conformance_failure.reason_code or PLAN_CONFORMANCE_UNSATISFIED
                        ),
                    )
                    post_validation_conformance_fix_attempts += 1
                    val_result = _post_validation_conformance_fix_result(
                        failure=conformance_failure,
                        workspace_id=workspace_id,
                        artifacts_root=self._config.compose_projects_root,
                        attempt=post_validation_conformance_fix_attempts,
                    )
            if conformance_failure is None:
                successful_validation_run_id = validation_run_id
                successful_validation_workspace_head_sha = validation_workspace_head_sha
                if recovery is not None and ws.pr_url and planning_validation_handoff is not None:
                    post_conformance_head_sha = await self._capture_workspace_head_sha(
                        workspace_id=workspace_id,
                        worktree_path=worktree_path,
                    )
                    if post_conformance_head_sha:
                        successful_validation_workspace_head_sha = post_conformance_head_sha
                await self._finish_pending_validate_operations(
                    workspace_id=workspace_id,
                    status=OperationStatus.succeeded,
                    validation_run_id=validation_run_id,
                    requested_tier=validation_tier,
                    reason_code="VALIDATION_OK",
                    coverage=validation_coverage,
                )
                if validation_fix_passes_used or post_validation_conformance_fix_attempts:
                    _log.info(
                        "executor.validation_recovered",
                        workspace_id=workspace_id,
                        fix_passes_used=validation_fix_passes_used,
                        post_validation_conformance_fix_attempts=(
                            post_validation_conformance_fix_attempts
                        ),
                    )
                break

        first_fail = val_result.first_failure
        is_post_validation_conformance_fix_pass = (
            first_fail is not None
            and first_fail.phase == "conformance"
            and first_fail.command == "post-validation plan conformance"
        )
        _log.info(
            "executor.validation_failed",
            workspace_id=workspace_id,
            failed_command=first_fail.command if first_fail else None,
            fix_pass=pass_number,
            max_fix_passes=max_fix_passes,
            validation_fix_passes_used=validation_fix_passes_used,
            post_validation_conformance_fix_attempts=(post_validation_conformance_fix_attempts),
        )
        last_failure_message = _validation_failure_message(
            val_result,
            baseline_coverage=baseline_coverage,
        )

        if first_fail is None or (
            not is_post_validation_conformance_fix_pass
            and validation_fix_passes_used >= max_fix_passes
        ):
            # Exhausted our budget (or no failure details to anchor a
            # fix prompt on) — mark failed and let the operator triage.
            # If a post-validation conformance fix already consumed a
            # prior successful run, this terminal path intentionally
            # reports coverage from the fresh failing validation result.
            await self._finish_pending_validate_operations(
                workspace_id=workspace_id,
                status=OperationStatus.failed,
                validation_run_id=validation_run_id,
                requested_tier=validation_tier,
                reason_code=_validation_run_reason_code(val_result),
                coverage=_validation_run_coverage_metadata(
                    val_result,
                    baseline_coverage=baseline_coverage,
                ),
                error_message=last_failure_message,
            )
            if first_fail is not None and first_fail.phase == "healthcheck":
                await self._record_health_check_failed_event(
                    workspace_id=workspace_id,
                    failure=first_fail,
                )
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.validating,
                failure_reason=_failure_reason_for_phase(first_fail),
                message=(
                    last_failure_message
                    + (f" (after {max_fix_passes} fix attempts)" if max_fix_passes > 0 else "")
                )[:2000],
                reason_code=_validation_run_reason_code(val_result),
            )
            return ExecutionValidationResult(
                stop=True,
                successful_validation_run_id=successful_validation_run_id,
                successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                has_known_non_plan_output=has_known_non_plan_output,
            )

        # Fire a fix pass: re-invoke the coding CLI with the failure
        # context, then re-commit whatever it changed.
        if is_post_validation_conformance_fix_pass:
            fix_pass_number = max(1, post_validation_conformance_fix_attempts)
            fix_pass_total_passes = max(1, post_validation_conformance_fix_pass_budget)
            fix_pass_kind = "post-validation conformance"
        else:
            fix_pass_number = validation_fix_passes_used + 1
            fix_pass_total_passes = max_fix_passes
            fix_pass_kind = "validation"
        fix_context = ValidationFixContext(
            failed_command=first_fail.command,
            returncode=first_fail.returncode,
            stdout_tail=read_output_tail(first_fail.stdout_path),
            stderr_tail=read_output_tail(first_fail.stderr_path),
            pass_number=fix_pass_number,
            total_passes=fix_pass_total_passes,
            test_commands=test_commands_tuple,
            reason_code=_validation_run_reason_code(val_result),
            coverage_percent=val_result.coverage.percent if val_result.coverage else None,
            coverage_minimum_percent=(
                val_result.coverage.minimum_percent if val_result.coverage else None
            ),
            baseline_coverage_percent=(
                baseline_coverage.percent if baseline_coverage is not None else None
            ),
            failing_test_node_ids=(
                tuple(val_result.coverage.failing_test_node_ids)
                if val_result.coverage is not None
                else ()
            ),
            failing_test_evidence=(
                tuple(val_result.coverage.failing_test_evidence)
                if val_result.coverage is not None
                else ()
            ),
        )
        fix_prompt = build_fix_prompt(fix_context)
        _log.info(
            "executor.fix_pass_start",
            workspace_id=workspace_id,
            pass_number=fix_pass_number,
            max_fix_passes=fix_pass_total_passes,
            fix_pass_kind=fix_pass_kind,
            failed_command=first_fail.command,
        )
        if not await self._recheck_status(
            workspace_id,
            expected=WorkspaceStatus.validating,
            action="validation_fix_agent_run",
        ):
            return ExecutionValidationResult(
                stop=True,
                successful_validation_run_id=successful_validation_run_id,
                successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                has_known_non_plan_output=has_known_non_plan_output,
            )
        if not await self._ensure_worktree_available(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            expected=WorkspaceStatus.validating,
            action="validation_fix_agent_run",
            validation_run_id=validation_run_id,
            requested_tier=validation_tier,
        ):
            return ExecutionValidationResult(
                stop=True,
                successful_validation_run_id=successful_validation_run_id,
                successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                has_known_non_plan_output=has_known_non_plan_output,
            )
        fix_command_evidence: list[str] = []
        try:
            fix_result = await adapter.run(
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=fix_prompt,
                model=run_model,
                workspace_id=workspace_id,
            )
            append_command_evidence(
                fix_command_evidence,
                stdout=fix_result.stdout,
                stderr=fix_result.stderr,
            )
        except ComposeExecCleanupError as exc:
            message = cleanup_failure_message(exc)
            _log.error(
                "executor.fix_pass_cleanup_failed",
                workspace_id=workspace_id,
                pass_number=fix_pass_number,
                fix_pass_kind=fix_pass_kind,
                source=exc.source,
                label=exc.label,
                invocation_id=exc.invocation_id,
                reason_code=exc.reason_code,
            )
            await self._finish_pending_validate_operations(
                workspace_id=workspace_id,
                status=OperationStatus.failed,
                validation_run_id=validation_run_id,
                requested_tier=validation_tier,
                reason_code=EXEC_PROCESS_CLEANUP_FAILED,
                coverage=_validation_run_coverage_metadata(
                    val_result,
                    baseline_coverage=baseline_coverage,
                ),
                error_message=message,
            )
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.validating,
                failure_reason=FailureReason.infrastructure_failure,
                message=message,
                reason_code=EXEC_PROCESS_CLEANUP_FAILED,
            )
            return ExecutionValidationResult(
                stop=True,
                successful_validation_run_id=successful_validation_run_id,
                successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                has_known_non_plan_output=has_known_non_plan_output,
            )
        except AgentRunError as exc:
            append_command_evidence(
                fix_command_evidence,
                stdout=exc.result.stdout,
                stderr=exc.result.stderr,
            )
            # Coding CLI exited non-zero on the fix pass. Mirrors the
            # initial-run behaviour: log, remember the note, fall
            # through to commit any salvaged work, then continue the
            # loop (next validation will tell us if it's pushable).
            # Initial no-work provider failures are handled by the
            # post-agent failure path. Fix-pass provider errors keep
            # the validation salvage flow so review/fix recovery
            # remains owned by the PR-monitor path.
            _log.warning(
                "executor.fix_pass_agent_nonzero_exit",
                workspace_id=workspace_id,
                pass_number=fix_pass_number,
                fix_pass_kind=fix_pass_kind,
                returncode=exc.result.returncode,
                reason_code=exc.reason_code,
            )

        if not is_post_validation_conformance_fix_pass:
            validation_fix_passes_used += 1

        if not await self._recheck_status(
            workspace_id,
            expected=WorkspaceStatus.validating,
            action="validation_fix_commit",
        ):
            return ExecutionValidationResult(
                stop=True,
                successful_validation_run_id=successful_validation_run_id,
                successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                has_known_non_plan_output=has_known_non_plan_output,
            )
        if not await self._ensure_worktree_available(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            expected=WorkspaceStatus.validating,
            action="validation_fix_git_add",
            validation_run_id=validation_run_id,
            requested_tier=validation_tier,
        ):
            return ExecutionValidationResult(
                stop=True,
                successful_validation_run_id=successful_validation_run_id,
                successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                has_known_non_plan_output=has_known_non_plan_output,
            )

        async def _fail_fix_pass_git_command(
            *,
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
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.validating,
                failure_reason=FailureReason.infrastructure_failure,
                message=message,
                reason_code=reason_code,
                details=details,
            )

        # Commit whatever the fix pass produced. Simpler than the initial
        # post-agent commit block — orphan-history recovery isn't possible
        # here (HEAD already descends from base after the initial run
        # succeeded); zero-change fix passes are allowed.
        fix_add = await git_in_worktree(["add", "-A"])
        await self._repair_agent_git_ownership(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            reason="validation_fix_git_add",
        )
        if not fix_add.ok:
            _log.warning(
                "executor.fix_pass_add_failed",
                workspace_id=workspace_id,
                stderr=fix_add.stderr[:400],
            )
            await _fail_fix_pass_git_command(
                current_validation_run_id=validation_run_id,
                current_validation_coverage=validation_coverage,
                reason_code="VALIDATION_FIX_GIT_ADD_FAILED",
                operation="git add -A",
                result=fix_add,
            )
            return ExecutionValidationResult(
                stop=True,
                successful_validation_run_id=successful_validation_run_id,
                successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                has_known_non_plan_output=has_known_non_plan_output,
            )
        if not await self._ensure_worktree_available(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            expected=WorkspaceStatus.validating,
            action="validation_fix_git_diff",
            validation_run_id=validation_run_id,
            requested_tier=validation_tier,
        ):
            return ExecutionValidationResult(
                stop=True,
                successful_validation_run_id=successful_validation_run_id,
                successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                has_known_non_plan_output=has_known_non_plan_output,
            )
        fix_cached = await git_in_worktree(["diff", "--cached", "--name-only"])
        if not fix_cached.ok:
            _log.warning(
                "executor.fix_pass_diff_failed",
                workspace_id=workspace_id,
                stderr=fix_cached.stderr[:400],
            )
            await _fail_fix_pass_git_command(
                current_validation_run_id=validation_run_id,
                current_validation_coverage=validation_coverage,
                reason_code="VALIDATION_FIX_GIT_DIFF_FAILED",
                operation="git diff --cached",
                result=fix_cached,
            )
            return ExecutionValidationResult(
                stop=True,
                successful_validation_run_id=successful_validation_run_id,
                successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                has_known_non_plan_output=has_known_non_plan_output,
            )
        fix_staged_paths = _git_name_lines(fix_cached.stdout) if fix_cached.stdout.strip() else []
        supply_chain_result = await self._refresh_supply_chain_policy_for_workspace(
            workspace_id=workspace_id,
            command_evidence=fix_command_evidence,
            changed_paths=fix_staged_paths,
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
            await self._mark_failed(
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
                has_known_non_plan_output=has_known_non_plan_output,
            )
        if fix_staged_paths:
            if await self._fail_if_plan_only_paths(
                workspace_id=workspace_id,
                changed_paths=fix_staged_paths,
                expected_status=WorkspaceStatus.validating,
            ):
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
                    error_message=plan_only_output_message(fix_staged_paths),
                )
                return ExecutionValidationResult(
                    stop=True,
                    successful_validation_run_id=successful_validation_run_id,
                    successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                    has_known_non_plan_output=has_known_non_plan_output,
                )
            has_known_non_plan_output = True
            protected_file_diffs = await self._protected_file_diffs_for_staged_paths(
                worktree_path=worktree_path,
                base_ref=base_commit,
                changed_paths=fix_staged_paths,
                owned_paths=list(ws.owned_paths),
            )
            violations = find_protected_quality_gate_changes(
                changed_paths=fix_staged_paths,
                owned_paths=list(ws.owned_paths),
                protected_file_diffs=protected_file_diffs,
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
                await self._mark_failed(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.validating,
                    failure_reason=FailureReason.policy_failure,
                    reason_code="QUALITY_GATE_POLICY_CHANGED",
                    message=message[:2000],
                )
                return ExecutionValidationResult(
                    stop=True,
                    successful_validation_run_id=successful_validation_run_id,
                    successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                    has_known_non_plan_output=has_known_non_plan_output,
                )
            if not await self._ensure_worktree_available(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                expected=WorkspaceStatus.validating,
                action="validation_fix_git_commit",
                validation_run_id=validation_run_id,
                requested_tier=validation_tier,
            ):
                return ExecutionValidationResult(
                    stop=True,
                    successful_validation_run_id=successful_validation_run_id,
                    successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                    has_known_non_plan_output=has_known_non_plan_output,
                )
            commit_msg = f"awf: fix pass {fix_pass_number} for {ws.task_title}"[:72]
            commit_body = (
                f"AWF {fix_pass_kind} fix pass {fix_pass_number} of "
                f"{fix_pass_total_passes} for workspace {workspace_id} "
                f"(agent: {ws.agent}). Failed command: "
                f"{first_fail.command}."
            )
            fix_commit = await self._runner.run(
                [
                    "git",
                    *git_safe_directory_config_args(worktree_path),
                    "-C",
                    str(worktree_path),
                    *git_identity_config_args(),
                    "commit",
                    "-m",
                    commit_msg,
                    "-m",
                    commit_body,
                ],
            )
            await self._repair_agent_git_ownership(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                reason="validation_fix_git_commit",
            )
            if not fix_commit.ok:
                _log.warning(
                    "executor.fix_pass_commit_failed",
                    workspace_id=workspace_id,
                    stderr=fix_commit.stderr[:400],
                )
                await _fail_fix_pass_git_command(
                    current_validation_run_id=validation_run_id,
                    current_validation_coverage=validation_coverage,
                    reason_code="VALIDATION_FIX_GIT_COMMIT_FAILED",
                    operation="git commit",
                    result=fix_commit,
                )
                return ExecutionValidationResult(
                    stop=True,
                    successful_validation_run_id=successful_validation_run_id,
                    successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                    has_known_non_plan_output=has_known_non_plan_output,
                )
        # Loop back to re-validate.

    return ExecutionValidationResult(
        stop=False,
        successful_validation_run_id=successful_validation_run_id,
        successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
        has_known_non_plan_output=has_known_non_plan_output,
    )
