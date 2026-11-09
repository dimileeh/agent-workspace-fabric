"""``awf service status`` work-slot check (slot-release).

Reports slots in use, queued waiters and unsatisfiable waiters for the local
node, with the holder and waiter workspace IDs. Informational only: the check
is always ``ok`` so doctor, bootstrap and readiness never fail on queued work,
and a lookup failure degrades to an ``unavailable`` status instead of failing
the service.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError

from awf.common.audit import redact_audit_text
from awf.db.session import make_engine, make_session_factory
from awf.service.config import ServiceSettings
from awf.service.metrics_capacity import _work_slot_summary
from awf.service.metrics_types import WorkSlotSummary
from awf.service.node_identity import effective_service_node_id

WORK_SLOTS_OK_REASON = "WORK_SLOTS_OK"
WORK_SLOTS_UNAVAILABLE_REASON = "WORK_SLOTS_UNAVAILABLE"
_CHECK_TIMEOUT_SECONDS = 5.0

WorkSlotSummaryLookup = Callable[[ServiceSettings], Awaitable[WorkSlotSummary]]


async def collect_work_slot_status(
    settings: ServiceSettings,
    *,
    summary_lookup: WorkSlotSummaryLookup | None = None,
) -> dict[str, object]:
    lookup = summary_lookup or _default_work_slot_summary
    try:
        summary = await asyncio.wait_for(lookup(settings), timeout=_CHECK_TIMEOUT_SECONDS)
    except (SQLAlchemyError, OSError, TimeoutError, ValueError) as exc:
        return {
            "ok": True,
            "status": "unavailable",
            "reason": WORK_SLOTS_UNAVAILABLE_REASON,
            "detail": redact_audit_text(f"{type(exc).__name__}: {exc}")[:240],
        }
    queued = summary.waiting + summary.unsatisfiable
    return {
        "ok": True,
        "status": "waiting" if queued else "ok",
        "reason": summary.reason_code or WORK_SLOTS_OK_REASON,
        "detail": (
            f"{summary.in_use}/{summary.limit} work slots in use; "
            f"{summary.waiting} waiting; {summary.unsatisfiable} unsatisfiable"
        ),
        "limit": summary.limit,
        "in_use": summary.in_use,
        "available": summary.available,
        "waiting": summary.waiting,
        "unsatisfiable": summary.unsatisfiable,
        "adopted": summary.adopted,
        "cleanup_unproven": summary.cleanup_unproven,
        "oldest_wait_seconds": summary.oldest_wait_seconds,
        "holder_workspace_ids": list(summary.holder_workspace_ids),
        "waiting_workspace_ids": list(summary.waiting_workspace_ids),
        "unsatisfiable_workspace_ids": list(summary.unsatisfiable_workspace_ids),
    }


async def _default_work_slot_summary(settings: ServiceSettings) -> WorkSlotSummary:
    engine = make_engine(settings.database_url)
    try:
        async with make_session_factory(engine)() as session:
            return await _work_slot_summary(
                session,
                node_id=effective_service_node_id(settings),
                limit=settings.worker_max_concurrent_executions,
                now=datetime.now(UTC),
            )
    finally:
        await engine.dispose()
