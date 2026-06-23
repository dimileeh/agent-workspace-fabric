"""Merge-block attention marker and awaiting-human attention flag persistence.

Extracted from ``lifecycle`` so that module stays within the first-party
file-line guardrail (``tests/unit/test_core_decomposition_maintainability.py``).
Behavior is unchanged: the functions are wired back onto the runner via
``mixins.py`` and tests reach them through the same ``runner._...`` surface.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from awf.db.repositories import WorkspaceRepository
from awf.runtime.pr_monitor import (
    _MERGE_BLOCK_ATTENTION_STATE_KEY,
    MonitorState,
)


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
    ``awaiting_human_since``. On the next poll, if the PR parks on a non-human gate
    wait (merge queue / reviewer settle / initial review grace / merge lock)
    BEFORE another merge attempt reaches the fallback, the preserve path in
    ``_clear_stale_merge_attention`` sees the fresh marker, re-stamps it, and
    returns WITHOUT setting ``awaiting_human_since`` — so the active
    branch-protection escalation is never surfaced to the operator until a later
    merge retry re-enters the fallback (PRRT_kwDOSJAM6s6Lcgk0).

    Writing both the marker (merged onto ``monitor_threads_addressed``) and the
    attention columns (``awaiting_human_since`` / ``awaiting_human_reason``) on the
    same workspace row inside one transaction makes the two pieces durable together:
    a restart can never observe one without the other. The marker is taken from the
    in-memory ``state`` (already stamped by ``mark_merge_block_attention``), and
    the attention flag uses the same ``COALESCE`` episode-stability semantics as
    ``set_workspace_attention``.

    The merge-method preflight arm needs no such atomic pairing: it records a
    sticky ``_merge_method_blocked_key`` blocker that flips ``decide()`` to
    ``NotifyHuman``, so the ``Merge``-arm non-human gate waits (and their preserve
    path) are never reached for that head — the reciprocal window cannot form.
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
    allow_age_out: bool = True,
) -> None:
    """Clear a resolved ``NotifyHuman`` attention flag before a non-human gate wait,
    unless the merge loop itself set attention for an *still-active* branch-protection
    block.

    The merge loop's non-human gate waits (merge queue, reviewer settle, initial
    review grace) and the merge critical-section entry clear ``awaiting_human_since``
    so a *resolved* ``NotifyHuman`` episode does not keep surfacing "awaiting human"
    while the monitor only waits on a non-human gate or is actively merging (#659,
    #661). But the branch-protection fallback escalates to a human *without* a
    sticky blocker, so ``decide()`` keeps returning ``Merge``; that attention is
    still active while the block persists. Skipping the clear when the marker is
    fresh keeps that signal up across a queue/settle/grace wait instead of wrongly
    nulling it (PRRT_kwDOSJAM6s6LXscz, #663 regression).

    A bounded marker TTL distinguishes a STILL-blocked fallback (re-stamped every
    poll, fresh within the TTL) from a RESOLVED block (no fallback has fired
    recently, marker age exceeds the TTL) (#663). When the marker is stale the
    helper clears it (via ``state.clear_merge_block_attention()``) and proceeds
    with ``_clear_workspace_attention`` so the surfaced flag stops reporting
    "awaiting human" once only non-human gates remain. When fresh (still-blocked)
    behavior is unchanged (preserve — #663 regression intact).

    PRESERVE-WHILE-QUEUED (operator decision on the #663 queue-wait tension): the
    pre-merge non-human gate waits (merge queue, reviewer settle, initial review
    grace) pass ``allow_age_out=False`` so the marker is NEVER aged out by TTL
    while the monitor is parked behind a non-human gate — a still-active
    branch-protection escalation that has resolved externally between polls is
    indistinguishable from one that is still blocked while queued (both yield
    ``decide()=Merge`` + queue blocker + a marker no fallback has re-stamped this
    poll), so ageing the marker out by TTL would falsely clear a still-active
    escalation. The marker persists until a REAL signal confirms resolution: a
    merge re-stamp (the branch-protection fallback firing again), a successful
    merge, or a new commit landing. The bounded false-positive (a resolved block
    still shows "awaiting human" until the queue clears) is an ACCEPTED
    limitation; no forge re-check is added (deferred to a follow-up). The
    critical-section-entry clear (the #661 path) keeps ``allow_age_out=True`` so a
    marker that was already stale at coordinator entry (the block resolved BEFORE
    the wait) is still cleared once the monitor is actively merging.

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

    Refresh on preserve: the branch-protection fallback only re-stamps the marker
    when it actually runs (``handle_merge_action``'s merge-blocker arm, after the
    serialized merge coordinator). Polls that park on a non-human gate wait
    (merge queue, reviewer settle, initial review grace) BEFORE reaching the
    merge attempt never re-stamp the marker, so a still-active block can age past
    the TTL across consecutive waits and the next wait's clear would drop
    ``awaiting_human_since`` even though the human gate is unchanged
    (PRRT_kwDOSJAM6s6LbXWQ). Re-stamping the marker whenever this helper
    preserves it resets the TTL clock for the next wait, so a marker that was
    fresh when observed stays fresh across consecutive non-human gate waits.
    The branch-protection fallback still re-stamps when it fires, and a genuinely
    resolved marker (stale) is still cleared below.

    The freshness check measures the marker's age against the caller-supplied
    ``now`` (defaulting to the current wall-clock), but the durable re-stamp on
    preserve ALWAYS uses a fresh ``datetime.now(UTC)``. The merge critical-section
    entry and post-lock gate clears pass the coordinator-ENTRY timestamp as ``now``
    so a marker FRESH at entry is preserved across a serialized merge wait longer
    than the TTL (PRRT_kwDOSJAM6s6La_SZ, PRRT_kwDOSJAM6s6LcfXk); but that entry
    timestamp is stale relative to real time after the wait, so re-stamping the
    marker with it would let the marker age past the TTL during the subsequent
    post-lock gate wait. The next poll — or a restart during that wait — would
    then clear ``awaiting_human_since`` though the operator block never resolved
    (PRRT_kwDOSJAM6s6LdM4X). Using a current wall-clock for the re-stamp keeps
    the TTL clock fresh against real time going into the gate wait, while the
    entry-time reference still governs the preserve/clear decision.
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
    if not allow_age_out:
        # PRESERVE-WHILE-QUEUED (operator decision on the #663 queue-wait
        # tension): the monitor is parking behind a pre-merge non-human gate
        # wait (merge queue / reviewer settle / initial review grace). While
        # queued there is no observable signal distinguishing "block still
        # active" from "block resolved externally between polls" — both yield
        # ``decide()=Merge`` + queue blocker + a marker no fallback has
        # re-stamped this poll — so ageing the marker out by TTL would
        # falsely clear a still-active branch-protection escalation. Never
        # age out by TTL here: the marker persists until a REAL signal
        # confirms resolution (a merge re-stamp from the branch-protection
        # fallback firing again, a successful merge, or a new commit
        # landing). Re-stamp to a CURRENT wall-clock so the TTL clock resets
        # for the next wait (PRRT_kwDOSJAM6s6LbXWQ), and persist the re-stamp
        # durably so a cancel/restart during the wait does not strand the old
        # marker timestamp on the DB row (PRRT_kwDOSJAM6s6LcL-G). The bounded
        # false-positive (a resolved block still shows "awaiting human" until
        # the queue clears) is an ACCEPTED limitation; no forge re-check is
        # added (deferred to a follow-up).
        stamp_now = datetime.now(UTC)
        state.mark_merge_block_attention(now=stamp_now)
        await self._persist_merge_block_attention_durably(workspace_id, state)
        return
    # ``allow_age_out=True`` (critical-section entry, the #661 path): use the
    # bounded TTL to distinguish a STILL-blocked fallback (re-stamped every
    # poll, fresh within the TTL) from a RESOLVED block (no fallback has fired
    # recently, marker age exceeds the TTL). The freshness check uses
    # ``reference`` (the caller-supplied ``now``): the critical-section entry
    # passes the coordinator-ENTRY timestamp so a marker FRESH at entry is
    # preserved across a serialized merge wait longer than the TTL
    # (PRRT_kwDOSJAM6s6La_SZ, PRRT_kwDOSJAM6s6LcfXk). A marker already stale
    # at entry (the block resolved BEFORE the wait) is still cleared.
    if state.merge_block_attention_active(
        now=reference,
        ttl_seconds=ttl_seconds if ttl_seconds is not None and ttl_seconds > 0 else None,
    ):
        # Still-active block: refresh the marker's timestamp so the TTL clock
        # resets for the next non-human gate wait. Without this, consecutive
        # waits that never reach the merge-blocker fallback let the marker age
        # past the TTL and the next wait clears the still-active signal
        # (PRRT_kwDOSJAM6s6LbXWQ).
        #
        # The durable re-stamp MUST use a CURRENT wall-clock: the entry
        # timestamp is stale relative to real time after the wait, so stamping
        # the marker with it would let the marker age past the TTL during the
        # subsequent post-lock gate wait (merge queue / reviewer settle /
        # initial review grace). The next poll — or a restart during that
        # wait — would then measure the stale marker, exceed the TTL, clear
        # ``awaiting_human_since``, and let the still-active branch-protection
        # rejection re-stamp it, restarting the human-wait timer though the
        # operator block never resolved (PRRT_kwDOSJAM6s6LdM4X). Use a fresh
        # wall-clock for the re-stamp so the TTL clock resets against real time
        # going into the gate wait; the freshness check is unaffected because
        # a marker fresh at ``reference`` is still fresh at any later clock.
        stamp_now = datetime.now(UTC)
        state.mark_merge_block_attention(now=stamp_now)
        # Persist the re-stamped marker DURABLY before returning. The outer
        # ``run()`` loop only flushes ``state`` after ``_execute`` returns
        # (``runner.py:455``); a cancel/restart during the subsequent non-human
        # gate wait (merge queue / reviewer settle / initial review grace)
        # would otherwise strand the OLD marker timestamp on the DB row. On
        # the next poll ``_clear_stale_merge_attention`` would measure the
        # stale marker against ``now``, exceed the TTL, clear
        # ``awaiting_human_since``, and let the still-active branch-protection
        # rejection re-stamp it — restarting the human-wait timer even though
        # the operator block never resolved (PRRT_kwDOSJAM6s6LcL-G). Mirrors the
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
    # (``_clear_workspace_attention`` then ``_clear_merge_block_attention_durably``)
    # left a cancel/restart window where ``awaiting_human_since`` was already
    # nulled but the STALE marker still sat on the DB row. The next poll's
    # ``_clear_stale_merge_attention`` — or the ``allow_age_out=False`` queue-wait
    # preserve path — would then re-stamp the stale marker fresh and PRESERVE the
    # human-wait signal without any fallback having fired to restore
    # ``awaiting_human_since`` (the ``merge_loop.py`` branch-protection re-stamp
    # only fires on an active rejection), wedging the monitor in a faux
    # "awaiting human" state until another merge fallback runs
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


async def _clear_merge_block_attention_durably(self: Any, workspace_id: str) -> None:
    """Remove ONLY the merge-block attention marker from the persisted row.

    Symmetric counterpart to :func:`_persist_merge_block_attention_durably`
    and companion to the in-memory :func:`MonitorState.clear_merge_block_attention`.
    ``_clear_stale_merge_attention``'s stale (resolved) branch drops the marker
    in-memory and clears ``awaiting_human_since`` in the DB, but the outer
    ``run()`` loop only flushes ``state`` after ``_execute`` returns
    (``runner.py:455``). A cancel/restart before that full ``_persist_state``
    would otherwise reload the STALE marker from the persisted row while
    ``awaiting_human_since`` is already null — so the next poll's
    ``_clear_stale_merge_attention`` (or the ``allow_age_out=False`` queue-wait
    preserve path) would re-stamp the stale marker fresh and PRESERVE the
    human-wait signal without any fallback having fired to restore
    ``awaiting_human_since`` (the ``merge_loop.py`` branch-protection re-stamp
    only fires on an active rejection), wedging the monitor in a faux
    "awaiting human" state until another merge fallback runs
    (PRRT_kwDOSJAM6s6Lf_37).

    Mirrors the established single-key durable-clear pattern
    (``_clear_preserved_head_marker_durably``): touch ONLY the
    ``_MERGE_BLOCK_ATTENTION_STATE_KEY``, merged off the DB-persisted
    ``monitor_threads_addressed``, and NEVER flush the whole in-memory
    ``MonitorState``. No-op when the workspace row is gone (a GC/destroy race)
    or the marker is already absent.
    """
    async with self._deps.session_factory() as s:
        ws = await WorkspaceRepository(s).get_for_update(workspace_id)
        if ws is None:
            return
        threads_addressed = dict(ws.monitor_threads_addressed or {})
        if threads_addressed.pop(_MERGE_BLOCK_ATTENTION_STATE_KEY, None) is None:
            return
        ws.monitor_threads_addressed = threads_addressed
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

    Companion to the "refresh on preserve" re-stamp in
    :func:`_clear_stale_merge_attention`: that re-stamp lives in the in-memory
    ``state`` the outer ``run()`` loop only flushes AFTER ``_execute`` returns
    (``runner.py:455``). A cancel/restart during the subsequent non-human gate
    wait (merge queue / reviewer settle / initial review grace) would strand
    the OLD marker timestamp on the persisted row; the next poll's
    ``_clear_stale_merge_attention`` would measure the stale marker, exceed the
    TTL, clear ``awaiting_human_since``, and let the still-active
    branch-protection rejection re-stamp it — restarting the human-wait timer
    even though the operator block never resolved (PRRT_kwDOSJAM6s6LcL-G).

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
