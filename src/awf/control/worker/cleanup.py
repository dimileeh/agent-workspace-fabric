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
from typing import Any, cast

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
from awf.control.worker.cleanup_auth_overlay import (
    _TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
    _TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE,
    _TERMINAL_AUTH_OVERLAY_UNMOUNT_RETRY_SCAN_FAILED_EVENT_TYPE,
    _TERMINAL_AUTH_OVERLAY_UNMOUNT_RETRY_SCAN_FAILED_REASON_CODE,
    _teardown_terminal_auth_overlay,
)
from awf.control.worker.config import (
    effective_worker_config_node_id,
)
from awf.control.worker.constants import (
    _CLASSIFIED_ORPHAN_REAP_FAILED_REASON_CODE,
    _CLAUDE_BASE_REAP_SWEEP_FAILED_REASON_CODE,
    _ORPHAN_DIR_RECONCILE_FAILED_REASON_CODE,
    _PROMPT_TERMINAL_RUNTIME_RELEASE_FAILED_EVENT_TYPE,
    _PROMPT_TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE,
    _SERVICE_GC_TRIGGER_CONSUME_FAILED_REASON_CODE,
    _TERMINAL_RELEASE_STATUSES,
    _TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
    _TERMINAL_RUNTIME_RELEASE_FAILED_EVENT_TYPE,
    _TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE,
    _TERMINAL_RUNTIME_RELEASE_REASON_CODE,
    _TERMINAL_WORKSPACE_GC_REAP_FAILED_REASON_CODE,
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
from awf.db.repositories import ServiceGCRequestRepository, WorkspaceRepository
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
from awf.service.gc_worker_delegation import SERVICE_GC_WORKER_RECLAIM_FAILED
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


async def _load_terminal_runtime_candidate(
    self: Any,
    workspace_id: str,
) -> _TerminalRuntimeCandidate | None:
    """Build a single terminal-runtime release candidate for one workspace.

    Used by the prompt release fired on a terminal transition (#583, #584).
    Returns ``None`` (a clean no-op for the prompt path) when the workspace row
    is missing, is not in a terminal-release status, carries no ``repo_url``, or
    is owned by another node (``node_id`` neither this worker's nor ``NULL`` —
    mirroring ``_list_terminal_runtime_candidates``' single-node fallback so we
    never release a runtime another node may still own).

    Unlike ``_list_terminal_runtime_candidates`` this deliberately does **not**
    gate on the effective-release marker. A ``--stop-stack`` cancel pre-emits a
    ``terminal_runtime_released`` event in the service after only a ``docker
    stop`` (containers Exited, ``-net`` network still present), so gating here
    would skip the real ``compose down`` and re-leak #583. Idempotency is provided
    downstream instead: the cleaner's ``down`` is a no-op on an already-down stack,
    and ``_record_terminal_runtime_released`` early-returns when a release event
    already exists, so re-invocation never double-records.
    """
    worker_node_id = effective_worker_config_node_id(self._config)
    stmt = (
        select(
            Workspace.status,
            Workspace.repo_url,
            Workspace.compose_project_name,
            Workspace.compose_file_path,
            Workspace.node_id,
        )
        .where(Workspace.id == workspace_id)
        .limit(1)
    )

    async def _operation(session: AsyncSession) -> Any:
        result = await session.execute(stmt)
        return result.one_or_none()

    row = await run_db_operation_with_retry(
        self._session_factory,
        _operation,
        on_retry=self._log_transient_db_retry,
    )
    if row is None:
        return None
    status_val, repo_url, compose_project_name, compose_file_path, node_id = row
    if status_val not in {status.value for status in _TERMINAL_RELEASE_STATUSES}:
        return None
    if not repo_url:
        return None
    if node_id is not None and node_id != worker_node_id:
        return None
    return _TerminalRuntimeCandidate(
        workspace_id=workspace_id,
        status=WorkspaceStatus(status_val),
        repo_url=repo_url,
        compose_project_name=compose_project_name,
        compose_file_path=compose_file_path,
    )


async def _release_terminal_runtime_promptly(self: Any, workspace_id: str) -> None:
    """Eagerly release a workspace's terminal runtime on its terminal transition.

    Reuses the *existing* per-candidate teardown
    (``_release_terminal_runtime_for_candidate``: ``compose down`` removing the
    agent+postgres containers and the per-ws ``-net`` network, then the Claude
    auth-overlay umount, then the idempotent ``terminal_runtime_released`` event)
    that the periodic ``_maybe_release_terminal_runtime`` interval otherwise
    drives. Firing it here, right after a terminal transition, reclaims the
    runtime promptly instead of up to an interval (~1h) later — fixing the
    cancel/stop-stack container+network leak (#583) and the per-ws auth-overlay
    pile-up (#584). The periodic interval stays in place as the unchanged backstop.

    A no-op when no runtime cleaner is wired or the workspace is not (yet) a
    release candidate (non-terminal, missing, or foreign-node — see
    :func:`_load_terminal_runtime_candidate`). The preservation policy is
    unchanged: this only changes *when* the existing release fires, never *what*
    it preserves, so failed-workspace auth-dir preservation (owned by the terminal
    GC reaper) is honored automatically.

    The teardown runs through the shield-and-reawait pattern used by
    ``_release_execution_claim_after_cancellation`` so a ``CancelledError`` from
    worker shutdown / task-cancel cannot tear a ``compose down`` apart mid-flight
    and re-leak the stack; the ``CancelledError`` is re-raised after the teardown
    completes. Any other failure is swallowed and logged — a prompt-release
    failure must never break the terminal transition itself, and the interval
    backstop reclaims any miss.
    """
    if self._runtime_cleaner is None:
        return

    async def _body() -> None:
        candidate = await self._load_terminal_runtime_candidate(workspace_id)
        if candidate is None:
            return
        await self._release_terminal_runtime_for_candidate(candidate)

    body_task = asyncio.create_task(
        _body(),
        name=f"awf-prompt-terminal-runtime-release-{workspace_id}",
    )
    observed_cancel = False
    while not body_task.done():
        try:
            await asyncio.shield(body_task)
        except asyncio.CancelledError:
            # An external cancel (worker shutdown) landed on the shield, not on the
            # shielded teardown. Record it and re-await so the ``compose down`` /
            # overlay umount still run to completion, then re-raise below.
            observed_cancel = True
        except Exception:
            # The teardown raised; ``body_task`` is now done. Fall through to the
            # swallow-and-log below — never propagate, so the terminal transition
            # is not broken.
            break
    # ``body_task.exception()`` *raises* ``CancelledError`` (rather than returning
    # it) when the teardown coroutine self-cancels internally — e.g. a
    # ``CancelledError`` propagated out of
    # ``_release_terminal_runtime_for_candidate``'s ``except asyncio.CancelledError:
    # raise`` guards marks ``body_task`` as cancelled, not failed. Guard with
    # ``cancelled()`` first so such an edge case is treated as the swallow-and-log
    # path (the cancel is still re-raised below via ``observed_cancel``) instead of
    # propagating unintentionally and shadowing the caller's original exception.
    failure = None if body_task.cancelled() else body_task.exception()
    if failure is not None:
        _log.warning(
            _PROMPT_TERMINAL_RUNTIME_RELEASE_FAILED_EVENT_TYPE,
            workspace_id=workspace_id,
            reason_code=_PROMPT_TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE,
            error_type=type(failure).__name__,
            error=str(failure)[:240],
        )
    if observed_cancel:
        raise asyncio.CancelledError


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


async def _maybe_reap_superseded_claude_bases(self: Any) -> None:
    """Periodically reap superseded shared ``~/.claude`` overlay bases (#509).

    The shared-base reaper (``service.gc_claude_base.reap_superseded_claude_bases``)
    must run in a CAP_SYS_ADMIN context to trust its ``/proc/mounts`` live-mount
    verification — only the worker holds that capability and shares the agent's
    mount namespace, so the API ``/v1/service/gc`` path conservatively protects
    every base and reaps nothing. This loop drives the reaper from the worker so
    superseded bases (~1.7 GB each) are actually reclaimed.

    No-op when no reaper callback is wired or the kill-switch
    (``claude_base_gc_enabled``) is off, in which case the cursor is left eligible
    so enabling the flag does not wait out a stale interval (mirrors
    ``_maybe_reap_classified_orphans``'s ``auto_cleanup_orphans`` gate). The reaper
    closure wraps a blocking multi-GB ``rmtree`` in ``asyncio.to_thread`` and never
    raises for expected partial/permission cases — it returns a report dict — so
    failures are summarised and the cursor is always rescheduled. Unlike the
    classified-orphan loop the reaper does no DB I/O, so there is no transient-DB
    branch; any unexpected exception is logged loudly via ``_log.exception`` and
    swallowed so one failed sweep cannot block provisioning or dispatch.
    ``asyncio.CancelledError`` propagates for cooperative shutdown.
    """
    if self._claude_base_reaper is None:
        return
    if not self._config.claude_base_gc_enabled:
        return

    now = monotonic()
    if now < self._next_claude_base_reap_scan_at:
        return

    try:
        report = await self._claude_base_reaper()
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "worker.claude_base_reap_failed",
            reason_code=_CLAUDE_BASE_REAP_SWEEP_FAILED_REASON_CODE,
        )
    else:
        _log_claude_base_reap_summary(report)

    interval = max(0.0, self._config.claude_base_reap_scan_interval_seconds)
    self._next_claude_base_reap_scan_at = monotonic() + interval


