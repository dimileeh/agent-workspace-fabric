"""Timeout-preserving ``AgentRunError`` handling for the verdict protocol (#932).

Every ``AgentRunError`` used to roll the item back to ``item_start_head`` before
raising ``AgentVerdictExecutionError``. For a *provider* failure that is right:
whatever the agent left behind is unaccepted residue. For a **timeout** it is
destructive — the watchdog can fire on a healthy run that already committed a
real fix, and ws_84fddb4a98c94f7b8d6aa0d3 (PR #922) lost an hour of work exactly
that way.

``AGENT_IDLE_TIMEOUT`` / ``AGENT_TIMEOUT`` therefore take a preserve path:

1. Sink uncommitted item-scoped edits through the existing dirty-worktree sink.
2. Keep the item's commits — no rollback, ever.
3. Remember the *original* ``item_start_head`` for the item — in memory and
   durably on the workspace row, because the salvaged commits outlive a worker
   crash and the anchor must too — so the re-attempt's FIXED evidence range still
   starts where the item started, and the preserved commits count as this item's
   own work under the #925/#928/#931 rules. That
   restored anchor is for evidence only: the re-attempt's rollback floor stays at
   the preserved HEAD, so a later bad verdict cannot undo the preservation (#934).
   The marker also carries the hash of the feedback body it was written for: a
   reviewer who edits the comment or replies to the thread between the timeout and
   the retry keeps the same item id but poses different feedback, and the preserved
   work answers the *old* body, so it must not be handed to the new one as FIXED
   evidence (#934 audit).
4. Raise ``AgentVerdictExecutionError`` carrying the preserved HEAD, which the
   callers record as ``agent_failed`` — already a re-queueing outcome. That HEAD
   is only reported when work actually survived: a timeout whose sink committed
   nothing and whose HEAD was *read* and had not moved past this attempt's start
   reports ``None``, so gates that read it as "work survived" (the operator-hint
   timeout retry) are not fooled by an unchanged HEAD (#934 audit). A HEAD the
   probe could not read is unknown rather than unchanged, and fails open
   (PRRT_kwDOSJAM6s6f0zfW).

One timeout does *not* re-queue: a sink that ran and left the timed-out edits
dirty — whether it committed nothing or raised — has FAILED, not found the
worktree empty, and re-queueing it hands the next comment-repair pass a dirty
worktree its pre-existing-dirty guard rejects as ``PRE_EXISTING_DIRTY_WORKTREE``
— the sink failure masked and the preserved work stranded. That case escalates as
``REPAIR_DIRTY_COMMIT_FAILED`` instead, exactly as the CI-repair commit sink
already does, and still without a rollback (PRRT_kwDOSJAM6s6fwr71,
PRRT_kwDOSJAM6s6fxp82).

Kept in a sibling module so ``comment_verdict`` stays under the line budget;
re-exported from there (``X as X``) so monkeypatch seams keep working. The
item-start marker helpers and the anchor-reachability probes are split one level
further out for the same reason — ``comment_verdict_timeout_preserve_anchor`` and
``comment_verdict_timeout_preserve_reachability`` — and re-exported here the same
way, so this module stays the single import surface for the whole preserve path.
"""

from __future__ import annotations

import asyncio
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, NoReturn

from awf.adapters.base import AgentRunError
from awf.adapters.provider_failures import AGENT_IDLE_TIMEOUT, AGENT_TIMEOUT
from awf.common.compose_exec import ComposeExecCleanupError
from awf.common.logging import get_logger
from awf.runtime.pr_monitor_runner.comment_verdict_residue_fingerprint import (
    _fingerprint_has_pr_worthy_path_residue,
    read_protocol_attempt_start_head,
)
from awf.runtime.pr_monitor_runner.comment_verdict_rollback import (
    _rollback_or_classify_failure,
)
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve_anchor import (
    _decode_item_start_marker as _decode_item_start_marker,
)
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve_anchor import (
    _encode_item_start_marker as _encode_item_start_marker,
)
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve_anchor import (
    consume_item_start_head as consume_item_start_head,
)
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve_anchor import (
    item_start_body_hash_changed as item_start_body_hash_changed,
)
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve_anchor import (
    item_start_head_state_key as item_start_head_state_key,
)
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve_anchor import (
    peek_item_start_body_hash as peek_item_start_body_hash,
)
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve_anchor import (
    peek_item_start_head as peek_item_start_head,
)
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve_anchor import (
    remember_item_start_head as remember_item_start_head,
)
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve_anchor import (
    remember_item_start_head_durably as remember_item_start_head_durably,
)
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve_anchor import (
    restore_item_start_head as restore_item_start_head,
)
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve_reachability import (
    preserved_anchor_is_reachable as preserved_anchor_is_reachable,
)
from awf.runtime.pr_monitor_runner.constants import (
    _REPAIR_DIRTY_COMMIT_FAILED_REASON,
    _TASK_TAG_UNSET,
    _TaskTagUnset,
)
from awf.runtime.pr_monitor_runner.types import SINK_INFRASTRUCTURE_ERRORS

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from awf.runtime.pr_monitor import MonitorState
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner

_log = get_logger(__name__)


class _TimeoutBaselineUnset:
    """Sentinel type meaning "no timeout-work baseline was threaded by the caller".

    The baseline is legitimately ``None`` — the item's floor before the first
    service-recovery rerun is unknown whenever the item-start HEAD read failed —
    and ``None`` there means "cannot show HEAD standing still", which
    ``_work_survived_timeout`` fails open on. So ``None`` cannot double as "not
    provided": collapsing the two would hand the handler the *raised* floor and
    report the preserved commits as nothing at all.
    """

    __slots__ = ()


_TIMEOUT_BASELINE_UNSET = _TimeoutBaselineUnset()

AGENT_TIMEOUT_REASON_CODES = frozenset({AGENT_TIMEOUT, AGENT_IDLE_TIMEOUT})
"""Reason codes that mean "the watchdog fired", not "the agent's work is junk"."""


