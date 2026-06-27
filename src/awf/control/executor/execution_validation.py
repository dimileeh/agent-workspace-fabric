"""Validation and fix-cycle phase for WorkspaceExecutor."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
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
from awf.control.executor import planning_artifacts as _planning_artifacts
from awf.control.executor.agent_service_recovery import (
    _run_agent_callable_with_service_recovery,
)
from awf.control.executor.constants import (
    PLAN_CONFORMANCE_UNSATISFIED,
    POST_VALIDATION_CONFORMANCE_FAILED_REASON_CODE,
    POST_VALIDATION_CONFORMANCE_REPORT_CLEANUP_FAILED_REASON_CODE,
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
from awf.runtime.validation import (
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationResult,
    profile_phase_command_plan,
)
from awf.runtime.validation_worktree import (
    VALIDATION_INFRASTRUCTURE_ERROR,
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED,
    VALIDATION_WORKTREE_STATUS_FAILED,
    ValidationWorktreeCleanup,
    check_validation_worktree_clean,
    cleanup_validation_worktree_side_effects,
    validation_worktree_preexisting_dirty_message,
)


def _safe_validation_artifact_name(value: str) -> str:
    """Return a filesystem-safe artifact name for validation evidence."""
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)
    return safe or "validation"


def _cleaned_side_effect_paths(cleanup_result: ValidationWorktreeCleanup) -> tuple[str, ...]:
    """Return stable path evidence for cleaned validation side effects."""
    return cleanup_result.side_effect_paths


def _side_effect_failure_result(
    *,
    val_result: ValidationResult,
    cleanup_result: ValidationWorktreeCleanup,
    workspace_id: str,
    validation_run_id: str,
    artifacts_root: Path,
) -> ValidationResult:
    """Add a synthetic validation failure for cleaned successful side effects."""
    side_effect_paths = _cleaned_side_effect_paths(cleanup_result)
    paths_text = ", ".join(side_effect_paths) if side_effect_paths else "<unknown>"
    artifacts_dir = artifacts_root / workspace_id / "validation_worktree"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    safe_validation_run_id = _safe_validation_artifact_name(validation_run_id)
    stdout_path = artifacts_dir / f"{safe_validation_run_id}.side_effects.stdout"
    stderr_path = artifacts_dir / f"{safe_validation_run_id}.side_effects.stderr"
    stdout_path.write_text(
        (
            "AWF validation commands passed only before validation worktree cleanup "
            "restored or deleted side effects. The restored commit state was not "
            f"validated. Cleaned paths: {paths_text}."
        ),
        encoding="utf-8",
    )
    stderr_path.write_text("", encoding="utf-8")
    command = ValidationCommandResult(
        command="validation worktree side-effect guard",
        returncode=1,
        duration_seconds=0.0,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        reason_code=VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED,
        policy_failed=True,
        metadata={
            "cleaned_paths": list(side_effect_paths),
            "restore_ref": cleanup_result.restore_ref,
        },
    )
    return replace(val_result, commands=[*val_result.commands, command])


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
    git_in_worktree: Callable[[list[str]], Awaitable[CommandResult]],
    execution_owner_id: str | None = None,
    resume_disable_fix_passes: bool = False,
    before_agent_retry: Callable[[], Awaitable[bool]] | None = None,
    after_agent_cleanup_failure_repair: (
        Callable[[ComposeExecCleanupError], Awaitable[bool]] | None
    ) = None,
) -> ExecutionValidationResult:
    """Run validate/fix attempts and emit the terminal validation state.

    ``resume_disable_fix_passes`` marks a blocked-resume with active operator
    grants — both an approve-and-keep grant resume (agent skipped, kept verbatim)
    and a combined directive+grant resume (directive agent ran, grants still
    active). A validation fix pass re-invokes the coding adapter, which would both
    spend tokens and — while grants are active — potentially rewrite a granted
    protected file and have the new violation suppressed by the same single-use
    grant. So grant-bearing resumes run validation with zero fix passes: a failure
    marks the workspace FAILED for operator triage instead of firing a fix pass.
    This mirrors the pre-commit repair gate (PRRT_kwDOSJAM6s6J5SDf).
    """
    if run_model is None:
        run_model = default_model

    successful_validation_run_id: str | None = None
    successful_validation_workspace_head_sha: str | None = None

    # ── Step 2: validation (tests + optional Alembic), with fix-cycle ──
    max_fix_passes = 0 if resume_disable_fix_passes else self._config.max_validation_fix_passes
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

    def _deposit_planning_artifacts_if_required() -> None:
        # Best-effort deposit for terminal paths that do not already pass through
        # one of the preserving helpers below. In particular, when the workspace
        # reaches a callback-terminal status during validation,
        # ``_finish_validation_callback_if_terminal`` returns True and this
        # cycle returns stop=True before the normal validation-success deposit.
        # The console keys its artifact refetch on ``updated_at``; depositing
        # before the caller observes the terminal status orders artifact
        # availability ahead of the polling signal. Idempotent and gated on
        # ``planning.required``.
        _planning_artifacts._deposit_planning_artifacts_best_effort(
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
        # Deposit the worktree plan + conformance report into the served
        # artifact dir BEFORE publishing the terminal FAILED status. The console
        # keys its artifact refetch on the workspace ``updated_at``
        # (TaskArtifactsSection ``refreshKey``); ``_mark_failed`` bumps
        # ``updated_at`` when it publishes FAILED, but the filesystem deposit
        # does not touch the row. Marking FAILED first would let a poll observe
        # the terminal status in the window before the post-cycle deposit (in
        # ``execution_flow``), record an empty artifact list, then never refetch
        # — hiding the Plan/Validation controls on the preserved-FAILED
        # workspace. Depositing here orders artifact availability ahead of the
        # polling signal. Best-effort, idempotent, gated on ``planning.required``.
        _planning_artifacts._deposit_planning_artifacts_best_effort(
            self,
            profile=profile,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
        )
        await self._mark_failed(**mark_kwargs)

    async def _enter_blocked_preserving_planning_artifacts(**block_kwargs: Any) -> None:
        # Same artifact-ordering rationale as the FAILED path above: deposit the
        # plan + conformance report BEFORE the block transition bumps
        # ``updated_at`` (the console's artifact refetch key). A protected
        # quality-gate violation pauses for an operator instead of failing.
        _planning_artifacts._deposit_planning_artifacts_best_effort(
            self,
            profile=profile,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
        )
        await self.enter_blocked_for_protected_violation(**block_kwargs)

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
    # The loop always exits via ``break`` (validation+conformance success) or a
    # terminal ``return`` (budget exhausted / hard failure); the per-category
    # budgets guarantee the final attempt hits one of those paths, so the
    # ``range`` is never exhausted by natural fall-through to the trailing
    # ``return`` below. The fall-through branch is kept as a defensive backstop.
    for pass_number in range(max_validation_attempts):  # pragma: no branch
        # This loop covers the initial validation plus any validation or
        # post-validation conformance fix prompts. The per-category
        # counters below enforce their separate budgets.
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

                    async def _run_conformance_agent(
                        _accept_existing_plan: bool,
                        *,
                        _handoff: _PlanningValidationHandoff = conformance_handoff,
                        _validation_run_id: str = validation_run_id,
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
                        )

                    async def _finish_conformance_recovery_failure(
                        *,
                        _validation_run_id: str = validation_run_id,
                        _validation_coverage: dict[str, object] | None = validation_coverage,
                    ) -> None:
                        _deposit_planning_artifacts_if_required()
                        await self._finish_pending_validate_operations(
                            workspace_id=workspace_id,
                            status=OperationStatus.failed,
                            validation_run_id=_validation_run_id,
                            requested_tier=validation_tier,
                            reason_code=AGENT_SERVICE_UNHEALTHY,
                            coverage=_validation_coverage,
                            error_message=(
                                "agent compose service recovery failed during "
                                "post-validation conformance"
                            ),
                        )

                    (
                        conformance_recovered,
                        conformance_failure,
                    ) = await _run_agent_callable_with_service_recovery(
                        self,
                        run_agent=_run_conformance_agent,
                        workspace=ws,
                        profile=profile,
                        compose_project=compose_project,
                        compose_file=compose_file,
                        model=run_model,
                        command_evidence=[],
                        workspace_id=workspace_id,
                        execution_owner_id=execution_owner_id,
                        before_mark_failed=_finish_conformance_recovery_failure,
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
                    # ``resume_disable_fix_passes`` (a grant-bearing resume) must
                    # likewise never fire a conformance fix pass: re-invoking the
                    # agent while operator grants are active could rewrite a
                    # granted protected file and have the new violation suppressed
                    # by the same single-use grant. Mark FAILED for operator
                    # triage instead (mirrors the zeroed validation-fix budget).
                    if (
                        conformance_report_cleanup_failed
                        or recovery is not None
                        or remaining_conformance_iterations <= 0
                        or resume_disable_fix_passes
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
                                if conformance_report_cleanup_failed
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
        try:

            async def _run_fix_agent(
                _accept_existing_plan: bool,
                *,
                _fix_prompt: str = fix_prompt,
            ) -> AgentRunResult:
                return await adapter.run(
                    compose_project=compose_project,
                    compose_file=compose_file,
                    prompt=_fix_prompt,
                    model=run_model,
                    workspace_id=workspace_id,
                )

            async def _finish_fix_recovery_failure(
                *,
                _validation_run_id: str = validation_run_id,
                _val_result: ValidationResult = val_result,
            ) -> None:
                _deposit_planning_artifacts_if_required()
                await self._finish_pending_validate_operations(
                    workspace_id=workspace_id,
                    status=OperationStatus.failed,
                    validation_run_id=_validation_run_id,
                    requested_tier=validation_tier,
                    reason_code=AGENT_SERVICE_UNHEALTHY,
                    coverage=_validation_run_coverage_metadata(
                        _val_result,
                        baseline_coverage=baseline_coverage,
                    ),
                    error_message=(
                        "agent compose service recovery failed during validation fix pass"
                    ),
                )

            fix_recovered, fix_result = await _run_agent_callable_with_service_recovery(
                self,
                run_agent=_run_fix_agent,
                workspace=ws,
                profile=profile,
                compose_project=compose_project,
                compose_file=compose_file,
                model=run_model,
                command_evidence=fix_command_evidence,
                workspace_id=workspace_id,
                execution_owner_id=execution_owner_id,
                before_mark_failed=_finish_fix_recovery_failure,
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
            await _mark_failed_preserving_planning_artifacts(
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
            await _mark_failed_preserving_planning_artifacts(
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
        if fix_staged_paths:
            if await self._committed_and_staged_output_is_plan_only(
                worktree_path=worktree_path,
                base_commit=base_commit,
                staged_paths=fix_staged_paths,
            ):
                # Plan-only fix-pass output marks the workspace FAILED. Deposit
                # the worktree plan + conformance report BEFORE
                # ``_fail_if_plan_only_paths`` publishes the terminal status —
                # mirroring the post-agent plan-only gate in ``execution_flow``
                # and the ``_mark_failed_preserving_planning_artifacts`` helper
                # every other terminal path in this cycle uses. The console keys
                # its artifact refetch on the workspace ``updated_at``
                # (TaskArtifactsSection ``refreshKey``), and
                # ``_fail_if_plan_only_paths`` routes through bare
                # ``_mark_failed`` (not the preserving helper), so marking FAILED
                # first would bump ``updated_at`` and let a poll observe it in
                # the window before the post-cycle deposit (in
                # ``execution_flow``), record an empty artifact list, then never
                # refetch — hiding the Plan/Validation controls on the
                # preserved-FAILED workspace. Best-effort, idempotent, gated on
                # ``planning.required``.
                _planning_artifacts._deposit_planning_artifacts_best_effort(
                    self,
                    profile=profile,
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                )
                # The plan-only gate above already confirmed the staged delta is
                # entirely internal plan artifacts, so this marks the workspace
                # FAILED (PLAN_ONLY_OUTPUT) and returns True.
                await self._fail_if_plan_only_paths(
                    workspace_id=workspace_id,
                    changed_paths=fix_staged_paths,
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
                    error_message=plan_only_output_message(fix_staged_paths),
                )
                return ExecutionValidationResult(
                    stop=True,
                    successful_validation_run_id=successful_validation_run_id,
                    successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
                )
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
                await _enter_blocked_preserving_planning_artifacts(
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

        fix_pass_ignored_check = await check_validation_worktree_clean(
            run_git=git_in_worktree,
            worktree_path=worktree_path,
            ignore_all_ignored=True,
        )
        if not fix_pass_ignored_check.clean:
            reason_code = (
                fix_pass_ignored_check.reason_code or VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
            )
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

        # Loop back to re-validate.

    return ExecutionValidationResult(
        stop=False,
        successful_validation_run_id=successful_validation_run_id,
        successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
    )
