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
    _validation_run_reason_code,
    _validation_tier_for_workspace,
)
from awf.control.executor.logging_ops import _validation_run_log_stream_refs
from awf.db.repositories import (
    TaskAttemptRepository,
    ValidationRunRepository,
    WorkspaceRepository,
)
from awf.runtime.agent_scratch import apply_agent_scratch_excludes
from awf.runtime.pr_monitor_runner.constants import (
    _MONITOR_POLICY_BLOCKED_REASON,
    _PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
)
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.path_parsing import (
    _changed_paths_from_name_status_z,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_constants import (
    _PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON,
    _PRE_PUSH_VALIDATION_FAILED_REASON,
    _PRE_PUSH_VALIDATION_FIX_FAILED_REASON,
    _PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON,
    _PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON,
    _PRE_PUSH_VALIDATION_ROLLBACK_FAILED_REASON,
    _PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING_REASON,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass import (
    _cleanup_committed_pre_push_validation_fix_pass,  # noqa: F401  (re-exported for tests)
    _head_descends_from,  # noqa: F401  (re-exported for tests)
    _reparent_fix_pass_commit,  # noqa: F401  (re-exported for tests)
    _rollback_failed_pre_push_validation_fix_pass,  # noqa: F401  (re-exported for tests)
    _run_pre_push_validation_fix_pass,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    ProviderRecoveryAuthError,
    ProviderRecoveryFallbackError,
    ProviderRecoveryRetryError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
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

_FAILING_COMMAND_DETAIL_LIMIT = 1000


def _failed_pre_push_commands(result: ValidationResult) -> tuple[ValidationCommandResult, ...]:
    """Return failed command-like records from a validation result."""
    failures: list[ValidationCommandResult] = []
    if result.migration is not None and not result.migration.ok:
        failures.append(result.migration)
    failures.extend(command for command in result.commands if command.blocks_validation)
    coverage_command = result.coverage.command_result if result.coverage is not None else None
    if coverage_command is not None and not coverage_command.ok:
        failures.append(coverage_command)
    return tuple(failures)


def _first_real_pre_push_failure(result: ValidationResult) -> ValidationCommandResult | None:
    """Return the first non-127 failure, giving real lint/test failures precedence."""
    failures = _failed_pre_push_commands(result)
    return _first_real_pre_push_failure_for_result(result, failures)


def _first_failure_outside_collected_failures(
    result: ValidationResult,
    failures: tuple[ValidationCommandResult, ...],
) -> ValidationCommandResult | None:
    """Return ``first_failure`` when it is not represented in collected commands."""
    first_failure = result.first_failure
    if first_failure is None:
        return None
    if any(first_failure is failure for failure in failures):
        return None
    return first_failure


def _first_real_pre_push_failure_for_result(
    result: ValidationResult,
    failures: tuple[ValidationCommandResult, ...],
) -> ValidationCommandResult | None:
    """Return the first non-127 failure across command and provider records.

    Provider-level ``first_failure`` may describe a policy failure whose
    underlying command succeeded, such as coverage below threshold with
    ``ok=True`` and ``returncode=0``.
    """
    real_failure = _first_real_pre_push_failure_from_failures(failures)
    if real_failure is not None:
        return real_failure
    first_failure = _first_failure_outside_collected_failures(result, failures)
    if first_failure is not None and first_failure.returncode != 127:
        return first_failure
    return None


def _first_real_pre_push_failure_from_failures(
    failures: tuple[ValidationCommandResult, ...],
) -> ValidationCommandResult | None:
    """Return the first non-127 failure from an already collected failure tuple."""
    return next(
        (failure for failure in failures if failure.returncode != 127),
        None,
    )


def _pure_toolchain_missing_failure(
    result: ValidationResult,
) -> ValidationCommandResult | None:
    """Return the first 127 failure only when all command failures are command-not-found.

    Mixed results are treated as genuine validation failures so a real lint/test
    failure is not hidden behind an earlier missing-tool diagnostic.
    """
    failures = _failed_pre_push_commands(result)
    return _pure_toolchain_missing_failure_for_result(result, failures)


def _pure_toolchain_missing_failure_for_result(
    result: ValidationResult,
    failures: tuple[ValidationCommandResult, ...],
) -> ValidationCommandResult | None:
    """Return a pure 127 failure, including command-less provider failures."""
    first_failure = _first_failure_outside_collected_failures(result, failures)
    if first_failure is not None and first_failure.returncode != 127:
        return None
    toolchain_failure = _pure_toolchain_missing_failure_from_failures(failures)
    if toolchain_failure is not None:
        return toolchain_failure
    if failures:
        return None
    if first_failure is not None and first_failure.returncode == 127:
        return first_failure
    return None


def _pure_toolchain_missing_failure_from_failures(
    failures: tuple[ValidationCommandResult, ...],
) -> ValidationCommandResult | None:
    """Return the first 127 failure only when the collected failures are all 127."""
    if not failures:
        return None
    if any(failure.returncode != 127 for failure in failures):
        return None
    return failures[0]


def _preferred_pre_push_failure(result: ValidationResult) -> ValidationCommandResult | None:
    """Return the failure that should drive diagnostics and repair prompts."""
    return _preferred_pre_push_failure_from_failures(
        result,
        _failed_pre_push_commands(result),
    )


def _preferred_pre_push_failure_from_failures(
    result: ValidationResult,
    failures: tuple[ValidationCommandResult, ...],
) -> ValidationCommandResult | None:
    """Return the preferred failure using an already collected failure tuple."""
    real_failure = _first_real_pre_push_failure_for_result(result, failures)
    if real_failure is not None:
        return real_failure
    toolchain_failure = _pure_toolchain_missing_failure_from_failures(failures)
    if toolchain_failure is not None:
        return toolchain_failure
    return result.first_failure


def _pre_push_validation_reason_code_for_preferred_failure(
    result: ValidationResult,
    preferred_failure: ValidationCommandResult | None,
) -> str:
    """Return the validation reason for an already selected preferred failure."""
    validation_reason_code = _validation_run_reason_code(result)
    if preferred_failure is None:
        return validation_reason_code
    coverage_command = result.coverage.command_result if result.coverage is not None else None
    # ValidationResult.first_failure returns this same coverage command object when
    # a coverage policy fails; preserve that identity if coverage results are copied.
    if (
        result.coverage is not None
        and not result.coverage.ok
        and preferred_failure is coverage_command
    ):
        return validation_reason_code
    return preferred_failure.reason_code


def _pre_push_validation_reason_code(result: ValidationResult) -> str:
    """Return the underlying validation reason, honoring mixed-failure precedence."""
    return _pre_push_validation_reason_code_for_preferred_failure(
        result,
        _preferred_pre_push_failure(result),
    )


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
        operation_start_head=operation_start_head,
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
    operation_start_head: str | None = None,
) -> _PrePushValidationResult:
    """Execute pre-push validation plus optional fix/retry attempts."""
    max_fix_passes = max(0, self._runner_config.pre_push_validation_fix_passes)
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
    )


async def _operation_owned_delta_paths(
    self: Any,
    *,
    worktree_path: Path,
    operation_start_head: str,
) -> set[str] | None:
    """Return paths changed by the current operation's committed, staged, and unstaged delta.

    The repair-start dirty guard proves the worktree was clean at
    ``operation_start_head``, so the current monitor operation owns the union
    of:

    - its committed delta: ``git diff --name-status -z operation_start_head..HEAD``
    - its staged delta: ``git diff --name-status -z --cached operation_start_head``
    - its unstaged working-tree delta:
      ``git diff --name-status -z operation_start_head`` (compares the commit
      to the working tree, so it includes both staged and unstaged edits)

    All three deltas are parsed from ``--name-status -z`` (via
    ``_changed_paths_from_name_status_z``) rather than raw ``--name-only``
    lines so the owned set uses the same path representation as the dirty
    check (``check_validation_worktree_clean`` ->
    ``changed_paths_from_porcelain``). ``--name-status -z`` emits NUL-delimited
    records with both the source and destination for ``R``/``C`` records, and
    never C-quotes paths (the ``-z`` form always emits raw bytes), so a staged
    rename's source path and a non-ASCII path are not mistaken for unrelated
    dirt and stranded as ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` (review
    thread ``PRRT_kwDOSJAM6s6KaAWk``).

    The staged delta is load-bearing for the case where the operation's
    ``_commit_dirty_worktree`` returns False *before* creating a commit (e.g.
    ``git commit`` fails after the agent already staged its edits via
    ``git add -A``): ``operation_start_head..HEAD`` is then empty, but the
    operation still owns the staged paths it attempted to commit. Without the
    staged union every operation-owned dirty path would be treated as
    unrelated and the finalize would strand the operation's own residue as
    ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` (review thread
    ``PRRT_kwDOSJAM6s6KYd-r``).

    The unstaged working-tree delta is load-bearing for the case where the
    operation's ``_commit_dirty_worktree`` returns False because ``git add -A``
    itself failed: the repair output was never staged and remains as unstaged
    working-tree changes, so both ``operation_start_head..HEAD`` and
    ``--cached operation_start_head`` are empty. Without the working-tree
    union those operation-owned unstaged paths would be treated as unrelated
    and the finalize would strand the operation's own repair output as
    ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` (review thread
    ``PRRT_kwDOSJAM6s6KaUHP``).

    Returns ``None`` when any delta cannot be resolved (e.g. the start ref
    is unknown, git failed, or the parsed ``--name-status -z`` output was
    malformed) so the caller can keep the fail-closed dirty path instead of
    committing unowned dirt.
    """
    committed_result = await self._deps.runner.run(
        git_worktree_command(
            worktree_path, "diff", "--name-status", "-z", f"{operation_start_head}..HEAD"
        )
    )
    if not committed_result.ok:
        _log.warning(
            "monitor.pre_push_dirty_finalize_delta_unavailable",
            operation_start_head=operation_start_head,
            returncode=committed_result.returncode,
            stderr=(committed_result.stderr or "")[:400],
        )
        return None
    staged_result = await self._deps.runner.run(
        git_worktree_command(
            worktree_path, "diff", "--name-status", "-z", "--cached", operation_start_head
        )
    )
    if not staged_result.ok:
        _log.warning(
            "monitor.pre_push_dirty_finalize_staged_delta_unavailable",
            operation_start_head=operation_start_head,
            returncode=staged_result.returncode,
            stderr=(staged_result.stderr or "")[:400],
        )
        return None
    working_tree_result = await self._deps.runner.run(
        git_worktree_command(worktree_path, "diff", "--name-status", "-z", operation_start_head)
    )
    if not working_tree_result.ok:
        _log.warning(
            "monitor.pre_push_dirty_finalize_working_tree_delta_unavailable",
            operation_start_head=operation_start_head,
            returncode=working_tree_result.returncode,
            stderr=(working_tree_result.stderr or "")[:400],
        )
        return None
    owned_paths: set[str] = set()
    for source in (
        committed_result.stdout,
        staged_result.stdout,
        working_tree_result.stdout,
    ):
        try:
            owned_paths.update(_changed_paths_from_name_status_z(source or ""))
        except ProtectedScopeDiffError:
            _log.warning(
                "monitor.pre_push_dirty_finalize_delta_malformed",
                operation_start_head=operation_start_head,
                source=(source or "")[:400],
            )
            return None
    return owned_paths


async def _committed_delta_paths(
    self: Any,
    *,
    worktree_path: Path,
    operation_start_head: str,
) -> set[str] | None:
    """Return only the paths committed since ``operation_start_head``.

    Unlike ``_operation_owned_delta_paths``, this excludes the staged and
    unstaged working-tree deltas. A successful finalize commit moves the
    operation's owned residue into the committed delta, so the post-commit
    unowned-delta re-validation only needs to inspect what was actually
    committed — paths that remain only in the working tree were not added by
    the finalize commit and must not be flagged as
    ``PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA`` (review thread
    ``PRRT_kwDOSJAM6s6Ka0aO``).

    Returns ``None`` when the committed delta cannot be resolved so the caller
    can keep the fail-closed dirty path instead of trusting the commit.
    """
    committed_result = await self._deps.runner.run(
        git_worktree_command(
            worktree_path, "diff", "--name-status", "-z", f"{operation_start_head}..HEAD"
        )
    )
    if not committed_result.ok:
        _log.warning(
            "monitor.pre_push_dirty_finalize_committed_delta_unavailable",
            operation_start_head=operation_start_head,
            returncode=committed_result.returncode,
            stderr=(committed_result.stderr or "")[:400],
        )
        return None
    try:
        return set(_changed_paths_from_name_status_z(committed_result.stdout or ""))
    except ProtectedScopeDiffError:
        _log.warning(
            "monitor.pre_push_dirty_finalize_committed_delta_malformed",
            operation_start_head=operation_start_head,
            source=(committed_result.stdout or "")[:400],
        )
        return None


async def _try_finalize_pre_push_dirty_repair_state(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    compose_project: str,
    compose_file: Path,
    state: object | None,
    check: ValidationWorktreeCheck,
    operation_start_head: str | None = None,
    remote_branch: str | None = None,
    remote_url: str | None = None,
) -> ValidationWorktreeCheck | None:
    """Commit monitor-owned residual repair dirt before pre-push validation.

    The finalize is gated on the dirty paths being operation-owned: the
    repair-start dirty guard (``_pre_existing_dirty_repair_worktree_result``)
    proves the worktree was clean at ``operation_start_head``, so the current
    monitor operation's own delta is the union of its committed delta
    (``git diff --name-status -z operation_start_head..HEAD``), its staged
    delta (``git diff --name-status -z --cached operation_start_head``), and
    its unstaged working-tree delta
    (``git diff --name-status -z operation_start_head``, which compares the
    commit to the working tree and includes unstaged edits). All three
    deltas are parsed from ``--name-status -z`` so the owned set uses the same
    path representation as the dirty check (``changed_paths_from_porcelain``):
    ``--name-status -z`` emits both the source and destination for ``R``/``C``
    records and never C-quotes paths, so a staged rename's source and a
    non-ASCII path are not mistaken for unrelated dirt (review thread
    ``PRRT_kwDOSJAM6s6KaAWk``). The staged union covers the case where the
    operation's ``_commit_dirty_worktree`` returned
    False before creating a commit (e.g. ``git commit`` failed after
    ``git add -A``), leaving the operation's staged edits dirty but
    ``operation_start_head..HEAD`` empty (review thread
    ``PRRT_kwDOSJAM6s6KYd-r``). The unstaged working-tree union covers the
    case where ``_commit_dirty_worktree`` returned False because
    ``git add -A`` itself failed, leaving the operation's repair output
    unstaged in the working tree (review thread ``PRRT_kwDOSJAM6s6KaUHP``).
    Only dirt confined to those paths is safe to finalize — dirt on any
    other path was introduced after the repair-start guard (e.g. by a
    failed cleanup or another local process) and must stay fail-closed as
    ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` so it is never silently swept
    into the PR (review thread ``PRRT_kwDOSJAM6s6KXLaI``).
    When ``operation_start_head`` is unavailable (no operation-owned anchor),
    the finalize is skipped entirely.

    The pre-commit gate is necessary but not sufficient: ``_commit_dirty_worktree``
    runs a fresh ``git status``, may invoke protected-scope repair (which runs
    the agent CLI), and then stages all non-ignored dirty paths. A side effect
    between the gate check and the fresh staging scan can introduce an extra
    path outside ``owned_delta_paths``, bypassing the stale gate. After a
    successful commit the finalize therefore re-validates the operation's
    committed delta and fails closed with
    ``PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA`` if any unowned path appears, so
    the unowned commit is never silently pushed (review thread
    ``PRRT_kwDOSJAM6s6KZP8f``).

    ``remote_branch``/``remote_url`` are forwarded to
    ``_commit_dirty_worktree`` as ``protected_scope_revert_remote_branch`` /
    ``remote_push_url`` so ``_repair_protected_scope_changes_before_commit``
    can filter out protected files already restored to the remote PR branch.
    Omitting them (as this call previously did) leaves a restored protected
    file counted as a violation, so the monitor launches another provider
    repair or falls back to a no-commit dirty failure instead of committing
    the safe rollback and proceeding to validation (review thread
    ``PRRT_kwDOSJAM6s6KZjtR``).
    """

    # Skip finalization if: no state provided, the tree is already clean, or
    # the worktree status check itself failed. When the status check failed,
    # the working tree state cannot be reliably determined, so committing
    # would be unsafe; skip and let the caller surface the failed status.
    if state is None or check.clean or check.reason_code == VALIDATION_WORKTREE_STATUS_FAILED:
        return None
    # Without an operation-owned anchor the finalize cannot prove the dirt is
    # operation-owned, so it must not commit. Keep the fail-closed dirty path.
    if operation_start_head is None:
        return None
    owned_delta_paths = await _operation_owned_delta_paths(
        self,
        worktree_path=worktree_path,
        operation_start_head=operation_start_head,
    )
    if owned_delta_paths is None:
        # The delta could not be resolved; do not commit unowned dirt.
        return None
    # ``git diff --name-status -z`` (committed/staged/working-tree) cannot see
    # purely untracked paths, so the operation-owned delta computed from diffs
    # omits repair output that was never staged (e.g. a file the agent created
    # but ``git add -A`` never reached). The pre-push cleanliness check uses
    # ``git status --porcelain``, which DOES list untracked files, so those
    # operation-owned untracked paths would otherwise be flagged as
    # ``unrelated_dirty`` and the finalize would skip, stranding the
    # operation's own residue as ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` and
    # the push would fail-closed. The repair-start dirty guard
    # (``_pre_existing_dirty_repair_worktree_result``) proved the worktree was
    # clean at ``operation_start_head``, so every untracked path now present is
    # owned by this operation. ``check.untracked_paths`` already excludes
    # AWF-agent-runtime artifacts (``check_validation_worktree_clean`` suppresses
    # them unconditionally), so folding them in here is safe and does not sweep
    # agent-runtime dirt into the PR (review thread ``PRRT_kwDOSJAM6s6Ka0aK``).
    owned_delta_paths = owned_delta_paths | set(check.untracked_paths)
    dirty_paths = set(check.paths)
    if not dirty_paths:
        return None
    unrelated_dirty = dirty_paths - owned_delta_paths
    if unrelated_dirty:
        _log.warning(
            "monitor.pre_push_dirty_finalize_skipped_unrelated_dirt",
            workspace_id=workspace_id,
            operation_start_head=operation_start_head,
            dirty_paths=sorted(dirty_paths),
            unrelated_dirty=sorted(unrelated_dirty),
        )
        return None
    from awf.runtime.validation_worktree import ValidationWorktreeCheck

    try:
        committed = bool(
            await self._commit_dirty_worktree(
                workspace_id=workspace_id,
                message=f"awf: finalize PR monitor repair for {workspace_id}",
                compose_project=compose_project,
                compose_file=compose_file,
                state=state,
                protected_scope_revert_remote_branch=remote_branch,
                remote_push_url=remote_url,
            )
        )
    except _MonitorPolicyBlockedError as exc:
        # ``_commit_dirty_worktree`` raises this when monitor-authored changes
        # violate blocking workspace policy. Like the other commit callers
        # (``remote_ops.py``, ``ci_ops.py``, ``fix_cycle.py``,
        # ``operator_hints.py``), preserve the policy reason code end-to-end
        # instead of letting it collapse into the generic pre-existing-dirty
        # failure. Returning a non-clean check carrying the reason code flows
        # through ``_pre_push_dirty_result`` into ``_GitPushResult.reason_code``.
        _log.warning(
            "monitor.pre_push_dirty_finalize_policy_blocked",
            workspace_id=workspace_id,
            error=repr(exc),
            paths=list(check.paths),
        )
        return ValidationWorktreeCheck(
            clean=False,
            paths=check.paths,
            reason_code=_MONITOR_POLICY_BLOCKED_REASON,
            message=str(exc) or "monitor policy blocked the pre-push dirty finalize",
        )
    except _MonitorAgentRuntimeOwnershipRepairFailedError as exc:
        # ``_commit_dirty_worktree`` raises this (carrying a ``reason_code``
        # property) when monitor cannot repair agent worktree ownership.
        # Preserve that reason code end-to-end like the other commit callers
        # instead of collapsing it into the generic pre-existing-dirty failure.
        _log.warning(
            "monitor.pre_push_dirty_finalize_ownership_repair_failed",
            workspace_id=workspace_id,
            error=repr(exc),
            paths=list(check.paths),
            reason_code=exc.reason_code,
        )
        return ValidationWorktreeCheck(
            clean=False,
            paths=check.paths,
            reason_code=exc.reason_code,
            message=str(exc) or "agent runtime ownership repair failed",
        )
    except ProtectedScopeDiffError as exc:
        # ``_commit_dirty_worktree`` -> ``_repair_protected_scope_changes_before_commit``
        # raises this when the committed diff against the remote PR branch cannot be
        # verified (e.g. the protected-scope remote-diff baseline is unavailable). Like
        # the other commit callers (``ci_ops.py``, ``operator_hints.py``,
        # ``fix_cycle.py``, ``remote_ops.py``), preserve the
        # ``PROTECTED_SCOPE_DIFF_UNAVAILABLE`` reason code end-to-end instead of
        # letting it collapse into the generic pre-existing-dirty failure.
        # Returning a non-clean check carrying the reason code flows through
        # ``_pre_push_dirty_result`` into ``_GitPushResult.reason_code``.
        _log.warning(
            "monitor.pre_push_dirty_finalize_protected_scope_diff_unavailable",
            workspace_id=workspace_id,
            error=repr(exc),
            paths=list(check.paths),
        )
        return ValidationWorktreeCheck(
            clean=False,
            paths=check.paths,
            reason_code=_PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
            message=str(exc) or "protected-scope diff unavailable before pre-push dirty finalize",
        )
    except ProviderRecoveryRetryError:
        # ``_commit_dirty_worktree`` -> ``_repair_protected_scope_changes_before_commit``
        # raises this when provider recovery suppresses the CLI and the operation must
        # back off and retry later. Every other commit caller lets it propagate so the
        # loop's ``except ProviderRecoveryRetryError`` handler surfaces ``PROVIDER_OUTAGE``
        # retry semantics; re-raise here instead of swallowing it into the generic
        # pre-existing-dirty failure.
        _log.warning(
            "monitor.pre_push_dirty_finalize_provider_recovery_retry",
            workspace_id=workspace_id,
            paths=list(check.paths),
        )
        raise
    except ProviderRecoveryFallbackError:
        # ``_commit_dirty_worktree`` -> ``_repair_protected_scope_changes_before_commit`` ->
        # ``_handle_provider_agent_run_error`` raises this when a provider failure
        # triggers a fallback workspace. The loop's
        # ``except ProviderRecoveryFallbackError`` handler surfaces ``PROVIDER_FALLBACK``
        # semantics, so the finalize must re-raise it instead of letting the broad
        # ``except Exception`` below swallow it into the generic pre-existing-dirty
        # failure (review thread ``PRRT_kwDOSJAM6s6KYd-t``).
        _log.warning(
            "monitor.pre_push_dirty_finalize_provider_recovery_fallback",
            workspace_id=workspace_id,
            paths=list(check.paths),
        )
        raise
    except ProviderRecoveryAuthError:
        # ``_commit_dirty_worktree`` -> ``_repair_protected_scope_changes_before_commit`` ->
        # ``_handle_provider_agent_run_error`` raises this when provider auth is broken
        # and the operation cannot continue. The loop's
        # ``except ProviderRecoveryAuthError`` handler surfaces the auth-failed operation
        # outcome, so the finalize must re-raise it instead of letting the broad
        # ``except Exception`` below swallow it into the generic pre-existing-dirty
        # failure (review thread ``PRRT_kwDOSJAM6s6KYd-t``).
        _log.warning(
            "monitor.pre_push_dirty_finalize_provider_recovery_auth",
            workspace_id=workspace_id,
            paths=list(check.paths),
        )
        raise
    except Exception as exc:
        _log.warning(
            "monitor.pre_push_dirty_finalize_failed",
            workspace_id=workspace_id,
            error=repr(exc),
            paths=list(check.paths),
        )
        return None
    if not committed:
        # ``_commit_dirty_worktree`` can have side effects (e.g. protected-scope
        # repair restores the only dirty files) yet return False because there
        # was nothing left to commit. Re-check the tree before giving up so a
        # cleanup-only repair can proceed to validation instead of being
        # stranded as ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` on the stale
        # dirty check captured before the finalize attempt.
        recheck = await _pre_push_validation_worktree_check(
            self,
            worktree_path=worktree_path,
        )
        if recheck.clean:
            _log.info(
                "monitor.pre_push_dirty_finalize_no_commit_clean",
                workspace_id=workspace_id,
                paths=list(check.paths),
            )
            return recheck
        _log.warning(
            "monitor.pre_push_dirty_finalize_no_commit",
            workspace_id=workspace_id,
            paths=list(check.paths),
        )
        return recheck

    # Re-validate the operation's committed delta AFTER the commit sink's side
    # effects. The ownership gate above was computed before calling
    # ``_commit_dirty_worktree``, but that sink runs a fresh ``git status``, may
    # invoke protected-scope repair (which runs the agent CLI), and then stages
    # all non-ignored dirty paths. A side effect between the gate check and the
    # fresh staging scan can introduce an extra path outside
    # ``owned_delta_paths``; without this post-commit re-validation the stale
    # gate would let the unowned commit through and the verify recheck below
    # would observe a clean tree (the unowned path was just committed), silently
    # sweeping unowned dirt into the PR. Fail closed with a dedicated reason
    # code so the unowned commit is never pushed (review thread
    # ``PRRT_kwDOSJAM6s6KZP8f``).
    #
    # Only the *committed* delta is re-validated here. ``owned_delta_paths``
    # (the pre-commit union of committed + staged + working-tree deltas) is the
    # baseline the finalize was allowed to commit; after a successful finalize
    # commit the operation's owned residue has moved into the committed delta.
    # Re-using the full ``_operation_owned_delta_paths`` union would include the
    # commit-vs-working-tree diff, so an unrelated tracked edit that remains
    # only in the working tree after a valid finalize would be flagged as
    # ``PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA`` even though the finalize commit
    # did not add it. Restricting the re-validation to the committed delta
    # confines the fail-closed check to paths the finalize commit actually
    # introduced (review thread ``PRRT_kwDOSJAM6s6Ka0aO``).
    post_commit_delta = await _committed_delta_paths(
        self,
        worktree_path=worktree_path,
        operation_start_head=operation_start_head,
    )
    if post_commit_delta is None:
        # The post-commit committed delta could not be resolved; do not trust
        # the commit.
        _log.warning(
            "monitor.pre_push_dirty_finalize_post_commit_delta_unavailable",
            workspace_id=workspace_id,
            operation_start_head=operation_start_head,
            paths=list(check.paths),
        )
        return ValidationWorktreeCheck(
            clean=False,
            paths=check.paths,
            reason_code=_PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON,
            message=(
                "pre-push dirty finalize could not re-validate the committed "
                "operation delta after the commit sink side effects"
            ),
        )
    unowned_committed = post_commit_delta - owned_delta_paths
    if unowned_committed:
        _log.warning(
            "monitor.pre_push_dirty_finalize_unowned_delta",
            workspace_id=workspace_id,
            operation_start_head=operation_start_head,
            owned_delta_paths=sorted(owned_delta_paths),
            unowned_committed=sorted(unowned_committed),
        )
        return ValidationWorktreeCheck(
            clean=False,
            paths=tuple(sorted(unowned_committed)),
            reason_code=_PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON,
            message=(
                "pre-push dirty finalize committed paths outside the "
                "operation-owned delta after the commit sink side effects"
            ),
        )

    verify = await _pre_push_validation_worktree_check(
        self,
        worktree_path=worktree_path,
    )
    if verify.clean:
        _log.info(
            "monitor.pre_push_dirty_finalized",
            workspace_id=workspace_id,
            paths=list(check.paths),
        )
    else:
        _log.warning(
            "monitor.pre_push_dirty_finalize_still_dirty",
            workspace_id=workspace_id,
            paths=list(verify.paths),
        )
    return verify


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
