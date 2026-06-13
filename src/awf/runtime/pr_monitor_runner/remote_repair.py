"""Extracted PullRequestMonitorRunner domain operations.

This module contains mechanically moved methods from ``awf.runtime.pr_monitor_runner.runner`` and keeps behavior unchanged.
"""

from __future__ import annotations

import asyncio as asyncio
import hashlib as hashlib
import json as json
import os as os
import re as re
import time as time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from awf.adapters.base import AgentRunError
from awf.common.audit import redact_audit_text
from awf.common.commands import CommandResult
from awf.common.git_identity import (
    git_safe_directory_config_args,
)
from awf.common.task_tag import commit_message_with_task_tag
from awf.control.protected_file_diffs import (
    protected_file_diffs_for_committed_paths,
)
from awf.control.quality_gates import (
    QualityGateViolation,
    find_protected_quality_gate_changes,
    quality_gate_violation_details,
    quality_gate_violation_message,
)
from awf.db.repositories import (
    MergeCandidateRepository,
    WorkspaceEventCreate,
    WorkspaceRepository,
)
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.ownership import (
    MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    repair_agent_runtime_ownership,
)
from awf.runtime.pr_monitor import (
    MonitorState,
    PRStatus,
)
from awf.runtime.pr_monitor_runner.commit_autofix import (
    _retry_monitor_precommit_autofix_commit_once,
)
from awf.runtime.pr_monitor_runner.constants import (
    _AUDIT_GIT_PUSH_EVENT,
    _PRE_EXISTING_DIRTY_WORKTREE_REASON,
    _PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
    _PROTECTED_SCOPE_PUSH_BLOCKED_REASON,
    _PROTECTED_SCOPE_REPAIR_FAILED_REASON,
    _REPAIR_START_HEAD_UNAVAILABLE_REASON,
    _REPAIR_WORKTREE_STATUS_FAILED_REASON,
    _TASK_TAG_UNSET,
    _TaskTagUnset,
)
from awf.runtime.pr_monitor_runner.git_utils import (
    git_worktree_command,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _changed_paths_from_name_only_z,
    _changed_paths_from_name_status_z,
    _changed_paths_from_porcelain,
    _changed_paths_from_porcelain_z,
    _quality_gate_violation_paths,
    _untracked_paths_from_porcelain,
    _untracked_paths_from_porcelain_z,
)
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.remote_ops import (
    AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
    _GitPushResult,
    _ProtectedScopePushBlock,
)
from awf.runtime.pr_monitor_runner.types import (
    BaseFetchError,
    ProtectedScopeDiffError,
    ProviderRecoveryRetryError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorPolicyBlockedError,
    _ProtectedScopeRollbackDeltaEvidence,
)
from awf.runtime.validation_worktree import (
    is_under_agent_runtime_root,
)


async def _pre_existing_dirty_repair_worktree_result(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    operation_type: str,
) -> _GitPushResult | None:
    if not worktree_path.exists():
        return None
    # ``--untracked-files=all`` is load-bearing here: with git's default
    # ``normal`` mode a *fully*-untracked ``.claude/`` (no tracked content under
    # it) collapses all the way to a single ``?? .claude/`` entry, which is NOT
    # under the ``.claude/agent-memory/`` ignored root and would therefore stay
    # in ``paths`` and refuse repair in the common case this guard unblocks.
    # Enumerating leaf paths lets the agent-runtime filter below see and drop the
    # memory files. Mirrors ``check_validation_worktree_clean``.
    status = await self._deps.runner.run(
        git_worktree_command(worktree_path, "status", "--porcelain", "--untracked-files=all")
    )
    if not status.ok:
        stderr = status.stderr[:400]
        _log.warning(
            "monitor.repair_worktree_status_failed",
            workspace_id=workspace_id,
            operation_type=operation_type,
            returncode=status.returncode,
            stderr=stderr,
        )
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=status.returncode,
            stderr="Could not inspect repair worktree before starting the agent.",
            reason_code=_REPAIR_WORKTREE_STATUS_FAILED_REASON,
            details={
                "phase": "repair_start",
                "operation_type": operation_type,
                "status_stderr": stderr,
                "pushed": False,
            },
        )
    if not status.stdout.strip():
        return None

    # AWF-agent-runtime artifacts (reviewer subagent memory) written into the
    # repair worktree are not part of the PR, so drop UNTRACKED memory paths
    # before deciding the worktree is dirty. Tracked-modified memory (and every
    # other path) stays visible/blocking. If nothing else remains, the worktree
    # is effectively clean — return None, same as the empty-status path above.
    all_paths = _changed_paths_from_porcelain(status.stdout)
    untracked = set(_untracked_paths_from_porcelain(status.stdout))
    paths = sorted(
        path for path in all_paths if not (path in untracked and is_under_agent_runtime_root(path))
    )
    if not paths:
        return None
    _log.warning(
        "monitor.repair_worktree_pre_existing_dirty",
        workspace_id=workspace_id,
        operation_type=operation_type,
        paths=paths,
    )
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr=(
            "Repair worktree has pre-existing uncommitted changes; "
            "refusing to start agent repair because protected-scope rollback "
            "would not be limited to the current operation."
        ),
        reason_code=_PRE_EXISTING_DIRTY_WORKTREE_REASON,
        details={
            "phase": "repair_start",
            "operation_type": operation_type,
            "paths": paths,
            "pushed": False,
        },
    )


