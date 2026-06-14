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

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from awf.service.gc import CLEANUP_EXECUTION_PARTIAL, CLEANUP_EXECUTION_SUCCEEDED

# API-side auth-overlay unmount skip reason codes. The API container lacks
# ``CAP_SYS_ADMIN`` and so cannot release the worker's overlay mount; that records
# a ``delete_errors`` entry which forces the whole run ``partial``. A *completed*
# worker reclaim (the worker alone holds ``CAP_SYS_ADMIN``) actually removes those
# auth dirs, so these specific failures become stale and are dropped on fold.
# Kept as bare literals — mirrors the CLI's ``_OVERLAY_UNMOUNT_REASON_CODES`` —
# so this pure helper does not import the node layer.
_AUTH_OVERLAY_UNMOUNT_RECONCILED_REASON_CODES = frozenset(
    {
        "CLAUDE_AUTH_OVERLAY_UNMOUNT_INCAPABLE",
        "CLAUDE_AUTH_OVERLAY_UNMOUNT_FAILED",
    }
)

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

    On a successful delegation the worker's reclaimed ``deleted_path_count`` is
    **summed** onto the headline count — the API-side pass recorded the
    auth/claude-base paths as ``skipped`` (never in ``deleted_paths``), so the
    worker's actual deletions are net-new and never double-counted — and the
    ``worker_reclaim`` sub-object is attached. If the worker's own reap was
    ``partial`` the headline status is downgraded so the operator is not told a
    self-protected sweep fully succeeded.

    ``total_estimated_bytes`` is **not** summed: the base value is the API GC
    *plan* total, a directory-scan estimate that already includes the auth-dir
    estimate even though that path was skipped at execution time. The worker
    re-estimates the same auth dir it removes, so adding its plan total would
    double-count those bytes (~1.7GB per workspace). The headline keeps the base
    plan total; the worker's own estimate stays visible on ``worker_reclaim``.

    When the worker fully reclaimed the capability-gated auth overlays +
    claude-base, the API-side skip failures for *those* paths are stale: the
    auth-unmount ``delete_errors`` are dropped and a headline that was ``partial``
    *solely* because of those skips is restored to ``succeeded`` — otherwise a
    complete reclaim would still warn the auth dir was preserved and exit non-zero.
    Unrelated API failures are preserved (see ``_reconcile_worker_reclaimed_skips``).

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
        if outcome.worker_partial:
            # The worker's own reap was partial — it leaked disk it could not
            # reclaim, so a previously-clean run must not still read as success and
            # the API-side auth/claude-base skips may genuinely remain unreclaimed.
            # Only ever downgrade here; never upgrade an already-partial base.
            if base.get("status") == "succeeded":
                folded["status"] = "partial"
                report = outcome.report or {}
                folded["reason_code"] = report.get("reason_code") or CLEANUP_EXECUTION_PARTIAL
            # Even on a partial worker reap, any auth dir the worker *did* reclaim
            # (proven by its ``deleted_paths``) makes the API-side auth-unmount skip
            # stale: drop those specific skips so the CLI does not warn the dir was
            # preserved although it was reclaimed, while keeping the run partial for
            # the unrelated failure that drove the worker partial.
            _drop_worker_reclaimed_auth_skips(folded, _worker_reclaimed_paths(outcome))
            return folded
        _reconcile_worker_reclaimed_skips(folded)
        return folded
    folded["status"] = "partial"
    folded["reason_code"] = outcome.reason_code
    return folded


