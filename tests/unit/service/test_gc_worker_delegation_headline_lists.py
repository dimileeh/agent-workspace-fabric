"""Headline candidate/preserved-list folding tests for ``gc_worker_delegation`` (#582).

Split out of ``test_gc_worker_delegation`` to keep each module under the
first-party line limit. These cover how ``fold_worker_reclaim`` surfaces
worker-only candidates and preserved rows on the headline lists without
double-counting rows the API plan already carried.
"""

from __future__ import annotations

import pytest

from awf.service.gc_worker_delegation import (
    WorkerReclaimOutcome,
    fold_worker_reclaim,
)

pytestmark = pytest.mark.unit


def _api_base(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "dry_run": False,
        "deleted_path_count": 2,
        "total_estimated_bytes": 100,
    }
    base.update(overrides)
    return base


def test_fold_skips_worker_only_candidate_with_non_mapping_estimate() -> None:
    # A worker-only candidate (workspace_id absent from the API plan) whose
    # ``estimated_bytes`` is malformed external JSON (a scalar, not the expected
    # per-path Mapping) contributes nothing: the headline keeps the base plan total
    # rather than crashing or guessing a byte count.
    report = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_path_count": 1,
        "deleted_paths": ["/work/_shared/auth/ws-cancelled"],
        "total_estimated_bytes": 1_700_000_000,
        "candidates": [
            {
                "workspace_id": "ws-cancelled",
                "status": "cancelled",
                "estimated_bytes": 1_700_000_000,
            }
        ],
    }
    outcome = WorkerReclaimOutcome.from_report(report)
    base = _api_base(
        deleted_path_count=0,
        deleted_paths=[],
        total_estimated_bytes=100,
        candidates=[],
    )

    folded = fold_worker_reclaim(base, outcome)

    # The scalar estimate is not a Mapping, so no net-new bytes are summed.
    assert folded["total_estimated_bytes"] == 100


def test_fold_skips_worker_candidate_missing_workspace_id() -> None:
    # PRRT_kwDOSJAM6s6JcnXo: a worker candidate with NO ``workspace_id`` cannot be
    # matched against the API plan's candidate set (which excludes missing IDs), so it
    # must not be treated as worker-only. Even when it carries a well-formed
    # ``estimated_bytes.total``, it contributes 0 to the headline — honouring the
    # documented no-double-count default rather than inflating bytes for malformed
    # payloads.
    report = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_path_count": 1,
        "deleted_paths": ["/work/_shared/auth/ws-cancelled"],
        "total_estimated_bytes": 1_700_000_000,
        "candidates": [
            {
                "status": "cancelled",
                "estimated_bytes": {"auth": 1_700_000_000, "total": 1_700_000_000},
            }
        ],
    }
    outcome = WorkerReclaimOutcome.from_report(report)
    base = _api_base(
        deleted_path_count=0,
        deleted_paths=[],
        total_estimated_bytes=100,
        candidates=[],
    )

    folded = fold_worker_reclaim(base, outcome)

    # No workspace_id means the candidate is not confirmable as net-new, so the base
    # plan total is left untouched instead of inflated by the worker-only bytes.
    assert folded["total_estimated_bytes"] == 100


def test_fold_adds_worker_only_candidate_to_headline_candidate_list() -> None:
    # PRRT_kwDOSJAM6s6Jcixj: a default ``--execute`` run whose only reclaim is the
    # worker's discarded-status augmentation pass (a cancelled/destroyed workspace the
    # API default policy omits) must surface that candidate on the headline
    # ``candidates``/``candidate_count``. Otherwise the response reports its deleted
    # auth paths and GB-scale bytes next to ``candidate_count: 0`` and an empty
    # candidate list, contradicting the dry-run preview that lists the same candidate.
    candidate = {
        "workspace_id": "ws-cancelled",
        "status": "cancelled",
        "estimated_bytes": {"auth": 1_700_000_000, "total": 1_700_000_000},
    }
    report = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_path_count": 1,
        "deleted_paths": ["/work/_shared/auth/ws-cancelled"],
        "total_estimated_bytes": 1_700_000_000,
        "candidates": [candidate],
    }
    outcome = WorkerReclaimOutcome.from_report(report)
    base = _api_base(
        deleted_path_count=0,
        deleted_paths=[],
        total_estimated_bytes=0,
        candidate_count=0,
        candidates=[],
    )

    folded = fold_worker_reclaim(base, outcome)

    assert folded["candidates"] == [candidate]
    assert folded["candidate_count"] == 1
    # The caller's base list is left untouched (a fresh list is built).
    assert base["candidates"] == []
    assert base["candidate_count"] == 0


