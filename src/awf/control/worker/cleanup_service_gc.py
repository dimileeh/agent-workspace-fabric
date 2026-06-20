"""On-demand ``service_gc_requests`` trigger consumption for the worker (#582, #590).

Extracted from :mod:`awf.control.worker.cleanup` with behavior unchanged to keep that
module under the first-party line-count guardrail. The capability-less
``/v1/service/gc --execute`` API path cannot reclaim the per-workspace Claude auth
overlays or ``_shared/claude-base`` itself; it persists a ``pending`` row and waits
for the worker. This module owns the claim → run-reaper → finish lifecycle for that
row, plus the expire-on-timeout sweep that retires past-deadline rows (a never-claimed
``pending`` row, or a ``running`` row abandoned by a worker cancelled mid-reap) to the
terminal ``expired`` state so a timed-out reap never fires behind the operator's back.
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
    _SERVICE_GC_TRIGGER_STALE_EXPIRED_REASON_CODE,
)
from awf.control.worker.logging import _log
from awf.db.repositories import ServiceGCRequestRepository
from awf.db.resilience import run_db_operation_with_retry
from awf.service.gc import CLEANUP_EXECUTION_PARTIAL
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

    Before claiming, rows past their ``deadline_at`` are retired to the terminal
    ``expired`` state (:meth:`_expire_stale_service_gc_triggers`) — a never-claimed
    ``pending`` row or a ``running`` row a worker abandoned mid-reap — so a reap the
    operator was already told timed out never runs later behind their back, and the row
    does not accumulate forever (#590, expire-on-timeout). The sweep is independently
    guarded: an expire failure must not skip the claim below.
    """
    if self._terminal_gc_reaper is None:
        return

    try:
        await self._expire_stale_service_gc_triggers()
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "worker.service_gc_trigger_stale_expire_failed",
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


