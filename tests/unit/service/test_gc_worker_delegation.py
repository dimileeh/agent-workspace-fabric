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
    _claude_base_reaped_path_strs,
    _reconcile_candidate_auth,
    bounded_worker_deadline_seconds,
    fold_worker_reclaim,
)

pytestmark = pytest.mark.unit


def test_bounded_deadline_keeps_full_budget_when_api_phase_fits() -> None:
    # API phase well within its own ``deadline_seconds`` budget → worker keeps the lot.
    assert bounded_worker_deadline_seconds(900.0, 10.0) == 900.0
    # Exactly at its budget still leaves the worker its full deadline.
    assert bounded_worker_deadline_seconds(900.0, 900.0) == 900.0


def test_bounded_deadline_shrinks_when_api_phase_overruns() -> None:
    # API phase ran 250s past its 900s budget → worker deadline shrinks by the overrun
    # so total server time stays at 2*900 = 1800s, inside the CLI's 2*900+30 budget.
    assert bounded_worker_deadline_seconds(900.0, 1150.0) == 650.0


def test_bounded_deadline_floors_at_zero_when_api_phase_exhausts_budget() -> None:
    # API phase alone exceeded 2*deadline (client already aborted) → never negative.
    assert bounded_worker_deadline_seconds(900.0, 2000.0) == 0.0
    # Defensive: a negative elapsed (clock skew) is treated as zero elapsed.
    assert bounded_worker_deadline_seconds(900.0, -5.0) == 900.0
    # A non-positive deadline yields a non-positive (zero) budget.
    assert bounded_worker_deadline_seconds(0.0, 0.0) == 0.0


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


def test_fold_adds_worker_only_candidate_bytes_to_headline() -> None:
    # PRRT_kwDOSJAM6s6JbHKg: when the API default pass has no eligible candidates
    # (plan total 0) but the worker's discarded-status augmentation reaps a
    # cancelled/destroyed workspace the API plan never estimated, its GB-scale auth
    # bytes are net-new and must reach the headline ``total_estimated_bytes`` — not
    # be dropped, leaving GB-scale ``deleted_path_count`` alongside 0 bytes.
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
                "estimated_bytes": {"auth": 1_700_000_000, "total": 1_700_000_000},
            }
        ],
    }
    outcome = WorkerReclaimOutcome.from_report(report)
    base = _api_base(
        deleted_path_count=0,
        deleted_paths=[],
        total_estimated_bytes=0,
        candidates=[],
    )

    folded = fold_worker_reclaim(base, outcome)

    assert folded["deleted_path_count"] == 1
    # The worker-only candidate's estimate is added: GB-scale deletions now report
    # GB-scale bytes instead of 0.
    assert folded["total_estimated_bytes"] == 1_700_000_000


def test_fold_does_not_double_count_overlapping_api_planned_candidate_bytes() -> None:
    # The API plan already estimates its own default-policy candidates' auth dirs
    # (counted even though the capability-less API pass skipped deleting them). The
    # worker re-estimates the *same* workspace, so its bytes must NOT be summed again
    # — only worker-only workspaces (absent from the API plan) are net-new.
    report = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_path_count": 1,
        "deleted_paths": ["/work/_shared/auth/ws-1"],
        "total_estimated_bytes": 1_700_000_000,
        "candidates": [
            {
                "workspace_id": "ws-1",
                "status": "completed",
                "estimated_bytes": {"auth": 1_700_000_000, "total": 1_700_000_000},
            }
        ],
    }
    outcome = WorkerReclaimOutcome.from_report(report)
    base = _api_base(
        deleted_path_count=0,
        deleted_paths=[],
        total_estimated_bytes=1_700_000_000,
        candidates=[
            {
                "workspace_id": "ws-1",
                "status": "completed",
                "estimated_bytes": {"auth": 1_700_000_000, "total": 1_700_000_000},
            }
        ],
    )

    folded = fold_worker_reclaim(base, outcome)

    # ``ws-1`` is already in the API plan total — adding the worker's re-estimate
    # would double-count ~1.7GB. The headline keeps the single plan total.
    assert folded["total_estimated_bytes"] == 1_700_000_000