async def _repair_operation_start_head_result(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    operation_type: str,
    fallback_head_sha: str | None = None,
) -> tuple[str, _GitPushResult | None]:
    if not worktree_path.exists():
        source = "status" if fallback_head_sha else "candidate"
        fallback_head = fallback_head_sha or await self._open_merge_candidate_head_sha(workspace_id)
        if fallback_head:
            _log.info(
                "monitor.repair_operation_start_head_from_fallback",
                workspace_id=workspace_id,
                operation_type=operation_type,
                head_sha=fallback_head[:10],
                source=source,
            )
            return fallback_head, None
    result = await self._deps.runner.run(git_worktree_command(worktree_path, "rev-parse", "HEAD"))
    head_sha = result.stdout.strip()
    if result.ok and head_sha:
        return head_sha, None

    stdout = result.stdout[:400]
    stderr = result.stderr[:400]
    _log.warning(
        "monitor.repair_operation_start_head_unavailable",
        workspace_id=workspace_id,
        operation_type=operation_type,
        returncode=result.returncode,
        stdout=stdout,
        stderr=stderr,
    )
    return "", _GitPushResult(
        pushed=False,
        failed=True,
        returncode=result.returncode if result.returncode != 0 else 1,
        stderr=(
            "Could not capture repair operation start HEAD before starting the agent; "
            "refusing to start repair because protected-scope rollback would not have "
            "a stable baseline."
        ),
        reason_code=_REPAIR_START_HEAD_UNAVAILABLE_REASON,
        details={
            "phase": "repair_start",
            "operation_type": operation_type,
            "head_stdout": stdout,
            "head_stderr": stderr,
            "pushed": False,
        },
    )


async def _open_merge_candidate_head_sha(self: Any, workspace_id: str) -> str | None:
    async with self._deps.session_factory() as session:
        repository = MergeCandidateRepository(session)
        candidate = await repository.get_open_for_workspace_with_merge_inputs(workspace_id)
        return candidate.head_sha if candidate is not None else None


async def _resolve_task_tag(self: Any, workspace_id: str) -> str | None:
    """Load the workspace's optional Jira issue key for commit-message tagging."""
    async with self._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        return workspace.task_tag if workspace is not None else None


async def _commit_dirty_worktree(
    self: Any,
    *,
    workspace_id: str,
    message: str,
    compose_project: str | None = None,
    compose_file: Path | None = None,
    state: MonitorState | None = None,
    command_evidence: Sequence[str] = (),
    protected_scope_revert_remote_branch: str | None = None,
    remote_push_url: str | None = None,
    task_tag: str | None | _TaskTagUnset = _TASK_TAG_UNSET,
) -> bool:
    """Commit dirty monitor-agent edits so PR feedback is not stranded.

    Coding CLIs can apply a valid fix and still exit non-zero while
    formatting, testing, or summarising. PR #35 exposed that failure
    mode: the monitor treated the CLI failure as a bot defer, but the
    useful fix was left dirty in the service worktree and never pushed.
    """

    worktree_path = self._worktrees_root / workspace_id
    if not worktree_path.exists():
        return False
    status = await self._deps.runner.run(
        git_worktree_command(worktree_path, "status", "--porcelain")
    )
    if not status.ok:
        _log.warning(
            "monitor.dirty_check_failed",
            workspace_id=workspace_id,
            stderr=status.stderr[:400],
        )
        return False
    if not status.stdout.strip():
        return False

    changed_paths = tuple(_changed_paths_from_porcelain(status.stdout))
    policy_message = await self._refresh_supply_chain_policy_before_push(
        workspace_id=workspace_id,
        command_evidence=command_evidence,
        changed_paths=changed_paths,
    )
    if policy_message is not None:
        raise _MonitorPolicyBlockedError(policy_message)

    if not await repair_agent_runtime_ownership(
        logger=_log,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="dirty_worktree_pre_commit",
        event_name=MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
        reason_code=AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
    ):
        raise _MonitorAgentRuntimeOwnershipRepairFailedError(
            AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
        )

    if compose_project is not None and compose_file is not None:
        repaired_status = await self._repair_protected_scope_changes_before_commit(
            workspace_id=workspace_id,
            status_stdout=status.stdout,
            compose_project=compose_project,
            compose_file=compose_file,
            state=state,
            protected_scope_revert_remote_branch=protected_scope_revert_remote_branch,
            remote_push_url=remote_push_url,
        )
        if repaired_status is None:
            return False
        status = repaired_status
        changed_paths = tuple(_changed_paths_from_porcelain(status.stdout))

    add = await self._deps.runner.run(git_worktree_command(worktree_path, "add", "-A"))
    if not add.ok:
        _log.warning(
            "monitor.dirty_add_failed",
            workspace_id=workspace_id,
            stderr=add.stderr[:400],
        )
        return False

    cached = await self._deps.runner.run(
        git_worktree_command(worktree_path, "diff", "--cached", "--quiet")
    )
    if cached.returncode == 0:
        return False

    # Prepend the workspace's Jira issue key (if any) so monitor review-fix /
    # CI-fix commits link to the issue. Idempotent: a re-run never double-prefixes.
    # Truncate to [:72] after tagging for parity with every other AWF-authored
    # commit subject (executor agent/recovery commits, post-validation conformance).
    # The caller (a repair path) resolves ``task_tag`` once per monitor cycle and
    # threads it in; fall back to a self-resolve only when nothing was threaded
    # (the sentinel default), preserving behavior for callers that do not pass it.
    resolved_task_tag = (
        await _resolve_task_tag(self, workspace_id)
        if isinstance(task_tag, _TaskTagUnset)
        else task_tag
    )
    message = commit_message_with_task_tag(message, resolved_task_tag)[:72]

    commit = await self._deps.runner.run(
        git_worktree_command(worktree_path, "commit", "-m", message)
    )
    if not commit.ok:
        if not await repair_agent_runtime_ownership(
            logger=_log,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            reason="dirty_worktree_post_commit_failed",
            event_name=MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
            reason_code=AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
        ):
            _log.warning(
                "monitor.dirty_worktree_post_commit_ownership_repair_failed",
                workspace_id=workspace_id,
                commit_stderr=commit.stderr[:400],
            )
            raise _MonitorAgentRuntimeOwnershipRepairFailedError(
                AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
            )
        retry = await _retry_monitor_precommit_autofix_commit_once(
            runner=self._deps.runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            message=message,
            commit_result=commit,
            operation_dirty_paths=changed_paths,
        )
        if retry is None:
            _log.warning(
                "monitor.dirty_commit_failed",
                workspace_id=workspace_id,
                stderr=commit.stderr[:400],
            )
            return False

        retry_commit, restaged_paths = retry
        if not retry_commit.ok:
            _log.warning(
                "monitor.dirty_commit_autofix_retry_failed",
                workspace_id=workspace_id,
                restaged_paths=list(restaged_paths),
                stderr=retry_commit.stderr[:400],
            )
            _log.warning(
                "monitor.dirty_commit_failed",
                workspace_id=workspace_id,
                stderr=commit.stderr[:400],
            )
            return False
        _log.info(
            "monitor.dirty_commit_autofix_retry_succeeded",
            workspace_id=workspace_id,
            restaged_paths=list(restaged_paths),
        )
    _log.info("monitor.dirty_worktree_committed", workspace_id=workspace_id)

    if not await repair_agent_runtime_ownership(
        logger=_log,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="dirty_worktree_post_commit_succeeded",
        event_name=MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
        reason_code=AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
    ):
        _log.warning(
            "monitor.dirty_worktree_post_commit_succeeded_ownership_repair_failed",
            workspace_id=workspace_id,
        )
        raise _MonitorAgentRuntimeOwnershipRepairFailedError(
            AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
        )
    return True


