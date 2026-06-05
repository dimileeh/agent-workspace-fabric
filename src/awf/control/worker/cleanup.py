"""Extracted ControlWorker domain operations.

This module contains mechanically moved methods from ``awf.control.worker.manager`` and keeps behavior unchanged.
"""

from __future__ import annotations

import asyncio as asyncio
import contextlib as contextlib
import hashlib as hashlib
import json as json
import re as re
import subprocess as subprocess
import uuid as uuid
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.orm.attributes import flag_modified

from awf.control.executor.planning_ops import (
    _PLANNING_SCOPE_AUTO_RETRY_PENDING_TERMINAL_RELEASE_EVENT_TYPES,
    _PLANNING_SCOPE_AUTO_RETRY_RESUME_FAILED_REASON_CODE,
    _PLANNING_SCOPE_AUTO_RETRY_TERMINAL_RELEASE_EVENTS,
    _TERMINAL_RUNTIME_RELEASE_RETRY_AFTER,
    _WORKSPACE_RETRY_REQUESTED_EVENT_TYPE,
    _record_planning_scope_auto_retry_resume_failed_after_runtime_release,
    _resume_blocked_planning_scope_auto_retry_after_runtime_release,
)
from awf.control.worker.config import (
    effective_worker_config_node_id,
)
from awf.control.worker.constants import (
    _CLASSIFIED_ORPHAN_REAP_FAILED_REASON_CODE,
    _ORPHAN_DIR_RECONCILE_FAILED_REASON_CODE,
    _TERMINAL_RELEASE_STATUSES,
    _TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
    _TERMINAL_RUNTIME_RELEASE_FAILED_EVENT_TYPE,
    _TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE,
    _TERMINAL_RUNTIME_RELEASE_REASON_CODE,
)
from awf.control.worker.helpers import (
    _worker_exception_is_transient_db_connection,
)
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
    terminal_runtime_effectively_released_expr,
)
from awf.db.resilience import (
    DB_CONNECTION_CLOSED_REASON,
    run_db_operation_with_retry,
)
from awf.db.session import (
    session_scope,
)
from awf.node.cleanup import WorkspaceCleanupResult
from awf.runtime.planning import AGENT_PLAN_PHASE_SCOPE_VIOLATION
from awf.service.failure_causality import (
    attach_primary_failure,
    load_primary_failure_snapshot,
)
from awf.service.secret_leases import (
    SecretLeaseService,
)

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

_TERMINAL_AUTH_OVERLAY_UNMOUNT_RETRY_FAILED_EVENT_TYPE = (
    "worker.terminal_auth_overlay_unmount_retry_failed"
)
"""Structured-log event for a per-candidate deferred-sweep failure (does not abort the scan)."""


async def _maybe_expire_due_secret_leases(self: Any) -> None:
    """Periodically expire due secret leases, respecting the scan interval."""
    now = monotonic()
    if now < self._next_secret_lease_expiration_scan_at:
        return

    try:
        await self._expire_due_secret_leases()
    except Exception as exc:
        if _worker_exception_is_transient_db_connection(exc):
            interval = max(0.0, self._config.secret_lease_expiration_scan_interval_seconds)
            self._next_secret_lease_expiration_scan_at = monotonic() + interval
            _log.warning(
                "worker.secret_lease_expiration_db_connection_closed",
                reason_code=DB_CONNECTION_CLOSED_REASON,
                error_type=type(exc).__name__,
                error=str(exc)[:240],
            )
            return
        _log.exception(
            "worker.secret_lease_expiration_failed",
            reason_code="SECRET_LEASE_EXPIRATION_FAILED",
        )
        raise

    interval = max(0.0, self._config.secret_lease_expiration_scan_interval_seconds)
    self._next_secret_lease_expiration_scan_at = monotonic() + interval


async def _expire_due_secret_leases(self: Any) -> None:
    """Expire due secret leases and log the count of leases expired."""
    async with session_scope(self._session_factory) as session:
        expired = await SecretLeaseService(session).expire_due_secret_leases()
        expired_count = len(expired)
        workspace_ids = sorted({lease.workspace_id for lease in expired})

    if expired_count:
        _log.info(
            "worker.secret_leases_expired",
            reason_code="SECRET_LEASES_EXPIRED",
            expired_count=expired_count,
            workspace_ids=workspace_ids,
        )


