"""Deferred Claude auth-overlay umount retry for terminal workspaces (issue #399).

Extracted from :mod:`awf.control.worker.cleanup` with behavior unchanged. The worker
is the only context that can unmount a per-workspace Claude auth overlay (it holds
``CAP_SYS_ADMIN`` and shares the agent container's mount namespace), so this module
owns the bounded inline umount retry and the deferred re-sweep that re-attempt a
persistent failure without ever blocking port/runtime reclaim.
"""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any

from sqlalchemy import func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from awf.control.worker.config import effective_worker_config_node_id
from awf.control.worker.constants import _TERMINAL_RELEASE_STATUSES
from awf.control.worker.logging import _log
from awf.control.worker.types import _TerminalRuntimeCandidate
from awf.db.enums import WorkspaceStatus
from awf.db.models import (
    ResourceReservation,
    Workspace,
    WorkspaceEvent,
)
from awf.db.repositories import WorkspaceRepository
from awf.db.repositories.base import (
    has_terminal_runtime_released_event,
    latest_terminal_runtime_release_event_order_expr,
    terminal_runtime_effectively_released_expr,
)
from awf.db.resilience import run_db_operation_with_retry

# --- Bounded retry for the worker-side Claude auth-overlay umount (issue #399) ---
#
# The worker is the only context that holds ``CAP_SYS_ADMIN`` and shares the agent
# container's mount namespace, so it is the only place the per-workspace Claude auth
# overlay can be unmounted. The capability-less API-container GC can only
# detect-and-skip. Historically the worker attempted the umount exactly once on the
# terminal-runtime-release pass; a transient *or* persistent failure then leaked the
# overlay mount + auth dir forever (the ``terminal_runtime_released`` event
# permanently excluded the workspace from future sweeps). Two bounded, complementary
# mechanisms close that gap without ever blocking port/runtime reclaim:
#
#   1. an immediate, bounded inline retry inside ``_teardown_terminal_auth_overlay``
#      for the common ultra-transient "target is busy" race, and
#   2. a deferred re-sweep, piggybacked on the existing terminal-runtime-release
#      scan, that re-attempts the umount on later cycles for persistent failures.
#
# Both are fixed-count and log every failed attempt (with its reason code, the
# ``umount(8)`` stderr, and the attempt index) so failures are never hidden behind
# blind retries. After the deferred bound, an ``exhausted`` marker is recorded and
# GC's loud-failure path remains the final backstop.
_TERMINAL_AUTH_OVERLAY_UNMOUNT_INLINE_ATTEMPTS = 3
"""Immediate (no-sleep) umount attempts on a single terminal-runtime-release pass."""

_TERMINAL_AUTH_OVERLAY_UNMOUNT_MAX_DEFERRED_SWEEPS = 5
"""Bound on deferred re-sweeps: once this many ``pending`` markers exist, give up loudly."""

_TERMINAL_AUTH_OVERLAY_UNMOUNT_FAILED_EVENT_TYPE = "worker.terminal_auth_overlay_unmount_failed"
"""Structured-log event emitted for each failed inline umount attempt."""

_TERMINAL_AUTH_OVERLAY_UNMOUNT_FAILED_REASON_CODE = "CLAUDE_AUTH_OVERLAY_UNMOUNT_FAILED"
"""Default reason code for an umount failure that does not carry its own ``reason_code``."""

_TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE = (
    "workspace.terminal_auth_overlay_unmount_pending"
)
"""Marker recorded when inline retries are exhausted; re-appended on each failed deferred sweep.

The *count* of these events for a workspace equals the number of umount attempts and
bounds the deferred re-sweeps (event-based counter, per "retries must preserve reason
codes, logs, and events").
"""

_TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE = "TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING"
"""Reason code paired with the ``terminal_auth_overlay_unmount_pending`` marker."""

_TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_EVENT_TYPE = (
    "workspace.terminal_auth_overlay_unmount_resolved"
)
"""Terminal marker: a later sweep unmounted the overlay (or found nothing to unmount)."""

_TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_REASON_CODE = "TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED"
"""Reason code paired with the ``terminal_auth_overlay_unmount_resolved`` marker."""

_TERMINAL_AUTH_OVERLAY_UNMOUNT_EXHAUSTED_EVENT_TYPE = (
    "workspace.terminal_auth_overlay_unmount_exhausted"
)
"""Terminal marker: the deferred-sweep bound was reached; GC's loud path is the backstop."""

_TERMINAL_AUTH_OVERLAY_UNMOUNT_EXHAUSTED_REASON_CODE = "TERMINAL_AUTH_OVERLAY_UNMOUNT_EXHAUSTED"
"""Reason code paired with the ``terminal_auth_overlay_unmount_exhausted`` marker."""

_TERMINAL_AUTH_OVERLAY_UNMOUNT_RETRY_SCAN_FAILED_EVENT_TYPE = (
    "worker.terminal_auth_overlay_unmount_retry_scan_failed"
)
"""Structured-log event for a deferred-sweep that failed without perturbing release."""

_TERMINAL_AUTH_OVERLAY_UNMOUNT_RETRY_SCAN_FAILED_REASON_CODE = (
    "TERMINAL_AUTH_OVERLAY_UNMOUNT_RETRY_SCAN_FAILED"
)
"""Reason code for a scan-level deferred overlay-umount failure.

Kept distinct from ``TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING`` (a normal lifecycle marker) so
operators filtering structured logs on this reason code see only error events, not markers.
"""

_TERMINAL_AUTH_OVERLAY_UNMOUNT_RETRY_FAILED_EVENT_TYPE = (
    "worker.terminal_auth_overlay_unmount_retry_failed"
)
"""Structured-log event for a per-candidate deferred-sweep failure (does not abort the scan)."""

_TERMINAL_AUTH_OVERLAY_UNMOUNT_RETRY_FAILED_REASON_CODE = (
    "TERMINAL_AUTH_OVERLAY_UNMOUNT_RETRY_FAILED"
)
"""Reason code for a per-candidate deferred overlay-umount failure.

Kept distinct from ``TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING`` (a normal lifecycle marker) so
operators filtering structured logs on this reason code see only error events, not markers.
"""

_TERMINAL_AUTH_OVERLAY_UNMOUNT_RELEASE_REVOKED_EVENT_TYPE = (
    "worker.terminal_auth_overlay_unmount_release_revoked"
)
"""Structured-log event for a deferred-sweep marker write skipped because the
terminal-runtime release was revoked between candidate listing and the write."""

_TERMINAL_AUTH_OVERLAY_UNMOUNT_RELEASE_REVOKED_REASON_CODE = (
    "TERMINAL_AUTH_OVERLAY_UNMOUNT_RELEASE_REVOKED"
)
"""Reason code for a deferred overlay-umount marker write skipped under the row lock.

The candidate query gates on *effective release*, but the provisioner can write a
``terminal_runtime_release_revoked`` event (also under ``get_for_update``) after the
candidate is listed and before the deferred retry's marker write runs. Writing a
``resolved``/``exhausted`` marker (or burning an attempt via a fresh ``pending``) in that
window would suppress the umount retry still owed once the runtime is genuinely released,
leaking the overlay mount. The write is skipped under the row lock and surfaced with this
reason code so the deferred retry stays owed and the skip stays diagnosable.
"""