async def _repair_protected_scope_commits_before_push(
    self: Any,
    *,
    workspace_id: str,
    pr_number: int,
    protected_scope_block: _ProtectedScopePushBlock,
    compose_project: str,
    compose_file: Path,
    remote_branch: str,
    remote_push_url: str | None = None,
    status: PRStatus | None = None,
    state: MonitorState | None = None,
    base_branch: str = "",
    operation_id: str | None = None,
    operation_type: str | None = None,
    monitor_log: WorkspaceLogSink | None = None,
    operation_start_head: str | None = None,
    source_head_sha: str | None = None,
) -> _GitPushResult:
    """Roll back a protected-scope repair delta before any push occurs."""

    del compose_project, compose_file, state

    if (
        protected_scope_block.reason_code != _PROTECTED_SCOPE_PUSH_BLOCKED_REASON
        or not protected_scope_block.violations
    ):
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr=protected_scope_block.message,
            reason_code=protected_scope_block.reason_code,
        )

    violations = list(protected_scope_block.violations)
    paths = _quality_gate_violation_paths(violations)
    worktree_path = self._worktrees_root / workspace_id
    attempted_head = await self._rev_parse_head(worktree_path)
    await self._record_pr_monitor_audit_event(
        workspace_id=workspace_id,
        event_type=_AUDIT_GIT_PUSH_EVENT,
        action="protected_scope_transactional_rollback",
        outcome="requested",
        reason_code=_PROTECTED_SCOPE_PUSH_BLOCKED_REASON,
        pr_number=pr_number,
        status=status,
        base_branch=base_branch,
        remote_branch=remote_branch,
        operation_id=operation_id,
        operation_type=operation_type,
        monitor_log=monitor_log,
        source_head_sha=source_head_sha or operation_start_head,
        evidence={
            "phase": "pre_push_committed_diff",
            "paths": paths,
            "protected_patterns": [violation.protected_pattern for violation in violations],
            "violations": quality_gate_violation_details(violations),
            "message": protected_scope_block.message,
            "operation_start_head_sha": operation_start_head,
            "attempted_head_sha": attempted_head,
            "rollback_strategy": "git_reset_hard_to_operation_start",
            "pushed": False,
        },
    )
    _log.warning(
        "monitor.protected_scope_transactional_rollback_requested",
        workspace_id=workspace_id,
        paths=paths,
        operation_start_head=operation_start_head,
        attempted_head=attempted_head,
    )
    if not operation_start_head:
        details: dict[str, object] = {
            "phase": "pre_push_committed_diff",
            "paths": paths,
            "protected_patterns": [violation.protected_pattern for violation in violations],
            "violations": quality_gate_violation_details(violations),
            "operation_start_head_sha": operation_start_head,
            "attempted_head_sha": attempted_head,
            "rollback_strategy": "git_reset_hard_to_operation_start",
            "rollback_status": "skipped_missing_operation_start_head",
            "branch_restored": False,
            "pushed": False,
        }
        await self._record_protected_scope_rollback_result(
            workspace_id=workspace_id,
            pr_number=pr_number,
            status=status,
            base_branch=base_branch,
            remote_branch=remote_branch,
            operation_id=operation_id,
            operation_type=operation_type,
            monitor_log=monitor_log,
            outcome="failed",
            reason_code=protected_scope_block.reason_code,
            source_head_sha=source_head_sha or operation_start_head,
            details=details,
        )
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr=(
                f"{protected_scope_block.message}\n"
                "AWF could not roll back the protected-scope repair because "
                "the operation start commit was unavailable; no push was attempted."
            ),
            reason_code=protected_scope_block.reason_code,
            details=details,
        )

    return cast(
        _GitPushResult,
        await self._rollback_protected_scope_repair_delta_before_push(
            workspace_id=workspace_id,
            pr_number=pr_number,
            worktree_path=worktree_path,
            protected_scope_block=protected_scope_block,
            operation_start_head=operation_start_head,
            attempted_head=attempted_head,
            remote_branch=remote_branch,
            remote_push_url=remote_push_url,
            status=status,
            base_branch=base_branch,
            operation_id=operation_id,
            operation_type=operation_type,
            monitor_log=monitor_log,
            source_head_sha=source_head_sha or operation_start_head,
        ),
    )