def test_fold_keeps_plan_total_when_worker_report_omits_candidate_breakdown() -> None:
    # With no candidate breakdown there is no way to prove which bytes are worker-only,
    # so the conservative default holds: keep the base plan total, never blindly sum
    # the worker's (potentially overlapping) total.
    report = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_path_count": 1,
        "deleted_paths": ["/work/_shared/auth/ws-1"],
        "total_estimated_bytes": 1_700_000_000,
    }
    outcome = WorkerReclaimOutcome.from_report(report)
    base = _api_base(deleted_path_count=0, deleted_paths=[], total_estimated_bytes=100)

    folded = fold_worker_reclaim(base, outcome)

    assert folded["total_estimated_bytes"] == 100


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


def test_from_report_counts_claude_base_only_reaps() -> None:
    # When the worker reaps only superseded ``_shared/claude-base`` bases (no
    # per-workspace auth candidates), ``run_service_workspace_gc`` keeps those
    # deletions under the nested ``claude_base_reap.reaped`` payload while the
    # top-level ``deleted_path_count`` stays the (zero) workspace-candidate total.
    # The outcome must still surface the reaped bases or the operator sees 0
    # reclaimed even though GB-scale shared bases were removed (PRRT_kwDOSJAM6s6JbAow).
    report = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_path_count": 0,
        "deleted_paths": [],
        "total_estimated_bytes": 0,
        "claude_base_reap": {
            "status": "ok",
            "base_root": "/work/auth/_shared/claude-base",
            "reaped": ["sigA", "sigB"],
        },
    }
    outcome = WorkerReclaimOutcome.from_report(report)

    assert outcome.deleted_path_count == 2


def test_fold_surfaces_claude_base_only_worker_reclaim() -> None:
    # The headline scenario this change exists for: the API-side pass reclaimed
    # nothing the worker did not, and the worker only reaped shared bases. The
    # folded report and ``worker_reclaim`` must reflect the reaped bases (count and
    # merged paths) rather than reporting 0 reclaimed (PRRT_kwDOSJAM6s6JbAow).
    report = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_path_count": 0,
        "deleted_paths": [],
        "total_estimated_bytes": 0,
        "claude_base_reap": {
            "status": "ok",
            "base_root": "/work/auth/_shared/claude-base",
            "reaped": ["sigA", "sigB"],
        },
    }
    outcome = WorkerReclaimOutcome.from_report(report)
    base = _api_base(deleted_path_count=0, deleted_paths=[], total_estimated_bytes=0)

    folded = fold_worker_reclaim(base, outcome)

    assert folded["deleted_path_count"] == 2
    assert folded["deleted_paths"] == [
        "/work/auth/_shared/claude-base/sigA",
        "/work/auth/_shared/claude-base/sigB",
    ]
    # The headline invariant survives the fold.
    assert folded["deleted_path_count"] == len(folded["deleted_paths"])
    worker_reclaim = folded["worker_reclaim"]
    assert isinstance(worker_reclaim, dict)
    assert worker_reclaim["deleted_path_count"] == 2


def test_from_report_counts_claude_base_reaps_alongside_candidates() -> None:
    # Per-workspace candidates *and* shared-base reaps both count: the top-level
    # ``deleted_path_count`` covers the candidates, the nested reap adds the bases.
    report = {
        "status": "succeeded",
        "deleted_path_count": 1,
        "deleted_paths": ["/work/auth/ws-1"],
        "claude_base_reap": {
            "status": "ok",
            "base_root": "/work/auth/_shared/claude-base",
            "reaped": ["sigA"],
        },
    }
    outcome = WorkerReclaimOutcome.from_report(report)
    base = _api_base(deleted_path_count=0, deleted_paths=[])

    folded = fold_worker_reclaim(base, outcome)

    assert outcome.deleted_path_count == 2
    assert folded["deleted_paths"] == [
        "/work/auth/ws-1",
        "/work/auth/_shared/claude-base/sigA",
    ]
    assert folded["deleted_path_count"] == len(folded["deleted_paths"])