async def _teardown_terminal_auth_overlay(
    self: Any,
    candidate: _TerminalRuntimeCandidate,
) -> bool | None:
    """Unmount a terminal workspace's Claude auth overlay in the worker namespace.

    Returns a three-valued unmount outcome for the release audit:

    - ``True``  — the overlay teardown ran and succeeded.
    - ``False`` — teardown was attempted but failed (logged); the release still
      proceeds so port reclaim is not blocked and GC's loud-failure path is the
      backstop for any residual.
    - ``None``  — not applicable: no auth-overlay work dir is wired (a
      copy-fallback workspace never provisioned an overlay), so there was
      nothing to unmount. Distinguishing this from ``False`` keeps log-based
      alerting from conflating a healthy no-overlay workspace with a real
      umount error.

    The worker is the only context that can see the per-workspace overlay mount,
    so it releases it on the terminal-runtime-release sweep — before GC, running
    capability-less in the API container, would otherwise fail loudly trying to
    remove a still-mounted auth dir.

    A worker downgraded from ``CAP_SYS_ADMIN`` (overlay capable → copy fallback)
    may still hold surviving overlay ``upper`` dirs from a capable past life; in
    that state ``teardown_workspace_auth_overlay`` raises the capability-less
    ``OverlayUnmountUnverifiableError`` (a ``RuntimeError``, not an ``OSError``).
    That must also degrade to ``False`` here, never escape and abort the sweep.
    """

    work_dir = getattr(self, "_auth_overlay_work_dir", None)
    if work_dir is None:
        return None

    from awf.node.auth_mounts import (
        OverlayUnmountUnverifiableError,
        teardown_workspace_auth_overlay,
    )

    # Bounded, immediate (no-sleep) retry loop. The common failure is an
    # ultra-transient "target is busy" race right after the compose stack came
    # down; an immediate re-attempt usually wins. Each failed attempt is logged
    # with its reason code, the ``umount(8)`` stderr, and the attempt index, so
    # this never hides a failure behind a blind retry. Success short-circuits to
    # ``True``; exhaustion returns ``False`` exactly as the single attempt did,
    # so port reclaim is never blocked and the deferred re-sweep / GC backstops
    # still cover a persistent failure.
    for attempt in range(1, _TERMINAL_AUTH_OVERLAY_UNMOUNT_INLINE_ATTEMPTS + 1):
        try:
            await asyncio.to_thread(
                teardown_workspace_auth_overlay,
                work_dir=work_dir,
                workspace_id=candidate.workspace_id,
            )
        except (OverlayUnmountUnverifiableError, OSError, subprocess.SubprocessError) as exc:
            _log.warning(
                _TERMINAL_AUTH_OVERLAY_UNMOUNT_FAILED_EVENT_TYPE,
                workspace_id=candidate.workspace_id,
                status=candidate.status.value,
                compose_project_name=candidate.compose_project_name,
                reason_code=getattr(
                    exc, "reason_code", _TERMINAL_AUTH_OVERLAY_UNMOUNT_FAILED_REASON_CODE
                ),
                error=repr(exc)[:400],
                # ``repr(CalledProcessError)`` drops the ``umount(8)`` stderr (e.g.
                # "target is busy"); forward it so the EBUSY root cause is greppable.
                stderr=getattr(exc, "stderr", None),
                attempt=attempt,
            )
            continue
        return True
    return False


async def _retry_pending_terminal_auth_overlay_unmounts(
    self: Any,
    *,
    limit: int | None = None,
) -> None:
    """Re-attempt the worker-side Claude auth-overlay umount for pending workspaces.

    Piggybacked on the terminal-runtime-release scan. For each terminal workspace
    that still carries an unresolved ``pending`` umount marker, re-run the bounded
    overlay teardown. A per-candidate failure is routed to a dedicated handler
    (mirroring the resume-pending scan) rather than aborting the whole sweep, and
    ``CancelledError`` is re-raised immediately so cooperative cancellation is never
    masked. The runtime/port was already reclaimed on the original release pass, so
    nothing here can block reclaim — it only chips away at a residual leaked mount.
    """
    candidates = await self._list_pending_terminal_auth_overlay_unmount_candidates(limit=limit)
    for candidate in candidates:
        try:
            await self._retry_pending_terminal_auth_overlay_unmount_for_candidate(candidate)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await _handle_terminal_auth_overlay_unmount_retry_failure(
                self,
                candidate=candidate,
                exc=exc,
            )


