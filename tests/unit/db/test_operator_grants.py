"""ORM tests for block-state columns and operator grant audit records."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import WorkspaceStatus
from awf.db.models import OperatorGrantAuditRecord, Workspace
from awf.db.repositories import WorkspaceRepository
from tests.postgres import postgres_test_session


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_session() as s:
        yield s


async def _workspace(session: AsyncSession) -> Workspace:
    workspace = await WorkspaceRepository(session).create(
        repo_url="git@github.com:example/grants.git",
        branch_base="main",
        task_title="operator grant test",
        task_prompt="exercise block-state columns",
        agent="codex",
        test_commands=[],
    )
    workspace.status = WorkspaceStatus.running.value
    await session.flush()
    return workspace


@pytest.mark.unit
async def test_block_state_columns_default_and_persist(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    # Defaults: block_epoch starts at 0, the rest are unset.
    assert workspace.block_epoch == 0
    assert workspace.block_reason_code is None
    assert workspace.block_violations is None
    assert workspace.pending_operator_hint is None

    workspace.status = WorkspaceStatus.blocked.value
    workspace.block_reason_code = "QUALITY_GATE_POLICY_CHANGED"
    workspace.block_type = "protected_quality_gate"
    workspace.block_violations = [
        {"path": "pyproject.toml", "section": "tool.coverage", "line": 12, "reason": "weakened"}
    ]
    workspace.block_resume_phase = "validating"
    workspace.block_epoch = 1
    workspace.blocked_at = datetime.now(UTC)
    workspace.pending_operator_hint = {"status": "pending", "directive": "revert"}
    await session.flush()
    session.expunge_all()

    reloaded = await session.get(Workspace, workspace.id)
    assert reloaded is not None
    assert reloaded.status == WorkspaceStatus.blocked.value
    assert reloaded.block_reason_code == "QUALITY_GATE_POLICY_CHANGED"
    assert reloaded.block_violations[0]["path"] == "pyproject.toml"
    assert reloaded.block_epoch == 1
    assert reloaded.pending_operator_hint == {"status": "pending", "directive": "revert"}


@pytest.mark.unit
async def test_operator_grant_audit_record_persists_and_cascades(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    grant = OperatorGrantAuditRecord(
        id="grant_1",
        workspace_id=workspace.id,
        operator="alice@example.com",
        reason="approved benign dependency bump",
        normalized_path="pyproject.toml",
        block_epoch=1,
        approve_policy_downgrade=False,
    )
    session.add(grant)
    await session.flush()
    session.expunge_all()

    reloaded = await session.get(OperatorGrantAuditRecord, "grant_1")
    assert reloaded is not None
    assert reloaded.operator == "alice@example.com"
    assert reloaded.block_epoch == 1
    assert reloaded.approve_policy_downgrade is False
    # Unconsumed + unrevoked grant is active for its epoch.
    assert reloaded.is_active_for_epoch is True

    reloaded.consumed_at = datetime.now(UTC)
    await session.flush()
    assert reloaded.is_active_for_epoch is False

    # Cascade delete-orphan: removing the workspace removes its grants.
    ws = await session.get(Workspace, workspace.id)
    assert ws is not None
    await session.delete(ws)
    await session.flush()
    remaining = (
        await session.execute(
            select(OperatorGrantAuditRecord).where(OperatorGrantAuditRecord.id == "grant_1")
        )
    ).scalar_one_or_none()
    assert remaining is None