async def _maybe_release_terminal_runtime(self: Any) -> None:
    """Periodically release terminal-runtime resources for stopped workspaces."""
    now = monotonic()
    if now < self._next_terminal_runtime_release_scan_at:
        return

    try:
        await self._release_terminal_runtime_resources()
    except Exception as exc:
        if _worker_exception_is_transient_db_connection(exc):
            _log.warning(
                "worker.terminal_runtime_release_db_connection_closed",
                reason_code=DB_CONNECTION_CLOSED_REASON,
                error_type=type(exc).__name__,
                error=str(exc)[:240],
            )
        else:
            _log.exception(
                "worker.terminal_runtime_release_failed",
                reason_code=_TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE,
                error_type=type(exc).__name__,
            )
        interval = max(0.0, self._config.terminal_runtime_release_scan_interval_seconds)
        self._next_terminal_runtime_release_scan_at = monotonic() + interval
        return

    interval = max(0.0, self._config.terminal_runtime_release_scan_interval_seconds)
    self._next_terminal_runtime_release_scan_at = monotonic() + interval


async def _maybe_reconcile_orphan_dirs(self: Any) -> None:
    """Periodically reap orphaned per-workspace dirs whose DB row is gone (WS-B2).

    No-op when no reconciler callback is wired. The callback already decides
    report-only vs execute based on the ``auto_cleanup_orphans`` flag; this
    method handles interval gating and transient-DB resilience. Like
    :func:`_maybe_release_terminal_runtime`, all reconcile failures are
    swallowed-and-rescheduled rather than propagated: non-transient errors
    are logged loudly via ``_log.exception`` (ERROR level + traceback) so they
    stay operator-visible, but they are not re-raised. Re-raising would skip
    all workspace provisioning/dispatch for the iteration and surface through
    ``run_forever``'s last-resort ``run_once_failed`` handler as a second,
    context-free log entry that can fire on-call alerts for an
    already-handled, already-rescheduled event. The cursor is always
    rescheduled so a failing sweep cannot hot-loop ``run_once``.
    """
    if self._orphan_dir_reconciler is None:
        return

    now = monotonic()
    if now < self._next_orphan_reconcile_scan_at:
        return

    try:
        await self._orphan_dir_reconciler()
    except Exception as exc:
        if _worker_exception_is_transient_db_connection(exc):
            _log.warning(
                "worker.orphan_dir_reconcile_db_connection_closed",
                reason_code=DB_CONNECTION_CLOSED_REASON,
                error_type=type(exc).__name__,
                error=str(exc)[:240],
            )
        else:
            _log.exception(
                "worker.orphan_dir_reconcile_failed",
                reason_code=_ORPHAN_DIR_RECONCILE_FAILED_REASON_CODE,
                error_type=type(exc).__name__,
            )
        interval = max(0.0, self._config.orphan_reconcile_scan_interval_seconds)
        self._next_orphan_reconcile_scan_at = monotonic() + interval
        return

    interval = max(0.0, self._config.orphan_reconcile_scan_interval_seconds)
    self._next_orphan_reconcile_scan_at = monotonic() + interval


async def _maybe_reap_classified_orphans(self: Any) -> None:
    """Periodically reap classified orphan Docker resources and worktrees.

    Unlike the orphan-directory reconciler callback, this loop gates callback
    execution directly on ``auto_cleanup_orphans`` so the worker does not build
    Docker/worktree inventories while destructive cleanup is disabled. Failures
    are logged and rescheduled, matching ``_maybe_reconcile_orphan_dirs`` so one
    failed sweep cannot block provisioning or dispatch.
    """
    if self._classified_orphan_reaper is None:
        return
    if not self._config.auto_cleanup_orphans:
        return

    now = monotonic()
    if now < self._next_classified_orphan_reap_scan_at:
        return

    try:
        await self._classified_orphan_reaper()
    except Exception as exc:
        if _worker_exception_is_transient_db_connection(exc):
            _log.warning(
                "worker.classified_orphan_reap_db_connection_closed",
                reason_code=DB_CONNECTION_CLOSED_REASON,
                error_type=type(exc).__name__,
                error=str(exc)[:240],
            )
        else:
            _log.exception(
                "worker.classified_orphan_reap_failed",
                reason_code=_CLASSIFIED_ORPHAN_REAP_FAILED_REASON_CODE,
                error_type=type(exc).__name__,
            )

    interval = max(0.0, self._config.classified_orphan_reap_scan_interval_seconds)
    self._next_classified_orphan_reap_scan_at = monotonic() + interval