def _log_claude_base_reap_summary(report: dict[str, object]) -> None:
    """Emit a structured summary for a completed Claude-base reap sweep.

    Logs ``info`` when bases were reclaimed and ``warning`` when the reaper
    self-protected to a ``partial`` (e.g. a permission-denied removal or an
    unverifiable live mount). A clean ``skipped`` no-op (nothing superseded, or a
    dry-run preview) is intentionally silent to avoid log noise each interval. Base
    contents/secrets are never logged — only signature names the reaper already
    surfaces in its report.
    """
    reaped = report.get("reaped") or []
    # Independent conditions, not mutually exclusive: a ``partial`` pass can still
    # have reclaimed some bases while another base errored, so surface both the
    # reclaim (info) and the partial (warning) rather than masking one behind the
    # other.
    if reaped:
        _log.info(
            "worker.claude_base_superseded_reaped",
            reason_code=report.get("reason_code"),
            reaped=reaped,
        )
    if report.get("status") == "partial":
        _log.warning(
            "worker.claude_base_reap_partial",
            reason_code=report.get("reason_code"),
            errors=report.get("errors") or [],
            unverifiable=report.get("unverifiable") or [],
        )


async def _maybe_reap_terminal_workspace_gc(self: Any) -> None:
    """Periodically reap per-workspace terminal-workspace auth dirs from the worker (#513).

    The terminal-workspace GC (``service.gc.run_service_workspace_gc``) reclaims the
    per-workspace ``auth/<id>/`` dirs (~1.7 GB each) of completed/cancelled/destroyed
    workspaces. Removing one first unmounts a possible live Claude overlay at
    ``auth/<id>/claude/merged``, and ``teardown_workspace_auth_overlay`` raises
    ``OverlayUnmountUnverifiableError`` whenever the caller cannot prove it holds
    CAP_SYS_ADMIN — so the capability-less API ``/v1/service/gc`` path records every
    auth path ``skipped`` and deletes nothing. Only the worker holds CAP_SYS_ADMIN and
    shares the agent's mount namespace, so this loop drives the same already-tested GC
    from the worker, where the unmount-before-remove actually succeeds. Reuses the GC's
    classification, path planning, ``WorkspaceGCPathOutcome`` bookkeeping, and the
    ``preserves_failed_workspaces=True`` policy (failed workspaces are never reaped).

    No-op when no reaper callback is wired or the kill-switch
    (``terminal_workspace_gc_enabled``) is off, in which case the cursor is left
    eligible so enabling the flag does not wait out a stale interval (mirrors
    ``_maybe_reap_superseded_claude_bases``). The reaper closure ``await``s the async
    GC (which offloads its blocking multi-GB ``rmtree``/DB work via
    ``asyncio.to_thread`` internally) and returns a report dict rather than raising for
    expected partial/skipped cases, so failures are summarised and the cursor is always
    rescheduled. Any unexpected exception is logged loudly via ``_log.exception`` and
    swallowed so one failed sweep cannot block provisioning or dispatch.
    ``asyncio.CancelledError`` propagates for cooperative shutdown.
    """
    if self._terminal_gc_reaper is None:
        return
    if not self._config.terminal_workspace_gc_enabled:
        return

    now = monotonic()
    if now < self._next_terminal_gc_reap_scan_at:
        return

    try:
        report = await self._terminal_gc_reaper()
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "worker.terminal_workspace_gc_reap_failed",
            reason_code=_TERMINAL_WORKSPACE_GC_REAP_FAILED_REASON_CODE,
        )
    else:
        _log_terminal_gc_reap_summary(report)

    interval = max(0.0, self._config.terminal_workspace_gc_scan_interval_seconds)
    self._next_terminal_gc_reap_scan_at = monotonic() + interval


