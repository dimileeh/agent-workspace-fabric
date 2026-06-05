"""Pre-push validation for PR monitor authored repair commits."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from awf.adapters.base import AgentRunError
from awf.common.audit import redact_audit_text
from awf.common.command_evidence import append_command_evidence
from awf.common.compose_exec import ComposeExecCleanupError, cleanup_failure_message
from awf.common.git_identity import git_identity_config_args
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
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.pre_push_validation_constants import (
    _PRE_PUSH_VALIDATION_FAILED_REASON,
    _PRE_PUSH_VALIDATION_FIX_FAILED_REASON,
    _PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON,
    _PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON,
    _PRE_PUSH_VALIDATION_ROLLBACK_FAILED_REASON,
    _PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING_REASON,
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

_FAILING_COMMAND_DETAIL_LIMIT = 1000


def _failed_pre_push_commands(result: ValidationResult) -> tuple[ValidationCommandResult, ...]:
    """Return failed command-like records from a validation result."""
    failures: list[ValidationCommandResult] = []
    if result.migration is not None and not result.migration.ok:
        failures.append(result.migration)
    failures.extend(command for command in result.commands if not command.ok)
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

    from awf.runtime.validation_worktree import check_validation_worktree_clean

    return await check_validation_worktree_clean(
        run_git=_run_git,
        worktree_path=worktree_path,
        ignore_all_ignored=True,
    )


async def _head_descends_from(
    self: Any,
    *,
    worktree_path: Path,
    ancestor: str,
    descendant: str,
) -> bool:
    """Return True when ``descendant`` is a descendant of ``ancestor``.

    Uses ``git merge-base --is-ancestor`` which exits 0 when the first ref is an
    ancestor of the second and non-zero otherwise. Callers only invoke this with
    distinct SHAs, so a 0 exit means the fix-pass agent advanced HEAD on top of
    the pre-fix commit rather than moving it sideways or backward.
    """
    result = await self._deps.runner.run(
        git_worktree_command(
            worktree_path,
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
        )
    )
    return bool(result.ok)


async def _reparent_fix_pass_commit(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    fix_start_head: str,
    current_head: str,
    pass_number: int,
) -> tuple[str | None, bool, str | None]:
    """Re-parent the fix agent's resulting tree onto ``fix_start_head`` (issue #411).

    A fix-pass agent that rewrites the tip (e.g. ``git commit --amend`` or
    ``git reset --hard HEAD~1`` + recommit) leaves HEAD as a non-descendant of
    ``fix_start_head``. A legitimate content-modifying amend and a work-dropping
    reset+recommit can produce byte-identical trees, so they are topologically and
    tree-content indistinguishable. Rather than discriminate (and risk orphaning a
    valid fix, the #406-class over-rollback), we ALWAYS reconstruct whatever the
    agent produced as a single commit whose parent is ``fix_start_head``:

    - ``fix_start_head`` is the new commit's parent, so it can never be orphaned and
      is rescued from danglinghood after an amend.
    - The agent's resulting tree is always kept, so the fix pass converges.
    - Any dropped work is visible as ``git diff fix_start_head..HEAD`` and is
      re-validated before push by the surrounding fix-pass loop.

    Returns ``(new_head, no_net_change, failure_reason)``:
      - ``(new_sha, False, None)``      -> reconstructed commit; accept against new_sha.
      - ``(None, True, None)``          -> agent produced no net change; signal no-commit.
      - ``(None, False, REPARENT)``     -> infra failure (rev-parse/commit-tree/reset).
    """

    def _warn(git_step: str, result: Any) -> None:
        """Warn on a re-parent git step failure without logging secrets."""
        _log.warning(
            "monitor.pre_push_validation_fix_reparent_failed",
            workspace_id=workspace_id,
            pass_number=pass_number,
            git_step=git_step,
            stderr=(result.stderr or "")[:400],
        )

    current_tree_result = await self._deps.runner.run(
        git_worktree_command(worktree_path, "rev-parse", f"{current_head}^{{tree}}")
    )
    current_tree = current_tree_result.stdout.strip() if current_tree_result.ok else ""
    if not current_tree:
        _warn("rev-parse current tree", current_tree_result)
        return None, False, _PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON

    start_tree_result = await self._deps.runner.run(
        git_worktree_command(worktree_path, "rev-parse", f"{fix_start_head}^{{tree}}")
    )
    start_tree = start_tree_result.stdout.strip() if start_tree_result.ok else ""
    if not start_tree:
        _warn("rev-parse fix_start_head tree", start_tree_result)
        return None, False, _PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON

    if current_tree == start_tree:
        # The agent produced no net change relative to the pre-fix-pass commit. Signal
        # no-commit so the caller falls through to the existing rollback.
        return None, True, None

    message_result = await self._deps.runner.run(
        git_worktree_command(worktree_path, "log", "-1", "--format=%B", current_head)
    )
    message = message_result.stdout.strip() if message_result.ok else ""
    if not message:
        message = f"awf: pre-push validation fix for {workspace_id}"

    commit_result = await self._deps.runner.run(
        git_worktree_command(
            worktree_path,
            *git_identity_config_args(),
            "commit-tree",
            current_tree,
            "-p",
            fix_start_head,
            "-m",
            message,
        )
    )
    new_sha = commit_result.stdout.strip() if commit_result.ok else ""
    if not new_sha:
        _warn("commit-tree", commit_result)
        return None, False, _PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON

    reset_result = await self._deps.runner.run(
        git_worktree_command(worktree_path, "reset", "--hard", new_sha)
    )
    if not reset_result.ok:
        _warn("reset --hard reparented commit", reset_result)
        return None, False, _PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON

    return new_sha, False, None


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
    pass_number: int,
    total_passes: int,
    validation_commands: tuple[str, ...],
) -> tuple[bool, str | None]:
    """Attempt a validation fix pass and return commit status plus terminal failure reason."""
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
        rollback_failure_reason = await _rollback_failed_pre_push_validation_fix_pass(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            restore_ref=fix_start_head,
            pass_number=pass_number,
            reason="compose_cleanup_failed",
        )
        if rollback_failure_reason is not None:
            return False, rollback_failure_reason
        return False, None
    except Exception as exc:
        _log.warning(
            "monitor.pre_push_validation_fix_failed",
            workspace_id=workspace_id,
            pass_number=pass_number,
            error=repr(exc),
        )
        rollback_failure_reason = await _rollback_failed_pre_push_validation_fix_pass(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            restore_ref=fix_start_head,
            pass_number=pass_number,
            reason="agent_exception",
        )
        if rollback_failure_reason is not None:
            return False, rollback_failure_reason
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
        rollback_failure_reason = await _rollback_failed_pre_push_validation_fix_pass(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            restore_ref=fix_start_head,
            pass_number=pass_number,
            reason="commit_exception",
        )
        if rollback_failure_reason is not None:
            return False, rollback_failure_reason
        return False, None
    if not committed:
        current_head = await self._rev_parse_head(worktree_path)
        if current_head is not None and current_head != fix_start_head:
            # The fix-pass agent self-committed a valid repair (the worktree is clean,
            # so ``_commit_dirty_worktree`` had nothing to commit and returned False).
            advanced = await _head_descends_from(
                self,
                worktree_path=worktree_path,
                ancestor=fix_start_head,
                descendant=current_head,
            )
            if advanced:
                # HEAD strictly advanced on top of the pre-fix commit (issue #406):
                # accept as-is.
                _log.info(
                    "monitor.pre_push_validation_fix_self_commit_detected",
                    workspace_id=workspace_id,
                    pass_number=pass_number,
                    fix_start_head=fix_start_head,
                    committed_head=current_head,
                    self_commit_kind="advance",
                )
                cleanup_failure_reason = await _cleanup_committed_pre_push_validation_fix_pass(
                    self,
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    committed_head=current_head,
                    pass_number=pass_number,
                )
                return True, cleanup_failure_reason
            # Non-descendant rewrite (plain amend / content-modifying amend /
            # reset+recommit). These are topologically and tree-content
            # indistinguishable, so we STOP DISCRIMINATING and ALWAYS re-parent the
            # agent's resulting tree onto ``fix_start_head`` (issue #411). This keeps
            # the agent's fix (the pass converges), can never orphan ``fix_start_head``
            # (it is the new commit's parent), and leaves any dropped work auditable as
            # ``git diff fix_start_head..HEAD`` to be re-validated before push.
            new_head, no_net_change, reparent_failure_reason = await _reparent_fix_pass_commit(
                self,
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                fix_start_head=fix_start_head,
                current_head=current_head,
                pass_number=pass_number,
            )
            if reparent_failure_reason is not None:
                return True, reparent_failure_reason
            if not no_net_change and new_head is not None:
                _log.info(
                    "monitor.pre_push_validation_fix_reparented",
                    workspace_id=workspace_id,
                    pass_number=pass_number,
                    from_sha=current_head,
                    to_sha=new_head,
                    fix_start_head=fix_start_head,
                )
                cleanup_failure_reason = await _cleanup_committed_pre_push_validation_fix_pass(
                    self,
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    committed_head=new_head,
                    pass_number=pass_number,
                )
                return True, cleanup_failure_reason
            # no_net_change: the agent's tree equals ``fix_start_head``'s tree, so there
            # is nothing to preserve. Fall through to the existing rollback below.
        rollback_failure_reason = await _rollback_failed_pre_push_validation_fix_pass(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            restore_ref=fix_start_head,
            pass_number=pass_number,
            reason="commit_failed",
        )
        if rollback_failure_reason is not None:
            return False, rollback_failure_reason
        return False, None

    committed_head = await self._rev_parse_head(worktree_path)
    if committed_head is None:
        _log.warning(
            "monitor.pre_push_validation_fix_commit_head_unavailable",
            workspace_id=workspace_id,
            pass_number=pass_number,
        )
        return True, PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON
    # ``_commit_dirty_worktree`` commits onto whatever the agent left as HEAD. If the
    # fix-pass agent rewrote the tip (``commit --amend`` / ``reset`` to a non-descendant
    # of ``fix_start_head``) AND also left dirty or untracked work, that commit lands as a
    # child of the rewritten tip — so ``committed_head`` no longer descends from
    # ``fix_start_head``, which is orphaned, and the later non-force push (remote_ops.py)
    # would be rejected as a non-fast-forward (issue #411 gap). Re-parent the committed
    # tree onto ``fix_start_head`` so the original tip is preserved as the new commit's
    # parent and the push stays fast-forward, mirroring the self-commit reparent above.
    if not await _head_descends_from(
        self,
        worktree_path=worktree_path,
        ancestor=fix_start_head,
        descendant=committed_head,
    ):
        new_head, no_net_change, reparent_failure_reason = await _reparent_fix_pass_commit(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            fix_start_head=fix_start_head,
            current_head=committed_head,
            pass_number=pass_number,
        )
        if reparent_failure_reason is not None:
            return True, reparent_failure_reason
        if not no_net_change and new_head is not None:
            _log.info(
                "monitor.pre_push_validation_fix_reparented",
                workspace_id=workspace_id,
                pass_number=pass_number,
                from_sha=committed_head,
                to_sha=new_head,
                fix_start_head=fix_start_head,
            )
            committed_head = new_head
        else:
            # The committed tree equals ``fix_start_head``'s tree: the rewrite carried no
            # net change worth preserving. Roll back to the pre-fix tip rather than accept
            # an orphaning rewrite that the push would reject.
            rollback_failure_reason = await _rollback_failed_pre_push_validation_fix_pass(
                self,
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                restore_ref=fix_start_head,
                pass_number=pass_number,
                reason="commit_reparent_no_net_change",
            )
            if rollback_failure_reason is not None:
                return False, rollback_failure_reason
            return False, None
    cleanup_failure_reason = await _cleanup_committed_pre_push_validation_fix_pass(
        self,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        committed_head=committed_head,
        pass_number=pass_number,
    )
    return True, cleanup_failure_reason


async def _cleanup_committed_pre_push_validation_fix_pass(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    committed_head: str,
    pass_number: int,
) -> str | None:
    """Clean validation side effects against a committed fix head.

    Used for both the dirty-worktree commit produced by ``_commit_dirty_worktree``
    and the agent self-commit detected when HEAD advanced but the worktree is
    clean. Returns a failure reason code when cleanup fails, otherwise ``None``.
    """
    cleanup = await _pre_push_validation_cleanup(
        self,
        worktree_path=worktree_path,
        restore_ref=committed_head,
    )
    ok = bool(cleanup.ok)
    log = _log.info if ok else _log.warning
    log(
        "monitor.pre_push_validation_fix_commit_cleanup",
        workspace_id=workspace_id,
        pass_number=pass_number,
        restore_ref=committed_head,
        reason_code=None if ok else cleanup.reason_code,
        cleanup_stderr=(cleanup.cleanup_stderr or "")[:400],
    )
    if not ok:
        return cleanup.reason_code or VALIDATION_WORKTREE_CLEANUP_FAILED
    return None


async def _rollback_failed_pre_push_validation_fix_pass(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    restore_ref: str,
    pass_number: int,
    reason: str,
) -> str | None:
    """Rollback fix-pass edits and return a failure reason when recovery fails."""
    reset = await self._deps.runner.run(
        git_worktree_command(worktree_path, "reset", "--hard", restore_ref)
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
        return PRE_PUSH_VALIDATION_ROLLBACK_FAILED_REASON

    cleanup = await _pre_push_validation_cleanup(
        self,
        worktree_path=worktree_path,
        restore_ref=restore_ref,
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
        clean_reason_code=None if ok else cleanup.reason_code,
        reset_stderr=(reset.stderr or "")[:400],
        clean_stderr=(cleanup.cleanup_stderr or "")[:400],
    )
    if ok:
        return None
    return cleanup.reason_code or VALIDATION_WORKTREE_CLEANUP_FAILED


async def _run_pre_push_validation(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    compose_project: str,
    compose_file: Path,
    remote_branch: str,
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
