"""Post-attempt tip probe for the item verdict protocol.

Kept separate so ``comment_verdict`` stays under the first-party line budget.
Behavior is unchanged; the probe, rollback, and park seams are resolved through
``comment_verdict`` at call time so monkeypatches on that module still reach them.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from awf.common.logging import get_logger
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    AGENT_VERDICT_PROTOCOL_VIOLATION,
    AgentVerdictProtocolError,
)

if TYPE_CHECKING:
    from awf.runtime.pr_monitor import MonitorState
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner

_log = get_logger(__name__)


async def probe_post_attempt_tip(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
    rollback_floor_head: str | None,
    item_start_head: str | None,
    item_start_last_push_sha: str | None,
    state: MonitorState | None,
    protocol_attempt: int,
    rev_parse_head: object,
) -> str | None:
    """Return the verified tip after a protocol attempt, rolling back if the probe fails."""
    from awf.runtime.pr_monitor_runner import comment_verdict as _comment_verdict

    if not worktree_path.exists():
        return None
    try:
        return await _comment_verdict.read_protocol_attempt_start_head(
            runner,
            worktree_path=worktree_path,
            rev_parse_head=(rev_parse_head if callable(rev_parse_head) else None),
        )
    except Exception as tip_exc:
        rollback_ok = await _comment_verdict._rollback_or_classify_failure(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            item_start_head=rollback_floor_head,
            item_start_last_push_sha=item_start_last_push_sha,
            state=state,
        )
        if not rollback_ok:
            _log.warning(
                "monitor.agent_verdict_post_attempt_tip_rollback_failed",
                workspace_id=workspace_id,
                item_start_head=item_start_head,
                protocol_attempt=protocol_attempt,
                exc_type=type(tip_exc).__name__,
            )
            raise AgentVerdictProtocolError(
                reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                message=(
                    "Could not roll back unaccepted edits after post-attempt tip probe failure."
                ),
            ) from tip_exc
        _comment_verdict.reraise_or_park_item(
            tip_exc, "post_attempt_tip_probe", rollback_floor_head, worktree_path
        )
