"""ORM tests for block-state columns and operator grant audit records."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import sqlite
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


async def _blocked_workspace(session: AsyncSession, *, block_epoch: int = 1) -> Workspace:
    workspace = await _workspace(session)
    workspace.status = WorkspaceStatus.blocked.value
    workspace.block_epoch = block_epoch
    workspace.blocked_at = datetime.now(UTC)
    await session.flush()
    return workspace


@pytest.mark.unit
async def test_list_resumable_blocked_ids_selects_only_operator_cleared(
    session: AsyncSession,
) -> None:
    repo = WorkspaceRepository(session)

    # Awaiting an operator decision: no directive, no grant → not resumable.
    awaiting = await _blocked_workspace(session)

    # Directive armed (revert/redo) → resumable.
    with_directive = await _blocked_workspace(session)
    with_directive.pending_operator_hint = {"status": "pending", "directive": "revert it"}

    # Hint present but carries no directive and no grant → the resume path would
    # apply neither branch, so it must NOT be selected (else blocked -> running
    # -> blocked spins re-running the same gate every cycle).
    directiveless = await _blocked_workspace(session)
    directiveless.pending_operator_hint = {"status": "pending", "reason": "paused on gate"}

    # Hint with a whitespace-only directive is treated as directive-less by the
    # resume path (``_optional_stripped_string``) → also not resumable.
    blank_directive = await _blocked_workspace(session)
    blank_directive.pending_operator_hint = {
        "status": "pending",
        "reason": "paused on gate",
        "directive": "   ",
    }

    # Hint with a NON-STRING directive (e.g. a JSON boolean): the resume path's
    # ``_optional_stripped_string`` discards any non-string value as absent, so a
    # directive that merely coerces to non-empty text must NOT clear eligibility
    # here — otherwise it reproduces the blocked -> running -> blocked spin loop.
    non_string_directive = await _blocked_workspace(session)
    non_string_directive.pending_operator_hint = {
        "status": "pending",
        "reason": "paused on gate",
        "directive": True,
    }

    # Active grant for the current epoch (approve-and-keep) → resumable.
    with_grant = await _blocked_workspace(session, block_epoch=2)
    session.add(
        OperatorGrantAuditRecord(
            id="grant_active",
            workspace_id=with_grant.id,
            operator="op@example.com",
            reason="approved",
            normalized_path="pyproject.toml",
            block_epoch=2,
        )
    )

    # Grant exists but is stale (consumed) or scoped to a prior epoch → not resumable.
    with_stale_grant = await _blocked_workspace(session, block_epoch=3)
    session.add(
        OperatorGrantAuditRecord(
            id="grant_consumed",
            workspace_id=with_stale_grant.id,
            operator="op@example.com",
            reason="approved",
            normalized_path="pyproject.toml",
            block_epoch=3,
            consumed_at=datetime.now(UTC),
        )
    )
    session.add(
        OperatorGrantAuditRecord(
            id="grant_wrong_epoch",
            workspace_id=with_stale_grant.id,
            operator="op@example.com",
            reason="approved",
            normalized_path="pyproject.toml",
            block_epoch=2,
        )
    )

    # A directive on a non-blocked workspace is ignored (status gate).
    running = await _workspace(session)
    running.pending_operator_hint = {"status": "pending", "directive": "revert it"}

    await session.flush()

    ids = await repo.list_resumable_blocked_ids(limit=10)
    assert set(ids) == {with_directive.id, with_grant.id}
    assert awaiting.id not in ids
    assert directiveless.id not in ids
    assert blank_directive.id not in ids
    assert non_string_directive.id not in ids
    assert with_stale_grant.id not in ids
    assert running.id not in ids

    # exclude_ids drops already-tracked workspaces; limit bounds the batch.
    excluded = await repo.list_resumable_blocked_ids(limit=10, exclude_ids={with_directive.id})
    assert set(excluded) == {with_grant.id}
    assert len(await repo.list_resumable_blocked_ids(limit=1)) == 1
    assert await repo.list_resumable_blocked_ids(limit=0) == []


@pytest.mark.unit
async def test_list_resumable_blocked_ids_sqlite_validates_directive_string_type() -> None:
    """The SQLite eligibility branch must type-check the directive via json_type.

    ``db/session.make_engine`` enforces Postgres, so the SQLite dialect branch is
    exercised by compiling the statement rather than executing it. The directive
    must be gated on ``json_type(...) = 'text'`` so a non-string directive cannot
    clear eligibility, mirroring the resume path's ``_optional_stripped_string``.
    """

    class _EmptyResult:
        def scalars(self) -> _EmptyResult:
            return self

        def all(self) -> list[str]:
            return []

    class _RecordingSession:
        info: dict[str, str] = {}
        bind = None

        def __init__(self) -> None:
            self.executed: list[object] = []

        async def execute(self, statement: object) -> _EmptyResult:
            self.executed.append(statement)
            return _EmptyResult()

    recording_session = _RecordingSession()
    repo = WorkspaceRepository(recording_session, dialect_name="sqlite")  # type: ignore[arg-type]

    result = await repo.list_resumable_blocked_ids(limit=10)

    assert result == []
    assert len(recording_session.executed) == 1
    sql = " ".join(
        str(
            recording_session.executed[0].compile(  # type: ignore[attr-defined]
                dialect=sqlite.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).split()
    )
    assert "json_type(workspaces.pending_operator_hint, '$.directive') = 'text'" in sql
    assert "json_extract(workspaces.pending_operator_hint, '$.directive')" in sql


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