def test_fold_does_not_duplicate_api_planned_candidate_in_headline_list() -> None:
    # A worker candidate already carried by the API plan (same ``workspace_id``) is not
    # re-appended — it is the same workspace the API pass already listed, so duplicating
    # it would inflate ``candidate_count`` and the candidate list.
    api_candidate = {"workspace_id": "ws-1", "status": "completed"}
    report = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_path_count": 1,
        "deleted_paths": ["/work/_shared/auth/ws-1"],
        "total_estimated_bytes": 1_700_000_000,
        "candidates": [{"workspace_id": "ws-1", "status": "completed"}],
    }
    outcome = WorkerReclaimOutcome.from_report(report)
    base = _api_base(
        deleted_path_count=0,
        deleted_paths=[],
        total_estimated_bytes=1_700_000_000,
        candidate_count=1,
        candidates=[api_candidate],
    )

    folded = fold_worker_reclaim(base, outcome)

    assert folded["candidates"] == [api_candidate]
    assert folded["candidate_count"] == 1


def test_fold_appends_worker_only_candidate_when_base_lacks_candidate_list() -> None:
    # Defensive: a base payload without a ``candidates`` list (never the real
    # ``WorkspaceGCResult`` shape, but robust to a stubbed/garbled payload) still
    # surfaces the worker-only candidate rather than dropping it.
    candidate = {"workspace_id": "ws-cancelled", "status": "cancelled"}
    report = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_path_count": 1,
        "deleted_paths": ["/work/_shared/auth/ws-cancelled"],
        "total_estimated_bytes": 1_700_000_000,
        "candidates": [candidate],
    }
    outcome = WorkerReclaimOutcome.from_report(report)
    base = _api_base(deleted_path_count=0, deleted_paths=[], total_estimated_bytes=0)

    folded = fold_worker_reclaim(base, outcome)

    assert folded["candidates"] == [candidate]
    assert folded["candidate_count"] == 1


def test_fold_worker_partial_adds_worker_only_candidate_to_headline_list() -> None:
    # The worker-only candidate fold also applies on a partial worker reap — the
    # candidate was still reaped; the partial is for an unrelated side effect — so the
    # headline candidate list stays consistent with the (folded) deleted paths/bytes.
    candidate = {
        "workspace_id": "ws-cancelled",
        "status": "cancelled",
        "estimated_bytes": {"auth": 1_700_000_000, "total": 1_700_000_000},
    }
    report = {
        "status": "partial",
        "reason_code": "CLEANUP_EXECUTION_PARTIAL",
        "deleted_path_count": 1,
        "deleted_paths": ["/work/_shared/auth/ws-cancelled"],
        "total_estimated_bytes": 1_700_000_000,
        "candidates": [candidate],
    }
    outcome = WorkerReclaimOutcome.from_report(report)
    base = _api_base(
        deleted_path_count=0,
        deleted_paths=[],
        total_estimated_bytes=0,
        candidate_count=0,
        candidates=[],
    )

    folded = fold_worker_reclaim(base, outcome)

    assert folded["status"] == "partial"
    assert folded["candidates"] == [candidate]
    assert folded["candidate_count"] == 1


