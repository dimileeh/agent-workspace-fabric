"""Operator hint: a failed item-start snapshot parks the hint without running it (#1081).

The hint pass runs the same per-item verdict protocol as a review comment. When
the item-start Git config snapshot fails, AWF cannot safely undo an agent run,
so the hint is not attempted: it parks ``needs_human`` with
``AGENT_VERDICT_ITEM_START_GIT_CONFIG_SNAPSHOT_FAILED`` instead of failing the
workspace. Nothing ran, so there is no kept commit and nothing is pushed.
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
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    AGENT_VERDICT_ITEM_START_GIT_CONFIG_SNAPSHOT_FAILED,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import seed_monitoring_workspace
from tests.unit.runtime._verdict_trailing_output_fixtures import F1
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
async def test_hint_with_a_failed_item_start_snapshot_parks_without_running(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 3: the hint parks unattempted, nothing is pushed, the workspace lives."""
    workspace_id = await seed_monitoring_workspace(factory)
    head = [_START_HEAD]
    pushes: list[str] = []
    runner = _real_protocol_runner(
        factory,
        tmp_path,
        monkeypatch,
        workspace_id=workspace_id,
        stdout=F1,
        commits=True,
        head=head,
        pushes=pushes,
    )
    agent_calls: list[dict[str, object]] = []

    async def _agent(**kwargs: object) -> AgentRunResult:
        # Had the hint run, it would have committed and been pushed.
        agent_calls.append(kwargs)
        head[0] = _HINT_FIX_HEAD
        return AgentRunResult(returncode=0, stdout=F1, stderr="")

    def _snapshot_fails(_worktree_path: Path) -> bool:
        return False

    monkeypatch.setattr(runner, "_run_monitor_agent_with_service_recovery", _agent)
    monkeypatch.setattr(comment_verdict, "remember_item_start_local_git_configs", _snapshot_fails)
    hint = _hint()
    state = MonitorState(pending_operator_hint=hint)

    result = await _run_cycle(
        runner, workspace_id=workspace_id, hint=hint, state=state, tmp_path=tmp_path
    )

    assert agent_calls == []
    assert result.failed is False
    assert result.terminal_monitor_failure is False
    # No kept commit: nothing ran, so there is nothing to push.
    assert pushes == []
    assert head[0] == _START_HEAD
    assert runner._deps.runner.reset_targets == []  # type: ignore[attr-defined]
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"
    status_reason = state.pending_operator_hint.status_reason or ""
    assert status_reason.startswith(f"{AGENT_VERDICT_ITEM_START_GIT_CONFIG_SNAPSHOT_FAILED}: ")
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    assert ws.status == WorkspaceStatus.monitoring_pr.value
