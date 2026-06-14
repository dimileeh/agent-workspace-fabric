"""Pure-fold unit tests for ``gc_worker_delegation`` (#582).

These cover the summation of reclaimed counts/bytes, the ``worker_reclaim`` shape,
and the status downgrade on a delegation error or a partial worker reap.
"""

from __future__ import annotations

import pytest

from awf.service.gc_worker_delegation import (
    SERVICE_GC_WORKER_DELEGATION_TIMEOUT,
    SERVICE_GC_WORKER_RECLAIM_FAILED,
    SERVICE_GC_WORKER_RECLAIMED,
    SERVICE_GC_WORKER_UNAVAILABLE,
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


def test_fold_sums_worker_reclaim_into_headline() -> None:
    report = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_path_count": 3,
        "total_estimated_bytes": 1_700_000_000,
    }
    outcome = WorkerReclaimOutcome.from_report(report)

    folded = fold_worker_reclaim(_api_base(), outcome)

    # ``deleted_path_count`` is genuinely additive: the API-side pass recorded the
    # auth/claude-base paths as ``skipped`` (never in ``deleted_paths``), so the
    # worker's actual deletions are net-new.
    assert folded["deleted_path_count"] == 5
    # ``total_estimated_bytes`` must NOT be summed: the API plan total already
    # includes the auth-dir estimate (it is a directory-scan estimate, independent
    # of the skip), and the worker re-estimates the same auth dir it removes. Adding
    # them double-counts ~1.7GB per workspace, so the base plan total is kept.
    assert folded["total_estimated_bytes"] == 100
    assert folded["status"] == "succeeded"
    worker_reclaim = folded["worker_reclaim"]
    assert isinstance(worker_reclaim, dict)
    assert worker_reclaim["status"] == "completed"
    assert worker_reclaim["reason_code"] == SERVICE_GC_WORKER_RECLAIMED
    assert worker_reclaim["deleted_path_count"] == 3
    # The worker's own reclaim estimate stays visible on the sub-object.
    assert worker_reclaim["total_estimated_bytes"] == 1_700_000_000
    assert worker_reclaim["report"] == report


def test_fold_merges_worker_deleted_paths_into_headline_list() -> None:
    # ``WorkspaceGCResult.to_dict()`` guarantees ``deleted_path_count ==
    # len(deleted_paths)``. A successful worker reclaim sums its count onto the
    # headline, so its actually-removed paths must also be merged into the headline
    # ``deleted_paths`` (the API recorded them as ``skipped`` — never in
    # ``deleted_paths`` — so they are net-new and never duplicated). Otherwise the
    # payload reports ``deleted_path_count > len(deleted_paths)`` and audit consumers
    # miss the paths that were actually removed.
    report = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_path_count": 2,
        "deleted_paths": ["/work/_shared/auth/ws-1", "/work/_shared/claude-base"],
        "total_estimated_bytes": 1_700_000_000,
    }
    outcome = WorkerReclaimOutcome.from_report(report)
    base = _api_base(deleted_path_count=1, deleted_paths=["/work/worktrees/ws-1"])

    folded = fold_worker_reclaim(base, outcome)

    assert folded["deleted_path_count"] == 3
    assert folded["deleted_paths"] == [
        "/work/worktrees/ws-1",
        "/work/_shared/auth/ws-1",
        "/work/_shared/claude-base",
    ]
    # The headline invariant survives the fold.
    assert folded["deleted_path_count"] == len(folded["deleted_paths"])


def test_fold_worker_partial_merges_reclaimed_deleted_paths() -> None:
    # Even a partial worker reap merges the paths it proved it removed, so the
    # headline ``deleted_paths`` stays consistent with the (additive) count.
    report = {
        "status": "partial",
        "reason_code": "CLEANUP_EXECUTION_PARTIAL",
        "deleted_path_count": 1,
        "deleted_paths": ["/work/_shared/auth/ws-1"],
        "total_estimated_bytes": 10,
    }
    outcome = WorkerReclaimOutcome.from_report(report)
    base = _api_base(deleted_path_count=1, deleted_paths=["/work/worktrees/ws-1"])

    folded = fold_worker_reclaim(base, outcome)

    assert folded["status"] == "partial"
    assert folded["deleted_path_count"] == 2
    assert folded["deleted_paths"] == [
        "/work/worktrees/ws-1",
        "/work/_shared/auth/ws-1",
    ]
    assert folded["deleted_path_count"] == len(folded["deleted_paths"])


