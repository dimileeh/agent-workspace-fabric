"""Reactivation invalidates the durable filesystem completion marker."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import false
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.control.state_machine import InvalidWorkspaceTransitionError
from awf.db.enums import WorkspaceStatus as S
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory

MARKER = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize("method", ["orm", "postgresql", "atomic"])
@pytest.mark.parametrize(
    ("src", "dst", "clear"),
    [
        (S.failed, S.monitoring_pr, True),
        (S.completed, S.destroying, True),
        (S.failed, S.destroying, True),
        (S.cancelled, S.destroying, True),
        (S.destroying, S.destroyed, False),
        (S.monitoring_pr, S.failed, False),
    ],
)
async def test_transition_marker_semantics(engine: AsyncEngine, method, src, dst, clear):
    factory = make_session_factory(engine)
    async with factory() as session:
        repo = WorkspaceRepository(
            session, dialect_name="sqlite" if method == "atomic" else "postgresql"
        )
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="reactivation",
            task_prompt="task",
            agent="codex",
            test_commands=[],
        )
        ws.status = src.value
        ws.filesystem_reclaimed_at = MARKER
        await session.commit()
        if method == "orm":
            result = await repo.transition(ws, to=dst, reason_code="TEST_REACTIVATE")
        else:
            result = await repo.transition_if_current(
                ws.id, from_status=src, to=dst, reason_code="TEST_REACTIVATE"
            )
        assert result is not None
        assert result.status == dst.value
        assert result.filesystem_reclaimed_at == (None if clear else MARKER)
        await session.commit()
        await session.refresh(ws)
        assert ws.filesystem_reclaimed_at == (None if clear else MARKER)
        assert ws.events[-1].reason_code == "TEST_REACTIVATE"


@pytest.mark.parametrize("method", ["orm", "postgresql", "atomic"])
async def test_rejected_or_unmatched_transition_preserves_marker(engine: AsyncEngine, method):
    async with make_session_factory(engine)() as session:
        repo = WorkspaceRepository(
            session, dialect_name="sqlite" if method == "atomic" else "postgresql"
        )
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="rejected",
            task_prompt="task",
            agent="codex",
            test_commands=[],
        )
        ws.status = S.failed.value
        ws.filesystem_reclaimed_at = MARKER
        await session.commit()
        with pytest.raises(InvalidWorkspaceTransitionError):
            if method == "orm":
                await repo.transition(ws, to=S.running, reason_code="REJECTED")
            else:
                await repo.transition_if_current(
                    ws.id, from_status=S.failed, to=S.running, reason_code="REJECTED"
                )
        if method != "orm":
            assert (
                await repo.transition_if_current(
                    ws.id,
                    from_status=S.failed,
                    to=S.monitoring_pr,
                    reason_code="NO_MATCH",
                    extra_conditions=(false(),),
                )
                is None
            )
        assert ws.status == S.failed.value
        assert ws.filesystem_reclaimed_at == MARKER
