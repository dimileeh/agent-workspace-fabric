"""Anchor resolution and pre-launch repairs for one verdict-protocol item.

Kept separate so ``comment_verdict`` stays under the first-party line budget.
The block runs once, before the protocol attempt loop, and is self-contained:
it reads only the item's own inputs and either returns the resolved anchors or
raises the same pre-launch failures the caller already propagates.

Ownership repair, mirror discovery, hooks repair and the git-config snapshot are
resolved through ``comment_verdict`` at call time so monkeypatches on that module
(and the ``comments`` forwarding shim) keep reaching them after the module split.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from awf.common.logging import get_logger
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
    consume_item_start_head,
)
from awf.runtime.pr_monitor_runner.types import (
    _MonitorAgentRuntimeOwnershipRepairFailedError,
)

if TYPE_CHECKING:
    from awf.runtime.pr_monitor import MonitorState
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner

_log = get_logger(__name__)


@dataclass(frozen=True)
class ItemProtocolAnchors:
    """Where this item starts, and how far back a rollback may rewind."""

    worktree_path: Path
    item_path: str | None
    item_line: int | None
    item_start_head: str | None
    # Floor for every rollback in the call. Normally the same commit as the
    # evidence anchor; on the #932 re-attempt it stays at the *preserved* HEAD so
    # no rollback can delete the timed-out attempt's kept commits (#934 audit
    # item). Never rewound to the restored original item start.
    rollback_floor_head: str | None
    # Resolved once here and carried to the attempt loop so a later probe cannot
    # observe a different mirror or a re-``getattr``-ed HEAD reader than the
    # pre-launch repairs did.
    mirror_path: Path | None
    # Raw ``getattr`` result, not narrowed: call sites that need a real coroutine
    # apply their own ``callable()`` guard, and the ones that accept ``Any`` must
    # keep seeing the same value the inline block handed them.
    rev_parse_head: Any


async def prepare_item_protocol_anchors(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    state: MonitorState | None,
    timeout_preserve_item_id: str | None,
    operation_start_head: str | None,
    evidence_item_path: str | None,
    evidence_item_line: int | None,
    evidence_anchor_head: str | None,
) -> ItemProtocolAnchors:
    """Resolve the item's anchors and run the pre-launch safety repairs.

    Consumes the #932 preserved ``item_start_head`` marker (the caller re-arms it
    when the attempt dies before a verdict), re-maps a stale review path/line
    through the commits between the review anchor and the item start, fills in a
    missing anchor or floor from the live HEAD, then repairs agent runtime
    ownership and the mirror ``hooksPath`` and snapshots the worktree's local git
    configs. Raises ``_MonitorAgentRuntimeOwnershipRepairFailedError`` or the
    hooks-repair failure exactly as the inline block did.
    """
    from awf.runtime.pr_monitor_runner import comment_verdict as _comment_verdict
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _map_review_line_through_commits,
        _map_review_path_through_commits,
        _normalize_evidence_item_path,
    )

    worktree_path = runner._worktrees_root / workspace_id
    item_path = _normalize_evidence_item_path(evidence_item_path or "") or None
    item_line = evidence_item_line
    item_start_head = (operation_start_head or "").strip() or None
    rollback_floor_head = item_start_head
    # #932: a previous attempt for this item timed out and its commits were
    # deliberately kept, so the caller's ``operation_start_head`` is now the
    # *preserved* HEAD. Anchor this attempt at the original item start instead,
    # so the preserved commits stay inside the item's own evidence range and
    # count as its own work under the #925/#928/#931 rules. Consumed on read, and
    # re-armed by the entry point if this attempt dies before a verdict.
    preserved_item_start_head = consume_item_start_head(state, timeout_preserve_item_id)
    if preserved_item_start_head is not None:
        _log.info(
            "monitor.agent_verdict_item_start_head_restored",
            workspace_id=workspace_id,
            item_start_head=preserved_item_start_head,
            attempt_start_head=item_start_head,
        )
        item_start_head = preserved_item_start_head
    anchor_head = (evidence_anchor_head or "").strip() or None
    if (
        item_path is not None
        and anchor_head is not None
        and item_start_head is not None
        and anchor_head.lower() != item_start_head.lower()
    ):
        original_item_path = item_path
        mapped_path = await _map_review_path_through_commits(
            runner,
            worktree_path=worktree_path,
            anchor_head=anchor_head,
            target_head=item_start_head,
            path=item_path,
        )
        if mapped_path is None:
            item_line = -1
        else:
            item_path = mapped_path
        if item_line is not None:
            mapped_line = await _map_review_line_through_commits(
                runner,
                worktree_path=worktree_path,
                anchor_head=anchor_head,
                target_head=item_start_head,
                path=original_item_path,
                line=item_line,
            )
            item_line = -1 if mapped_line is None else mapped_line

    rev_parse_head = getattr(runner, "_rev_parse_head", None)
    if (
        (item_start_head is None or rollback_floor_head is None)
        and worktree_path.exists()
        and callable(rev_parse_head)
    ):
        live_head = await rev_parse_head(worktree_path)
        if item_start_head is None:
            item_start_head = live_head
        if rollback_floor_head is None:
            # A restored anchor never becomes the floor: the live HEAD already
            # includes the preserved commits, so it is the honest floor.
            rollback_floor_head = live_head

    if not await _comment_verdict.repair_agent_runtime_ownership(
        logger=_log,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="monitor_agent_pre_launch",
        event_name=_comment_verdict.MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    ):
        raise _MonitorAgentRuntimeOwnershipRepairFailedError(
            "AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED"
        )

    mirror_path = _comment_verdict.mirror_path_for_worktree(worktree_path)
    if mirror_path is not None:
        await _comment_verdict._repair_mirror_hooks_or_raise(
            workspace_id=workspace_id,
            mirror_path=mirror_path,
            stage="before_comment_agent",
        )

    # Snapshot after hooksPath repair so non-FIXED rollback cannot reintroduce a
    # poisoned executable hook path the pre-launch safety repair just removed
    # (PRRT_kwDOSJAM6s6e0yQN). Off the event loop: nested-.git discovery walks
    # the full worktree under a 100k-entry / 30s budget (PRRT_kwDOSJAM6s6e5nws).
    if worktree_path.exists() and not await asyncio.to_thread(
        _comment_verdict.remember_item_start_local_git_configs,
        worktree_path,
    ):
        # Fingerprint probes fail closed when local config cannot be snapshotted.
        # Do not abort the item here: unit fixtures often use non-contained
        # ``gitdir:`` stubs, and production still refuses config-blind non-FIXED
        # acceptance via ``None`` residue fingerprints (PRRT_kwDOSJAM6s6e0Xdl).
        _log.warning(
            "monitor.agent_verdict_item_start_git_config_snapshot_failed",
            workspace_id=workspace_id,
        )

    return ItemProtocolAnchors(
        worktree_path=worktree_path,
        item_path=item_path,
        item_line=item_line,
        item_start_head=item_start_head,
        rollback_floor_head=rollback_floor_head,
        mirror_path=mirror_path,
        rev_parse_head=rev_parse_head,
    )