class TimeoutSinkOutcome(Enum):
    """What the timeout dirty sink did — not merely "did it commit?".

    ``_commit_dirty_worktree`` answers ``False`` for two very different things:
    there was nothing PR-worthy to commit (the ordinary case), or its own
    ``git status`` / ``git add`` / ``git commit`` failed after the timed-out
    agent left repair output dirty. Collapsing them into one bool hands the
    second case to the caller as an ordinary preserved timeout, which records
    ``agent_failed`` and re-queues the item — but the edits are still dirty, so
    the next comment-repair pass is rejected by the pre-existing-dirty guard as
    ``PRE_EXISTING_DIRTY_WORKTREE`` before the agent can resume, masking the sink
    failure and stranding the preserved work (PRRT_kwDOSJAM6s6fwr71).

    The sink itself cannot tell the two apart, so ``NO_COMMIT`` is disambiguated
    by a residue probe at the one call site that must escalate. ``RAISED`` is a
    third answer only because the exception is swallowed to keep the timeout's
    reason code; the worktree it leaves behind is indistinguishable from a failed
    ``NO_COMMIT``, its own reason code is logged but never the persisted outcome,
    and re-queueing it strands the edits the same way — so it takes the same
    probe (PRRT_kwDOSJAM6s6fxp82).
    """

    COMMITTED = "committed"
    NO_COMMIT = "no_commit"
    RAISED = "raised"
    DISABLED = "disabled"


_SINK_OUTCOMES_THAT_MAY_STRAND = frozenset(
    {TimeoutSinkOutcome.NO_COMMIT, TimeoutSinkOutcome.RAISED}
)
"""Outcomes where the sink ran and may have left the timed-out edits dirty."""


class TimeoutPreserveOutcome(NamedTuple):
    """What a shielded preserve sequence actually managed to do.

    The sink outcome alone reads as an unqualified success once an anchor failure
    stops aborting the sequence: the durable marker can be gone while the edits
    were committed fine. The preserved record has to carry both, the same way the
    cancelled sibling reports ``item_start_head_persisted`` — only the in-memory
    marker is left behind, and the worker these paths run under may never persist
    it (PRRT_kwDOSJAM6s6f2ckK, PRRT_kwDOSJAM6s6f2_KT).
    """

    sink: TimeoutSinkOutcome
    anchor_persisted: bool


# Infrastructure exits the dirty-worktree sink already declares. They are logged
# and swallowed here: the preserved commits must survive a sink failure, and the
# timeout reason code must still reach the caller.
_SINK_INFRASTRUCTURE_ERRORS = SINK_INFRASTRUCTURE_ERRORS


