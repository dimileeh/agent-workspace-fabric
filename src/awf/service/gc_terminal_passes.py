"""Two-pass terminal-workspace GC scope + report folding (worker + API, #513/#590).

The worker runs terminal-workspace GC twice — the conservative default-policy pass
(completed/failed/superseded) plus an explicit ``include_statuses`` pass for the
cancelled/destroyed rows that policy never classifies (#513) — so a single helper
resolves *which* discarded statuses the augmentation pass covers and *how* the two
reports fold into one summary.

Both the worker's ``--execute`` reaper and the API ``--execute`` *dry-run preview*
share this module so the ``awf service gc`` plan lists exactly what ``--execute``
reaps. Previewing only the default pass would let ``--execute`` delete an old
cancelled workspace's auth dir the dry-run reported as ``0`` candidates, breaking the
plan-before-delete contract (PRRT_kwDOSJAM6s6JbN6x). Kept here (not in the heavy
``worker`` wiring module) so the capability-less API route can import it without
pulling in the node/control runtime stack.
"""

from __future__ import annotations

from typing import cast

from awf.db.enums import WorkspaceStatus
from awf.service.gc import CLEANUP_EXECUTION_PARTIAL


def terminal_gc_discarded_statuses(
    requested_statuses: tuple[str, ...] | None,
    excluded_statuses: tuple[str, ...] | None,
) -> tuple[str, ...]:
    """Cancelled/destroyed statuses the default-policy pass omits, honouring operator scope.

    The conservative default policy only classifies completed/failed/superseded, so the
    worker runs a second explicit pass to reap the discarded cancelled/destroyed rows whose
    ~1.7 GB auth dirs would otherwise leak (#513). Two ``awf service gc`` scope cases narrow
    that augmentation so the worker never reclaims auth dirs the operator's flags excluded
    (#590): an explicit ``--status`` set means the first pass already covers exactly the
    requested terminal statuses (cancelled/destroyed included when asked for), so no
    augmentation is needed and this returns empty; ``--exclude-status`` removes those
    statuses from the augmentation set.
    """
    if requested_statuses is not None:
        return ()
    excluded = set(excluded_statuses or ())
    return tuple(
        status
        for status in (WorkspaceStatus.cancelled.value, WorkspaceStatus.destroyed.value)
        if status not in excluded
    )


def combine_terminal_gc_reports(
    default_report: dict[str, object], discarded_report: dict[str, object]
) -> dict[str, object]:
    """Fold the discarded-status (cancelled/destroyed) GC pass into the default pass.

    The worker runs the terminal-workspace GC twice — once under the conservative
    default policy and once with an explicit ``include_statuses`` for the
    cancelled/destroyed rows that policy never classifies (#513) — and the cleanup
    loop logs a single summary, so the two reports are merged here. The passes act on
    disjoint status sets and never reclaim the same path, so deleted paths /
    candidates / delete-errors concatenate and preserved counts and byte estimates
    sum. ``total_estimated_bytes`` in particular must add both passes' totals (and
    not keep only the default pass's, as ``dict(default_report)`` would): unlike the
    API-side ``fold_worker_reclaim`` — where the base is one plan total that already
    estimated the skipped auth dir — here each pass estimates only its own disjoint
    dirs, so the discarded pass's GB-scale auth reclaim is net-new bytes. A
    ``partial`` from either pass wins (it leaked disk it could not reclaim), so a
    self-protected sweep is never masked behind the other's clean success.
    """
    combined = dict(default_report)
    for key in ("deleted_paths", "candidates", "delete_errors"):
        first = cast("list[object]", default_report.get(key) or [])
        second = cast("list[object]", discarded_report.get(key) or [])
        combined[key] = [*first, *second]
    combined["deleted_path_count"] = len(cast("list[object]", combined["deleted_paths"]))
    combined["candidate_count"] = len(cast("list[object]", combined["candidates"]))
    combined["preserved_count"] = cast("int", default_report.get("preserved_count") or 0) + cast(
        "int", discarded_report.get("preserved_count") or 0
    )
    combined["total_estimated_bytes"] = cast(
        "int", default_report.get("total_estimated_bytes") or 0
    ) + cast("int", discarded_report.get("total_estimated_bytes") or 0)
    if "partial" in (default_report.get("status"), discarded_report.get("status")):
        combined["status"] = "partial"
        combined["reason_code"] = CLEANUP_EXECUTION_PARTIAL
    return combined
