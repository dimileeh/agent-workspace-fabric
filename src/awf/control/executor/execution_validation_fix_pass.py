"""Validation fix-pass agent run and commit for WorkspaceExecutor."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
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
from awf.control.executor import validation_fix_helpers as _validation_fix_helpers
from awf.control.executor.agent_service_recovery import _run_agent_callable_with_service_recovery
from awf.control.executor.git_ops import _git_name_lines
from awf.control.executor.helpers import _is_adapter_retired, _validation_run_coverage_metadata
from awf.control.executor.hosted_validation_sync import (
    _hosted_agent_error_terminal_head_sha,
    _sync_hosted_validation_fix_head,
)
from awf.control.executor.quality_gates import _log
from awf.control.executor.validation_cleanup_guards import ExecutionValidationResult
from awf.control.quality_gates import find_protected_quality_gate_changes
from awf.control.validation_fix_cycle import ValidationFixContext, build_fix_prompt
from awf.db.enums import FailureReason, OperationStatus, WorkspaceStatus
from awf.db.models import Workspace
from awf.profiles.models import WorkspaceProfile
from awf.runtime.validation import ValidationCoverageResult, ValidationResult


@dataclass(frozen=True)
class ValidationFixPassContinue:
    """Fix pass completed; the validation loop should re-run profile phases."""

    validation_fix_passes_used: int
    hosted_pr_identity: dict[str, Any] | None


async def run_validation_fix_pass(
    self: Any,
    *,
    workspace_id: str,
    ws: Workspace,
    worktree_path: Path,
    compose_project: str,
    compose_file: Path,
    profile: WorkspaceProfile,
    adapter: AgentAdapter | None,
    run_model: str | None,
    base_commit: str,
    git_in_worktree: Callable[[list[str]], Awaitable[CommandResult]],
    first_fail: Any,
    val_result: ValidationResult,
    validation_run_id: str,
    validation_tier: int,
    validation_coverage: dict[str, object] | None,
    validation_workspace_head_sha: str,
    baseline_coverage: ValidationCoverageResult | None,
    hosted_pr_identity: dict[str, Any] | None,
    is_post_validation_conformance_fix_pass: bool,
    validation_fix_passes_used: int,
    fix_context: ValidationFixContext,
    successful_validation_run_id: str | None,
    successful_validation_workspace_head_sha: str | None,
    execution_owner_id: str | None,
    deposit_planning_artifacts_if_required: Callable[[], None],
    mark_failed_preserving_planning_artifacts: Callable[..., Awaitable[None]],
    enter_blocked_preserving_planning_artifacts: Callable[..., Awaitable[None]],
    before_agent_retry: Callable[[], Awaitable[bool | str]] | None,
    after_agent_cleanup_failure_repair: (
        Callable[[ComposeExecCleanupError], Awaitable[bool | str]] | None
    ),
) -> ExecutionValidationResult | ValidationFixPassContinue:
    """Run one validation fix pass and commit any produced changes."""
    fix_pass_kind = (
        "post-validation conformance" if is_post_validation_conformance_fix_pass else "validation"
    )
    fix_pass_number = fix_context.pass_number
    fix_pass_total_passes = fix_context.total_passes
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
        deposit_planning_artifacts_if_required()
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
        deposit_planning_artifacts_if_required()
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
            await mark_failed_preserving_planning_artifacts(
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
        await mark_failed_preserving_planning_artifacts(
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

    updated_validation_fix_passes_used = validation_fix_passes_used
    if not is_post_validation_conformance_fix_pass:
        updated_validation_fix_passes_used += 1

    if not await self._recheck_status(
        workspace_id,
        expected=WorkspaceStatus.validating,
        action="validation_fix_commit",
    ):
        deposit_planning_artifacts_if_required()
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
        deposit_planning_artifacts_if_required()
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
        mark_failed_preserving_planning_artifacts=mark_failed_preserving_planning_artifacts,
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
        mark_failed_preserving_planning_artifacts=mark_failed_preserving_planning_artifacts,
        enter_blocked_preserving_planning_artifacts=enter_blocked_preserving_planning_artifacts,
        execution_owner_id=execution_owner_id,
    )

    updated_hosted_pr_identity = hosted_pr_identity
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
        if updated_hosted_pr_identity is not None:
            updated_hosted_pr_identity = {
                **updated_hosted_pr_identity,
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
            deposit_planning_artifacts_if_required()
            return ExecutionValidationResult(
                stop=True,
                successful_validation_run_id=successful_validation_run_id,
                successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
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
                successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
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
        return ValidationFixPassContinue(
            validation_fix_passes_used=updated_validation_fix_passes_used,
            hosted_pr_identity=updated_hosted_pr_identity,
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
        deposit_planning_artifacts_if_required()
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
            deposit_planning_artifacts_if_required()
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

    return ValidationFixPassContinue(
        validation_fix_passes_used=updated_validation_fix_passes_used,
        hosted_pr_identity=updated_hosted_pr_identity,
    )
