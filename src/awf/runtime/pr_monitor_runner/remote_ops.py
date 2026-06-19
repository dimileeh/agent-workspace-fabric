"""Submodule for handling git pushes, remote rebases, syncs, and supply chain policy checks before pushing."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from awf.adapters.base import AgentRunError
from awf.common.audit import redact_audit_text
from awf.common.command_evidence import append_command_evidence
from awf.common.commands import CommandResult
from awf.common.git_identity import git_safe_directory_config_args
from awf.common.logging import get_logger
from awf.common.task_tag import commit_message_with_task_tag
from awf.control.blocked_transition import MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceEventCreate, WorkspaceRepository
from awf.node.git_manager import (
    GitOperationError,
    mirror_path_for_worktree,
    repair_mirror_hooks_path,
)
from awf.runtime.ownership import (
    MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    repair_agent_runtime_ownership,
)
from awf.runtime.pr_monitor_runner.constants import (
    _GIT_MIRROR_BROKEN_REF_REMOVED_REASON,
    _GIT_PUSH_REJECTED_NON_FAST_FORWARD_REASON,
    _GITHUB_WORKFLOW_SCOPE_REQUIRED_REASON,
    _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
    _MIRROR_HOOKS_PATH_POISONED_REASON,
    _PROTECTED_SCOPE_REPAIR_FAILED_REASON,
    _REPAIR_DIRTY_COMMIT_FAILED_REASON,
    _SYNC_BASE_RESOLVABLE_STALE_REASONS,
)
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
from awf.runtime.pr_monitor_runner.types import (
    BaseBehindCountError,
    BaseFetchError,
    ProviderRecoveryRetryError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
)
from awf.runtime.validation_worktree_constants import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    VALIDATION_WORKTREE_STATUS_FAILED,
)

if TYPE_CHECKING:
    from awf.common.github_client import RepoRef
    from awf.runtime.logs import WorkspaceLogSink
    from awf.runtime.pr_monitor import MonitorState
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner

_log = get_logger(__name__)

# Constants
_GIT_PUSH_FAILED_REASON = "GIT_PUSH_FAILED"
_WORKFLOW_FILE_PATH_RE = re.compile(r"\.github/workflows/[^\s`'\"()]+")
_WORKFLOW_PERMISSION_TERM = r"`?workflows?`?(?:\s+|-)+(?:scope|permission)s?"
_MISSING_WORKFLOW_SCOPE_PATTERNS = (
    re.compile(
        rf"\bwithout\s+(?:the\s+|a\s+)?{_WORKFLOW_PERMISSION_TERM}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b{_WORKFLOW_PERMISSION_TERM}\s+(?:is\s+|are\s+)?required\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:requires?|needs?|must\s+have|must\s+include)\s+"
        rf"(?:the\s+|a\s+)?{_WORKFLOW_PERMISSION_TERM}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:missing|lacks?|does\s+not\s+have|doesn't\s+have|has\s+no)\s+"
        rf"(?:the\s+|a\s+)?{_WORKFLOW_PERMISSION_TERM}\b",
        re.IGNORECASE,
    ),
)
_WORKFLOW_SCOPE_PUSH_CONTEXT_RE = re.compile(
    r"refusing to allow|create or update workflow|workflow(?:-|\s+)file|"
    r"\.github/workflows/",
    re.IGNORECASE,
)
_DIRECT_WORKFLOW_SCOPE_REQUIRED_RE = re.compile(
    rf"\b(?:remote:\s*)?(?:error:\s*)?{_WORKFLOW_PERMISSION_TERM}\s+"
    rf"(?:is\s+|are\s+)?required\b",
    re.IGNORECASE,
)
_GIT_PUSH_OUTPUT_CONTEXT_RE = re.compile(
    r"\bremote:|\bremote rejected\b|\bpre-receive hook\b|\bhook declined\b|"
    r"\bfailed to push\b",
    re.IGNORECASE,
)
_PROTECTED_SCOPE_PUSH_BLOCKED_REASON = "PROTECTED_SCOPE_PUSH_BLOCKED"
_PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON = "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
_PRE_EXISTING_DIRTY_WORKTREE_REASON = "PRE_EXISTING_DIRTY_WORKTREE"
_REPAIR_START_HEAD_UNAVAILABLE_REASON = "REPAIR_START_HEAD_UNAVAILABLE"
_REPAIR_WORKTREE_STATUS_FAILED_REASON = "REPAIR_WORKTREE_STATUS_FAILED"
AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE = "AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED"
_GIT_MIRROR_BROKEN_REF_REPAIR_MAX_ATTEMPTS = 5
# Orphaned AWF branch refs surface as ``refs/heads/awf/ws_...`` and, for
# task-tagged workspaces, ``refs/heads/<TAG>-awf/ws_...`` (the provisioner
# prepends a Jira issue key like ``PROJ-123-`` via ``branch_with_task_tag``).
# Group 1 captures the full branch (with any tag prefix) so the repair deletes
# the exact ref; group 2 captures the bare workspace id for the active-workspace
# guard. Keep the tag shape in lockstep with ``TASK_TAG_PATTERN`` + ``BRANCH_SEP``.
_BROKEN_AWF_REF_RE = re.compile(r"refs/heads/((?:[A-Z][A-Z0-9]+-[0-9]+-)?awf/(ws_[A-Za-z0-9_-]+))")
_TERMINAL_WORKSPACE_STATUSES = {
    WorkspaceStatus.completed.value,
    WorkspaceStatus.failed.value,
    WorkspaceStatus.cancelled.value,
    WorkspaceStatus.destroyed.value,
}


@dataclass(frozen=True)
class _GitPushResult:
    """Result envelope for a monitor-owned git push attempt."""

    pushed: bool
    failed: bool
    returncode: int
    stdout: str = ""
    stderr: str = ""
    recovered_by_resync: bool = False
    reason_code: str = _GIT_PUSH_FAILED_REASON
    details: Mapping[str, object] | None = None
    paused_into_blocked: bool = False
    """The push site paused the workspace into ``blocked`` for an operator
    decision (a protected-scope violation in an unpushed commit, WS-2). The row
    already left ``monitoring_pr``; the monitor loop ends this cycle WITHOUT
    terminally failing the workspace and the offending commit is preserved."""

    @property
    def error_message(self: Any) -> str | None:
        """Return a normalized push error message when the push failed."""
        if not self.failed:
            return None
        return self.stderr.strip() or "<no output>"

    @property
    def protected_scope_blocked(self: Any) -> bool:
        """Return whether protected-scope policy blocked this push."""
        return self.failed and self.reason_code in {
            _PROTECTED_SCOPE_PUSH_BLOCKED_REASON,
            _PROTECTED_SCOPE_REPAIR_FAILED_REASON,
            _PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
        }

    @property
    def protected_scope_diff_unavailable(self) -> bool:
        """Return whether protected-scope diff evidence could not be gathered."""
        return self.failed and self.reason_code == _PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON

    @property
    def workflow_scope_required(self) -> bool:
        """Return whether GitHub rejected a workflow-file push for missing scope."""
        return self.failed and self.reason_code == _GITHUB_WORKFLOW_SCOPE_REQUIRED_REASON

    @property
    def terminal_monitor_failure(self: Any) -> bool:
        """Return whether the push failure should end monitor recovery."""
        # A pause into ``blocked`` is NOT a terminal failure: the workspace is
        # preserved for an operator decision, not failed. The loop ends the
        # cycle via the dedicated paused-into-blocked handling instead.
        if self.paused_into_blocked:
            return False
        return self.failed and (
            self.protected_scope_blocked
            or self.reason_code
            in {
                AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
                _PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON,
                _PRE_PUSH_VALIDATION_ROLLBACK_FAILED_REASON,
                _PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON,
                _PRE_PUSH_DIRTY_FINALIZE_DELTA_UNAVAILABLE_REASON,
                _PRE_EXISTING_DIRTY_WORKTREE_REASON,
                _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
                _MIRROR_HOOKS_PATH_POISONED_REASON,
                _PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING_REASON,
                _REPAIR_START_HEAD_UNAVAILABLE_REASON,
                _REPAIR_WORKTREE_STATUS_FAILED_REASON,
                _REPAIR_DIRTY_COMMIT_FAILED_REASON,
                VALIDATION_WORKTREE_CLEANUP_FAILED,
                VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
                VALIDATION_WORKTREE_STATUS_FAILED,
            }
        )

    def failure_evidence(self: Any) -> dict[str, object]:
        """Build structured evidence for a failed push operation."""
        evidence: dict[str, object] = {
            "operation": "git push",
            "returncode": self.returncode,
            "error_message": self.error_message or "<no output>",
            "reason_code": self.reason_code,
        }
        if self.recovered_by_resync:
            evidence["recovered_by_resync"] = True
        if self.details:
            evidence.update(self.details)
        return evidence


@dataclass(frozen=True)
class _ProtectedScopePushBlock:
    """Protected-scope policy decision that blocks a monitor push."""

    message: str
    reason_code: str
    violations: tuple[Any, ...] = ()


@dataclass(frozen=True)
class _WorkflowScopePushBlock:
    """Parsed GitHub missing-workflow-scope push rejection details."""

    blocked: bool
    message: str = ""
    paths: tuple[str, ...] = ()


def _git_push_failure_outcome(push_result: _GitPushResult) -> str:
    """Map a push result to the monitor operation outcome label."""
    if push_result.reason_code == _PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING_REASON:
        return "pre_push_validation_toolchain_missing"
    if push_result.reason_code in {
        _PRE_PUSH_VALIDATION_FAILED_REASON,
        _PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON,
        _PRE_PUSH_VALIDATION_FIX_FAILED_REASON,
        _PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON,
        _PRE_PUSH_VALIDATION_ROLLBACK_FAILED_REASON,
        _PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON,
        _PRE_PUSH_DIRTY_FINALIZE_DELTA_UNAVAILABLE_REASON,
        VALIDATION_WORKTREE_CLEANUP_FAILED,
        VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
        VALIDATION_WORKTREE_STATUS_FAILED,
    }:
        return "pre_push_validation_failed"
    if push_result.workflow_scope_required:
        return "github_workflow_scope_required"
    if push_result.protected_scope_diff_unavailable:
        return "protected_scope_diff_unavailable"
    if push_result.protected_scope_blocked:
        return "protected_scope_push_blocked"
    if push_result.reason_code in {
        _PRE_EXISTING_DIRTY_WORKTREE_REASON,
        _REPAIR_START_HEAD_UNAVAILABLE_REASON,
        _REPAIR_WORKTREE_STATUS_FAILED_REASON,
    }:
        return "repair_start_blocked"
    return "git_push_failed"


def _git_failure_message(operation: str, result: CommandResult) -> str:
    """Format a concise failure message for a git command result."""
    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    output = f"; stderr: {stderr}" if stderr else f"; stdout: {stdout}" if stdout else ""
    return f"{operation} failed with exit code {result.returncode}{output}"


def _append_git_recovery_failure(
    *,
    push_stderr: str | None,
    recovery_stderr: str | None,
    operation: str,
) -> str:
    """Append recovery failure context to the original push failure output."""
    push_msg = (push_stderr or "").strip()
    recovery_msg = (recovery_stderr or "").strip()
    lines = []
    if push_msg:
        lines.append(push_msg)
    suffix = f" ({operation} failed)"
    if recovery_msg:
        suffix = f" ({operation} failed: {recovery_msg})"
    lines.append(f"AWF worktree recovery failed during git push failure resync{suffix}")
    return "\n".join(lines)


async def _refresh_supply_chain_policy_before_push(
    runner: PullRequestMonitorRunner,
    workspace_id: str,
    command_evidence: Sequence[str],
    changed_paths: Sequence[str],
) -> str | None:
    """Refresh supply-chain policy findings before a monitor-authored push."""
    from awf.runtime.pr_monitor_runner.helpers import _supply_chain_policy_blocked_message
    from awf.service.supply_chain_policy import (
        SupplyChainPolicyRefreshError,
        SupplyChainPolicyRefreshService,
    )

    try:
        async with runner._deps.session_factory() as s:
            result = await SupplyChainPolicyRefreshService(s).refresh_workspace_open_candidate(
                workspace_id,
                command_evidence=command_evidence,
                changed_paths=changed_paths,
            )
            await s.commit()
    except SupplyChainPolicyRefreshError:
        _log.warning(
            "monitor.supply_chain_policy_refresh_skipped",
            workspace_id=workspace_id,
            reason="workspace_not_found",
        )
        return None
    blocking_codes = [
        finding.reason_code for finding in result.findings if finding.severity == "blocking"
    ]
    if not blocking_codes:
        return None
    return _supply_chain_policy_blocked_message(blocking_codes)


async def _fetch_base(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
    base_branch: str,
) -> None:
    """Fetch the PR base branch, repairing orphaned AWF refs when possible."""
    result = await runner._fetch_base_once(worktree_path=worktree_path, base_branch=base_branch)
    repairs_attempted = 0
    while not result.ok and repairs_attempted < _GIT_MIRROR_BROKEN_REF_REPAIR_MAX_ATTEMPTS:
        try:
            repaired = await runner._repair_orphaned_broken_awf_ref(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                stderr=result.stderr,
            )
        except Exception as exc:
            _log.exception(
                "monitor.git_mirror_broken_ref_repair_failed",
                workspace_id=workspace_id,
                base_branch=base_branch,
                repairs_attempted=repairs_attempted,
            )
            raise BaseFetchError(
                redact_audit_text(
                    "git fetch base failed: broken AWF ref repair failed "
                    f"after fetch failure: {exc!r}",
                    limit=2000,
                )
            ) from exc
        if not repaired:
            break
        repairs_attempted += 1
        result = await runner._fetch_base_once(
            worktree_path=worktree_path,
            base_branch=base_branch,
        )
    if result.ok:
        return
    raise BaseFetchError(_git_failure_message("git fetch base", result))


async def _fetch_base_once(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
    base_branch: str,
) -> CommandResult:
    """Run one forced fetch of the base branch into the local origin mirror."""
    return await runner._deps.runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "fetch",
            "origin",
            f"+refs/heads/{base_branch}:refs/remotes/origin/{base_branch}",
        ]
    )


async def _repair_orphaned_broken_awf_ref(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
    stderr: str,
) -> bool:
    """Remove an orphaned AWF ref that prevents fetching from the origin mirror."""
    match = _BROKEN_AWF_REF_RE.search(stderr or "")
    if match is None:
        return False
    broken_ref = f"refs/heads/{match.group(1)}"
    broken_workspace_id = match.group(2)
    if not await runner._can_remove_broken_awf_ref(broken_workspace_id):
        _log.warning(
            "monitor.git_mirror_broken_ref_active_workspace",
            workspace_id=workspace_id,
            broken_workspace_id=broken_workspace_id,
            broken_ref=broken_ref,
        )
        return False
    delete_result = await runner._deps.runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "update-ref",
            "-d",
            broken_ref,
        ]
    )
    if not delete_result.ok:
        _log.warning(
            "monitor.git_mirror_broken_ref_delete_failed",
            workspace_id=workspace_id,
            broken_ref=broken_ref,
            stderr=(delete_result.stderr or "")[:400],
        )
        return False
    await runner._deps.runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "worktree",
            "prune",
        ]
    )
    await runner._append_workspace_events(
        workspace_id=workspace_id,
        events=[
            WorkspaceEventCreate(
                event_type="workspace.git_mirror_repaired",
                reason_code=_GIT_MIRROR_BROKEN_REF_REMOVED_REASON,
                payload={
                    "broken_ref": broken_ref,
                    "broken_workspace_id": broken_workspace_id,
                },
            )
        ],
    )
    return True


async def _can_remove_broken_awf_ref(self: Any, broken_workspace_id: str) -> bool:
    """Return whether an AWF ref belongs to a terminal or unknown workspace."""
    async with self._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get(broken_workspace_id)
        if workspace is None:
            return True
        return workspace.status in _TERMINAL_WORKSPACE_STATUSES


async def _count_base_behind(self: Any, *, worktree_path: Path, base_branch: str) -> int:
    """Count commits by which the worktree HEAD trails the fetched base branch."""
    result = await self._deps.runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "rev-list",
            "--count",
            f"HEAD..origin/{base_branch}",
        ]
    )
    if not result.ok:
        raise BaseBehindCountError(f"git rev-list failed: {result.stderr}")
    try:
        return int(result.stdout.strip() or "0")
    except ValueError as exc:
        raise BaseBehindCountError(
            f"git rev-list returned non-integer output: {result.stdout!r}"
        ) from exc


async def _rev_parse_head(self: Any, worktree_path: Path) -> str | None:
    """Resolve the current worktree HEAD SHA, returning None on failure."""
    result = await self._deps.runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "rev-parse",
            "HEAD",
        ]
    )
    if not result.ok:
        return None
    value = result.stdout.strip()
    return value or None


async def _git_push(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
    remote_branch: str,
    remote_url: str | None = None,
) -> bool:
    """Push HEAD to the remote branch and report whether anything was pushed."""
    refspec = f"HEAD:refs/heads/{remote_branch}"
    result = await runner._git_push_result(
        worktree_path=worktree_path,
        remote_branch=remote_branch,
        refspec=refspec,
        remote_url=remote_url,
    )
    return result.pushed


async def _git_push_result(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
    remote_branch: str,
    remote_url: str | None = None,
    refspec: str | None = None,
    allow_resync_on_rejection: bool = True,
) -> _GitPushResult:
    """Push HEAD and return detailed failure or resync information.

    ``allow_resync_on_rejection`` gates the non-fast-forward divergence recovery
    (``git fetch`` + ``git reset --hard`` to the remote head). It defaults to
    ``True`` (every ordinary monitor/merge push resyncs a stale local branch). The
    operator-hint approve-and-keep resume sets it ``False``: that recovery would
    drop the preserved protected commit the grant is meant to land BEFORE the grant
    is consumed, then no-op the next cycle and wedge the hint at ``monitoring_pr``
    where ``guide --grant`` is rejected. When ``False`` and the push is rejected
    non-fast-forward, return the failure WITHOUT resetting — HEAD (and the preserved
    commit) is kept — tagged with ``_GIT_PUSH_REJECTED_NON_FAST_FORWARD_REASON`` so
    the caller can re-block instead (PRRT_kwDOSJAM6s6KZK1v).
    """
    from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command

    remote = remote_url or "origin"
    refspec = refspec or f"HEAD:refs/heads/{remote_branch}"
    policy_block_message = await runner._active_policy_block_message(worktree_path.name)
    if policy_block_message is not None:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr=policy_block_message,
        )
    r = await runner._deps.runner.run(git_worktree_command(worktree_path, "push", remote, refspec))
    if r.ok:
        pushed = "up-to-date" not in (r.stderr or "").lower()
        return _GitPushResult(
            pushed=pushed,
            failed=False,
            returncode=r.returncode,
            stdout=r.stdout,
            stderr=r.stderr,
        )

    push_output = _combined_git_output(r.stderr, r.stdout)
    workflow_scope_block = _workflow_scope_push_block(push_output)
    if workflow_scope_block.blocked:
        message = workflow_scope_block.message
        paths = workflow_scope_block.paths
        _log.warning(
            "monitor.push_failed_missing_workflow_scope",
            paths=list(paths),
        )
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=r.returncode,
            stdout=r.stdout or "",
            stderr=message,
            reason_code=_GITHUB_WORKFLOW_SCOPE_REQUIRED_REASON,
            details={
                "phase": "git_push",
                "permission": "workflow_scope",
                "paths": list(paths),
                "pushed": False,
            },
        )

    _log_unmatched_workflow_file_push_output(push_output)
    stderr_lower = (r.stderr or "").lower()
    is_rejection = (
        "[rejected]" in stderr_lower
        or "non-fast-forward" in stderr_lower
        or "fetch first" in stderr_lower
    )
    if not is_rejection:
        _log.warning(
            "monitor.push_failed_non_divergence",
            stderr=(r.stderr or "")[:400],
        )
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=r.returncode,
            stdout=r.stdout,
            stderr=r.stderr,
        )

    if not allow_resync_on_rejection:
        # The caller (an approve-and-keep operator-hint resume) must KEEP the
        # preserved protected commit; resyncing here would reset --hard it away
        # before the grant is consumed. Surface the rejection unrecovered so the
        # caller re-blocks. ``recovered_by_resync`` stays False — no recovery ran.
        _log.warning(
            "monitor.push_rejected_resync_suppressed",
            worktree_path=str(worktree_path),
            remote_branch=remote_branch,
            stderr=(r.stderr or "")[:400],
        )
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=r.returncode,
            stdout=r.stdout,
            stderr=r.stderr,
            reason_code=_GIT_PUSH_REJECTED_NON_FAST_FORWARD_REASON,
        )

    _log.warning(
        "monitor.push_rejected_resyncing_local",
        worktree_path=str(worktree_path),
        remote_branch=remote_branch,
        stderr=(r.stderr or "")[:400],
    )
    if remote_url:
        fetch_result = await runner._deps.runner.run(
            git_worktree_command(
                worktree_path,
                "fetch",
                remote_url,
                f"refs/heads/{remote_branch}",
            )
        )
        reset_target = "FETCH_HEAD"
    else:
        fetch_result = await runner._deps.runner.run(
            git_worktree_command(worktree_path, "fetch", "origin", remote_branch)
        )
        reset_target = f"origin/{remote_branch}"
    if not fetch_result.ok:
        stderr = _append_git_recovery_failure(
            push_stderr=r.stderr,
            recovery_stderr=fetch_result.stderr,
            operation="resync fetch",
        )
        _log.warning(
            "monitor.push_rejected_resync_fetch_failed",
            worktree_path=str(worktree_path),
            remote_branch=remote_branch,
            stderr=stderr[:400],
        )
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=r.returncode,
            stdout=r.stdout,
            stderr=stderr,
        )
    await runner._deps.runner.run(
        git_worktree_command(worktree_path, "reset", "--hard", reset_target)
    )
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=r.returncode,
        stdout=r.stdout,
        stderr=r.stderr,
        recovered_by_resync=True,
    )


def _combined_git_output(*outputs: str | None) -> str:
    """Join git output fragments, ignoring empty pieces."""
    return "\n".join(output for output in outputs if output)


def _log_unmatched_workflow_file_push_output(output: str) -> None:
    """Log detected workflow file context from a push-failure output."""
    paths = tuple(
        dict.fromkeys(
            match.group(0).rstrip(".,;:") for match in _WORKFLOW_FILE_PATH_RE.finditer(output)
        )
    )
    if not paths:
        return
    _log.warning(
        "monitor.push_failed_unmatched_workflow_file_context",
        paths=list(paths),
        output=redact_audit_text(output, limit=400),
    )


def _workflow_scope_push_block(output: str) -> _WorkflowScopePushBlock:
    """Detect whether push failure is due to missing workflow scope."""
    normalized = " ".join(output.split())
    if not any(pattern.search(normalized) for pattern in _MISSING_WORKFLOW_SCOPE_PATTERNS):
        return _WorkflowScopePushBlock(blocked=False)
    has_workflow_file_context = _WORKFLOW_SCOPE_PUSH_CONTEXT_RE.search(normalized) is not None
    has_direct_hook_context = (
        _DIRECT_WORKFLOW_SCOPE_REQUIRED_RE.search(normalized) is not None
        and _GIT_PUSH_OUTPUT_CONTEXT_RE.search(normalized) is not None
    )
    if not (has_workflow_file_context or has_direct_hook_context):
        return _WorkflowScopePushBlock(blocked=False)
    paths = tuple(
        dict.fromkeys(
            match.group(0).rstrip(".,;:") for match in _WORKFLOW_FILE_PATH_RE.finditer(normalized)
        )
    )
    path_suffix = f" for {', '.join(paths)}" if paths else ""
    message = (
        "GitHub rejected the workflow-file push because the token lacks "
        f"`workflow` scope{path_suffix}. Grant a GitHub token with workflow "
        "push permission, then rerun the monitor repair."
    )
    return _WorkflowScopePushBlock(blocked=True, message=message, paths=paths)


async def _run_sync_base(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    state: object | None = None,
    repo: RepoRef,
    pr_number: int,
    pr_head_sha: str | None = None,
    base_branch: str,
    remote_branch: str,
    remote_push_url: str | None = None,
    compose_project: str,
    compose_file: Path,
    operation_id: str | None = None,
    operation_type: str | None = None,
    monitor_log: WorkspaceLogSink | None = None,
) -> _GitPushResult:
    """Merge the latest base branch into the workspace and push the repair."""
    from awf.runtime.monitor_prompts import sync_base_conflict_prompt
    from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command

    worktree_path = runner._worktrees_root / workspace_id
    operation_start_head, head_result = await runner._repair_operation_start_head_result(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        operation_type="sync_base",
    )
    if head_result is not None:
        return head_result

    async def _git(*args: str) -> tuple[int, str, str]:
        """Run a git command in the sync-base worktree."""
        r = await runner._deps.runner.run(git_worktree_command(worktree_path, *args))
        return r.returncode, r.stdout, r.stderr

    task_tag = await runner._resolve_task_tag(workspace_id)
    await _git("merge", "--abort")
    await runner._fetch_base(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        base_branch=base_branch,
    )
    mirror_path = mirror_path_for_worktree(worktree_path)
    if mirror_path is not None:
        try:
            await repair_mirror_hooks_path(mirror_path)
        except (GitOperationError, OSError) as exc:
            _log.warning(
                "monitor.sync_base_mirror_hooks_path_repair_failed",
                workspace_id=workspace_id,
                reason_code=_MIRROR_HOOKS_PATH_POISONED_REASON,
                error_type=exc.__class__.__name__,
            )
            return _GitPushResult(
                pushed=False,
                failed=True,
                returncode=1,
                stderr="could not repair poisoned mirror hooks path before sync-base merge",
                reason_code=_MIRROR_HOOKS_PATH_POISONED_REASON,
            )
    merge_args: tuple[str, ...] = ("merge", "--no-edit", f"origin/{base_branch}")
    if task_tag:
        # A clean (conflict-free) base sync produces an AWF-authored merge commit;
        # tag its subject so it carries the Jira key like every other AWF commit
        # (the conflict path is tagged later via ``_commit_dirty_worktree``). On a
        # conflict git stashes this in ``MERGE_MSG`` and the conflict-resolution
        # commit overrides it, so ``-m`` only shapes the clean-merge commit.
        merge_args = (
            "merge",
            "--no-edit",
            f"origin/{base_branch}",
            "-m",
            commit_message_with_task_tag(
                f"Merge remote-tracking branch 'origin/{base_branch}'", task_tag
            ),
        )
    rc, _stdout, stderr = await _git(*merge_args)
    if rc != 0:
        _rc_status, status_out, _ = await _git("status", "--porcelain")
        conflicting_files = tuple(
            line[3:]
            for line in status_out.splitlines()
            if line.startswith(("UU ", "AA ", "DD ", "AU ", "UA ", "DU ", "UD "))
        )
        prompt = sync_base_conflict_prompt(
            pr_number=pr_number,
            repo_slug=repo.slug(),
            base_branch=base_branch,
            conflicting_files=conflicting_files,
            workspace_runtime_context=runner._workspace_runtime_context,
            task_tag=task_tag,
        )
        agent_run_err = None
        command_evidence: list[str] = []
        if await runner._provider_recovery_suppresses_cli(workspace_id):
            raise ProviderRecoveryRetryError()
        if not await repair_agent_runtime_ownership(
            logger=_log,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            reason="monitor_agent_pre_launch",
            event_name=MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
        ):
            return _GitPushResult(
                pushed=False,
                failed=True,
                returncode=1,
                stderr="agent runtime ownership repair failed before sync-base agent launch",
                reason_code=AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
            )
        try:
            result = await runner._deps.adapter.run(
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=prompt,
                workspace_id=workspace_id,
                log_source="recovery",
            )
            append_command_evidence(command_evidence, stdout=result.stdout, stderr=result.stderr)
        except AgentRunError as exc:
            agent_run_err = exc
            append_command_evidence(
                command_evidence,
                stdout=exc.result.stdout,
                stderr=exc.result.stderr,
            )

        if agent_run_err is not None:
            await runner._handle_provider_agent_run_error(workspace_id, agent_run_err)

        try:
            await runner._commit_dirty_worktree(
                workspace_id=workspace_id,
                message=f"fix: resolve PR #{pr_number} base conflicts",
                command_evidence=command_evidence,
                compose_project=compose_project,
                compose_file=compose_file,
                task_tag=task_tag,
                operation_start_head=operation_start_head,
            )
        except _MonitorPolicyBlockedError as exc:
            return _GitPushResult(
                pushed=False,
                failed=True,
                returncode=1,
                stderr=str(exc),
            )
        except _MonitorAgentRuntimeOwnershipRepairFailedError as exc:
            return _GitPushResult(
                pushed=False,
                failed=True,
                returncode=1,
                stderr=str(exc),
                reason_code=exc.reason_code,
            )
        except _MonitorHeadObjectMissingError as exc:
            return _GitPushResult(
                pushed=False,
                failed=True,
                returncode=1,
                stderr=str(exc),
                reason_code=_HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
            )
        except _MonitorMirrorHooksPathRepairFailedError as exc:
            return _GitPushResult(
                pushed=False,
                failed=True,
                returncode=1,
                stderr=str(exc),
                reason_code=exc.reason_code,
            )

        if agent_run_err is not None:
            _log.warning(
                "monitor.sync_base_cli_failed",
                workspace_id=workspace_id,
                stderr=agent_run_err.result.stderr[:400],
            )

    protected_scope_block = await runner._protected_scope_push_block(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        remote_branch=remote_branch,
        remote_push_url=remote_push_url,
        base_branch=base_branch,
    )
    if protected_scope_block is not None:
        if protected_scope_block.violations:
            # A real protected-scope violation in the base-conflict resolution
            # commit PAUSES the workspace into ``blocked`` for an operator
            # decision (WS-2), preserving the offending commit, instead of
            # terminally failing the monitor. Without this, sync-base was the
            # one POST-PR push path still outside WS-2: it returned a plain
            # failed ``PROTECTED_SCOPE_PUSH_BLOCKED`` result that the loop's
            # SyncBase branch treated as terminal. A diff-unavailable block (no
            # violations) keeps the terminal failed handling below.
            return await runner._pause_monitor_for_protected_scope_block(
                workspace_id=workspace_id,
                pr_number=pr_number,
                pr_head_sha=pr_head_sha or "",
                protected_scope_block=protected_scope_block,
                worktree_path=worktree_path,
                resume_phase=MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE,
                state=cast("MonitorState | None", state),
                remote_branch=remote_branch,
                base_branch=base_branch,
                operation_id=operation_id,
                operation_type=operation_type,
                monitor_log=monitor_log,
                source_head_sha=pr_head_sha,
            )
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr=protected_scope_block.message,
            reason_code=protected_scope_block.reason_code,
        )
    return await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        remote_branch=remote_branch,
        compose_project=compose_project,
        compose_file=compose_file,
        remote_url=remote_push_url,
        state=state,
        operation_start_head=operation_start_head,
    )


async def _refresh_staleness_after_sync_base(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    base_branch: str,
) -> None:
    """Refresh candidate stale state after a successful sync-base repair."""
    from awf.db.repositories import (
        MergeCandidateRepository,
        StaleReasonCreate,
        StaleReasonRepository,
        sync_candidate_readiness,
    )
    from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command

    try:
        async with runner._deps.session_factory() as session:
            candidate = await MergeCandidateRepository(
                session
            ).get_open_for_workspace_with_merge_inputs(workspace_id)
            if candidate is None:
                # No tracked candidate — nothing to refresh.
                return
            active_reasons = await StaleReasonRepository(session).list_active_for_candidate(
                candidate.id
            )
            resolvable = [
                r for r in active_reasons if r.reason_code in _SYNC_BASE_RESOLVABLE_STALE_REASONS
            ]
            worktree_path = runner._worktrees_root / workspace_id
            rev_parse = await runner._deps.runner.run(
                git_worktree_command(worktree_path, "rev-parse", f"origin/{base_branch}")
            )
            if rev_parse.returncode != 0 or not rev_parse.stdout.strip():
                _log.warning(
                    "monitor.sync_base_staleness_refresh_skipped",
                    workspace_id=workspace_id,
                    reason="rev_parse_failed",
                    stderr=(rev_parse.stderr[:400] if rev_parse.stderr else None),
                )
                return
            new_base_sha = rev_parse.stdout.strip()
            candidate.base_sha = new_base_sha
            if not resolvable:
                # No target-derived rows to clear. Leave any intrinsic
                # blocking reason (e.g. ``docs_task_scope_violation``) and
                # the candidate's stale flag untouched — a rebase does not
                # remediate those — and just persist the advanced base.
                await session.commit()
                return
            # Resolve only the target-derived rows by re-stating every other
            # active finding: ``replace_active_findings`` keeps rows whose
            # (reason_code, trigger_type, trigger_ref) key is supplied and
            # resolves the rest, so the preserved intrinsic rows survive.
            preserved = [
                r
                for r in active_reasons
                if r.reason_code not in _SYNC_BASE_RESOLVABLE_STALE_REASONS
            ]
            await StaleReasonRepository(session).replace_active_findings(
                workspace_id=candidate.workspace_id,
                candidate_id=candidate.id,
                attempt_id=candidate.attempt_id,
                task_id=candidate.task_id,
                findings=[
                    StaleReasonCreate(
                        reason_code=r.reason_code,
                        trigger_type=r.trigger_type,
                        trigger_ref=r.trigger_ref,
                        explanation=r.explanation,
                    )
                    for r in preserved
                ],
            )
            # The candidate's stale_reason column may still hold a reason we
            # just resolved (or a generic "stale"). Clear it, then let
            # sync_candidate_readiness reinstate only the intrinsic reasons
            # (docs scope / validation tier) that genuinely still apply.
            candidate.stale = False
            candidate.stale_reason = None
            sync_candidate_readiness(
                candidate,
                workspace=candidate.workspace,
                attempt=candidate.attempt,
                sync_validation_staleness=True,
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 — best-effort; reconciler will retry
        _log.warning(
            "monitor.sync_base_staleness_refresh_failed",
            workspace_id=workspace_id,
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