async def handle_agent_run_error(
    runner: PullRequestMonitorRunner,
    *,
    exc: AgentRunError,
    workspace_id: str,
    worktree_path: Path,
    item_start_head: str | None,
    rollback_floor_head: str | None,
    timeout_work_baseline_head: str | None | _TimeoutBaselineUnset = _TIMEOUT_BASELINE_UNSET,
    item_start_last_push_sha: str | None,
    state: MonitorState | None,
    item_id: str | None,
    item_body_hash: str | None = None,
    commit_message: str,
    compose_project: str,
    compose_file: Path,
    task_tag: str | None | _TaskTagUnset = _TASK_TAG_UNSET,
    command_evidence: list[str],
    commit_dirty_changes: bool,
    rev_parse_head: Any,
    timeout_preservation_sink: list[str],
) -> NoReturn:
    """Classify an ``AgentRunError``: roll back a provider failure, preserve a timeout.

    ``item_start_head`` is the item's evidence anchor (restored to the original
    start on a re-attempt after a preserved timeout) and stays the sink anchor and
    the value remembered for the next attempt, bound to ``item_body_hash`` so an
    edited comment or a new thread reply cannot inherit it. ``rollback_floor_head`` is the
    commit a rollback may rewind to — this attempt's own start — so a provider
    failure on that re-attempt cannot delete the preserved commits (#934). It is
    also the baseline for "did HEAD move?", which decides whether the raised
    error reports a preserved HEAD at all.

    ``timeout_work_baseline_head`` overrides that second use. When the service-
    recovery loop reran the agent after a watchdog timeout inside the agent run,
    the caller raises ``rollback_floor_head`` to the HEAD it reran over
    (PRRT_kwDOSJAM6s6fvdil) — commits this attempt itself made. Measuring "did
    this attempt leave work behind" against that raised floor would under-report
    exactly the work the raise protects, so the caller also passes the floor as
    it stood *before the first such rerun in this item* — a raise from an earlier
    protocol attempt hides that attempt's kept commits just as effectively. That
    floor is ``None`` when the item never read a HEAD to start from, which is a
    threaded value meaning "fail open", not an absent one: only the
    ``_TIMEOUT_BASELINE_UNSET`` sentinel default falls back to
    ``rollback_floor_head``.

    ``timeout_preservation_sink`` is how the preserve path tells its caller that
    no rollback floor applies from here on. Every step below awaits, and worker
    cancellation bypasses their handlers — ``CancelledError`` is a
    ``BaseException`` — landing on the caller's cancellation branch, which would
    rewind to ``rollback_floor_head`` and delete the very commits this path
    exists to keep (PRRT_kwDOSJAM6s6fylWD). The preservation the claim promises
    is finished under a shield first, so the branch never sees it half-done
    (PRRT_kwDOSJAM6s6f2gbw).
    """
    from awf.runtime.pr_monitor_runner.comment_verdict import (
        AGENT_VERDICT_PROTOCOL_VIOLATION,
        AgentVerdictExecutionError,
        AgentVerdictProtocolError,
    )

    if exc.reason_code not in AGENT_TIMEOUT_REASON_CODES:
        rollback_ok = await _rollback_or_classify_failure(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            item_start_head=rollback_floor_head,
            item_start_last_push_sha=item_start_last_push_sha,
            state=state,
        )
        if not rollback_ok:
            _log.warning(
                "monitor.agent_verdict_provider_failure_rollback_failed",
                workspace_id=workspace_id,
                item_start_head=item_start_head,
                rollback_floor_head=rollback_floor_head,
                reason_code=exc.reason_code,
            )
            raise AgentVerdictProtocolError(
                reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                message="Could not roll back unaccepted edits after provider failure.",
            ) from exc
        await runner._handle_provider_agent_run_error(workspace_id, exc, state=state)
        raise AgentVerdictExecutionError(reason_code=exc.reason_code) from exc

    # Published before the first await, together with the item-start marker, so a
    # cancellation anywhere in the sequence below neither rewinds the preserved
    # work nor costs the re-attempt its evidence anchor — the same ordering
    # ``preserve_timeout_work_and_raise_cleanup_error`` already uses. Every await
    # from here on is exception-proof by design, so writing the marker early is a
    # no-op for every other exit (PRRT_kwDOSJAM6s6fylWD).
    #
    # The marker is also written straight to the workspace row, ahead of every
    # other await, because an in-memory marker only survives a *clean* exit: it
    # reaches the DB through ``run()``'s post-``_execute`` ``_persist_state``, and
    # a worker killed between here and there leaves the salvaged commits on disk
    # with no anchor, so the retry rejects an honest no-change ``FIXED`` as
    # ``AGENT_FIXED_WITHOUT_EVIDENCE`` (PRRT_kwDOSJAM6s6fzBXj).
    #
    # Publishing the claim is only the *first* of the three things this path
    # owes, and the other two await. A cancellation landing in either one used to
    # escape with the claim already made: the caller skipped the rollback, right,
    # but its nested guard also skipped ``preserve_cancelled_timeout_work``, which
    # only runs for timeouts no handler ever saw. The half-finished sequence then
    # left the edits dirty for the next pass's ``PRE_EXISTING_DIRTY_WORKTREE``
    # guard, or the anchor in memory only on a worker that may never persist it.
    # So run both under a shield and hand the cancellation on afterwards
    # (PRRT_kwDOSJAM6s6f2gbw).
    timeout_preservation_sink.append(exc.reason_code)
    preserve_outcome = await _finish_timeout_preservation(
        _timeout_anchor_and_sink_steps(
            runner,
            workspace_id=workspace_id,
            reason_code=exc.reason_code,
            item_start_head=item_start_head,
            state=state,
            item_id=item_id,
            item_body_hash=item_body_hash,
            commit_message=commit_message,
            compose_project=compose_project,
            compose_file=compose_file,
            task_tag=task_tag,
            command_evidence=command_evidence,
            commit_dirty_changes=commit_dirty_changes,
        ),
        workspace_id=workspace_id,
        reason_code=exc.reason_code,
        item_start_head=item_start_head,
    )
    sink_outcome = preserve_outcome.sink
    dirty_changes_committed = sink_outcome is TimeoutSinkOutcome.COMMITTED
    # Only a sink that *ran* without committing can be hiding a failed
    # ``git status`` / ``git add`` / ``git commit``: behind "nothing to commit"
    # (PRRT_kwDOSJAM6s6fwr71) or behind a swallowed exception whose reason code
    # is logged but never persisted (PRRT_kwDOSJAM6s6fxp82). Both leave the
    # timed-out edits where the next pass's pre-existing-dirty guard rejects
    # them, so both take the residue probe. A committed sink cleared the dirt and
    # a disabled one never owned it.
    sink_stranded_dirt = sink_outcome in _SINK_OUTCOMES_THAT_MAY_STRAND and (
        await _timeout_sink_left_pr_worthy_residue(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            reason_code=exc.reason_code,
        )
    )

    preserved = await _preserved_head_probe(
        runner,
        worktree_path=worktree_path,
        rev_parse_head=rev_parse_head,
        fallback=item_start_head,
    )
    preserved_head = preserved.sha
    work_preserved = _work_survived_timeout(
        dirty_changes_committed=dirty_changes_committed,
        preserved_head=preserved_head,
        preserved_head_read=preserved.read,
        attempt_start_head=(
            rollback_floor_head
            if isinstance(timeout_work_baseline_head, _TimeoutBaselineUnset)
            else timeout_work_baseline_head
        ),
    )
    _log.warning(
        "monitor.agent_verdict_timeout_work_preserved",
        workspace_id=workspace_id,
        reason_code=exc.reason_code,
        item_start_head=item_start_head,
        preserved_head=preserved_head,
        preserved_head_read=preserved.read,
        dirty_changes_committed=dirty_changes_committed,
        work_preserved=work_preserved,
        sink_stranded_dirt=sink_stranded_dirt,
        item_start_head_persisted=preserve_outcome.anchor_persisted,
    )
    if sink_stranded_dirt:
        # The sink failed rather than finding nothing: escalate the commit-sink
        # failure instead of re-queueing an ordinary timeout the next pass can
        # only reject as ``PRE_EXISTING_DIRTY_WORKTREE``.
        await _raise_stranded_timeout_sink_failure(
            runner,
            exc=exc,
            workspace_id=workspace_id,
            state=state,
            item_start_head=item_start_head,
            preserved_head=preserved_head,
            sink_outcome=sink_outcome,
        )
    # Recorded after the work is preserved and the marker is written, so a
    # provider-recovery escalation (retry / fallback / auth) still finds both in
    # place. Those escalations deliberately propagate: they short-circuit the
    # monitor cycle so the next pass runs on the fallback provider, and — unlike
    # the pre-#932 handler — nothing between here and the monitor loop rolls the
    # worktree back, so propagating them no longer costs the agent its commits.
    await runner._handle_provider_agent_run_error(workspace_id, exc, state=state)
    raise AgentVerdictExecutionError(
        reason_code=exc.reason_code,
        reason=_preserved_work_reason(
            reason_code=exc.reason_code,
            preserved_head=preserved_head,
            item_start_head=item_start_head,
            work_preserved=work_preserved,
        ),
        preserved_head_sha=preserved_head if work_preserved else None,
    ) from exc


