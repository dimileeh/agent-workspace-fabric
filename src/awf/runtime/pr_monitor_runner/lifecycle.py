"""Pull request monitor lifecycle operations.

Mechanically extracted from the original orchestrator; behavior is unchanged.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from inspect import isawaitable
from pathlib import Path
from typing import Any

from awf.common.audit import redact_audit_text
from awf.common.compose_exec import EXEC_PROCESS_CLEANUP_FAILED
from awf.control.operator_grants import (
    consume_active_operator_grants_in_session,
)
from awf.db.enums import (
    FailureReason,
    WorkspaceStatus,
)
from awf.db.models import Workspace
from awf.db.repositories import (
    WorkspaceEventCreate,
    WorkspaceRepository,
)
from awf.node.auth_mounts import (
    OverlayUnmountUnverifiableError,
    teardown_workspace_auth_overlay,
)
from awf.node.compose_manager import ComposeManager
from awf.runtime.operator_hints import (
    OPERATOR_HINT_PROCESSED_KEY_PREFIX,
    OPERATOR_HINT_STATE_KEY,
    operator_hint_from_threads,
    operator_hint_processed_key,
    persist_operator_hint,
)
from awf.runtime.pr_monitor import (
    _MERGE_BLOCK_ATTENTION_STATE_KEY,
    _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY,
    AbortReason,
    MonitorState,
    OperatorHint,
)
from awf.runtime.pr_monitor_runner.constants import (
    _SYNC_BASE_NO_PROGRESS_COUNT_KEY,
    _SYNC_BASE_NO_PROGRESS_SIGNATURE_KEY,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _forge_transient_retry_count_key,
    _initial_review_grace_done_key,
    _initial_review_grace_started_key,
    _initial_review_grace_state_for_persistence,
    _initial_review_grace_state_for_runtime,
    _initial_review_grace_wall_seconds,
    _non_check_reviewer_settle_done_key,
    _non_check_reviewer_settle_freeze_key,
    _non_check_reviewer_settle_started_prefix,
    _non_check_reviewer_settle_state_for_persistence,
    _non_check_reviewer_settle_state_for_runtime,
    _record_ignored_monitor_terminal_callback,
    _target_reconcile_failure_payload,
    _target_reconcile_log_fields,
    _target_reconcile_payload,
    _truncate_target_reconcile_failure_payload,
)
from awf.runtime.pr_monitor_runner.logging import _log
from awf.service.gc import (
    WorkspaceGCCandidate,
    WorkspaceGCComposeTeardown,
    WorkspaceGCComposeTeardownResult,
    WorkspaceGCResult,
    compose_teardown_result_for_exception,
    run_workspace_filesystem_gc,
)


async def _load_workspace(self: Any, workspace_id: str) -> Workspace:
    async with self._deps.session_factory() as s:
        ws = await WorkspaceRepository(s).get_with_validation_runs(workspace_id)
        if ws is None:
            raise RuntimeError(f"workspace {workspace_id} disappeared mid-monitor")
        return ws


def _load_state(_self: Any, ws: Workspace) -> MonitorState:
    started_raw = ws.monitor_started_at
    # ``MonitorState.started_at`` is monotonic; tests prefer wall-clock
    # semantics so we reconstruct by subtracting the elapsed seconds.
    # If monitor_started_at is unset (legacy/remonitor row), use now; run()
    # persists it before actions that can sleep.
    import time as _time  # local to avoid confusion with datetime above

    now_monotonic = _time.monotonic()
    now_wall = datetime.now(UTC)
    if started_raw is None:
        started_at = now_monotonic
    else:
        started_dt = started_raw
        if started_dt.tzinfo is None:
            started_dt = started_dt.replace(tzinfo=UTC)
        elapsed = (now_wall - started_dt).total_seconds()
        started_at = now_monotonic - max(elapsed, 0.0)
    threads_addressed = dict(ws.monitor_threads_addressed or {})
    sync_base_no_progress_signature = threads_addressed.pop(
        _SYNC_BASE_NO_PROGRESS_SIGNATURE_KEY,
        None,
    )
    sync_base_no_progress_count_raw = threads_addressed.pop(
        _SYNC_BASE_NO_PROGRESS_COUNT_KEY,
        "0",
    )
    try:
        sync_base_no_progress_count = int(sync_base_no_progress_count_raw)
    except (TypeError, ValueError):
        sync_base_no_progress_count = 0
    pending_operator_hint = operator_hint_from_threads(threads_addressed)
    threads_addressed.pop(OPERATOR_HINT_STATE_KEY, None)
    if pending_operator_hint is not None and _operator_hint_is_processed(
        threads_addressed,
        pending_operator_hint,
    ):
        pending_operator_hint = None
    if ws.pr_number is not None:
        threads_addressed = _initial_review_grace_state_for_runtime(
            threads_addressed,
            pr_number=ws.pr_number,
            now_monotonic=now_monotonic,
            now_wall_seconds=now_wall.timestamp(),
            legacy_monotonic_fallback=started_at if started_raw is not None else None,
        )
        threads_addressed = _non_check_reviewer_settle_state_for_runtime(
            threads_addressed,
            pr_number=ws.pr_number,
            now_monotonic=now_monotonic,
            now_wall_seconds=now_wall.timestamp(),
        )
    return MonitorState(
        iter_count=ws.monitor_iter_count,
        last_push_sha=ws.monitor_last_commit_sha,
        sync_base_no_progress_signature=sync_base_no_progress_signature,
        sync_base_no_progress_count=sync_base_no_progress_count,
        threads_addressed_ids=threads_addressed,
        started_at=started_at,
        pending_operator_hint=pending_operator_hint,
    )


def _operator_hint_matches(left: OperatorHint, right: OperatorHint) -> bool:
    if left.operation_id or right.operation_id:
        return left.operation_id == right.operation_id
    return left == right


def _operator_hint_is_terminal(hint: OperatorHint) -> bool:
    return hint.status in {"needs_human", "agent_failed"}


def _operator_hint_is_processed(
    threads_addressed: dict[str, str],
    hint: OperatorHint,
) -> bool:
    return (
        hint.operation_id is not None
        and threads_addressed.get(operator_hint_processed_key(hint.operation_id)) == "processed"
    )


def _clear_processed_operator_hint_from_state(
    state: MonitorState,
    *,
    db_threads_addressed: dict[str, str],
) -> bool:
    state_hint = state.pending_operator_hint
    if state_hint is None or state_hint.operation_id is None:
        return False
    processed_key = operator_hint_processed_key(state_hint.operation_id)
    if db_threads_addressed.get(processed_key) == "processed":
        state.mark_addressed(processed_key, "processed")
    if not _operator_hint_is_processed(state.threads_addressed_ids, state_hint):
        return False
    state.pending_operator_hint = None
    return True


def _merge_concurrent_operator_hint(
    threads_addressed: dict[str, str],
    *,
    db_threads_addressed: dict[str, str],
    state_hint: OperatorHint | None,
) -> dict[str, str]:
    if state_hint is not None and _operator_hint_is_processed(db_threads_addressed, state_hint):
        threads_addressed = persist_operator_hint(threads_addressed, None)
    for key, value in db_threads_addressed.items():
        if key.startswith(OPERATOR_HINT_PROCESSED_KEY_PREFIX) and value == "processed":
            threads_addressed[key] = value
    db_hint = operator_hint_from_threads(db_threads_addressed)
    if db_hint is None:
        return threads_addressed
    if _operator_hint_is_processed(threads_addressed, db_hint):
        return threads_addressed
    if state_hint is not None and _operator_hint_matches(state_hint, db_hint):
        if _operator_hint_is_terminal(db_hint):
            return persist_operator_hint(threads_addressed, db_hint)
        return threads_addressed
    # A newer DB hint supersedes the in-flight hint only in persisted state.
    # The runner still finishes its current in-memory hint cycle; the next
    # persist/load pass re-adds the newer hint for a separate repair attempt.
    return persist_operator_hint(threads_addressed, db_hint)


async def _refresh_operator_state_from_workspace(
    self: Any,
    workspace_id: str,
    state: MonitorState,
) -> bool:
    """Import concurrent operator hint/freeze state without discarding runtime state."""
    async with self._deps.session_factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        if ws is None:
            return False
        db_threads_addressed = dict(ws.monitor_threads_addressed or {})
        pr_number = ws.pr_number

    changed = False
    if _clear_processed_operator_hint_from_state(
        state,
        db_threads_addressed=db_threads_addressed,
    ):
        changed = True

    db_hint = operator_hint_from_threads(db_threads_addressed)
    if db_hint is not None and not (
        _operator_hint_is_processed(state.threads_addressed_ids, db_hint)
        or _operator_hint_is_processed(db_threads_addressed, db_hint)
    ):
        state_hint = state.pending_operator_hint
        state_hint_matches = state_hint is not None and _operator_hint_matches(
            state_hint,
            db_hint,
        )
        db_hint_is_terminal_update = _operator_hint_is_terminal(db_hint) and state_hint != db_hint
        if not state_hint_matches or db_hint_is_terminal_update:
            state.pending_operator_hint = db_hint
            changed = True

    if pr_number is None:
        return changed

    now_monotonic = time.monotonic()
    now_wall = datetime.now(UTC)
    now_wall_seconds = now_wall.timestamp()
    persisted_threads = dict(state.threads_addressed_ids)
    persisted_threads = _initial_review_grace_state_for_persistence(
        persisted_threads,
        pr_number=pr_number,
        now_monotonic=now_monotonic,
        now_wall_seconds=now_wall_seconds,
    )
    persisted_threads = _non_check_reviewer_settle_state_for_persistence(
        persisted_threads,
        pr_number=pr_number,
        now_monotonic=now_monotonic,
        now_wall_seconds=now_wall_seconds,
    )
    newly_marked_thread_ids = state.changed_thread_ids()
    merged_threads = _merge_concurrent_operator_freeze_state(
        dict(persisted_threads),
        db_threads_addressed=db_threads_addressed,
        pr_number=pr_number,
        newly_marked_thread_ids=newly_marked_thread_ids,
    )
    if merged_threads == persisted_threads:
        return changed

    runtime_threads = _initial_review_grace_state_for_runtime(
        merged_threads,
        pr_number=pr_number,
        now_monotonic=now_monotonic,
        now_wall_seconds=now_wall_seconds,
        legacy_monotonic_fallback=state.started_at,
    )
    runtime_threads = _non_check_reviewer_settle_state_for_runtime(
        runtime_threads,
        pr_number=pr_number,
        now_monotonic=now_monotonic,
        now_wall_seconds=now_wall_seconds,
    )
    state.threads_addressed_ids = runtime_threads
    return True


def _same_persisted_wait_marker(left: str | None, right: str) -> bool:
    if left == right:
        return True
    left_seconds = _initial_review_grace_wall_seconds(left)
    right_seconds = _initial_review_grace_wall_seconds(right)
    if left_seconds is None or right_seconds is None:
        return False
    return abs(left_seconds - right_seconds) <= 1.0


def _preserve_concurrent_wait_marker(
    threads_addressed: dict[str, str],
    *,
    db_threads_addressed: dict[str, str],
    started_key: str,
    done_key: str,
    newly_marked_thread_ids: set[str],
) -> None:
    db_started = db_threads_addressed.get(started_key)
    if db_started is None:
        return
    started_matches_db = _same_persisted_wait_marker(
        threads_addressed.get(started_key),
        db_started,
    )
    if not started_matches_db:
        threads_addressed[started_key] = db_started
    if done_key not in db_threads_addressed and (
        done_key not in newly_marked_thread_ids or not started_matches_db
    ):
        threads_addressed.pop(done_key, None)


def _merge_concurrent_operator_freeze_state(
    threads_addressed: dict[str, str],
    *,
    db_threads_addressed: dict[str, str],
    pr_number: int | None,
    newly_marked_thread_ids: set[str] | None = None,
) -> dict[str, str]:
    if pr_number is None:
        return threads_addressed
    newly_marked_thread_ids = newly_marked_thread_ids or set()

    _preserve_concurrent_wait_marker(
        threads_addressed,
        db_threads_addressed=db_threads_addressed,
        started_key=_initial_review_grace_started_key(pr_number),
        done_key=_initial_review_grace_done_key(pr_number),
        newly_marked_thread_ids=newly_marked_thread_ids,
    )

    settle_started_prefix = _non_check_reviewer_settle_started_prefix(
        pr_number=pr_number,
    )
    settle_done_prefix = _non_check_reviewer_settle_done_key(
        pr_number=pr_number,
        head_sha="",
    )
    for started_key in db_threads_addressed:
        if not started_key.startswith(settle_started_prefix):
            continue
        suffix = started_key.removeprefix(settle_started_prefix)
        if not suffix:
            continue
        db_started = db_threads_addressed.get(started_key)
        if db_started is None:
            continue
        done_key = f"{settle_done_prefix}{suffix}"
        started_matches_db = _same_persisted_wait_marker(
            threads_addressed.get(started_key),
            db_started,
        )
        head_sha = suffix.split(":", 1)[0]
        freeze_key = _non_check_reviewer_settle_freeze_key(
            pr_number=pr_number,
            head_sha=head_sha,
        )
        db_has_freeze = db_threads_addressed.get(freeze_key) == "armed"
        if not db_has_freeze and threads_addressed.get(done_key) != "elapsed":
            continue
        preserve_freeze = db_has_freeze and (
            done_key not in newly_marked_thread_ids or not started_matches_db
        )
        if preserve_freeze:
            threads_addressed[freeze_key] = "armed"
        _preserve_concurrent_wait_marker(
            threads_addressed,
            db_threads_addressed=db_threads_addressed,
            started_key=started_key,
            done_key=done_key,
            newly_marked_thread_ids=newly_marked_thread_ids,
        )
    return threads_addressed


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
        ttl_seconds=ttl_seconds if ttl_seconds > 0 else None,
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
    # durable removal of the persisted marker: the outer ``run()`` loop only
    # flushes ``state`` after ``_execute`` returns (``runner.py:455``), so a
    # cancel/restart before that full ``_persist_state`` would otherwise reload
    # the STALE marker from the DB row while ``awaiting_human_since`` is already
    # null (cleared by ``_clear_workspace_attention``). The next
    # ``_clear_stale_merge_attention`` poll — or the
    # ``allow_age_out=False`` queue-wait preserve path — would then re-stamp
    # the stale marker fresh and PRESERVE the human-wait signal without any
    # fallback having fired to restore ``awaiting_human_since`` (the
    # ``merge_loop.py`` branch-protection re-stamp only fires on an active
    # rejection), wedging the monitor in a faux "awaiting human" state until
    # another merge fallback runs (PRRT_kwDOSJAM6s6Lf_37). Mirrors the symmetric
    # durable persist on the PRESERVE branch (``_persist_merge_block_attention_durably``
    # at line 645) and the established single-key durable-clear pattern
    # (``_clear_preserved_head_marker_durably``): touch ONLY the
    # ``_MERGE_BLOCK_ATTENTION_STATE_KEY``, never flushing the whole in-memory
    # ``MonitorState``.
    state.clear_merge_block_attention()
    await self._clear_workspace_attention(workspace_id)
    await self._clear_merge_block_attention_durably(workspace_id)


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


async def _persist_state(self: Any, workspace_id: str, state: MonitorState) -> None:
    async with self._deps.session_factory() as s:
        ws = await WorkspaceRepository(s).get_for_update(workspace_id)
        if ws is None:
            return
        now_monotonic = time.monotonic()
        now_wall = datetime.now(UTC)
        db_threads_addressed = dict(ws.monitor_threads_addressed or {})
        _clear_processed_operator_hint_from_state(
            state,
            db_threads_addressed=db_threads_addressed,
        )
        threads_addressed = dict(state.threads_addressed_ids)
        newly_marked_thread_ids = state.changed_thread_ids()
        if ws.pr_number is not None:
            threads_addressed = _initial_review_grace_state_for_persistence(
                threads_addressed,
                pr_number=ws.pr_number,
                now_monotonic=now_monotonic,
                now_wall_seconds=now_wall.timestamp(),
            )
            threads_addressed = _non_check_reviewer_settle_state_for_persistence(
                threads_addressed,
                pr_number=ws.pr_number,
                now_monotonic=now_monotonic,
                now_wall_seconds=now_wall.timestamp(),
            )
        threads_addressed = persist_operator_hint(threads_addressed, state.pending_operator_hint)
        threads_addressed = _merge_concurrent_operator_hint(
            threads_addressed,
            db_threads_addressed=db_threads_addressed,
            state_hint=state.pending_operator_hint,
        )
        threads_addressed = _merge_concurrent_operator_freeze_state(
            threads_addressed,
            db_threads_addressed=db_threads_addressed,
            pr_number=ws.pr_number,
            newly_marked_thread_ids=newly_marked_thread_ids,
        )
        if (
            state.sync_base_no_progress_signature is not None
            and state.sync_base_no_progress_count > 0
        ):
            threads_addressed[_SYNC_BASE_NO_PROGRESS_SIGNATURE_KEY] = (
                state.sync_base_no_progress_signature
            )
            threads_addressed[_SYNC_BASE_NO_PROGRESS_COUNT_KEY] = str(
                state.sync_base_no_progress_count
            )
        ws.monitor_iter_count = state.iter_count
        ws.monitor_threads_addressed = threads_addressed
        if state.last_push_sha is not None:
            ws.monitor_last_commit_sha = state.last_push_sha
        if ws.monitor_started_at is None:
            elapsed_seconds = max(now_monotonic - state.started_at, 0.0)
            ws.monitor_started_at = now_wall - timedelta(seconds=elapsed_seconds)
        await s.commit()
        state.clear_changed_thread_ids(newly_marked_thread_ids)


async def _persist_forge_transient_retry_count(
    self: Any,
    workspace_id: str,
    *,
    context: str,
    retry_number: int,
) -> None:
    """Persist ONLY the bounded forge-transient retry counter for ``context``.

    The full :func:`_persist_state` flushes the entire in-memory ``MonitorState``.
    Inside a fix-cycle ``_execute`` pass that state can still carry an
    *unconfirmed* addressed verdict — e.g. a ``fix_committed`` thread whose
    ``resolve_thread`` call, or a ``defer`` thread whose tracking-issue creation,
    is the very forge operation being retried; the caller only rolls that verdict
    back *after* the wait returns. Flushing the whole state during the backoff
    would persist the unconfirmed marker before the rollback: if the monitor is
    cancelled or restarted mid-wait, the next poll reloads the thread as addressed
    even though the forge call failed, so ``decide()`` could skip still-open review
    feedback and merge over it (#305). The fix-cycle safety model relies on
    ``_execute`` never persisting unconfirmed markers (see
    ``_resolve_addressed_outdated_threads``). Persist only the retry counter,
    merged onto the DB-persisted state, so the bounded budget still survives across
    polls without flushing any unconfirmed verdict.
    """
    key = _forge_transient_retry_count_key(context)
    async with self._deps.session_factory() as s:
        ws = await WorkspaceRepository(s).get_for_update(workspace_id)
        if ws is None:
            return
        threads_addressed = dict(ws.monitor_threads_addressed or {})
        threads_addressed[key] = str(retry_number)
        ws.monitor_threads_addressed = threads_addressed
        await s.commit()


async def _remove_forge_transient_retry_count(
    self: Any,
    workspace_id: str,
    *,
    context: str,
) -> None:
    """Remove ONLY the bounded forge-transient retry counter for ``context``.

    The success-path counterpart to :func:`_persist_forge_transient_retry_count`:
    once a context's forge operation finally succeeds the incident is over, so the
    persisted counter must be dropped. Like the persist side, it must touch *only*
    the counter key, merged onto the DB-persisted state — never flush the whole
    in-memory ``MonitorState``. Inside a fix-cycle the in-memory state can still
    carry *unconfirmed* addressed verdicts for *later* ``threads_to_resolve`` whose
    forge resolve calls have not run yet; the full :func:`_persist_state` would
    flush those before their own resolve/rollback, so a cancel during a subsequent
    transient resolve wait would reload them as addressed and let ``decide()`` skip
    still-open feedback and merge over it (#305). Removing just the counter key
    keeps the bounded-budget reset durable without persisting any unconfirmed
    marker.
    """
    key = _forge_transient_retry_count_key(context)
    async with self._deps.session_factory() as s:
        ws = await WorkspaceRepository(s).get_for_update(workspace_id)
        if ws is None:
            return
        threads_addressed = dict(ws.monitor_threads_addressed or {})
        if threads_addressed.pop(key, None) is None:
            return
        ws.monitor_threads_addressed = threads_addressed
        await s.commit()


async def _clear_preserved_head_marker_durably(self: Any, workspace_id: str) -> None:
    """Remove ONLY the protected-block preserved-head marker from the persisted row.

    The block-time RECORDING of ``_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY`` is
    durable as soon as the ``monitoring_pr -> blocked`` transition commits — not only
    in the in-memory ``MonitorState`` that the loop flushes later
    (PRRT_kwDOSJAM6s6KEtU6). The CLEAR must be symmetric: when a directive-only
    terminal resume drops the preserved commit and clears the in-memory marker, that
    clear is otherwise not durable until the outer loop's later :func:`_persist_state`.
    A crash in that window reloads the pending directive with the STALE marker, and the
    directive-drop restart shortcut in ``_run_operator_hint_cycle`` — which only checks
    that the local HEAD is on the remote — would finalize the new hint as a no-op,
    SKIP the CLI, and swallow the terminal verdict (PRRT_kwDOSJAM6s6KWAIx). Removing the
    key durably (merged onto the DB row, never flushing the rest of the in-memory state,
    which may still carry an unset terminal verdict) closes that window: a later crash
    leaves NO marker, so the shortcut is skipped and the directive CLI re-runs behind the
    normal guards and re-derives the verdict. No-ops when the key is already absent."""
    async with self._deps.session_factory() as s:
        ws = await WorkspaceRepository(s).get_for_update(workspace_id)
        if ws is None:
            return
        threads_addressed = dict(ws.monitor_threads_addressed or {})
        if threads_addressed.pop(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, None) is None:
            return
        ws.monitor_threads_addressed = threads_addressed
        await s.commit()


async def _clear_preserved_marker_and_consume_grants_durably(self: Any, workspace_id: str) -> None:
    """Clear the preserved-head marker AND consume the active operator grants in ONE
    durable transaction.

    The terminal directive+grant DROP branch of ``_terminal_directive_grant_reblock``
    must do BOTH when the combined CLI dropped the preserved commit: clear the stale
    marker (so a later grant-revoking directive cannot satisfy the directive-drop
    restart shortcut and no-op the operator's follow-up — PRRT_kwDOSJAM6s6KVgwV) AND
    consume the single-use grant that approved ONLY the now-dropped commit (so a later
    ``SyncBase`` cannot honor it for a conflict edit — PRRT_kwDOSJAM6s6KVt_Q).

    Running these as two SEPARATE committed transactions leaves a crash window: clearing
    the marker first drops ``has_preserved_protected_block`` to False on the persisted
    row, so ``decide()`` no longer ranks the resume ahead of ``SyncBase``; if the process
    dies after that commit but before the grant consume commits, the row durably reloads
    with the marker GONE but the grant STILL ACTIVE — exactly the KVt_Q grant leak the
    consume was added to close. Performing both writes under one transaction closes that
    window: a crash before the single commit leaves marker-present + grant-active (the
    resume re-blocks / re-runs safely), and a crash after leaves marker-gone +
    grant-consumed (nothing carries the stale approval forward). Never flushes the rest
    of the in-memory state. No-ops when both are already settled."""
    async with self._deps.session_factory() as s:
        ws = await WorkspaceRepository(s).get_for_update(workspace_id)
        if ws is None:
            return
        threads_addressed = dict(ws.monitor_threads_addressed or {})
        marker_cleared = (
            threads_addressed.pop(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, None) is not None
        )
        if marker_cleared:
            ws.monitor_threads_addressed = threads_addressed
        consumed = await consume_active_operator_grants_in_session(s, workspace_id)
        if marker_cleared or consumed:
            await s.commit()


async def _terminate_completed(
    self: Any,
    workspace_id: str,
    *,
    pr_merge_sha: str | None,
    repo_url: str | None = None,
    base_branch: str | None = None,
    compose_project: str | None = None,
    compose_file: Path | None = None,
) -> None:
    async with self._deps.session_factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.get(workspace_id)
        if ws is None:
            return
        if ws.status != WorkspaceStatus.monitoring_pr.value:
            if (
                ws.status == WorkspaceStatus.completed.value
                and pr_merge_sha
                and not ws.pr_merge_sha
            ):
                ws.pr_merge_sha = pr_merge_sha
            await _record_ignored_monitor_terminal_callback(
                repo,
                ws,
                requested_status=WorkspaceStatus.completed,
                reason_code="MONITOR_DONE",
            )
            await s.commit()
            return
        if pr_merge_sha:
            ws.pr_merge_sha = pr_merge_sha
        await repo.transition(ws, to=WorkspaceStatus.completed, reason_code="MONITOR_DONE")
        await s.commit()
    if repo_url and base_branch:
        await self._reconcile_target_branch_after_merge(
            workspace_id=workspace_id,
            repo_url=repo_url,
            base_branch=base_branch,
        )
    # GC is best-effort and never masks the completion signal. When compose
    # context is present, pass it through the GC engine so compose teardown,
    # auth-overlay unmount, and path deletion share the same failure gate.
    await self._gc_completed_workspace_filesystem(
        workspace_id,
        compose_project=compose_project,
        compose_file=compose_file,
    )


async def _reconcile_target_branch_after_merge(
    self: Any,
    *,
    workspace_id: str,
    repo_url: str,
    base_branch: str,
) -> None:
    reconciler = self._deps.post_merge_target_reconciler
    if reconciler is None:
        return
    try:
        result = await reconciler(repo_url=repo_url, branch=base_branch, workspace_id=workspace_id)
    except Exception as exc:
        failure_event_payload = {
            **_target_reconcile_failure_payload(exc, error_limit=1000),
            "repo_url": repo_url,
            "base_branch": base_branch,
        }
        failure_log_payload = {
            **_truncate_target_reconcile_failure_payload(failure_event_payload, error_limit=500),
            "workspace_id": workspace_id,
        }
        _log.warning(
            "monitor.target_branch_reconcile_failed",
            **failure_log_payload,
        )
        await self._append_workspace_events(
            workspace_id=workspace_id,
            events=[
                WorkspaceEventCreate(
                    event_type="target_branch.reconcile_failed",
                    reason_code="TARGET_BRANCH_RECONCILE_FAILED",
                    payload=failure_event_payload,
                )
            ],
        )
        return

    payload = _target_reconcile_payload(result)
    log_payload = {
        **_target_reconcile_log_fields(payload),
        "workspace_id": workspace_id,
        "base_branch": base_branch,
    }
    _log.info(
        "monitor.target_branch_reconciled",
        **log_payload,
    )
    await self._append_workspace_events(
        workspace_id=workspace_id,
        events=[
            WorkspaceEventCreate(
                event_type="target_branch.reconciled",
                reason_code=str(payload.get("status") or "TARGET_BRANCH_RECONCILED"),
                payload=payload,
            )
        ],
    )


async def _teardown_compose_stack(
    self: Any,
    *,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
) -> bool:
    """Legacy compatibility helper for direct compose teardown calls.

    Completed-workspace monitor cleanup now routes compose teardown through
    ``_gc_completed_workspace_filesystem`` so compose, auth-overlay unmount, and
    path deletion share the filesystem GC failure gate.

    Run ``docker compose down --remove-orphans --volumes`` for a
    terminated workspace. Never raises a regular ``Exception``.

    The call is wrapped in ``except Exception`` so the failure modes
    that routinely bubble up — ``FileNotFoundError`` (no ``docker``
    on PATH, common on dev laptops without the daemon), transient
    I/O errors from the subprocess runner, compose returning junk
    stderr — don't fail a workspace that already merged its PR. The
    DB completion transition has already landed before this method
    runs.

    ``asyncio.CancelledError`` is intentionally NOT caught here
    (since Python 3.8 it inherits from ``BaseException``, so the
    ``except Exception`` clause does not match it). Cancellation
    must propagate cleanly — swallowing it would defeat the loop
    runner's shutdown path."""
    try:
        r = await self._deps.runner.run(
            [
                "docker",
                "compose",
                "-p",
                compose_project,
                "-f",
                str(compose_file),
                "down",
                "--remove-orphans",
                "--volumes",
            ]
        )
    except Exception as exc:
        # docker binary missing, transient I/O, subprocess-runner
        # hiccup — any of these would otherwise propagate and crash
        # the monitor runner. Log and swallow; the DB transition
        # already completed. Cancellation (BaseException) is not in
        # this branch by design — it flows through.
        _log.warning(
            "monitor.compose_teardown_raised",
            workspace_id=workspace_id,
            compose_project=compose_project,
            error=repr(exc)[:400],
        )
        return False

    if r.ok:
        _log.info(
            "monitor.compose_teardown_ok",
            workspace_id=workspace_id,
            compose_project=compose_project,
        )
        return True
    # Compose may already be gone (operator tore it down
    # manually, or an earlier teardown in a retry loop).
    _log.warning(
        "monitor.compose_teardown_failed",
        workspace_id=workspace_id,
        compose_project=compose_project,
        returncode=r.returncode,
        stderr=(r.stderr or "")[:400],
    )
    return False


def _teardown_completed_workspace_auth_overlay(work_dir: Path, workspace_id: str) -> None:
    """Best-effort unmount for a completed workspace's Claude auth overlay.

    Legacy/direct monitor callers may invoke ``run_workspace_filesystem_gc``
    with no ``compose_teardown`` hook. Preserve the old pre-GC unmount attempt
    for that path, even though GC now also unmounts via
    ``_unmount_candidate_auth_overlay`` when it reaches candidate path deletion.
    A still-mounted overlay makes GC's ``rmtree`` of ``auth/<workspace_id>``
    fail with ``EBUSY`` and strands the merged mount. Best-effort: a genuine
    busy/umount error is logged and swallowed (GC's own rmtree surfaces any
    residual EBUSY) so it never masks the completion signal the merge already
    produced.

    A capability-less completion path raises ``OverlayUnmountUnverifiableError``
    (a ``RuntimeError``) — not ``OSError``/``SubprocessError`` — when it cannot see
    the worker's mount namespace. That must be swallowed too: it runs inside the
    ``asyncio.to_thread`` call *before* the GC ``try`` in
    ``_gc_completed_workspace_filesystem``, so letting it propagate would skip the
    whole filesystem GC and strand every pressure/auth dir. The downstream
    ``run_workspace_filesystem_gc`` re-runs the unmount via
    ``_unmount_candidate_auth_overlay`` and, on the same unverifiable result,
    skips only the auth dir while still reclaiming the rest — mirroring the
    ``service gc`` path.
    """

    try:
        teardown_workspace_auth_overlay(work_dir=work_dir, workspace_id=workspace_id)
    except OverlayUnmountUnverifiableError as exc:
        _log.warning(
            "monitor.auth_overlay_teardown_incapable",
            reason_code=exc.reason_code,
            workspace_id=workspace_id,
            error=str(exc)[:400],
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning(
            "monitor.auth_overlay_teardown_failed",
            reason_code="CLAUDE_AUTH_OVERLAY_UNMOUNT_FAILED",
            workspace_id=workspace_id,
            error=repr(exc)[:400],
            # ``repr(CalledProcessError)`` drops the ``umount(8)`` stderr (e.g.
            # "target is busy"); forward it so the EBUSY root cause is greppable.
            stderr=getattr(exc, "stderr", None),
        )


def _completed_workspace_teardown_template_sentinel(work_dir: Path) -> Path:
    """Return the unused template sentinel for monitor-side compose teardown."""
    # ``ComposeManager`` requires a template path for render setup, but this
    # teardown-only instance never renders: it only calls ``teardown_project``
    # with a persisted compose file. Keep the sentinel under ``work_dir`` so
    # installed packages do not infer a fragile source-tree-relative path.
    return work_dir / "compose" / ".completed-workspace-teardown-does-not-render.yml.j2"


def _compose_file_for_gc_candidate(
    candidate: WorkspaceGCCandidate,
    fallback_compose_file: Path | None,
) -> Path:
    """Prefer persisted compose metadata while keeping monitor-run context as fallback."""
    if candidate.compose_file_path:
        return Path(candidate.compose_file_path).expanduser()
    if fallback_compose_file is not None:
        return fallback_compose_file
    return candidate.compose.path / "compose.yml"


def _completed_workspace_compose_teardown(
    self: Any,
    *,
    compose_project: str | None,
    compose_file: Path | None,
) -> WorkspaceGCComposeTeardown | None:
    """Build a volume-removing compose teardown callback for post-merge GC."""
    # A missing compose file is still useful context: ``teardown_project`` falls
    # back to label-scoped cleanup when the path is absent. An empty project
    # string is still accepted so persisted GC candidate metadata can supply the
    # project name; callers that rely on the fallback pass a real monitor
    # project.
    if compose_project is None:
        return None
    fallback_compose_project = compose_project

    manager = ComposeManager(
        work_dir=self._work_dir,
        template_path=_completed_workspace_teardown_template_sentinel(self._work_dir),
    )

    async def _teardown(candidate: WorkspaceGCCandidate) -> WorkspaceGCComposeTeardownResult:
        result = await manager.teardown_project(
            project_name=candidate.compose_project_name or fallback_compose_project,
            compose_file=_compose_file_for_gc_candidate(candidate, compose_file),
            workspace_id=candidate.workspace_id,
            remove_volumes=True,
        )
        return WorkspaceGCComposeTeardownResult(
            status=result.status,
            reason_code=result.reason_code,
            error=result.error,
        )

    return _teardown


def _compose_project_for_gc_log(
    result: WorkspaceGCResult,
    *,
    workspace_id: str,
    fallback_compose_project: str | None,
) -> str | None:
    for candidate in result.plan.candidates:
        if candidate.workspace_id == workspace_id:
            return candidate.compose_project_name or fallback_compose_project
    for preserved in result.plan.preserved:
        if preserved.workspace_id == workspace_id:
            return preserved.compose_project_name or fallback_compose_project
    return fallback_compose_project


def _log_completed_workspace_compose_teardown_result(
    result: WorkspaceGCResult,
    *,
    workspace_id: str,
    compose_project: str | None,
) -> None:
    teardown = result.compose_teardowns.get(workspace_id)
    if teardown is None:
        return

    resolved_compose_project = _compose_project_for_gc_log(
        result,
        workspace_id=workspace_id,
        fallback_compose_project=compose_project,
    )
    _log_completed_workspace_compose_teardown_outcome(
        teardown,
        workspace_id=workspace_id,
        compose_project=resolved_compose_project,
    )


def _log_completed_workspace_compose_teardown_outcome(
    teardown: WorkspaceGCComposeTeardownResult,
    *,
    workspace_id: str,
    compose_project: str | None,
) -> None:
    log_fields: dict[str, object] = {
        "workspace_id": workspace_id,
        "status": teardown.status,
        "reason_code": teardown.reason_code,
    }
    if compose_project is not None:
        log_fields["compose_project"] = compose_project
    if teardown.ok:
        _log.info("monitor.compose_teardown_ok", **log_fields)
        return
    if teardown.error:
        log_fields["error"] = teardown.error[:400]
    _log.warning("monitor.compose_teardown_failed", **log_fields)


def _failed_compose_teardowns_for_gc_log(result: WorkspaceGCResult) -> dict[str, dict[str, object]]:
    return {
        workspace_id: teardown.to_dict()
        for workspace_id, teardown in result.compose_teardowns.items()
        if not teardown.ok
    }


def _track_completed_workspace_compose_teardown(
    compose_teardown: WorkspaceGCComposeTeardown | None,
    *,
    compose_project: str | None,
    tracked_results: dict[str, WorkspaceGCComposeTeardownResult],
    tracked_projects: dict[str, str | None],
) -> WorkspaceGCComposeTeardown | None:
    if compose_teardown is None:
        return None

    async def _tracked(candidate: WorkspaceGCCandidate) -> WorkspaceGCComposeTeardownResult:
        tracked_projects[candidate.workspace_id] = candidate.compose_project_name or compose_project
        try:
            result = compose_teardown(candidate)
            if isawaitable(result):
                result = await result
        except Exception as exc:
            tracked_results[candidate.workspace_id] = compose_teardown_result_for_exception(exc)
            # Re-raise so ``_run_gc_compose_teardowns`` records the failure in
            # ``result.compose_teardowns``; this tracked copy is only for GC
            # exception paths.
            raise
        tracked_results[candidate.workspace_id] = result
        return result

    return _tracked


async def _gc_completed_workspace_filesystem(
    self: Any,
    workspace_id: str,
    *,
    compose_project: str | None = None,
    compose_file: Path | None = None,
) -> None:
    """Remove local pressure directories for a successfully completed workspace.

    The durable DB row, events, logs, and artifacts are intentionally kept.
    """

    compose_teardown = _completed_workspace_compose_teardown(
        self,
        compose_project=compose_project,
        compose_file=compose_file,
    )
    tracked_compose_teardowns: dict[str, WorkspaceGCComposeTeardownResult] = {}
    tracked_compose_projects: dict[str, str | None] = {}
    gc_compose_teardown = _track_completed_workspace_compose_teardown(
        compose_teardown,
        compose_project=compose_project,
        tracked_results=tracked_compose_teardowns,
        tracked_projects=tracked_compose_projects,
    )
    if compose_teardown is None:
        # Legacy/direct callers may invoke this method without compose context.
        # In that case, preserve the old best-effort pre-GC unmount path. When a
        # compose callback exists, GC performs the unmount after compose teardown
        # so a running agent container cannot keep the overlay busy.
        await asyncio.to_thread(
            _teardown_completed_workspace_auth_overlay, self._work_dir, workspace_id
        )
    try:
        result = await run_workspace_filesystem_gc(
            self._deps.session_factory,
            work_dir=self._work_dir,
            workspace_id=workspace_id,
            execute=True,
            # A merged PR's pressure dirs are reclaimable now -- bypass the
            # retention window so disk is returned on merge rather than a week
            # later. The durable DB row, events, and logs are always kept.
            ignore_retention=True,
            compose_teardown=gc_compose_teardown,
        )
    except Exception as exc:
        # Keep the historical alert signal distinct from ordinary partial GC
        # failures logged below.
        _log.warning(
            "monitor.filesystem_gc_raised",
            workspace_id=workspace_id,
            error=repr(exc)[:400],
        )
        for tracked_workspace_id, tracked_teardown in tracked_compose_teardowns.items():
            _log_completed_workspace_compose_teardown_outcome(
                tracked_teardown,
                workspace_id=tracked_workspace_id,
                compose_project=tracked_compose_projects.get(tracked_workspace_id),
            )
        # A present tracked success is the only fallback-path signal that the
        # auth overlay can be unmounted without leaving containers holding it.
        raised_teardown = tracked_compose_teardowns.get(workspace_id)
        if compose_teardown is not None and raised_teardown is not None and raised_teardown.ok:
            await asyncio.to_thread(
                _teardown_completed_workspace_auth_overlay, self._work_dir, workspace_id
            )
        return
    _log_completed_workspace_compose_teardown_result(
        result,
        workspace_id=workspace_id,
        compose_project=compose_project,
    )
    # Empty/no-delete plans never enter GC's candidate deletion loop, so GC has
    # no chance to perform its normal post-compose auth overlay unmount. Run the
    # successful-fallback unmount before any partial-result return; non-compose
    # side effects can still make GC partial after containers are already down.
    if compose_teardown is not None and not any(
        candidate.workspace_id == workspace_id for candidate in result.plan.candidates
    ):
        empty_plan_teardown = result.compose_teardowns.get(workspace_id)
        # A failed fallback teardown may leave agent containers alive. Keep the
        # overlay mounted in that case so runtime side effects stay preserved
        # together with the failed compose teardown evidence.
        if empty_plan_teardown is not None and empty_plan_teardown.ok:
            await asyncio.to_thread(
                _teardown_completed_workspace_auth_overlay, self._work_dir, workspace_id
            )
    if result.status == "partial":
        log_fields: dict[str, object] = {
            "workspace_id": workspace_id,
            "deleted_path_count": len(result.deleted_paths),
            "delete_errors": [error.to_dict() for error in result.delete_errors],
            "reservation_releases": result.reservation_releases,
        }
        failed_compose_teardowns = _failed_compose_teardowns_for_gc_log(result)
        if failed_compose_teardowns:
            log_fields["compose_teardowns"] = failed_compose_teardowns
        _log.warning("monitor.filesystem_gc_failed", **log_fields)
        return
    if not result.plan.candidates and result.plan.preserved:
        preserved = result.plan.preserved[0]
        deferred_log_fields: dict[str, object] = {
            "workspace_id": workspace_id,
            "reason_code": preserved.reason_code,
            "age_hours": preserved.age_hours,
            "retention_hours": result.plan.min_age_hours,
        }
        deferred_teardown = result.compose_teardowns.get(workspace_id)
        if deferred_teardown is not None:
            deferred_log_fields["compose_teardown_status"] = deferred_teardown.status
        _log.info("monitor.filesystem_gc_deferred", **deferred_log_fields)
        return
    ok_log_fields: dict[str, object] = {
        "workspace_id": workspace_id,
        "deleted_path_count": len(result.deleted_paths),
        "reclaimed_bytes": result.plan.total_estimated_bytes,
    }
    ok_teardown = result.compose_teardowns.get(workspace_id)
    if ok_teardown is not None:
        ok_log_fields["compose_teardown_status"] = ok_teardown.status
        if not result.plan.candidates and not result.deleted_paths:
            ok_log_fields["compose_teardown_only"] = True
    _log.info("monitor.filesystem_gc_ok", **ok_log_fields)


async def _terminate_failed(
    self: Any,
    workspace_id: str,
    *,
    message: str,
    reason_code: AbortReason | str | None = None,
) -> None:
    async with self._deps.session_factory() as s:
        repo = WorkspaceRepository(s)
        # Lock the row before the owner fence below: a plain ``get`` leaves a
        # TOCTOU window where this stale runner reads its old ``monitor_claimed_by``,
        # a newer worker reclaims the monitor lease (``claim_monitoring_pr``
        # reassigns expired leases), and this session still commits ``failed`` at
        # the transition below. ``get_for_update`` holds the row from this read
        # through the commit so the fence check and the state write are atomic.
        ws = await repo.get_for_update(workspace_id)
        if ws is None:
            return
        rc = reason_code.value if isinstance(reason_code, AbortReason) else reason_code
        rc = rc or "MONITOR_ABORT"
        if ws.status != WorkspaceStatus.monitoring_pr.value:
            await _record_ignored_monitor_terminal_callback(
                repo,
                ws,
                requested_status=WorkspaceStatus.failed,
                reason_code=rc,
            )
            await s.commit()
            return
        superseded_claimed_runner = (
            self._monitor_owner_id is not None and ws.monitor_claimed_by != self._monitor_owner_id
        )
        # The inline initial handoff runs with no monitor claim
        # (``_monitor_owner_id`` is None) while the row sits unclaimed in
        # ``monitoring_pr`` — exactly the state ``claim_monitoring_pr`` can
        # immediately reclaim for a recovery monitor on another worker. Once a
        # recovery monitor has claimed it, ``monitor_claimed_by`` is no longer
        # None and this inline runner is the stale one. Mirror the
        # ``fence_unclaimed_monitor`` arm of the pause CAS here so the stale
        # inline runner does not clobber the takeover to ``failed``
        # (PRRT_kwDOSJAM6s6KTj4R).
        superseded_inline_handoff = (
            self._monitor_owner_id is None and ws.monitor_claimed_by is not None
        )
        if superseded_claimed_runner or superseded_inline_handoff:
            # A superseded monitor runner — its lease expired and a newer worker
            # reclaimed the row (``claim_monitoring_pr`` reassigns expired leases),
            # OR an inline initial handoff whose unclaimed row was taken over by a
            # recovery monitor — while this stale runner stayed alive must NOT
            # clobber the live claimant's workspace to ``failed``. The status guard
            # above misses this race because the takeover keeps the row in
            # ``monitoring_pr``; the protected-scope pause fence
            # (PRRT_kwDOSJAM6s6KHtX5 / PRRT_kwDOSJAM6s6KKmGo) already stops the
            # stale runner from PAUSING, but the fenced CAS-miss returns a TERMINAL
            # failed push result, so the loop would otherwise reach here and fail
            # the takeover. Mirror that owner fence at this terminal sink and
            # record an ignored terminal callback instead (PRRT_kwDOSJAM6s6KIep5,
            # PRRT_kwDOSJAM6s6KTj4R). A genuine inline handoff with no takeover
            # leaves ``monitor_claimed_by`` None and still fails normally.
            _log.warning(
                "monitor.terminal_failed_ignored_superseded_owner",
                workspace_id=workspace_id,
                reason_code=rc,
                monitor_owner_id=self._monitor_owner_id,
                monitor_claimed_by=ws.monitor_claimed_by,
            )
            await _record_ignored_monitor_terminal_callback(
                repo,
                ws,
                requested_status=WorkspaceStatus.failed,
                reason_code=rc,
            )
            await s.commit()
            return
        safe_message = redact_audit_text(message, limit=2000)
        ws.failure_reason = FailureReason.infrastructure_failure.value
        ws.failure_message = safe_message
        if rc == EXEC_PROCESS_CLEANUP_FAILED:
            await repo.add_event(
                ws,
                event_type="workspace.exec_process_cleanup_failed",
                reason_code=EXEC_PROCESS_CLEANUP_FAILED,
                payload={"message": safe_message[:1000]},
            )
        await repo.transition(ws, to=WorkspaceStatus.failed, reason_code=rc)
        await s.commit()