async def _rollback_protected_scope_repair_delta_before_push(
    self: Any,
    *,
    workspace_id: str,
    pr_number: int,
    worktree_path: Path,
    protected_scope_block: _ProtectedScopePushBlock,
    operation_start_head: str,
    attempted_head: str,
    remote_branch: str,
    remote_push_url: str | None = None,
    status: PRStatus | None = None,
    base_branch: str = "",
    operation_id: str | None = None,
    operation_type: str | None = None,
    monitor_log: WorkspaceLogSink | None = None,
    source_head_sha: str | None = None,
) -> _GitPushResult:
    del remote_push_url

    violations = list(protected_scope_block.violations)
    paths = _quality_gate_violation_paths(violations)
    delta_evidence = await self._protected_scope_repair_delta_paths(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        operation_start_head=operation_start_head,
    )
    reverted_paths = list(delta_evidence.reverted_paths)
    cleanup_paths = list(delta_evidence.cleanup_paths)
    reset_result = await self._deps.runner.run(
        git_worktree_command(worktree_path, "reset", "--hard", operation_start_head)
    )
    clean_result: CommandResult | None = None
    if reset_result.ok and cleanup_paths:
        clean_result = await self._deps.runner.run(
            git_worktree_command(
                worktree_path,
                "--literal-pathspecs",
                "clean",
                "-fd",
                "--",
                *cleanup_paths,
            )
        )
    untracked_evidence_complete = not any(
        error.get("phase") == "worktree_status_command"
        for error in delta_evidence.collection_errors
    )
    branch_reset = reset_result.ok
    cleanup_succeeded = clean_result is None or clean_result.ok
    branch_restored = branch_reset and untracked_evidence_complete and cleanup_succeeded
    if branch_restored:
        rollback_status = "succeeded"
    elif branch_reset and not untracked_evidence_complete:
        rollback_status = "reset_succeeded_cleanup_uncertain"
    elif branch_reset:
        rollback_status = "reset_succeeded_cleanup_failed"
    else:
        rollback_status = "failed"
    details: dict[str, object] = {
        "phase": "pre_push_committed_diff",
        "paths": paths,
        "protected_patterns": [violation.protected_pattern for violation in violations],
        "violations": quality_gate_violation_details(violations),
        "message": protected_scope_block.message,
        "operation_start_head_sha": operation_start_head,
        "attempted_head_sha": attempted_head,
        "rollback_strategy": "git_reset_hard_to_operation_start",
        "rollback_status": rollback_status,
        "branch_reset": branch_reset,
        "branch_restored": branch_restored,
        "untracked_evidence_complete": untracked_evidence_complete,
        "reverted_paths": reverted_paths,
        "cleanup_paths": cleanup_paths,
        "pushed": False,
        "reset_returncode": reset_result.returncode,
    }
    if reset_result.stderr:
        details["reset_stderr"] = reset_result.stderr[:400]
    if clean_result is not None:
        details["clean_attempted"] = True
        details["clean_returncode"] = clean_result.returncode
    if clean_result is not None and clean_result.stderr:
        details["clean_stderr"] = clean_result.stderr[:400]
    if delta_evidence.collection_errors:
        details["reverted_path_collection_errors"] = list(delta_evidence.collection_errors)

    await self._record_protected_scope_rollback_result(
        workspace_id=workspace_id,
        pr_number=pr_number,
        status=status,
        base_branch=base_branch,
        remote_branch=remote_branch,
        operation_id=operation_id,
        operation_type=operation_type,
        monitor_log=monitor_log,
        outcome="succeeded" if branch_restored else "failed",
        reason_code=protected_scope_block.reason_code,
        source_head_sha=source_head_sha or operation_start_head,
        details=details,
    )
    _log.warning(
        "monitor.protected_scope_transactional_rollback_completed",
        workspace_id=workspace_id,
        paths=paths,
        reverted_paths=reverted_paths,
        cleanup_paths=cleanup_paths,
        operation_start_head=operation_start_head,
        attempted_head=attempted_head,
        branch_reset=branch_reset,
        branch_restored=branch_restored,
        untracked_evidence_complete=untracked_evidence_complete,
        reverted_path_collection_errors=delta_evidence.collection_errors,
    )

    if branch_restored:
        stderr = (
            f"{protected_scope_block.message}\n"
            f"AWF rolled back the local repair delta to {operation_start_head}; "
            "no partial protected-scope repair was pushed."
        )
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr=stderr,
            reason_code=protected_scope_block.reason_code,
            details=details,
        )

    if branch_reset and not untracked_evidence_complete:
        stderr = (
            f"{protected_scope_block.message}\n"
            f"AWF reset the local repair delta to {operation_start_head} "
            "but could not collect untracked cleanup paths; "
            "untracked repair leftovers may remain. No push was attempted."
        )
    else:
        stderr = (
            f"{protected_scope_block.message}\n"
            "AWF attempted to roll back the protected-scope repair before push, "
            "but local rollback failed; no push was attempted."
        )
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=(
            reset_result.returncode
            if not reset_result.ok
            else clean_result.returncode
            if clean_result is not None
            else 1
        ),
        stderr=stderr,
        reason_code=protected_scope_block.reason_code,
        details=details,
    )