async def _anchor_item_start_head_without_aborting(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    reason_code: str,
    item_start_head: str | None,
    state: MonitorState | None,
    item_id: str | None,
    item_body_hash: str | None,
) -> bool:
    """Write the item's durable anchor; never take the rest of the sequence down.

    ``remember_item_start_head_durably`` only handles ``SQLAlchemyError``/``OSError``
    itself, and the worker shutdown these preserve sequences run under commonly
    closes the session factory, which raises outside that set. Letting that abort a
    sequence whose preservation claim is already published would skip
    ``_sink_timeout_dirty_changes`` and leave the timed-out edits dirty for the next
    pass's ``PRE_EXISTING_DIRTY_WORKTREE`` guard — the one state these sequences
    exist to avoid, and why the sibling ``_cancelled_timeout_preserve_steps`` keeps
    going too (PRRT_kwDOSJAM6s6f2_KT).

    Logged rather than re-raised, unlike in that sibling: both callers here owe a
    specific error — the timeout verdict, the cleanup failure — that an unrelated
    anchor exception must not displace, and the in-memory marker written before the
    durable one, plus the ordinary ``_persist_state``, remain the fallback for every
    exit that is not a killed worker. ``asyncio.CancelledError`` is a
    ``BaseException`` and still propagates to the caller's shield.

    Answers whether the anchor made it, so the caller's preserved record does not
    read as an unqualified success when it did not (PRRT_kwDOSJAM6s6f2ckK).
    """
    try:
        await remember_item_start_head_durably(
            runner,
            workspace_id=workspace_id,
            state=state,
            item_id=item_id,
            head=item_start_head,
            body_hash=item_body_hash,
        )
    except Exception as anchor_exc:  # noqa: BLE001 - logged; the sink still owes a run
        _log.warning(
            "monitor.agent_verdict_timeout_preserve_anchor_failed",
            workspace_id=workspace_id,
            reason_code=reason_code,
            item_start_head=item_start_head,
            exc_type=type(anchor_exc).__name__,
            error=repr(anchor_exc)[:400],
        )
        return False
    return True


async def _timeout_anchor_and_sink_steps(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    reason_code: str,
    item_start_head: str | None,
    state: MonitorState | None,
    item_id: str | None,
    item_body_hash: str | None,
    commit_message: str,
    compose_project: str,
    compose_file: Path,
    task_tag: str | None | _TaskTagUnset,
    command_evidence: list[str],
    commit_dirty_changes: bool,
) -> TimeoutPreserveOutcome:
    """Record the item's durable anchor, then sink the timed-out agent's edits.

    The two steps the preserve claim owes once it has been published, kept in one
    coroutine so a cancellation cannot land *between* them either. A failed anchor
    does not cancel the sink — losing the durable marker costs the retry its
    evidence range, while skipping the sink wedges the next pass outright — but it
    is reported alongside the sink outcome, because the preserved record is what
    the next pass reads.
    """
    anchor_persisted = await _anchor_item_start_head_without_aborting(
        runner,
        workspace_id=workspace_id,
        reason_code=reason_code,
        item_start_head=item_start_head,
        state=state,
        item_id=item_id,
        item_body_hash=item_body_hash,
    )
    sink_outcome = await _sink_timeout_dirty_changes(
        runner,
        workspace_id=workspace_id,
        reason_code=reason_code,
        item_start_head=item_start_head,
        commit_message=commit_message,
        compose_project=compose_project,
        compose_file=compose_file,
        state=state,
        task_tag=task_tag,
        command_evidence=command_evidence,
        commit_dirty_changes=commit_dirty_changes,
    )
    return TimeoutPreserveOutcome(sink=sink_outcome, anchor_persisted=anchor_persisted)


async def _finish_timeout_preservation[PreserveOutcomeT](
    steps: Coroutine[Any, Any, PreserveOutcomeT],
    *,
    workspace_id: str,
    reason_code: str,
    item_start_head: str | None,
) -> PreserveOutcomeT:
    """Run an already-claimed preserve sequence to completion, cancel or not.

    Same shield as ``preserve_cancelled_timeout_work``, for the same reason: the
    caller has already told its cancellation branch not to rewind, so a truncated
    sequence is the one state the next pass cannot recover from — dirty edits its
    pre-existing-dirty guard rejects, or an anchor a dying worker never persists.
    The service-recovery loop's rerun bookkeeping publishes the same claim and owes
    the same completion, so it shares this shield (PRRT_kwDOSJAM6s6f3oxD).

    The cancellation itself is *not* swallowed here, unlike in the tagged-
    cancellation helper: nothing re-raises it for this caller, and the worker is
    shutting down, so it is delivered onward once the steps have finished. A
    failure of the steps then has nowhere left to go — ``shield`` may even have
    consumed it — so it is logged rather than allowed to displace the
    cancellation (PRRT_kwDOSJAM6s6f2gbw).
    """
    preserve_task = asyncio.ensure_future(steps)
    cancelled: asyncio.CancelledError | None = None
    while not preserve_task.done():
        try:
            await asyncio.shield(preserve_task)
        except asyncio.CancelledError as cancel_exc:
            cancelled = cancel_exc
    if cancelled is None:
        return preserve_task.result()
    dropped_exc = None if preserve_task.cancelled() else preserve_task.exception()
    if dropped_exc is not None:
        _log_cancelled_timeout_preserve_failure(
            dropped_exc,
            workspace_id=workspace_id,
            reason_code=reason_code,
            item_start_head=item_start_head,
        )
    raise cancelled