async def _retry_pending_terminal_auth_overlay_unmount_for_candidate(
    self: Any,
    candidate: _TerminalRuntimeCandidate,
) -> None:
    """Re-attempt one workspace's overlay umount and record the deferred-sweep outcome.

    - umount now succeeds (``True``) or there is nothing to unmount (``None``) →
      record a terminal ``resolved`` marker; the workspace drops out of the
      deferred-candidate set.
    - umount still fails (``False``) → if the bounded number of ``pending`` markers
      has been reached, record a terminal ``exhausted`` marker (GC's loud-failure
      path remains the backstop); otherwise append a new ``pending`` marker with an
      incremented attempt index so a later sweep tries again.
    """
    auth_overlay_unmounted = await _teardown_terminal_auth_overlay(self, candidate)
    if auth_overlay_unmounted is not False:
        await self._record_terminal_auth_overlay_unmount_resolved(
            candidate,
            auth_overlay_unmounted=auth_overlay_unmounted,
        )
        return

    pending_attempts = await self._count_terminal_auth_overlay_unmount_pending_events(
        candidate.workspace_id
    )
    if pending_attempts >= _TERMINAL_AUTH_OVERLAY_UNMOUNT_MAX_DEFERRED_SWEEPS:
        await self._record_terminal_auth_overlay_unmount_exhausted(
            candidate,
            attempts=pending_attempts,
        )
        return
    await self._append_terminal_auth_overlay_unmount_pending(
        candidate,
        attempt=pending_attempts + 1,
    )


async def _handle_terminal_auth_overlay_unmount_retry_failure(
    self: Any,
    *,
    candidate: _TerminalRuntimeCandidate,
    exc: Exception,
) -> None:
    """Log a per-candidate deferred-sweep failure without aborting the scan.

    The broad ``except Exception`` upstream is deliberate (one candidate must not
    abort the whole sweep), so the traceback is preserved via ``exc_info`` to keep
    the unexpected failure fully diagnosable rather than masked behind the
    truncated ``error`` string.
    """
    _ = self
    _log.warning(
        _TERMINAL_AUTH_OVERLAY_UNMOUNT_RETRY_FAILED_EVENT_TYPE,
        workspace_id=candidate.workspace_id,
        status=candidate.status.value,
        compose_project_name=candidate.compose_project_name,
        reason_code=_TERMINAL_AUTH_OVERLAY_UNMOUNT_RETRY_FAILED_REASON_CODE,
        error_type=type(exc).__name__,
        error=str(exc)[:240],
        exc_info=True,
    )