async def _protected_scope_repair_delta_paths(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    operation_start_head: str,
) -> _ProtectedScopeRollbackDeltaEvidence:
    paths: set[str] = set()
    cleanup_paths: set[str] = set()
    collection_errors: list[dict[str, object]] = []
    committed = await self._deps.runner.run(
        git_worktree_command(
            worktree_path,
            "diff",
            "--name-status",
            "-z",
            f"{operation_start_head}..HEAD",
        )
    )
    if committed.ok:
        try:
            paths.update(_changed_paths_from_name_status_z(committed.stdout))
        except ProtectedScopeDiffError as exc:
            _log.warning(
                "monitor.protected_scope_transactional_rollback_diff_parse_failed",
                workspace_id=workspace_id,
                error=str(exc),
            )
            fallback = await self._deps.runner.run(
                git_worktree_command(
                    worktree_path,
                    "diff",
                    "--name-only",
                    "-z",
                    f"{operation_start_head}..HEAD",
                )
            )
            if fallback.ok:
                try:
                    paths.update(_changed_paths_from_name_only_z(fallback.stdout))
                except ProtectedScopeDiffError as fallback_exc:
                    collection_errors.extend(
                        [
                            {
                                "phase": "committed_diff_parse",
                                "message": str(exc),
                            },
                            {
                                "phase": "committed_diff_name_only_fallback_parse",
                                "message": str(fallback_exc),
                            },
                        ]
                    )
            else:
                collection_errors.extend(
                    [
                        {
                            "phase": "committed_diff_parse",
                            "message": str(exc),
                        },
                        {
                            "phase": "committed_diff_name_only_fallback_command",
                            "returncode": fallback.returncode,
                            "stderr": fallback.stderr[:400],
                        },
                    ]
                )
    else:
        _log.warning(
            "monitor.protected_scope_transactional_rollback_diff_failed",
            workspace_id=workspace_id,
            returncode=committed.returncode,
            stderr=committed.stderr[:400],
        )
        collection_errors.append(
            {
                "phase": "committed_diff_command",
                "returncode": committed.returncode,
                "stderr": committed.stderr[:400],
            }
        )
    status = await self._deps.runner.run(
        git_worktree_command(worktree_path, "status", "--porcelain", "-z")
    )
    if status.ok:
        paths.update(_changed_paths_from_porcelain_z(status.stdout))
        cleanup_paths.update(_untracked_paths_from_porcelain_z(status.stdout))
    else:
        _log.warning(
            "monitor.protected_scope_transactional_rollback_status_failed",
            workspace_id=workspace_id,
            returncode=status.returncode,
            stderr=status.stderr[:400],
        )
        collection_errors.append(
            {
                "phase": "worktree_status_command",
                "returncode": status.returncode,
                "stderr": status.stderr[:400],
            }
        )
    return _ProtectedScopeRollbackDeltaEvidence(
        reverted_paths=tuple(sorted(paths)),
        cleanup_paths=tuple(sorted(cleanup_paths)),
        collection_errors=tuple(collection_errors),
    )


async def _record_protected_scope_rollback_result(
    self: Any,
    *,
    workspace_id: str,
    pr_number: int,
    status: PRStatus | None,
    base_branch: str,
    remote_branch: str | None,
    operation_id: str | None,
    operation_type: str | None,
    monitor_log: WorkspaceLogSink | None,
    outcome: str,
    reason_code: str,
    source_head_sha: str | None,
    details: Mapping[str, object],
) -> None:
    await self._write_monitor_log(
        monitor_log,
        {
            "event": "monitor.protected_scope_transactional_rollback",
            "workspace_id": workspace_id,
            "outcome": outcome,
            "reason_code": reason_code,
            **dict(details),
        },
    )
    await self._record_pr_monitor_audit_event(
        workspace_id=workspace_id,
        event_type=_AUDIT_GIT_PUSH_EVENT,
        action="protected_scope_transactional_rollback",
        outcome=outcome,
        reason_code=reason_code,
        pr_number=pr_number,
        status=status,
        base_branch=base_branch,
        remote_branch=remote_branch,
        operation_id=operation_id,
        operation_type=operation_type,
        monitor_log=monitor_log,
        source_head_sha=source_head_sha,
        evidence=details,
    )