async def _sink_timeout_dirty_changes(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    reason_code: str,
    item_start_head: str | None,
    commit_message: str,
    compose_project: str,
    compose_file: Path,
    state: MonitorState | None,
    task_tag: str | None | _TaskTagUnset,
    command_evidence: list[str],
    commit_dirty_changes: bool,
) -> TimeoutSinkOutcome:
    """Commit whatever the timed-out agent left uncommitted; never raise."""
    if not commit_dirty_changes:
        return TimeoutSinkOutcome.DISABLED
    try:
        committed = await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message=f"{commit_message} (preserved after agent timeout)",
            compose_project=compose_project,
            compose_file=compose_file,
            state=state,
            command_evidence=command_evidence,
            task_tag=task_tag,
            operation_start_head=item_start_head,
        )
    except _SINK_INFRASTRUCTURE_ERRORS as sink_exc:
        # The sink failing does not license a rollback: commits the agent
        # already made stay, and the timeout reason code still flows out.
        _log.warning(
            "monitor.agent_verdict_timeout_dirty_sink_failed",
            workspace_id=workspace_id,
            reason_code=reason_code,
            item_start_head=item_start_head,
            exc_type=type(sink_exc).__name__,
            sink_reason_code=getattr(sink_exc, "reason_code", None),
        )
        return TimeoutSinkOutcome.RAISED
    except Exception as sink_exc:
        # The sink can also raise untyped failures — repository/session errors
        # from the supply-chain policy refresh, raw git errors — which the
        # normal verdict path already acknowledges. Letting one escape here
        # would skip the preserved-HEAD read and would replace the timeout
        # reason code with an unrelated exception, so the next pass could not
        # attribute the salvaged work to this item.
        # ``asyncio.CancelledError`` is a ``BaseException`` and still
        # propagates.
        _log.warning(
            "monitor.agent_verdict_timeout_dirty_sink_unexpected_failure",
            workspace_id=workspace_id,
            reason_code=reason_code,
            item_start_head=item_start_head,
            exc_type=type(sink_exc).__name__,
        )
        return TimeoutSinkOutcome.RAISED
    return TimeoutSinkOutcome.COMMITTED if committed else TimeoutSinkOutcome.NO_COMMIT


async def _timeout_sink_left_pr_worthy_residue(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
    reason_code: str,
) -> bool:
    """Did a no-commit timeout sink leave PR-worthy dirt behind?

    That is the "the sink failed" half of ``TimeoutSinkOutcome.NO_COMMIT``: the
    sink ran ``git status`` / ``git add`` / ``git commit``, reported no commit,
    and the timed-out agent's edits are still sitting in the worktree.

    Fails OPEN on an unreadable or raising probe, unlike the recovery-rerun sink
    that shares this probe: there the cost of guessing wrong is one skipped
    rerun, here it is turning every timeout whose worktree could not be read into
    a terminal commit-sink failure. An unreadable probe therefore keeps today's
    preserve-and-re-queue behaviour, and the next pass's dirty guard remains the
    backstop it already is.
    """
    from awf.runtime.pr_monitor_runner import comment_verdict as _comment_verdict

    try:
        residue_fingerprint = await _comment_verdict._read_correction_pr_worthy_residue_fingerprint(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
        )
    except Exception as probe_exc:
        # Broad on purpose, like every other residue probe on this path: it
        # spawns Git and can raise outside the git-spawn error set. Letting one
        # escape would replace the timeout reason code with an unrelated
        # exception. ``asyncio.CancelledError`` is a ``BaseException`` and still
        # propagates.
        _log.warning(
            "monitor.agent_verdict_timeout_dirty_sink_residue_probe_failed",
            workspace_id=workspace_id,
            reason_code=reason_code,
            exc_type=type(probe_exc).__name__,
        )
        return False
    if residue_fingerprint is None:
        _log.warning(
            "monitor.agent_verdict_timeout_dirty_sink_residue_probe_unreadable",
            workspace_id=workspace_id,
            reason_code=reason_code,
        )
        return False
    return _fingerprint_has_pr_worthy_path_residue(residue_fingerprint)


async def _raise_stranded_timeout_sink_failure(
    runner: PullRequestMonitorRunner,
    *,
    exc: AgentRunError,
    workspace_id: str,
    state: MonitorState | None,
    item_start_head: str | None,
    preserved_head: str | None,
    sink_outcome: TimeoutSinkOutcome,
) -> NoReturn:
    """Escalate a failed timeout sink instead of re-queueing an ordinary timeout.

    Mirrors the CI-repair commit sink (PRRT_kwDOSJAM6s6KY4Wi): surface
    ``REPAIR_DIRTY_COMMIT_FAILED`` — already a terminal monitor reason — so the
    stranded edits are attributable now, instead of resurfacing next pass as an
    unrelated ``PRE_EXISTING_DIRTY_WORKTREE``. Provider recovery is still
    *recorded* (that outage telemetry is real) but its control-flow exception is
    suppressed: letting it propagate would send the next pass to the fallback
    provider and straight into the dirty-worktree guard, masking the sink failure
    all over again.

    No rollback, ever — the timed-out agent's commits and the item's evidence
    anchor (both already in place) survive this exit exactly as they survive the
    ordinary preserve path.

    ``sink_outcome`` says which failure shape stranded the edits — a sink that
    answered "nothing to commit" or one whose exception was swallowed to keep the
    timeout's reason code — so the escalation stays attributable to the sink's own
    logged failure (PRRT_kwDOSJAM6s6fxp82).
    """
    from awf.runtime.pr_monitor_runner.comment_verdict import AgentVerdictProtocolError
    from awf.runtime.pr_monitor_runner.types import (
        ProviderRecoveryAuthError,
        ProviderRecoveryFallbackError,
        ProviderRecoveryRetryError,
    )

    provider_recovery_exc: BaseException | None = None
    try:
        await runner._handle_provider_agent_run_error(workspace_id, exc, state=state)
    except (
        ProviderRecoveryRetryError,
        ProviderRecoveryFallbackError,
        ProviderRecoveryAuthError,
    ) as recovery_exc:
        provider_recovery_exc = recovery_exc
    _log.warning(
        "monitor.agent_verdict_timeout_dirty_sink_stranded",
        workspace_id=workspace_id,
        reason_code=_REPAIR_DIRTY_COMMIT_FAILED_REASON,
        timeout_reason_code=exc.reason_code,
        item_start_head=item_start_head,
        preserved_head=preserved_head,
        sink_outcome=sink_outcome.value,
        provider_recovery=(
            type(provider_recovery_exc).__name__ if provider_recovery_exc is not None else None
        ),
    )
    raise AgentVerdictProtocolError(
        reason_code=_REPAIR_DIRTY_COMMIT_FAILED_REASON,
        message=(
            f"Agent timed out ({exc.reason_code}) and the dirty-worktree sink could not "
            "commit the edits it left behind; they are stranded in the repair worktree."
        ),
    ) from exc


