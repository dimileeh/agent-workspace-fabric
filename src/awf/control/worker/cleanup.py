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

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from awf.control.executor.planning_ops import (
    _PLANNING_SCOPE_AUTO_RETRY_PENDING_TERMINAL_RELEASE_EVENT_TYPES,
    _PLANNING_SCOPE_AUTO_RETRY_RESUME_FAILED_REASON_CODE,
    _PLANNING_SCOPE_AUTO_RETRY_TERMINAL_RELEASE_EVENTS,
    _TERMINAL_RUNTIME_RELEASE_RETRY_AFTER,
    _record_planning_scope_auto_retry_resume_failed_after_runtime_release,
    _resume_blocked_planning_scope_auto_retry_after_runtime_release,
)
from awf.control.worker.config import (
    effective_worker_config_node_id,
)
from awf.control.worker.constants import (
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


async def _release_terminal_runtime_resources(self: Any) -> None:
    """Run the terminal-runtime cleaner for all eligible candidates, raising on first failure."""
    await self._resume_pending_planning_scope_auto_retries_after_terminal_release(
        limit=self._config.terminal_runtime_release_max_per_scan,
    )
    if self._runtime_cleaner is None:
        return
    candidates = await self._list_terminal_runtime_candidates(
        limit=self._config.terminal_runtime_release_max_per_scan,
    )
    release_errors: list[Exception] = []
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
            WorkspaceEvent.event_type.label("event_type"),
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
            WorkspaceEvent.event_type.in_(tuple(_PLANNING_SCOPE_AUTO_RETRY_TERMINAL_RELEASE_EVENTS))
        )
        .where(
            WorkspaceEvent.payload["source_reason_code"].as_string()
            == AGENT_PLAN_PHASE_SCOPE_VIOLATION
        )
        .subquery()
    )
    effectively_released = terminal_runtime_effectively_released_expr(
        correlated_to=Workspace,
    )
    worker_node_id = effective_worker_config_node_id(self._config)
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
        .where(Workspace.status.in_(terminal_status_values))
        .where(
            or_(
                Workspace.node_id == worker_node_id,
                Workspace.node_id.is_(None),
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
        await self._record_terminal_runtime_released(candidate, cleanup)
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


async def _record_terminal_runtime_released(
    self: Any,
    candidate: _TerminalRuntimeCandidate,
    cleanup: WorkspaceCleanupResult,
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
