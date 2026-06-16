"""Behavior tests for the purpose-named ``guide`` operator control (issue #447)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import MergeCandidate
from awf.db.repositories import MergeCandidateRepository, OperationRepository, TaskAttemptRepository
from awf.runtime.monitor_state_keys import _non_check_reviewer_settle_done_key
from awf.runtime.operator_hints import (
    OPERATOR_HINT_STATE_KEY,
    operator_hint_from_threads,
)
from awf.service.controls import (
    WorkspaceGuideEmptyDirectiveError,
    WorkspaceGuideMissingPrUrlError,
    WorkspaceGuideStateError,
)
from tests.postgres import postgres_test_session
from tests.unit.service.test_controls_lifecycle_parts.controls_lifecycle_helpers import (
    _events,
    _operations,
    _service,
    _workspace,
    _workspace_with_candidate,
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_session() as s:
        yield s


async def _monitoring_workspace(session: AsyncSession) -> object:
    workspace = await _workspace(session, status=WorkspaceStatus.monitoring_pr)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/77"
    workspace.pr_number = 77
    workspace.base_commit = "b" * 40
    workspace.monitor_last_commit_sha = "h" * 40
    await session.flush()
    return workspace


@pytest.mark.unit
async def test_guide_arms_pending_hint_with_directive_distinct_from_reason(
    session: AsyncSession,
) -> None:
    workspace = await _monitoring_workspace(session)
    service, _stopper, _cleaner = _service(session)

    response = await service.guide_workspace(
        workspace.id,
        directive="implement the forge-neutral fix, do not defer",
        reason="operator decision recorded",
        idempotency_key="guide-key",
        expected_version=workspace.version,
    )
    operations = await _operations(session, workspace.id)
    events = await _events(session, workspace.id)
    monitor_state = dict(workspace.monitor_threads_addressed or {})
    persisted = json.loads(monitor_state[OPERATOR_HINT_STATE_KEY])

    assert response.status == WorkspaceStatus.monitoring_pr
    # No state transition: guide stays monitoring_pr.
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    assert workspace.version == 2
    assert operations[0].type == OperationType.guide.value
    assert operations[0].status == "succeeded"
    assert persisted["directive"] == "implement the forge-neutral fix, do not defer"
    assert persisted["reason"] == "operator decision recorded"
    assert persisted["reason_code"] == "OPERATOR_GUIDE"
    assert persisted["status"] == "pending"
    # Round-trips back into a live OperatorHint the monitor can act on.
    restored = operator_hint_from_threads(monitor_state)
    assert restored is not None
    assert restored.directive == "implement the forge-neutral fix, do not defer"

    guide_event = next(e for e in events if e.event_type == "workspace.guide_requested")
    assert guide_event.reason_code == "OPERATOR_GUIDE"
    assert guide_event.payload["directive"] == "implement the forge-neutral fix, do not defer"
    assert guide_event.payload["pending_operator_hint"] == persisted
    # The control is audited via the shared control-operation audit event.
    audit_event = next(e for e in events if e.event_type == "workspace.audit.control_operation")
    assert audit_event.reason_code == "OPERATOR_GUIDE"
    assert audit_event.payload["action"] == OperationType.guide.value


@pytest.mark.unit
async def test_guide_reason_optional_falls_back_to_directive_for_hint_reason(
    session: AsyncSession,
) -> None:
    workspace = await _monitoring_workspace(session)
    service, _stopper, _cleaner = _service(session)

    await service.guide_workspace(
        workspace.id,
        directive="address the deferred merge-gate finding",
        idempotency_key="guide-no-reason",
        expected_version=workspace.version,
    )
    monitor_state = dict(workspace.monitor_threads_addressed or {})
    persisted = json.loads(monitor_state[OPERATOR_HINT_STATE_KEY])

    # reason is audit-only and optional; the hint reason must stay non-empty so
    # operator_hint_from_threads keeps treating it as a live hint.
    assert persisted["reason"] == "address the deferred merge-gate finding"
    assert persisted["directive"] == "address the deferred merge-gate finding"


@pytest.mark.unit
async def test_guide_resets_claims_and_replays_exact_key(
    session: AsyncSession,
) -> None:
    workspace = await _monitoring_workspace(session)
    workspace.monitor_claimed_by = "monitor-worker"
    workspace.execution_claimed_by = "execution-worker"
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    response = await service.guide_workspace(
        workspace.id,
        directive="do the thing",
        idempotency_key="guide-replay",
        expected_version=workspace.version,
    )
    replay = await service.guide_workspace(
        workspace.id,
        directive="do the thing",
        idempotency_key="guide-replay",
        expected_version=workspace.version - 1,
    )
    operations = await _operations(session, workspace.id)

    assert response.operation_id == replay.operation_id
    assert len(operations) == 1
    assert workspace.monitor_claimed_by is None
    assert workspace.execution_claimed_by is None
    assert operations[0].result["claims_reset"]["monitor_claimed_by"] == "monitor-worker"


@pytest.mark.unit
async def test_guide_idempotency_identity_ignores_reason_whitespace(
    session: AsyncSession,
) -> None:
    """A whitespace-only variant of ``reason`` must replay, not conflict.

    The idempotency payload hashes the *stripped* reason so it matches the
    persisted hint (which stores ``reason_text``). A direct Python/MCP caller
    retrying with " operator note " after "operator note" under the same key
    therefore hits the cached operation instead of an idempotency conflict.
    """
    workspace = await _monitoring_workspace(session)
    service, _stopper, _cleaner = _service(session)

    response = await service.guide_workspace(
        workspace.id,
        directive="do the thing",
        reason="operator note",
        idempotency_key="guide-reason-whitespace",
        expected_version=workspace.version,
    )
    replay = await service.guide_workspace(
        workspace.id,
        directive="do the thing",
        reason="  operator note  ",
        idempotency_key="guide-reason-whitespace",
        expected_version=workspace.version - 1,
    )
    operations = await _operations(session, workspace.id)

    assert response.operation_id == replay.operation_id
    assert len(operations) == 1


@pytest.mark.unit
async def test_guide_idempotency_identity_excludes_volatile_pr_head(
    session: AsyncSession,
) -> None:
    """A same-key retry must replay even after the monitor records a new head.

    Volatile PR/head context (``source_head_sha``/``source_base_sha``) is
    persisted for provenance but kept out of the idempotency identity. When the
    first response is lost and the monitor pushes/records a new head before the
    client retries with the same key, the changed head must not turn a safe
    retry into IDEMPOTENCY_CONFLICT — the original operation is replayed.
    """
    workspace = await _monitoring_workspace(session)
    service, _stopper, _cleaner = _service(session)

    response = await service.guide_workspace(
        workspace.id,
        directive="do the thing",
        idempotency_key="guide-head-moved",
        expected_version=workspace.version,
    )
    # The monitor pushes a new head and records it before the client retries.
    workspace.monitor_last_commit_sha = "f" * 40
    workspace.base_commit = "e" * 40
    await session.flush()

    replay = await service.guide_workspace(
        workspace.id,
        directive="do the thing",
        idempotency_key="guide-head-moved",
        expected_version=workspace.version - 1,
    )
    operations = await _operations(session, workspace.id)

    assert response.operation_id == replay.operation_id
    assert len(operations) == 1
    # The originally persisted head context is preserved on the replayed op.
    assert operations[0].payload["source_head_sha"] == "h" * 40
    assert operations[0].payload["source_base_sha"] == "b" * 40


@pytest.mark.unit
async def test_guide_preserves_unexpired_monitor_and_execution_leases(
    session: AsyncSession,
) -> None:
    """Guide only re-arms a pending directive; it must not evict a *live* lease.

    ``claim_monitoring_pr`` treats a NULL ``monitor_claim_expires_at`` as
    immediately claimable, so nulling an unexpired lease would let a second
    worker start a duplicate monitor while the original loop is still running
    (the monitor heartbeat only stops on claim-loss, it never aborts the run).
    Unexpired leases are preserved and excluded from ``claims_reset``.
    """
    workspace = await _monitoring_workspace(session)
    future = datetime.now(UTC) + timedelta(minutes=10)
    workspace.monitor_claimed_by = "monitor-worker"
    workspace.monitor_claim_expires_at = future
    workspace.execution_claimed_by = "execution-worker"
    workspace.execution_claim_expires_at = future
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    response = await service.guide_workspace(
        workspace.id,
        directive="implement, do not defer",
        idempotency_key="guide-preserve-live-lease",
        expected_version=workspace.version,
    )
    operations = await _operations(session, workspace.id)

    # The live leases survive so the in-flight monitor keeps exclusive ownership.
    assert workspace.monitor_claimed_by == "monitor-worker"
    assert workspace.monitor_claim_expires_at == future
    assert workspace.execution_claimed_by == "execution-worker"
    assert workspace.execution_claim_expires_at == future
    assert response.status == WorkspaceStatus.monitoring_pr
    # claims_reset reports only claims actually cleared (none here).
    claims_reset = operations[0].result["claims_reset"]
    assert claims_reset == {
        "monitor_claimed_by": None,
        "monitor_claim_expires_at": None,
        "execution_claimed_by": None,
        "execution_claim_expires_at": None,
    }
    # The directive is still persisted for the next monitor cycle to pick up.
    monitor_state = dict(workspace.monitor_threads_addressed or {})
    persisted = json.loads(monitor_state[OPERATOR_HINT_STATE_KEY])
    assert persisted["directive"] == "implement, do not defer"
    assert persisted["status"] == "pending"


@pytest.mark.unit
async def test_guide_cancels_stale_pr_monitor_recovery_ops(
    session: AsyncSession,
) -> None:
    """Guide re-engages the monitor, so it must pre-empt in-flight PR-monitor
    recovery ops (same guard as remonitor); operator-launched ops are left alone."""
    workspace = await _monitoring_workspace(session)
    operation_repo = OperationRepository(session)
    stale_validate = await operation_repo.create(
        workspace_id=workspace.id,
        operation_type=OperationType.validate,
        status=OperationStatus.running,
        payload={
            "owner": "pr_monitor",
            "source": "pr_monitor",
            "recovery_mode": "validate_only",
            "reason_code": "VALIDATION_INSUFFICIENT_TIER",
        },
    )
    operator_validate = await operation_repo.create(
        workspace_id=workspace.id,
        operation_type=OperationType.validate,
        status=OperationStatus.running,
        payload={
            "owner": "operator",
            "source": "operator_api",
            "recovery_mode": "validate_only",
            "reason_code": "OPERATOR_VALIDATE",
        },
    )
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    await service.guide_workspace(
        workspace.id,
        directive="implement, do not defer",
        idempotency_key="guide-cancel-stale-recovery",
        expected_version=workspace.version,
    )
    operations = {op.id: op for op in await _operations(session, workspace.id)}
    events = await _events(session, workspace.id)

    assert operations[stale_validate.id].status == OperationStatus.cancelled.value
    assert operations[stale_validate.id].error_code == "OPERATOR_GUIDE"
    assert operations[stale_validate.id].result == {
        "status": "cancelled",
        "reason_code": "OPERATOR_GUIDE",
        "requested_action": "guide",
    }
    # Operator-launched recovery ops are not pr_monitor-owned, so they survive.
    assert operations[operator_validate.id].status == OperationStatus.running.value
    guide_event = next(e for e in events if e.event_type == "workspace.guide_requested")
    assert guide_event.payload["cancelled_recovery_operations"] == [
        {
            "operation_id": stale_validate.id,
            "operation_type": OperationType.validate.value,
            "operation_status": OperationStatus.running.value,
        }
    ]
    assert guide_event.payload["cancelled_recovery_reason_code"] == "OPERATOR_GUIDE"
    assert guide_event.payload["cancelled_recovery_requested_action"] == "guide"
    guide_op = next(op for op in operations.values() if op.type == OperationType.guide.value)
    assert guide_op.result["cancelled_recovery_operations"] == [
        {
            "operation_id": stale_validate.id,
            "operation_type": OperationType.validate.value,
            "operation_status": OperationStatus.running.value,
        }
    ]


@pytest.mark.unit
async def test_guide_rejects_wrong_state_and_missing_pr_before_creating_operation(
    session: AsyncSession,
) -> None:
    requested = await _workspace(session, status=WorkspaceStatus.requested)
    missing_pr = await _workspace(
        session,
        status=WorkspaceStatus.monitoring_pr,
        title="monitoring without pr",
    )
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceGuideStateError) as wrong_state:
        await service.guide_workspace(requested.id, directive="x")
    with pytest.raises(WorkspaceGuideMissingPrUrlError) as missing_pr_error:
        await service.guide_workspace(missing_pr.id, directive="x")

    assert wrong_state.value.detail == {
        "status": WorkspaceStatus.requested.value,
        "eligible_statuses": [
            WorkspaceStatus.monitoring_pr.value,
            WorkspaceStatus.failed.value,
            WorkspaceStatus.blocked.value,
        ],
    }
    assert missing_pr_error.value.detail == {"status": WorkspaceStatus.monitoring_pr.value}
    assert await _operations(session, requested.id) == []
    assert await _operations(session, missing_pr.id) == []


@pytest.mark.unit
@pytest.mark.parametrize("directive", ["", "   ", "\t\n"])
async def test_guide_rejects_empty_or_whitespace_directive_before_creating_operation(
    session: AsyncSession, directive: str
) -> None:
    """A blank directive must never persist an empty operator hint.

    REST strips whitespace at the schema boundary, but the MCP tool only sets an
    advisory ``minLength`` (not an enforced constraint), so the service itself
    guards the directive and fails fast before touching the DB.
    """
    workspace = await _monitoring_workspace(session)
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceGuideEmptyDirectiveError) as empty_directive:
        await service.guide_workspace(
            workspace.id,
            directive=directive,
            idempotency_key="guide-empty",
            expected_version=workspace.version,
        )

    assert empty_directive.value.error_code == "WORKSPACE_GUIDE_DIRECTIVE_REQUIRED"
    assert await _operations(session, workspace.id) == []
    assert workspace.monitor_threads_addressed in (None, {})


@pytest.mark.unit
async def test_guide_rearms_pending_hint_after_prior_needs_human_wait(
    session: AsyncSession,
) -> None:
    """A workspace stuck on a needs_human hint (prior NotifyHuman) is re-engaged:
    guide overwrites it with a fresh pending directive hint, closing the loop."""
    workspace = await _monitoring_workspace(session)
    # Simulate a prior deferral that left a non-pending (needs_human) hint.
    workspace.monitor_threads_addressed = {
        OPERATOR_HINT_STATE_KEY: json.dumps(
            {
                "reason": "agent deferred two findings",
                "operation_id": "op_old",
                "reason_code": "OPERATOR_REMONITOR",
                "status": "needs_human",
                "status_reason": "needs a human decision",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    }
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    await service.guide_workspace(
        workspace.id,
        directive="implement, do not defer",
        idempotency_key="guide-close-loop",
        expected_version=workspace.version,
    )
    monitor_state = dict(workspace.monitor_threads_addressed or {})
    restored = operator_hint_from_threads(monitor_state)

    assert restored is not None
    assert restored.status == "pending"
    assert restored.directive == "implement, do not defer"


@pytest.mark.unit
async def test_guide_failed_workspace_with_pr_resets_for_monitor_restart(
    session: AsyncSession,
) -> None:
    """Guide on a failed workspace with a PR resets it to monitoring_pr (issue #456)."""
    workspace = await _workspace(session, status=WorkspaceStatus.failed)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/88"
    workspace.pr_number = 88
    workspace.base_commit = "b" * 40
    workspace.monitor_last_commit_sha = "h" * 40
    workspace.failure_reason = "infrastructure_failure"
    workspace.failure_message = "monitor crashed during ci wait"
    workspace.monitor_iter_count = 6
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    response = await service.guide_workspace(
        workspace.id,
        directive="fix the rebase conflict and continue",
        reason="operator decision to restart failed monitor",
        idempotency_key="guide-failed-reset",
        expected_version=workspace.version,
    )
    operations = await _operations(session, workspace.id)
    events = await _events(session, workspace.id)
    monitor_state = dict(workspace.monitor_threads_addressed or {})
    persisted = json.loads(monitor_state[OPERATOR_HINT_STATE_KEY])

    assert response.status == WorkspaceStatus.monitoring_pr
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    assert workspace.failure_reason is None
    assert workspace.failure_message is None
    assert workspace.monitor_iter_count == 0
    assert workspace.version == 2
    assert operations[0].status == "succeeded"
    assert persisted["directive"] == "fix the rebase conflict and continue"
    assert persisted["reason"] == "operator decision to restart failed monitor"
    assert persisted["reason_code"] == "OPERATOR_GUIDE"
    assert persisted["status"] == "pending"
    guide_event = next(e for e in events if e.event_type == "workspace.guide_requested")
    assert guide_event.reason_code == "OPERATOR_GUIDE"
    assert guide_event.old_state == WorkspaceStatus.failed.value
    assert guide_event.new_state == WorkspaceStatus.monitoring_pr.value
    assert guide_event.payload["state_reset"] == {
        "from": WorkspaceStatus.failed.value,
        "to": WorkspaceStatus.monitoring_pr.value,
        "monitor_iter_count_reset_from": 6,
        "candidate_reopened": False,
    }
    audit_event = next(e for e in events if e.event_type == "workspace.audit.control_operation")
    assert audit_event.reason_code == "OPERATOR_GUIDE"
    assert audit_event.payload["action"] == OperationType.guide.value


@pytest.mark.unit
async def test_guide_failed_workspace_reopens_merge_candidate(
    session: AsyncSession,
) -> None:
    """Guide on a failed workspace with a closed candidate reopens it (issue #456)."""
    workspace, candidate = await _workspace_with_candidate(
        session,
        status=WorkspaceStatus.failed,
        title="failed candidate guide",
    )
    workspace.failure_reason = "infrastructure_failure"
    workspace.failure_message = "monitor crashed during repair"
    workspace.monitor_iter_count = 5
    candidate.status = "closed"
    candidate.close_reason = "STALE_MONITOR_FAILED"
    candidate.closed_at = datetime.now(UTC)
    candidate.merged_at = datetime.now(UTC)
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    response = await service.guide_workspace(
        workspace.id,
        directive="continue with the fix",
        idempotency_key="guide-failed-candidate",
        expected_version=workspace.version,
    )
    attempt = await TaskAttemptRepository(session).get_by_workspace_id(workspace.id)
    events = await _events(session, workspace.id)

    assert response.status == WorkspaceStatus.monitoring_pr
    assert attempt is not None
    assert attempt.status == WorkspaceStatus.monitoring_pr.value
    assert candidate.status == "open"
    assert candidate.close_reason is None
    assert candidate.closed_at is None
    assert candidate.merged_at is None
    guide_event = next(e for e in events if e.event_type == "workspace.guide_requested")
    assert guide_event.payload["state_reset"]["candidate_reopened"] is True


@pytest.mark.unit
async def test_guide_failed_workspace_without_pr_rejected(
    session: AsyncSession,
) -> None:
    """Guide on a failed workspace with no PR raises MissingPrUrl, not StateError (issue #456)."""
    workspace = await _workspace(session, status=WorkspaceStatus.failed)
    # No pr_url set
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceGuideMissingPrUrlError) as exc_info:
        await service.guide_workspace(workspace.id, directive="fix the issue")

    assert exc_info.value.error_code == "WORKSPACE_PR_URL_REQUIRED"
    assert await _operations(session, workspace.id) == []