def cleanup_error_agent_timeout_reason_code(exc: ComposeExecCleanupError) -> str | None:
    """The watchdog reason code a compose-cleanup failure is masking, if any.

    The adapter tears the exec stack down *before* raising the agent's own
    ``AgentRunError``, so a cleanup failure on a timed-out run reaches the
    verdict protocol as ``ComposeExecCleanupError`` and never touches the #932
    preserve path. It carries the watchdog classification (see
    ``ComposeExecCleanupError.agent_reason_code``) exactly so that path can still
    be taken (PRRT_kwDOSJAM6s6fvPT_).
    """
    reason_code = getattr(exc, "agent_reason_code", None)
    if isinstance(reason_code, str) and reason_code in AGENT_TIMEOUT_REASON_CODES:
        return reason_code
    return None


def cancellation_agent_timeout_reason_code(exc: BaseException) -> str | None:
    """The watchdog reason code a worker cancellation is masking, if any.

    That same pre-``AgentRunError`` teardown *awaits*, so cancellation can also
    land inside it — with the run already classified as a timeout and nothing
    published yet. The adapter tags the escaping ``CancelledError`` the way it
    tags a cleanup failure so the cancellation branch can protect the timed-out
    run's work instead of rewinding over it (PRRT_kwDOSJAM6s6f0n6B).
    """
    reason_code = getattr(exc, "agent_reason_code", None)
    if isinstance(reason_code, str) and reason_code in AGENT_TIMEOUT_REASON_CODES:
        return reason_code
    return None


async def _cancelled_timeout_preserve_steps(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    reason_code: str,
    item_start_head: str | None,
    state: MonitorState | None,
    item_id: str | None,
    item_body_hash: str | None,
    commit_message: str,
    compose_project: str,
    compose_file: Path,
    task_tag: str | None | _TaskTagUnset,
    command_evidence: list[str],
    commit_dirty_changes: bool,
) -> None:
    """The anchor + sink half of the #932 sequence, in the handlers' own order.

    The anchor write comes first but may not take the sink down with it. Its own
    handler covers ``SQLAlchemyError``/``OSError``; a session factory closed by the
    shutdown that caused this cancellation raises outside that set, and letting it
    abort here would leave the timed-out edits dirty for the next pass to reject as
    ``PRE_EXISTING_DIRTY_WORKTREE`` — the one state this path exists to avoid. The
    failure is still re-raised once the sink has run, so the caller logs the lost
    anchor rather than silently swallowing it, and the preserved record itself
    carries whether the anchor survived: only the in-memory marker is left, and the
    worker this cancellation is shutting down may never persist it, so the record
    must not read as an unqualified success (PRRT_kwDOSJAM6s6f2ckK).
    """
    anchor_exc: Exception | None = None
    try:
        await remember_item_start_head_durably(
            runner,
            workspace_id=workspace_id,
            state=state,
            item_id=item_id,
            head=item_start_head,
            body_hash=item_body_hash,
        )
    except Exception as exc:  # noqa: BLE001 - re-raised below, after the sink runs
        anchor_exc = exc
    sink_outcome = await _sink_timeout_dirty_changes(
        runner,
        workspace_id=workspace_id,
        reason_code=reason_code,
        item_start_head=item_start_head,
        commit_message=commit_message,
        compose_project=compose_project,
        compose_file=compose_file,
        state=state,
        task_tag=task_tag,
        command_evidence=command_evidence,
        commit_dirty_changes=commit_dirty_changes,
    )
    _log.warning(
        "monitor.agent_verdict_cancelled_timeout_work_preserved",
        workspace_id=workspace_id,
        reason_code=reason_code,
        item_start_head=item_start_head,
        dirty_changes_committed=sink_outcome is TimeoutSinkOutcome.COMMITTED,
        sink_outcome=sink_outcome.value,
        item_start_head_persisted=anchor_exc is None,
        item_start_head_error=None if anchor_exc is None else repr(anchor_exc)[:400],
    )
    if anchor_exc is not None:
        raise anchor_exc


def _log_cancelled_timeout_preserve_failure(
    exc: BaseException,
    *,
    workspace_id: str,
    reason_code: str,
    item_start_head: str | None,
) -> None:
    """Record a preserve sequence that died, however its failure surfaced."""
    _log.warning(
        "monitor.agent_verdict_cancelled_timeout_preserve_failed",
        workspace_id=workspace_id,
        reason_code=reason_code,
        item_start_head=item_start_head,
        exc_type=type(exc).__name__,
    )