def test_fold_does_not_mutate_base() -> None:
    base = _api_base(deleted_paths=["/work/worktrees/ws-1"])
    outcome = WorkerReclaimOutcome.from_report(
        {"deleted_path_count": 1, "deleted_paths": ["/work/_shared/auth/ws-1"]}
    )

    fold_worker_reclaim(base, outcome)

    assert base["deleted_path_count"] == 2
    # Merging the worker's reclaimed paths must not mutate the caller's list.
    assert base["deleted_paths"] == ["/work/worktrees/ws-1"]
    assert "worker_reclaim" not in base


def test_fold_worker_partial_downgrades_status() -> None:
    report = {
        "status": "partial",
        "reason_code": "CLEANUP_EXECUTION_PARTIAL",
        "deleted_path_count": 1,
        "total_estimated_bytes": 10,
    }
    outcome = WorkerReclaimOutcome.from_report(report)

    folded = fold_worker_reclaim(_api_base(), outcome)

    assert folded["status"] == "partial"
    assert folded["reason_code"] == "CLEANUP_EXECUTION_PARTIAL"
    # Reclaimed totals still fold in even on a partial worker sweep.
    assert folded["deleted_path_count"] == 3


def test_fold_worker_unavailable_downgrades_and_preserves_api_reclaim() -> None:
    outcome = WorkerReclaimOutcome.unavailable("no fresh worker heartbeat for node node-a")

    folded = fold_worker_reclaim(_api_base(), outcome)

    assert folded["status"] == "partial"
    assert folded["reason_code"] == SERVICE_GC_WORKER_UNAVAILABLE
    # The API-side reclaim is preserved (never zeroed) and not inflated.
    assert folded["deleted_path_count"] == 2
    assert folded["total_estimated_bytes"] == 100
    worker_reclaim = folded["worker_reclaim"]
    assert isinstance(worker_reclaim, dict)
    assert worker_reclaim["status"] == "unavailable"
    assert worker_reclaim["deleted_path_count"] == 0


def test_fold_timeout_and_failed_reason_codes() -> None:
    timed_out = fold_worker_reclaim(
        _api_base(), WorkerReclaimOutcome.delegation_timeout("deadline exceeded")
    )
    assert timed_out["status"] == "partial"
    assert timed_out["reason_code"] == SERVICE_GC_WORKER_DELEGATION_TIMEOUT

    failed = fold_worker_reclaim(
        _api_base(),
        WorkerReclaimOutcome.reclaim_failed("boom", report={"status": "failed"}),
    )
    assert failed["status"] == "partial"
    assert failed["reason_code"] == SERVICE_GC_WORKER_RECLAIM_FAILED
    worker_reclaim = failed["worker_reclaim"]
    assert isinstance(worker_reclaim, dict)
    assert worker_reclaim["message"] == "boom"
    assert worker_reclaim["report"] == {"status": "failed"}


def test_from_report_coerces_missing_and_float_fields() -> None:
    outcome = WorkerReclaimOutcome.from_report(
        {"deleted_path_count": True, "total_estimated_bytes": 12.9}
    )
    assert outcome.deleted_path_count == 1
    assert outcome.total_estimated_bytes == 12

    empty = WorkerReclaimOutcome.from_report({})
    assert empty.deleted_path_count == 0
    assert empty.total_estimated_bytes == 0
    assert empty.succeeded is True


def test_fold_partial_worker_reap_without_reason_code_falls_back() -> None:
    # A partial worker report missing its reason_code still downgrades the
    # headline to the canonical partial reason code.
    outcome = WorkerReclaimOutcome.from_report({"status": "partial"})

    folded = fold_worker_reclaim(_api_base(), outcome)

    assert folded["status"] == "partial"
    assert folded["reason_code"] == "CLEANUP_EXECUTION_PARTIAL"