def _log_terminal_gc_reap_summary(report: dict[str, object]) -> None:
    """Emit a structured summary for a completed terminal-workspace GC sweep.

    Logs ``info`` when auth/worktree/compose paths were reclaimed and ``warning`` when
    the GC self-reported a ``partial`` (e.g. a delete error or an unverifiable overlay
    unmount). A clean run that reclaimed nothing (no eligible candidates, or a dry-run
    preview) is intentionally silent to avoid log noise each interval. Only counts and
    the reason code the GC report already surfaces are logged — never auth-dir contents
    or secrets.
    """
    deleted_path_count = report.get("deleted_path_count") or 0
    # Independent conditions, not mutually exclusive: a ``partial`` pass can still have
    # reclaimed some paths while another path errored, so surface both the reclaim
    # (info) and the partial (warning) rather than masking one behind the other.
    if deleted_path_count:
        _log.info(
            "worker.terminal_workspace_gc_reaped",
            reason_code=report.get("reason_code"),
            deleted_path_count=deleted_path_count,
            candidate_count=report.get("candidate_count"),
            preserved_count=report.get("preserved_count"),
        )
    if report.get("status") == "partial":
        _log.warning(
            "worker.terminal_workspace_gc_partial",
            reason_code=report.get("reason_code"),
            # Count only — never the per-path ``error``/``path`` entries — so an
            # auth-dir path can't leak into the worker log.
            delete_error_count=len(cast("list[object]", report.get("delete_errors") or [])),
        )