async def preserve_cancelled_timeout_work(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    reason_code: str,
    item_start_head: str | None,
    state: MonitorState | None,
    item_id: str | None,
    item_body_hash: str | None = None,
    commit_message: str,
    compose_project: str,
    compose_file: Path,
    task_tag: str | None | _TaskTagUnset = _TASK_TAG_UNSET,
    command_evidence: list[str],
    commit_dirty_changes: bool,
) -> None:
    """Finish the #932 sequence for a timeout only a cancellation tag reports.

    The adapter classifies the watchdog timeout inside the compose cleanup it runs
    *before* raising its ``AgentRunError``, so a cancellation landing there escapes
    tagged with the reason code and nothing else: neither ``handle_agent_run_error``
    nor ``preserve_timeout_work_and_raise_cleanup_error`` ever ran. Reading the tag
    as "do not rewind" keeps the timed-out agent's commits, but the rest of what
    those handlers do is still owed — the uncommitted edits strand the next pass at
    ``PRE_EXISTING_DIRTY_WORKTREE``, and with no marker a self-committed fix
    restarts anchored at its own preserved HEAD and is rejected as
    ``AGENT_FIXED_WITHOUT_EVIDENCE`` (PRRT_kwDOSJAM6s6f2I94).

    Runs shielded: the caller is already cancelled, so an ordinary await here can be
    cut short again and leave the marker written but the edits dirty — the one state
    the next pass cannot recover from. Never raises for the same reason the adapter's
    own cancelled-cleanup sweep does not: the tagged ``CancelledError`` is re-raised
    immediately after and must not be displaced by a storage or Git failure. A sink
    that leaves dirt behind is *not* escalated the way ``handle_agent_run_error``
    escalates it — escalating means raising, and the cancellation wins — so the next
    pass's pre-existing-dirty guard stays the backstop there.
    """
    preserve_task = asyncio.ensure_future(
        _cancelled_timeout_preserve_steps(
            runner,
            workspace_id=workspace_id,
            reason_code=reason_code,
            item_start_head=item_start_head,
            state=state,
            item_id=item_id,
            item_body_hash=item_body_hash,
            commit_message=commit_message,
            compose_project=compose_project,
            compose_file=compose_file,
            task_tag=task_tag,
            command_evidence=command_evidence,
            commit_dirty_changes=commit_dirty_changes,
        )
    )
    while not preserve_task.done():
        try:
            await asyncio.shield(preserve_task)
        except asyncio.CancelledError:
            # Cancelled again while the sequence was mid-flight. The shield kept
            # the steps alive; wait for them rather than propagate a half-written
            # preservation — the caller re-raises the tagged cancellation anyway.
            continue
        except Exception as preserve_exc:
            # Broad on purpose: every step below is already best-effort, but the
            # durable marker write can still fail outside its own error set (a
            # closed loop, a repository fault), and losing the anchor must not
            # cost the cancellation its watchdog classification.
            _log_cancelled_timeout_preserve_failure(
                preserve_exc,
                workspace_id=workspace_id,
                reason_code=reason_code,
                item_start_head=item_start_head,
            )
            return
    # A cancellation delivered in the same loop step the sequence failed in makes
    # ``shield`` retrieve the exception itself and never hand it to the awaiter:
    # the loop then just sees a finished task and would return as if preservation
    # had succeeded. Read the outcome off the task so a lost anchor is still
    # reported (PRRT_kwDOSJAM6s6f2ckS).
    dropped_exc = None if preserve_task.cancelled() else preserve_task.exception()
    if dropped_exc is not None:
        _log_cancelled_timeout_preserve_failure(
            dropped_exc,
            workspace_id=workspace_id,
            reason_code=reason_code,
            item_start_head=item_start_head,
        )


async def _cleanup_failure_preservation_steps(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    timeout_reason_code: str,
    item_start_head: str | None,
    state: MonitorState | None,
    item_id: str | None,
    item_body_hash: str | None,
    commit_message: str,
    compose_project: str,
    compose_file: Path,
    task_tag: str | None | _TaskTagUnset,
    command_evidence: list[str],
    commit_dirty_changes: bool,
    mirror_path: Path | None,
) -> TimeoutPreserveOutcome:
    """Everything the failed-cleanup preserve claim owes, in one coroutine.

    Ordering is the caller's: durable anchor first, then the hook repair that
    strips a poisoned hooks path, then the sink that commits the timed-out edits.
    Kept together so a cancellation cannot land *between* the steps either — a
    repair failure still propagates in place of the cleanup error, because
    ``_finish_timeout_preservation`` re-raises it when nothing cancelled. Only the
    repair may end the sequence early: it guards the commit the sink is about to
    run. A failed anchor does not, or the published claim would strand the dirt
    (PRRT_kwDOSJAM6s6f2_KT).
    """
    from awf.runtime.pr_monitor_runner import comment_verdict as _comment_verdict

    anchor_persisted = await _anchor_item_start_head_without_aborting(
        runner,
        workspace_id=workspace_id,
        reason_code=timeout_reason_code,
        item_start_head=item_start_head,
        state=state,
        item_id=item_id,
        item_body_hash=item_body_hash,
    )
    if mirror_path is not None:
        await _comment_verdict._repair_mirror_hooks_or_raise(
            workspace_id=workspace_id,
            mirror_path=mirror_path,
            stage="after_comment_agent_timeout_cleanup_failure",
        )
    sink_outcome = await _sink_timeout_dirty_changes(
        runner,
        workspace_id=workspace_id,
        reason_code=timeout_reason_code,
        item_start_head=item_start_head,
        commit_message=commit_message,
        compose_project=compose_project,
        compose_file=compose_file,
        state=state,
        task_tag=task_tag,
        command_evidence=command_evidence,
        commit_dirty_changes=commit_dirty_changes,
    )
    return TimeoutPreserveOutcome(sink=sink_outcome, anchor_persisted=anchor_persisted)


