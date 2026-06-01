"""Pre-push validation for PR monitor authored repair commits."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from awf.adapters.base import AgentRunError
from awf.common.command_evidence import append_command_evidence
from awf.common.compose_exec import ComposeExecCleanupError, cleanup_failure_message
from awf.common.logging import get_logger
from awf.control.executor.helpers import (
    _profile_for_workspace,
    _should_run_local_coverage,
    _validation_run_command_records,
    _validation_run_coverage_metadata,
    _validation_run_reason_code,
    _validation_tier_for_workspace,
)
from awf.control.executor.logging_ops import _validation_run_log_stream_refs
from awf.control.validation_fix_cycle import (
    ValidationFixContext,
    build_fix_prompt,
    read_output_tail,
)
from awf.db.repositories import (
    TaskAttemptRepository,
    ValidationRunRepository,
    WorkspaceRepository,
)
from awf.runtime.pr_monitor_runner.comments import _git_worktree_command
from awf.runtime.pr_monitor_runner.pre_push_validation_constants import (
    _PRE_PUSH_VALIDATION_FAILED_REASON,
    _PRE_PUSH_VALIDATION_FIX_FAILED_REASON,
    _PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON,
    _PRE_PUSH_VALIDATION_ROLLBACK_FAILED_REASON,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.validation import profile_phase_command_plan
from awf.runtime.validation_identity import (
    environment_identity_digest,
    environment_identity_inputs,
    resolved_profile_digest,
)
from awf.runtime.validation_types import (
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationResult,
)
from awf.runtime.validation_worktree_constants import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    VALIDATION_WORKTREE_STATUS_FAILED,
)

if TYPE_CHECKING:
    from awf.runtime.validation_worktree import (
        ValidationWorktreeCheck,
        ValidationWorktreeCleanup,
    )

_log = get_logger(__name__)

PRE_PUSH_VALIDATION_FAILED_REASON = _PRE_PUSH_VALIDATION_FAILED_REASON
PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON = _PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON
PRE_PUSH_VALIDATION_FIX_FAILED_REASON = _PRE_PUSH_VALIDATION_FIX_FAILED_REASON
PRE_PUSH_VALIDATION_ROLLBACK_FAILED_REASON = _PRE_PUSH_VALIDATION_ROLLBACK_FAILED_REASON


@dataclass(frozen=True)
class _PrePushValidationResult:
    """Pre-push validation outcome for a single workspace push attempt."""

    passed: bool
    validation_run_id: str | None
    workspace_head_sha: str | None
    reason_code: str
    message: str
    validation_reason_code: str | None = None
    result: ValidationResult | None = None
    coverage: ValidationCoverageResult | None = None
    extra_details: Mapping[str, object] | None = None
    ignore_ignored_paths: tuple[str, ...] = ()
    ignore_ignored_paths_snapshot: tuple[str, ...] = ()

    @property
    def first_failure(self) -> ValidationCommandResult | None:
        """Return the first failed validation command, if any."""
        return self.result.first_failure if self.result is not None else None

    def failure_details(self) -> dict[str, object]:
        """Build details payload for pre-push validation push failures."""
        details: dict[str, object] = {
            "phase": "pre_push_validation",
            "reason_code": self.reason_code,
            "error_message": self.message,
            "pushed": False,
        }
        if self.validation_run_id is not None:
            details["validation_run_id"] = self.validation_run_id
        if self.workspace_head_sha is not None:
            details["workspace_head_sha"] = self.workspace_head_sha
            details["target_head_sha"] = self.workspace_head_sha
        if self.validation_reason_code is not None:
            details["validation_reason_code"] = self.validation_reason_code
        if self.extra_details is not None:
            details.update(self.extra_details)
        return details


async def _validated_git_push_result(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    remote_branch: str,
    compose_project: str,
    compose_file: Path,
    remote_url: str | None = None,
    refspec: str | None = None,
    state: object | None = None,
) -> _GitPushResult:
    """Run pre-push validation with optional fix passes before pushing."""
    if self._deps.validation is None:
        return cast(
            _GitPushResult,
            await self._git_push_result(
                worktree_path=worktree_path,
                remote_branch=remote_branch,
                remote_url=remote_url,
                refspec=refspec,
            ),
        )
    validation_result = await _run_pre_push_validation_with_fix_passes(
        self,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        compose_project=compose_project,
        compose_file=compose_file,
        remote_branch=remote_branch,
        remote_url=remote_url,
        state=state,
    )
    if not validation_result.passed:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr=validation_result.message,
            reason_code=validation_result.reason_code,
            details=validation_result.failure_details(),
        )
    return cast(
        _GitPushResult,
        await self._git_push_result(
            worktree_path=worktree_path,
            remote_branch=remote_branch,
            remote_url=remote_url,
            refspec=refspec,
        ),
    )


async def _run_pre_push_validation_with_fix_passes(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    compose_project: str,
    compose_file: Path,
    remote_branch: str,
    remote_url: str | None,
    state: object | None,
) -> _PrePushValidationResult:
    """Execute pre-push validation plus optional fix/retry attempts."""
    max_fix_passes = max(0, self._runner_config.pre_push_validation_fix_passes)
    validation_commands = await _pre_push_validation_commands(
        self,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
    )
    last_result: _PrePushValidationResult | None = None
    baseline_ignored_paths: tuple[str, ...] | None = None
    baseline_ignored_paths_snapshot: tuple[str, ...] | None = None
    for pass_index in range(max_fix_passes + 1):
        validation_result = await _run_pre_push_validation(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            compose_project=compose_project,
            compose_file=compose_file,
            remote_branch=remote_branch,
            ignore_ignored_paths=baseline_ignored_paths,
            ignore_all_ignored=baseline_ignored_paths is None,
            capture_ignored_paths_snapshot=(
                baseline_ignored_paths is None or bool(baseline_ignored_paths)
            ),
        )
        if baseline_ignored_paths is None:
            baseline_ignored_paths = validation_result.ignore_ignored_paths
            baseline_ignored_paths_snapshot = validation_result.ignore_ignored_paths_snapshot
        if validation_result.passed:
            return validation_result
        last_result = validation_result
        if validation_result.reason_code == PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON:
            return validation_result
        if validation_result.first_failure is None:
            return validation_result
        if pass_index >= max_fix_passes:
            break
        committed, rollback_failed_reason = await _run_pre_push_validation_fix_pass(
            self,
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            remote_branch=remote_branch,
            remote_url=remote_url,
            state=state,
            validation_result=validation_result,
            ignore_ignored_paths=baseline_ignored_paths or (),
            ignore_ignored_paths_snapshot=baseline_ignored_paths_snapshot,
            pass_number=pass_index + 1,
            total_passes=max_fix_passes,
            validation_commands=validation_commands,
        )
        if not committed:
            if rollback_failed_reason is not None:
                return replace(
                    validation_result,
                    reason_code=rollback_failed_reason,
                    message=(
                        "PR monitor pre-push validation fix pass rollback failed "
                        f"after {pass_index + 1}/{max_fix_passes} attempts: "
                        f"{validation_result.message}"
                    ),
                )
            return replace(
                validation_result,
                reason_code=PRE_PUSH_VALIDATION_FIX_FAILED_REASON,
                message=(
                    "PR monitor pre-push validation fix pass failed after "
                    f"{pass_index + 1}/{max_fix_passes} attempts: {validation_result.message}"
                ),
            )
    assert last_result is not None
    return last_result


async def _pre_push_validation_commands(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
) -> tuple[str, ...]:
    """Load the post-agent and validate commands for a workspace profile."""
    async with self._deps.session_factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        if ws is None:
            return ()
        profile = _profile_for_workspace(ws, worktree_path=worktree_path)
    return tuple(
        step.command.command
        for step in profile_phase_command_plan(profile, ("post_agent", "validate"))
    )


async def _pre_push_validation_worktree_check(
    self: Any,
    *,
    worktree_path: Path,
    ignore_all_ignored: bool = True,
    ignore_ignored_paths: tuple[str, ...] | None = None,
    capture_ignored_paths_snapshot: bool = True,
) -> ValidationWorktreeCheck:
    """Check pre-push validation preconditions for clean validation worktree state."""

    async def _run_git(args: list[str]) -> Any:
        """Run git command arguments inside the workspace worktree."""
        return await self._deps.runner.run(_git_worktree_command(worktree_path, *args))

    from awf.runtime.validation_worktree import check_validation_worktree_clean

    return await check_validation_worktree_clean(
        run_git=_run_git,
        worktree_path=worktree_path,
        ignore_all_ignored=ignore_all_ignored,
        ignore_ignored_paths=ignore_ignored_paths,
        capture_ignored_paths_snapshot=capture_ignored_paths_snapshot,
    )


async def _pre_push_validation_cleanup(
    self: Any,
    *,
    worktree_path: Path,
    restore_ref: str,
    ignore_ignored_paths: tuple[str, ...] | None = None,
    ignore_ignored_paths_snapshot: tuple[str, ...] | None = None,
) -> ValidationWorktreeCleanup:
    """Clean validation side effects and restore the worktree to the requested ref."""

    async def _run_git(args: list[str]) -> Any:
        """Run git command arguments inside the workspace worktree."""
        return await self._deps.runner.run(_git_worktree_command(worktree_path, *args))

    from awf.runtime.validation_worktree import cleanup_validation_worktree_side_effects

    return await cleanup_validation_worktree_side_effects(
        run_git=_run_git,
        worktree_path=worktree_path,
        restore_ref=restore_ref,
        ignore_ignored_paths=ignore_ignored_paths,
        ignore_ignored_paths_snapshot=ignore_ignored_paths_snapshot,
    )


def _pre_push_dirty_result(
    *,
    workspace_head_sha: str | None,
    check: ValidationWorktreeCheck,
) -> _PrePushValidationResult:
    """Build a pre-push validation result for pre-existing dirt."""
    from awf.runtime.validation_worktree import validation_worktree_preexisting_dirty_message

    reason_code = check.reason_code or VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    message = (
        check.message
        if reason_code == VALIDATION_WORKTREE_STATUS_FAILED
        else validation_worktree_preexisting_dirty_message(check)
    )
    return _PrePushValidationResult(
        passed=False,
        validation_run_id=None,
        workspace_head_sha=workspace_head_sha,
        reason_code=reason_code,
        message=message,
        extra_details=check.details(),
        ignore_ignored_paths_snapshot=check.ignored_paths_snapshot,
    )


def _pre_push_cleanup_result(
    validation_run_id: str | None,
    workspace_head_sha: str | None,
    cleanup: ValidationWorktreeCleanup,
    *,
    upstream_failure: Mapping[str, object] | None = None,
) -> _PrePushValidationResult:
    """Build a pre-push validation result from cleanup failure details."""
    from awf.runtime.validation_worktree import validation_worktree_cleanup_failure_message

    extra_details: dict[str, object] = cleanup.details()
    if upstream_failure is not None:
        extra_details.update(upstream_failure)
    return _PrePushValidationResult(
        passed=False,
        validation_run_id=validation_run_id,
        workspace_head_sha=workspace_head_sha,
        reason_code=cleanup.reason_code or VALIDATION_WORKTREE_CLEANUP_FAILED,
        message=validation_worktree_cleanup_failure_message(cleanup),
        extra_details=extra_details,
    )


async def _run_pre_push_validation_fix_pass(
    self: Any,
    *,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    remote_branch: str,
    remote_url: str | None,
    state: object | None,
    validation_result: _PrePushValidationResult,
    ignore_ignored_paths: tuple[str, ...] = (),
    ignore_ignored_paths_snapshot: tuple[str, ...] | None = None,
    pass_number: int,
    total_passes: int,
    validation_commands: tuple[str, ...],
) -> tuple[bool, str | None]:
    """Attempt a validation fix pass using the failure context and evidence."""
    first_fail = validation_result.first_failure
    if first_fail is None:
        return False, None
    worktree_path = self._worktrees_root / workspace_id
    fix_start_head = await self._rev_parse_head(worktree_path)
    if fix_start_head is None:
        _log.warning(
            "monitor.pre_push_validation_fix_start_head_unavailable",
            workspace_id=workspace_id,
            pass_number=pass_number,
        )
        return False, None
    context = ValidationFixContext(
        failed_command=first_fail.command,
        returncode=first_fail.returncode,
        stdout_tail=read_output_tail(first_fail.stdout_path),
        stderr_tail=read_output_tail(first_fail.stderr_path),
        pass_number=pass_number,
        total_passes=total_passes,
        test_commands=validation_commands,
        reason_code=(
            validation_result.validation_reason_code
            if validation_result.validation_reason_code is not None
            else validation_result.reason_code
        ),
        coverage_percent=(
            validation_result.coverage.percent if validation_result.coverage is not None else None
        ),
        coverage_minimum_percent=(
            validation_result.coverage.minimum_percent
            if validation_result.coverage is not None
            else None
        ),
        failing_test_node_ids=(
            tuple(validation_result.coverage.failing_test_node_ids)
            if validation_result.coverage is not None
            else ()
        ),
        failing_test_evidence=(
            tuple(validation_result.coverage.failing_test_evidence)
            if validation_result.coverage is not None
            else ()
        ),
    )
    command_evidence: list[str] = []
    try:
        fix_result = await self._deps.adapter.run(
            compose_project=compose_project,
            compose_file=compose_file,
            prompt=build_fix_prompt(context),
            workspace_id=workspace_id,
            log_source="monitor-pre-push-validation-fix",
        )
        append_command_evidence(
            command_evidence,
            stdout=fix_result.stdout,
            stderr=fix_result.stderr,
        )
    except AgentRunError as exc:
        append_command_evidence(
            command_evidence,
            stdout=exc.result.stdout,
            stderr=exc.result.stderr,
        )
        _log.warning(
            "monitor.pre_push_validation_fix_agent_nonzero",
            workspace_id=workspace_id,
            pass_number=pass_number,
            reason_code=exc.reason_code,
        )
    except ComposeExecCleanupError as exc:
        _log.warning(
            "monitor.pre_push_validation_fix_cleanup_failed",
            workspace_id=workspace_id,
            pass_number=pass_number,
            reason_code=exc.reason_code,
        )
        rollback_ok = await _rollback_failed_pre_push_validation_fix_pass(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            restore_ref=fix_start_head,
            ignore_ignored_paths=ignore_ignored_paths,
            ignore_ignored_paths_snapshot=ignore_ignored_paths_snapshot,
            pass_number=pass_number,
            reason="compose_cleanup_failed",
        )
        if not rollback_ok:
            return False, PRE_PUSH_VALIDATION_ROLLBACK_FAILED_REASON
        return False, None
    except Exception as exc:
        _log.warning(
            "monitor.pre_push_validation_fix_failed",
            workspace_id=workspace_id,
            pass_number=pass_number,
            error=repr(exc),
        )
        rollback_ok = await _rollback_failed_pre_push_validation_fix_pass(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            restore_ref=fix_start_head,
            ignore_ignored_paths=ignore_ignored_paths,
            ignore_ignored_paths_snapshot=ignore_ignored_paths_snapshot,
            pass_number=pass_number,
            reason="agent_exception",
        )
        if not rollback_ok:
            return False, PRE_PUSH_VALIDATION_ROLLBACK_FAILED_REASON
        return False, None

    try:
        committed = bool(
            await self._commit_dirty_worktree(
                workspace_id=workspace_id,
                message=f"awf: pre-push validation fix for {workspace_id}",
                compose_project=compose_project,
                compose_file=compose_file,
                state=state,
                command_evidence=command_evidence,
                protected_scope_revert_remote_branch=remote_branch,
                remote_push_url=remote_url,
            )
        )
    except Exception as exc:
        _log.warning(
            "monitor.pre_push_validation_fix_commit_failed",
            workspace_id=workspace_id,
            pass_number=pass_number,
            error=repr(exc),
        )
        rollback_ok = await _rollback_failed_pre_push_validation_fix_pass(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            restore_ref=fix_start_head,
            ignore_ignored_paths=ignore_ignored_paths,
            ignore_ignored_paths_snapshot=ignore_ignored_paths_snapshot,
            pass_number=pass_number,
            reason="commit_exception",
        )
        if not rollback_ok:
            return False, PRE_PUSH_VALIDATION_ROLLBACK_FAILED_REASON
        return False, None
    if not committed:
        rollback_ok = await _rollback_failed_pre_push_validation_fix_pass(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            restore_ref=fix_start_head,
            ignore_ignored_paths=ignore_ignored_paths,
            ignore_ignored_paths_snapshot=ignore_ignored_paths_snapshot,
            pass_number=pass_number,
            reason="commit_failed",
        )
        if not rollback_ok:
            return False, PRE_PUSH_VALIDATION_ROLLBACK_FAILED_REASON
    return committed, None


async def _rollback_failed_pre_push_validation_fix_pass(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    restore_ref: str,
    ignore_ignored_paths: tuple[str, ...] = (),
    ignore_ignored_paths_snapshot: tuple[str, ...] | None = None,
    pass_number: int,
    reason: str,
) -> bool:
    """Rollback uncommitted validation-fix edits before the monitor loops again."""
    reset = await self._deps.runner.run(
        _git_worktree_command(worktree_path, "reset", "--hard", restore_ref)
    )
    if not reset.ok:
        log = _log.warning
        log(
            "monitor.pre_push_validation_fix_rollback",
            workspace_id=workspace_id,
            pass_number=pass_number,
            reason=reason,
            restore_ref=restore_ref,
            reset_returncode=reset.returncode,
            clean_returncode=None,
            reset_stderr=(reset.stderr or "")[:400],
            clean_stderr=None,
        )
        return False

    cleanup = await _pre_push_validation_cleanup(
        self,
        worktree_path=worktree_path,
        restore_ref=restore_ref,
        ignore_ignored_paths=ignore_ignored_paths,
        ignore_ignored_paths_snapshot=ignore_ignored_paths_snapshot,
    )
    ok = bool(cleanup.ok)
    log = _log.info if ok else _log.warning
    log(
        "monitor.pre_push_validation_fix_rollback",
        workspace_id=workspace_id,
        pass_number=pass_number,
        reason=reason,
        restore_ref=restore_ref,
        reset_returncode=reset.returncode,
        clean_returncode=0 if ok else None,
        reset_stderr=(reset.stderr or "")[:400],
        clean_stderr=(cleanup.cleanup_stderr or "")[:400],
    )
    return ok


async def _run_pre_push_validation(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    compose_project: str,
    compose_file: Path,
    remote_branch: str,
    ignore_ignored_paths: tuple[str, ...] | None = None,
    ignore_all_ignored: bool = True,
    capture_ignored_paths_snapshot: bool = True,
) -> _PrePushValidationResult:
    """Run a single pre-push validation cycle and persist run metadata."""
    async with self._deps.session_factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        if ws is None:
            return _PrePushValidationResult(
                passed=False,
                validation_run_id=None,
                workspace_head_sha=None,
                reason_code=PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON,
                message="workspace disappeared before PR monitor pre-push validation",
            )
        profile = _profile_for_workspace(ws, worktree_path=worktree_path)
        validation_tier = _validation_tier_for_workspace(ws, profile)
        base_commit = ws.base_commit

    workspace_head_sha = await self._rev_parse_head(worktree_path)
    if workspace_head_sha is None:
        return _PrePushValidationResult(
            passed=False,
            validation_run_id=None,
            workspace_head_sha=None,
            reason_code=PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON,
            message="could not capture local HEAD before PR monitor pre-push validation",
        )

    pre_validation_check = await _pre_push_validation_worktree_check(
        self,
        worktree_path=worktree_path,
        ignore_all_ignored=ignore_all_ignored,
        ignore_ignored_paths=ignore_ignored_paths,
        capture_ignored_paths_snapshot=capture_ignored_paths_snapshot,
    )
    if not pre_validation_check.clean:
        return _pre_push_dirty_result(
            workspace_head_sha=workspace_head_sha,
            check=pre_validation_check,
        )
    pre_validation_ignore_paths = pre_validation_check.ignored_paths
    pre_validation_ignored_paths_snapshot = pre_validation_check.ignored_paths_snapshot

    validation_run_id = await _start_pre_push_validation_run(
        self,
        workspace_id=workspace_id,
        profile=profile,
        base_commit=base_commit,
        workspace_head_sha=workspace_head_sha,
        target_branch=remote_branch,
        tier=validation_tier,
    )
    if validation_run_id is None:
        return _PrePushValidationResult(
            passed=False,
            validation_run_id=None,
            workspace_head_sha=workspace_head_sha,
            reason_code=PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON,
            message="workspace has no task attempt before PR monitor pre-push validation",
            ignore_ignored_paths=pre_validation_ignore_paths,
            ignore_ignored_paths_snapshot=pre_validation_ignored_paths_snapshot,
        )
    coverage_result: ValidationCoverageResult | None = None
    try:
        assert self._deps.validation is not None
        result = await self._deps.validation.run_profile_phases(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            profile=profile,
            phase_names=("post_agent", "validate"),
            run_healthchecks=True,
            worktree_path=worktree_path,
            include_coverage=False,
        )
        if _should_run_local_coverage(profile) and result.all_passed:
            coverage_result = await self._deps.validation.run_profile_coverage(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                profile=profile,
                phase="coverage",
            )
            if coverage_result is not None:
                result = replace(result, coverage=coverage_result)
    except ComposeExecCleanupError as exc:
        message = cleanup_failure_message(exc)
        compose_failure_context: dict[str, object] = {
            "compose_exec_reason_code": exc.reason_code,
            "compose_exec_source": exc.source,
            "compose_exec_label": exc.label,
            "compose_exec_invocation_id": exc.invocation_id,
            "compose_exec_message": message,
        }
        _log.warning(
            "pre_push_validation.compose_exec_cleanup_failed",
            workspace_id=workspace_id,
            compose_exec_reason_code=exc.reason_code,
            compose_exec_source=exc.source,
            compose_exec_label=exc.label,
            compose_exec_invocation_id=exc.invocation_id,
            compose_exec_message=message,
        )
        cleanup_result = await _pre_push_validation_cleanup(
            self,
            worktree_path=worktree_path,
            restore_ref=workspace_head_sha,
            ignore_ignored_paths=pre_validation_check.ignored_paths,
            ignore_ignored_paths_snapshot=pre_validation_ignored_paths_snapshot,
        )
        if not cleanup_result.ok:
            await _finish_pre_push_validation_run(
                self,
                validation_run_id,
                status="failed",
                reason_code=cleanup_result.reason_code,
            )
            return _pre_push_cleanup_result(
                validation_run_id=validation_run_id,
                workspace_head_sha=workspace_head_sha,
                cleanup=cleanup_result,
                upstream_failure=compose_failure_context,
            )
        await _finish_pre_push_validation_run(
            self,
            validation_run_id,
            status="failed",
            reason_code=exc.reason_code,
        )
        return _PrePushValidationResult(
            passed=False,
            validation_run_id=validation_run_id,
            workspace_head_sha=workspace_head_sha,
            reason_code=PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON,
            message=message,
            ignore_ignored_paths=pre_validation_ignore_paths,
            ignore_ignored_paths_snapshot=pre_validation_ignored_paths_snapshot,
        )
    except Exception as exc:
        message = f"unexpected error during PR monitor pre-push validation: {exc!r}"[:2000]
        unexpected_failure_context: dict[str, object] = {
            "unexpected_exception_type": exc.__class__.__name__,
            "unexpected_exception_message": message,
        }
        _log.warning(
            "pre_push_validation.unexpected_error",
            workspace_id=workspace_id,
            error_type=exc.__class__.__name__,
            error_message=message,
        )
        cleanup_result = await _pre_push_validation_cleanup(
            self,
            worktree_path=worktree_path,
            restore_ref=workspace_head_sha,
            ignore_ignored_paths=pre_validation_check.ignored_paths,
            ignore_ignored_paths_snapshot=pre_validation_ignored_paths_snapshot,
        )
        if not cleanup_result.ok:
            await _finish_pre_push_validation_run(
                self,
                validation_run_id,
                status="failed",
                reason_code=cleanup_result.reason_code,
            )
            return _pre_push_cleanup_result(
                validation_run_id=validation_run_id,
                workspace_head_sha=workspace_head_sha,
                cleanup=cleanup_result,
                upstream_failure=unexpected_failure_context,
            )
        await _finish_pre_push_validation_run(
            self,
            validation_run_id,
            status="failed",
            reason_code=PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON,
        )
        return _PrePushValidationResult(
            passed=False,
            validation_run_id=validation_run_id,
            workspace_head_sha=workspace_head_sha,
            reason_code=PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON,
            message=message,
            ignore_ignored_paths=pre_validation_ignore_paths,
            ignore_ignored_paths_snapshot=pre_validation_ignored_paths_snapshot,
        )

    cleanup_result = await _pre_push_validation_cleanup(
        self,
        worktree_path=worktree_path,
        restore_ref=workspace_head_sha,
        ignore_ignored_paths=pre_validation_check.ignored_paths,
        ignore_ignored_paths_snapshot=pre_validation_ignored_paths_snapshot,
    )
    if not cleanup_result.ok:
        await _finish_pre_push_validation_run(
            self,
            validation_run_id,
            status="failed",
            reason_code=cleanup_result.reason_code,
        )
        return _pre_push_cleanup_result(
            validation_run_id=validation_run_id,
            workspace_head_sha=workspace_head_sha,
            cleanup=cleanup_result,
        )

    reason_code = _validation_run_reason_code(result)
    await _finish_pre_push_validation_run(
        self,
        validation_run_id,
        status="succeeded" if result.all_passed else "failed",
        reason_code=reason_code,
        retry_count=result.total_retries,
        coverage=_validation_run_coverage_metadata(result),
        command_retries=[c.retry_count for c in result.commands],
    )
    return _PrePushValidationResult(
        passed=result.all_passed,
        validation_run_id=validation_run_id,
        workspace_head_sha=workspace_head_sha,
        reason_code=("VALIDATION_OK" if result.all_passed else PRE_PUSH_VALIDATION_FAILED_REASON),
        message=(
            "PR monitor pre-push validation passed"
            if result.all_passed
            else f"PR monitor pre-push validation failed: {reason_code}"
        ),
        validation_reason_code=None if result.all_passed else reason_code,
        result=result,
        coverage=coverage_result or result.coverage,
        ignore_ignored_paths=pre_validation_ignore_paths,
        ignore_ignored_paths_snapshot=pre_validation_ignored_paths_snapshot,
    )


async def _start_pre_push_validation_run(
    self: Any,
    *,
    workspace_id: str,
    profile: Any,
    base_commit: str | None,
    workspace_head_sha: str,
    target_branch: str,
    tier: int,
) -> str | None:
    """Create and start a pre-push validation run record."""
    command_records = _validation_run_command_records(
        profile=profile,
        phase_names=("post_agent", "validate"),
        run_healthchecks=True,
    )
    async with self._deps.session_factory() as session:
        attempt = await TaskAttemptRepository(session).get_by_workspace_id(workspace_id)
        if attempt is None:
            return None
        run = await ValidationRunRepository(session).start(
            workspace_id=workspace_id,
            attempt_id=attempt.id,
            tier=tier,
            commands=command_records,
            base_commit=base_commit,
            base_sha=base_commit,
            workspace_head_sha=workspace_head_sha,
            target_branch=target_branch,
            target_head_sha=workspace_head_sha,
            profile_name=profile.name,
            profile_version=profile.version,
            profile_source=profile.source,
            resolved_profile_digest=resolved_profile_digest(profile),
            environment_identity_digest=environment_identity_digest(profile),
            environment_identity_inputs=environment_identity_inputs(profile),
            log_stream_refs=_validation_run_log_stream_refs(command_records),
            started_at=datetime.now(UTC),
        )
        await session.commit()
        return run.id


async def _finish_pre_push_validation_run(
    self: Any,
    validation_run_id: str,
    *,
    status: str,
    reason_code: str | None,
    retry_count: int = 0,
    coverage: dict[str, object] | None = None,
    command_retries: list[int] | None = None,
) -> None:
    """Finalize a pre-push validation run and persist completion details."""
    async with self._deps.session_factory() as session:
        await ValidationRunRepository(session).finish(
            validation_run_id,
            status=status,
            reason_code=reason_code,
            finished_at=datetime.now(UTC),
            retry_count=retry_count,
            coverage=coverage,
            command_retries=command_retries,
        )
        await session.commit()