async def _expire_stale_service_gc_triggers(self: Any) -> None:
    """Retire past-deadline gc-trigger rows to the terminal ``expired`` state (#590).

    Expire-on-timeout. Once a row's ``deadline_at`` (the API client's polling budget) has
    elapsed the operator has already been told the trigger timed out, so the reap must
    **not** run later behind their back — a destructive reap after a reported timeout is
    the hazard this rejects. Both a never-claimed ``pending`` row (which a later
    poll/restart would otherwise pick up and run with the *current* clock + stored
    filters) and a ``running`` row a worker abandoned mid-reap on shutdown (which
    :meth:`_claim_service_gc_trigger`, via ``claim_oldest_pending``, never re-selects and
    so would accumulate forever) are marked ``expired`` instead. The worker's periodic
    (~1h) interval reaper remains the durable backstop that performs the actual disk
    reclaim. The repository's ``FOR UPDATE SKIP LOCKED`` skips any row a concurrent
    claim/finish is actively holding. Best-effort: an expired count is logged for
    evidence; failures are surfaced by the guarded caller, never here.
    """
    node_id = effective_worker_config_node_id(self._config)
    now = datetime.now(UTC)

    async def _operation(session: AsyncSession) -> list[str]:
        return await ServiceGCRequestRepository(session).expire_stale_requests(
            node_id=node_id,
            now=now,
        )

    expired = await run_db_operation_with_retry(
        self._session_factory,
        _operation,
        commit=True,
        on_retry=self._log_transient_db_retry,
    )
    if expired:
        _log.warning(
            "worker.service_gc_trigger_stale_expired",
            reason_code=_SERVICE_GC_TRIGGER_STALE_EXPIRED_REASON_CODE,
            request_ids=expired,
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
    periodic backstop). ``CancelledError`` propagates; any reaper failure — *and* a
    failure to parse the stored params (e.g. an unparseable ``now``, which raises
    ``ValueError`` from ``datetime.fromisoformat``) — is recorded on the row so the
    polling API does not hang on a row stuck ``running`` until ``deadline_at`` and never
    reports false success (PRRT_kwDOSJAM6s6JdSy-). Param parsing therefore lives *inside*
    the guarded block, not before it.

    After the DB-row-driven terminal reaper, the same guarded run also drives the
    classification-driven orphan reaper with ``enabled=True`` *forced* (regardless of the
    default-off ``auto_cleanup_orphans`` flag) and ``row_less_only=True`` so this
    operator-requested ``gc`` reclaims only no-DB-record ("row-less") orphaned
    volumes/worktrees the DB-row-driven candidate set can never see (#637). ``row_less_only``
    keeps the additive sweep from tearing down a terminal workspace the operator scoped out
    via ``--status``/``--exclude-status`` (PRRT_kwDOSJAM6s6LB30p): those terminal workspaces
    have DB rows and are already reaped by the scope-honouring ``_terminal_gc_reaper`` above,
    whereas row-less orphans have no status to scope on. The operator's ``--limit`` is
    threaded into the sweep as well so it is bounded to that many oldest-first row-less
    workspaces — restoring the ``--limit`` blast-radius parity the terminal reaper already
    honours rather than reaping every aged orphan in one pass (PRRT_kwDOSJAM6s6LCCJZ). Its
    ``OrphanReapResult`` is folded
    into the combined report under ``classified_orphan_reap`` (flowing into the API's
    ``worker_reclaim.report``), and a non-raising ``partial`` sweep (a compose-teardown /
    worktree-delete error surfaced as a status rather than an exception) also downgrades the
    *top-level* combined ``status`` to ``partial`` — the worker-delegation fold derives
    ``worker_partial`` from that top-level status, so without the downgrade an orphan-sweep
    failure would still report a successful ``service gc --execute`` (PRRT_kwDOSJAM6s6LB30q).
    The fold sits inside the same ``try`` so an orphan-sweep raise is recorded on the row like
    any reaper failure rather than surfacing a false success; it is skipped when no orphan
    reaper is wired (back-compat).
    """
    try:
        reaper_kwargs: dict[str, Any] = {}
        min_age_hours = params.get("min_age_hours")
        if min_age_hours is not None:
            reaper_kwargs["min_age_hours"] = min_age_hours
        limit = params.get("limit")
        if limit is not None:
            reaper_kwargs["limit"] = limit
        # The API persists its retention cutoff anchor (``datetime.now(UTC)`` at
        # invocation) as an ISO string so the worker derives the *same* ``cutoff_at``
        # the API-side pass used instead of recomputing it from the (minutes-later)
        # claim clock — otherwise a workspace just under ``--min-age-hours`` at
        # invocation could age into eligibility and be reaped though no plan/dry-run
        # listed it (PRRT_kwDOSJAM6s6JbriQ).
        now = params.get("now")
        if now is not None:
            reaper_kwargs["now"] = datetime.fromisoformat(now)
        statuses = params.get("statuses")
        if statuses:
            reaper_kwargs["statuses"] = statuses
        exclude_statuses = params.get("exclude_statuses")
        if exclude_statuses:
            reaper_kwargs["exclude_statuses"] = exclude_statuses

        report = await self._terminal_gc_reaper(**reaper_kwargs)
        # Additive on-demand sweep of no-DB-record orphans (#637). ``enabled=True`` is
        # forced for the explicit operator request; the same retention/min-age scope the
        # closure already passes is reused. ``row_less_only=True`` restricts the sweep to
        # row-less ("missing") orphans so it never tears down a terminal workspace the
        # operator scoped out via ``--status``/``--exclude-status`` — those terminal rows
        # are already handled by the scope-honouring ``_terminal_gc_reaper`` above, and the
        # status filters are not (and need not be) threaded into a row-less-only sweep
        # (PRRT_kwDOSJAM6s6LB30p). The operator's ``--limit`` (the same ``limit`` already
        # threaded into the terminal reaper above) is forwarded too so the additive sweep
        # is bounded to that many oldest-first row-less workspaces rather than tearing down
        # every aged orphan in one pass — restoring ``--limit`` parity across both passes
        # (PRRT_kwDOSJAM6s6LCCJZ); ``None`` (no ``--limit``) stays unbounded. Guarded on the
        # dependency being wired so the DB-only gc path stays unchanged when no orphan
        # reaper is present.
        if self._classified_orphan_reaper is not None:
            orphan_result = await self._classified_orphan_reaper(
                enabled=True, row_less_only=True, limit=limit
            )
            report = {**report, "classified_orphan_reap": orphan_result.to_dict()}
            # A non-raising ``partial`` orphan reap (a compose teardown / worktree delete
            # error surfaced as ``PATH_DELETE_PERMISSION_DENIED`` rather than an exception)
            # must downgrade the *top-level* combined status, not only nest under
            # ``classified_orphan_reap``. The worker-delegation fold derives
            # ``worker_partial`` from ``report["status"]`` (``WorkerReclaimOutcome.from_report``),
            # so without this an orphan-sweep failure would still report a successful
            # ``service gc --execute`` (PRRT_kwDOSJAM6s6LB30q). Mirror ``combine_terminal_gc_
            # reports``' "partial wins" rule: only ever downgrade, never upgrade an
            # already-partial terminal report.
            if orphan_result.status == "partial":
                report["status"] = "partial"
                report["reason_code"] = CLEANUP_EXECUTION_PARTIAL
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
