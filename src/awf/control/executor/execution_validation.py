"""Validation and fix-cycle phase for WorkspaceExecutor."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

from awf.adapters.base import AgentAdapter, AgentRunError, AgentRunResult
from awf.adapters.provider_failures import AGENT_SERVICE_UNHEALTHY
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
from awf.common.task_tag import commit_message_with_task_tag, strip_leading_task_tag
from awf.common.workspace_policy import pr_adoption_is_hosted
from awf.control.executor import planning_artifacts as _planning_artifacts
from awf.control.executor import validation_fix_helpers as _validation_fix_helpers
from awf.control.executor.agent_service_recovery import (
    AGENT_SERVICE_RECOVERY_ABORTED,
    _run_agent_callable_with_service_recovery,
)
from awf.control.executor.constants import (
    _VALIDATE_ONLY_RECOVERY_MODES,
    PLAN_CONFORMANCE_UNSATISFIED,
    POST_VALIDATION_CONFORMANCE_FAILED_REASON_CODE,
    POST_VALIDATION_CONFORMANCE_REPORT_CLEANUP_FAILED_REASON_CODE,
)
from awf.control.executor.git_ops import _git_name_lines
from awf.control.executor.helpers import (
    _apply_baseline_coverage_ratchet,
    _failure_reason_for_phase,
    _hosted_validate_only_validation_phases,
    _is_adapter_retired,
    _post_validation_conformance_fix_result,
    _profile_for_workspace,
    _should_run_local_coverage,
    _validation_failure_message,
    _validation_run_coverage_metadata,
    _validation_run_reason_code,
    _validation_tier_for_workspace,
)
from awf.control.executor.hosted_validation_sync import (
    _hosted_agent_error_terminal_head_sha,
    _sync_hosted_validation_fix_head,
)
from awf.control.executor.quality_gates import (
    _log,
    _post_validation_conformance_agent_failure_details,
    _post_validation_conformance_agent_failure_message,
)
from awf.control.executor.state_ops import _sync_resolved_profile
from awf.control.executor.types import (
    _CoverageEvidenceResult,
    _PlanningRunFailure,
    _PlanningValidationHandoff,
    _RebaseRecoveryResult,
)
from awf.control.executor.validation_cleanup_guards import (
    ExecutionValidationResult,
)
from awf.control.executor.validation_cleanup_guards import (
    fail_validation_worktree_guard as _fail_validation_worktree_guard,
)
from awf.control.executor.validation_cleanup_guards import (
    handle_validation_cleanup_guard as _handle_validation_cleanup_guard,
)
from awf.control.executor.validation_side_effects import _side_effect_failure_result
from awf.control.quality_gates import find_protected_quality_gate_changes
from awf.control.validation_fix_cycle import (
    ValidationFixContext,
    build_fix_prompt,
    read_output_tail,
)
from awf.db.enums import FailureReason, OperationStatus, WorkspaceStatus
from awf.db.models import Workspace
from awf.runtime.hosted_pr_identity import hosted_pr_identity_for_workspace
from awf.runtime.validation import (
    ValidationCoverageResult,
    ValidationResult,
    profile_phase_command_plan,
)
from awf.runtime.validation_worktree import (
    VALIDATION_INFRASTRUCTURE_ERROR,
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    VALIDATION_WORKTREE_STATUS_FAILED,
    check_validation_worktree_clean,
    cleanup_validation_worktree_side_effects,
    validation_worktree_preexisting_dirty_message,
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
    adapter: AgentAdapter | None,
    run_model: str | None = None,
    default_model: str | None = None,
    baseline_coverage: ValidationCoverageResult | None,
    planning_validation_handoff: _PlanningValidationHandoff | None,
    recovery: Mapping[str, Any] | None,
    rebase_recovery_result: _RebaseRecoveryResult | None,
    git_in_worktree: Callable[[list[str]], Awaitable[CommandResult]],
    execution_owner_id: str | None = None,
    resume_disable_fix_passes: bool = False,
    before_agent_retry: Callable[[], Awaitable[bool | str]] | None = None,
    after_agent_cleanup_failure_repair: (
        Callable[[ComposeExecCleanupError], Awaitable[bool | str]] | None
    ) = None,
) -> ExecutionValidationResult:
    """Run validate/fix attempts and emit the terminal validation state.

    Grant-bearing resumes run with zero fix passes to prevent rewriting granted files.
    """
    if run_model is None:
        run_model = default_model

    successful_validation_run_id: str | None = None
    successful_validation_workspace_head_sha: str | None = None

    # ── Step 2: validation (tests + optional Alembic), with fix-cycle ──
    max_fix_passes = (
        0
        if (resume_disable_fix_passes or adapter is None or _is_adapter_retired(adapter))
        else self._config.max_validation_fix_passes
    )
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
    hosted_pr_identity: dict[str, Any] | None = (
        dict(hosted_pr_identity_for_workspace(ws))
        if pr_adoption_is_hosted(getattr(ws, "task_policy", None))
        else None
    )
    hosted_pr_adoption_validate_only_recovery = (
        hosted_pr_identity is not None
        and recovery is not None
        and recovery.get("recovery_mode") in _VALIDATE_ONLY_RECOVERY_MODES
    )
    if hosted_pr_identity is not None and rebase_recovery_result is not None:
        # Rebase recovery has already pushed the hosted PR head; the workspace
        # row may still carry the stale pre-rebase monitor/adoption head.
        hosted_pr_identity = {
            **hosted_pr_identity,
            "expected_head_sha": rebase_recovery_result.head_sha,
        }

    _deposit_planning_artifacts_if_required = partial(
        _planning_artifacts._deposit_validation_planning_artifacts,
        self,
        profile=profile,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
    )

    if not await self._transition_if_current(
        workspace_id,
        from_status=WorkspaceStatus.running,
        to=WorkspaceStatus.validating,
        reason="AGENT_RUN_OK",
        action="start_validation",
    ):
        _deposit_planning_artifacts_if_required()
        return ExecutionValidationResult(
            stop=True,
            successful_validation_run_id=successful_validation_run_id,
            successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
        )

    async def _mark_failed_preserving_planning_artifacts(**mark_kwargs: Any) -> None:
        await _planning_artifacts._mark_failed_preserving_validation_planning_artifacts(
            self,
            artifact_profile=profile,
            artifact_workspace_id=workspace_id,
            artifact_worktree_path=worktree_path,
            **mark_kwargs,
        )

    async def _enter_blocked_preserving_planning_artifacts(**block_kwargs: Any) -> None:
        await _planning_artifacts._enter_blocked_preserving_validation_planning_artifacts(
            self,
            artifact_profile=profile,
            artifact_workspace_id=workspace_id,
            artifact_worktree_path=worktree_path,
            **block_kwargs,
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
        if planning_validation_handoff is not None
        and recovery is None
        and not resume_disable_fix_passes
        else 0
    )
    max_validation_attempts = max_fix_passes + post_validation_conformance_fix_pass_budget + 1
    # Loop exits via break or terminal return; per-category budgets guarantee
    # the final attempt hits one of those paths.
    for pass_number in range(max_validation_attempts):  # pragma: no branch
        if not await self._recheck_status(
            workspace_id,
            expected=WorkspaceStatus.validating,
            action="validate",
        ):
            _deposit_planning_artifacts_if_required()
            return ExecutionValidationResult(
                stop=True,
                successful_validation_run_id=successful_validation_run_id,
                successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
            )
        validation_workspace_head_sha = await self._capture_workspace_head_sha(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
        )
        pre_validation_check = await check_validation_worktree_clean(
            run_git=git_in_worktree,
            worktree_path=worktree_path,
            ignore_all_ignored=True,
            remove_empty_untracked_dirs=True,
        )
        validation_phase_names = _hosted_validate_only_validation_phases(
            hosted_pr_adoption_validate_only_recovery=hosted_pr_adoption_validate_only_recovery,
        )
        validation_run_id = await self._start_validation_run(
            workspace_id=workspace_id,
            profile=profile,
            base_commit=base_commit,
            workspace_head_sha=validation_workspace_head_sha,
            target_branch=expected_branch,
            target_head_sha=None,
            tier=validation_tier,
            phase_names=validation_phase_names,
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
                validation_run_id=validation_run_id,
                validation_tier=validation_tier,
                reason_code=reason_code,
                message=message,
                profile=profile,
                worktree_path=worktree_path,
            )
        if validation_workspace_head_sha is None:
            return await _fail_validation_worktree_guard(
                self,
                workspace_id=workspace_id,
                validation_run_id=validation_run_id,
                validation_tier=validation_tier,
                reason_code=VALIDATION_INFRASTRUCTURE_ERROR,
                message="could not capture workspace HEAD before AWF validation",
                profile=profile,
                worktree_path=worktree_path,
            )
        validation_runner = self._validation
        validation_run_kwargs: dict[str, Any] = {}
        if hosted_pr_identity is not None:
            validation_runner = getattr(self, "_hosted_validation", None)
            if validation_runner is None:
                message = (
                    "hosted PR adoption validation failed: no hosted validation runner configured"
                )
                await self._finish_validation_run(
                    validation_run_id,
                    status="failed",
                    reason_code=VALIDATION_INFRASTRUCTURE_ERROR,
                )
                await self._finish_pending_validate_operations(
                    workspace_id=workspace_id,
                    status=OperationStatus.failed,
                    validation_run_id=validation_run_id,
                    requested_tier=validation_tier,
                    reason_code=VALIDATION_INFRASTRUCTURE_ERROR,
                    error_message=message,
                )
                await _mark_failed_preserving_planning_artifacts(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.validating,
                    failure_reason=FailureReason.infrastructure_failure,
                    message=message,
                    reason_code=VALIDATION_INFRASTRUCTURE_ERROR,
                )
                return ExecutionValidationResult(
                    stop=True,
                    successful_validation_run_id=successful_validation_run_id,
                    successful_validation_workspace_head_sha=(
                        successful_validation_workspace_head_sha
                    ),
                )
            validation_run_kwargs["pr_identity"] = hosted_pr_identity
        run_local_coverage = _should_run_local_coverage(profile)
        coverage_evidence = _CoverageEvidenceResult(coverage=None)
        try:
            await self._update_subphase(workspace_id, "validation")
            val_result = await validation_runner.run_profile_phases(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                profile=profile,
                phase_names=validation_phase_names,
                run_healthchecks=True,
                worktree_path=worktree_path,
                include_coverage=False,
                **validation_run_kwargs,
            )
            if run_local_coverage and val_result.all_passed:
                coverage_evidence = await self._run_final_coverage_gate(
                    workspace_id=workspace_id,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    profile=profile,
                    validation_tier=validation_tier,
                    workspace_head_sha=validation_workspace_head_sha,
                    phase_names=validation_phase_names,
                    coverage_runner=validation_runner,
                    coverage_run_kwargs={
                        **validation_run_kwargs,
                        "worktree_path": worktree_path,
                    },
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
                    callback_ignored=callback_ignored,
                    cleanup_result=cleanup_result,
                    profile=profile,
                    worktree_path=worktree_path,
                    check_callback_after_cleanup=True,
                )
            ) is not None:
                if cleanup_result.ok:
                    _deposit_planning_artifacts_if_required()
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
            await _mark_failed_preserving_planning_artifacts(
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
                    callback_ignored=callback_ignored,
                    cleanup_result=cleanup_result,
                    profile=profile,
                    worktree_path=worktree_path,
                    check_callback_after_cleanup=True,
                )
            ) is not None:
                if cleanup_result.ok:
                    _deposit_planning_artifacts_if_required()
                return cleanup_guard_result
            await self._finish_validation_run(
                validation_run_id,
                status="failed",
                reason_code=VALIDATION_INFRASTRUCTURE_ERROR,
            )
            await self._finish_pending_validate_operations(
                workspace_id=workspace_id,
                status=OperationStatus.failed,
                validation_run_id=validation_run_id,
                requested_tier=validation_tier,
                reason_code=VALIDATION_INFRASTRUCTURE_ERROR,
                error_message=message,
            )
            await _mark_failed_preserving_planning_artifacts(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.validating,
                failure_reason=FailureReason.infrastructure_failure,
                message=message,
                reason_code=VALIDATION_INFRASTRUCTURE_ERROR,
            )
            return ExecutionValidationResult(
                stop=True,
                successful_validation_run_id=successful_validation_run_id,
                successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
            )
        cleanup_result = await cleanup_validation_worktree_side_effects(
            run_git=git_in_worktree,
            worktree_path=worktree_path,
            restore_ref=validation_workspace_head_sha,
        )
        if not cleanup_result.ok:
            callback_ignored = await self._finish_validation_callback_if_terminal(
                workspace_id=workspace_id,
                validation_run_id=validation_run_id,
                requested_tier=validation_tier,
            )
            # ``_handle_validation_cleanup_guard`` only returns ``None`` when the
            # cleanup result is OK; this call site is already inside
            # ``if not cleanup_result.ok``, so the guard always returns a
            # terminal result here and the ``is None`` fall-through cannot run.
            if (  # pragma: no branch
                cleanup_guard_result := await _handle_validation_cleanup_guard(
                    self,
                    workspace_id=workspace_id,
                    validation_run_id=validation_run_id,
                    validation_tier=validation_tier,
                    successful_validation_run_id=successful_validation_run_id,
                    successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                    callback_ignored=callback_ignored,
                    cleanup_result=cleanup_result,
                    profile=profile,
                    worktree_path=worktree_path,
                    check_callback_after_cleanup=True,
                )
            ) is not None:
                return cleanup_guard_result
        if await self._finish_validation_callback_if_terminal(
            workspace_id=workspace_id,
            validation_run_id=validation_run_id,
            requested_tier=validation_tier,
        ):
            _deposit_planning_artifacts_if_required()
            return ExecutionValidationResult(
                stop=True,
                successful_validation_run_id=successful_validation_run_id,
                successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
            )
        val_result = _apply_baseline_coverage_ratchet(
            val_result,
            baseline_coverage=baseline_coverage,
        )
        cleaned_side_effects = bool(cleanup_result.side_effect_paths)
        if (
            val_result.all_passed
            and cleanup_result.ok
            and (cleaned_side_effects or not cleanup_result.check.clean)
        ):
            val_result = _side_effect_failure_result(
                val_result=val_result,
                cleanup_result=cleanup_result,
                workspace_id=workspace_id,
                validation_run_id=validation_run_id,
                artifacts_root=self._config.compose_projects_root,
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
                if adapter is None or _is_adapter_retired(adapter):
                    conformance_failure = _PlanningRunFailure(
                        message="post-validation conformance check failed: agent adapter is unavailable",
                        reason_code=POST_VALIDATION_CONFORMANCE_FAILED_REASON_CODE,
                    )
                else:
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
                        conformance_scope_baseline = (
                            await self._capture_post_validation_conformance_scope_baseline(
                                worktree_path,
                                conformance_handoff.report_path,
                            )
                        )

                        async def _run_conformance_agent(
                            _accept_existing_plan: bool,
                            *,
                            _handoff: _PlanningValidationHandoff = conformance_handoff,
                            _validation_run_id: str = validation_run_id,
                            _hosted_pr_identity: dict[str, Any] | None = hosted_pr_identity,
                            _conformance_scope_baseline: Any = conformance_scope_baseline,
                        ) -> Any:
                            return await self._run_post_validation_conformance_check(
                                adapter=adapter,
                                workspace=ws,
                                profile=profile,
                                compose_project=compose_project,
                                compose_file=compose_file,
                                worktree_path=worktree_path,
                                model=run_model,
                                handoff=_handoff,
                                validation_run_id=_validation_run_id,
                                base_commit=base_commit,
                                hosted_pr_identity=_hosted_pr_identity,
                                conformance_scope_baseline=_conformance_scope_baseline,
                                require_hosted_terminal_head=(
                                    not hosted_pr_adoption_validate_only_recovery
                                ),
                            )

                        async def _finish_conformance_recovery_failure(
                            *,
                            reason_code: str = AGENT_SERVICE_RECOVERY_ABORTED,
                            details: Mapping[str, Any] | None = None,
                            _validation_run_id: str = validation_run_id,
                            _validation_coverage: dict[str, object] | None = validation_coverage,
                        ) -> None:
                            message = (
                                "agent compose service recovery failed during "
                                "post-validation conformance"
                            )
                            await self._finish_pending_validate_operations(
                                workspace_id=workspace_id,
                                status=OperationStatus.failed,
                                validation_run_id=_validation_run_id,
                                requested_tier=validation_tier,
                                reason_code=reason_code,
                                coverage=_validation_coverage,
                                error_message=message,
                            )
                            await _mark_failed_preserving_planning_artifacts(
                                workspace_id=workspace_id,
                                from_status=WorkspaceStatus.validating,
                                failure_reason=FailureReason.infrastructure_failure,
                                message=message,
                                reason_code=reason_code,
                                details=details,
                            )

                        (
                            conformance_recovered,
                            conformance_failure,
                        ) = await _run_agent_callable_with_service_recovery(
                            self,
                            run_agent=_run_conformance_agent,
                            adapter=adapter,
                            workspace=ws,
                            profile=profile,
                            compose_project=compose_project,
                            compose_file=compose_file,
                            model=run_model,
                            command_evidence=[],
                            workspace_id=workspace_id,
                            execution_owner_id=execution_owner_id,
                            before_mark_failed=_finish_conformance_recovery_failure,
                            before_mark_failed_marks_workspace=True,
                            before_agent_retry=before_agent_retry,
                            after_agent_cleanup_failure_repair=after_agent_cleanup_failure_repair,
                            expected_status=WorkspaceStatus.validating,
                            failure_from_status=WorkspaceStatus.validating,
                        )
                        if not conformance_recovered:
                            return ExecutionValidationResult(
                                stop=True,
                                successful_validation_run_id=successful_validation_run_id,
                                successful_validation_workspace_head_sha=(
                                    successful_validation_workspace_head_sha
                                ),
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
                        await _mark_failed_preserving_planning_artifacts(
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
                        await _mark_failed_preserving_planning_artifacts(
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
                        await _mark_failed_preserving_planning_artifacts(
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
                        )
                if conformance_failure is not None:
                    remaining_conformance_iterations = max(
                        0,
                        conformance_handoff.max_iterations - conformance_handoff.iteration,
                    )
                    conformance_report_cleanup_failed = (
                        conformance_failure.reason_code
                        == POST_VALIDATION_CONFORMANCE_REPORT_CLEANUP_FAILED_REASON_CODE
                    )
                    # Recovery skips feature execution; retrying this
                    # conformance miss would only rerun validation.
                    # Grant-bearing resumes (resume_disable_fix_passes) never fire a conformance
                    # fix pass; mark FAILED for operator triage instead.
                    if (
                        conformance_report_cleanup_failed
                        or recovery is not None
                        or remaining_conformance_iterations <= 0
                        or resume_disable_fix_passes
                        or adapter is None
                        or _is_adapter_retired(adapter)
                    ):
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
                        mark_failed_kwargs = {
                            "workspace_id": workspace_id,
                            "from_status": WorkspaceStatus.validating,
                            "failure_reason": (
                                FailureReason.infrastructure_failure
                                if (
                                    conformance_report_cleanup_failed
                                    or adapter is None
                                    or _is_adapter_retired(adapter)
                                )
                                else FailureReason.agent_failure
                            ),
                            "message": conformance_failure.message[:2000],
                            "reason_code": conformance_failure.reason_code,
                            "details": conformance_failure.details,
                        }
                        if conformance_report_cleanup_failed:
                            # ``_run_post_validation_conformance_check`` already
                            # deposited the correct served report before cleanup.
                            # Re-depositing from the dirty worktree here can
                            # overwrite that satisfied report with stale content.
                            await self._mark_failed(**mark_failed_kwargs)
                        else:
                            await _mark_failed_preserving_planning_artifacts(**mark_failed_kwargs)
                        return ExecutionValidationResult(
                            stop=True,
                            successful_validation_run_id=successful_validation_run_id,
                            successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
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
                if planning_validation_handoff is None:
                    # Planning was required but conformance was satisfied inline
                    # (no AWF-validation handoff). The plan/conformance report was
                    # never deposited by _run_post_validation_conformance_check,
                    # so deposit it now while the worktree still exists and before
                    # the terminal success transition makes artifacts refetchable.
                    # Best-effort and idempotent, gated on planning.required.
                    _planning_artifacts._deposit_planning_artifacts_best_effort(
                        self,
                        profile=profile,
                        workspace_id=workspace_id,
                        worktree_path=worktree_path,
                    )
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

        if (
            first_fail is None
            or adapter is None
            or _is_adapter_retired(adapter)
            or (
                not is_post_validation_conformance_fix_pass
                and validation_fix_passes_used >= max_fix_passes
            )
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
            await _mark_failed_preserving_planning_artifacts(
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
            task_tag=ws.task_tag,
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
            _deposit_planning_artifacts_if_required()
            return ExecutionValidationResult(
                stop=True,
                successful_validation_run_id=successful_validation_run_id,
                successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
            )
        if not await self._ensure_worktree_available(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            expected=WorkspaceStatus.validating,
            action="validation_fix_agent_run",
            validation_run_id=validation_run_id,
            requested_tier=validation_tier,
        ):
            _deposit_planning_artifacts_if_required()
            return ExecutionValidationResult(
                stop=True,
                successful_validation_run_id=successful_validation_run_id,
                successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
            )
        fix_command_evidence: list[str] = []
        fix_result: AgentRunResult | None = None
        try:

            async def _run_fix_agent(
                _accept_existing_plan: bool,
                *,
                _fix_prompt: str = fix_prompt,
                _hosted_pr_identity: dict[str, Any] | None = hosted_pr_identity,
            ) -> AgentRunResult:
                if adapter is None or _is_adapter_retired(adapter):
                    raise RuntimeError("No agent adapter available for validation fix pass")
                return await adapter.run(
                    compose_project=compose_project,
                    compose_file=compose_file,
                    prompt=_fix_prompt,
                    model=run_model,
                    workspace_id=workspace_id,
                    hosted_pr_identity=_hosted_pr_identity,
                    profile=profile,
                    worktree_path=worktree_path,
                )

            async def _finish_fix_recovery_failure(
                *,
                reason_code: str = AGENT_SERVICE_UNHEALTHY,
                details: Mapping[str, Any] | None = None,
                _validation_run_id: str = validation_run_id,
                _val_result: ValidationResult = val_result,
            ) -> None:
                message = "agent compose service recovery failed during validation fix pass"
                await self._finish_pending_validate_operations(
                    workspace_id=workspace_id,
                    status=OperationStatus.failed,
                    validation_run_id=_validation_run_id,
                    requested_tier=validation_tier,
                    reason_code=reason_code,
                    coverage=_validation_run_coverage_metadata(
                        _val_result,
                        baseline_coverage=baseline_coverage,
                    ),
                    error_message=message,
                )
                await _mark_failed_preserving_planning_artifacts(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.validating,
                    failure_reason=FailureReason.infrastructure_failure,
                    message=message,
                    reason_code=reason_code,
                    details=details,
                )

            fix_recovered, fix_result = await _run_agent_callable_with_service_recovery(
                self,
                run_agent=_run_fix_agent,
                adapter=adapter,
                workspace=ws,
                profile=profile,
                compose_project=compose_project,
                compose_file=compose_file,
                model=run_model,
                command_evidence=fix_command_evidence,
                workspace_id=workspace_id,
                execution_owner_id=execution_owner_id,
                before_mark_failed=_finish_fix_recovery_failure,
                before_mark_failed_marks_workspace=True,
                before_agent_retry=before_agent_retry,
                after_agent_cleanup_failure_repair=after_agent_cleanup_failure_repair,
                expected_status=WorkspaceStatus.validating,
                failure_from_status=WorkspaceStatus.validating,
            )
            if not fix_recovered:
                return ExecutionValidationResult(
                    stop=True,
                    successful_validation_run_id=successful_validation_run_id,
                    successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
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
            await _mark_failed_preserving_planning_artifacts(
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
            )
        except AgentRunError as exc:
            append_command_evidence(
                fix_command_evidence,
                stdout=exc.result.stdout,
                stderr=exc.result.stderr,
            )
            if getattr(adapter, "is_hosted", False):
                fix_result = AgentRunResult(
                    returncode=exc.result.returncode,
                    stdout=exc.result.stdout,
                    stderr=exc.result.stderr,
                    terminal_head_sha=_hosted_agent_error_terminal_head_sha(exc),
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
            _deposit_planning_artifacts_if_required()
            return ExecutionValidationResult(
                stop=True,
                successful_validation_run_id=successful_validation_run_id,
                successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
            )
        if not await self._ensure_worktree_available(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            expected=WorkspaceStatus.validating,
            action="validation_fix_git_add",
            validation_run_id=validation_run_id,
            requested_tier=validation_tier,
        ):
            _deposit_planning_artifacts_if_required()
            return ExecutionValidationResult(
                stop=True,
                successful_validation_run_id=successful_validation_run_id,
                successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
            )

        _fail_fix_pass_git_command = partial(
            _validation_fix_helpers.fail_fix_pass_git_command,
            self,
            workspace_id=workspace_id,
            validation_tier=validation_tier,
            mark_failed_preserving_planning_artifacts=(_mark_failed_preserving_planning_artifacts),
        )
        _check_post_fix_worktree_clean = partial(
            _validation_fix_helpers.check_post_fix_worktree_clean,
            self,
            workspace_id=workspace_id,
            validation_tier=validation_tier,
            git_in_worktree=git_in_worktree,
            worktree_path=worktree_path,
            profile=profile,
        )
        _gate_fix_changed_paths = partial(
            _validation_fix_helpers.gate_fix_changed_paths,
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            ws=ws,
            profile=profile,
            fix_command_evidence=fix_command_evidence,
            validation_run_id=validation_run_id,
            val_result=val_result,
            validation_tier=validation_tier,
            baseline_coverage=baseline_coverage,
            successful_validation_run_id=successful_validation_run_id,
            successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
            find_protected_quality_gate_changes_fn=find_protected_quality_gate_changes,
            mark_failed_preserving_planning_artifacts=(_mark_failed_preserving_planning_artifacts),
            enter_blocked_preserving_planning_artifacts=(
                _enter_blocked_preserving_planning_artifacts
            ),
            execution_owner_id=execution_owner_id,
        )

        if getattr(adapter, "is_hosted", False) and fix_result is not None:
            if not fix_result.terminal_head_sha:
                await _fail_fix_pass_git_command(
                    current_validation_run_id=validation_run_id,
                    current_validation_coverage=validation_coverage,
                    reason_code="HOSTED_REMOTE_HEAD_MISSING",
                    operation="hosted terminal head sync",
                    result=CommandResult(
                        returncode=1,
                        stdout=fix_result.stdout,
                        stderr="hosted validation fix completed without terminal_head_sha",
                        reason_code="HOSTED_REMOTE_HEAD_MISSING",
                    ),
                )
                return ExecutionValidationResult(
                    stop=True,
                    successful_validation_run_id=successful_validation_run_id,
                    successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                )
            sync_result = await _sync_hosted_validation_fix_head(
                self,
                worktree_path=worktree_path,
                hosted_pr_identity=hosted_pr_identity,
                terminal_head_sha=fix_result.terminal_head_sha,
            )
            if not sync_result.ok:
                await _fail_fix_pass_git_command(
                    current_validation_run_id=validation_run_id,
                    current_validation_coverage=validation_coverage,
                    reason_code=sync_result.reason_code or "HOSTED_REMOTE_HEAD_SYNC_FAILED",
                    operation="hosted terminal head sync",
                    result=sync_result,
                )
                return ExecutionValidationResult(
                    stop=True,
                    successful_validation_run_id=successful_validation_run_id,
                    successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                )
            if hosted_pr_identity is not None:
                hosted_pr_identity = {
                    **hosted_pr_identity,
                    "expected_head_sha": sync_result.stdout.strip(),
                }
            if not await self._ensure_worktree_available(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                expected=WorkspaceStatus.validating,
                action="validation_fix_git_diff",
                validation_run_id=validation_run_id,
                requested_tier=validation_tier,
            ):
                _deposit_planning_artifacts_if_required()
                return ExecutionValidationResult(
                    stop=True,
                    successful_validation_run_id=successful_validation_run_id,
                    successful_validation_workspace_head_sha=(
                        successful_validation_workspace_head_sha
                    ),
                )
            hosted_terminal_head_sha = sync_result.stdout.strip()
            hosted_fix_diff = await git_in_worktree(
                [
                    "diff",
                    "--name-only",
                    f"{validation_workspace_head_sha}..{hosted_terminal_head_sha}",
                ]
            )
            if not hosted_fix_diff.ok:
                _log.warning(
                    "executor.hosted_fix_pass_diff_failed",
                    workspace_id=workspace_id,
                    stderr=hosted_fix_diff.stderr[:400],
                )
                await _fail_fix_pass_git_command(
                    current_validation_run_id=validation_run_id,
                    current_validation_coverage=validation_coverage,
                    reason_code="VALIDATION_FIX_GIT_DIFF_FAILED",
                    operation="hosted git diff --name-only",
                    result=hosted_fix_diff,
                )
                return ExecutionValidationResult(
                    stop=True,
                    successful_validation_run_id=successful_validation_run_id,
                    successful_validation_workspace_head_sha=(
                        successful_validation_workspace_head_sha
                    ),
                )
            hosted_fix_paths = (
                _git_name_lines(hosted_fix_diff.stdout) if hosted_fix_diff.stdout.strip() else []
            )
            if (
                gate_result := await _gate_fix_changed_paths(
                    changed_paths=hosted_fix_paths,
                    diff_base_ref=validation_workspace_head_sha,
                )
            ) is not None:
                return gate_result
            if (dirty_result := await _check_post_fix_worktree_clean()) is not None:
                return dirty_result
            continue

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
            )
        if not await self._ensure_worktree_available(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            expected=WorkspaceStatus.validating,
            action="validation_fix_git_diff",
            validation_run_id=validation_run_id,
            requested_tier=validation_tier,
        ):
            _deposit_planning_artifacts_if_required()
            return ExecutionValidationResult(
                stop=True,
                successful_validation_run_id=successful_validation_run_id,
                successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
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
            )
        fix_staged_paths = _git_name_lines(fix_cached.stdout) if fix_cached.stdout.strip() else []
        if (
            gate_result := await _gate_fix_changed_paths(
                changed_paths=fix_staged_paths,
                diff_base_ref=base_commit,
            )
        ) is not None:
            return gate_result
        if fix_staged_paths:
            if not await self._ensure_worktree_available(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                expected=WorkspaceStatus.validating,
                action="validation_fix_git_commit",
                validation_run_id=validation_run_id,
                requested_tier=validation_tier,
            ):
                _deposit_planning_artifacts_if_required()
                return ExecutionValidationResult(
                    stop=True,
                    successful_validation_run_id=successful_validation_run_id,
                    successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                )
            commit_msg = commit_message_with_task_tag(
                f"awf: fix pass {fix_pass_number} for "
                f"{strip_leading_task_tag(ws.task_title, ws.task_tag)}",
                ws.task_tag,
            )[:72]
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
                )

        if (dirty_result := await _check_post_fix_worktree_clean()) is not None:
            return dirty_result

        # Loop back to re-validate.

    return ExecutionValidationResult(
        stop=False,
        successful_validation_run_id=successful_validation_run_id,
        successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
    )