def test_fold_reconciles_auth_unmount_skip_after_worker_success() -> None:
    # The API container lacks CAP_SYS_ADMIN, so the auth overlay unmount was
    # recorded as a delete error and forced the run partial. A completed worker
    # reclaim actually removed those auth dirs, so the headline must reconcile to
    # success and the stale auth-unmount delete error must be dropped — otherwise
    # the CLI warns the auth dir was preserved and exits non-zero (PRRT…JakrC).
    base = _api_base(
        status="partial",
        reason_code="CLEANUP_EXECUTION_PARTIAL",
        delete_errors=[
            {
                "kind": "auth_overlay_unmount",
                "reason_code": "CLAUDE_AUTH_OVERLAY_UNMOUNT_INCAPABLE",
                "error": "cannot verify Claude auth overlay teardown without CAP_SYS_ADMIN",
            }
        ],
    )
    outcome = WorkerReclaimOutcome.from_report(
        {"status": "succeeded", "deleted_path_count": 3, "total_estimated_bytes": 1_700_000_000}
    )

    folded = fold_worker_reclaim(base, outcome)

    assert folded["status"] == "succeeded"
    assert folded["reason_code"] == "CLEANUP_EXECUTION_SUCCEEDED"
    # The stale auth-unmount delete error is dropped — the worker reclaimed it.
    assert folded["delete_errors"] == []
    assert folded["deleted_path_count"] == 5


def test_fold_reconciles_claude_base_partial_after_worker_success() -> None:
    # A partial API-side claude-base reap (live-mount view unverifiable without
    # CAP_SYS_ADMIN) also drives the run partial; the worker reclaims claude-base,
    # so a completed reclaim reconciles the headline to success.
    nested = {
        "status": "partial",
        "reason_code": "CLAUDE_BASE_REAP_PARTIAL",
        "unverifiable": ["sig-a"],
    }
    base = _api_base(
        status="partial",
        reason_code="CLEANUP_EXECUTION_PARTIAL",
        claude_base_reap=nested,
    )
    outcome = WorkerReclaimOutcome.from_report({"status": "succeeded", "deleted_path_count": 1})

    folded = fold_worker_reclaim(base, outcome)

    assert folded["status"] == "succeeded"
    assert folded["reason_code"] == "CLEANUP_EXECUTION_SUCCEEDED"
    # The nested claude-base reap must not still read ``partial`` next to a
    # succeeded headline: the worker superseded it (PRRT…JasjU). Its status is
    # reconciled and a marker records that the worker did the actual reclaim,
    # while the diagnostic lists are preserved.
    reconciled = folded["claude_base_reap"]
    assert isinstance(reconciled, dict)
    assert reconciled["status"] == "succeeded"
    assert reconciled["reason_code"] == SERVICE_GC_WORKER_RECLAIMED
    assert reconciled["reconciled_by_worker"] is True
    assert reconciled["unverifiable"] == ["sig-a"]
    # The base's nested object is not mutated in place.
    assert nested["status"] == "partial"
    assert "reconciled_by_worker" not in nested


def test_fold_preserves_unrelated_failure_while_dropping_auth_skip() -> None:
    # The run is partial for two reasons: the reconcilable auth-unmount skip AND a
    # genuine compose teardown failure the worker does not own. The worker reclaim
    # drops the auth skip but the run stays partial for the real failure.
    base = _api_base(
        status="partial",
        reason_code="CLEANUP_EXECUTION_PARTIAL",
        delete_errors=[
            {
                "kind": "auth_overlay_unmount",
                "reason_code": "CLAUDE_AUTH_OVERLAY_UNMOUNT_INCAPABLE",
                "error": "no CAP_SYS_ADMIN",
            },
            {
                "kind": "compose_teardown",
                "reason_code": "COMPOSE_COMMAND_FAILED",
                "error": "docker compose down failed",
            },
        ],
    )
    outcome = WorkerReclaimOutcome.from_report({"status": "succeeded", "deleted_path_count": 2})

    folded = fold_worker_reclaim(base, outcome)

    assert folded["status"] == "partial"
    assert folded["reason_code"] == "CLEANUP_EXECUTION_PARTIAL"
    # The auth skip is dropped; the unrelated compose failure is preserved.
    remaining = folded["delete_errors"]
    assert isinstance(remaining, list)
    assert [error["kind"] for error in remaining] == ["compose_teardown"]


