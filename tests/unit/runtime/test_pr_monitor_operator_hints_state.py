"""Regression tests for operator remonitor hints in the PR monitor runner."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.operator_hints import (
    OPERATOR_HINT_STATE_KEY,
    operator_hint_processed_key,
    persist_operator_hint,
)
from awf.runtime.pr_monitor import (
    CheckState,
    CheckTiming,
    Merge,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    NotifyHuman,
    OperatorHint,
    PRStatus,
    decide,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

REPO_URL = "git@github.com:dimileeh/aira-web.git"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _ready_status(
    *,
    head_sha: str = "abc1234567890def",
    checks: tuple[CheckTiming, ...] = (),
) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        checks=checks,
    )


@pytest.mark.unit
@pytest.mark.parametrize("terminal_status", ["needs_human", "agent_failed"])
async def test_persist_state_preserves_concurrent_terminal_operator_hint_status(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    terminal_status: Literal["needs_human", "agent_failed"],
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    pending_hint = OperatorHint(
        reason="investigate the operator supplied remonitor hint",
        operation_id="op_hint_terminal_elsewhere",
        requested_at="2026-05-31T00:30:00+00:00",
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = persist_operator_hint({}, pending_hint)
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    stale_workspace = await runner._load_workspace(workspace_id)
    stale_state = runner._load_state(stale_workspace)
    assert stale_state.pending_operator_hint == pending_hint

    terminal_reason = "agent already determined this hint requires human attention"
    terminal_hint = OperatorHint(
        reason=pending_hint.reason,
        operation_id=pending_hint.operation_id,
        requested_at=pending_hint.requested_at,
        status=terminal_status,
        status_reason=terminal_reason,
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = persist_operator_hint({}, terminal_hint)
        await session.commit()

    stale_state.mark_addressed("second-thread", "fix_committed")
    await runner._persist_state(workspace_id, stale_state)

    async with factory() as session:
        persisted = await WorkspaceRepository(session).get(workspace_id)

    assert persisted is not None
    monitor_state = dict(persisted.monitor_threads_addressed)
    persisted_hint = json.loads(monitor_state[OPERATOR_HINT_STATE_KEY])
    assert persisted_hint["operation_id"] == "op_hint_terminal_elsewhere"
    assert persisted_hint["status"] == terminal_status
    assert persisted_hint["status_reason"] == terminal_reason
    assert monitor_state["second-thread"] == "fix_committed"


@pytest.mark.unit
@pytest.mark.parametrize("terminal_status", ["needs_human", "agent_failed"])
async def test_refresh_operator_state_imports_concurrent_terminal_same_operation_hint(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    terminal_status: Literal["needs_human", "agent_failed"],
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    pending_hint = OperatorHint(
        reason="investigate the operator supplied remonitor hint",
        operation_id="op_hint_refresh_terminal_elsewhere",
        requested_at="2026-05-31T00:45:00+00:00",
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = persist_operator_hint({}, pending_hint)
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    stale_workspace = await runner._load_workspace(workspace_id)
    stale_state = runner._load_state(stale_workspace)
    assert stale_state.pending_operator_hint == pending_hint
    assert await runner._refresh_operator_state_from_workspace(workspace_id, stale_state) is False
    assert stale_state.pending_operator_hint == pending_hint

    terminal_reason = "another monitor pass could not safely apply the hint"
    terminal_hint = OperatorHint(
        reason=pending_hint.reason,
        operation_id=pending_hint.operation_id,
        requested_at=pending_hint.requested_at,
        status=terminal_status,
        status_reason=terminal_reason,
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = persist_operator_hint({}, terminal_hint)
        await session.commit()

    changed = await runner._refresh_operator_state_from_workspace(workspace_id, stale_state)
    action = decide(_ready_status(), stale_state, MonitorConfig(auto_merge=True))

    assert changed is True
    assert stale_state.pending_operator_hint == terminal_hint
    assert isinstance(action, NotifyHuman)


@pytest.mark.unit
async def test_refresh_operator_state_clears_processed_operator_hint_marker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    hint = OperatorHint(
        reason="operator hint was processed by another monitor pass",
        operation_id="op_hint_refresh_processed_elsewhere",
        requested_at="2026-05-31T01:05:00+00:00",
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = persist_operator_hint({}, hint)
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    stale_workspace = await runner._load_workspace(workspace_id)
    stale_state = runner._load_state(stale_workspace)
    assert stale_state.pending_operator_hint == hint

    processed_key = operator_hint_processed_key("op_hint_refresh_processed_elsewhere")
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = {processed_key: "processed"}
        await session.commit()

    changed = await runner._refresh_operator_state_from_workspace(workspace_id, stale_state)
    action = decide(_ready_status(), stale_state, MonitorConfig(auto_merge=True))

    assert changed is True
    assert stale_state.pending_operator_hint is None
    assert stale_state.threads_addressed_ids[processed_key] == "processed"
    assert isinstance(action, Merge)