async def _repair_protected_scope_changes_before_commit(
    self: Any,
    *,
    workspace_id: str,
    status_stdout: str,
    compose_project: str,
    compose_file: Path,
    state: MonitorState | None = None,
    protected_scope_revert_remote_branch: str | None = None,
    remote_push_url: str | None = None,
) -> CommandResult | None:
    """Give the agent one chance to remove protected out-of-scope edits.

    The check runs before commit/push so protected files such as GitHub
    workflow definitions never enter the PR branch history unless the task
    explicitly owns them. That matters for OAuth tokens that cannot push
    workflow changes at all, and for merge safety more generally.
    """

    violations = await self._protected_scope_violations_for_status(
        workspace_id=workspace_id,
        status_stdout=status_stdout,
    )
    if violations and protected_scope_revert_remote_branch is not None:
        filtered_violations = await self._protected_scope_violations_not_restored_to_remote_branch(
            workspace_id=workspace_id,
            status_stdout=status_stdout,
            violations=violations,
            remote_branch=protected_scope_revert_remote_branch,
            remote_push_url=remote_push_url,
        )
        violations = filtered_violations
    if not violations:
        return CommandResult(returncode=0, stdout=status_stdout, stderr="")

    prompt = await self._protected_scope_repair_prompt(
        workspace_id=workspace_id,
        violations=violations,
    )
    _log.warning(
        "monitor.protected_scope_repair_requested",
        workspace_id=workspace_id,
        paths=_quality_gate_violation_paths(violations),
    )
    if await self._provider_recovery_suppresses_cli(workspace_id):
        raise ProviderRecoveryRetryError()
    agent_run_err = None
    try:
        await self._deps.adapter.run(
            compose_project=compose_project,
            compose_file=compose_file,
            prompt=prompt,
            workspace_id=workspace_id,
            log_source="recovery",
        )
    except AgentRunError as exc:
        agent_run_err = exc
        await self._handle_provider_agent_run_error(workspace_id, exc, state=state)

    worktree_path = self._worktrees_root / workspace_id
    repaired_status = await self._deps.runner.run(
        git_worktree_command(worktree_path, "status", "--porcelain")
    )
    if not repaired_status.ok:
        return None
    remaining = await self._protected_scope_violations_for_status(
        workspace_id=workspace_id,
        status_stdout=repaired_status.stdout,
    )
    if remaining and protected_scope_revert_remote_branch is not None:
        filtered_remaining = await self._protected_scope_violations_not_restored_to_remote_branch(
            workspace_id=workspace_id,
            status_stdout=repaired_status.stdout,
            violations=remaining,
            remote_branch=protected_scope_revert_remote_branch,
            remote_push_url=remote_push_url,
        )
        remaining = filtered_remaining
    if remaining:
        _log.warning(
            "monitor.protected_scope_repair_failed",
            workspace_id=workspace_id,
            paths=_quality_gate_violation_paths(remaining),
            cli_failed=agent_run_err is not None,
        )
        await self._append_workspace_events(
            workspace_id=workspace_id,
            events=[
                WorkspaceEventCreate(
                    event_type="workspace.monitor_protected_scope_repair_failed",
                    reason_code=_PROTECTED_SCOPE_REPAIR_FAILED_REASON,
                    payload={
                        "paths": _quality_gate_violation_paths(remaining),
                        "protected_patterns": [
                            violation.protected_pattern for violation in remaining
                        ],
                        "violations": quality_gate_violation_details(remaining),
                        "message": quality_gate_violation_message(remaining),
                    },
                )
            ],
        )
        return None
    _log.info(
        "monitor.protected_scope_repair_succeeded",
        workspace_id=workspace_id,
        paths=_quality_gate_violation_paths(violations),
    )
    return cast(CommandResult | None, repaired_status)