def test_from_report_ignores_claude_base_reap_without_base_root() -> None:
    # A reap report missing ``base_root`` still counts the reaped bases (by bare
    # signature name) rather than silently dropping them.
    report = {
        "status": "succeeded",
        "deleted_path_count": 0,
        "claude_base_reap": {"status": "ok", "reaped": ["sigA", "sigB"]},
    }
    outcome = WorkerReclaimOutcome.from_report(report)

    assert outcome.deleted_path_count == 2
    base = _api_base(deleted_path_count=0, deleted_paths=[])
    folded = fold_worker_reclaim(base, outcome)
    assert folded["deleted_paths"] == ["sigA", "sigB"]


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


def test_fold_keeps_partial_for_fallback_compose_teardown_failure() -> None:
    # PRRT_kwDOSJAM6s6JciIz: a *fallback* compose teardown failure is recorded only
    # under ``compose_teardowns`` (the single-workspace fallback path never enters the
    # delete-paths loop that mirrors candidate teardowns into ``delete_errors``), yet
    # it still drives the run partial. With only a reconcilable auth-unmount skip in
    # ``delete_errors``, the fold must NOT promote the headline to success — the failed
    # compose teardown the worker does not own keeps the run partial.
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
        compose_teardowns={
            "ws-1": {
                "status": "failed",
                "reason_code": "COMPOSE_COMMAND_FAILED",
                "error": "docker compose down failed",
            }
        },
    )
    outcome = WorkerReclaimOutcome.from_report({"status": "succeeded", "deleted_path_count": 2})

    folded = fold_worker_reclaim(base, outcome)

    # The auth skip is reconciled away, but the unowned compose teardown failure
    # keeps the run partial — a genuinely failed cleanup never reads as success.
    assert folded["status"] == "partial"
    assert folded["reason_code"] == "CLEANUP_EXECUTION_PARTIAL"
    # The stale auth-unmount skip is still dropped — the worker reclaimed it.
    assert folded["delete_errors"] == []


def test_fold_promotes_when_compose_teardowns_all_succeeded_or_skipped() -> None:
    # A successful/skipped compose teardown is not a failure, so it must not block the
    # restore-to-success once the only other partial driver (the auth-unmount skip) is
    # reconciled by the completed worker reclaim — mirrors ``...TeardownResult.ok``.
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
        compose_teardowns={
            "ws-1": {"status": "succeeded", "reason_code": "COMPOSE_TEARDOWN_OK"},
            "ws-2": {"status": "skipped", "reason_code": "COMPOSE_TEARDOWN_SKIPPED"},
        },
    )
    outcome = WorkerReclaimOutcome.from_report({"status": "succeeded", "deleted_path_count": 1})

    folded = fold_worker_reclaim(base, outcome)

    assert folded["status"] == "succeeded"
    assert folded["reason_code"] == "CLEANUP_EXECUTION_SUCCEEDED"


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


def _candidate_with_skipped_auth(workspace_id: str, auth_path: str) -> dict[str, object]:
    # Mirrors the API-side serialization of a candidate whose auth dir the
    # capability-less API container could not unmount: the per-path entry reads
    # ``status: skipped, deleted: false`` with the unmount reason/error.
    return {
        "workspace_id": workspace_id,
        "status": "completed",
        "paths": {
            "auth": {
                "path": auth_path,
                "exists": True,
                "estimated_bytes": 1_700_000_000,
                "deleted": False,
                "status": "skipped",
                "reason_code": "CLAUDE_AUTH_OVERLAY_UNMOUNT_INCAPABLE",
                "error": "cannot verify Claude auth overlay teardown without CAP_SYS_ADMIN",
            },
            "worktree": {
                "path": f"/work/worktrees/{workspace_id}",
                "deleted": True,
                "status": "deleted",
            },
        },
    }


