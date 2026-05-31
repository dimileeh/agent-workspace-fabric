"""Regression tests for persisted operator hint monitor state."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime import operator_hints
from awf.runtime.operator_hints import OPERATOR_HINT_STATE_KEY, persist_operator_hint
from awf.runtime.pr_monitor import OperatorHint
from awf.runtime.pr_monitor_runner import helpers as runner_helpers
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_persist_state_preserves_concurrent_operator_hint_and_freeze(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    head_sha = "f" * 40
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=42,
        head_sha=head_sha,
    )
    initial_done_key = runner_helpers._initial_review_grace_done_key(42)
    initial_started_key = runner_helpers._initial_review_grace_started_key(42)
    settle_done_key = runner_helpers._non_check_reviewer_settle_done_key(
        pr_number=42,
        head_sha=head_sha,
    )
    settle_started_key = runner_helpers._non_check_reviewer_settle_started_key(
        pr_number=42,
        head_sha=head_sha,
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_last_commit_sha = head_sha
        workspace.monitor_threads_addressed = {
            initial_done_key: "elapsed",
            settle_done_key: "elapsed",
            "review-thread": "fix_committed",
        }
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
    assert stale_state.pending_operator_hint is None
    assert stale_state.threads_addressed_ids[initial_done_key] == "elapsed"
    assert stale_state.threads_addressed_ids[settle_done_key] == "elapsed"

    hint = OperatorHint(
        reason="do not merge until this operator warning is handled",
        operation_id="op_concurrent_hint",
        requested_at="2026-05-30T23:40:00+00:00",
    )
    freeze_now = datetime(2026, 5, 30, 23, 40, tzinfo=UTC)
    freeze_started_value = runner_helpers._initial_review_grace_wall_started_value_from_datetime(
        freeze_now
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        monitor_state = persist_operator_hint(dict(workspace.monitor_threads_addressed), hint)
        operator_hints.arm_operator_hint_freeze(
            monitor_state,
            pr_number=42,
            head_sha=head_sha,
            now=freeze_now,
        )
        workspace.monitor_threads_addressed = monitor_state
        await session.commit()

    stale_state.mark_addressed("second-thread", "fix_committed")
    await runner._persist_state(workspace_id, stale_state)

    async with factory() as session:
        persisted = await WorkspaceRepository(session).get(workspace_id)

    assert persisted is not None
    monitor_state = dict(persisted.monitor_threads_addressed)
    persisted_hint = json.loads(monitor_state[OPERATOR_HINT_STATE_KEY])
    assert persisted_hint == {
        "operation_id": "op_concurrent_hint",
        "reason": "do not merge until this operator warning is handled",
        "reason_code": "OPERATOR_REMONITOR",
        "requested_at": "2026-05-30T23:40:00+00:00",
        "status": "pending",
    }
    assert monitor_state[initial_started_key] == freeze_started_value
    assert monitor_state[settle_started_key] == freeze_started_value
    assert initial_done_key not in monitor_state
    assert settle_done_key not in monitor_state
    assert monitor_state["review-thread"] == "fix_committed"
    assert monitor_state["second-thread"] == "fix_committed"
