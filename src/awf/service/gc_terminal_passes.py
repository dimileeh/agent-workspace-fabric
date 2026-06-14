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

from collections.abc import Mapping
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
    candidates / delete-errors concatenate, the per-workspace detail maps
    (compose teardowns, secret leases, worktree removes, reservation releases)
    union, and preserved counts and byte estimates sum. Folding those detail maps
    matters when the discarded pass goes ``partial`` for a non-delete side effect —
    a ``reservation_releases`` entry with an error, a failed compose teardown — that
    never lands in ``delete_errors``: keeping only the default pass's (clean) maps
    would leave the merged ``partial`` status with no visible cause
    (PRRT_kwDOSJAM6s6Jbctw). ``total_estimated_bytes`` in particular must add both passes' totals (and
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
    for key in ("compose_teardowns", "secret_leases", "worktree_removes", "reservation_releases"):
        first_map = cast("Mapping[str, object]", default_report.get(key) or {})
        second_map = cast("Mapping[str, object]", discarded_report.get(key) or {})
        if first_map or second_map:
            combined[key] = {**first_map, **second_map}
    combined["deleted_path_count"] = len(cast("list[object]", combined["deleted_paths"]))
    combined["candidate_count"] = len(cast("list[object]", combined["candidates"]))
    combined["preserved_count"] = cast("int", default_report.get("preserved_count") or 0) + cast(
        "int", discarded_report.get("preserved_count") or 0
    )
    combined["total_estimated_bytes"] = cast(
        "int", default_report.get("total_estimated_bytes") or 0
    ) + cast("int", discarded_report.get("total_estimated_bytes") or 0)
    default_reap = default_report.get("claude_base_reap")
    discarded_reap = discarded_report.get("claude_base_reap")
    if default_reap is not None or discarded_reap is not None:
        combined["claude_base_reap"] = _merge_claude_base_reaps(default_reap, discarded_reap)
    if "partial" in (default_report.get("status"), discarded_report.get("status")):
        combined["status"] = "partial"
        combined["reason_code"] = CLEANUP_EXECUTION_PARTIAL
    return combined


def _merge_claude_base_reaps(
    default_reap: object, discarded_reap: object
) -> dict[str, object] | None:
    """Fold the two passes' ``claude_base_reap`` sub-reports into one (#590).

    Both terminal-GC passes now run the shared-base reaper: the default pass frees
    bases pinned only by its completed/failed/superseded candidates, and the discarded
    pass frees bases pinned only by the cancelled/destroyed auth dirs it deletes — a
    base pinned solely by a discarded workspace stays protected through the first pass
    and is reaped only after the second deletes that pin (PRRT_kwDOSJAM6s6JbT1B).
    ``combine_terminal_gc_reports`` keeps the default pass's payload via
    ``dict(default_report)``, so without this fold the discarded pass's reaped/planned
    bases vanish from the summary and the operator-facing ``deleted_path_count``
    (which counts reaped bases — PRRT_kwDOSJAM6s6JbAow) under-reports the GB-scale
    reclaim. On ``--execute`` the two passes act on disjoint signatures (the first
    removes what it can before the second runs), so each list concatenates; a signature
    the second pass reaped or planned is dropped from the first pass's ``protected`` (its
    pin was still on disk during the first pass). The API ``--execute`` *dry-run* preview
    deletes nothing, so its second pass also previews the first pass's planned auth dirs
    (``extra_pruned_auth_dirs``) and can re-list a base the first pass already planned —
    so ``reaped``/``planned`` are de-duplicated (order-preserving) rather than blindly
    concatenated (PRRT_kwDOSJAM6s6Jbinh); de-dup is a no-op for the disjoint execute
    lists. A ``partial`` in either reap wins; otherwise a pass that actually
    reaped/planned a base supersedes a no-op ``skipped`` sibling.
    """
    if not isinstance(default_reap, Mapping):
        return dict(discarded_reap) if isinstance(discarded_reap, Mapping) else None
    if not isinstance(discarded_reap, Mapping):
        return dict(default_reap)
    merged: dict[str, object] = dict(default_reap)
    for key in ("scanned", "unverifiable", "errors"):
        merged[key] = [*_as_list(default_reap.get(key)), *_as_list(discarded_reap.get(key))]
    for key in ("reaped", "planned"):
        merged[key] = _dedupe_preserving_order(
            [*_as_list(default_reap.get(key)), *_as_list(discarded_reap.get(key))]
        )
    reclaimed = {*_as_list(merged["reaped"]), *_as_list(merged["planned"])}
    protected = [
        *_as_list(default_reap.get("protected")),
        *_as_list(discarded_reap.get("protected")),
    ]
    merged["protected"] = [signature for signature in protected if signature not in reclaimed]
    default_status = default_reap.get("status")
    discarded_status = discarded_reap.get("status")
    if "partial" in (default_status, discarded_status):
        merged["status"] = "partial"
        merged["reason_code"] = (
            default_reap.get("reason_code")
            if default_status == "partial"
            else discarded_reap.get("reason_code")
        )
    elif default_status == "skipped":
        # The first pass reaped/planned nothing, so the discarded pass's outcome (a
        # reaped/planned base, or another no-op) is the whole picture — adopt its
        # status/reason rather than report ``skipped`` next to a non-empty ``reaped``.
        merged["status"] = discarded_status
        merged["reason_code"] = discarded_reap.get("reason_code")
    # else: the first pass already reaped/planned a base (``ok``); the discarded pass
    # only adds more of the same, so the first pass's ``ok`` status/reason stand.
    return merged


def _as_list(value: object) -> list[object]:
    """Return ``value`` as a list, or an empty list when it is not one."""
    return value if isinstance(value, list) else []


def _dedupe_preserving_order(values: list[object]) -> list[object]:
    """Return ``values`` with later duplicates dropped, keeping first-seen order.

    Unhashable entries (defensive — reaped/planned hold ``<signature>`` strings) are
    kept as-is so a malformed payload never raises here.
    """
    seen: set[object] = set()
    result: list[object] = []
    for value in values:
        try:
            if value in seen:
                continue
            seen.add(value)
        except TypeError:
            result.append(value)
            continue
        result.append(value)
    return result