async def preserve_timeout_work_and_raise_cleanup_error(
    runner: PullRequestMonitorRunner,
    *,
    exc: ComposeExecCleanupError,
    timeout_reason_code: str,
    workspace_id: str,
    worktree_path: Path,
    item_start_head: str | None,
    state: MonitorState | None,
    item_id: str | None,
    item_body_hash: str | None = None,
    commit_message: str,
    compose_project: str,
    compose_file: Path,
    task_tag: str | None | _TaskTagUnset = _TASK_TAG_UNSET,
    command_evidence: list[str],
    commit_dirty_changes: bool,
    mirror_path: Path | None,
    timeout_preservation_sink: list[str],
) -> NoReturn:
    """Keep a timed-out agent's work, then escalate the cleanup failure.

    Same preservation as ``handle_agent_run_error``: sink the uncommitted edits,
    keep every commit, and remember the item's original start HEAD so the
    re-attempt's evidence range still covers the preserved work. The cleanup
    error is then re-raised unchanged — a process AWF could not prove dead is
    still the outcome, and it must not be downgraded to a timeout. What must not
    happen is the rollback the ordinary cleanup path performs, which would delete
    the timed-out agent's commits (PRRT_kwDOSJAM6s6fvPT_).

    Ordering mirrors the ordinary cleanup branch: the item-start marker is
    written first — and straight to the workspace row, so a worker killed before
    the next ``_persist_state`` cannot cost the re-attempt its anchor either
    (PRRT_kwDOSJAM6s6fzBXj) — so no exit from here loses it, then
    mirror hooks are repaired before the sink runs a commit — a failed teardown
    can leave a live agent behind, and the repair strips a poisoned hooks path.
    A repair failure propagates in place of the cleanup error, again without a
    rollback, so the preserved commits survive either exit.

    ``timeout_preservation_sink`` carries that "no rollback floor applies from
    here on" claim to the caller, exactly as in ``handle_agent_run_error``. The
    anchor write, the hook repair and the sink all await, and worker cancellation
    bypasses their handlers — ``CancelledError`` is a ``BaseException`` — landing
    on the caller's cancellation branch, which would rewind to
    ``rollback_floor_head`` and delete the commits this path exists to keep
    (PRRT_kwDOSJAM6s6fyvd1). So, as on the ordinary entry, the preservation the
    published claim promises finishes under a shield before the cancellation is
    handed onward: a cancellation landing mid-sequence would otherwise escape
    with the claim already made but the work only half kept, and the caller's
    nested guard cannot repair that — ``preserve_cancelled_timeout_work`` runs
    only for timeouts no handler ever saw (PRRT_kwDOSJAM6s6f2gbw).
    """
    timeout_preservation_sink.append(timeout_reason_code)
    preserve_outcome = await _finish_timeout_preservation(
        _cleanup_failure_preservation_steps(
            runner,
            workspace_id=workspace_id,
            timeout_reason_code=timeout_reason_code,
            item_start_head=item_start_head,
            state=state,
            item_id=item_id,
            item_body_hash=item_body_hash,
            commit_message=commit_message,
            compose_project=compose_project,
            compose_file=compose_file,
            task_tag=task_tag,
            command_evidence=command_evidence,
            commit_dirty_changes=commit_dirty_changes,
            mirror_path=mirror_path,
        ),
        workspace_id=workspace_id,
        reason_code=timeout_reason_code,
        item_start_head=item_start_head,
    )
    _log.warning(
        "monitor.agent_verdict_timeout_cleanup_failure_work_preserved",
        workspace_id=workspace_id,
        reason_code=timeout_reason_code,
        cleanup_reason_code=exc.reason_code,
        item_start_head=item_start_head,
        dirty_changes_committed=preserve_outcome.sink is TimeoutSinkOutcome.COMMITTED,
        item_start_head_persisted=preserve_outcome.anchor_persisted,
        worktree_path=str(worktree_path),
    )
    raise exc


def _work_survived_timeout(
    *,
    dirty_changes_committed: bool,
    preserved_head: str | None,
    attempt_start_head: str | None,
    preserved_head_read: bool = True,
) -> bool:
    """Did this attempt actually leave work behind for the next one to resume?

    ``preserved_head`` is simply whatever HEAD the worktree is on, so it is
    nonempty even when the agent produced nothing — and consumers such as the
    operator-hint retry gate read a nonempty ``preserved_head_sha`` as proof that
    work survived (#934 audit). Only a sink that committed, or a HEAD that moved
    past this attempt's own start, is that proof. An unknown attempt start cannot
    show HEAD standing still, so it fails open: over-reporting costs one extra
    attempt, under-reporting parks work a human then has to rescue.

    ``preserved_head_read`` fails open the same way, for the other unknown. A
    timed-out agent that self-committed leaves a clean tree, so the dirty sink
    reports no commit and the moved HEAD is the *only* remaining proof; when the
    probe could not read it, ``preserved_head`` is the pre-attempt fallback and
    comparing it against the attempt start would "prove" the opposite of the
    truth — the commits stay on disk while the item records "no new work" and the
    operator hint skips its one-time retry straight to a human
    (PRRT_kwDOSJAM6s6f0zfW).
    """
    if dirty_changes_committed:
        return True
    if preserved_head is None:
        return False
    if not preserved_head_read:
        return True
    if attempt_start_head is None:
        return True
    return preserved_head.lower() != attempt_start_head.lower()


def _preserved_work_reason(
    *,
    reason_code: str,
    preserved_head: str | None,
    item_start_head: str | None,
    work_preserved: bool = True,
) -> str:
    if preserved_head is None:
        return f"agent timed out ({reason_code}); no commit could be read to preserve"
    if not work_preserved:
        return (
            f"agent timed out ({reason_code}); no new work to preserve — "
            f"HEAD is still {preserved_head}"
        )
    resume = (
        f" — retrying from the original item start {item_start_head}"
        if item_start_head
        else " — retrying from that state"
    )
    return f"agent timed out ({reason_code}); preserved work at {preserved_head}{resume}"


class _PreservedHeadProbe(NamedTuple):
    """The HEAD a timeout leaves behind, and whether it was actually read.

    ``sha`` degrades to the item-start fallback whenever the worktree is gone or
    the probe could not answer, so on its own it cannot be told apart from a real
    read of an unmoved HEAD. ``read`` keeps that distinction, because
    ``_work_survived_timeout`` draws opposite conclusions from the two
    (PRRT_kwDOSJAM6s6f0zfW).
    """

    sha: str | None
    read: bool


async def _preserved_head_probe(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
    rev_parse_head: Any,
    fallback: str | None,
) -> _PreservedHeadProbe:
    """Read the HEAD the timeout is leaving behind, falling back to the item start."""
    if not worktree_path.exists():
        return _PreservedHeadProbe(fallback, read=False)
    try:
        head = await read_protocol_attempt_start_head(
            runner,
            worktree_path=worktree_path,
            rev_parse_head=rev_parse_head if callable(rev_parse_head) else None,
        )
    except Exception as probe_exc:
        # Broad on purpose, for the same reason the dirty sink is: the probe runs
        # ``_rev_parse_head`` and the item-start-trust snapshot reader, which can
        # raise repository/session or raw git errors outside the git-spawn set.
        # Letting one escape would replace the timeout reason code with an
        # unrelated exception, so the next pass could not attribute the salvaged
        # work to this item. The item start is the designed degraded answer.
        # ``asyncio.CancelledError`` is a ``BaseException`` and still propagates.
        _log.warning(
            "monitor.agent_verdict_timeout_preserved_head_probe_failed",
            worktree_path=str(worktree_path),
            exc_type=type(probe_exc).__name__,
        )
        return _PreservedHeadProbe(fallback, read=False)
    if not head:
        return _PreservedHeadProbe(fallback, read=False)
    return _PreservedHeadProbe(head, read=True)