def test_fold_reconciles_candidate_auth_entry_after_worker_success() -> None:
    # PRRT_kwDOSJAM6s6JbLTR: the fold reconciles the headline (deleted_paths,
    # delete_errors, status) when the worker reclaims an auth dir the API skipped,
    # but the per-candidate ``paths.auth`` entry stays ``skipped/deleted:false``.
    # Consumers auditing per-candidate outcomes would be told the reclaimed auth dir
    # was preserved. The candidate entry must be rewritten to a deleted outcome.
    candidate = _candidate_with_skipped_auth("ws-1", "/work/_shared/auth/ws-1")
    base = _api_base(
        status="partial",
        reason_code="CLEANUP_EXECUTION_PARTIAL",
        deleted_path_count=0,
        deleted_paths=[],
        delete_errors=[
            {
                "kind": "auth_overlay_unmount",
                "path": "/work/_shared/auth/ws-1",
                "reason_code": "CLAUDE_AUTH_OVERLAY_UNMOUNT_INCAPABLE",
                "error": "no CAP_SYS_ADMIN",
            }
        ],
        candidates=[candidate],
    )
    outcome = WorkerReclaimOutcome.from_report(
        {
            "status": "succeeded",
            "deleted_path_count": 1,
            "deleted_paths": ["/work/_shared/auth/ws-1"],
        }
    )

    folded = fold_worker_reclaim(base, outcome)

    folded_candidates = folded["candidates"]
    assert isinstance(folded_candidates, list)
    auth = folded_candidates[0]["paths"]["auth"]
    assert auth["deleted"] is True
    assert auth["status"] == "deleted"
    assert auth["reason_code"] == SERVICE_GC_WORKER_RECLAIMED
    assert auth["reconciled_by_worker"] is True
    # The stale "could not unmount" error is dropped from the per-candidate entry.
    assert "error" not in auth
    # The base candidate is not mutated in place.
    assert candidate["paths"]["auth"]["deleted"] is False
    assert candidate["paths"]["auth"]["status"] == "skipped"
    assert "reconciled_by_worker" not in candidate["paths"]["auth"]


