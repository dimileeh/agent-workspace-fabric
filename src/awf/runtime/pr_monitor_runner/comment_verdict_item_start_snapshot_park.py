"""Park a review item before any agent run when its Git config snapshot fails (#1081).

Kept separate so ``comment_verdict`` stays under the first-party line budget.

At item start, pre-launch (``prepare_item_protocol_anchors``) snapshots the
worktree's local Git configs, so a later rollback can undo Git settings the
agent changed. When that snapshot fails, AWF already knows it cannot undo an
agent run. The agent used to run anyway. Every rollback after such a run then
refused to park the item and raised the terminal ``AgentVerdictProtocolError``,
so the batch skipped its end-of-batch push and the workspace failed, breaking
the #1074/#1076 rule that one review item must never kill the monitor.

Now pre-launch raises ``ItemStartGitConfigSnapshotFailedError`` instead, and the
protocol entry point (``comment_verdict_entrypoint``) turns it into a
``needs_human`` item with ``item_start_git_config_snapshot_failed_result``.
Nothing has run, so the worktree is unchanged and there is nothing to roll back
or push. The rollback guards that refuse to park without a baseline
(``reraise_or_park_item``, ``park_unaccepted_correction`` and
``_clean_edits_above_kept_commit``) stay as defense in depth.
"""

from __future__ import annotations

from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    AGENT_VERDICT_ITEM_START_GIT_CONFIG_SNAPSHOT_FAILED,
    VerdictResult,
)


class ItemStartGitConfigSnapshotFailedError(Exception):
    """Internal signal: the item-start Git config snapshot failed, so the item is not run.

    Raised only by pre-launch, before any agent invocation, and caught only by
    the protocol entry point. It never leaves the item protocol.
    """

    def __init__(self) -> None:
        super().__init__("Item-start Git config snapshot failed; the item was not attempted.")


def item_start_git_config_snapshot_failed_result() -> VerdictResult:
    """Park the item ``needs_human``; pre-launch already logged the warning."""
    return VerdictResult(
        verdict="needs_human",
        reason=(
            f"{AGENT_VERDICT_ITEM_START_GIT_CONFIG_SNAPSHOT_FAILED}: AWF could not save this "
            "worktree's Git settings at item start, so it could not safely undo an agent "
            "run. The item was not attempted and is parked for human review."
        ),
    )