def test_fold_does_not_touch_partial_unrelated_to_auth_or_base() -> None:
    # A run partial only for a reservation release error has nothing for the worker
    # reclaim to reconcile, so the headline stays partial and is left untouched.
    base = _api_base(
        status="partial",
        reason_code="CLEANUP_EXECUTION_PARTIAL",
        delete_errors=[],
        reservation_releases={"ws-1": {"error": "lease release failed"}},
    )
    outcome = WorkerReclaimOutcome.from_report({"status": "succeeded", "deleted_path_count": 1})

    folded = fold_worker_reclaim(base, outcome)

    assert folded["status"] == "partial"
    assert folded["reason_code"] == "CLEANUP_EXECUTION_PARTIAL"


def test_fold_keeps_partial_when_worker_reap_partial_even_with_auth_skip() -> None:
    # If the worker's own reap was partial and gives no ``deleted_paths`` proof, it
    # may not have reclaimed the auth dir, so the loud auth-unmount skip stays and
    # the run stays partial — an unproven partial reclaim never drops the skip.
    base = _api_base(
        status="partial",
        reason_code="CLEANUP_EXECUTION_PARTIAL",
        delete_errors=[
            {
                "kind": "auth_overlay_unmount",
                "reason_code": "CLAUDE_AUTH_OVERLAY_UNMOUNT_INCAPABLE",
                "error": "no CAP_SYS_ADMIN",
            }
        ],
    )
    outcome = WorkerReclaimOutcome.from_report({"status": "partial", "deleted_path_count": 1})

    folded = fold_worker_reclaim(base, outcome)

    assert folded["status"] == "partial"
    remaining = folded["delete_errors"]
    assert isinstance(remaining, list)
    assert [error["kind"] for error in remaining] == ["auth_overlay_unmount"]


def test_fold_drops_reclaimed_auth_skip_when_worker_partial_for_unrelated_step() -> None:
    # The worker's combined reap is overall ``partial`` for an unrelated sub-step
    # (e.g. a companion-image prune failure), but its ``deleted_paths`` prove it
    # actually removed the auth dir the API container could not unmount. That makes
    # the API-side auth-unmount skip stale: it must be dropped so the CLI does not
    # warn the auth dir was preserved although it was reclaimed — while the headline
    # stays partial for the unrelated failure (PRRT…Ja64r).
    base = _api_base(
        status="partial",
        reason_code="CLEANUP_EXECUTION_PARTIAL",
        delete_errors=[
            {
                "kind": "auth_overlay_unmount",
                "path": "/work/_shared/auth/ws-1",
                "reason_code": "CLAUDE_AUTH_OVERLAY_UNMOUNT_INCAPABLE",
                "error": "no CAP_SYS_ADMIN",
            }
        ],
    )
    outcome = WorkerReclaimOutcome.from_report(
        {
            "status": "partial",
            "reason_code": "CLEANUP_EXECUTION_PARTIAL",
            "deleted_path_count": 1,
            "deleted_paths": ["/work/_shared/auth/ws-1"],
        }
    )

    folded = fold_worker_reclaim(base, outcome)

    # Headline stays partial for the unrelated worker failure...
    assert folded["status"] == "partial"
    assert folded["reason_code"] == "CLEANUP_EXECUTION_PARTIAL"
    # ...but the stale auth-unmount skip for the reclaimed path is dropped.
    assert folded["delete_errors"] == []


def test_fold_keeps_auth_skip_when_worker_partial_did_not_reclaim_it() -> None:
    # A partial worker reap whose ``deleted_paths`` do NOT include the auth dir
    # leaves the auth-unmount skip in place: the worker genuinely failed to reclaim
    # it, so the loud skip must stay even as other reclaimed paths are dropped.
    base = _api_base(
        status="partial",
        reason_code="CLEANUP_EXECUTION_PARTIAL",
        delete_errors=[
            {
                "kind": "auth_overlay_unmount",
                "path": "/work/_shared/auth/ws-1",
                "reason_code": "CLAUDE_AUTH_OVERLAY_UNMOUNT_INCAPABLE",
                "error": "no CAP_SYS_ADMIN",
            }
        ],
    )
    outcome = WorkerReclaimOutcome.from_report(
        {
            "status": "partial",
            "deleted_path_count": 1,
            "deleted_paths": ["/work/_shared/auth/ws-2"],
        }
    )

    folded = fold_worker_reclaim(base, outcome)

    assert folded["status"] == "partial"
    remaining = folded["delete_errors"]
    assert isinstance(remaining, list)
    assert [error["kind"] for error in remaining] == ["auth_overlay_unmount"]