async def _protected_scope_violations_not_restored_to_remote_branch(
    self: Any,
    *,
    workspace_id: str,
    status_stdout: str,
    violations: list[QualityGateViolation],
    remote_branch: str,
    remote_push_url: str | None = None,
) -> list[QualityGateViolation]:
    """Filter out protected dirty paths that restore the remote PR branch tree."""

    if not violations:
        return []
    untracked_paths = set(_untracked_paths_from_porcelain(status_stdout))
    worktree_path = self._worktrees_root / workspace_id
    remote = remote_push_url or "origin"
    fetch_result = await self._deps.runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "fetch",
            remote,
            f"refs/heads/{remote_branch}",
        ]
    )
    if not fetch_result.ok:
        message = (
            "Could not verify protected-scope restore against the remote PR branch: "
            f"fetch refs/heads/{remote_branch} exit={fetch_result.returncode} "
            f"stdout={fetch_result.stdout.strip() or '<empty>'} "
            f"stderr={fetch_result.stderr.strip() or '<empty>'}"
        )
        _log.warning(
            "monitor.protected_scope_revert_baseline_fetch_failed",
            workspace_id=workspace_id,
            stderr=fetch_result.stderr[:400],
        )
        raise ProtectedScopeDiffError(message)

    remaining: list[QualityGateViolation] = []
    restored_paths: list[str] = []
    for violation in violations:
        if violation.path in untracked_paths:
            remote_blob_result = await self._deps.runner.run(
                [
                    "git",
                    *git_safe_directory_config_args(worktree_path),
                    "-C",
                    str(worktree_path),
                    "rev-parse",
                    "--verify",
                    f"FETCH_HEAD:{violation.path}^{{blob}}",
                ]
            )
            if not remote_blob_result.ok:
                remaining.append(violation)
                continue
            worktree_blob_result = await self._deps.runner.run(
                [
                    "git",
                    *git_safe_directory_config_args(worktree_path),
                    "-C",
                    str(worktree_path),
                    "hash-object",
                    "--path",
                    violation.path,
                    "--",
                    violation.path,
                ]
            )
            if not worktree_blob_result.ok:
                message = (
                    "Could not verify protected-scope restore against the remote PR branch: "
                    f"hash-object --path {violation.path} exit={worktree_blob_result.returncode} "
                    f"stdout={worktree_blob_result.stdout.strip() or '<empty>'} "
                    f"stderr={worktree_blob_result.stderr.strip() or '<empty>'}"
                )
                _log.warning(
                    "monitor.protected_scope_revert_diff_failed",
                    workspace_id=workspace_id,
                    path=violation.path,
                    stderr=worktree_blob_result.stderr[:400],
                )
                raise ProtectedScopeDiffError(message)
            if remote_blob_result.stdout.strip() == worktree_blob_result.stdout.strip():
                restored_paths.append(violation.path)
                continue
            remaining.append(violation)
            continue

        diff_result = await self._deps.runner.run(
            [
                "git",
                *git_safe_directory_config_args(worktree_path),
                "-C",
                str(worktree_path),
                "diff",
                "--quiet",
                "FETCH_HEAD",
                "--",
                violation.path,
            ]
        )
        if diff_result.returncode == 0:
            restored_paths.append(violation.path)
            continue
        if diff_result.returncode == 1:
            remaining.append(violation)
            continue
        message = (
            "Could not verify protected-scope restore against the remote PR branch: "
            f"diff FETCH_HEAD -- {violation.path} exit={diff_result.returncode} "
            f"stdout={diff_result.stdout.strip() or '<empty>'} "
            f"stderr={diff_result.stderr.strip() or '<empty>'}"
        )
        _log.warning(
            "monitor.protected_scope_revert_diff_failed",
            workspace_id=workspace_id,
            path=violation.path,
            stderr=diff_result.stderr[:400],
        )
        raise ProtectedScopeDiffError(message)

    if restored_paths:
        _log.info(
            "monitor.protected_scope_revert_verified",
            workspace_id=workspace_id,
            paths=restored_paths,
        )
    return remaining


async def _protected_scope_violations_for_status(
    self: Any,
    *,
    workspace_id: str,
    status_stdout: str,
) -> list[QualityGateViolation]:
    changed_paths = _changed_paths_from_porcelain(status_stdout)
    if not changed_paths:
        return []
    async with self._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        if workspace is None:
            return []
        owned_paths = list(workspace.owned_paths)
    try:
        protected_file_diffs = await self._protected_file_diffs_for_status_paths(
            worktree_path=self._worktrees_root / workspace_id,
            changed_paths=changed_paths,
            owned_paths=owned_paths,
        )
    except RuntimeError as exc:
        raise ProtectedScopeDiffError(
            "Could not read dirty protected-scope file contents "
            "for validation before commit: "
            f"{exc}"
        ) from exc
    return find_protected_quality_gate_changes(
        changed_paths=changed_paths,
        owned_paths=owned_paths,
        protected_file_diffs=protected_file_diffs,
    )


async def _protected_scope_violations_for_unpushed_commits(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    remote_branch: str,
    remote_push_url: str | None = None,
) -> list[QualityGateViolation]:
    async with self._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        if workspace is None:
            raise ProtectedScopeDiffError(
                f"Workspace row {workspace_id} disappeared; cannot load owned_paths "
                "for protected-scope validation."
            )
        owned_paths = list(workspace.owned_paths)

    local_base, changed_paths = await self._remote_branch_diff_base_and_changed_paths(
        worktree_path=worktree_path,
        remote_branch=remote_branch,
        remote_push_url=remote_push_url,
    )
    if not changed_paths:
        return []
    try:
        protected_file_diffs = await protected_file_diffs_for_committed_paths(
            self._deps.runner,
            worktree_path=worktree_path,
            base_ref=local_base,
            changed_paths=changed_paths,
            owned_paths=owned_paths,
        )
    except RuntimeError as exc:
        raise ProtectedScopeDiffError(
            "Could not read committed protected-scope file contents "
            "against the remote PR branch for validation: "
            f"{exc}"
        ) from exc
    return find_protected_quality_gate_changes(
        changed_paths=changed_paths,
        owned_paths=owned_paths,
        protected_file_diffs=protected_file_diffs,
    )