def _reconcile_worker_reclaimed_skips(folded: dict[str, object]) -> None:
    """Drop API-side auth/claude-base skip failures a completed worker reclaim supersedes.

    When the API container lacks ``CAP_SYS_ADMIN`` it records the auth-overlay
    unmount as a ``delete_errors`` entry (and the claude-base reap as ``partial``),
    forcing the whole run ``partial``. A completed worker reclaim actually removed
    those paths, so those specific failures are stale: remove the auth-unmount
    ``delete_errors`` and, when no *other* failure kept the run partial, restore the
    headline success. Unrelated API failures — compose teardown, worktree remove,
    reservation release, companion image prune, and non-auth path deletes — are
    preserved verbatim so a genuinely partial run never reads as a clean success.
    """
    if folded.get("status") != "partial":
        return
    removed_auth_skip = False
    remaining: list[object] = []
    errors = folded.get("delete_errors")
    if isinstance(errors, list):
        for error in errors:
            if (
                isinstance(error, Mapping)
                and error.get("reason_code") in _AUTH_OVERLAY_UNMOUNT_RECONCILED_REASON_CODES
            ):
                removed_auth_skip = True
                continue
            remaining.append(error)
        folded["delete_errors"] = remaining
    claude_base = folded.get("claude_base_reap")
    claude_base_partial = (
        isinstance(claude_base, Mapping) and claude_base.get("status") == "partial"
    )
    if not (removed_auth_skip or claude_base_partial):
        # The partial was driven by something the worker does not own; leave it.
        return
    if _has_unreconciled_failure(folded, remaining):
        return
    folded["status"] = "succeeded"
    folded["reason_code"] = CLEANUP_EXECUTION_SUCCEEDED
    if isinstance(claude_base, Mapping) and claude_base_partial:
        # The worker actually reclaimed claude-base, so the API-side ``partial``
        # reap is stale. Reconcile the nested object too — otherwise callers see
        # a ``succeeded`` headline next to ``claude_base_reap.status: partial``.
        # Copy before mutating so the caller's ``base`` is left untouched, and
        # keep the original diagnostic lists while recording the supersession.
        reconciled = dict(claude_base)
        reconciled["status"] = "succeeded"
        reconciled["reason_code"] = SERVICE_GC_WORKER_RECLAIMED
        reconciled["reconciled_by_worker"] = True
        folded["claude_base_reap"] = reconciled


def _worker_reclaimed_paths(outcome: WorkerReclaimOutcome) -> frozenset[str]:
    """The paths the worker actually deleted, per its reap report.

    Used to prove which API-side auth-unmount skips a *partial* worker reap
    nonetheless superseded — a full reclaim drops them wholesale, a partial one
    only drops what it can prove it removed.
    """
    report = outcome.report or {}
    deleted = report.get("deleted_paths")
    if isinstance(deleted, list):
        return frozenset(str(path) for path in deleted)
    return frozenset()


def _drop_worker_reclaimed_auth_skips(
    folded: dict[str, object], reclaimed_paths: frozenset[str]
) -> None:
    """Drop auth-unmount skip errors for paths a partial worker reap proved reclaimed.

    On a partial worker reap the headline must stay partial for the unrelated
    failure, but any auth dir whose path appears in the worker's ``deleted_paths``
    was genuinely removed — its API-side ``CLAUDE_AUTH_OVERLAY_UNMOUNT_*``
    ``delete_errors`` entry is stale and would otherwise make the CLI warn the auth
    dir was preserved. Auth skips for paths the worker did *not* reclaim, and every
    non-auth failure, are preserved verbatim.
    """
    if not reclaimed_paths:
        return
    errors = folded.get("delete_errors")
    if not isinstance(errors, list):
        return
    folded["delete_errors"] = [
        error
        for error in errors
        if not (
            isinstance(error, Mapping)
            and error.get("reason_code") in _AUTH_OVERLAY_UNMOUNT_RECONCILED_REASON_CODES
            and error.get("path") in reclaimed_paths
        )
    ]


def _has_unreconciled_failure(
    folded: dict[str, object],
    remaining_delete_errors: list[object],
) -> bool:
    """Whether a failure the worker reclaim does *not* supersede keeps the run partial."""
    if remaining_delete_errors:
        return True
    companion = folded.get("companion_image_prune")
    if isinstance(companion, Mapping) and companion.get("status") == "failed":
        return True
    reservations = folded.get("reservation_releases")
    if isinstance(reservations, Mapping):
        return any(
            isinstance(release, Mapping) and release.get("error") is not None
            for release in reservations.values()
        )
    return False


def _as_int(value: object) -> int:
    """Coerce a possibly-missing JSON numeric field to ``int`` (defaulting to 0)."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0