async def _release_terminal_runtime_resources(self: Any) -> None:
    """Run the terminal-runtime cleaner for all eligible candidates, raising on first failure."""
    limit = self._config.terminal_runtime_release_max_per_scan
    if limit is not None and limit <= 0:
        return
    if self._runtime_cleaner is None:
        return

    release_errors: list[Exception] = []
    candidates = await self._list_terminal_runtime_candidates(limit=limit)
    for candidate in candidates:
        try:
            await self._release_terminal_runtime_for_candidate(candidate)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _worker_exception_is_transient_db_connection(exc):
                _log.warning(
                    "worker.terminal_runtime_release_candidate_db_connection_closed",
                    workspace_id=candidate.workspace_id,
                    status=candidate.status.value,
                    compose_project_name=candidate.compose_project_name,
                    reason_code=DB_CONNECTION_CLOSED_REASON,
                    error_type=type(exc).__name__,
                    error=str(exc)[:240],
                )
            else:
                _log.exception(
                    "worker.terminal_runtime_release_candidate_failed",
                    workspace_id=candidate.workspace_id,
                    status=candidate.status.value,
                    compose_project_name=candidate.compose_project_name,
                    reason_code=_TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE,
                    error_type=type(exc).__name__,
                    error=str(exc)[:240],
                )
            release_errors.append(exc)
    try:
        await self._resume_pending_planning_scope_auto_retries_after_terminal_release(limit=limit)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if not release_errors:
            raise
        _log.warning(
            "worker.terminal_runtime_release_resume_scan_failed_after_release_error",
            reason_code=_PLANNING_SCOPE_AUTO_RETRY_RESUME_FAILED_REASON_CODE,
            error_type=type(exc).__name__,
            error=str(exc)[:240],
        )
    # Piggyback the deferred Claude auth-overlay umount re-sweep on the same scan
    # (mirrors the resume-pending scan above). It is fully guarded: a failure here
    # is swallowed-and-logged so it never perturbs the release-error aggregation or
    # the final re-raise below, and ``CancelledError`` still propagates promptly.
    try:
        await self._retry_pending_terminal_auth_overlay_unmounts(limit=limit)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning(
            _TERMINAL_AUTH_OVERLAY_UNMOUNT_RETRY_SCAN_FAILED_EVENT_TYPE,
            reason_code=_TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE,
            error_type=type(exc).__name__,
            error=str(exc)[:240],
        )
    if len(release_errors) == 1:
        raise release_errors[0]
    if release_errors:
        raise ExceptionGroup(
            "terminal runtime release failed",
            release_errors,
        )


