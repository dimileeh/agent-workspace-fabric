"""Executor error paths: missing worktree failures (split from part 001)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populate registry
from awf.common.commands import FakeCommandRunner
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine
from tests.unit.control.executor_paths import _test_worktree_path
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_001 import (
    _make_executor,
    _move_to_operator_control_status,
    _queue_pre_agent_symlink_baseline,
    _RemovingValidation,
    _seed_ready,
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


class TestMissingWorktreeFailure:
    @pytest.mark.unit
    async def test_missing_worktree_before_post_agent_commit_marks_infrastructure_failure(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory, create_worktree=False)
        worktree_path = _test_worktree_path(factory, ws_id)
        _queue_pre_agent_symlink_baseline(fake)
        fake.queue_result(returncode=0, stdout="adapter ok")
        executor = _make_executor(fake, factory, tmp_path)

        await executor.execute(ws_id)

        git_calls = [call.args for call in fake.calls if call.args[:1] == ["git"]]
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None

        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"
        assert "WORKTREE_MISSING" in (ws.failure_message or "")
        assert str(worktree_path) in (ws.failure_message or "")
        assert ws.events[-1].reason_code == "WORKTREE_MISSING"
        assert any(
            event.event_type == "workspace.executor_worktree_missing"
            and event.reason_code == "WORKTREE_MISSING"
            for event in ws.events
        )
        # Pre-agent symlink-form baseline may probe ``ls-files`` before the
        # missing-worktree gate; no other git side effects should run.
        assert len(git_calls) == 1
        assert "ls-files" in git_calls[0]

    @pytest.mark.unit
    async def test_missing_worktree_before_pr_push_marks_infrastructure_failure(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        worktree_path = _test_worktree_path(factory, ws_id)
        validation = _RemovingValidation(worktree_path)
        _queue_pre_agent_symlink_baseline(fake)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/x\n")  # branch drift check
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="a.py\n")  # cached diff
        fake.queue_result(returncode=0)  # commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base
        validation_head = "e" * 40
        fake.queue_result(returncode=0, stdout=f"{validation_head}\n")  # validation HEAD
        fake.queue_result(returncode=0, stdout="")  # pre-validation status
        executor = _make_executor(fake, factory, tmp_path, validation=validation)

        await executor.execute(ws_id)

        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None

        assert validation.calls == [("setup", "pre_agent"), ("post_agent", "validate")]
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"
        assert "WORKTREE_MISSING" in (ws.failure_message or "")
        assert str(worktree_path) in (ws.failure_message or "")
        assert ws.events[-1].reason_code == "WORKTREE_MISSING"
        assert not any("push" in call.args for call in fake.calls)
        assert not any(call.args[:3] == ["gh", "pr", "create"] for call in fake.calls)

    @pytest.mark.unit
    @pytest.mark.parametrize("final_status", [WorkspaceStatus.cancelled, WorkspaceStatus.destroyed])
    async def test_cancelled_or_destroyed_status_wins_over_missing_worktree(
        self,
        final_status: WorkspaceStatus,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory, create_worktree=False)
        _queue_pre_agent_symlink_baseline(fake)
        fake.queue_result(returncode=0, stdout="adapter ok")
        executor = _make_executor(fake, factory, tmp_path)
        original_recheck_status = executor._recheck_status

        async def _recheck_then_operator_status(
            workspace_id: str,
            *,
            expected: WorkspaceStatus,
            action: str,
            reason_code: str = "EXECUTOR_STALE_STATUS",
        ) -> bool:
            result = await original_recheck_status(
                workspace_id,
                expected=expected,
                action=action,
                reason_code=reason_code,
            )
            if result and action == "post_agent_commit":
                await _move_to_operator_control_status(factory, workspace_id, final_status)
            return result

        executor._recheck_status = _recheck_then_operator_status  # type: ignore[method-assign]

        with structlog.testing.capture_logs() as captured:
            await executor.execute(ws_id)

        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None

        assert ws.status == final_status.value
        assert ws.failure_reason is None
        assert any(
            event.get("event") == "executor.skip_stale_status"
            and event.get("action") == "post_agent_commit"
            for event in captured
        )
        assert not any(event.get("event") == "executor.worktree_missing" for event in captured)
        assert not any(
            event.event_type == "workspace.state_changed"
            and event.reason_code == "WORKTREE_MISSING"
            for event in ws.events
        )
