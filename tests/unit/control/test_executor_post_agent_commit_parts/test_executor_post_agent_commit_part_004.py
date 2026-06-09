"""Post-agent plan-only gate adoption of the shared #427 helper (#431).

These tests pin the behavior of the post-agent / running-phase plan-only gate
after it was rewritten to route through
``WorkspaceExecutor._committed_and_staged_output_is_plan_only`` (introduced by
#427) instead of inlining the "staged plan-only AND committed ``base..HEAD``
plan-only" computation. The rewrite must stay behavior-preserving:

  1. A genuine plan-only post-agent output (staged artifacts are only internal
     plan files AND the net committed output is empty/plan-only) still fails
     ``PLAN_ONLY_OUTPUT`` at ``WorkspaceStatus.running`` — no commit, no push.

  2. Staged plan-only artifacts on top of real committed work in ``base..HEAD``
     do NOT false-fail: the helper is the routed seam (it is awaited), it
     returns ``False`` for real net output, ``_fail_if_plan_only_paths`` is not
     reached, and execution proceeds past the post-agent block.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populate registry
from awf.common.commands import FakeCommandRunner
from awf.control.quality_gates import PLAN_ONLY_OUTPUT_REASON_CODE
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceEventRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_001 import (
    _seed_ready,
)
from tests.unit.control.test_executor_post_agent_commit_parts.test_executor_post_agent_commit_part_001 import (
    _failed_state_event,
    _make_executor,
)


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        session_factory._awf_test_worktrees_root = tmp_path / "work" / "worktrees"  # type: ignore[attr-defined]
        yield session_factory


@pytest.fixture
def fake() -> FakeCommandRunner:
    return FakeCommandRunner()


@pytest.mark.unit
async def test_post_agent_gate_genuine_plan_only_output_fails(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Staged plan-only artifacts with an empty net ``base..HEAD`` diff still
    fail ``PLAN_ONLY_OUTPUT`` at ``running`` — the helper-routed gate is
    behavior-preserving."""
    ws_id = await _seed_ready(factory)
    plan_path = f"docs/awf-plans/{ws_id}.md"
    fake.queue_result(returncode=0, stdout="adapter ok")  # agent
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout=f"{plan_path}\n")  # cached diff: plan-only staged
    fake.queue_result(returncode=0, stdout="")  # committed base..HEAD --name-only: empty net

    executor = _make_executor(fake, factory, tmp_path)
    await executor.execute(ws_id)

    event = await _failed_state_event(factory, ws_id)
    assert event.reason_code == PLAN_ONLY_OUTPUT_REASON_CODE
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "agent_failure"

    # The gate fired before AWF committed anything or pushed.
    assert not any("commit" in call.args for call in fake.calls)
    assert not any("push" in call.args for call in fake.calls)


@pytest.mark.unit
async def test_post_agent_gate_real_committed_output_proceeds_via_helper(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Staged plan-only artifacts on top of real committed output do not
    false-fail: ``_committed_and_staged_output_is_plan_only`` is the routed
    seam (awaited), returns ``False`` for real net output, and
    ``_fail_if_plan_only_paths`` is never reached at the post-agent gate."""
    ws_id = await _seed_ready(factory)
    plan_path = f"docs/awf-plans/{ws_id}.md"

    executor = _make_executor(fake, factory, tmp_path)

    # Routed seam: the helper returns False (real committed output exists).
    helper_spy = AsyncMock(return_value=False)
    monkeypatch.setattr(executor, "_committed_and_staged_output_is_plan_only", helper_spy)
    # Must not be reached when the helper reports real output.
    fail_plan_only_paths = AsyncMock(return_value=True)
    monkeypatch.setattr(executor, "_fail_if_plan_only_paths", fail_plan_only_paths)

    # Stop cleanly right after the post-agent block by refusing the
    # running→validating transition, keeping the test focused on the gate.
    real_transition = executor._transition_if_current

    async def _transition(
        workspace_id: str, *, from_status: Any, to: Any, reason: str, action: str
    ) -> bool:
        if action == "start_validation":
            return False
        return await real_transition(
            workspace_id, from_status=from_status, to=to, reason=reason, action=action
        )

    monkeypatch.setattr(executor, "_transition_if_current", _transition)

    fake.queue_result(returncode=0, stdout="adapter ok")  # agent
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout=f"{plan_path}\n")  # cached diff: plan-only staged
    fake.queue_result(returncode=0)  # git commit
    fake.queue_result(returncode=0, stdout="1\n")  # rev-list --count base..HEAD
    fake.queue_result(returncode=0)  # merge-base --is-ancestor ok

    await executor.execute(ws_id)

    helper_spy.assert_awaited_once()
    fail_plan_only_paths.assert_not_awaited()

    # The gate proceeded: AWF committed the work and never failed plan-only.
    assert any("commit" in call.args for call in fake.calls)
    assert not any("push" in call.args for call in fake.calls)
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        # The validation transition was refused, so the workspace stays running
        # without ever being marked failed by the post-agent plan-only gate.
        assert ws.status == WorkspaceStatus.running.value
        events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type="workspace.state_changed",
        )
    assert not any(event.new_state == "failed" for event in events)
