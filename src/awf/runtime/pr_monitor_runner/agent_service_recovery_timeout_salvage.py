"""Timeout-salvage bookkeeping for the monitor's agent service recovery loop.

Split out of ``agent_service_recovery.py`` to keep that module under the
first-party 1500-line maintainability guardrail. The recovery loop reruns
watchdog timeouts, and #932 forbids deleting what the timed-out run left
behind — these helpers salvage its dirty edits and publish the HEAD floor that
covers them before the rerun starts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from awf.adapters.provider_failures import AGENT_TIMEOUT
from awf.common.compose_exec import ComposeExecCleanupError
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.worktree_writer_lock import worktree_writer_locks_borrowed_from


def _masked_agent_timeout_reason_code(exc: ComposeExecCleanupError) -> str | None:
    """The watchdog reason code a recovered cleanup failure is masking, if any.

    Deferred import: the verdict-protocol module that owns this classification
    imports the monitor runner, so binding it at module import time would close a
    cycle. Reusing it keeps one definition of "this cleanup failure replaced a
    timeout" across the preserve handler and this recovery loop.
    """
    from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
        cleanup_error_agent_timeout_reason_code,
    )

    return cleanup_error_agent_timeout_reason_code(exc)


async def _record_timeout_rerun_floor(
    self: Any,
    *,
    workspace_id: str,
    sink: list[str] | None,
    dirty_sink: Callable[[str], Awaitable[bool]] | None = None,
    timeout_reason_code: str = AGENT_TIMEOUT,
    preservation_sink: list[str] | None = None,
) -> bool:
    """Publish the HEAD a timed-out run is leaving behind before it is rerun.

    ``_recover_monitor_agent_service_after_error`` recovers watchdog timeouts and
    nothing else, so every rerun this loop performs replaces a run whose commits
    #932 forbids deleting — and it happens *inside* this helper, where the
    caller's #932 preserve handler never sees the timeout. The caller's rollback
    floor still points at the attempt start, so a provider failure on the rerun
    would rewind straight past those commits (PRRT_kwDOSJAM6s6fvdil). Appending
    the pre-rerun HEAD lets the caller raise that floor to it.

    A HEAD that cannot be published gives the rerun up. The caller raises its
    rollback floor only when this sink is non-empty, so rerunning with nothing
    published leaves that floor at the attempt start: a provider failure or a
    non-FIXED verdict on the rerun then resets straight through the commits the
    timed-out run — or the dirty salvage below — left behind, which is exactly
    the deletion #932 forbids (PRRT_kwDOSJAM6s6fxp80). Raising the timeout
    instead hands it to the caller's preserve handler, which keeps that work and
    re-queues the item; the cost is one rerun, the same trade the unconfirmed
    salvage below already makes.

    The run this probe follows always timed out, so the live Git configuration it
    left behind can be poisoned (``include.path`` → FIFO). Read HEAD through
    ``read_protocol_attempt_start_head``, which prefers the remembered item-start
    configs and otherwise bounds live ``_rev_parse_head`` with a timeout: an
    unbounded probe would hang the recovery loop, so neither the rerun nor the
    timeout would ever reach the #932 preserve handler (PRRT_kwDOSJAM6s6fvv27).
    A probe that raises is still swallowed rather than propagated — replacing the
    watchdog reason code with an unrelated exception would lose the
    classification the preserve handler keys on — but it publishes nothing, so it
    gives the rerun up like any other unpublishable floor.

    A SHA floor alone cannot hold the timed-out run's *uncommitted* edits, which
    #932 protects just as much as its commits: a run that committed nothing
    publishes a HEAD equal to the attempt floor, so a provider or protocol
    failure on the rerun resets straight through those edits and deletes them.
    ``dirty_sink`` therefore commits them first — the same dirty-worktree sink
    the #932 preserve handler runs — and the HEAD published afterwards covers
    the resulting commit (PRRT_kwDOSJAM6s6fvw8r). Like the probe it never raises
    into the recovery loop, and it runs before the probe's own capability gate so
    the edits are salvaged even when no HEAD can be published for them.

    Returns whether the rerun may proceed. ``dirty_sink`` answers that question
    itself — True once the edits are committed or once it has confirmed there is
    nothing PR-worthy left uncommitted, False when they are still stranded — and
    a False answer is *not* discardable bookkeeping: the published floor is only
    a SHA, so rerunning past stranded edits lets the rollback a provider failure
    or non-FIXED verdict on the rerun performs delete them
    (PRRT_kwDOSJAM6s6fwTyO). A sink that *raises* still never costs the rerun:
    the production sink reports its own outcome and swallows its failures, so an
    escaping exception is a broken bookkeeping seam rather than evidence about
    the worktree, and losing the rerun to it would be worse than the rollback —
    though the floor probe still has its own say afterwards.

    ``preservation_sink`` guards the window in between. Neither protection
    channel is populated while this bookkeeping runs — the floor sink by
    definition, the caller's ``timeout_preservation_sink`` because the recovery
    loop intercepted the timeout before its preserve handler ever saw it — and
    both awaits below are cancellable. ``CancelledError`` is a ``BaseException``,
    so escaping either one bypasses this function's own handlers and lands on the
    caller's cancellation branch, which resets to the unraised floor and deletes
    the timed-out run's commits along with the salvage commit the dirty sink may
    just have made (PRRT_kwDOSJAM6s6fy7ju). Marking the work protected before the
    first await keeps that branch from rewinding; publishing the floor below
    hands the same protection back to the caller's floor raise, so the mark is
    released there and the rerun's own residue keeps rolling back to that floor.

    Marking is only the *first* thing that claim owes, though, and the salvage
    below awaits. A cancellation landing in the sink used to escape with the mark
    already published: the caller skipped the rollback, right, but its nested
    guard also skipped ``preserve_cancelled_timeout_work``, which only runs for
    timeouts no handler ever saw — so the timed-out run's edits stayed dirty and
    the next pass rejected them as ``PRE_EXISTING_DIRTY_WORKTREE``. The sequence
    therefore finishes under the preserve path's own shield before the
    cancellation is handed onward (PRRT_kwDOSJAM6s6f3oxD).
    """
    if sink is None:
        return True
    worktree_path = self._worktrees_root / workspace_id
    if not worktree_path.exists():
        return True

    if preservation_sink is not None:
        preservation_sink.append(timeout_reason_code)

    # Deferred like ``_masked_agent_timeout_reason_code`` above: the preserve
    # module imports the monitor runner, so binding it at import time would close
    # a cycle. Reusing its shield keeps one definition of "an already-claimed
    # preservation runs to completion, cancel or not".
    from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
        _finish_timeout_preservation,
    )

    return await _finish_timeout_preservation(
        _secure_timeout_rerun_preservation(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            sink=sink,
            dirty_sink=dirty_sink,
            timeout_reason_code=timeout_reason_code,
            preservation_sink=preservation_sink,
            lock_owner=asyncio.current_task(),
        ),
        workspace_id=workspace_id,
        reason_code=timeout_reason_code,
        item_start_head=None,
    )


async def _secure_timeout_rerun_preservation(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    sink: list[str],
    dirty_sink: Callable[[str], Awaitable[bool]] | None,
    timeout_reason_code: str,
    preservation_sink: list[str] | None,
    lock_owner: asyncio.Task[Any] | None,
) -> bool:
    """Salvage the timed-out run's edits under the recovery loop's writer lock.

    The shield runs these steps in a task of its own, and the whole recovery loop
    already holds the worktree writer lock, whose reentrancy is keyed on the
    holding task — so the dirty sink, which stages and commits under that same
    lock, would block against its own holder. Borrow the loop's ownership for the
    sequence: the shield awaits it to completion inside that holding frame.
    """
    with worktree_writer_locks_borrowed_from(lock_owner):
        return await _timeout_rerun_salvage_steps(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            sink=sink,
            dirty_sink=dirty_sink,
            timeout_reason_code=timeout_reason_code,
            preservation_sink=preservation_sink,
        )


async def _timeout_rerun_salvage_steps(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    sink: list[str],
    dirty_sink: Callable[[str], Awaitable[bool]] | None,
    timeout_reason_code: str,
    preservation_sink: list[str] | None,
) -> bool:
    """Sink the timed-out run's edits, then publish the floor that covers them.

    The two steps ``_record_timeout_rerun_floor``'s published claim owes, kept in
    one coroutine so its shield cannot be interrupted *between* them either. See
    that function's docstring for why each step behaves the way it does.
    """
    rerun_allowed = True
    if dirty_sink is not None:
        try:
            rerun_allowed = await dirty_sink(timeout_reason_code)
        except Exception as sink_exc:
            # Broad on purpose, exactly like the HEAD probe below: the sink
            # spawns Git and touches repository/session state, and losing the
            # rerun to a failed salvage would be worse than the rollback this
            # bookkeeping guards against. ``asyncio.CancelledError`` is a
            # ``BaseException`` and still propagates.
            _log.warning(
                "monitor.agent_service_recovery_rerun_dirty_sink_failed",
                workspace_id=workspace_id,
                reason_code=timeout_reason_code,
                exc_type=type(sink_exc).__name__,
            )
    if not rerun_allowed:
        _log.warning(
            "monitor.agent_service_recovery_rerun_salvage_unconfirmed",
            workspace_id=workspace_id,
            reason_code=timeout_reason_code,
        )

    from awf.runtime.pr_monitor_runner.comment_verdict_residue_fingerprint import (
        item_start_snapshot_covers_outer_git_dir,
        read_protocol_attempt_start_head,
    )

    rev_parse_head = getattr(self, "_rev_parse_head", None)
    head: str | None = None
    if item_start_snapshot_covers_outer_git_dir(worktree_path) or callable(rev_parse_head):
        try:
            head = await read_protocol_attempt_start_head(
                self,
                worktree_path=worktree_path,
                rev_parse_head=rev_parse_head if callable(rev_parse_head) else None,
            )
        except Exception as probe_exc:
            # Broad on purpose: the trusted probe stages a private git-dir and
            # spawns Git, so it can raise outside the git-spawn error set. Letting
            # one escape would replace the watchdog reason code with an unrelated
            # exception, and the caller's preserve handler keys on that code.
            # ``asyncio.CancelledError`` is a ``BaseException`` and still
            # propagates.
            _log.warning(
                "monitor.agent_service_recovery_rerun_floor_probe_failed",
                workspace_id=workspace_id,
                exc_type=type(probe_exc).__name__,
            )
    if not head:
        # No floor to publish, so the caller's rollback floor stays at the attempt
        # start and the rerun would expose the timed-out run's commits — including
        # any the dirty sink above just made — to it. Give the rerun up instead
        # (PRRT_kwDOSJAM6s6fxp80).
        _log.warning(
            "monitor.agent_service_recovery_rerun_floor_unpublished",
            workspace_id=workspace_id,
            reason_code=timeout_reason_code,
        )
        return False
    sink.append(head)
    if preservation_sink is not None and preservation_sink[-1:] == [timeout_reason_code]:
        # The published floor covers this work from here on, and the caller raises
        # its rollback floor to it on every exit from the run — so ordinary
        # cancellation semantics may resume: a rewind now stops at the timed-out
        # run's HEAD and discards only the rerun's own unaccepted residue, which
        # a still-protected cancellation would strand in the worktree instead.
        preservation_sink.pop()
    return rerun_allowed
