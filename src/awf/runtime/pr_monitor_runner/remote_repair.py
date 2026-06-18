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
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from awf.common.task_tag import commit_message_with_task_tag
from awf.db.repositories import (
    MergeCandidateRepository,
    WorkspaceRepository,
)
from awf.runtime.ownership import (
    MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    repair_agent_runtime_ownership,
)
from awf.runtime.pr_monitor import (
    MonitorState,
)
from awf.runtime.pr_monitor_runner.commit_autofix import (
    _retry_monitor_precommit_autofix_commit_once,
)
from awf.runtime.pr_monitor_runner.constants import (
    _PRE_EXISTING_DIRTY_WORKTREE_REASON,
    _REPAIR_START_HEAD_UNAVAILABLE_REASON,
    _REPAIR_WORKTREE_STATUS_FAILED_REASON,
    _TASK_TAG_UNSET,
    _TaskTagUnset,
)
from awf.runtime.pr_monitor_runner.git_utils import (
    git_worktree_command,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _changed_paths_from_porcelain,
    _untracked_paths_from_porcelain,
)
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.remote_ops import (
    AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
    _GitPushResult,
)
from awf.runtime.pr_monitor_runner.types import (
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorPolicyBlockedError,
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


async def _resolve_block_resume_phase(self: Any, workspace_id: str) -> str | None:
    """Load the workspace's recorded protected-scope block resume phase.

    Persisted by ``enter_blocked_for_protected_violation_in_session`` and used to
    discriminate a sync-base-originated pause (``monitor_protected_scope_sync_base``)
    from a generic push pause or a no-block remonitor when selecting the
    protected-scope validator on an operator-hint resume."""
    async with self._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        return workspace.block_resume_phase if workspace is not None else None


async def _clear_block_resume_phase(self: Any, workspace_id: str) -> None:
    """Clear the recorded protected-scope block resume phase once its resume settles.

    The phase column discriminates a sync-base-originated pause
    (``monitor_protected_scope_sync_base``) when ``_run_operator_hint_cycle`` selects
    the protected-scope validator. It is set at block time and never overwritten
    except by a fresh block. A later operator-hint or remonitor cycle on
    ``monitoring_pr`` arms a hint WITHOUT re-blocking, so the stale sync-base phase
    would still select the sync-base-aware validator — letting a repair that reverts
    an unowned protected file back to base contents push without a grant or re-block.
    Reset it to ``None`` once the resume is finalized so the next cycle falls back to
    the generic unpushed-commit validator (PRRT_kwDOSJAM6s6KFqEg)."""
    async with self._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        if workspace is None or workspace.block_resume_phase is None:
            return
        workspace.block_resume_phase = None
        await session.commit()


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
    # Decide dirtiness with the SAME untracked AWF-agent-runtime exclusion the
    # pre-existing-dirty guard and the staging filter below apply. A worktree
    # dirtied only by reviewer subagent memory (untracked ``.claude/agent-memory/...``)
    # must short-circuit here, BEFORE any commit-side effects — supply-chain policy
    # refresh, agent-runtime ownership repair, and protected-scope repair (which can
    # launch the agent CLI) — exactly as the guard and staging logic intentionally
    # skip it. ``--untracked-files=all`` is load-bearing: with git's default
    # ``normal`` mode a fully-untracked ``.claude/`` collapses to a single
    # ``?? .claude/`` entry that is NOT under ``.claude/agent-memory/`` and so would
    # escape the agent-runtime filter, letting memory-only dirt fall through into the
    # side-effecting path. Enumerating leaf paths lets the filter drop the memory files.
    status = await self._deps.runner.run(
        git_worktree_command(worktree_path, "status", "--porcelain", "--untracked-files=all")
    )
    if not status.ok:
        _log.warning(
            "monitor.dirty_check_failed",
            workspace_id=workspace_id,
            stderr=status.stderr[:400],
        )
        return False
    untracked = set(_untracked_paths_from_porcelain(status.stdout))
    changed_paths = tuple(
        path
        for path in _changed_paths_from_porcelain(status.stdout)
        if not (path in untracked and is_under_agent_runtime_root(path))
    )
    if not changed_paths:
        return False

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

    # The pre-existing-dirty guard (``_pre_existing_dirty_repair_worktree_result``)
    # lets a repair run when the only dirt is UNTRACKED AWF-agent-runtime memory
    # (reviewer subagent ``.claude/agent-memory/...`` files), which never belongs
    # to the PR. The commit path must apply the SAME exclusion before staging, or a
    # blind ``git add -A`` would stage that pre-existing memory back into the PR.
    # ``--untracked-files=all`` is load-bearing here, exactly as in the guard: with
    # git's default ``normal`` mode a fully-untracked ``.claude/`` collapses to a
    # single ``?? .claude/`` entry that escapes the agent-runtime filter; enumerating
    # leaf paths lets the filter drop the memory files. If nothing else remains to
    # stage, there is no PR-worthy change — return False like the clean path above.
    stage_status = await self._deps.runner.run(
        git_worktree_command(worktree_path, "status", "--porcelain", "--untracked-files=all")
    )
    if not stage_status.ok:
        _log.warning(
            "monitor.dirty_stage_status_failed",
            workspace_id=workspace_id,
            stderr=stage_status.stderr[:400],
        )
        return False
    stage_untracked = set(_untracked_paths_from_porcelain(stage_status.stdout))
    stage_paths = sorted(
        path
        for path in _changed_paths_from_porcelain(stage_status.stdout)
        if not (path in stage_untracked and is_under_agent_runtime_root(path))
    )
    if not stage_paths:
        return False

    add = await self._deps.runner.run(
        git_worktree_command(worktree_path, "--literal-pathspecs", "add", "-A", "--", *stage_paths)
    )
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
        # Scope the autofix retry to the paths we actually staged. ``stage_paths``
        # is the leaf-enumerated (``--untracked-files=all``), agent-runtime-filtered
        # set computed above, so it never carries untracked ``.claude/agent-memory/``
        # leftovers or a collapsed ``?? .claude/`` directory entry into
        # ``operation_dirty_paths`` — which would otherwise widen the retry's
        # in-scope check beyond what this operation committed.
        retry = await _retry_monitor_precommit_autofix_commit_once(
            runner=self._deps.runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            message=message,
            commit_result=commit,
            operation_dirty_paths=stage_paths,
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
