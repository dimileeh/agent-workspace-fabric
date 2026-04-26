"""Terminal workspace filesystem GC tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    WorkspaceLogStreamRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.service.gc import plan_terminal_workspace_gc, run_terminal_workspace_gc


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield make_session_factory(engine)


async def _workspace(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: WorkspaceStatus,
    updated_at: datetime,
    title: str = "gc candidate",
    compose_file_path: str | None = None,
) -> str:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/repo.git",
            branch_base="development",
            task_title=title,
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        workspace.status = status.value
        workspace.updated_at = updated_at
        workspace.compose_file_path = compose_file_path
        await session.commit()
        return workspace.id


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.mark.unit
async def test_plan_reports_terminal_candidates_paths_and_estimated_bytes(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=200),
        compose_file_path=str(work_dir / "compose" / "stored-compose-id" / "compose.yml"),
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    compose = work_dir / "compose" / "stored-compose-id"
    auth = work_dir / "auth" / workspace_id
    _write(worktree / "repo.txt", "1234567")
    _write(compose / "compose.yml", "12345")
    _write(auth / "codex" / "auth.json", "123456789")

    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        now=now,
    )

    assert plan.total_estimated_bytes == 21
    assert [candidate.workspace_id for candidate in plan.candidates] == [workspace_id]
    candidate = plan.candidates[0]
    assert candidate.status == WorkspaceStatus.failed.value
    assert candidate.age_hours == 200
    assert candidate.worktree.path == worktree
    assert candidate.worktree.exists is True
    assert candidate.worktree.estimated_bytes == 7
    assert candidate.compose.path == compose
    assert candidate.compose.exists is True
    assert candidate.compose.estimated_bytes == 5
    assert candidate.auth.path == auth
    assert candidate.auth.exists is True
    assert candidate.auth.estimated_bytes == 9
    assert candidate.total_estimated_bytes == 21


@pytest.mark.unit
async def test_plan_excludes_active_and_destroying_workspaces(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    ineligible_statuses = [
        WorkspaceStatus.requested,
        WorkspaceStatus.provisioning,
        WorkspaceStatus.ready,
        WorkspaceStatus.running,
        WorkspaceStatus.validating,
        WorkspaceStatus.pushing,
        WorkspaceStatus.monitoring_pr,
        WorkspaceStatus.destroying,
    ]
    for status in ineligible_statuses:
        await _workspace(
            session_factory,
            status=status,
            updated_at=now - timedelta(days=30),
            title=f"{status.value} workspace",
        )

    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=tmp_path / "service",
        min_age_hours=1,
        now=now,
    )

    assert plan.candidates == []


@pytest.mark.unit
async def test_plan_reports_requested_statuses_when_no_statuses_are_eligible(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)

    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=tmp_path / "service",
        include_statuses=[WorkspaceStatus.running],
        exclude_statuses=[WorkspaceStatus.completed],
        now=now,
    )

    assert plan.candidates == []
    assert plan.to_dict()["include_statuses"] == [WorkspaceStatus.running.value]
    assert plan.to_dict()["exclude_statuses"] == [WorkspaceStatus.completed.value]


@pytest.mark.unit
async def test_plan_applies_min_age_filter_and_limit_oldest_first(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    oldest = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=300),
        title="oldest",
    )
    await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=100),
        title="middle",
    )
    await _workspace(
        session_factory,
        status=WorkspaceStatus.cancelled,
        updated_at=now - timedelta(hours=2),
        title="fresh",
    )

    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=tmp_path / "service",
        min_age_hours=24,
        limit=1,
        now=now,
    )

    assert [candidate.workspace_id for candidate in plan.candidates] == [oldest]

    older_than_120h = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=tmp_path / "service",
        min_age_hours=120,
        now=now,
    )
    assert [candidate.workspace_id for candidate in older_than_120h.candidates] == [oldest]


@pytest.mark.unit
async def test_plan_tolerates_missing_workspace_paths(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.destroyed,
        updated_at=now - timedelta(hours=48),
    )

    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        now=now,
    )

    candidate = plan.candidates[0]
    assert candidate.workspace_id == workspace_id
    assert candidate.total_estimated_bytes == 0
    assert candidate.worktree.path == work_dir / "git" / "worktrees" / workspace_id
    assert candidate.compose.path == work_dir / "compose" / workspace_id
    assert candidate.auth.path == work_dir / "auth" / workspace_id
    assert candidate.worktree.exists is False
    assert candidate.compose.exists is False
    assert candidate.auth.exists is False


@pytest.mark.unit
async def test_run_defaults_to_dry_run_and_keeps_candidate_directories(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=48),
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    compose = work_dir / "compose" / workspace_id
    auth = work_dir / "auth" / workspace_id
    _write(worktree / "repo.txt", "repo")
    _write(compose / "compose.yml", "compose")
    _write(auth / "codex" / "auth.json", "auth")

    result = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        now=now,
    )

    assert result.dry_run is True
    assert result.deleted_paths == []
    assert worktree.exists()
    assert compose.exists()
    assert auth.exists()


@pytest.mark.unit
async def test_execute_deletes_only_workspace_pressure_dirs_and_preserves_db_and_logs(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.cancelled,
        updated_at=now - timedelta(hours=48),
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    compose = work_dir / "compose" / workspace_id
    auth = work_dir / "auth" / workspace_id
    log_file = work_dir / "logs" / workspace_id / "agent.log"
    _write(worktree / "repo.txt", "repo")
    _write(compose / "compose.yml", "compose")
    _write(auth / "codex" / "auth.json", "auth")
    _write(log_file, "agent log")
    async with session_factory() as session:
        await WorkspaceLogStreamRepository(session).create_or_get(
            workspace_id=workspace_id,
            stream_id="agent/stdout",
            source="agent",
            name="stdout",
            kind="stdout",
            path=str(log_file),
        )
        await session.commit()

    result = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
    )

    assert result.dry_run is False
    assert set(result.deleted_paths) == {worktree, compose, auth}
    assert not worktree.exists()
    assert not compose.exists()
    assert not auth.exists()
    assert log_file.exists()

    async with session_factory() as session:
        workspace = await session.get(Workspace, workspace_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.cancelled.value
        assert len(workspace.events) == 1
        streams = await WorkspaceLogStreamRepository(session).list_for_workspace(workspace_id)
        assert [stream.path for stream in streams] == [str(log_file)]
        assert (await session.execute(select(Workspace.id))).scalars().all() == [workspace_id]
