"""Behavior tests for the purpose-named ``guide`` operator control (issue #447)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import OperationType, WorkspaceStatus
from awf.runtime.operator_hints import (
    OPERATOR_HINT_STATE_KEY,
    operator_hint_from_threads,
)
from awf.service.controls import (
    WorkspaceGuideMissingPrUrlError,
    WorkspaceGuideStateError,
)
from tests.postgres import postgres_test_session
from tests.unit.service.test_controls_lifecycle_parts.controls_lifecycle_helpers import (
    _events,
    _operations,
    _service,
    _workspace,
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
        "eligible_statuses": [WorkspaceStatus.monitoring_pr.value],
    }
    assert missing_pr_error.value.detail == {"status": WorkspaceStatus.monitoring_pr.value}
    assert await _operations(session, requested.id) == []
    assert await _operations(session, missing_pr.id) == []


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
    assert restored.operation_id != "op_old"