def test_fold_adds_worker_only_preserved_to_headline_preserved_list() -> None:
    # PRRT_kwDOSJAM6s6JdGCK: a too-young cancelled/destroyed workspace is returned by
    # the worker's discarded-status pass under ``preserved`` (within ``--min-age-hours``)
    # rather than as a candidate, and the API default policy never classifies those
    # statuses. That preserved row must surface on the headline
    # ``preserved``/``preserved_count`` — otherwise the ``--execute`` response reports
    # zero preserved rows while the dry-run preview lists that same workspace.
    preserved = {
        "workspace_id": "ws-cancelled",
        "status": "cancelled",
        "reason_code": "WORKSPACE_WITHIN_RETENTION",
    }
    report = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_path_count": 0,
        "deleted_paths": [],
        "total_estimated_bytes": 0,
        "preserved": [preserved],
        "preserved_count": 1,
    }
    outcome = WorkerReclaimOutcome.from_report(report)
    base = _api_base(
        deleted_path_count=0,
        deleted_paths=[],
        total_estimated_bytes=0,
        preserved=[],
        preserved_count=0,
    )

    folded = fold_worker_reclaim(base, outcome)

    assert folded["preserved"] == [preserved]
    assert folded["preserved_count"] == 1
    # The caller's base list is left untouched (a fresh list is built).
    assert base["preserved"] == []
    assert base["preserved_count"] == 0


def test_fold_does_not_duplicate_api_preserved_row_in_headline_list() -> None:
    # A worker preserved row already carried by the API plan (same ``workspace_id``,
    # the shared default pass) is not re-appended — duplicating it would inflate
    # ``preserved_count`` and the preserved list.
    api_preserved = {"workspace_id": "ws-1", "status": "failed"}
    report = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_path_count": 0,
        "deleted_paths": [],
        "total_estimated_bytes": 0,
        "preserved": [{"workspace_id": "ws-1", "status": "failed"}],
        "preserved_count": 1,
    }
    outcome = WorkerReclaimOutcome.from_report(report)
    base = _api_base(
        deleted_path_count=0,
        deleted_paths=[],
        total_estimated_bytes=0,
        preserved=[api_preserved],
        preserved_count=1,
    )

    folded = fold_worker_reclaim(base, outcome)

    assert folded["preserved"] == [api_preserved]
    assert folded["preserved_count"] == 1


def test_fold_skips_worker_only_preserved_without_workspace_id() -> None:
    # A preserved row with no ``workspace_id`` cannot be proven net-new against the API
    # plan, so it is skipped rather than inflating the headline preserved count.
    report = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_path_count": 0,
        "deleted_paths": [],
        "total_estimated_bytes": 0,
        "preserved": [{"status": "cancelled", "reason_code": "WORKSPACE_WITHIN_RETENTION"}],
        "preserved_count": 1,
    }
    outcome = WorkerReclaimOutcome.from_report(report)
    base = _api_base(
        deleted_path_count=0,
        deleted_paths=[],
        total_estimated_bytes=0,
        preserved=[],
        preserved_count=0,
    )

    folded = fold_worker_reclaim(base, outcome)

    assert folded["preserved"] == []
    assert folded["preserved_count"] == 0


def test_fold_appends_worker_only_preserved_when_base_lacks_preserved_list() -> None:
    # Defensive: a base payload without a ``preserved`` list (robust to a stubbed or
    # garbled payload) still surfaces the worker-only preserved row.
    preserved = {"workspace_id": "ws-cancelled", "status": "cancelled"}
    report = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_path_count": 0,
        "deleted_paths": [],
        "total_estimated_bytes": 0,
        "preserved": [preserved],
        "preserved_count": 1,
    }
    outcome = WorkerReclaimOutcome.from_report(report)
    base = _api_base(deleted_path_count=0, deleted_paths=[], total_estimated_bytes=0)

    folded = fold_worker_reclaim(base, outcome)

    assert folded["preserved"] == [preserved]
    assert folded["preserved_count"] == 1
