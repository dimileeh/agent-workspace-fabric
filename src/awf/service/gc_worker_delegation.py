"""Fold the worker's capability-gated GC reap into the gc response (#582).

``awf service gc --execute`` runs in the API container, which lacks
``CAP_SYS_ADMIN`` and so reclaims **zero** of the dominant disk consumers — the
per-workspace Claude auth overlays (~1.7 GB each) and ``_shared/claude-base``.
The API delegates that reclaim to the worker (the only ``CAP_SYS_ADMIN`` context)
over a ``service_gc_requests`` row and folds the worker's actual reclaimed
bytes/paths back into the response with this pure helper, so the operator sees
real reclamation instead of ``deleted_path_count: 0``.

Kept pure (no I/O) so the summation / status-downgrade logic is exhaustively
unit-testable; the route owns the DB polling and heartbeat checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from awf.service.gc import CLEANUP_EXECUTION_PARTIAL

# Worker delegation reason codes (operator-facing, surfaced on the gc response and
# by ``awf service gc``). They flow API → ``worker_reclaim`` sub-object → CLI exit.
SERVICE_GC_WORKER_RECLAIMED = "SERVICE_GC_WORKER_RECLAIMED"
SERVICE_GC_WORKER_UNAVAILABLE = "SERVICE_GC_WORKER_UNAVAILABLE"
SERVICE_GC_WORKER_DELEGATION_TIMEOUT = "SERVICE_GC_WORKER_DELEGATION_TIMEOUT"
SERVICE_GC_WORKER_RECLAIM_FAILED = "SERVICE_GC_WORKER_RECLAIM_FAILED"

# The set of reason codes that mean "the worker could not be reached / did not
# finish / failed" — a non-success delegation the CLI must surface as a non-zero
# exit even though the API-side worktree/compose reclaim still happened.
SERVICE_GC_WORKER_DELEGATION_ERROR_REASON_CODES = frozenset(
    {
        SERVICE_GC_WORKER_UNAVAILABLE,
        SERVICE_GC_WORKER_DELEGATION_TIMEOUT,
        SERVICE_GC_WORKER_RECLAIM_FAILED,
    }
)

WorkerReclaimStatus = Literal["completed", "unavailable", "timeout", "failed"]


@dataclass(frozen=True)
class WorkerReclaimOutcome:
    """Structured result of delegating the capability-gated reap to the worker."""

    status: WorkerReclaimStatus
    reason_code: str
    deleted_path_count: int = 0
    total_estimated_bytes: int = 0
    worker_partial: bool = False
    message: str | None = None
    report: dict[str, object] | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "completed"

    @classmethod
    def from_report(cls, report: dict[str, object]) -> WorkerReclaimOutcome:
        """Build a success outcome from the worker's combined reap report."""
        return cls(
            status="completed",
            reason_code=SERVICE_GC_WORKER_RECLAIMED,
            deleted_path_count=_as_int(report.get("deleted_path_count")),
            total_estimated_bytes=_as_int(report.get("total_estimated_bytes")),
            worker_partial=report.get("status") == "partial",
            report=report,
        )

    @classmethod
    def unavailable(cls, message: str) -> WorkerReclaimOutcome:
        return cls(
            status="unavailable",
            reason_code=SERVICE_GC_WORKER_UNAVAILABLE,
            message=message,
        )

    @classmethod
    def delegation_timeout(cls, message: str) -> WorkerReclaimOutcome:
        return cls(
            status="timeout",
            reason_code=SERVICE_GC_WORKER_DELEGATION_TIMEOUT,
            message=message,
        )

    @classmethod
    def reclaim_failed(
        cls,
        message: str,
        *,
        report: dict[str, object] | None = None,
    ) -> WorkerReclaimOutcome:
        return cls(
            status="failed",
            reason_code=SERVICE_GC_WORKER_RECLAIM_FAILED,
            message=message,
            report=report,
        )

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": self.status,
            "reason_code": self.reason_code,
            "deleted_path_count": self.deleted_path_count,
            "total_estimated_bytes": self.total_estimated_bytes,
        }
        if self.message is not None:
            payload["message"] = self.message
        if self.report is not None:
            payload["report"] = self.report
        return payload


def fold_worker_reclaim(
    base: dict[str, object],
    outcome: WorkerReclaimOutcome,
) -> dict[str, object]:
    """Fold a worker reclaim outcome into the API-side gc result dict.

    On a successful delegation the worker's reclaimed ``deleted_path_count`` /
    ``total_estimated_bytes`` are **summed** onto the headline numbers (the
    API-side pass recorded the auth/claude-base paths as ``skipped``/0, so this is
    additive, never double-counted) and the ``worker_reclaim`` sub-object is
    attached. If the worker's own reap was ``partial`` the headline status is
    downgraded so the operator is not told a self-protected sweep fully succeeded.

    On a non-success delegation (worker unavailable / timeout / reclaim failed)
    the headline status is downgraded to ``partial`` with the delegation reason
    code — the run never reports success — while the API-side worktree/compose
    reclaim that already happened stays reported.
    """
    folded = dict(base)
    folded["worker_reclaim"] = outcome.to_dict()
    if outcome.succeeded:
        folded["deleted_path_count"] = (
            _as_int(base.get("deleted_path_count")) + outcome.deleted_path_count
        )
        folded["total_estimated_bytes"] = (
            _as_int(base.get("total_estimated_bytes")) + outcome.total_estimated_bytes
        )
        if outcome.worker_partial and base.get("status") == "succeeded":
            folded["status"] = "partial"
            report = outcome.report or {}
            folded["reason_code"] = report.get("reason_code") or CLEANUP_EXECUTION_PARTIAL
        return folded
    folded["status"] = "partial"
    folded["reason_code"] = outcome.reason_code
    return folded


def _as_int(value: object) -> int:
    """Coerce a possibly-missing JSON numeric field to ``int`` (defaulting to 0)."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0
