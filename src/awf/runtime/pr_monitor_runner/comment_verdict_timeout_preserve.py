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
   nothing and whose HEAD never moved past this attempt's start reports ``None``,
   so gates that read it as "work survived" (the operator-hint timeout retry)
   are not fooled by an unchanged HEAD (#934 audit).

One timeout does *not* re-queue: a sink that ran and left the timed-out edits
dirty — whether it committed nothing or raised — has FAILED, not found the
worktree empty, and re-queueing it hands the next comment-repair pass a dirty
worktree its pre-existing-dirty guard rejects as ``PRE_EXISTING_DIRTY_WORKTREE``
— the sink failure masked and the preserved work stranded. That case escalates as
``REPAIR_DIRTY_COMMIT_FAILED`` instead, exactly as the CI-repair commit sink
already does, and still without a rollback (PRRT_kwDOSJAM6s6fwr71,
PRRT_kwDOSJAM6s6fxp82).

Kept in a sibling module so ``comment_verdict`` stays under the line budget;
re-exported from there (``X as X``) so monkeypatch seams keep working.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from sqlalchemy.exc import SQLAlchemyError

from awf.adapters.base import AgentRunError
from awf.adapters.provider_failures import AGENT_IDLE_TIMEOUT, AGENT_TIMEOUT
from awf.common.compose_exec import ComposeExecCleanupError
from awf.common.logging import get_logger
from awf.db.repositories import WorkspaceRepository
from awf.runtime.pr_monitor_runner.comment_verdict_residue_fingerprint import (
    _fingerprint_has_pr_worthy_path_residue,
    read_protocol_attempt_start_head,
)
from awf.runtime.pr_monitor_runner.comment_verdict_rollback import (
    _rollback_or_classify_failure,
)
from awf.runtime.pr_monitor_runner.constants import (
    _REPAIR_DIRTY_COMMIT_FAILED_REASON,
    _TASK_TAG_UNSET,
    _TaskTagUnset,
)
from awf.runtime.pr_monitor_runner.types import SINK_INFRASTRUCTURE_ERRORS

if TYPE_CHECKING:
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


_ITEM_START_HEAD_STATE_KEY_PREFIX = "__awf_item_start_head__:"
_ITEM_START_HEAD_BODY_HASH_SEPARATOR = ":"

# ``git merge-base --is-ancestor`` answers "no" with exit 1; every other non-zero
# exit is an error, not an answer. ``git rev-parse --verify --quiet`` likewise
# uses exit 1 for "this name resolves to nothing".
_GIT_NOT_AN_ANCESTOR_RETURN_CODE = 1
_GIT_UNRESOLVABLE_NAME_RETURN_CODE = 1

# Infrastructure exits the dirty-worktree sink already declares. They are logged
# and swallowed here: the preserved commits must survive a sink failure, and the
# timeout reason code must still reach the caller.
_SINK_INFRASTRUCTURE_ERRORS = SINK_INFRASTRUCTURE_ERRORS


def item_start_head_state_key(item_id: str) -> str:
    """Reserved ``MonitorState.threads_addressed_ids`` key for an item's start HEAD."""
    return f"{_ITEM_START_HEAD_STATE_KEY_PREFIX}{item_id}"


def _encode_item_start_marker(head: str, body_hash: str | None) -> str:
    """Bind a remembered start HEAD to the feedback body it was written for."""
    if not body_hash:
        return head
    return f"{body_hash}{_ITEM_START_HEAD_BODY_HASH_SEPARATOR}{head}"


def _decode_item_start_marker(raw: str | None) -> tuple[str | None, str | None]:
    """Split a stored marker into ``(body_hash, head)``.

    Markers written by callers that carry no body hash — and any written by a
    parent monitor before the binding existed — are bare SHAs and decode to
    ``(None, sha)``, which keeps their pre-binding behaviour.
    """
    if not raw:
        return (None, None)
    body_hash, separator, head = raw.partition(_ITEM_START_HEAD_BODY_HASH_SEPARATOR)
    if not separator:
        return (None, raw)
    return (body_hash or None, head or None)


def item_start_body_hash_changed(recorded: str | None, current: str | None) -> bool:
    """Did the feedback body change since the marker was written?

    Only a definitive mismatch counts. An unknown hash on either side — a legacy
    bare-SHA marker, or a caller that supplies no body hash — proves nothing, and
    dropping the anchor on a guess costs the preserved commits their place in the
    item's own evidence range.
    """
    return bool(recorded) and bool(current) and recorded != current


def remember_item_start_head(
    state: MonitorState | None,
    item_id: str | None,
    head: str | None,
    body_hash: str | None = None,
) -> None:
    """Persist the item's original start HEAD, bound to its feedback body."""
    if state is None or not item_id or not head:
        return
    state.mark_addressed(
        item_start_head_state_key(item_id),
        _encode_item_start_marker(head, body_hash),
    )


async def remember_item_start_head_durably(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    state: MonitorState | None,
    item_id: str | None,
    head: str | None,
    body_hash: str | None = None,
) -> None:
    """Remember the item's start HEAD in memory *and* on the workspace row.

    In memory alone is not enough for a preserved timeout. The marker only reaches
    the DB through ``run()``'s post-``_execute`` ``_persist_state``, and this whole
    path exists because the worker can die in between — cancellation on shutdown,
    a crash, a container stop. The salvaged commits are already on disk, so a lost
    marker is not a lost fix but a wedged one: the retry after the restart anchors
    at the *preserved* HEAD, and the agent that correctly answers "already fixed"
    with no new commit is rejected as ``AGENT_FIXED_WITHOUT_EVIDENCE``.

    Only this one key is written, merged onto the row's own map — never the whole
    ``MonitorState``, which inside a fix cycle still carries unconfirmed addressed
    verdicts a later failure only rolls back in memory (#305). That is the same
    single-key shape ``_persist_forge_transient_retry_count`` and the item-commit
    provenance chain already use mid-``_execute``.

    Best-effort, like every other step of the preserve path: a DB fault must not
    replace the timeout's reason code, and the in-memory marker plus the ordinary
    ``_persist_state`` remain the fallback for the non-crash exits.
    """
    remember_item_start_head(state, item_id, head, body_hash)
    if not item_id or not head:
        return
    session_factory = getattr(getattr(runner, "_deps", None), "session_factory", None)
    if not callable(session_factory):
        return
    try:
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get_for_update(workspace_id)
            if ws is None:
                return
            threads_addressed = dict(ws.monitor_threads_addressed or {})
            threads_addressed[item_start_head_state_key(item_id)] = _encode_item_start_marker(
                head, body_hash
            )
            ws.monitor_threads_addressed = threads_addressed
            await session.commit()
    except (SQLAlchemyError, OSError) as exc:
        _log.warning(
            "monitor.agent_verdict_item_start_head_durable_write_failed",
            workspace_id=workspace_id,
            item_id=item_id,
            item_start_head=head,
            error=repr(exc)[:400],
        )


def consume_item_start_head(
    state: MonitorState | None,
    item_id: str | None,
) -> str | None:
    """Read *and clear* the item's remembered start HEAD.

    Consuming on read is what keeps the marker from outliving the retry it was
    written for: once this attempt produces a verdict the item is finished, and no
    stale anchor can survive into an unrelated later pass over the same item id.
    An attempt that ends *without* a verdict is still owed its anchor, so it is
    re-armed by ``restore_item_start_head`` on the way out (#934 audit).
    """
    if state is None or not item_id:
        return None
    raw = state.threads_addressed_ids.pop(item_start_head_state_key(item_id), None)
    return _decode_item_start_marker(raw)[1]


def peek_item_start_head(
    state: MonitorState | None,
    item_id: str | None,
) -> str | None:
    """Read the item's remembered start HEAD without clearing it."""
    if state is None or not item_id:
        return None
    return _decode_item_start_marker(
        state.threads_addressed_ids.get(item_start_head_state_key(item_id))
    )[1]


def peek_item_start_body_hash(
    state: MonitorState | None,
    item_id: str | None,
) -> str | None:
    """Read the feedback body hash the remembered start HEAD was written for."""
    if state is None or not item_id:
        return None
    return _decode_item_start_marker(
        state.threads_addressed_ids.get(item_start_head_state_key(item_id))
    )[0]


def restore_item_start_head(
    state: MonitorState | None,
    item_id: str | None,
    head: str | None,
    body_hash: str | None = None,
) -> None:
    """Re-arm an anchor consumed by an attempt that died before a verdict.

    ``consume_item_start_head`` runs at the top of the item, before the fallible
    pre-launch ownership/mirror repair, the provider-recovery gate and the agent
    run. Every failure exit from there aborts the fix cycle without marking the
    item addressed, so the item is attempted again — and without the marker that
    attempt would anchor at the *preserved* HEAD and push the timed-out attempt's
    commits out of its own ``FIXED`` evidence range (#934 audit). Consume-on-read
    still holds for a returned verdict: the item is finished, and no stale anchor
    survives into an unrelated later pass. A marker written since — a fresh
    timeout on this very attempt — is newer and wins.

    ``body_hash`` is the hash the consumed marker carried, so re-arming restores
    the same body binding rather than silently re-pointing the anchor at whatever
    feedback the next attempt reads.
    """
    if state is None or not item_id or not head:
        return
    key = item_start_head_state_key(item_id)
    if key in state.threads_addressed_ids:
        return
    state.mark_addressed(key, _encode_item_start_marker(head, body_hash))


async def preserved_anchor_is_reachable(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
    anchor_head: str,
    attempt_start_head: str | None,
) -> bool:
    """Is a preserved anchor still an ancestor of this attempt's start HEAD?

    Re-arming the marker on every attempt that dies before a verdict (#934 audit)
    lets it outlive several failed passes — long enough for a ``SyncBase`` rebase
    to rewrite the branch and strand the anchor on a dropped SHA. Anchoring a
    later attempt there gives an evidence range git cannot resolve, so an honest
    ``FIXED`` can never be proven and the item wedges. Only a definitive "not an
    ancestor" answer drops the anchor: an unreadable probe keeps it, because
    dropping it also costs the preserved commits their place in the item's own
    evidence range.

    ``merge-base --is-ancestor`` spells that definitive answer as exit 1 alone.
    Every other non-zero exit is a non-answer — the command runner reports its own
    timeout as exit 124, and git fatals (a broken worktree, a locked repo, an
    unreadable object store) exit 128 — so those fall through to a direct
    existence probe on the anchor rather than being read as "not an ancestor"
    (#934 audit). A pruned anchor object is the stranding this guard exists for
    and still drops; anything else keeps it.
    """
    from awf.runtime.pr_monitor_runner.comment_verdict_residue import (
        _RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS,
    )
    from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _git_env_for_merge_safety_object_lookup,
    )

    if attempt_start_head is None or anchor_head.lower() == attempt_start_head.lower():
        return True
    if not worktree_path.exists():
        return True
    try:
        result = await runner._deps.runner.run(
            git_worktree_command(
                worktree_path,
                "merge-base",
                "--is-ancestor",
                anchor_head,
                attempt_start_head,
            ),
            env=_git_env_for_merge_safety_object_lookup(),
            timeout_seconds=_RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS,
        )
    except (TimeoutError, OSError, RuntimeError) as probe_exc:
        _log.warning(
            "monitor.agent_verdict_item_start_head_probe_failed",
            anchor_head=anchor_head,
            attempt_start_head=attempt_start_head,
            exc_type=type(probe_exc).__name__,
        )
        return True
    if result.ok:
        return True
    if result.returncode == _GIT_NOT_AN_ANCESTOR_RETURN_CODE:
        return False
    _log.warning(
        "monitor.agent_verdict_item_start_head_probe_inconclusive",
        anchor_head=anchor_head,
        attempt_start_head=attempt_start_head,
        returncode=result.returncode,
        reason_code=result.reason_code,
    )
    return not await _anchor_object_is_missing(
        runner,
        worktree_path=worktree_path,
        anchor_head=anchor_head,
    )