async def _maybe_consume_service_gc_trigger(self: Any) -> None:
    """Consume an on-demand ``service_gc_requests`` row by running the GC reap now (#582).

    The capability-less API ``/v1/service/gc --execute`` path cannot reclaim the
    per-workspace Claude auth overlays or ``_shared/claude-base``; it writes a
    ``pending`` row and waits for the worker instead of silently reclaiming 0. This
    claims the oldest such row (``SELECT ... FOR UPDATE SKIP LOCKED``, so the
    interval reaper or a second worker never double-claims), marks it ``running``,
    runs the *already-wired* ``self._terminal_gc_reaper`` (its pass-1 already reaps
    claude-base — calling ``self._claude_base_reaper`` separately would double-reap),
    writes the combined report into ``result``, and marks the row ``completed``. A
    reaper failure marks the row ``failed`` with ``SERVICE_GC_WORKER_RECLAIM_FAILED``
    so the API surfaces a structured error instead of a false success.

    Runs every poll cycle (a single indexed ``status='pending'`` lookup is cheap)
    rather than on an interval, so the operator's on-demand trigger is picked up
    within ~one ``poll_interval_seconds``. Deliberately not gated on the
    ``terminal_workspace_gc_enabled`` interval kill-switch: that flag governs only
    the periodic backstop, while this path is an explicit operator request. No-op
    when no reaper is wired or no row is pending. Swallow-and-log discipline mirrors
    the sibling ``_maybe_reap_*`` methods — one failed consume must never break
    provisioning/dispatch. ``asyncio.CancelledError`` propagates for cooperative
    shutdown.
    """
    if self._terminal_gc_reaper is None:
        return

    try:
        claimed = await self._claim_service_gc_trigger()
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "worker.service_gc_trigger_claim_failed",
            reason_code=_SERVICE_GC_TRIGGER_CONSUME_FAILED_REASON_CODE,
        )
        return
    if claimed is None:
        return

    request_id, params = claimed
    # The run-and-finish path is guarded too, not just the claim above: the reaper
    # itself records its own failure on the row, but the terminal result-write
    # (``_finish_service_gc_trigger``) is otherwise unguarded — if its retries are
    # exhausted it would propagate through ``run_once`` and abort the whole poll
    # cycle, violating the swallow-and-log contract this docstring guarantees. Mirror
    # the sibling ``_maybe_reap_*`` methods: swallow-and-log here, re-raise only
    # ``CancelledError`` for cooperative shutdown.
    try:
        await self._run_claimed_service_gc_trigger(request_id, params)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "worker.service_gc_trigger_consume_failed",
            reason_code=_SERVICE_GC_TRIGGER_CONSUME_FAILED_REASON_CODE,
            request_id=request_id,
        )