async def _resume_pending_planning_scope_auto_retries_after_terminal_release(
    self: Any,
    *,
    limit: int | None = None,
) -> None:
    """Resume planning-scope auto-retries whose source runtime is released.

    If a third-party workspace now holds the requested host port, the resume
    attempt records a deduplicated host-port block and remains a candidate. The
    next cleanup scans intentionally keep rechecking until the port is free.
    """
    candidates = await self._list_terminal_released_pending_planning_scope_auto_retry_candidates(
        limit=limit,
    )
    for candidate in candidates:
        try:
            await _resume_blocked_planning_scope_auto_retry_after_runtime_release(
                self,
                workspace_id=candidate.workspace_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await _handle_planning_scope_auto_retry_resume_failure(
                self,
                workspace_id=candidate.workspace_id,
                status=candidate.status.value,
                compose_project_name=candidate.compose_project_name,
                exc=exc,
            )


async def _list_terminal_released_pending_planning_scope_auto_retry_candidates(
    self: Any,
    *,
    limit: int | None = None,
) -> list[_TerminalRuntimeCandidate]:
    if limit is not None and limit <= 0:
        return []
    latest_planning_event = (
        select(
            WorkspaceEvent.workspace_id.label("workspace_id"),
            WorkspaceEvent.id.label("event_id"),
            WorkspaceEvent.event_type.label("event_type"),
            WorkspaceEvent.occurred_at.label("occurred_at"),
            WorkspaceEvent.event_order.label("event_order"),
            WorkspaceEvent.payload["retry_after"].as_string().label("retry_after"),
            func.row_number()
            .over(
                partition_by=WorkspaceEvent.workspace_id,
                order_by=(
                    WorkspaceEvent.occurred_at.desc(),
                    WorkspaceEvent.event_order.desc().nullslast(),
                    WorkspaceEvent.id.desc(),
                ),
            )
            .label("event_rank"),
        )
        .where(
            WorkspaceEvent.event_type.in_(
                tuple(_PLANNING_SCOPE_AUTO_RETRY_PENDING_TERMINAL_RELEASE_EVENT_TYPES)
            )
        )
        .where(
            WorkspaceEvent.payload["source_reason_code"].as_string()
            == AGENT_PLAN_PHASE_SCOPE_VIOLATION
        )
        .subquery()
    )
    # Rank only resumable markers. Later manual retries and terminal
    # planning-scope events still suppress resume via this guard, without
    # inflating the ranked candidate set on every cleanup scan.
    newer_planning_event = aliased(WorkspaceEvent)
    same_planning_event_timestamp = (
        newer_planning_event.occurred_at == latest_planning_event.c.occurred_at
    )
    # Event IDs are random UUID strings, so they are safe for identity checks
    # but not as temporal tiebreakers. For same-tick non-pending legacy rows
    # where both event_order values are missing, suppress conservatively.
    newer_planning_event_exists = (
        select(newer_planning_event.id)
        .where(newer_planning_event.workspace_id == latest_planning_event.c.workspace_id)
        .where(
            newer_planning_event.event_type.in_(
                tuple(_PLANNING_SCOPE_AUTO_RETRY_TERMINAL_RELEASE_EVENTS)
            )
        )
        .where(
            or_(
                newer_planning_event.event_type == _WORKSPACE_RETRY_REQUESTED_EVENT_TYPE,
                newer_planning_event.payload["source_reason_code"].as_string()
                == AGENT_PLAN_PHASE_SCOPE_VIOLATION,
            )
        )
        .where(
            or_(
                newer_planning_event.occurred_at > latest_planning_event.c.occurred_at,
                and_(
                    same_planning_event_timestamp,
                    newer_planning_event.event_order.is_not(None),
                    latest_planning_event.c.event_order.is_(None),
                ),
                and_(
                    same_planning_event_timestamp,
                    newer_planning_event.event_order.is_not(None),
                    latest_planning_event.c.event_order.is_not(None),
                    newer_planning_event.event_order > latest_planning_event.c.event_order,
                ),
                and_(
                    same_planning_event_timestamp,
                    newer_planning_event.event_order.is_(None),
                    latest_planning_event.c.event_order.is_(None),
                    newer_planning_event.id != latest_planning_event.c.event_id,
                    ~newer_planning_event.event_type.in_(
                        tuple(_PLANNING_SCOPE_AUTO_RETRY_PENDING_TERMINAL_RELEASE_EVENT_TYPES)
                    ),
                ),
            )
        )
        .limit(1)
        .exists()
    )
    effectively_released = terminal_runtime_effectively_released_expr(
        correlated_to=Workspace,
    )
    worker_node_id = effective_worker_config_node_id(self._config)
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
    terminal_status_values = [status.value for status in _TERMINAL_RELEASE_STATUSES]
    stmt = (
        select(
            Workspace.id,
            Workspace.status,
            Workspace.repo_url,
            Workspace.compose_project_name,
            Workspace.compose_file_path,
        )
        .join(latest_planning_event, latest_planning_event.c.workspace_id == Workspace.id)
        .where(latest_planning_event.c.event_rank == 1)
        .where(
            latest_planning_event.c.event_type.in_(
                tuple(_PLANNING_SCOPE_AUTO_RETRY_PENDING_TERMINAL_RELEASE_EVENT_TYPES)
            )
        )
        .where(latest_planning_event.c.retry_after == _TERMINAL_RUNTIME_RELEASE_RETRY_AFTER)
        .where(~newer_planning_event_exists)
        .where(Workspace.status.in_(terminal_status_values))
        .where(
            or_(
                effective_node == worker_node_id,
                and_(
                    Workspace.node_id.is_(None),
                    active_reservation_node.is_(None),
                    latest_reservation_node.is_(None),
                ),
            )
        )
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


async def _list_terminal_runtime_candidates(
    self: Any,
    *,
    limit: int | None = None,
) -> list[_TerminalRuntimeCandidate]:
    """Return workspaces in terminal-release statuses that have not yet been effectively released."""
    if limit is not None and limit <= 0:
        return []
    terminal_status_values = [status.value for status in _TERMINAL_RELEASE_STATUSES]
    effectively_released = terminal_runtime_effectively_released_expr(
        correlated_to=Workspace,
    )
    worker_node_id = effective_worker_config_node_id(self._config)
    stmt = (
        select(
            Workspace.id,
            Workspace.status,
            Workspace.repo_url,
            Workspace.compose_project_name,
            Workspace.compose_file_path,
        )
        .where(Workspace.status.in_(terminal_status_values))
        # Include every terminal row on this node — even those where both
        # ``compose_project_name`` and ``compose_file_path`` are NULL.
        # The cleaner derives ``awf_<workspace_id>`` and falls back to
        # label-based removal, so legacy rows that predate persistence of
        # either field can still have a leaked default Compose project torn
        # down. Also include rows with NULL ``node_id``: ``Provisioner.
        # _mark_failed`` now stamps placement on the failure path so new
        # rows always carry the launching node, but legacy rows persisted
        # before that fix may still have NULL ``node_id``. In a multi-node
        # deployment the local cleaner would silently report success when
        # the resources actually live on a sibling node, so this fallback
        # is only safe while AWF is single-node (Phase 1 PRD §20.1). When
        # Phase 2 introduces multi-node, this branch must gain a node
        # ownership claim or a "found-something" precondition before it
        # records ``terminal_runtime_released``. ``~effectively_released``
        # keeps each row to a single sweep, but re-includes workspaces
        # whose release was later revoked (orphan containers still running).
        .where(
            or_(
                Workspace.node_id == worker_node_id,
                Workspace.node_id.is_(None),
            )
        )
        .where(~effectively_released)
        # Order by the retry marker if one has been recorded, falling back
        # to ``updated_at`` for rows that have never failed a release. The
        # marker lets persistently-failing workspaces rotate to the back of
        # the queue without advancing ``Workspace.updated_at`` — both
        # ``service/gc.py`` and ``service/orphan_resources.py`` use
        # ``updated_at`` as the retention cutoff, so bumping it on every
        # retry would indefinitely defer cleanup of stuck rows.
        .order_by(
            func.coalesce(Workspace.terminal_release_retry_at, Workspace.updated_at).asc(),
            Workspace.id.asc(),
        )
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
    for row in rows:
        (
            workspace_id,
            status_val,
            repo_url,
            compose_project_name,
            compose_file_path,
        ) = row
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


async def _release_terminal_runtime_for_candidate(
    self: Any,
    candidate: _TerminalRuntimeCandidate,
) -> None:
    """Clean up a single terminal workspace's runtime and record the outcome event."""
    if self._runtime_cleaner is None:
        return
    try:
        cleanup = await self._runtime_cleaner.cleanup(
            workspace_id=candidate.workspace_id,
            repo_url=candidate.repo_url,
            compose_project_name=candidate.compose_project_name,
            compose_file_path=(
                Path(candidate.compose_file_path) if candidate.compose_file_path else None
            ),
            remove_volumes=False,
            remove_worktree=False,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.exception(
            "worker.terminal_runtime_release_candidate_failed",
            workspace_id=candidate.workspace_id,
            status=candidate.status.value,
            compose_project_name=candidate.compose_project_name,
            reason_code=_TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE,
            error_type=type(exc).__name__,
            error=str(exc)[:240],
        )
        try:
            await self._record_terminal_runtime_release_failed(
                candidate,
                cleanup=None,
                message=f"runtime cleanup raised {type(exc).__name__}: {exc}"[:480],
            )
        except asyncio.CancelledError:
            raise
        except Exception as record_exc:
            _log.exception(
                "worker.terminal_runtime_release_event_write_failed",
                workspace_id=candidate.workspace_id,
                status=candidate.status.value,
                compose_project_name=candidate.compose_project_name,
                reason_code=_TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE,
                error_type=type(record_exc).__name__,
                error=str(record_exc)[:240],
            )
        return

    if cleanup.ok:
        # The compose stack is down, releasing the agent container's bind of the
        # overlay ``merged`` dir. The worker holds CAP_SYS_ADMIN and shares the
        # mount namespace the overlay was created in, so unmount it here — the one
        # context where the mount is visible — before GC (which runs capability-less
        # in the API container) ever tries to remove the auth dir. A failure does
        # not block port reclaim; GC's loud-failure net covers any residual.
        auth_overlay_unmounted = await _teardown_terminal_auth_overlay(self, candidate)
        await self._record_terminal_runtime_released(
            candidate, cleanup, auth_overlay_unmounted=auth_overlay_unmounted
        )
    else:
        try:
            await self._record_terminal_runtime_release_failed(
                candidate,
                cleanup=cleanup,
                message="failed to stop or remove terminal workspace runtime",
            )
        except asyncio.CancelledError:
            raise
        except Exception as record_exc:
            # Mirror the cleanup-raised branch above: swallow + log a
            # dedicated event-write failure entry instead of letting the
            # exception propagate to ``_release_terminal_runtime_resources``
            # where it would re-log as ``candidate_failed`` and re-raise.
            # Both outcomes leave the workspace eligible for retry on the
            # next scan, so the error is recoverable without surfacing.
            _log.exception(
                "worker.terminal_runtime_release_event_write_failed",
                workspace_id=candidate.workspace_id,
                status=candidate.status.value,
                compose_project_name=candidate.compose_project_name,
                reason_code=_TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE,
                error_type=type(record_exc).__name__,
                error=str(record_exc)[:240],
            )


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


async def _record_terminal_runtime_released(
    self: Any,
    candidate: _TerminalRuntimeCandidate,
    cleanup: WorkspaceCleanupResult,
    *,
    auth_overlay_unmounted: bool | None = None,
) -> None:
    """Record a ``workspace.terminal_runtime_released`` event after terminal cleanup completes.

    Uses ``SELECT FOR UPDATE SKIP LOCKED`` inside a retried DB operation to
    deduplicate against concurrent workers racing on the same candidate
    (possible when ``node_id`` is ``NULL``). Skips recording if the workspace
    is no longer in a terminal-release status or the event already exists.
    On recording failure, logs a warning and records a failed-release event
    instead so the host port is still reclaimable downstream.
    """
    payload = {
        "compose_project_name": candidate.compose_project_name,
        "workspace_status": candidate.status.value,
        "cleanup": cleanup.to_dict(),
        # Audit whether the worker released the per-workspace Claude overlay in its
        # own mount namespace (the only context that can). Three-valued so alerting
        # can tell apart a real failure from a no-overlay workspace: ``True`` =
        # unmounted, ``False`` = umount attempted but failed (GC's loud-failure path
        # is the net), ``None`` = not applicable (no overlay work dir was wired).
        "auth_overlay_unmounted": auth_overlay_unmounted,
    }

    async def _operation(session: AsyncSession) -> bool:
        repo = WorkspaceRepository(session)
        # ``SELECT FOR UPDATE SKIP LOCKED`` serializes concurrent workers
        # racing to record the success event for the same workspace, which
        # the candidate query may surface to multiple workers when
        # ``Workspace.node_id`` is ``NULL`` (legacy rows in a Phase 2
        # multi-node deployment). The loser sees ``None`` and exits
        # without writing a duplicate ``workspace.terminal_runtime_released``
        # entry. On a single-node deployment this is a no-op.
        ws = await repo.get_for_update(candidate.workspace_id, skip_locked=True)
        if ws is None:
            return False
        if ws.status not in {status.value for status in _TERMINAL_RELEASE_STATUSES}:
            return False
        if await self._has_terminal_runtime_release_event(session, candidate.workspace_id):
            return False
        await repo.add_event(
            ws,
            event_type=_TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
            reason_code=_TERMINAL_RUNTIME_RELEASE_REASON_CODE,
            payload=payload,
        )
        # The umount was attempted but failed (``False``; ``True``/``None`` need no
        # follow-up). Record a ``pending`` marker in the *same* transaction as the
        # release so the port is still reclaimed atomically, yet a later deferred
        # re-sweep can re-attempt the umount instead of leaking the overlay forever.
        if auth_overlay_unmounted is False:
            await repo.add_event(
                ws,
                event_type=_TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
                reason_code=_TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE,
                payload={
                    "compose_project_name": candidate.compose_project_name,
                    "workspace_status": candidate.status.value,
                    "attempt": 1,
                },
            )
        return True

    recorded = await run_db_operation_with_retry(
        self._session_factory,
        _operation,
        commit=True,
        on_retry=self._log_transient_db_retry,
    )
    if not recorded:
        return

    _log.info(
        _TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
        workspace_id=candidate.workspace_id,
        status=candidate.status.value,
        compose_project_name=candidate.compose_project_name,
        reason_code=_TERMINAL_RUNTIME_RELEASE_REASON_CODE,
    )
    try:
        await _resume_blocked_planning_scope_auto_retry_after_runtime_release(
            self,
            workspace_id=candidate.workspace_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await _handle_planning_scope_auto_retry_resume_failure(
            self,
            workspace_id=candidate.workspace_id,
            status=candidate.status.value,
            compose_project_name=candidate.compose_project_name,
            exc=exc,
        )


async def _handle_planning_scope_auto_retry_resume_failure(
    self: Any,
    *,
    workspace_id: str,
    status: str | None,
    compose_project_name: str | None,
    exc: Exception,
) -> None:
    log_fields: dict[str, Any] = {
        "workspace_id": workspace_id,
        "reason_code": _PLANNING_SCOPE_AUTO_RETRY_RESUME_FAILED_REASON_CODE,
        "error_type": type(exc).__name__,
        "error": str(exc)[:240],
    }
    if status is not None:
        log_fields["status"] = status
    if compose_project_name is not None:
        log_fields["compose_project_name"] = compose_project_name
    _log.warning(
        "worker.planning_scope_auto_retry_resume_after_runtime_release_failed",
        **log_fields,
    )
    try:
        await _record_planning_scope_auto_retry_resume_failed_after_runtime_release(
            self,
            workspace_id=workspace_id,
            error=exc,
        )
    except asyncio.CancelledError:
        raise
    except Exception as record_exc:
        _log.warning(
            "worker.planning_scope_auto_retry_resume_failed_event_write_failed",
            workspace_id=workspace_id,
            reason_code=_PLANNING_SCOPE_AUTO_RETRY_RESUME_FAILED_REASON_CODE,
            error_type=type(record_exc).__name__,
            error=str(record_exc)[:240],
        )


async def _record_terminal_runtime_release_failed(
    self: Any,
    candidate: _TerminalRuntimeCandidate,
    *,
    cleanup: WorkspaceCleanupResult | None,
    message: str,
) -> None:
    """Record a ``workspace.terminal_runtime_release_failed`` event and bump the retry marker."""
    payload: dict[str, Any] = {
        "compose_project_name": candidate.compose_project_name,
        "workspace_status": candidate.status.value,
        "message": message,
    }
    if cleanup is not None:
        payload["cleanup"] = cleanup.to_dict()

    async def _operation(session: AsyncSession) -> str:
        repo = WorkspaceRepository(session)
        # Mirror the ``SELECT FOR UPDATE SKIP LOCKED`` guard used by the
        # success path: when two workers surface the same NULL ``node_id``
        # candidate, both could otherwise pass the failure-event guard
        # before either commits and append duplicate
        # ``workspace.terminal_runtime_release_failed`` rows. The loser
        # sees ``None`` and exits; the winner records the single failure
        # event and bumps ``updated_at`` for backlog rotation.
        ws = await repo.get_for_update(candidate.workspace_id, skip_locked=True)
        if ws is None:
            return "skipped"
        if ws.status not in {status.value for status in _TERMINAL_RELEASE_STATUSES}:
            return "skipped"
        if await self._has_terminal_runtime_release_event(session, candidate.workspace_id):
            return "skipped"
        # Push the workspace behind newer terminal rows in the next scan: the
        # candidate query orders by
        # ``coalesce(terminal_release_retry_at, updated_at).asc()`` and
        # ``add_event`` does not touch either column, so without this bump a
        # persistently failing release would re-select the same rows every
        # scan and starve the backlog past ``terminal_runtime_release_max_per_scan``.
        # NOTE: the bump is intentionally applied *before* the idempotency
        # guard below. When a failure event already exists (the "duplicate"
        # early return), the mutation is still committed by
        # ``run_db_operation_with_retry`` (commit=True), rotating the row to
        # the back of the scan queue on every retry — preventing a single
        # persistently-failing workspace from monopolising the scan limit
        # across consecutive sweeps.
        #
        # ``Workspace.updated_at`` is intentionally NOT advanced here: GC
        # (``service/gc.py``) and orphan retention (``service/orphan_resources.py``)
        # use ``updated_at`` as the retention cutoff. Bumping it on every
        # retry would indefinitely defer cleanup of volumes/worktrees for
        # persistently-failing workspaces. Re-assigning ``ws.updated_at`` to
        # its prior value and flagging it modified suppresses the
        # ``onupdate=_now`` column default so the lifecycle timestamp stays
        # frozen across retries.
        prior_updated_at = ws.updated_at
        ws.terminal_release_retry_at = datetime.now(UTC)
        ws.updated_at = prior_updated_at
        flag_modified(ws, "updated_at")
        if await self._has_terminal_runtime_release_failure_event(session, candidate.workspace_id):
            return "duplicate"
        primary_failure = await load_primary_failure_snapshot(session, ws)
        event_payload = attach_primary_failure(payload, primary_failure)
        await repo.add_event(
            ws,
            event_type=_TERMINAL_RUNTIME_RELEASE_FAILED_EVENT_TYPE,
            reason_code=_TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE,
            payload=event_payload,
        )
        return "recorded"

    outcome = await run_db_operation_with_retry(
        self._session_factory,
        _operation,
        commit=True,
        on_retry=self._log_transient_db_retry,
    )
    if outcome == "skipped":
        return
    if outcome == "duplicate":
        # A prior retry already wrote the failure event; suppressing the
        # duplicate keeps the event log lean, but we still bump
        # ``updated_at`` above and the candidate query keeps reselecting the
        # row because no success event exists. Emit a structured warning so
        # each retry leaves reason-code evidence in the log even when no
        # new event row is written.
        _log.warning(
            "worker.terminal_runtime_release_failed_retry",
            workspace_id=candidate.workspace_id,
            status=candidate.status.value,
            compose_project_name=candidate.compose_project_name,
            reason_code=_TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE,
            message=message,
        )
        return

    _log.error(
        _TERMINAL_RUNTIME_RELEASE_FAILED_EVENT_TYPE,
        workspace_id=candidate.workspace_id,
        status=candidate.status.value,
        compose_project_name=candidate.compose_project_name,
        reason_code=_TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE,
        message=message,
    )


async def _has_terminal_runtime_release_event(
    self: Any,
    session: AsyncSession,
    workspace_id: str,
) -> bool:
    """Return True if the workspace already has a ``terminal_runtime_released`` event."""
    _ = self
    return await has_terminal_runtime_released_event(session, workspace_id)


async def _has_terminal_runtime_release_failure_event(
    self: Any,
    session: AsyncSession,
    workspace_id: str,
) -> bool:
    """Return True if the workspace already has a ``terminal_runtime_release_failed`` event."""
    _ = self
    stmt = (
        select(WorkspaceEvent.id)
        .where(
            WorkspaceEvent.workspace_id == workspace_id,
            WorkspaceEvent.event_type == _TERMINAL_RUNTIME_RELEASE_FAILED_EVENT_TYPE,
            WorkspaceEvent.reason_code == _TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE,
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


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
    """Log a per-candidate deferred-sweep failure without aborting the scan."""
    _ = self
    _log.warning(
        _TERMINAL_AUTH_OVERLAY_UNMOUNT_RETRY_FAILED_EVENT_TYPE,
        workspace_id=candidate.workspace_id,
        status=candidate.status.value,
        compose_project_name=candidate.compose_project_name,
        reason_code=_TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE,
        error_type=type(exc).__name__,
        error=str(exc)[:240],
    )


async def _list_pending_terminal_auth_overlay_unmount_candidates(
    self: Any,
    *,
    limit: int | None = None,
) -> list[_TerminalRuntimeCandidate]:
    """Return terminal workspaces with an unresolved pending overlay-umount marker.

    The predicate is intentionally event-type existence/count only (no JSONB value
    comparison), so it behaves identically on Postgres (prod) and SQLite (tests): a
    terminal-status workspace on this node (or ``node_id IS NULL``, matching
    ``_list_terminal_runtime_candidates``) with a non-empty ``repo_url``, at least one
    ``pending`` marker, and no ``resolved`` and no ``exhausted`` marker.
    """
    if limit is not None and limit <= 0:
        return []
    terminal_status_values = [status.value for status in _TERMINAL_RELEASE_STATUSES]
    worker_node_id = effective_worker_config_node_id(self._config)
    pending_exists = (
        select(WorkspaceEvent.id)
        .where(WorkspaceEvent.workspace_id == Workspace.id)
        .where(WorkspaceEvent.event_type == _TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE)
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
                Workspace.node_id == worker_node_id,
                Workspace.node_id.is_(None),
            )
        )
        .where(pending_exists)
        .where(~terminal_marker_exists)
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
    """Return the number of ``pending`` overlay-umount markers for *workspace_id*.

    The count equals the number of umount attempts recorded so far and bounds the
    deferred re-sweeps (event-based counter).
    """

    async def _operation(session: AsyncSession) -> int:
        stmt = (
            select(func.count())
            .select_from(WorkspaceEvent)
            .where(WorkspaceEvent.workspace_id == workspace_id)
            .where(WorkspaceEvent.event_type == _TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE)
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
    """Return True if a ``resolved`` or ``exhausted`` overlay-umount marker exists."""
    _ = self
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
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


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

    async def _operation(session: AsyncSession) -> bool:
        repo = WorkspaceRepository(session)
        # ``SELECT FOR UPDATE SKIP LOCKED`` + the terminal-marker guard make the
        # write idempotent under the NULL-``node_id`` multi-worker race: the loser
        # exits without double-writing a ``resolved``/``exhausted`` pair.
        ws = await repo.get_for_update(candidate.workspace_id, skip_locked=True)
        if ws is None:
            return False
        if await self._has_terminal_auth_overlay_unmount_terminal_event(
            session, candidate.workspace_id
        ):
            return False
        await repo.add_event(
            ws,
            event_type=_TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_EVENT_TYPE,
            reason_code=_TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_REASON_CODE,
            payload=payload,
        )
        return True

    recorded = await run_db_operation_with_retry(
        self._session_factory,
        _operation,
        commit=True,
        on_retry=self._log_transient_db_retry,
    )
    if not recorded:
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

    async def _operation(session: AsyncSession) -> bool:
        repo = WorkspaceRepository(session)
        ws = await repo.get_for_update(candidate.workspace_id, skip_locked=True)
        if ws is None:
            return False
        if await self._has_terminal_auth_overlay_unmount_terminal_event(
            session, candidate.workspace_id
        ):
            return False
        await repo.add_event(
            ws,
            event_type=_TERMINAL_AUTH_OVERLAY_UNMOUNT_EXHAUSTED_EVENT_TYPE,
            reason_code=_TERMINAL_AUTH_OVERLAY_UNMOUNT_EXHAUSTED_REASON_CODE,
            payload=payload,
        )
        return True

    recorded = await run_db_operation_with_retry(
        self._session_factory,
        _operation,
        commit=True,
        on_retry=self._log_transient_db_retry,
    )
    if not recorded:
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

    async def _operation(session: AsyncSession) -> bool:
        repo = WorkspaceRepository(session)
        ws = await repo.get_for_update(candidate.workspace_id, skip_locked=True)
        if ws is None:
            return False
        # If a concurrent worker already resolved/exhausted this workspace, do not
        # append a new ``pending`` that would resurrect it as a deferred candidate.
        if await self._has_terminal_auth_overlay_unmount_terminal_event(
            session, candidate.workspace_id
        ):
            return False
        await repo.add_event(
            ws,
            event_type=_TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
            reason_code=_TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE,
            payload=payload,
        )
        return True

    recorded = await run_db_operation_with_retry(
        self._session_factory,
        _operation,
        commit=True,
        on_retry=self._log_transient_db_retry,
    )
    if not recorded:
        return
    _log.warning(
        _TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
        workspace_id=candidate.workspace_id,
        status=candidate.status.value,
        compose_project_name=candidate.compose_project_name,
        reason_code=_TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE,
        attempt=attempt,
    )
