"""Merge-block attention marker and awaiting-human attention flag persistence.

Extracted from ``lifecycle`` so that module stays within the first-party
file-line guardrail (``tests/unit/test_core_decomposition_maintainability.py``).
Behavior is unchanged: the functions are wired back onto the runner via
``mixins.py`` and tests reach them through the same ``runner._...`` surface.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from awf.db.repositories import WorkspaceRepository
from awf.runtime.pr_monitor import (
    _MERGE_BLOCK_ATTENTION_STATE_KEY,
    MergeStateStatus,
    MonitorState,
    PRStatus,
)

_MergeBlockAttentionQueueVerdict = Literal["active", "resolved", "indeterminate"]


def _merge_block_attention_queue_verdict(
    status: PRStatus | None,
    *,
    forge: str = "github",
) -> _MergeBlockAttentionQueueVerdict:
    """Classify branch-protection attention from an observable forge status."""
    if status is None:
        return "indeterminate"
    if status.merge_state_status in (MergeStateStatus.BLOCKED, MergeStateStatus.HAS_HOOKS):
        return "active"
    if status.merge_state_status is MergeStateStatus.CLEAN:
        if forge == "bitbucket":
            return "indeterminate"
        return "resolved"
    return "indeterminate"


async def _set_workspace_attention(self: Any, workspace_id: str, *, reason: str) -> None:
    """Persist the awaiting-human attention flag when the monitor escalates.

    Set at the single ``NotifyHuman`` touch-point. Keeps the episode start stable
    across repeated escalations (COALESCE in the repo) while refreshing the reason.
    """
    async with self._deps.session_factory() as s:
        await WorkspaceRepository(s).set_workspace_attention(
            workspace_id,
            reason=reason,
            now=datetime.now(UTC),
        )
        await s.commit()


async def _set_workspace_attention_with_merge_block_marker(
    self: Any,
    workspace_id: str,
    state: MonitorState,
    *,
    reason: str,
) -> None:
    """Atomically persist the merge-block attention marker AND set the
    awaiting-human attention flag in a SINGLE transaction.

    The branch-protection fallback's previous ordering — ``_persist_state`` (marker
    durable) then a separate ``_set_workspace_attention`` commit — closed the window
    where ``awaiting_human_since`` was set but the marker was missing
    (PRRT_kwDOSJAM6s6LbY_X), but created the RECIPROCAL window: a cancel/restart
    after the marker commit but before the attention commit leaves the DB with a
    FRESH ``__awf_merge_block_attention__`` marker but NULL
    ``awaiting_human_since``. On the next poll, if the PR reaches a preserving
    gate before another merge attempt reaches the fallback, the merge
    critical-section TTL path can preserve/re-stamp the marker, while queue,
    reviewer-settle, and initial-grace waits can preserve from forge
    mergeability signals. Neither path creates a missing ``awaiting_human_since``
    episode, so the active branch-protection escalation would not be surfaced to
    the operator until a later merge retry re-enters the fallback
    (PRRT_kwDOSJAM6s6Lcgk0).

    Writing both the marker (merged onto ``monitor_threads_addressed``) and the
    attention columns (``awaiting_human_since`` / ``awaiting_human_reason``) on the
    same workspace row inside one transaction makes the two pieces durable together:
    a restart can never observe one without the other. The marker is taken from the
    in-memory ``state`` (already stamped by ``mark_merge_block_attention``), and
    the attention flag uses the same ``COALESCE`` episode-stability semantics as
    ``set_workspace_attention``.

    The merge-method preflight arm needs no such atomic pairing: it records a
    sticky ``_merge_method_blocked_key`` blocker that flips ``decide()`` to
    ``NotifyHuman``, so the ``Merge``-arm gate waits (and their preserve paths)
    are never reached for that head — the reciprocal window cannot form.
    """
    stamped = state.threads_addressed_ids.get(_MERGE_BLOCK_ATTENTION_STATE_KEY)
    async with self._deps.session_factory() as s:
        ws = await WorkspaceRepository(s).get_for_update(workspace_id)
        if ws is None:
            return
        if stamped is not None:
            threads_addressed = dict(ws.monitor_threads_addressed or {})
            if threads_addressed.get(_MERGE_BLOCK_ATTENTION_STATE_KEY) != stamped:
                threads_addressed[_MERGE_BLOCK_ATTENTION_STATE_KEY] = stamped
                ws.monitor_threads_addressed = threads_addressed
        await WorkspaceRepository(s).set_workspace_attention(
            workspace_id,
            reason=reason,
            now=datetime.now(UTC),
        )
        await s.commit()


async def _clear_stale_merge_attention(
    self: Any,
    workspace_id: str,
    state: MonitorState,
    *,
    now: datetime | None = None,
    status: PRStatus | None = None,
    forge: str = "github",
) -> None:
    """Clear a resolved attention flag at merge critical-section entry.

    This is the #661 merge-window path: once ``decide()`` resumes to ``Merge``,
    clear stale ``NotifyHuman`` attention before the monitor parks in the
    serialized merge lane. A merge-loop branch-protection fallback has no sticky
    blocker, so a fresh ``merge_block_attention`` marker still means the merge
    loop owns the surfaced human wait and must preserve it.

    Queue/reviewer/grace waits no longer use this TTL heuristic. They call
    ``_clear_or_preserve_merge_attention_for_queue_wait`` and decide from an
    observable forge mergeability signal instead (#671).

    The merge-method preflight arm needs no such guard: it records a sticky
    ``_merge_method_blocked_key`` so ``decide()`` returns ``NotifyHuman`` and these
    ``Merge``-arm gate waits are never reached for it.

    ``now`` (default ``datetime.now(UTC)``) lets the merge critical-section entry
    call measure the marker's age against the wall-clock at coordinator ENTRY,
    before the merge-coordinator wait. The branch-protection fallback re-stamps
    the marker every poll while blocked, but the serialized merge coordinator can
    block behind another merge for longer than the TTL without any fallback
    firing (no poll happens during that wait). Measuring age against the
    post-wait clock would reclassify a marker that was FRESH at entry as STALE
    after the wait, clearing ``awaiting_human_since`` and then letting the
    deterministic rejection re-stamp it — flickering/restarting the human-wait
    timer though the operator block never resolved
    (PRRT_kwDOSJAM6s6La_SZ). Measuring against entry time preserves a marker
    that was fresh when the wait started; a marker already stale at entry is
    still cleared (the block resolved before the wait).

    Refresh on preserve is now limited to this merge critical-section TTL path.
    Queue, reviewer-settle, and initial-grace waits do not call this helper for
    preserve decisions; they use
    ``_clear_or_preserve_merge_attention_for_queue_wait`` and decide from forge
    mergeability signals without re-stamping the marker. When this helper does
    preserve a marker that is fresh at critical-section entry, it re-stamps with
    a current wall-clock and persists that single marker key durably. The
    caller-supplied ``now`` still governs the TTL preserve/clear decision, while
    the fresh durable stamp keeps the critical-section TTL marker current for
    later critical-section polls or restarts. When the current forge status
    explicitly reports branch protection still active, preserve and re-stamp
    even if a long queue wait aged the marker past the TTL without a fallback
    re-stamp. A genuinely resolved marker (stale at entry with no active forge
    signal) is still cleared below.
    """
    ttl_seconds = self._config.merge_block_attention_ttl_seconds
    reference = now if now is not None else datetime.now(UTC)
    raw = state.threads_addressed_ids.get(_MERGE_BLOCK_ATTENTION_STATE_KEY)
    if not raw:
        # No marker: a resolved ``NotifyHuman`` episode (not branch-protection),
        # so there is no still-active block to preserve — clear the surfaced flag
        # so it does not keep reporting "awaiting human" while only non-human
        # gates remain (#659/#661). Idempotent clear matches the absent-marker
        # branch of ``merge_block_attention_active``.
        state.clear_merge_block_attention()
        await self._clear_workspace_attention(workspace_id)
        return
    # Use the bounded TTL to distinguish a STILL-blocked fallback (re-stamped
    # every poll, fresh within the TTL) from a RESOLVED block (no fallback has
    # fired recently, marker age exceeds the TTL). The freshness check uses
    # ``reference`` (the caller-supplied ``now``): the critical-section entry
    # passes the coordinator-ENTRY timestamp so a marker FRESH at entry is
    # preserved across a serialized merge wait longer than the TTL. A marker
    # already stale at entry (the block resolved BEFORE the wait) is still
    # cleared.
    forge_still_blocked = _merge_block_attention_queue_verdict(status, forge=forge) == "active"
    if forge_still_blocked or state.merge_block_attention_active(
        now=reference,
        ttl_seconds=ttl_seconds if ttl_seconds is not None and ttl_seconds > 0 else None,
    ):
        # Still-active block at merge critical-section entry: refresh the
        # marker's timestamp so the TTL clock for this critical-section path
        # stays current. Queue/reviewer/grace waits use forge-signal preserve
        # decisions and do not re-stamp.
        #
        # The durable re-stamp MUST use a CURRENT wall-clock rather than the
        # entry timestamp: ``reference`` may intentionally be older than real
        # time after a serialized coordinator wait. Use a fresh wall-clock for
        # the re-stamp while keeping the original entry reference for the
        # preserve/clear decision.
        stamp_now = datetime.now(UTC)
        state.mark_merge_block_attention(now=stamp_now)
        # Persist the re-stamped marker DURABLY before returning. The outer
        # ``run()`` loop only flushes ``state`` after ``_execute`` returns
        # (``runner.py:455``); a cancel/restart before that flush would otherwise
        # strand the OLD marker timestamp on the DB row. A later
        # critical-section TTL check could then clear and re-surface the same
        # still-active operator block as a new episode. Mirrors the
        # branch-protection fallback's own
        # ``mark_merge_block_attention`` + ``_persist_state`` pairing at
        # ``merge_loop.py:1424``. Persist ONLY the marker key (merged onto the
        # DB-persisted ``monitor_threads_addressed``), never flushing the whole
        # in-memory ``MonitorState`` — the established single-key durable
        # persist pattern (``_persist_forge_transient_retry_count`` /
        # ``_clear_preserved_head_marker_durably``) so unconfirmed in-flight
        # verdicts are not leaked to the DB.
        await self._persist_merge_block_attention_durably(workspace_id, state)
        return
    # Stale (resolved) marker: drop it so the next fresh poll re-stamps cleanly,
    # then clear the surfaced flag. The in-memory drop MUST be paired with a
    # durable removal of the persisted marker, and BOTH the marker removal and
    # the ``awaiting_human_since`` clear MUST land in a SINGLE transaction: the
    # outer ``run()`` loop only flushes ``state`` after ``_execute`` returns
    # (``runner.py:455``), and the prior two-commit sequence
    # (``_clear_workspace_attention`` then a separate single-key marker clear)
    # left a cancel/restart window where ``awaiting_human_since`` was already
    # nulled but the STALE marker still sat on the DB row. A later poll could then
    # reload one side without the other, losing the invariant that marker and
    # surfaced attention move together
    # (PRRT_kwDOSJAM6s6Lf_37, PRRT_kwDOSJAM6s6Lh0zt). Performing both writes under
    # one ``get_for_update`` transaction makes the marker/attention pair
    # unobservable independently — a restart can never see the cleared flag
    # without the also-cleared marker, or vice versa. Mirrors the symmetric
    # atomic persist on the PRESERVE branch
    # (``_set_workspace_attention_with_merge_block_marker``) and the established
    # single-key durable-clear pattern (``_clear_preserved_head_marker_durably``):
    # touch ONLY the ``_MERGE_BLOCK_ATTENTION_STATE_KEY``, never flushing the
    # whole in-memory ``MonitorState``.
    state.clear_merge_block_attention()
    await _clear_merge_block_attention_and_workspace_attention_row_durably(
        self,
        workspace_id,
    )


async def _clear_or_preserve_merge_attention_for_queue_wait(
    self: Any,
    workspace_id: str,
    state: MonitorState,
    *,
    status: PRStatus | None,
    forge: str = "github",
) -> None:
    """Apply the queue-wait forge verdict to ``merge_block_attention``.

    Queue, reviewer-settle, and initial-grace waits do not attempt a merge, so
    they cannot infer branch-protection state from a fresh merge rejection.
    Instead, use the forge mergeability signal already observed for this poll
    (or a targeted re-check performed by the caller when the signal was
    indeterminate):

    - branch protection still blocked -> preserve the existing marker and stable
      ``awaiting_human_since`` without re-stamping;
    - clean/mergeable -> clear on GitHub because it confirms resolution;
    - unknown/error -> preserve conservatively.

    Bitbucket open PRs map to ``CLEAN`` because Bitbucket Cloud does not expose a
    GitHub-style merge-state signal, so Bitbucket ``CLEAN`` is treated like an
    unknown signal and preserves conservatively.
    """
    if not state.threads_addressed_ids.get(_MERGE_BLOCK_ATTENTION_STATE_KEY):
        state.clear_merge_block_attention()
        await self._clear_workspace_attention(workspace_id)
        return
    verdict = _merge_block_attention_queue_verdict(status, forge=forge)
    if verdict in ("active", "indeterminate"):
        return
    await _clear_merge_block_attention_and_workspace_attention_durably(
        self,
        workspace_id,
        state,
    )


async def _clear_merge_block_attention_and_workspace_attention_durably(
    self: Any,
    workspace_id: str,
    state: MonitorState,
) -> None:
    """Clear the marker and awaiting-human attention in one row transaction."""
    state.clear_merge_block_attention()
    await _clear_merge_block_attention_and_workspace_attention_row_durably(
        self,
        workspace_id,
    )


async def _clear_merge_block_attention_and_workspace_attention_row_durably(
    self: Any,
    workspace_id: str,
) -> None:
    """Clear the persisted marker and awaiting-human attention in one transaction."""
    async with self._deps.session_factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.get_for_update(workspace_id)
        if ws is None:
            return
        threads_addressed = dict(ws.monitor_threads_addressed or {})
        if threads_addressed.pop(_MERGE_BLOCK_ATTENTION_STATE_KEY, None) is not None:
            ws.monitor_threads_addressed = threads_addressed
        await repo.clear_workspace_attention(workspace_id)
        await s.commit()


async def _clear_workspace_attention(self: Any, workspace_id: str) -> None:
    """Clear the awaiting-human attention flag once the monitor resumes.

    Called when ``decide()`` returns a resuming action (the human blocker is
    gone) — every action except ``NotifyHuman`` (the block itself) and ``Merge``
    (whose merge loop owns the flag so a branch-protection fallback that keeps
    returning ``Merge`` is not reset every poll). The ``IS NOT NULL``-guarded
    ``UPDATE`` changes no row when the flag is already clear, but it still
    round-trips (open session + statement
    + commit) once per poll per workspace. That per-poll cost is deliberate:
    ``awaiting_human_since`` is persisted, so inferring "already clear" from
    in-process state would strand a stale signal across a monitor restart that
    lands between the set and this clear. The round-trip is negligible beside the
    operation/state/audit writes each poll already performs.
    """
    async with self._deps.session_factory() as s:
        await WorkspaceRepository(s).clear_workspace_attention(workspace_id)
        await s.commit()


async def _persist_merge_block_attention_durably(
    self: Any,
    workspace_id: str,
    state: MonitorState,
) -> None:
    """Persist ONLY the re-stamped merge-block attention marker to the DB row.

    Companion to the merge critical-section "refresh on preserve" re-stamp in
    :func:`_clear_stale_merge_attention`: that re-stamp lives in the in-memory
    ``state`` the outer ``run()`` loop only flushes AFTER ``_execute`` returns
    (``runner.py:455``). Persisting the single marker key here avoids stranding
    the OLD marker timestamp on the DB row after a cancel/restart before the
    outer flush. Queue, reviewer-settle, and initial-grace waits use forge
    mergeability signals via
    :func:`_clear_or_preserve_merge_attention_for_queue_wait` and never use this
    durable helper to re-stamp on preserve.

    Mirrors the established single-key durable persist pattern
    (``_persist_forge_transient_retry_count`` /
    ``_clear_preserved_head_marker_durably``): touch ONLY the
    ``_MERGE_BLOCK_ATTENTION_STATE_KEY``, merged onto the DB-persisted
    ``monitor_threads_addressed``, and NEVER flush the whole in-memory
    ``MonitorState``. Inside a ``Merge``-arm poll the in-memory state can still
    carry markers the outer loop has not confirmed durably; a full
    ``_persist_state`` here would leak them ahead of their own commit windows.

    Writes the value already stamped into ``state`` by
    ``mark_merge_block_attention`` so the DB value exactly matches the
    in-memory value (no second wall-clock read that could drift). No-op when
    the workspace row is gone (a GC/destroy race) or the in-memory marker is
    absent (the caller did not re-stamp).
    """
    stamped = state.threads_addressed_ids.get(_MERGE_BLOCK_ATTENTION_STATE_KEY)
    if stamped is None:
        return
    async with self._deps.session_factory() as s:
        ws = await WorkspaceRepository(s).get_for_update(workspace_id)
        if ws is None:
            return
        threads_addressed = dict(ws.monitor_threads_addressed or {})
        if threads_addressed.get(_MERGE_BLOCK_ATTENTION_STATE_KEY) == stamped:
            return
        threads_addressed[_MERGE_BLOCK_ATTENTION_STATE_KEY] = stamped
        ws.monitor_threads_addressed = threads_addressed
        await s.commit()