async def _protected_scope_violations_for_sync_base_push(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    remote_branch: str,
    base_branch: str,
    remote_push_url: str | None = None,
) -> list[QualityGateViolation]:
    async with self._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        if workspace is None:
            raise ProtectedScopeDiffError(
                f"Workspace row {workspace_id} disappeared; cannot load owned_paths "
                "for protected-scope validation."
            )
        owned_paths = list(workspace.owned_paths)

    (
        remote_branch_base,
        changed_from_remote,
    ) = await self._remote_branch_diff_base_and_changed_paths(
        worktree_path=worktree_path,
        remote_branch=remote_branch,
        remote_push_url=remote_push_url,
    )
    if not changed_from_remote:
        return []

    try:
        await self._fetch_base(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            base_branch=base_branch,
        )
    except BaseFetchError as exc:
        raise ProtectedScopeDiffError(
            f"Could not refresh the base branch for protected-scope validation: {exc}"
        ) from exc

    merged_base = await self._merge_base_with_head(
        worktree_path=worktree_path,
        ref=f"origin/{base_branch}",
        error_context="against the merged base branch",
    )
    changed_from_base = await self._changed_paths_between_ref_and_head(
        worktree_path=worktree_path,
        ref=merged_base,
        error_context="against the merged base commit",
    )
    if not changed_from_base:
        return []

    changed_from_base_set = set(changed_from_base)
    sync_base_authored_paths = tuple(
        path for path in changed_from_remote if path in changed_from_base_set
    )
    if not sync_base_authored_paths:
        return []
    try:
        protected_file_diffs = await protected_file_diffs_for_committed_paths(
            self._deps.runner,
            worktree_path=worktree_path,
            base_ref=remote_branch_base,
            changed_paths=sync_base_authored_paths,
            owned_paths=owned_paths,
        )
    except RuntimeError as exc:
        raise ProtectedScopeDiffError(
            "Could not read sync-base protected-scope file contents "
            "against the remote PR branch for validation: "
            f"{exc}"
        ) from exc
    return find_protected_quality_gate_changes(
        changed_paths=sync_base_authored_paths,
        owned_paths=owned_paths,
        protected_file_diffs=protected_file_diffs,
    )


async def _protected_scope_diff_unavailable_block(
    self: Any,
    *,
    workspace_id: str,
    remote_branch: str,
    exc: ProtectedScopeDiffError,
) -> _ProtectedScopePushBlock:
    redacted_error = redact_audit_text(str(exc), limit=1000)
    message = (
        "AWF could not verify protected-scope changes before push; "
        "refusing to push this repair until the PR branch diff baseline "
        f"is available. {redacted_error}"
    )
    await self._append_workspace_events(
        workspace_id=workspace_id,
        events=[
            WorkspaceEventCreate(
                event_type="workspace.monitor_protected_scope_push_blocked",
                reason_code=_PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
                payload={
                    "reason": "diff_baseline_unavailable",
                    "remote_branch": remote_branch,
                    "error": redacted_error,
                },
            )
        ],
    )
    return _ProtectedScopePushBlock(
        message=message,
        reason_code=_PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
    )


async def _protected_scope_diff_unavailable_push_result(
    self: Any,
    *,
    workspace_id: str,
    remote_branch: str,
    exc: ProtectedScopeDiffError,
) -> _GitPushResult:
    block = await self._protected_scope_diff_unavailable_block(
        workspace_id=workspace_id,
        remote_branch=remote_branch,
        exc=exc,
    )
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr=block.message,
        reason_code=block.reason_code,
    )


async def _protected_scope_push_block(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    remote_branch: str,
    remote_push_url: str | None = None,
    base_branch: str | None = None,
) -> _ProtectedScopePushBlock | None:
    if not worktree_path.exists():
        return None
    try:
        if base_branch is None:
            violations = await self._protected_scope_violations_for_unpushed_commits(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                remote_branch=remote_branch,
                remote_push_url=remote_push_url,
            )
        else:
            violations = await self._protected_scope_violations_for_sync_base_push(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                remote_branch=remote_branch,
                base_branch=base_branch,
                remote_push_url=remote_push_url,
            )
    except ProtectedScopeDiffError as exc:
        return cast(
            _ProtectedScopePushBlock | None,
            await self._protected_scope_diff_unavailable_block(
                workspace_id=workspace_id,
                remote_branch=remote_branch,
                exc=exc,
            ),
        )
    if not violations:
        return None
    message = quality_gate_violation_message(violations)
    await self._append_workspace_events(
        workspace_id=workspace_id,
        events=[
            WorkspaceEventCreate(
                event_type="workspace.monitor_protected_scope_push_blocked",
                reason_code=_PROTECTED_SCOPE_PUSH_BLOCKED_REASON,
                payload={
                    "paths": _quality_gate_violation_paths(violations),
                    "protected_patterns": [violation.protected_pattern for violation in violations],
                    "violations": quality_gate_violation_details(violations),
                    "message": message,
                },
            )
        ],
    )
    return _ProtectedScopePushBlock(
        message=message,
        reason_code=_PROTECTED_SCOPE_PUSH_BLOCKED_REASON,
        violations=tuple(violations),
    )
