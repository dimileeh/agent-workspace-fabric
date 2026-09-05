"""Post-action PR terminal-state guard for the PR monitor runner (#910).

``awf.runtime.pr_monitor.decide()`` maps ``status.merged -> ShortCircuitCompleted``
and ``status.closed -> Abort(pr_closed_externally)``, but only at the START of a poll
cycle. A long agent action — comment repair, CI fix, sync-base, an operator-hint
resume — that began while the PR was still open runs to completion and then evaluates
its push / protected-scope-pause path against a PR snapshot that can be many minutes
stale. On aira-infra PR #229 a ``comment_repair`` that started at 06:30:52 finished at
06:44:47, thirteen minutes after the PR merged at 06:32:02: the workspace entered
``blocked``, posted a stale needs-human comment on the merged PR, and stopped polling,
so the loop's own merged short-circuit never ran.

Every post-action seam therefore re-reads PR state through :func:
`_post_action_pr_terminal_state` BEFORE pushing, pausing, or notifying. The guard
fails OPEN: an unresolvable repo or a transient ``ForgeClientError`` leaves today's
behavior exactly as it was, logged under its own reason code so the blip stays
attributable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import RepoRef
from awf.db.repositories import WorkspaceEventCreate, WorkspaceRepository
from awf.runtime.pr_monitor_runner.constants import (
    _MONITOR_ACTION_MOOT_PR_TERMINAL_REASON,
    _MONITOR_ACTION_MOOT_RECHECK_FAILED_REASON,
)
from awf.runtime.pr_monitor_runner.helpers import _redact_and_truncate_forge_error
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.pr_monitor_runner.types import _PostActionPrTerminalState

MONITOR_ACTION_MOOT_EVENT = "workspace.monitor_action_moot"


async def _post_action_repo_ref(self: Any, workspace_id: str) -> RepoRef | None:
    """Resolve the workspace's ``RepoRef`` for a seam that threaded none.

    Self-resolving rather than skipping the check keeps the guard from being
    silently bypassable by a call site that does not carry a ``repo``.
    """
    async with self._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        repo_url = None if workspace is None else workspace.repo_url
    if not repo_url:
        return None
    try:
        return RepoRef.from_url(repo_url)
    except ValueError:
        return None


async def _post_action_pr_terminal_state(
    self: Any,
    *,
    workspace_id: str,
    pr_number: int,
    context: str,
    operation_id: str | None = None,
    operation_type: str | None = None,
    repo: RepoRef | None = None,
    worktree_path: Path | None = None,
) -> _PostActionPrTerminalState | None:
    """Return a terminal-PR observation when the action's PR already ended.

    ``None`` means "carry on exactly as before": the PR is still open, the repo
    could not be resolved, or the re-fetch hit a transient forge fault. A
    non-``None`` result means the caller must NOT push, must NOT pause into
    ``blocked``, and must NOT post a PR comment; one
    ``workspace.monitor_action_moot`` event has already been appended recording
    the operation, the local unpushed HEAD, and the observed PR state.
    """
    resolved_repo = repo if repo is not None else await _post_action_repo_ref(self, workspace_id)
    if resolved_repo is None:
        _log.warning(
            "monitor.post_action_pr_terminal_recheck_unavailable",
            workspace_id=workspace_id,
            pr_number=pr_number,
            context=context,
            reason_code=_MONITOR_ACTION_MOOT_RECHECK_FAILED_REASON,
        )
        return None
    try:
        # ``retry=False`` mirrors the fix-cycle settle re-poll: this is a single
        # confirmation round-trip, not a polling read, so a blip must surface
        # immediately (and fail open) instead of consuming the retry budget.
        status = await self._deps.gh.fetch_pr_status(
            repo=resolved_repo,
            pr_number=pr_number,
            base_behind_count=0,
            retry=False,
        )
    except ForgeClientError as exc:
        # Fail OPEN. The original outcome (push / protected pause / notification)
        # is the caller's, and masking it behind a transient forge fault would be
        # strictly worse than the pre-#910 behavior.
        _log.warning(
            "monitor.post_action_pr_terminal_recheck_failed",
            workspace_id=workspace_id,
            pr_number=pr_number,
            context=context,
            operation_id=operation_id,
            operation_type=operation_type,
            reason_code=_MONITOR_ACTION_MOOT_RECHECK_FAILED_REASON,
            stderr=_redact_and_truncate_forge_error(exc.redacted_detail()),
        )
        return None
    if not (status.merged or status.closed):
        return None

    observation = _PostActionPrTerminalState(
        status=status,
        local_head_sha=await self._rev_parse_head(
            worktree_path if worktree_path is not None else self._worktrees_root / workspace_id
        ),
    )
    _log.warning(
        "monitor.post_action_pr_terminal",
        workspace_id=workspace_id,
        pr_number=pr_number,
        context=context,
        operation_id=operation_id,
        operation_type=operation_type,
        pr_state=observation.pr_state,
        local_head_sha=observation.local_head_sha,
        merge_commit_sha=observation.merge_commit_sha,
        reason_code=_MONITOR_ACTION_MOOT_PR_TERMINAL_REASON,
    )
    await self._append_workspace_events(
        workspace_id=workspace_id,
        events=[
            WorkspaceEventCreate(
                event_type=MONITOR_ACTION_MOOT_EVENT,
                reason_code=_MONITOR_ACTION_MOOT_PR_TERMINAL_REASON,
                payload={
                    "context": context,
                    "operation_id": operation_id,
                    "operation_type": operation_type,
                    "pr_number": pr_number,
                    "pr_state": observation.pr_state,
                    "merged": observation.merged,
                    "closed": observation.closed,
                    "merge_commit_sha": observation.merge_commit_sha,
                    "pr_head_sha": status.head_sha,
                    # The repair commit the action produced is NOT pushed and NOT
                    # rolled back; record its sha so an operator can recover it.
                    "local_head_sha": observation.local_head_sha,
                    "pushed": False,
                },
            )
        ],
    )
    return observation


def _post_action_pr_terminal_push_result(
    self: Any,
    observation: _PostActionPrTerminalState,
) -> _GitPushResult:
    """Build the non-paused, non-failed envelope for a moot monitor action.

    ``failed=False`` keeps the caller out of every failure branch (no terminal
    fail, no operator-hint needs_human parking) and ``paused_into_blocked=False``
    keeps it out of the pause branch, so the loop reaches the shared terminal
    finisher and runs the handling ``decide()`` would have chosen.
    """
    del self
    return _GitPushResult(
        pushed=False,
        failed=False,
        returncode=0,
        reason_code=_MONITOR_ACTION_MOOT_PR_TERMINAL_REASON,
        pr_terminal=observation,
    )


async def _post_action_pr_terminal_push_result_if_moot(
    self: Any,
    *,
    workspace_id: str,
    pr_number: int,
    context: str,
    operation_id: str | None = None,
    operation_type: str | None = None,
    repo: RepoRef | None = None,
    worktree_path: Path | None = None,
) -> _GitPushResult | None:
    """Run the guard and, when the PR is terminal, build the moot push result."""
    observation = await _post_action_pr_terminal_state(
        self,
        workspace_id=workspace_id,
        pr_number=pr_number,
        context=context,
        operation_id=operation_id,
        operation_type=operation_type,
        repo=repo,
        worktree_path=worktree_path,
    )
    if observation is None:
        return None
    return cast(
        _GitPushResult,
        self._post_action_pr_terminal_push_result(observation),
    )
