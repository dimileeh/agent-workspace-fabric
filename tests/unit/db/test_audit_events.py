"""Structured audit events stored through workspace_events."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.audit import AUDIT_SCHEMA
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import OperationRepository, WorkspaceEventRepository, WorkspaceRepository
from tests.postgres import postgres_test_session


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_session() as s:
        yield s


@pytest.mark.unit
async def test_add_audit_event_is_query_compatible_and_does_not_transition_workspace(
    session: AsyncSession,
) -> None:
    repo = WorkspaceRepository(session)
    workspace = await repo.create(
        repo_url="git@github.com:example/audit.git",
        branch_base="main",
        task_title="audit event",
        task_prompt="record audit",
        agent="codex",
        test_commands=[],
    )
    await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="PROVISIONING")
    await repo.transition(workspace, to=WorkspaceStatus.ready, reason_code="READY")
    operation = await OperationRepository(session).create(
        workspace_id=workspace.id,
        operation_type=OperationType.push,
        status=OperationStatus.running,
        payload={"source": "executor"},
    )

    event = await repo.add_audit_event(
        workspace,
        event_type="workspace.audit.git_push",
        actor="executor",
        action="git_push",
        outcome="succeeded",
        reason_code="VALIDATION_OK",
        operation_id=operation.id,
        operation_type=operation.type,
        pr_number=88,
        pr_url="https://github.com/example/audit/pull/88",
        remote_branch="awf/ws_audit",
        evidence={"log_stream_refs": {"push": "executor.push"}},
    )
    await session.commit()

    rows = await WorkspaceEventRepository(session).list(
        workspace_id=workspace.id,
        event_type="workspace.audit.git_push",
        limit=10,
    )

    assert [row.id for row in rows] == [event.id]
    assert rows[0].old_state == WorkspaceStatus.ready.value
    assert rows[0].new_state == WorkspaceStatus.ready.value
    assert rows[0].reason_code == "VALIDATION_OK"
    assert rows[0].payload == {
        "schema": AUDIT_SCHEMA,
        "actor": "executor",
        "source": "executor",
        "action": "git_push",
        "outcome": "succeeded",
        "reason_code": "VALIDATION_OK",
        "operation_id": operation.id,
        "operation_type": OperationType.push.value,
        "pr_number": 88,
        "pr_url": "https://github.com/example/audit/pull/88",
        "remote_branch": "awf/ws_audit",
        "evidence": {"log_stream_refs": {"push": "executor.push"}},
    }
    assert workspace.status == WorkspaceStatus.ready.value