async def _anchor_object_is_missing(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
    anchor_head: str,
) -> bool:
    """Is ``anchor_head`` provably absent from this worktree's object store?

    Reached only when the ancestry probe could not answer. ``git rev-parse
    --verify --quiet <sha>^{commit}`` exits 1 exactly when the name resolves to
    nothing — the pruned-anchor stranding — while a broken repo or a probe
    timeout exits 128 / 124 and proves nothing, so anything but that definitive 1
    keeps the anchor.
    """
    from awf.runtime.pr_monitor_runner.comment_verdict_residue import (
        _RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS,
    )
    from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _git_env_for_merge_safety_object_lookup,
    )

    try:
        result = await runner._deps.runner.run(
            git_worktree_command(
                worktree_path,
                "rev-parse",
                "--verify",
                "--quiet",
                f"{anchor_head}^{{commit}}",
            ),
            env=_git_env_for_merge_safety_object_lookup(),
            timeout_seconds=_RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS,
        )
    except (TimeoutError, OSError, RuntimeError) as probe_exc:
        _log.warning(
            "monitor.agent_verdict_item_start_head_existence_probe_failed",
            anchor_head=anchor_head,
            exc_type=type(probe_exc).__name__,
        )
        return False
    return result.returncode == _GIT_UNRESOLVABLE_NAME_RETURN_CODE


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
    cancellation bypasses all of them — ``CancelledError`` is a
    ``BaseException`` — landing on the caller's cancellation branch, which would
    rewind to ``rollback_floor_head`` and delete the very commits this path
    exists to keep (PRRT_kwDOSJAM6s6fylWD).
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
    timeout_preservation_sink.append(exc.reason_code)
    await remember_item_start_head_durably(
        runner,
        workspace_id=workspace_id,
        state=state,
        item_id=item_id,
        head=item_start_head,
        body_hash=item_body_hash,
    )

    sink_outcome = await _sink_timeout_dirty_changes(
        runner,
        workspace_id=workspace_id,
        reason_code=exc.reason_code,
        item_start_head=item_start_head,
        commit_message=commit_message,
        compose_project=compose_project,
        compose_file=compose_file,
        state=state,
        task_tag=task_tag,
        command_evidence=command_evidence,
        commit_dirty_changes=commit_dirty_changes,
    )
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

    preserved_head = await _preserved_head_sha(
        runner,
        worktree_path=worktree_path,
        rev_parse_head=rev_parse_head,
        fallback=item_start_head,
    )
    work_preserved = _work_survived_timeout(
        dirty_changes_committed=dirty_changes_committed,
        preserved_head=preserved_head,
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
        dirty_changes_committed=dirty_changes_committed,
        work_preserved=work_preserved,
        sink_stranded_dirt=sink_stranded_dirt,
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
    hook repair and the sink both await, and worker cancellation bypasses this
    path entirely — ``CancelledError`` is a ``BaseException`` — landing on the
    caller's cancellation branch, which would rewind to ``rollback_floor_head``
    and delete the commits this path exists to keep (PRRT_kwDOSJAM6s6fyvd1).
    """
    from awf.runtime.pr_monitor_runner import comment_verdict as _comment_verdict

    timeout_preservation_sink.append(timeout_reason_code)
    await remember_item_start_head_durably(
        runner,
        workspace_id=workspace_id,
        state=state,
        item_id=item_id,
        head=item_start_head,
        body_hash=item_body_hash,
    )
    if mirror_path is not None:
        await _comment_verdict._repair_mirror_hooks_or_raise(
            workspace_id=workspace_id,
            mirror_path=mirror_path,
            stage="after_comment_agent_timeout_cleanup_failure",
        )
    dirty_changes_committed = TimeoutSinkOutcome.COMMITTED is await _sink_timeout_dirty_changes(
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
    _log.warning(
        "monitor.agent_verdict_timeout_cleanup_failure_work_preserved",
        workspace_id=workspace_id,
        reason_code=timeout_reason_code,
        cleanup_reason_code=exc.reason_code,
        item_start_head=item_start_head,
        dirty_changes_committed=dirty_changes_committed,
        worktree_path=str(worktree_path),
    )
    raise exc


def _work_survived_timeout(
    *,
    dirty_changes_committed: bool,
    preserved_head: str | None,
    attempt_start_head: str | None,
) -> bool:
    """Did this attempt actually leave work behind for the next one to resume?

    ``preserved_head`` is simply whatever HEAD the worktree is on, so it is
    nonempty even when the agent produced nothing — and consumers such as the
    operator-hint retry gate read a nonempty ``preserved_head_sha`` as proof that
    work survived (#934 audit). Only a sink that committed, or a HEAD that moved
    past this attempt's own start, is that proof. An unknown attempt start cannot
    show HEAD standing still, so it fails open: over-reporting costs one extra
    attempt, under-reporting parks work a human then has to rescue.
    """
    if dirty_changes_committed:
        return True
    if preserved_head is None:
        return False
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


async def _preserved_head_sha(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
    rev_parse_head: Any,
    fallback: str | None,
) -> str | None:
    """Read the HEAD the timeout is leaving behind, falling back to the item start."""
    if not worktree_path.exists():
        return fallback
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
        return fallback
    return head or fallback
