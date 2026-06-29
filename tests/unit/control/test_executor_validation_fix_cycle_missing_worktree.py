"""Tail tests for executor validation fix-cycle missing-worktree cases."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from tests.unit.control.test_executor_validation_fix_cycle import (
    _make_executor,
    _queue_initial_pass,
    _RemoveWorktreeAfterSecondAdapterRun,
    _RemoveWorktreeOnCall,
    _seed_ready_workspace,
    _test_worktree_path,
)

pytest_plugins = ("tests.unit.control.test_executor_validation_fix_cycle",)


class TestFixCycleMissingWorktree:
    """Validate missing-worktree behavior during validation retries."""

    @pytest.mark.unit
    async def test_missing_worktree_before_fix_agent_stops_without_fix_attempt(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """A disappearing worktree must stop before attempting a fix pass."""
        ws_id = await _seed_ready_workspace(factory)
        worktree_path = _test_worktree_path(factory, ws_id)
        fake = _RemoveWorktreeOnCall(
            worktree_path,
            predicate=lambda args, result: (
                bool(args) and args[-1].endswith("pytest -q") and result.returncode != 0
            ),
        )
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=5)

        _queue_initial_pass(fake)
        fake.queue_result(returncode=1, stderr="pytest: 1 failed")

        await executor.execute(ws_id)

        adapter_calls = [c for c in fake.calls if "exec" in c.args and "codex" in c.args]
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None

        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"
        assert "WORKTREE_MISSING" in (ws.failure_message or "")
        assert "validation_fix_agent_run" in (ws.failure_message or "")
        assert len(adapter_calls) == 1

    @pytest.mark.unit
    async def test_missing_worktree_during_fix_pass_stops_without_repeated_attempts(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Missing worktrees during fix pass should not trigger another retry."""
        ws_id = await _seed_ready_workspace(factory)
        worktree_path = _test_worktree_path(factory, ws_id)
        fake = _RemoveWorktreeAfterSecondAdapterRun(worktree_path)
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=5)
        async with factory() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO operations (
                        id,
                        workspace_id,
                        type,
                        status,
                        payload,
                        created_at
                    )
                    VALUES (
                        'op_validate_missing_worktree',
                        :workspace_id,
                        'validate',
                        'pending',
                        '{"reason":"manual_validate"}',
                        :created_at
                    )
                    """
                ),
                {"workspace_id": ws_id, "created_at": datetime.now(UTC)},
            )
            await session.commit()

        _queue_initial_pass(fake)
        fake.queue_result(returncode=1, stderr="pytest: 1 failed")  # initial validation fails
        fake.queue_result(returncode=0)  # fix-agent returns, then the runner removes worktree

        await executor.execute(ws_id)

        adapter_calls = [c for c in fake.calls if "exec" in c.args and "codex" in c.args]
        validation_calls = [c for c in fake.calls if c.args and c.args[-1].endswith("pytest -q")]
        git_add_calls = [
            c for c in fake.calls if c.args[:1] == ["git"] and c.args[-2:] == ["add", "-A"]
        ]
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None
            operation = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT status, error_code, error_message, result
                        FROM operations
                        WHERE id = 'op_validate_missing_worktree'
                        """
                        )
                    )
                )
                .mappings()
                .one()
            )
            runs = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT status, reason_code
                        FROM validation_runs
                        WHERE workspace_id = :workspace_id
                        """
                        ),
                        {"workspace_id": ws_id},
                    )
                )
                .mappings()
                .all()
            )

        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"
        assert "WORKTREE_MISSING" in (ws.failure_message or "")
        assert str(worktree_path) in (ws.failure_message or "")
        assert ws.events[-1].reason_code == "WORKTREE_MISSING"
        assert operation["status"] == "failed"
        assert operation["error_code"] == "WORKTREE_MISSING"
        assert "WORKTREE_MISSING" in (operation["error_message"] or "")
        assert "validation_run_id" in operation["result"]
        assert runs == [{"status": "failed", "reason_code": "COMMAND_FAILED"}]
        assert len(adapter_calls) == 2
        assert len(validation_calls) == 1
        assert len(git_add_calls) == 1

    @pytest.mark.unit
    async def test_missing_worktree_after_fix_add_stops_before_diff(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """If worktree disappears after fix add, diff/commit steps must be skipped."""
        ws_id = await _seed_ready_workspace(factory)
        worktree_path = _test_worktree_path(factory, ws_id)
        fake = _RemoveWorktreeOnCall(
            worktree_path,
            predicate=lambda args, _result: args[:1] == ["git"] and args[-2:] == ["add", "-A"],
            occurrence=2,
        )
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=5)

        _queue_initial_pass(fake)
        fake.queue_result(returncode=1, stderr="pytest: 1 failed")
        fake.queue_result(returncode=0)  # fix-agent
        fake.queue_result(returncode=0)  # fix add removes worktree after returning

        await executor.execute(ws_id)

        git_diff_calls = [
            c
            for c in fake.calls
            if c.args[:1] == ["git"] and c.args[-3:] == ["diff", "--cached", "--name-only"]
        ]
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None

        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"
        assert "validation_fix_git_diff" in (ws.failure_message or "")
        assert len(git_diff_calls) == 1

    @pytest.mark.unit
    async def test_missing_worktree_after_fix_diff_stops_before_commit(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """If worktree disappears while computing fix diff, skip commit."""
        ws_id = await _seed_ready_workspace(factory)
        worktree_path = _test_worktree_path(factory, ws_id)
        fake = _RemoveWorktreeOnCall(
            worktree_path,
            predicate=lambda args, _result: (
                args[:1] == ["git"] and args[-3:] == ["diff", "--cached", "--name-only"]
            ),
            occurrence=2,
        )
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=5)

        _queue_initial_pass(fake)
        fake.queue_result(returncode=1, stderr="pytest: 1 failed")
        fake.queue_result(returncode=0)  # fix-agent
        fake.queue_result(returncode=0)  # fix add
        fake.queue_result(returncode=0, stdout="a.py\n")  # fix diff removes worktree

        await executor.execute(ws_id)

        fix_commit_calls = [
            c
            for c in fake.calls
            if c.args[:1] == ["git"]
            and "commit" in c.args
            and any("fix pass" in arg for arg in c.args)
        ]
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None

        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"
        assert "validation_fix_git_commit" in (ws.failure_message or "")
        assert fix_commit_calls == []
