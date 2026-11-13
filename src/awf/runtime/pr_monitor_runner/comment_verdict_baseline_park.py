"""Keep a comment repair when its commit sink hits a baseline outage (#1221).

A transient ``ProtectedScopeBaselineUnavailableError`` from the commit sink parks
the workspace ``monitoring_pr -> blocked`` with the worktree and unpushed repair
kept, so a later ``guide`` re-runs protected-scope validation and pushes it. The
verdict protocol's rollback-before-propagate would discard exactly that repair,
leaving the park to record the rolled-back HEAD (PRRT_kwDOSJAM6s63N-C1). Like the
CI repair's handler, this keeps the item's commits and edits and persists the
stranded-edit marker and evidence ahead of the park, so the resume commits
exactly the edits the sink left behind.

The preservation is claimed before its first await and finishes under a shield,
so a worker cancellation landing mid-way neither rewinds the repair through the
caller's cancellation rollback nor leaves a marker that describes a rolled-back
worktree (PRRT_kwDOSJAM6s63O77x).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from awf.common.logging import get_logger
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

_log = get_logger(__name__)


async def keep_repair_for_baseline_park(
    runner: PullRequestMonitorRunner,
    *,
    exc: BaseException,
    workspace_id: str,
    worktree_path: Path,
    state: MonitorState | None,
    rev_parse_head: Callable[..., Awaitable[str | None]] | None,
    preservation_claimed: list[bool],
) -> bool:
    """Return True when the repair is kept for the park instead of rolled back.

    Only the transient subclass parks; any other ``ProtectedScopeDiffError`` still
    fails closed through the caller's rollback. A hosted agent publishes before
    AWF gates its commit, so its rollback still rewinds that ungated push. An
    unreadable HEAD is recorded as empty, as the CI repair's park records it.

    ``preservation_claimed`` is appended to before the first await, so the
    caller's cancellation branch reads it as "do not rewind". A cancellation then
    waits for the marker, evidence, and state write to finish before it is handed
    on; a failure of those steps, which the cancellation displaces, is logged.
    """
    if (
        not isinstance(exc, ProtectedScopeBaselineUnavailableError)
        or state is None
        or getattr(runner._deps.adapter, "is_hosted", False)
    ):
        return False
    preservation_claimed.append(True)
    preserve_task = asyncio.ensure_future(
        _preserve_repair_for_park(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            state=state,
            rev_parse_head=rev_parse_head,
        )
    )
    cancelled: asyncio.CancelledError | None = None
    while not preserve_task.done():
        try:
            await asyncio.shield(preserve_task)
        except asyncio.CancelledError as cancel_exc:
            cancelled = cancel_exc
        except Exception:  # noqa: S110 - the steps' own failure, read off the task below
            pass
    if cancelled is None:
        preserve_task.result()
        return True
    dropped_exc = None if preserve_task.cancelled() else preserve_task.exception()
    if dropped_exc is not None:
        _log.warning(
            "monitor.agent_verdict_cancelled_baseline_park_preserve_failed",
            workspace_id=workspace_id,
            exc_type=type(dropped_exc).__name__,
        )
    raise cancelled


async def _preserve_repair_for_park(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
    state: MonitorState,
    rev_parse_head: Callable[..., Awaitable[str | None]] | None,
) -> None:
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
