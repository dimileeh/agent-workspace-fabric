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
        """Build a success outcome from the worker's combined reap report.

        The worker's ``run_service_workspace_gc`` report records its
        ``_shared/claude-base`` reclaim under the nested ``claude_base_reap.reaped``
        payload, *outside* the top-level ``deleted_path_count``/``deleted_paths`` (those
        cover only the per-workspace candidates). A worker pass that reaps only
        superseded bases — for example no per-workspace auth candidates but stale bases
        exist — would otherwise surface ``deleted_path_count: 0`` and hide a GB-scale
        shared-base reclaim from the operator. So the reaped bases are folded into the
        count here (and, via ``_worker_reclaimed_path_strs``, into the merged
        ``deleted_paths``) so ``worker_reclaim`` and the headline report the real
        reclamation (PRRT_kwDOSJAM6s6JbAow). ``total_estimated_bytes`` likewise sums the
        top-level workspace-candidate total with the reaped bases' on-disk size (now that
        the reaper measures each base before removal), so a base-only reap no longer
        reports ``total_estimated_bytes: 0`` next to GB-scale deletions
        (PRRT_kwDOSJAM6s6Jcixk).
        """
        return cls(
            status="completed",
            reason_code=SERVICE_GC_WORKER_RECLAIMED,
            deleted_path_count=(
                _as_int(report.get("deleted_path_count"))
                + len(_claude_base_reaped_path_strs(report))
            ),
            total_estimated_bytes=(
                _as_int(report.get("total_estimated_bytes"))
                + _claude_base_reaped_estimated_bytes(report)
            ),
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


def bounded_worker_deadline_seconds(deadline_seconds: float, api_elapsed_seconds: float) -> float:
    """Shrink the worker-delegation deadline by the API-phase time already spent.

    ``awf service gc --execute`` budgets its HTTP read timeout at
    ``2 * timeout_seconds + 30`` (CLI), allotting one ``timeout_seconds`` to the
    API-side worktree/compose reclaim and one to the worker-delegation wait. But the
    API-side pass has no server-side cap, so a reclaim that runs longer than
    ``timeout_seconds`` would push the *total* server time (API phase + the full
    worker deadline) past the client budget — httpx aborts before the structured
    ``worker_reclaim`` response returns, exactly the false timeout the buffer exists
    to avoid (PR #590 review, comment 4493565124). Cap the worker deadline so the
    total server time stays within ``2 * deadline_seconds`` (the client's two-phase
    budget, leaving its 30s settle margin for the final response): the worker keeps
    its full ``deadline_seconds`` while the API phase fits its own budget, and only
    shrinks once the API phase has eaten into the second budget. Never returns a
    negative budget; once the API phase alone exceeds ``2 * deadline_seconds`` (the
    client has already aborted) the worker deadline floors at 0 so the route returns
    a structured timeout rather than waiting on a budget the operator can no longer
    observe.
    """
    budget = max(0.0, deadline_seconds)
    remaining = 2.0 * budget - max(0.0, api_elapsed_seconds)
    return max(0.0, min(budget, remaining))


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

    ``total_estimated_bytes`` sums only the worker's **net-new** bytes — the
    workspaces it reaped that the API plan never estimated. The base value is the
    API GC *plan* total, a directory-scan estimate that already includes the
    auth-dir estimate for its own default-policy candidates even though those paths
    were skipped at execution time; the worker re-estimates those same dirs, so
    blindly adding its whole plan total would double-count ~1.7GB per overlapping
    workspace. But the worker also runs a discarded-status augmentation pass
    (cancelled/destroyed rows the API default policy omits — see
    ``_combine_terminal_gc_reports``); those workspaces are absent from the API plan,
    so their estimate is net-new and must reach the headline — otherwise a run that
    reaped only discarded auth dirs reports GB-scale ``deleted_path_count`` next to a
    plan total of 0 (PRRT_kwDOSJAM6s6JbHKg). ``_worker_only_estimated_bytes`` adds
    exactly the worker candidates whose ``workspace_id`` the API plan never carried;
    the worker's own full estimate stays visible on ``worker_reclaim``.

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
        # Merge the worker's actually-removed paths into the headline list so the
        # ``deleted_path_count == len(deleted_paths)`` invariant of
        # ``WorkspaceGCResult.to_dict()`` survives the fold. The API-side pass recorded
        # these auth/claude-base paths as ``skipped`` (never in ``deleted_paths``), so
        # they are net-new and never duplicate an existing entry; build a fresh list so
        # the caller's ``base`` stays untouched.
        base_deleted = base.get("deleted_paths")
        merged_paths: list[object] = list(base_deleted) if isinstance(base_deleted, list) else []
        merged_paths.extend(_worker_reclaimed_path_strs(outcome))
        folded["deleted_paths"] = merged_paths
        # Add the bytes of workspaces the worker reaped that the API plan never
        # estimated (the discarded-status augmentation pass — cancelled/destroyed
        # rows the API default policy omits), plus the reaped shared ``claude-base``
        # dirs (which the API plan never estimates — they live outside the
        # per-workspace candidates). Without this the headline reports GB-scale
        # ``deleted_path_count`` next to the bare API plan total — often 0 — so callers
        # see GB deletions with 0 bytes reclaimed (PRRT_kwDOSJAM6s6JbHKg /
        # PRRT_kwDOSJAM6s6Jcixk).
        worker_report = outcome.report or {}
        folded["total_estimated_bytes"] = (
            _as_int(base.get("total_estimated_bytes"))
            + _worker_only_estimated_bytes(base, worker_report)
            + _claude_base_reaped_estimated_bytes(worker_report)
        )
        # The fold reconciles the headline (``deleted_paths``, ``delete_errors``,
        # status, nested ``claude_base_reap``), but the already-serialized
        # ``candidates[*].paths.auth`` entry still reads ``status: skipped,
        # deleted: false`` from the API-side skip. Rewrite each candidate auth entry
        # the worker actually removed so consumers auditing per-candidate outcomes
        # are not told a reclaimed auth dir was preserved (PRRT_kwDOSJAM6s6JbLTR).
        reclaimed = _worker_reclaimed_paths(outcome)
        _reconcile_worker_reclaimed_auth_candidates(folded, reclaimed)
        # Fold the worker-only candidates (the discarded-status augmentation pass —
        # cancelled/destroyed rows the API default policy omits) into the headline
        # ``candidates``/``candidate_count``. The headline byte/path totals already
        # include them, but without this the candidate list and count stay API-only: a
        # default ``--execute`` run that reaped only an old cancelled workspace reports
        # its deleted auth paths and GB-scale bytes next to ``candidate_count: 0`` and an
        # empty candidate list, contradicting the dry-run preview that lists that same
        # candidate (PRRT_kwDOSJAM6s6Jcixj).
        _fold_worker_only_candidates(folded, base, outcome.report or {})
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
            _drop_worker_reclaimed_auth_skips(folded, reclaimed)
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


def _worker_reclaimed_path_strs(outcome: WorkerReclaimOutcome) -> list[str]:
    """The worker's actually-deleted paths as an ordered list of strings.

    Combines the top-level ``deleted_paths`` (per-workspace candidates) with the
    nested ``claude_base_reap`` reaped bases, which the worker removes but records
    outside the top-level totals (see ``_claude_base_reaped_path_strs``). Merged into
    the folded headline ``deleted_paths`` so the
    ``deleted_path_count == len(deleted_paths)`` invariant survives the fold (the count
    in ``from_report`` includes both), and reused (as a set) to prove which API-side
    auth-unmount skips a worker reap superseded.
    """
    report = outcome.report or {}
    paths: list[str] = []
    deleted = report.get("deleted_paths")
    if isinstance(deleted, list):
        paths.extend(str(path) for path in deleted)
    paths.extend(_claude_base_reaped_path_strs(report))
    return paths


def _claude_base_reaped_path_strs(report: dict[str, object]) -> list[str]:
    """The shared ``claude-base`` signature dirs the worker actually reaped, as paths.

    ``run_service_workspace_gc`` records its ``_shared/claude-base`` reclaim under the
    nested ``claude_base_reap`` object (``reaped`` lists the removed ``<signature>`` dir
    names, ``base_root`` the dir that holds them) rather than in the top-level
    ``deleted_paths``. Reconstruct ``<base_root>/<signature>`` for each reaped base so
    the count and the merged headline ``deleted_paths`` reflect bases the worker removed
    that the top-level totals omit — otherwise a base-only reap reads as 0 reclaimed
    (PRRT_kwDOSJAM6s6JbAow). Falls back to bare signature names if the report carries no
    ``base_root``. Only the ``reaped`` (actually-removed) list counts — ``planned`` /
    ``protected`` / ``unverifiable`` bases were not deleted.
    """
    claude_base = report.get("claude_base_reap")
    if not isinstance(claude_base, Mapping):
        return []
    reaped = claude_base.get("reaped")
    if not isinstance(reaped, list):
        return []
    base_root = claude_base.get("base_root")
    if isinstance(base_root, str) and base_root:
        prefix = base_root.rstrip("/")
        return [f"{prefix}/{signature}" for signature in reaped]
    return [str(signature) for signature in reaped]


def _claude_base_reaped_estimated_bytes(report: dict[str, object]) -> int:
    """On-disk bytes of the shared ``claude-base`` dirs the worker actually reaped.

    ``reap_superseded_claude_bases`` measures each base before ``rmtree`` and records the
    summed size under the nested ``claude_base_reap.reaped_estimated_bytes`` — outside the
    top-level ``total_estimated_bytes`` (which covers only the per-workspace candidates).
    Surfaced here so both ``worker_reclaim.total_estimated_bytes`` and the folded headline
    reflect the GB-scale shared-base reclaim a base-only reap would otherwise report as 0
    bytes (PRRT_kwDOSJAM6s6Jcixk). A missing or non-numeric field contributes 0.
    """
    claude_base = report.get("claude_base_reap")
    if not isinstance(claude_base, Mapping):
        return 0
    return _as_int(claude_base.get("reaped_estimated_bytes"))


def _worker_reclaimed_paths(outcome: WorkerReclaimOutcome) -> frozenset[str]:
    """The paths the worker actually deleted, per its reap report.

    Used to prove which API-side auth-unmount skips a *partial* worker reap
    nonetheless superseded — a full reclaim drops them wholesale, a partial one
    only drops what it can prove it removed.
    """
    return frozenset(_worker_reclaimed_path_strs(outcome))


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


def _reconcile_worker_reclaimed_auth_candidates(
    folded: dict[str, object], reclaimed_paths: frozenset[str]
) -> None:
    """Rewrite per-candidate ``auth`` path entries a worker reclaim proved deleted.

    The fold merges the worker-reclaimed auth dirs into the headline
    ``deleted_paths`` and drops the stale auth-unmount ``delete_errors``, but each
    ``candidates[*].paths.auth`` entry was serialized API-side as ``status:
    skipped, deleted: false`` (the capability-less API container could not unmount
    the overlay). Left as-is, a consumer auditing per-candidate outcomes is told the
    reclaimed auth dir was preserved while the headline says it was deleted. For
    every candidate whose ``auth`` path the worker actually removed, rewrite that
    entry to a deleted outcome (and mark ``reconciled_by_worker``, mirroring the
    nested claude-base reconciliation). Candidates are copied before mutation so the
    caller's ``base`` stays untouched; entries the worker did not reclaim, and all
    non-auth paths, are left verbatim.
    """
    if not reclaimed_paths:
        return
    candidates = folded.get("candidates")
    if not isinstance(candidates, list):
        return
    reconciled: list[object] = []
    changed = False
    for candidate in candidates:
        updated = _reconcile_candidate_auth(candidate, reclaimed_paths)
        changed = changed or updated is not candidate
        reconciled.append(updated)
    if changed:
        folded["candidates"] = reconciled


def _reconcile_candidate_auth(candidate: object, reclaimed_paths: frozenset[str]) -> object:
    """Return a candidate with its ``auth`` entry reconciled, or the original unchanged.

    Returns the same object when there is nothing to reconcile (not a mapping, no
    ``auth`` path, already deleted, or the worker did not reclaim that path) so the
    caller can detect whether a fresh list is needed.
    """
    if not isinstance(candidate, Mapping):
        return candidate
    paths = candidate.get("paths")
    if not isinstance(paths, Mapping):
        return candidate
    auth = paths.get("auth")
    if not isinstance(auth, Mapping):
        return candidate
    if auth.get("deleted") or str(auth.get("path")) not in reclaimed_paths:
        return candidate
    new_auth = dict(auth)
    new_auth["deleted"] = True
    new_auth["status"] = "deleted"
    new_auth["reason_code"] = SERVICE_GC_WORKER_RECLAIMED
    new_auth["reconciled_by_worker"] = True
    # The unmount error is stale now that the worker removed the dir; drop it so the
    # per-candidate entry does not still carry the "could not unmount" message.
    new_auth.pop("error", None)
    new_paths = dict(paths)
    new_paths["auth"] = new_auth
    new_candidate = dict(candidate)
    new_candidate["paths"] = new_paths
    return new_candidate


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
    # A fallback compose teardown failure is recorded only under
    # ``compose_teardowns`` (the single-workspace fallback path never enters the
    # delete-paths loop that mirrors candidate teardowns into ``delete_errors`` —
    # see ``_gc_result``/``_delete_gc_plan_paths``), yet it still drives the run
    # ``partial``. Inspect that map too so a failed teardown the worker does not own
    # keeps the run partial instead of being silently promoted to success — mirroring
    # ``WorkspaceGCComposeTeardownResult.ok`` (only ``succeeded``/``skipped`` pass).
    teardowns = folded.get("compose_teardowns")
    if isinstance(teardowns, Mapping) and any(
        isinstance(teardown, Mapping) and teardown.get("status") not in {"succeeded", "skipped"}
        for teardown in teardowns.values()
    ):
        return True
    reservations = folded.get("reservation_releases")
    if isinstance(reservations, Mapping):
        return any(
            isinstance(release, Mapping) and release.get("error") is not None
            for release in reservations.values()
        )
    return False


def _worker_only_estimated_bytes(base: dict[str, object], report: dict[str, object]) -> int:
    """Estimated bytes for workspaces the worker reaped that the API plan never estimated.

    The headline ``total_estimated_bytes`` carries the API GC *plan* total, which
    already estimates the auth dirs of its own default-policy candidates. The worker
    re-estimates those same workspaces, so summing its whole total double-counts them.
    The worker's discarded-status augmentation pass, however, reaps cancelled/destroyed
    workspaces the API default policy never classified (see
    ``_combine_terminal_gc_reports``); those carry no API plan estimate, so their bytes
    are net-new. Identify them by ``workspace_id`` — any worker-report candidate absent
    from the API plan's candidate set — and sum each one's estimated ``total`` so the
    headline reflects the real GB-scale reclaim instead of the bare plan total (often 0
    when the API default pass had no eligible candidates). Missing/garbled candidate
    payloads contribute 0, so a worker report without a candidate breakdown leaves the
    base plan total untouched (the conservative no-double-count default).
    """
    base_ids = {
        candidate.get("workspace_id")
        for candidate in _candidate_dicts(base)
        if candidate.get("workspace_id") is not None
    }
    total = 0
    for candidate in _candidate_dicts(report):
        if candidate.get("workspace_id") in base_ids:
            continue
        estimated = candidate.get("estimated_bytes")
        if isinstance(estimated, Mapping):
            total += _as_int(estimated.get("total"))
    return total


def _fold_worker_only_candidates(
    folded: dict[str, object], base: dict[str, object], report: dict[str, object]
) -> None:
    """Append candidates the worker reaped that the API plan never carried.

    The headline ``candidates``/``candidate_count`` come from the API default-policy
    pass only. The worker's discarded-status augmentation pass reaps cancelled/destroyed
    workspaces the API plan never classified (see ``_combine_terminal_gc_reports``), so
    their entries are net-new and must reach the headline — otherwise a default
    ``--execute`` run that reaped only a cancelled workspace reports GB-scale deleted
    paths/bytes alongside ``candidate_count: 0`` and an empty candidate list while the
    dry-run preview lists that same candidate (PRRT_kwDOSJAM6s6Jcixj). Identified by
    ``workspace_id`` (any worker candidate absent from the API plan's set), mirroring
    ``_worker_only_estimated_bytes`` so an API-planned candidate is never duplicated;
    candidates without a ``workspace_id`` are skipped (cannot prove they are net-new). A
    fresh list is built so the caller's ``base`` stays untouched.
    """
    base_ids = {
        candidate.get("workspace_id")
        for candidate in _candidate_dicts(base)
        if candidate.get("workspace_id") is not None
    }
    worker_only = [
        dict(candidate)
        for candidate in _candidate_dicts(report)
        if candidate.get("workspace_id") is not None
        and candidate.get("workspace_id") not in base_ids
    ]
    if not worker_only:
        return
    existing = folded.get("candidates")
    merged: list[object] = list(existing) if isinstance(existing, list) else []
    merged.extend(worker_only)
    folded["candidates"] = merged
    folded["candidate_count"] = len(merged)


def _candidate_dicts(payload: dict[str, object]) -> list[Mapping[str, object]]:
    """The candidate entries of a gc report/plan dict, skipping non-mapping noise."""
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return []
    return [candidate for candidate in candidates if isinstance(candidate, Mapping)]


def _as_int(value: object) -> int:
    """Coerce a possibly-missing JSON numeric field to ``int`` (defaulting to 0)."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0