@pytest.mark.unit
async def test_guide_failed_workspace_idempotency_replays(
    session: AsyncSession,
) -> None:
    """Same-key retry on a guide-from-failed workspace returns the cached operation (issue #456)."""
    workspace = await _workspace(session, status=WorkspaceStatus.failed)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/89"
    workspace.pr_number = 89
    workspace.base_commit = "b" * 40
    workspace.monitor_last_commit_sha = "h" * 40
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    response = await service.guide_workspace(
        workspace.id,
        directive="continue with the implementation",
        idempotency_key="guide-failed-idempotency",
        expected_version=workspace.version,
    )
    # After the first call workspace.version == 2 (state reset advanced it).
    replay = await service.guide_workspace(
        workspace.id,
        directive="continue with the implementation",
        idempotency_key="guide-failed-idempotency",
        expected_version=workspace.version - 1,
    )
    operations = await _operations(session, workspace.id)

    assert response.operation_id == replay.operation_id
    assert len(operations) == 1


@pytest.mark.unit
async def test_guide_failed_workspace_force_clears_unexpired_claims(
    session: AsyncSession,
) -> None:
    """Guide on a failed workspace must null unexpired claims from a dead worker.

    A failed workspace has no live worker; its monitor/execution claims
    (even if unexpired) are orphaned. If guide only clears *stale* leases,
    the failed→monitoring_pr transition leaves the row unclaimable until
    the lease expires — new workers see a live claim and skip it.
    Unlike guide on a live monitoring_pr workspace (where a real worker
    holds the lease and picks up the directive on its next cycle), the
    failed path must force-null all claims so claim_monitoring_pr can
    acquire immediately (mirrors remonitor_workspace).
    """
    workspace = await _workspace(session, status=WorkspaceStatus.failed)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/92"
    workspace.pr_number = 92
    workspace.base_commit = "b" * 40
    workspace.monitor_last_commit_sha = "h" * 40
    future = datetime.now(UTC) + timedelta(minutes=10)
    workspace.monitor_claimed_by = "dead-monitor-worker"
    workspace.monitor_claim_expires_at = future
    workspace.execution_claimed_by = "dead-execution-worker"
    workspace.execution_claim_expires_at = future
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    response = await service.guide_workspace(
        workspace.id,
        directive="continue after failure",
        idempotency_key="guide-failed-clear-claims",
        expected_version=workspace.version,
    )
    operations = await _operations(session, workspace.id)

    assert response.status == WorkspaceStatus.monitoring_pr
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    # Orphaned claims from the dead worker must be cleared unconditionally.
    assert workspace.monitor_claimed_by is None
    assert workspace.monitor_claim_expires_at is None
    assert workspace.execution_claimed_by is None
    assert workspace.execution_claim_expires_at is None
    # claims_reset must record the force-cleared values for the audit trail.
    claims_reset = operations[0].result["claims_reset"]
    assert claims_reset["monitor_claimed_by"] == "dead-monitor-worker"
    assert claims_reset["execution_claimed_by"] == "dead-execution-worker"


