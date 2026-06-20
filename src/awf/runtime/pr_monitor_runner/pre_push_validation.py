"""Pre-push validation for PR monitor authored repair commits."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from awf.common.audit import redact_audit_text
from awf.common.compose_exec import ComposeExecCleanupError, cleanup_failure_message
from awf.common.logging import get_logger
from awf.control.executor.helpers import (
    _profile_for_workspace,
    _should_run_local_coverage,
    _validation_run_command_records,
    _validation_run_coverage_metadata,
    _validation_tier_for_workspace,
)
from awf.control.executor.logging_ops import _validation_run_log_stream_refs
from awf.db.repositories import (
    TaskAttemptRepository,
    ValidationRunRepository,
    WorkspaceRepository,
)
from awf.node.git_manager import (
    GitOperationError,
    mirror_path_for_worktree,
    repair_mirror_hooks_path,
    verify_head_object_exists,
)
from awf.runtime.agent_scratch import apply_agent_scratch_excludes
from awf.runtime.ownership import (
    AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
    MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    repair_agent_runtime_ownership,
)
from awf.runtime.pr_monitor_runner.constants import (
    _HEAD_OBJECT_MISSING_RECOVERED_REASON,
    _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
    _MIRROR_HOOKS_PATH_POISONED_REASON,
    _PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
    _PROTECTED_SCOPE_REPAIR_FAILED_REASON,
)
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.pre_push_validation_constants import (
    _PRE_PUSH_DIRTY_FINALIZE_DELTA_UNAVAILABLE_REASON,
    _PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON,
    _PRE_PUSH_VALIDATION_FAILED_REASON,
    _PRE_PUSH_VALIDATION_FIX_FAILED_REASON,
    _PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON,
    _PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON,
    _PRE_PUSH_VALIDATION_ROLLBACK_FAILED_REASON,
    _PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING_REASON,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_dirty_finalize import (
    _committed_delta_paths,  # noqa: F401  (re-exported for tests)
    _operation_owned_delta_paths,  # noqa: F401  (re-exported for tests)
    _rollback_finalize_dirty_residue_before_provider_recovery,  # noqa: F401  (re-exported for tests)
    _try_finalize_pre_push_dirty_repair_state,  # noqa: F401  (re-exported for tests)
)
from awf.runtime.pr_monitor_runner.pre_push_validation_failures import (
    _failed_pre_push_commands,
    _pre_push_validation_reason_code_for_preferred_failure,
    _preferred_pre_push_failure,
    _preferred_pre_push_failure_from_failures,
    _pure_toolchain_missing_failure_for_result,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass import (
    _cleanup_committed_pre_push_validation_fix_pass,  # noqa: F401  (re-exported for tests)
    _head_descends_from,  # noqa: F401  (re-exported for tests)
    _protected_scope_violations_for_recovered_commit,
    _reparent_fix_pass_commit,  # noqa: F401  (re-exported for tests)
    _rollback_failed_pre_push_validation_fix_pass,  # noqa: F401  (re-exported for tests)
    _run_pre_push_validation_fix_pass,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.pr_monitor_runner.remote_repair import (
    _mirror_commit_object_exists,
    _open_merge_candidate_head_sha,
    _recover_missing_head_object_from_filesystem,
)
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
)
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
    VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED,
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
PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING_REASON = _PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING_REASON
PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON = _PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON
PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON = _PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON
PRE_PUSH_DIRTY_FINALIZE_DELTA_UNAVAILABLE_REASON = _PRE_PUSH_DIRTY_FINALIZE_DELTA_UNAVAILABLE_REASON

_FAILING_COMMAND_DETAIL_LIMIT = 1000


def _safe_pre_push_validation_artifact_name(value: str) -> str:
    """Return a filesystem-safe artifact name for pre-push validation evidence."""
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)
    return safe or "validation"


def _pre_push_side_effect_failure_result(
    *,
    result: ValidationResult,
    cleanup: ValidationWorktreeCleanup,
    workspace_id: str,
    validation_run_id: str,
    artifacts_root: Path,
) -> tuple[ValidationResult, Mapping[str, object]]:
    """Add a synthetic failure for passing validation that required cleanup side effects."""
    side_effect_paths = cleanup.side_effect_paths
    paths_text = ", ".join(side_effect_paths) if side_effect_paths else "<unknown>"
    artifacts_dir = artifacts_root / workspace_id / "pre_push_validation_worktree"
    safe_validation_run_id = _safe_pre_push_validation_artifact_name(validation_run_id)
    stdout_path = artifacts_dir / f"{safe_validation_run_id}.side_effects.stdout"
    stderr_path = artifacts_dir / f"{safe_validation_run_id}.side_effects.stderr"
    stdout = (
        "AWF pre-push validation commands passed only before validation worktree "
        "cleanup restored or deleted side effects. The restored commit state was "
        f"not validated. Cleaned paths: {paths_text}."
    )
    try:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
    except OSError as exc:
        _log.warning(
            "pre_push_validation.side_effect_artifact_write_failed",
            workspace_id=workspace_id,
            validation_run_id=validation_run_id,
            error_message=str(exc)[:500],
        )
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
            "restore_ref": cleanup.restore_ref,
        },
        captured_stdout=stdout,
        captured_stderr="",
    )
    details: dict[str, object] = {
        "side_effect_reason_code": VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED,
        "cleaned_paths": list(side_effect_paths),
        "validation_worktree_cleanup": cleanup.details(),
    }
    return replace(result, commands=[*result.commands, command]), details


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

    @property
    def first_failure(self) -> ValidationCommandResult | None:
        """Return the first failed validation command, if any."""
        return _preferred_pre_push_failure(self.result) if self.result is not None else None

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
        first_failure = self.first_failure
        if first_failure is not None and not first_failure.ok:
            details["failing_command"] = redact_audit_text(
                first_failure.command,
                limit=_FAILING_COMMAND_DETAIL_LIMIT,
            )
            details["failing_returncode"] = first_failure.returncode
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
    operation_start_head: str | None = None,
    allow_validation_fix_passes: bool = True,
    allow_resync_on_rejection: bool = True,
) -> _GitPushResult:
    """Run pre-push validation with optional fix passes before pushing.

    ``allow_resync_on_rejection`` is threaded straight to ``_git_push_result``: an
    approve-and-keep operator-hint resume sets it ``False`` so a non-fast-forward
    rejection does NOT reset --hard the preserved protected commit away before its
    grant is consumed (PRRT_kwDOSJAM6s6KZK1v).

    ``allow_validation_fix_passes`` gates the agent fix-pass + commit retry loop.
    The operator-hint resume path sets it ``False`` while an approve-and-keep grant
    is still active: a fix pass commits through ``_commit_dirty_worktree``, whose
    protected-scope check consults the STILL-ACTIVE grant (the grant is consumed
    only after the push), so a fix pass that edits the granted protected path would
    publish extra protected edits under an approval meant only for the preserved
    commit (PR #609 comment 4512881681). Disabling the fix passes leaves validation
    itself intact: a real failure surfaces (the grant survives for a re-resume)
    rather than being papered over with ungranted protected commits.
    """
    if self._deps.validation is None:
        return cast(
            _GitPushResult,
            await self._git_push_result(
                worktree_path=worktree_path,
                remote_branch=remote_branch,
                remote_url=remote_url,
                refspec=refspec,
                allow_resync_on_rejection=allow_resync_on_rejection,
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
        operation_start_head=operation_start_head,
        allow_validation_fix_passes=allow_validation_fix_passes,
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
            allow_resync_on_rejection=allow_resync_on_rejection,
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
    operation_start_head: str | None = None,
    allow_validation_fix_passes: bool = True,
) -> _PrePushValidationResult:
    """Execute pre-push validation plus optional fix/retry attempts.

    When ``allow_validation_fix_passes`` is ``False`` the fix-pass budget is forced
    to zero, so a failing validation returns its failure unchanged without invoking
    an agent fix pass (see ``_validated_git_push_result``).
    """
    max_fix_passes = (
        max(0, self._runner_config.pre_push_validation_fix_passes)
        if allow_validation_fix_passes
        else 0
    )
    pass_index = 0
    validation_commands: tuple[str, ...] | None = None
    while True:
        validation_result = await _run_pre_push_validation(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            compose_project=compose_project,
            compose_file=compose_file,
            remote_branch=remote_branch,
            state=state,
            operation_start_head=operation_start_head,
            remote_url=remote_url,
        )

        if validation_result.passed:
            return validation_result
        if validation_result.reason_code == PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON:
            return validation_result
        if validation_result.reason_code == PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING_REASON:
            return validation_result
        if validation_result.validation_reason_code == VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED:
            return validation_result
        if validation_result.first_failure is None:
            return validation_result
        if pass_index >= max_fix_passes:
            return validation_result
        if validation_commands is None:
            validation_commands = await _pre_push_validation_commands(
                self,
                workspace_id=workspace_id,
                worktree_path=worktree_path,
            )
        # ``_run_pre_push_validation_fix_pass`` re-raises reason-coded commit-sink
        # exceptions (``ProtectedScopeDiffError`` / ``_MonitorPolicyBlockedError`` /
        # ``_MonitorAgentRuntimeOwnershipRepairFailedError``) so the monitor loop's
        # dedicated handlers surface the right reason code. The monitor action loops
        # only catch provider-recovery exceptions around this validated-push call;
        # the protected/policy catches live in the earlier thread/comment address
        # arms of ``_run_fix_cycle``. Letting these escape would abort the monitor
        # without a ``_GitPushResult``, terminal reason code, or the rollback/failure
        # accounting the push path expects. Convert them here into the same
        # structured failure result used by the other commit-sink callers
        # (``ci_ops.py``, ``operator_hints.py``, ``fix_cycle.py``,
        # ``remote_ops.py``) so ``_validated_git_push_result`` returns a
        # ``_GitPushResult`` carrying the terminal reason code and the loop's
        # push-failure accounting runs normally (review thread PRRT_kwDOSJAM6s6KbbE4).
        try:
            committed, fix_pass_failure_reason = await _run_pre_push_validation_fix_pass(
                self,
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                remote_branch=remote_branch,
                remote_url=remote_url,
                state=state,
                validation_result=validation_result,
                pass_number=pass_index + 1,
                total_passes=max_fix_passes,
                validation_commands=validation_commands,
            )
        except ProtectedScopeDiffError as exc:
            _log.warning(
                "monitor.pre_push_validation_fix_pass_protected_scope_diff_unavailable",
                workspace_id=workspace_id,
                pass_number=pass_index + 1,
                error=repr(exc),
            )
            return replace(
                validation_result,
                reason_code=_PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
                message=(
                    "PR monitor pre-push validation fix pass blocked: "
                    f"protected-scope diff unavailable: {exc}"
                ),
            )
        except _MonitorPolicyBlockedError as exc:
            _log.warning(
                "monitor.pre_push_validation_fix_pass_policy_blocked",
                workspace_id=workspace_id,
                pass_number=pass_index + 1,
                error=repr(exc),
                reason_code=exc.reason_code,
            )
            return replace(
                validation_result,
                reason_code=exc.reason_code,
                message=(
                    "PR monitor pre-push validation fix pass blocked: "
                    f"monitor policy blocked the commit: {exc}"
                ),
            )
        except _MonitorAgentRuntimeOwnershipRepairFailedError as exc:
            _log.warning(
                "monitor.pre_push_validation_fix_pass_ownership_repair_failed",
                workspace_id=workspace_id,
                pass_number=pass_index + 1,
                error=repr(exc),
                reason_code=exc.reason_code,
            )
            return replace(
                validation_result,
                reason_code=exc.reason_code,
                message=(
                    "PR monitor pre-push validation fix pass blocked: "
                    f"agent runtime ownership repair failed: {exc}"
                ),
            )
        except _MonitorHeadObjectMissingError as exc:
            _log.warning(
                "monitor.pre_push_validation_fix_pass_head_object_missing",
                workspace_id=workspace_id,
                pass_number=pass_index + 1,
                error=repr(exc),
                reason_code=exc.reason_code,
            )
            return replace(
                validation_result,
                reason_code=exc.reason_code,
                message=(
                    f"PR monitor pre-push validation fix pass blocked: HEAD object missing: {exc}"
                ),
            )
        except _MonitorMirrorHooksPathRepairFailedError as exc:
            _log.warning(
                "monitor.pre_push_validation_fix_pass_mirror_hooks_path_poisoned",
                workspace_id=workspace_id,
                pass_number=pass_index + 1,
                error=repr(exc),
                reason_code=exc.reason_code,
            )
            return replace(
                validation_result,
                reason_code=exc.reason_code,
                message=(
                    "PR monitor pre-push validation fix pass blocked: "
                    f"mirror hooks path poisoned: {exc}"
                ),
            )
        if fix_pass_failure_reason is not None:
            failure_label = (
                "infrastructure failed"
                if fix_pass_failure_reason == PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON
                else (
                    "reparent failed"
                    if fix_pass_failure_reason == PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON
                    else (
                        "cleanup failed"
                        if committed
                        else (
                            "rollback failed"
                            if fix_pass_failure_reason == PRE_PUSH_VALIDATION_ROLLBACK_FAILED_REASON
                            else "rollback cleanup failed"
                        )
                    )
                )
            )
            return replace(
                validation_result,
                reason_code=fix_pass_failure_reason,
                message=(
                    f"PR monitor pre-push validation fix pass {failure_label} "
                    f"after {pass_index + 1}/{max_fix_passes} attempts: "
                    f"{validation_result.message}"
                ),
            )
        if not committed:
            return replace(
                validation_result,
                reason_code=PRE_PUSH_VALIDATION_FIX_FAILED_REASON,
                message=(
                    "PR monitor pre-push validation fix pass failed after "
                    f"{pass_index + 1}/{max_fix_passes} attempts: {validation_result.message}"
                ),
            )
        pass_index += 1


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
) -> ValidationWorktreeCheck:
    """Check pre-push validation preconditions for clean validation worktree state."""

    async def _run_git(args: list[str]) -> Any:
        """Run git command arguments inside the workspace worktree."""
        return await self._deps.runner.run(git_worktree_command(worktree_path, *args))

    # Re-install the agent runtime's checkout-local scratch excludes (e.g.
    # claude_code's ``.claude/worktrees/``) before the cleanliness guard runs.
    # The executor applies these once before the initial agent run, but a
    # monitor-adopted or resumed workspace may never have passed through that
    # setup, and the monitor's own fix-pass agent runs can create the same
    # scratch state. Without this, the guard below would refuse the otherwise
    # clean tree. Idempotent and a no-op for agents that declare no scratch.
    await apply_agent_scratch_excludes(
        run_git=_run_git,
        worktree_path=worktree_path,
        scratch_paths=self._deps.adapter.runtime_scratch_paths,
    )

    from awf.runtime.validation_worktree import check_validation_worktree_clean

    return await check_validation_worktree_clean(
        run_git=_run_git,
        worktree_path=worktree_path,
        ignore_all_ignored=True,
        remove_empty_untracked_dirs=True,
    )


async def _pre_push_validation_cleanup(
    self: Any,
    *,
    worktree_path: Path,
    restore_ref: str,
) -> ValidationWorktreeCleanup:
    """Clean validation side effects and restore the worktree to the requested ref."""

    async def _run_git(args: list[str]) -> Any:
        """Run git command arguments inside the workspace worktree."""
        return await self._deps.runner.run(git_worktree_command(worktree_path, *args))

    from awf.runtime.validation_worktree import cleanup_validation_worktree_side_effects

    return await cleanup_validation_worktree_side_effects(
        run_git=_run_git,
        worktree_path=worktree_path,
        restore_ref=restore_ref,
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


async def _post_pre_push_validation_mirror_hooks_repair_result(
    *,
    workspace_id: str,
    validation_run_id: str | None,
    workspace_head_sha: str | None,
    mirror_path: Path | None,
) -> _PrePushValidationResult | None:
    if mirror_path is None:
        return None
    try:
        await repair_mirror_hooks_path(mirror_path)
    except (GitOperationError, OSError) as exc:
        _log.warning(
            "monitor.post_pre_push_validation_mirror_hooks_path_repair_failed",
            workspace_id=workspace_id,
            reason_code=_MIRROR_HOOKS_PATH_POISONED_REASON,
            error_type=exc.__class__.__name__,
        )
        return _PrePushValidationResult(
            passed=False,
            validation_run_id=validation_run_id,
            workspace_head_sha=workspace_head_sha,
            reason_code=_MIRROR_HOOKS_PATH_POISONED_REASON,
            message="could not repair poisoned mirror hooks path after pre-push validation",
            extra_details={"post_validation_mirror_repair_failed": True},
        )
    return None


async def _run_pre_push_validation(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    compose_project: str,
    compose_file: Path,
    remote_branch: str,
    state: object | None = None,
    operation_start_head: str | None = None,
    remote_url: str | None = None,
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
    mirror_path = mirror_path_for_worktree(worktree_path)
    if mirror_path is not None:
        try:
            await repair_mirror_hooks_path(mirror_path)
        except (GitOperationError, OSError) as exc:
            _log.warning(
                "monitor.mirror_hooks_path_repair_failed",
                workspace_id=workspace_id,
                reason_code=_MIRROR_HOOKS_PATH_POISONED_REASON,
                error_type=exc.__class__.__name__,
            )
            return _PrePushValidationResult(
                passed=False,
                validation_run_id=None,
                workspace_head_sha=workspace_head_sha,
                reason_code=_MIRROR_HOOKS_PATH_POISONED_REASON,
                message="could not repair poisoned mirror hooks path before pre-push validation",
            )

    head_object_exists = await verify_head_object_exists(worktree_path)
    if not head_object_exists:
        _log.warning(
            "monitor.pre_push_validation_head_object_missing",
            workspace_id=workspace_id,
            reason_code=_HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
        )
        recovery_head = operation_start_head
        if recovery_head and mirror_path is not None:
            recovery_head_exists = await _mirror_commit_object_exists(
                self, mirror_path, recovery_head
            )
            if not recovery_head_exists:
                _log.warning(
                    "monitor.pre_push_validation_head_object_missing_recovery_anchor_missing",
                    workspace_id=workspace_id,
                    operation_start_head=recovery_head[:10],
                )
                recovery_head = None
        if not recovery_head:
            recovery_head = await _open_merge_candidate_head_sha(self, workspace_id)
        if recovery_head is None:
            return _PrePushValidationResult(
                passed=False,
                validation_run_id=None,
                workspace_head_sha=workspace_head_sha,
                reason_code=_HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
                message="HEAD object missing before PR monitor pre-push validation",
            )
        command_evidence = tuple(
            step.command.command
            for step in profile_phase_command_plan(profile, ("post_agent", "validate"))
        )
        try:
            recovered = await _recover_missing_head_object_from_filesystem(
                self,
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                operation_start_head=recovery_head,
                command_evidence=command_evidence,
            )
        except _MonitorPolicyBlockedError as exc:
            _log.warning(
                "monitor.pre_push_validation_recovered_head_policy_blocked",
                workspace_id=workspace_id,
                recovered_head=recovery_head[:10],
                reason_code=exc.reason_code,
            )
            cleanup = await _pre_push_validation_cleanup(
                self,
                worktree_path=worktree_path,
                restore_ref=recovery_head,
            )
            if not cleanup.ok:
                _log.warning(
                    "monitor.pre_push_validation_recovered_head_policy_blocked_cleanup_failed",
                    workspace_id=workspace_id,
                    recovered_head=recovery_head[:10],
                    cleanup_reason_code=cleanup.reason_code,
                    cleanup_message=cleanup.message[:400],
                    cleanup_stderr=cleanup.cleanup_stderr[:400],
                )
            return _PrePushValidationResult(
                passed=False,
                validation_run_id=None,
                workspace_head_sha=recovery_head,
                reason_code=exc.reason_code,
                message=(
                    "PR monitor pre-push validation blocked: "
                    f"recovered HEAD failed supply-chain policy: {exc}"
                ),
            )
        if recovered is None:
            return _PrePushValidationResult(
                passed=False,
                validation_run_id=None,
                workspace_head_sha=workspace_head_sha,
                reason_code=_HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
                message="HEAD object missing before PR monitor pre-push validation",
            )
        _log.info(
            "monitor.pre_push_validation_head_object_missing_recovered",
            workspace_id=workspace_id,
            recovered_head=recovered[:10],
            reason_code=_HEAD_OBJECT_MISSING_RECOVERED_REASON,
        )
        recovered_paths: tuple[str, ...] = ()
        if recovered != recovery_head:
            try:
                recovered_paths = await self._changed_paths_between_ref_and_head(
                    worktree_path=worktree_path,
                    ref=recovery_head,
                    error_context="for recovered pre-push validation HEAD",
                )
            except ProtectedScopeDiffError as exc:
                _log.warning(
                    "monitor.pre_push_validation_recovered_head_diff_unavailable",
                    workspace_id=workspace_id,
                    recovered_head=recovered[:10],
                    reason_code=_PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
                    error=repr(exc),
                )
                return _PrePushValidationResult(
                    passed=False,
                    validation_run_id=None,
                    workspace_head_sha=recovered,
                    reason_code=_PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
                    message=(
                        "PR monitor pre-push validation blocked: recovered HEAD "
                        f"diff unavailable: {exc}"
                    ),
                )
        if recovered_paths:
            if not await repair_agent_runtime_ownership(
                logger=_log,
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                reason="dirty_worktree_pre_commit",
                event_name=MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
            ):
                _log.warning(
                    "monitor.pre_push_validation_recovered_head_ownership_repair_failed",
                    workspace_id=workspace_id,
                    recovered_head=recovered[:10],
                    reason_code=AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
                )
                return _PrePushValidationResult(
                    passed=False,
                    validation_run_id=None,
                    workspace_head_sha=recovered,
                    reason_code=AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
                    message=(
                        "PR monitor pre-push validation blocked: "
                        "agent runtime ownership repair failed for recovered HEAD"
                    ),
                )
            try:
                violations = await _protected_scope_violations_for_recovered_commit(
                    self,
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    base_ref=recovery_head,
                    changed_paths=recovered_paths,
                )
            except ProtectedScopeDiffError as exc:
                _log.warning(
                    "monitor.pre_push_validation_recovered_head_diff_unavailable",
                    workspace_id=workspace_id,
                    recovered_head=recovered[:10],
                    reason_code=_PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
                    error=repr(exc),
                )
                return _PrePushValidationResult(
                    passed=False,
                    validation_run_id=None,
                    workspace_head_sha=recovered,
                    reason_code=_PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
                    message=(
                        "PR monitor pre-push validation blocked: recovered HEAD "
                        f"diff unavailable: {exc}"
                    ),
                )
            if violations:
                _log.warning(
                    "monitor.pre_push_validation_recovered_head_protected_scope_repair_failed",
                    workspace_id=workspace_id,
                    recovered_head=recovered[:10],
                    paths=[violation.path for violation in violations],
                    reason_code=_PROTECTED_SCOPE_REPAIR_FAILED_REASON,
                )
                cleanup = await _pre_push_validation_cleanup(
                    self,
                    worktree_path=worktree_path,
                    restore_ref=recovery_head,
                )
                if not cleanup.ok:
                    _log.warning(
                        "monitor.pre_push_validation_recovered_head_protected_scope_cleanup_failed",
                        workspace_id=workspace_id,
                        recovered_head=recovered[:10],
                        cleanup_reason_code=cleanup.reason_code,
                        cleanup_message=cleanup.message[:400],
                        cleanup_stderr=cleanup.cleanup_stderr[:400],
                    )
                return _PrePushValidationResult(
                    passed=False,
                    validation_run_id=None,
                    workspace_head_sha=recovery_head,
                    reason_code=_PROTECTED_SCOPE_REPAIR_FAILED_REASON,
                    message=(
                        "PR monitor pre-push validation blocked: "
                        "recovered HEAD protected-scope repair failed"
                    ),
                )
        workspace_head_sha = recovered

    pre_validation_check = await _pre_push_validation_worktree_check(
        self,
        worktree_path=worktree_path,
    )
    if not pre_validation_check.clean:
        finalized_check = await _try_finalize_pre_push_dirty_repair_state(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            compose_project=compose_project,
            compose_file=compose_file,
            state=state,
            check=pre_validation_check,
            operation_start_head=operation_start_head,
            remote_branch=remote_branch,
            remote_url=remote_url,
            finalize_start_head=workspace_head_sha,
        )
        if finalized_check is not None:
            pre_validation_check = finalized_check
            workspace_head_sha = await self._rev_parse_head(worktree_path)
    if not pre_validation_check.clean:
        return _pre_push_dirty_result(
            workspace_head_sha=workspace_head_sha,
            check=pre_validation_check,
        )
    if workspace_head_sha is None:
        return _PrePushValidationResult(
            passed=False,
            validation_run_id=None,
            workspace_head_sha=None,
            reason_code=PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON,
            message="could not capture local HEAD before PR monitor pre-push validation",
        )

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
        )
        if not cleanup_result.ok:
            await _finish_pre_push_validation_run(
                self,
                validation_run_id,
                status="failed",
                reason_code=cleanup_result.reason_code,
            )
            mirror_repair_result = await _post_pre_push_validation_mirror_hooks_repair_result(
                workspace_id=workspace_id,
                validation_run_id=validation_run_id,
                workspace_head_sha=workspace_head_sha,
                mirror_path=mirror_path,
            )
            if mirror_repair_result is not None:
                return mirror_repair_result
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
        mirror_repair_result = await _post_pre_push_validation_mirror_hooks_repair_result(
            workspace_id=workspace_id,
            validation_run_id=validation_run_id,
            workspace_head_sha=workspace_head_sha,
            mirror_path=mirror_path,
        )
        if mirror_repair_result is not None:
            return mirror_repair_result
        return _PrePushValidationResult(
            passed=False,
            validation_run_id=validation_run_id,
            workspace_head_sha=workspace_head_sha,
            reason_code=PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON,
            message=message,
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
        )
        if not cleanup_result.ok:
            await _finish_pre_push_validation_run(
                self,
                validation_run_id,
                status="failed",
                reason_code=cleanup_result.reason_code,
            )
            mirror_repair_result = await _post_pre_push_validation_mirror_hooks_repair_result(
                workspace_id=workspace_id,
                validation_run_id=validation_run_id,
                workspace_head_sha=workspace_head_sha,
                mirror_path=mirror_path,
            )
            if mirror_repair_result is not None:
                return mirror_repair_result
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
        mirror_repair_result = await _post_pre_push_validation_mirror_hooks_repair_result(
            workspace_id=workspace_id,
            validation_run_id=validation_run_id,
            workspace_head_sha=workspace_head_sha,
            mirror_path=mirror_path,
        )
        if mirror_repair_result is not None:
            return mirror_repair_result
        return _PrePushValidationResult(
            passed=False,
            validation_run_id=validation_run_id,
            workspace_head_sha=workspace_head_sha,
            reason_code=PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON,
            message=message,
        )

    cleanup_result = await _pre_push_validation_cleanup(
        self,
        worktree_path=worktree_path,
        restore_ref=workspace_head_sha,
    )
    if not cleanup_result.ok:
        await _finish_pre_push_validation_run(
            self,
            validation_run_id,
            status="failed",
            reason_code=cleanup_result.reason_code,
        )
        mirror_repair_result = await _post_pre_push_validation_mirror_hooks_repair_result(
            workspace_id=workspace_id,
            validation_run_id=validation_run_id,
            workspace_head_sha=workspace_head_sha,
            mirror_path=mirror_path,
        )
        if mirror_repair_result is not None:
            return mirror_repair_result
        return _pre_push_cleanup_result(
            validation_run_id=validation_run_id,
            workspace_head_sha=workspace_head_sha,
            cleanup=cleanup_result,
        )

    side_effect_details: Mapping[str, object] | None = None
    if result.all_passed and (cleanup_result.side_effect_paths or not cleanup_result.check.clean):
        result, side_effect_details = _pre_push_side_effect_failure_result(
            result=result,
            cleanup=cleanup_result,
            workspace_id=workspace_id,
            validation_run_id=validation_run_id,
            artifacts_root=self._artifacts_root,
        )

    failed_commands = () if result.all_passed else _failed_pre_push_commands(result)
    toolchain_missing_failure = (
        None
        if result.all_passed
        else _pure_toolchain_missing_failure_for_result(result, failed_commands)
    )
    preferred_failure = (
        None
        if result.all_passed
        else _preferred_pre_push_failure_from_failures(result, failed_commands)
    )
    validation_reason_code = _pre_push_validation_reason_code_for_preferred_failure(
        result,
        preferred_failure,
    )
    pre_push_reason_code = (
        "VALIDATION_OK"
        if result.all_passed
        else (
            PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING_REASON
            if toolchain_missing_failure is not None
            else PRE_PUSH_VALIDATION_FAILED_REASON
        )
    )
    # Persist the most specific validation/toolchain code for operator triage,
    # while returning the outer pre-push code that drives push orchestration.
    persisted_reason_code = (
        pre_push_reason_code if toolchain_missing_failure is not None else validation_reason_code
    )
    await _finish_pre_push_validation_run(
        self,
        validation_run_id,
        status="succeeded" if result.all_passed else "failed",
        reason_code=persisted_reason_code,
        retry_count=result.total_retries,
        coverage=_validation_run_coverage_metadata(result),
        command_retries=[c.retry_count for c in result.commands],
    )
    mirror_repair_result = await _post_pre_push_validation_mirror_hooks_repair_result(
        workspace_id=workspace_id,
        validation_run_id=validation_run_id,
        workspace_head_sha=workspace_head_sha,
        mirror_path=mirror_path,
    )
    if mirror_repair_result is not None:
        return mirror_repair_result
    return _PrePushValidationResult(
        passed=result.all_passed,
        validation_run_id=validation_run_id,
        workspace_head_sha=workspace_head_sha,
        reason_code=pre_push_reason_code,
        message=(
            "PR monitor pre-push validation passed"
            if result.all_passed
            else f"PR monitor pre-push validation failed: {persisted_reason_code}"
        ),
        validation_reason_code=None if result.all_passed else validation_reason_code,
        result=result,
        coverage=coverage_result or result.coverage,
        extra_details=side_effect_details,
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
