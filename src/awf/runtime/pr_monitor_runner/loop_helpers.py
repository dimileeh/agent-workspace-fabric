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
) -> None:
    """Post the human hint without blocking workflow-scope failure handling."""
    try:
        await self._post_human_notification_once(
            repo=repo,
            pr_number=pr_number,
            status=status,
            state=state,
            blocker_reason=blocker_reason,
            preserve_full_blocker_reason=True,
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

    The workspace-level writes run AFTER the terminate sink, gated on its owner
    fence (PRRT_kwDOSJAM6s6flswY). This seam is reachable exactly when a long
    action lost its monitor claim mid-flight and only then observed the merge, so
    persisting first would let a superseded runner overwrite the live claimant's
    ``monitor_threads_addressed`` / ``monitor_last_commit_sha`` and publish a
    "monitor is done" defer signal while the row is still ``monitoring_pr`` under
    its new owner — neither write is fenced on ``monitor_claimed_by`` itself. The
    operation record is this runner's own audit row, so it is finished either way.
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
    if terminal.merged:
        terminated = await self._terminate_completed(
            workspace_id,
            pr_merge_sha=terminal.merge_commit_sha or status.head_sha,
            repo_url=repo_url,
            base_branch=base_branch,
            compose_project=compose_project,
            compose_file=compose_file,
        )
    else:
        terminated = await self._terminate_failed(
            workspace_id,
            message=f"monitor: abort ({AbortReason.pr_closed_externally.value})",
            reason_code=AbortReason.pr_closed_externally,
        )
    if terminated:
        await self._persist_state(workspace_id, state)
        self._write_defer_signal(
            workspace_id=workspace_id,
            pr_number=pr_number,
            terminal_action="ShortCircuitCompleted" if terminal.merged else "Abort",
            merged=terminal.merged,
            status=status,
            state=state,
        )
    return True