@pytest.mark.unit
async def test_guide_failed_workspace_uses_workspace_sha_when_past_settle(
    session: AsyncSession,
) -> None:
    """Guide on a failed workspace uses workspace monitor_last_commit_sha when settle elapsed.

    When the reviewer-settle window has elapsed for the workspace's head SHA and
    the workspace was updated more recently than the (still-open) candidate, guide
    must reopen the merge candidate with workspace.monitor_last_commit_sha — not
    the stale candidate.head_sha.  This mirrors remonitor_workspace's
    _remonitor_current_head_sha logic, which guide omitted before this fix
    (thread PRRT_kwDOSJAM6s6Hr-2z).

    The candidate must be open for the settle preference to fire; a closed
    candidate falls back to latest.head_sha in both remonitor and guide.
    """
    workspace, candidate = await _workspace_with_candidate(
        session,
        status=WorkspaceStatus.failed,
        title="guide settle head sha",
    )
    workspace.failure_reason = "infrastructure_failure"
    workspace.failure_message = "monitor crashed"
    workspace.monitor_iter_count = 3
    # Advance the workspace to a newer head SHA while the open candidate still
    # holds the older one — this is the state after the monitor pushed a new
    # commit but failed before it could update the candidate row.
    workspace_sha = "w" * 40
    candidate_sha = "c" * 40
    workspace.monitor_last_commit_sha = workspace_sha
    candidate.head_sha = candidate_sha
    # Keep the candidate open so _remonitor_current_head_sha can compare the two SHAs.
    candidate.status = "open"
    await session.flush()
    # workspace.updated_at must be > candidate.updated_at for the settle preference
    # to activate; backdate the candidate to guarantee the ordering.
    await session.execute(
        sa_update(MergeCandidate)
        .where(MergeCandidate.id == candidate.id)
        .values(updated_at=workspace.updated_at - timedelta(minutes=5))
    )
    await session.flush()
    # Mark the reviewer-settle window as elapsed for the workspace's head SHA.
    settle_key = _non_check_reviewer_settle_done_key(
        pr_number=workspace.pr_number, head_sha=workspace_sha
    )
    workspace.monitor_threads_addressed = {settle_key: "elapsed"}
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    await service.guide_workspace(
        workspace.id,
        directive="continue after failure",
        idempotency_key="guide-settle-sha",
        expected_version=workspace.version,
    )
    refreshed = await MergeCandidateRepository(session).get_by_attempt_id(candidate.attempt_id)

    assert refreshed is not None
    assert refreshed.status == "open"
    # Guide must use the workspace's post-settle SHA, not the stale candidate SHA.
    assert refreshed.head_sha == workspace_sha
