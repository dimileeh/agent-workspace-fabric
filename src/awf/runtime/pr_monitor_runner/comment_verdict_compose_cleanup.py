"""Compose-cleanup commit sink for the comment verdict protocol.

Kept separate so ``comment_verdict`` stays under the first-party line budget;
re-exported from ``comment_verdict`` for callers and tests.

``ComposeExecCleanupError`` means the agent ran — and may have edited or
self-committed — but tearing its exec stack down failed. The protocol still
sinks the dirty worktree so residue cannot block remonitor, then rolls back to
the attempt's floor and re-raises the cleanup error: the cleanup failure is the
outcome, never a verdict. Every exit from here raises, which is why the block
extracts cleanly out of the attempt loop.

Rollback helpers are resolved through ``comment_verdict`` at call time so
monkeypatches on that module (and the ``comments`` forwarding shim) still apply.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from awf.common.compose_exec import ComposeExecCleanupError
from awf.common.logging import get_logger
from awf.runtime.pr_monitor_runner.constants import _TASK_TAG_UNSET, _TaskTagUnset
from awf.runtime.pr_monitor_runner.types import (
    SINK_INFRASTRUCTURE_ERRORS,
    ProtectedScopeDiffError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
)

if TYPE_CHECKING:
    from awf.runtime.pr_monitor import MonitorState
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner

_log = get_logger(__name__)

# Sink exits that already carry a terminal reason code ``fix_cycle`` handles
# directly. They must never be masked behind a protocol violation, even when the
# post-sink rollback also fails.
_REASON_CODED_SINK_EXITS = (
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
    ProtectedScopeDiffError,
)


async def sink_and_raise_compose_cleanup_error(
    runner: PullRequestMonitorRunner,
    *,
    compose_cleanup_error: ComposeExecCleanupError,
    workspace_id: str,
    worktree_path: Path,
    item_start_head: str | None,
    rollback_floor_head: str | None,
    item_start_last_push_sha: str | None,
    state: MonitorState | None,
    protocol_attempt: int,
    commit_message: str,
    compose_project: str,
    compose_file: Path,
    task_tag: str | None | _TaskTagUnset = _TASK_TAG_UNSET,
    command_evidence: list[str],
    commit_dirty_changes: bool,
) -> NoReturn:
    """Sink dirty changes, roll back to the attempt floor, then re-raise.

    ``rollback_floor_head`` is the commit a rollback may rewind to — this
    attempt's own start — so a cleanup failure on a re-attempt after a preserved
    timeout cannot delete the commits #932 kept. Never returns.
    """
    from awf.runtime.pr_monitor_runner import comment_verdict as _comment_verdict

    try:
        if commit_dirty_changes:
            await runner._commit_dirty_worktree(
                workspace_id=workspace_id,
                message=commit_message,
                compose_project=compose_project,
                compose_file=compose_file,
                state=state,
                command_evidence=command_evidence,
                task_tag=task_tag,
                operation_start_head=item_start_head,
            )
    except SINK_INFRASTRUCTURE_ERRORS as exc:
        # Roll back before propagating commit-sink infrastructure exits so
        # unaccepted residue does not wedge remonitor or get pushed later.
        rollback_ok = await _comment_verdict._rollback_unaccepted_protocol_retry_changes(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            item_start_head=rollback_floor_head,
            item_start_last_push_sha=item_start_last_push_sha,
            state=state,
        )
        if not rollback_ok:
            _log.warning(
                "monitor.agent_verdict_compose_cleanup_sink_rollback_failed",
                workspace_id=workspace_id,
                item_start_head=item_start_head,
                protocol_attempt=protocol_attempt,
                exc_type=type(exc).__name__,
            )
            if isinstance(exc, _REASON_CODED_SINK_EXITS):
                raise
            raise _comment_verdict.AgentVerdictProtocolError(
                reason_code=_comment_verdict.AGENT_VERDICT_PROTOCOL_VIOLATION,
                message=(
                    "Could not roll back unaccepted edits after compose cleanup "
                    "commit sink infrastructure exit."
                ),
            ) from exc
        raise
    except Exception as exc:
        # ``_commit_dirty_worktree`` can raise untyped failures (for example
        # repository/session errors from supply-chain policy refresh) after
        # the agent has already edited the worktree. Roll back before
        # propagating so unaccepted residue does not wedge remonitor or
        # get pushed later.
        rollback_ok = await _comment_verdict._rollback_unaccepted_protocol_retry_changes(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            item_start_head=rollback_floor_head,
            item_start_last_push_sha=item_start_last_push_sha,
            state=state,
        )
        if not rollback_ok:
            _log.warning(
                "monitor.agent_verdict_compose_cleanup_sink_unexpected_rollback_failed",
                workspace_id=workspace_id,
                item_start_head=item_start_head,
                protocol_attempt=protocol_attempt,
                exc_type=type(exc).__name__,
            )
            raise _comment_verdict.AgentVerdictProtocolError(
                reason_code=_comment_verdict.AGENT_VERDICT_PROTOCOL_VIOLATION,
                message=(
                    "Could not roll back unaccepted edits after unexpected "
                    "compose cleanup commit sink failure."
                ),
            ) from exc
        raise
    rollback_ok = await _comment_verdict._rollback_unaccepted_protocol_retry_changes(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        item_start_head=rollback_floor_head,
        item_start_last_push_sha=item_start_last_push_sha,
        state=state,
    )
    if not rollback_ok:
        _log.warning(
            "monitor.agent_verdict_compose_cleanup_sink_rollback_failed",
            workspace_id=workspace_id,
            item_start_head=item_start_head,
            protocol_attempt=protocol_attempt,
        )
        raise _comment_verdict.AgentVerdictProtocolError(
            reason_code=_comment_verdict.AGENT_VERDICT_PROTOCOL_VIOLATION,
            message=("Could not roll back unaccepted edits after compose cleanup commit sink."),
        ) from compose_cleanup_error
    raise compose_cleanup_error
