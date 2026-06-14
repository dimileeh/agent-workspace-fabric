"""On-demand ``service_gc_requests`` trigger consumption for the worker (#582, #590).

Extracted from :mod:`awf.control.worker.cleanup` with behavior unchanged to keep that
module under the first-party line-count guardrail. The capability-less
``/v1/service/gc --execute`` API path cannot reclaim the per-workspace Claude auth
overlays or ``_shared/claude-base`` itself; it persists a ``pending`` row and waits
for the worker. This module owns the claim → run-reaper → finish lifecycle for that
row, plus the stale-``running`` reclaim that recovers a row abandoned by a worker
cancelled mid-reap.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from awf.control.worker.cleanup import _log_terminal_gc_reap_summary
from awf.control.worker.config import effective_worker_config_node_id
from awf.control.worker.constants import (
    _SERVICE_GC_TRIGGER_CONSUME_FAILED_REASON_CODE,
    _SERVICE_GC_TRIGGER_STALE_RUNNING_RECLAIMED_REASON_CODE,
)
from awf.control.worker.logging import _log
from awf.db.repositories import ServiceGCRequestRepository
from awf.db.resilience import run_db_operation_with_retry
from awf.service.gc_worker_delegation import SERVICE_GC_WORKER_RECLAIM_FAILED


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

    Before claiming, abandoned ``running`` rows past their ``deadline_at`` are re-queued
    (:meth:`_reclaim_stale_running_service_gc_triggers`) so a row left ``running`` by a
    worker cancelled mid-reap is recovered instead of accumulating forever (#590). The
    reclaim is independently guarded: a reclaim failure must not skip the claim below.
    """
    if self._terminal_gc_reaper is None:
        return

    try:
        await self._reclaim_stale_running_service_gc_triggers()
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "worker.service_gc_trigger_stale_reclaim_failed",
            reason_code=_SERVICE_GC_TRIGGER_CONSUME_FAILED_REASON_CODE,
        )

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


async def _reclaim_stale_running_service_gc_triggers(self: Any) -> None:
    """Re-queue abandoned ``running`` gc-trigger rows past their ``deadline_at`` (#590).

    A worker cancelled (graceful shutdown) mid-reap leaves its claimed row ``running``;
    because :meth:`_claim_service_gc_trigger` (via ``claim_oldest_pending``) only selects
    ``pending`` rows, such a row has no recovery path and the stored ``deadline_at`` is
    never read — it would accumulate indefinitely. Once the row's ``deadline_at`` (the API
    client's polling budget) has elapsed the original claimant is gone, so resetting it to
    ``pending`` lets a later poll re-claim and re-run the idempotent reap: the operator's
    on-demand GC request is still honoured across a worker restart, even with the periodic
    backstop kill-switch off. The repository's ``FOR UPDATE SKIP LOCKED`` skips any row a
    concurrent claim/finish is actively holding, so a genuinely in-flight reap is never
    disturbed. Best-effort: a reclaim count is logged for evidence; failures are surfaced
    by the guarded caller, never here.
    """
    node_id = effective_worker_config_node_id(self._config)
    now = datetime.now(UTC)

    async def _operation(session: AsyncSession) -> list[str]:
        return await ServiceGCRequestRepository(session).reclaim_stale_running(
            node_id=node_id,
            now=now,
        )

    reclaimed = await run_db_operation_with_retry(
        self._session_factory,
        _operation,
        commit=True,
        on_retry=self._log_transient_db_retry,
    )
    if reclaimed:
        _log.warning(
            "worker.service_gc_trigger_stale_running_reclaimed",
            reason_code=_SERVICE_GC_TRIGGER_STALE_RUNNING_RECLAIMED_REASON_CODE,
            request_ids=reclaimed,
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
