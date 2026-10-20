"""Operator hint: an untyped error after a successful rollback parks the hint (#1076).

The hint pass runs the same per-item verdict protocol as a review comment. When
the agent invocation raises an untyped error after the agent already
self-committed, the protocol rolls the hint's edits back to its start. That
rollback succeeded, so the hint parks ``needs_human`` with
``AGENT_VERDICT_ITEM_UNEXPECTED_ERROR`` instead of failing the workspace. It is
an ordinary parked hint: the rollback ran, so there is no kept commit and
nothing is pushed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunResult
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    AGENT_VERDICT_ITEM_UNEXPECTED_ERROR,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import seed_monitoring_workspace
from tests.unit.runtime.test_pr_monitor_operator_hints_part_012 import (
    _HINT_FIX_HEAD,
    _START_HEAD,
    _hint,
    _real_protocol_runner,
    _run_cycle,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_hint_unexpected_invocation_error_parks_the_hint(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance 6: S1 on the hint path parks the hint and the workspace lives."""
    workspace_id = await seed_monitoring_workspace(factory)
    head = [_START_HEAD]
    pushes: list[str] = []
    runner = _real_protocol_runner(
        factory,
        tmp_path,
        monkeypatch,
        workspace_id=workspace_id,
        stdout="unused",
        commits=False,
        head=head,
        pushes=pushes,
    )

    async def _agent_commits_then_fails(**_kwargs: object) -> AgentRunResult:
        head[0] = _HINT_FIX_HEAD
        raise RuntimeError("agent transport dropped")

    monkeypatch.setattr(
        runner, "_run_monitor_agent_with_service_recovery", _agent_commits_then_fails
    )
    hint = _hint()
    state = MonitorState(pending_operator_hint=hint)

    result = await _run_cycle(
        runner, workspace_id=workspace_id, hint=hint, state=state, tmp_path=tmp_path
    )

    assert result.failed is False
    assert result.terminal_monitor_failure is False
    # No kept commit: the rollback ran, so the hint's own commit is gone.
    assert pushes == []
    assert head[0] == _START_HEAD
    assert runner._deps.runner.reset_targets == [_START_HEAD]  # type: ignore[attr-defined]
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"
    status_reason = state.pending_operator_hint.status_reason or ""
    assert status_reason.startswith(
        f"{AGENT_VERDICT_ITEM_UNEXPECTED_ERROR}: RuntimeError at agent_invocation "
    )
    assert "agent transport dropped" in status_reason
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    assert ws.status == WorkspaceStatus.monitoring_pr.value
