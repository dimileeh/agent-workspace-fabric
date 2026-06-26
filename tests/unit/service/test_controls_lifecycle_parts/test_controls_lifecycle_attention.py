"""Regression tests for clearing persisted HUMAN_WAIT attention on operator re-engagement.

Operator ``remonitor_workspace`` and the ``monitoring_pr`` ``guide_workspace`` path
are explicit operator re-engagement: they re-arm the monitor (persisting the
operator hint / clearing stale claims) so the next ``decide()`` cycle re-engages
the agent even from a prior ``NotifyHuman`` wait. They must ALSO clear the
out-of-band ``awaiting_human_since`` / ``awaiting_human_reason`` attention
columns persisted by the monitor's HUMAN_WAIT episode, mirroring the in-process
monitor resume clear — otherwise the workspace row stays stuck with
``attention_required=true`` after the operator has already acted (live
awf-cloud PR231/PR208). A future genuine ``NotifyHuman`` may set a new episode
later via the monitor; that path is unchanged.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import OperationType, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.runtime.operator_hints import OPERATOR_HINT_STATE_KEY
from tests.postgres import postgres_test_session
from tests.unit.service.test_controls_lifecycle_parts.controls_lifecycle_helpers import (
    _events,
    _operations,
    _service,
    _workspace_with_candidate,
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_session() as s:
        yield s


async def _persist_human_wait_episode(
    session: AsyncSession,
    workspace_id: str,
    *,
    reason: str = "awaiting operator guidance on a protected-scope block",
) -> datetime:
    """Stamp a HUMAN_WAIT attention episode directly via the monitor write surface."""
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    await WorkspaceRepository(session).set_workspace_attention(workspace_id, reason=reason, now=now)
    return now


@pytest.mark.unit
async def test_remonitor_clears_persisted_human_attention(session: AsyncSession) -> None:
    workspace, _candidate = await _workspace_with_candidate(
        session,
        status=WorkspaceStatus.monitoring_pr,
        title="remonitor clears attention",
    )
    monitor_expiry = datetime(2026, 5, 1, 12, 30, tzinfo=UTC)
    execution_expiry = monitor_expiry + timedelta(minutes=10)
    workspace.monitor_claimed_by = "monitor-worker"
    workspace.monitor_claim_expires_at = monitor_expiry
    workspace.execution_claimed_by = "execution-worker"
    workspace.execution_claim_expires_at = execution_expiry
    await session.flush()
    await _persist_human_wait_episode(session, workspace.id)
    await session.refresh(workspace)
    assert workspace.awaiting_human_since is not None
    assert workspace.awaiting_human_reason is not None
    service, _stopper, _cleaner = _service(session)

    response = await service.remonitor_workspace(
        workspace.id,
        reason="operator re-engaged the monitor",
        idempotency_key="remonitor-clears-attention",
        expected_version=workspace.version,
    )
    operations = await _operations(session, workspace.id)
    events = await _events(session, workspace.id)
    await session.refresh(workspace)

    assert response.status == WorkspaceStatus.monitoring_pr
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    assert workspace.awaiting_human_since is None
    assert workspace.awaiting_human_reason is None
    # Existing claim reset / worker resume behavior preserved.
    assert workspace.monitor_claimed_by is None
    assert workspace.monitor_claim_expires_at is None
    assert workspace.execution_claimed_by is None
    assert workspace.execution_claim_expires_at is None
    assert workspace.version == 2
    assert operations[0].type == OperationType.remonitor.value
    assert operations[0].status == "succeeded"
    assert any(e.event_type == "workspace.remonitor_requested" for e in events)


@pytest.mark.unit
async def test_guide_clears_persisted_human_attention_for_monitoring_pr(
    session: AsyncSession,
) -> None:
    workspace, _candidate = await _workspace_with_candidate(
        session,
        status=WorkspaceStatus.monitoring_pr,
        title="guide clears attention",
    )
    await _persist_human_wait_episode(session, workspace.id)
    await session.refresh(workspace)
    assert workspace.awaiting_human_since is not None
    assert workspace.awaiting_human_reason is not None
    service, _stopper, _cleaner = _service(session)

    response = await service.guide_workspace(
        workspace.id,
        directive="implement the forge-neutral fix, do not defer",
        reason="operator decision recorded",
        idempotency_key="guide-clears-attention",
        expected_version=workspace.version,
    )
    operations = await _operations(session, workspace.id)
    events = await _events(session, workspace.id)
    await session.refresh(workspace)

    assert response.status == WorkspaceStatus.monitoring_pr
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    assert workspace.awaiting_human_since is None
    assert workspace.awaiting_human_reason is None
    # Existing guide behavior preserved: pending operator hint persisted.
    monitor_state = dict(workspace.monitor_threads_addressed or {})
    assert OPERATOR_HINT_STATE_KEY in monitor_state
    assert workspace.version == 2
    assert operations[0].type == OperationType.guide.value
    assert operations[0].status == "succeeded"
    assert any(e.event_type == "workspace.guide_requested" for e in events)


@pytest.mark.unit
async def test_remonitor_no_attention_episode_is_unchanged(session: AsyncSession) -> None:
    """A workspace with no HUMAN_WAIT episode is untouched by the guarded clear."""
    workspace, _candidate = await _workspace_with_candidate(
        session,
        status=WorkspaceStatus.monitoring_pr,
        title="remonitor no episode",
    )
    await session.refresh(workspace)
    assert workspace.awaiting_human_since is None
    assert workspace.awaiting_human_reason is None
    service, _stopper, _cleaner = _service(session)

    response = await service.remonitor_workspace(
        workspace.id,
        reason="operator re-engaged the monitor",
        idempotency_key="remonitor-no-episode",
        expected_version=workspace.version,
    )
    await session.refresh(workspace)

    assert response.status == WorkspaceStatus.monitoring_pr
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    assert workspace.awaiting_human_since is None
    assert workspace.awaiting_human_reason is None
