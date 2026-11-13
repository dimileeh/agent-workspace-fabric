"""Keep a comment repair when its commit sink hits a baseline outage (#1221).

A transient ``ProtectedScopeBaselineUnavailableError`` from the commit sink parks
the workspace ``monitoring_pr -> blocked`` with the worktree and unpushed repair
kept, so a later ``guide`` re-runs protected-scope validation and pushes it. The
verdict protocol's rollback-before-propagate would discard exactly that repair,
leaving the park to record the rolled-back HEAD (PRRT_kwDOSJAM6s63N-C1). Like the
CI repair's handler, this keeps the item's commits and edits and persists the
stranded-edit marker and evidence ahead of the park, so the resume commits
exactly the edits the sink left behind.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from awf.runtime.pr_monitor_runner.comment_verdict_residue_fingerprint import (
    read_protocol_attempt_start_head,
)
from awf.runtime.pr_monitor_runner.provider_park_stranded_edits import (
    mark_provider_park_stranded_edits,
    record_provider_park_stranded_edits_evidence,
)
from awf.runtime.pr_monitor_runner.types import ProtectedScopeBaselineUnavailableError

if TYPE_CHECKING:
    from awf.runtime.pr_monitor import MonitorState
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner


async def keep_repair_for_baseline_park(
    runner: PullRequestMonitorRunner,
    *,
    exc: BaseException,
    workspace_id: str,
    worktree_path: Path,
    state: MonitorState | None,
    rev_parse_head: Callable[..., Awaitable[str | None]] | None,
) -> bool:
    """Return True when the repair is kept for the park instead of rolled back.

    Only the transient subclass parks; any other ``ProtectedScopeDiffError`` still
    fails closed through the caller's rollback. A hosted agent publishes before
    AWF gates its commit, so its rollback still rewinds that ungated push. An
    unreadable HEAD is recorded as empty, as the CI repair's park records it.
    """
    if (
        not isinstance(exc, ProtectedScopeBaselineUnavailableError)
        or state is None
        or getattr(runner._deps.adapter, "is_hosted", False)
    ):
        return False
    try:
        preserved_head = await read_protocol_attempt_start_head(
            runner, worktree_path=worktree_path, rev_parse_head=rev_parse_head
        )
    except (TimeoutError, OSError, RuntimeError):
        preserved_head = None
    mark_provider_park_stranded_edits(state, preserved_head)
    await record_provider_park_stranded_edits_evidence(
        runner, workspace_id=workspace_id, worktree_path=worktree_path, state=state
    )
    await runner._persist_state(workspace_id, state)
    return True