async def _list_pending_terminal_auth_overlay_unmount_candidates(
    self: Any,
    *,
    limit: int | None = None,
) -> list[_TerminalRuntimeCandidate]:
    """Return terminal workspaces with an unresolved pending overlay-umount marker.

    The marker predicate is intentionally event-type existence/count only (no JSONB
    value comparison), so it behaves identically on Postgres (prod) and SQLite
    (tests): a terminal-status workspace owned by this node — by ``Workspace.node_id``
    or, for legacy unstamped rows, by the active/latest ``ResourceReservation`` node
    (the same effective-node reservation fallback
    ``_list_terminal_released_pending_planning_scope_auto_retry_candidates`` uses), only
    falling back to ``node_id IS NULL`` when no reservation exists — with a non-empty
    ``repo_url``, at least one ``pending`` marker, and no ``resolved`` and no
    ``exhausted`` marker.

    All three marker predicates are scoped to the *current* effective-release cycle —
    events at or after the latest ``terminal_runtime_released`` ``event_order`` (the
    ``latest_terminal_runtime_release_event_order_expr`` floor). Markers are append-only
    and workspace-lifetime, so without this scope a workspace that was revoked and later
    genuinely re-released would (a) stay excluded forever by a stale earlier-cycle
    ``resolved``/``exhausted`` marker even though the new cycle's overlay is still mounted,
    and (b) carry an inflated ``pending`` count toward the bounded sweep. Scoping resets
    the marker view per release cycle so each genuine release gets its full deferred-sweep
    budget (issue #399).

    Candidates are additionally gated on the *effective-release* predicate (the same
    ``terminal_runtime_effectively_released_expr`` the terminal-runtime candidate/resume
    paths use, here in its positive form). A ``pending`` marker is always co-written
    with ``terminal_runtime_released``, but that release can later be superseded by
    ``terminal_runtime_release_revoked`` when orphan containers could not be stopped —
    in which case the agent container still holds the overlay bind. Re-attempting the
    umount then is futile: it would burn the bounded deferred sweeps and record a
    terminal ``exhausted``/``resolved`` marker that permanently suppresses the retry
    owed once the runtime is *actually* released later. Gating on effective release
    keeps the workspace out of the sweep until the bind is genuinely gone.
    """
    if limit is not None and limit <= 0:
        return []
    terminal_status_values = [status.value for status in _TERMINAL_RELEASE_STATUSES]
    effectively_released = terminal_runtime_effectively_released_expr(
        correlated_to=Workspace,
    )
    worker_node_id = effective_worker_config_node_id(self._config)
    # Derive an effective owning node from reservations, mirroring
    # ``_list_terminal_released_pending_planning_scope_auto_retry_candidates``.
    # A plain ``node_id IS NULL`` fallback would admit a legacy unstamped row on
    # *every* worker; a non-owning worker would then run the overlay teardown
    # against its own namespace, see no mount/upper, and record a terminal
    # ``resolved`` marker — permanently suppressing the owning worker's deferred
    # retry while the overlay stays leaked on the real node. Coalescing onto the
    # active/latest reservation node keeps the deferred sweep on the reservation
    # owner; only a row with no reservation at all still falls back to NULL.
    active_reservation_node = (
        select(ResourceReservation.node_id)
        .where(ResourceReservation.workspace_id == Workspace.id)
        .where(ResourceReservation.released_at.is_(None))
        .order_by(ResourceReservation.reserved_at.desc(), ResourceReservation.id.desc())
        .limit(1)
        .correlate(Workspace)
        .scalar_subquery()
    )
    latest_reservation_node = (
        select(ResourceReservation.node_id)
        .where(ResourceReservation.workspace_id == Workspace.id)
        .order_by(ResourceReservation.reserved_at.desc(), ResourceReservation.id.desc())
        .limit(1)
        .correlate(Workspace)
        .scalar_subquery()
    )
    effective_node = func.coalesce(
        Workspace.node_id,
        active_reservation_node,
        latest_reservation_node,
    )
    # Scope every overlay marker to the *current* effective-release cycle. Markers are
    # append-only and workspace-lifetime, but a release that was revoked and later
    # genuinely re-released opens a new cycle; a ``resolved``/``exhausted`` (or a stale
    # ``pending``) from an earlier cycle must not leak into this one. ``event_order`` is
    # per-workspace monotonic and reserved for every marker, so markers at or after the
    # latest ``terminal_runtime_released`` order belong to the current cycle. ``coalesce``
    # to ``-1`` keeps the lifetime predicate when no release event exists yet (no floor).
    release_cycle_floor = func.coalesce(
        latest_terminal_runtime_release_event_order_expr(correlated_to=Workspace),
        literal(-1),
    )
    pending_exists = (
        select(WorkspaceEvent.id)
        .where(WorkspaceEvent.workspace_id == Workspace.id)
        .where(WorkspaceEvent.event_type == _TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE)
        .where(WorkspaceEvent.event_order >= release_cycle_floor)
        .correlate(Workspace)
        .exists()
    )
    terminal_marker_exists = (
        select(WorkspaceEvent.id)
        .where(WorkspaceEvent.workspace_id == Workspace.id)
        .where(
            WorkspaceEvent.event_type.in_(
                (
                    _TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_EVENT_TYPE,
                    _TERMINAL_AUTH_OVERLAY_UNMOUNT_EXHAUSTED_EVENT_TYPE,
                )
            )
        )
        .where(WorkspaceEvent.event_order >= release_cycle_floor)
        .correlate(Workspace)
        .exists()
    )
    stmt = (
        select(
            Workspace.id,
            Workspace.status,
            Workspace.repo_url,
            Workspace.compose_project_name,
            Workspace.compose_file_path,
        )
        .where(Workspace.status.in_(terminal_status_values))
        .where(
            or_(
                effective_node == worker_node_id,
                # ``coalesce(...) IS NULL`` holds iff every coalesced arg is NULL,
                # so this captures the legacy "no node_id and no reservation"
                # fallback without re-inlining the reservation subqueries.
                effective_node.is_(None),
            )
        )
        .where(pending_exists)
        .where(~terminal_marker_exists)
        .where(effectively_released)
        .order_by(Workspace.updated_at.asc(), Workspace.id.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    async def _operation(session: AsyncSession) -> list[Any]:
        result = await session.execute(stmt)
        return list(result.all())

    rows = await run_db_operation_with_retry(
        self._session_factory,
        _operation,
        on_retry=self._log_transient_db_retry,
    )

    candidates: list[_TerminalRuntimeCandidate] = []
    for workspace_id, status_val, repo_url, compose_project_name, compose_file_path in rows:
        if not repo_url:
            continue
        candidates.append(
            _TerminalRuntimeCandidate(
                workspace_id=workspace_id,
                status=WorkspaceStatus(status_val),
                repo_url=repo_url,
                compose_project_name=compose_project_name,
                compose_file_path=compose_file_path,
            )
        )
    return candidates


async def _count_terminal_auth_overlay_unmount_pending_events(
    self: Any,
    workspace_id: str,
) -> int:
    """Return the number of current-cycle ``pending`` overlay-umount markers for *workspace_id*.

    The count equals the number of umount attempts recorded so far and bounds the
    deferred re-sweeps (event-based counter). It is scoped to the current
    effective-release cycle (markers at or after the latest ``terminal_runtime_released``
    order) so that ``pending`` markers co-written with an earlier release that was later
    revoked do not inflate the count into a premature ``exhausted`` for the current
    release (issue #399).
    """

    async def _operation(session: AsyncSession) -> int:
        release_cycle_floor = func.coalesce(
            latest_terminal_runtime_release_event_order_expr(workspace_id=workspace_id),
            literal(-1),
        )
        stmt = (
            select(func.count())
            .select_from(WorkspaceEvent)
            .where(WorkspaceEvent.workspace_id == workspace_id)
            .where(WorkspaceEvent.event_type == _TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE)
            .where(WorkspaceEvent.event_order >= release_cycle_floor)
        )
        return int((await session.execute(stmt)).scalar_one())

    return await run_db_operation_with_retry(
        self._session_factory,
        _operation,
        on_retry=self._log_transient_db_retry,
    )


async def _has_terminal_auth_overlay_unmount_terminal_event(
    self: Any,
    session: AsyncSession,
    workspace_id: str,
) -> bool:
    """Return True if a current-cycle ``resolved`` or ``exhausted`` overlay-umount marker exists.

    Scoped to the current effective-release cycle (markers at or after the latest
    ``terminal_runtime_released`` order) so an earlier cycle's terminal marker neither
    suppresses a fresh ``resolved``/``exhausted`` write nor — paired with the equally
    scoped candidate query — wedges the sweep into re-listing a workspace it can never
    resolve in the current cycle (issue #399).
    """
    _ = self
    release_cycle_floor = func.coalesce(
        latest_terminal_runtime_release_event_order_expr(workspace_id=workspace_id),
        literal(-1),
    )
    stmt = (
        select(WorkspaceEvent.id)
        .where(WorkspaceEvent.workspace_id == workspace_id)
        .where(
            WorkspaceEvent.event_type.in_(
                (
                    _TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_EVENT_TYPE,
                    _TERMINAL_AUTH_OVERLAY_UNMOUNT_EXHAUSTED_EVENT_TYPE,
                )
            )
        )
        .where(WorkspaceEvent.event_order >= release_cycle_floor)
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


def _log_terminal_auth_overlay_unmount_release_revoked(
    candidate: _TerminalRuntimeCandidate,
    *,
    marker: str,
) -> None:
    """Surface a deferred-sweep marker write that was skipped because release was revoked.

    The skip keeps the umount retry owed once the runtime is genuinely released; logging
    it (rather than returning silently like the dedup/missing-row skips) keeps the
    revoke-race observable under its own reason code.
    """
    _log.warning(
        _TERMINAL_AUTH_OVERLAY_UNMOUNT_RELEASE_REVOKED_EVENT_TYPE,
        workspace_id=candidate.workspace_id,
        status=candidate.status.value,
        compose_project_name=candidate.compose_project_name,
        reason_code=_TERMINAL_AUTH_OVERLAY_UNMOUNT_RELEASE_REVOKED_REASON_CODE,
        marker=marker,
    )


async def _record_terminal_auth_overlay_unmount_resolved(
    self: Any,
    candidate: _TerminalRuntimeCandidate,
    *,
    auth_overlay_unmounted: bool | None,
) -> None:
    """Record a terminal ``resolved`` marker after a deferred sweep clears the overlay."""
    payload = {
        "compose_project_name": candidate.compose_project_name,
        "workspace_status": candidate.status.value,
        "auth_overlay_unmounted": auth_overlay_unmounted,
    }

    async def _operation(session: AsyncSession) -> str:
        repo = WorkspaceRepository(session)
        # ``SELECT FOR UPDATE SKIP LOCKED`` + the terminal-marker guard make the
        # write idempotent under the NULL-``node_id`` multi-worker race: the loser
        # exits without double-writing a ``resolved``/``exhausted`` pair.
        ws = await repo.get_for_update(candidate.workspace_id, skip_locked=True)
        if ws is None:
            return "skipped"
        if await self._has_terminal_auth_overlay_unmount_terminal_event(
            session, candidate.workspace_id
        ):
            return "skipped"
        # Re-check effective release under the same row lock the provisioner takes to
        # write ``terminal_runtime_release_revoked``. The candidate query gated on
        # effective release, but a revoke can land between listing and this write; a
        # ``resolved`` marker recorded then would permanently suppress the umount retry
        # still owed once the runtime is genuinely released, leaking the overlay mount.
        if not await has_terminal_runtime_released_event(session, candidate.workspace_id):
            return "revoked"
        await repo.add_event(
            ws,
            event_type=_TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_EVENT_TYPE,
            reason_code=_TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_REASON_CODE,
            payload=payload,
        )
        return "recorded"

    outcome = await run_db_operation_with_retry(
        self._session_factory,
        _operation,
        commit=True,
        on_retry=self._log_transient_db_retry,
    )
    if outcome == "revoked":
        _log_terminal_auth_overlay_unmount_release_revoked(candidate, marker="resolved")
        return
    if outcome != "recorded":
        return
    _log.info(
        _TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_EVENT_TYPE,
        workspace_id=candidate.workspace_id,
        status=candidate.status.value,
        compose_project_name=candidate.compose_project_name,
        reason_code=_TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_REASON_CODE,
        auth_overlay_unmounted=auth_overlay_unmounted,
    )


async def _record_terminal_auth_overlay_unmount_exhausted(
    self: Any,
    candidate: _TerminalRuntimeCandidate,
    *,
    attempts: int,
) -> None:
    """Record a terminal ``exhausted`` marker once the deferred-sweep bound is reached.

    This is a loud (``error``-level), visible give-up — not a silent cap. GC's
    capability-less loud-failure path (``CLAUDE_AUTH_OVERLAY_UNMOUNT_INCAPABLE``)
    remains the final backstop for the residual mount.
    """
    payload = {
        "compose_project_name": candidate.compose_project_name,
        "workspace_status": candidate.status.value,
        "attempts": attempts,
    }

    async def _operation(session: AsyncSession) -> str:
        repo = WorkspaceRepository(session)
        ws = await repo.get_for_update(candidate.workspace_id, skip_locked=True)
        if ws is None:
            return "skipped"
        if await self._has_terminal_auth_overlay_unmount_terminal_event(
            session, candidate.workspace_id
        ):
            return "skipped"
        # Re-check effective release under the row lock (see the matching note in
        # ``_record_terminal_auth_overlay_unmount_resolved``): an ``exhausted`` marker
        # written during a post-listing revoke window would permanently suppress the
        # retry still owed once the runtime is genuinely released.
        if not await has_terminal_runtime_released_event(session, candidate.workspace_id):
            return "revoked"
        await repo.add_event(
            ws,
            event_type=_TERMINAL_AUTH_OVERLAY_UNMOUNT_EXHAUSTED_EVENT_TYPE,
            reason_code=_TERMINAL_AUTH_OVERLAY_UNMOUNT_EXHAUSTED_REASON_CODE,
            payload=payload,
        )
        return "recorded"

    outcome = await run_db_operation_with_retry(
        self._session_factory,
        _operation,
        commit=True,
        on_retry=self._log_transient_db_retry,
    )
    if outcome == "revoked":
        _log_terminal_auth_overlay_unmount_release_revoked(candidate, marker="exhausted")
        return
    if outcome != "recorded":
        return
    _log.error(
        _TERMINAL_AUTH_OVERLAY_UNMOUNT_EXHAUSTED_EVENT_TYPE,
        workspace_id=candidate.workspace_id,
        status=candidate.status.value,
        compose_project_name=candidate.compose_project_name,
        reason_code=_TERMINAL_AUTH_OVERLAY_UNMOUNT_EXHAUSTED_REASON_CODE,
        attempts=attempts,
    )


async def _append_terminal_auth_overlay_unmount_pending(
    self: Any,
    candidate: _TerminalRuntimeCandidate,
    *,
    attempt: int,
) -> None:
    """Append a fresh ``pending`` marker after a deferred sweep still failed to unmount."""
    payload = {
        "compose_project_name": candidate.compose_project_name,
        "workspace_status": candidate.status.value,
        "attempt": attempt,
    }

    async def _operation(session: AsyncSession) -> str:
        repo = WorkspaceRepository(session)
        ws = await repo.get_for_update(candidate.workspace_id, skip_locked=True)
        if ws is None:
            return "skipped"
        # If a concurrent worker already resolved/exhausted this workspace, do not
        # append a new ``pending`` that would resurrect it as a deferred candidate.
        if await self._has_terminal_auth_overlay_unmount_terminal_event(
            session, candidate.workspace_id
        ):
            return "skipped"
        # Re-check effective release under the row lock (see the matching note in
        # ``_record_terminal_auth_overlay_unmount_resolved``): appending a fresh
        # ``pending`` during a post-listing revoke window burns one of the bounded
        # deferred attempts on a futile umount (the overlay bind is held again), pushing
        # the workspace toward a premature ``exhausted`` once it is genuinely released.
        if not await has_terminal_runtime_released_event(session, candidate.workspace_id):
            return "revoked"
        await repo.add_event(
            ws,
            event_type=_TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
            reason_code=_TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE,
            payload=payload,
        )
        return "recorded"

    outcome = await run_db_operation_with_retry(
        self._session_factory,
        _operation,
        commit=True,
        on_retry=self._log_transient_db_retry,
    )
    if outcome == "revoked":
        _log_terminal_auth_overlay_unmount_release_revoked(candidate, marker="pending")
        return
    if outcome != "recorded":
        return
    _log.warning(
        _TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
        workspace_id=candidate.workspace_id,
        status=candidate.status.value,
        compose_project_name=candidate.compose_project_name,
        reason_code=_TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE,
        attempt=attempt,
    )