async def _claim_service_gc_trigger(self: Any) -> tuple[str, dict[str, Any]] | None:
    """Atomically claim the oldest pending gc-trigger row for this node, if any.

    Returns the claimed row's ``id`` together with the operator-supplied ``params``
    the API persisted for this run (``min_age_hours``/``limit``) so the worker reap
    honours the same scope the operator just ran on the API side rather than the
    worker's server defaults (#590).
    """
    node_id = effective_worker_config_node_id(self._config)
    now = datetime.now(UTC)

    async def _operation(session: AsyncSession) -> tuple[str, dict[str, Any]] | None:
        request = await ServiceGCRequestRepository(session).claim_oldest_pending(
            node_id=node_id,
            now=now,
        )
        if request is None:
            return None
        return request.id, dict(request.params or {})

    return await run_db_operation_with_retry(
        self._session_factory,
        _operation,
        commit=True,
        on_retry=self._log_transient_db_retry,
    )


async def _run_claimed_service_gc_trigger(
    self: Any, request_id: str, params: dict[str, Any]
) -> None:
    """Run the reaper for a claimed trigger row and persist its terminal outcome.

    The claim and the (potentially multi-GB, multi-second) reap are in separate
    transactions so the row lock is not held across the reap. The operator-supplied
    ``min_age_hours``/``limit`` and ``statuses``/``exclude_statuses`` filters (resolved
    by the API and stored in ``params``) are forwarded to the reaper so the
    auth-overlay/claude-base reclaim matches the scope of the API-side pass the operator
    just ran (#590); absent keys leave the reaper on its server defaults (e.g. the
    periodic backstop). ``CancelledError`` propagates; any reaper failure is recorded on
    the row so the polling API does not hang and never reports false success.
    """
    reaper_kwargs: dict[str, Any] = {}
    min_age_hours = params.get("min_age_hours")
    if min_age_hours is not None:
        reaper_kwargs["min_age_hours"] = min_age_hours
    limit = params.get("limit")
    if limit is not None:
        reaper_kwargs["limit"] = limit
    statuses = params.get("statuses")
    if statuses:
        reaper_kwargs["statuses"] = statuses
    exclude_statuses = params.get("exclude_statuses")
    if exclude_statuses:
        reaper_kwargs["exclude_statuses"] = exclude_statuses

    try:
        report = await self._terminal_gc_reaper(**reaper_kwargs)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.exception(
            "worker.service_gc_trigger_reap_failed",
            reason_code=SERVICE_GC_WORKER_RECLAIM_FAILED,
            request_id=request_id,
        )
        await self._finish_service_gc_trigger(
            request_id,
            report=None,
            error=f"{type(exc).__name__}: {exc}"[:480],
        )
        return

    await self._finish_service_gc_trigger(request_id, report=report, error=None)
    _log_terminal_gc_reap_summary(report)


