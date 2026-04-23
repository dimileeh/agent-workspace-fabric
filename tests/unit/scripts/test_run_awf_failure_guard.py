"""Tests for ``scripts.run_awf._run_task_with_failure_guard`` and
``_mark_orphan_workspace_failed``.

Scope: the runaway-watcher bug observed in production. When a handler
crashed mid-provisioning (compose-up failure, git error, etc.), the
DB row stayed stuck in ``provisioning`` forever. The downstream
scheduler (``schedule_release_pr._monitor_already_running``) then saw
the stuck row AND thought the monitor was still live — or, worse in
the pre-fix world, the PROCESS-based check saw the crashed process
gone and spawned another on top. Either way, orphans piled up.

The guard wrapper and orphan-marker helper together turn those crashes
into clean terminal ``failed`` transitions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.base import Base
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from scripts.run_awf import (
    TaskConfig,
    _mark_orphan_workspace_failed,
    _run_task_with_failure_guard,
)


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'awf.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


async def _seed_ws(
    factory: async_sessionmaker[AsyncSession],
    *,
    status: WorkspaceStatus,
    repo_url: str = "git@github.com:dimileeh/aira-web.git",
    task_title: str = "release-monitor: dimileeh/aira-web#278",
) -> str:
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url=repo_url,
            branch_base="main",
            task_title=task_title,
            task_prompt="x",
            agent="codex",
            test_commands=[],
            requires_database=False,
        )
        if status != WorkspaceStatus.requested:
            await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="X")
            if status not in (WorkspaceStatus.provisioning,):
                # walk partway if the test asked for something downstream
                for stage in (
                    WorkspaceStatus.ready,
                    WorkspaceStatus.running,
                    WorkspaceStatus.validating,
                    WorkspaceStatus.pushing,
                    WorkspaceStatus.monitoring_pr,
                    WorkspaceStatus.completed,
                    WorkspaceStatus.failed,
                ):
                    await repo.transition(ws, to=stage, reason_code="X")
                    if stage == status:
                        break
        await s.commit()
        return ws.id


class TestMarkOrphanWorkspaceFailed:
    @pytest.mark.unit
    async def test_transitions_latest_non_terminal_row(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ws_id = await _seed_ws(factory, status=WorkspaceStatus.provisioning)

        await _mark_orphan_workspace_failed(
            session_factory=factory,
            repo_url="git@github.com:dimileeh/aira-web.git",
            task_title="release-monitor: dimileeh/aira-web#278",
            message="compose up failed — test message",
        )

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert "compose up failed" in (ws.failure_message or "")

    @pytest.mark.unit
    async def test_no_matching_row_is_noop(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """Guard fires an orphan-mark call even for tasks that never
        reached workspace creation. Must not crash on empty DB."""
        await _mark_orphan_workspace_failed(
            session_factory=factory,
            repo_url="git@github.com:dimileeh/nothing-here.git",
            task_title="ghost",
            message="no such ws",
        )  # just must not raise

    @pytest.mark.unit
    async def test_leaves_terminal_rows_untouched(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Guard must NOT retroactively flip a successful run to
        failed — its only job is to recover stuck non-terminal rows."""
        ws_id = await _seed_ws(factory, status=WorkspaceStatus.completed)

        await _mark_orphan_workspace_failed(
            session_factory=factory,
            repo_url="git@github.com:dimileeh/aira-web.git",
            task_title="release-monitor: dimileeh/aira-web#278",
            message="would-be regression if applied",
        )

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.failure_reason is None


class TestRunTaskWithFailureGuard:
    @pytest.mark.unit
    async def test_successful_handler_returns_result_unchanged(
        self,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The guard must be transparent for the happy path — it wraps
        exception handling, nothing else."""
        expected_result = {"workspace_id": "ws_123", "status": "completed"}

        async def _fake_run_task(cfg, **kwargs):  # type: ignore[no-untyped-def]
            return expected_result

        monkeypatch.setattr("scripts.run_awf._run_task", _fake_run_task)

        cfg = TaskConfig(
            repo_url="git@github.com:x/y.git",
            branch_base="development",
            task_title="test",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        result = await _run_task_with_failure_guard(
            cfg,
            work_dir=Path("/tmp"),
            session_factory=factory,
            auth_mounts=[],
            git_name="x",
            git_email="x@x",
        )
        assert result is expected_result

    @pytest.mark.unit
    async def test_exception_marks_orphan_failed_and_reraises(
        self,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Core contract: any exception from the inner handler →
        orphan row marked failed, exception propagates (so the gather
        in _main still reports the crash to the operator)."""
        ws_id = await _seed_ws(factory, status=WorkspaceStatus.provisioning)

        async def _boom(cfg, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("compose up failed")

        monkeypatch.setattr("scripts.run_awf._run_task", _boom)

        cfg = TaskConfig(
            repo_url="git@github.com:dimileeh/aira-web.git",
            branch_base="main",
            task_title="release-monitor: dimileeh/aira-web#278",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )

        with pytest.raises(RuntimeError, match="compose up failed"):
            await _run_task_with_failure_guard(
                cfg,
                work_dir=Path("/tmp"),
                session_factory=factory,
                auth_mounts=[],
                git_name="x",
                git_email="x@x",
            )

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "compose up failed" in (ws.failure_message or "")
