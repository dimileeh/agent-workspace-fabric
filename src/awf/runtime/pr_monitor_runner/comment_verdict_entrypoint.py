"""Entry points that run one review item through the verdict protocol.

Kept separate so ``comment_verdict`` stays under the first-party line budget;
both functions are re-exported (``X as X``) from ``comment_verdict``, which is
still the module ``comments`` and the tests resolve them through.

``_run_item_verdict_protocol`` is resolved through ``comment_verdict`` at call
time — both to avoid an import cycle and so a monkeypatch on that module (or on
the ``comments`` forwarding shim) still reaches the protocol body.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, cast

from awf.common.logging import get_logger
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    AgentVerdict,
    VerdictResult,
)
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
    consume_item_start_head,
    item_start_body_hash_changed,
    peek_item_start_body_hash,
    peek_item_start_head,
    preserved_anchor_is_reachable,
    remember_item_start_head_durably,
    restore_item_start_head,
)
from awf.runtime.pr_monitor_runner.constants import (
    _TASK_TAG_UNSET,
    _TaskTagUnset,
)

if TYPE_CHECKING:
    from awf.runtime.pr_monitor import MonitorState
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner

_log = get_logger(__name__)


async def _invoke_cli_for_verdict(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    prompt: str,
    commit_message: str,
    compose_project: str,
    compose_file: Path,
    state: MonitorState | None = None,
    task_tag: str | None | _TaskTagUnset = _TASK_TAG_UNSET,
    operation_start_head: str | None = None,
) -> AgentVerdict:
    return cast(
        AgentVerdict,
        (
            await runner._invoke_cli_for_verdict_result(
                workspace_id=workspace_id,
                prompt=prompt,
                commit_message=commit_message,
                compose_project=compose_project,
                compose_file=compose_file,
                state=state,
                task_tag=task_tag,
                operation_start_head=operation_start_head,
            )
        ).verdict,
    )


async def _invoke_cli_for_verdict_result(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    prompt: str,
    commit_message: str,
    compose_project: str,
    compose_file: Path,
    state: MonitorState | None = None,
    task_tag: str | None | _TaskTagUnset = _TASK_TAG_UNSET,
    operation_start_head: str | None = None,
    commit_dirty_changes: bool = True,
    require_fix_evidence: bool = True,
    evidence_item_id: str | None = None,
    evidence_body_hash: str | None = None,
    evidence_item_path: str | None = None,
    evidence_item_line: int | None = None,
    evidence_anchor_head: str | None = None,
) -> VerdictResult:
    """Run one logical item, re-arming its #932 anchor if no verdict is produced.

    The protocol below consumes the preserved-timeout ``item_start_head`` marker
    on entry, before any fallible pre-launch, provider-recovery or agent work. An
    attempt that dies there leaves the item unaddressed and therefore eligible for
    another attempt, so the anchor is put back: otherwise the next attempt would
    anchor at the preserved HEAD and drop the timed-out attempt's commits from its
    own ``FIXED`` evidence range (#934 audit). A returned verdict still consumes it.
    An item can also *earn* an anchor mid-run without ever having had one: when the
    service-recovery loop preserves a watchdog timeout and reruns the agent inside
    the agent run, the #932 preserve handler never sees that timeout and never
    writes the marker, so the protocol publishes the owed anchor here and the same
    verdict-less exits re-arm it (PRRT_kwDOSJAM6s6fwTyP).

    Because it survives every failed attempt, the anchor is also checked here for
    staleness, in two ways. A ``SyncBase`` rebase between passes rewrites the branch
    and strands it on a dropped SHA, and anchoring there gives an evidence range git
    cannot resolve; an anchor that is no longer an ancestor of this attempt's start
    HEAD is dropped — and not re-armed — so the attempt falls back to its own start.
    And the feedback itself can change under a stable item id: a reviewer who edits
    the comment or replies to the thread after the timeout but before the retry poses
    *different* feedback, which the preserved work never answered, so an anchor whose
    recorded body hash no longer matches is dropped the same way and the preserved
    commits cannot be spent as this feedback's ``FIXED`` evidence (#934 audit).
    """
    from awf.runtime.pr_monitor_runner import comment_verdict as _comment_verdict

    item_id = (evidence_item_id or "").strip() or None
    item_body_hash = (evidence_body_hash or "").strip() or None
    preserved_item_start_head = peek_item_start_head(state, item_id)
    preserved_item_body_hash = peek_item_start_body_hash(state, item_id)
    if preserved_item_start_head is not None and item_start_body_hash_changed(
        preserved_item_body_hash, item_body_hash
    ):
        _log.warning(
            "monitor.agent_verdict_item_start_head_body_changed",
            workspace_id=workspace_id,
            item_start_head=preserved_item_start_head,
            attempt_start_head=operation_start_head,
        )
        consume_item_start_head(state, item_id)
        preserved_item_start_head = None
    if preserved_item_start_head is not None and not await preserved_anchor_is_reachable(
        runner,
        worktree_path=runner._worktrees_root / workspace_id,
        anchor_head=preserved_item_start_head,
        attempt_start_head=(operation_start_head or "").strip() or None,
    ):
        _log.warning(
            "monitor.agent_verdict_item_start_head_unreachable",
            workspace_id=workspace_id,
            item_start_head=preserved_item_start_head,
            attempt_start_head=operation_start_head,
        )
        consume_item_start_head(state, item_id)
        preserved_item_start_head = None
    # An anchor this item only starts owing mid-run: the service-recovery loop
    # intercepts a watchdog timeout, preserves its work and reruns the agent, so
    # the #932 preserve handler that writes the marker never sees that timeout
    # (PRRT_kwDOSJAM6s6fwTyP).
    timeout_rerun_anchor_sink: list[str] = []
    try:
        return await _comment_verdict._run_item_verdict_protocol(
            runner,
            workspace_id=workspace_id,
            prompt=prompt,
            commit_message=commit_message,
            compose_project=compose_project,
            compose_file=compose_file,
            state=state,
            task_tag=task_tag,
            operation_start_head=operation_start_head,
            commit_dirty_changes=commit_dirty_changes,
            require_fix_evidence=require_fix_evidence,
            evidence_item_id=evidence_item_id,
            evidence_body_hash=evidence_body_hash,
            evidence_item_path=evidence_item_path,
            evidence_item_line=evidence_item_line,
            evidence_anchor_head=evidence_anchor_head,
            timeout_rerun_anchor_sink=timeout_rerun_anchor_sink,
        )
    except BaseException:
        # Every exit from here — infrastructure repair failure, provider failure,
        # protocol violation, worker cancellation — fails the fix cycle without
        # recording a verdict for the item, so the item is re-addressed later.
        anchor_head = preserved_item_start_head
        anchor_body_hash = preserved_item_body_hash
        anchor_earned_mid_run = False
        if anchor_head is None and timeout_rerun_anchor_sink:
            # This attempt had no anchor on entry but earned one: a timeout the
            # recovery loop reran over left commits behind, and only the item's
            # own start covers them. Bind it to the feedback this attempt ran on,
            # exactly as the preserve handler would have.
            anchor_head = timeout_rerun_anchor_sink[-1]
            anchor_body_hash = item_body_hash
            anchor_earned_mid_run = True
        restore_item_start_head(state, item_id, anchor_head, anchor_body_hash)
        if anchor_earned_mid_run:
            # An anchor restored from entry is already on the workspace row —
            # consuming it only cleared the in-memory copy. A newly earned one
            # has never been written there, and the exits that lose it are real:
            # ``run()``'s service-recovery-failed and superseded arms return
            # without ``_persist_state``. The timed-out commit stays on disk, so
            # the next invocation would anchor the item at that preserved HEAD
            # and reject an honest no-change ``FIXED`` as
            # ``AGENT_FIXED_WITHOUT_EVIDENCE`` — the same wedge the preserve
            # handler's durable write exists to prevent (PRRT_kwDOSJAM6s6f0ft2).
            # Whatever the restore left in state is what gets persisted: a fresh
            # timeout on this very attempt is newer, wins the re-arm, and has
            # already written itself durably.
            await _write_mid_run_anchor_durably(
                runner,
                workspace_id=workspace_id,
                state=state,
                item_id=item_id,
                anchor_head=anchor_head,
            )
        raise


async def _write_mid_run_anchor_durably(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    state: MonitorState | None,
    item_id: str | None,
    anchor_head: str | None,
) -> None:
    """Put a newly earned anchor on the workspace row, cancelled or not.

    Shielded and re-awaited, like every other durable anchor write on this path
    (``_finish_timeout_preservation``, ``preserve_cancelled_timeout_work``): the
    caller is an exception handler that runs *for* worker cancellation among other
    exits, so an ordinary await here is cut short by the shutdown that is already
    propagating — and ``run()`` does not persist the state on cancellation either.
    The salvaged commit stays on disk while its only anchor dies with the process,
    and the next worker rejects an honest no-change ``FIXED`` as
    ``AGENT_FIXED_WITHOUT_EVIDENCE`` — exactly the wedge this write exists to
    prevent (PRRT_kwDOSJAM6s6f8cgY).

    Nothing raised in here reaches the caller, cancellation included. This is the
    one durable write that runs inside an exception handler, so anything escaping
    it replaces the failure on its way out — the recovery-failed or protocol reason
    code that ``run()`` classifies the pass by would be swallowed by a storage
    error, or by a second shutdown cancellation. The helper already degrades on
    ``SQLAlchemyError``/``OSError``; a session factory can still fail outside that
    set (a closed loop, a repository error), and losing the anchor is strictly the
    smaller harm: the in-memory marker written before the first await still carries
    it across a clean exit. A cancellation the caller was raised with propagates
    from the ``raise`` that follows this call regardless.
    """
    write_task = asyncio.ensure_future(
        remember_item_start_head_durably(
            runner,
            workspace_id=workspace_id,
            state=state,
            item_id=item_id,
            head=peek_item_start_head(state, item_id),
            body_hash=peek_item_start_body_hash(state, item_id),
        )
    )
    while not write_task.done():
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.shield(write_task)
    # Read off the task rather than off the await: a cancellation delivered in the
    # same loop step the write failed in makes ``shield`` retrieve the exception
    # itself and hand the awaiter a ``CancelledError``, so the failure would
    # otherwise vanish (the ``PRRT_kwDOSJAM6s6f2ckS`` shape).
    write_exc = None if write_task.cancelled() else write_task.exception()
    if write_exc is not None:
        _log.warning(
            "monitor.agent_verdict_mid_run_anchor_durable_write_failed",
            workspace_id=workspace_id,
            item_id=item_id,
            item_start_head=anchor_head,
            exc_type=type(write_exc).__name__,
        )