async def _finish_service_gc_trigger(
    self: Any,
    request_id: str,
    *,
    report: dict[str, object] | None,
    error: str | None,
) -> None:
    """Mark a claimed gc-trigger row ``completed`` (with report) or ``failed``."""
    now = datetime.now(UTC)

    async def _operation(session: AsyncSession) -> None:
        repo = ServiceGCRequestRepository(session)
        if error is None:
            await repo.mark_completed(
                request_id=request_id,
                result=report or {},
                now=now,
            )
        else:
            await repo.mark_failed(
                request_id=request_id,
                error_code=SERVICE_GC_WORKER_RECLAIM_FAILED,
                error_message=error,
                now=now,
            )

    await run_db_operation_with_retry(
        self._session_factory,
        _operation,
        commit=True,
        on_retry=self._log_transient_db_retry,
    )


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
    resume_failure: Exception | None = None
    try:
        await self._resume_pending_planning_scope_auto_retries_after_terminal_release(limit=limit)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if release_errors:
            _log.warning(
                "worker.terminal_runtime_release_resume_scan_failed_after_release_error",
                reason_code=_PLANNING_SCOPE_AUTO_RETRY_RESUME_FAILED_REASON_CODE,
                error_type=type(exc).__name__,
                error=str(exc)[:240],
            )
        else:
            # Defer the re-raise until the independent best-effort overlay sweep below
            # has run. The sweep mirrors the resume scan but is decoupled from it, so a
            # resume-scan failure must not skip the deferred overlay umount retries owed
            # for this interval (they would otherwise be silently dropped until the next).
            resume_failure = exc
    # Piggyback the deferred Claude auth-overlay umount re-sweep on the same scan
    # (mirrors the resume-pending scan above). It is fully guarded: a failure here
    # is swallowed-and-logged so it never perturbs the release-error aggregation or
    # the final re-raise below, and ``CancelledError`` still propagates promptly.
    try:
        await self._retry_pending_terminal_auth_overlay_unmounts(limit=limit)
    except asyncio.CancelledError:
        # Cooperative shutdown. Any pending ``resume_failure`` captured above is
        # intentionally dropped here rather than re-raised: a resume-scan failure is
        # transient and idempotently retried on the next worker start, so propagating
        # the ``CancelledError`` promptly (instead of swapping in ``resume_failure``)
        # honours the teardown signal without masking it and loses nothing durable.
        raise
    except Exception as exc:
        # The per-candidate loop already handles individual umount failures, so an
        # exception reaching this scan-level guard is unexpected (e.g. the candidate
        # listing query). Preserve its full traceback via ``exc_info`` — mirroring the
        # per-candidate handler — so the swallowed best-effort failure stays diagnosable
        # rather than masked behind the truncated ``error`` string.
        _log.warning(
            _TERMINAL_AUTH_OVERLAY_UNMOUNT_RETRY_SCAN_FAILED_EVENT_TYPE,
            reason_code=_TERMINAL_AUTH_OVERLAY_UNMOUNT_RETRY_SCAN_FAILED_REASON_CODE,
            error_type=type(exc).__name__,
            error=str(exc)[:240],
            exc_info=True,
        )
    if resume_failure is not None:
        # Only set when there were no release errors, so it is the sole pending failure.
        raise resume_failure
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
            # The release event already exists — e.g. a ``cancel --stop-stack`` pre-emits
            # ``terminal_runtime_released`` in the service after only a ``docker stop``
            # (#583/#584), yet the prompt path still ran ``compose down`` + the overlay
            # umount above. When that umount failed we must still seed the deferred-retry
            # ``pending`` marker; the bare early-return previously dropped it, so the
            # deferred re-sweep had no candidate and the ~1.7 GB overlay leaked despite the
            # prompt cleanup. Idempotent: skip when any pending/terminal overlay marker
            # already exists for the current release cycle so the deferred sweep keeps
            # owning the attempt lifecycle (and the umount count never inflates).
            if (
                auth_overlay_unmounted is False
                and not await self._has_current_cycle_terminal_auth_overlay_unmount_marker(
                    session, candidate.workspace_id
                )
            ):
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
