"""Pull request monitor loop helper functions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import RepoRef
from awf.db.enums import OperationStatus
from awf.runtime.pr_monitor import AbortReason, MonitorState, PRStatus
from awf.runtime.pr_monitor_runner.helpers import _redact_and_truncate_forge_error
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult


async def _post_workflow_scope_notification_best_effort(
    self: Any,
    *,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    status: PRStatus,
    state: MonitorState,
    blocker_reason: str,
) -> _GitPushResult | None:
    """Post the human hint without blocking workflow-scope failure handling.

    Returns the moot push envelope when the notification boundary's fresh read
    found the PR already merged/closed, so an arm that would otherwise terminally
    fail can run the same terminal handling ``decide()`` would return next poll
    (PRRT_kwDOSJAM6s6fvGsp). ``None`` means "carry on exactly as before".
    """
    # ``status`` is the snapshot ``decide()`` ran on at the START of this poll
    # cycle. The agent action, the pre-push validation suite and the push whose
    # workflow-scope rejection is being escalated all ran since, and the last
    # #910 recheck sits BEFORE that push — so the caller's snapshot can say
    # "open" about a PR that has already merged or closed. ``workspace_id`` opts
    # the notification boundary into the fresh terminal re-read; it fails OPEN, so
    # an unresolvable repo or a transient forge fault leaves the escalation as-is.
    try:
        terminal = await self._post_human_notification_once(
            repo=repo,
            pr_number=pr_number,
            status=status,
            state=state,
            blocker_reason=blocker_reason,
            preserve_full_blocker_reason=True,
            workspace_id=workspace_id,
            recheck_context="workflow_scope_notification",
        )
    except ForgeClientError as exc:
        # A Bitbucket workspace posts the human hint through ``BitbucketClient``,
        # whose ``post_comment`` raises ``BitbucketClientError`` (not
        # ``GitHubClientError``). Catch it alongside the GitHub error so a
        # transient or permanent comment failure degrades to a logged warning
        # here too, instead of escaping this best-effort helper and aborting the
        # surrounding workflow-scope failure handling.
        _log.warning(
            "monitor.workflow_scope_notification_failed",
            workspace_id=workspace_id,
            pr_number=pr_number,
            head_sha=status.head_sha[:10],
            error=_redact_and_truncate_forge_error(str(exc)),
        )
        return None
    if terminal is None:
        return None
    moot_result: _GitPushResult = self._post_action_pr_terminal_push_result(terminal)
    return moot_result


async def _finish_cycle_for_terminal_pr(
    self: Any,
    *,
    workspace_id: str,
    operation: Any,
    push_result: _GitPushResult,
    state: MonitorState,
    pr_number: int,
    repo_url: str,
    base_branch: str,
    compose_project: str,
    compose_file: Path,
) -> bool:
    """Finish the cycle for an action whose PR ended mid-flight (#910).

    Returns ``False`` when the action was NOT moot, so every arm can call this
    unconditionally right after its ``_run_*`` returns. When it WAS moot, the
    monitor runs the exact terminal handling ``decide()`` would return on the next
    poll — ``ShortCircuitCompleted`` for a merged PR, ``Abort(pr_closed_externally)``
    for a closed one — instead of waiting for another poll that a paused/blocked
    workspace would never make.

    The workspace-level writes are gated on the terminate sink's owner fence
    (PRRT_kwDOSJAM6s6flswY). This seam is reachable exactly when a long action lost
    its monitor claim mid-flight and only then observed the merge, so publishing
    ahead of that fence would let a superseded runner overwrite the live claimant's
    ``monitor_threads_addressed`` / ``monitor_last_commit_sha`` and publish a
    "monitor is done" defer signal while the row is still ``monitoring_pr`` under
    its new owner — neither write is fenced on ``monitor_claimed_by`` itself. They
    run at the sink's transition COMMIT, not at its return, so the merged sink's
    cancellable post-commit cleanup cannot strand a terminal row with no artifact
    (PRRT_kwDOSJAM6s6fvDbP). The operation record is this runner's own audit row, so
    it is finished either way.
    """
    terminal = push_result.pr_terminal
    if terminal is None:
        return False
    status = terminal.status
    await self._finish_monitor_operation(
        operation,
        status=OperationStatus.succeeded,
        result={
            "status": "succeeded",
            "outcome": "pr_terminal_moot",
            "reason_code": push_result.reason_code,
            "pr_state": terminal.pr_state,
            "local_head_sha": terminal.local_head_sha,
            "pushed": False,
        },
    )

    async def _publish_terminal_writes() -> None:
        """Flush the gated workspace writes once the transition commits."""
        # The defer signal goes first: it is the artifact downstream tooling polls
        # for, and unlike ``_persist_state`` it never raises, so a DB fault on the
        # bookkeeping write cannot swallow the terminal signal.
        self._write_defer_signal(
            workspace_id=workspace_id,
            pr_number=pr_number,
            terminal_action="ShortCircuitCompleted" if terminal.merged else "Abort",
            merged=terminal.merged,
            status=status,
            state=state,
        )
        await self._persist_state(workspace_id, state)

    if terminal.merged:
        # Publish at the sink's transition commit rather than at its return: the
        # ``completed`` sink still has cancellable post-commit work (target-branch
        # reconcile + filesystem GC) after that commit, and the row is terminal from
        # the commit onward, so a cancellation inside that cleanup would leave a
        # completed workspace whose defer artifact no later monitor ever writes
        # (PRRT_kwDOSJAM6s6fvDbP).
        terminated = await self._terminate_completed(
            workspace_id,
            pr_merge_sha=terminal.merge_commit_sha or status.head_sha,
            repo_url=repo_url,
            base_branch=base_branch,
            compose_project=compose_project,
            compose_file=compose_file,
            on_transition_committed=_publish_terminal_writes,
        )
    else:
        terminated = await self._terminate_failed(
            workspace_id,
            message=f"monitor: abort ({AbortReason.pr_closed_externally.value})",
            reason_code=AbortReason.pr_closed_externally,
            on_transition_committed=_publish_terminal_writes,
        )
    if not terminated:
        # The terminate sink refused the write (superseded owner, or the row
        # already left ``monitoring_pr``). Skipping the two writes above is not
        # enough on its own: every arm returns ``True`` from here, which reaches
        # ``run()``'s unconditional post-``_execute`` ``_persist_state`` and would
        # flush this stale state onto the live claimant's row anyway — clobbering
        # its ``monitor_threads_addressed`` / ``monitor_last_commit_sha`` so open
        # feedback could read as addressed. Propagate the refusal so the outer
        # loop drops that persist too (PRRT_kwDOSJAM6s6fsqcA).
        state.monitor_writes_suppressed = True
    return True