def test_fold_reconciles_candidate_auth_entry_on_partial_worker_reap() -> None:
    # A partial worker reap still proves (via ``deleted_paths``) which auth dirs it
    # removed; those candidate entries must be reconciled too, while the run stays
    # partial for the unrelated failure.
    candidate = _candidate_with_skipped_auth("ws-1", "/work/_shared/auth/ws-1")
    base = _api_base(
        status="partial",
        reason_code="CLEANUP_EXECUTION_PARTIAL",
        candidates=[candidate],
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

    assert folded["status"] == "partial"
    folded_candidates = folded["candidates"]
    assert isinstance(folded_candidates, list)
    assert folded_candidates[0]["paths"]["auth"]["deleted"] is True
    assert folded_candidates[0]["paths"]["auth"]["status"] == "deleted"


def test_fold_leaves_candidate_auth_entry_worker_did_not_reclaim() -> None:
    # An auth dir the worker did NOT remove keeps its skipped per-candidate entry —
    # only paths in the worker's ``deleted_paths`` are reconciled.
    candidate = _candidate_with_skipped_auth("ws-2", "/work/_shared/auth/ws-2")
    base = _api_base(
        status="partial",
        reason_code="CLEANUP_EXECUTION_PARTIAL",
        candidates=[candidate],
    )
    outcome = WorkerReclaimOutcome.from_report(
        {
            "status": "partial",
            "deleted_path_count": 1,
            "deleted_paths": ["/work/_shared/auth/ws-1"],
        }
    )

    folded = fold_worker_reclaim(base, outcome)

    folded_candidates = folded["candidates"]
    assert isinstance(folded_candidates, list)
    auth = folded_candidates[0]["paths"]["auth"]
    assert auth["deleted"] is False
    assert auth["status"] == "skipped"
    assert "reconciled_by_worker" not in auth


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


def test_claude_base_reaped_paths_empty_when_reaped_not_a_list() -> None:
    # ``reaped`` is external worker JSON; a malformed non-list value (e.g. the worker
    # serialized a scalar instead of an array) must yield no reaped base paths rather
    # than iterating a string/int — the documented fallback is the empty list.
    report = {
        "claude_base_reap": {
            "status": "ok",
            "base_root": "/work/auth/_shared/claude-base",
            "reaped": "sigA",
        }
    }

    assert _claude_base_reaped_path_strs(report) == []


def test_reconcile_candidate_auth_returns_non_mapping_candidate_unchanged() -> None:
    # ``candidates`` entries come from external worker JSON; a non-mapping element
    # (e.g. a bare string) is returned verbatim so the caller leaves the list as-is.
    sentinel = "not-a-candidate-mapping"

    result = _reconcile_candidate_auth(sentinel, frozenset({"/work/_shared/auth/ws-1"}))

    assert result is sentinel


def test_reconcile_candidate_auth_returns_candidate_when_auth_not_a_mapping() -> None:
    # ``paths.auth`` is malformed (a scalar rather than the expected entry object); the
    # candidate is returned unchanged so the per-candidate audit view is left verbatim.
    candidate = {
        "workspace_id": "ws-1",
        "paths": {"auth": "garbled-auth-entry"},
    }

    result = _reconcile_candidate_auth(candidate, frozenset({"/work/_shared/auth/ws-1"}))

    assert result is candidate


def test_fold_keeps_partial_when_companion_image_prune_failed() -> None:
    # The auth-unmount skip is reconcilable, but an unrelated companion-image prune
    # failure (a Mapping with ``status: failed``) is a failure the worker reclaim does
    # not supersede, so the headline must stay partial.
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
        companion_image_prune={"status": "failed", "error": "docker image prune failed"},
    )
    outcome = WorkerReclaimOutcome.from_report(
        {
            "status": "succeeded",
            "deleted_path_count": 1,
            "deleted_paths": ["/work/_shared/auth/ws-1"],
        }
    )

    folded = fold_worker_reclaim(base, outcome)

    # The reconcilable auth skip is dropped, but the run stays partial for the prune.
    assert folded["status"] == "partial"
    assert folded["reason_code"] == "CLEANUP_EXECUTION_PARTIAL"
    assert folded["delete_errors"] == []


def test_fold_keeps_partial_when_reservation_release_carries_error() -> None:
    # ``reservation_releases`` is a Mapping of workspace_id -> release outcome. One
    # release carrying an ``error`` is an unreconciled failure, so even after dropping
    # the reconcilable auth skip the headline stays partial.
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
        reservation_releases={
            "ws-1": {"error": None},
            "ws-2": {"error": "lease release failed"},
        },
    )
    outcome = WorkerReclaimOutcome.from_report(
        {
            "status": "succeeded",
            "deleted_path_count": 1,
            "deleted_paths": ["/work/_shared/auth/ws-1"],
        }
    )

    folded = fold_worker_reclaim(base, outcome)

    assert folded["status"] == "partial"
    assert folded["reason_code"] == "CLEANUP_EXECUTION_PARTIAL"


def test_fold_restores_success_when_reservation_releases_all_clean() -> None:
    # When every ``reservation_releases`` entry is a Mapping with no ``error``, there is
    # no unreconciled failure: dropping the stale auth skip restores the headline to
    # succeeded.
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
        reservation_releases={
            "ws-1": {"error": None},
            "ws-2": {"error": None},
        },
    )
    outcome = WorkerReclaimOutcome.from_report(
        {
            "status": "succeeded",
            "deleted_path_count": 1,
            "deleted_paths": ["/work/_shared/auth/ws-1"],
        }
    )

    folded = fold_worker_reclaim(base, outcome)

    assert folded["status"] == "succeeded"
    assert folded["reason_code"] == "CLEANUP_EXECUTION_SUCCEEDED"


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
