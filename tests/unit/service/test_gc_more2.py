from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.db.enums import WorkspaceStatus
from awf.db.models import ResourceReservation, Workspace
from awf.db.repositories import (
    ResourceReservationRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.service.gc import (
    WorkspaceGCPreserved,
    WorkspaceGCWorktreeRemoveResult,
    _classify_workspace_for_gc,
    _pr_has_merged,
    plan_terminal_workspace_gc,
    run_terminal_workspace_gc,
    run_workspace_filesystem_gc,
)


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield make_session_factory(engine)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


async def _workspace(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: WorkspaceStatus,
    updated_at: datetime,
    title: str = "gc candidate",
    compose_file_path: str | None = None,
    pr: bool = False,
    pr_merge_sha: str | None = None,
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
        if pr:
            workspace.pr_url = "https://github.com/example/repo/pull/123"
            workspace.pr_number = 123
            workspace.pr_merge_sha = pr_merge_sha
        await session.commit()
        return workspace.id


def test_classify_workspace_failed_has_work_but_expired_no_default_policy():
    ws = Workspace(
        id="ws_1",
        status=WorkspaceStatus.failed.value,
        updated_at=datetime.now(UTC) - timedelta(hours=25),
        compose_project_name="proj",
    )
    with patch("awf.service.gc._failed_terminal_workspace_has_no_work", return_value=False):
        res = _classify_workspace_for_gc(
            ws,
            work_dir=Path("/tmp"),
            now=datetime.now(UTC),
            cutoff_at=datetime.now(UTC) - timedelta(hours=24),
            default_policy=False,
            cleanup_enabled=True,
        )
        assert isinstance(res, WorkspaceGCPreserved)


def test_pr_has_merged_true_when_merge_sha_set():
    ws = Workspace(
        id="ws_1",
        status=WorkspaceStatus.completed.value,
        pr_merge_sha="a" * 40,
    )
    assert _pr_has_merged(ws) is True


def test_pr_has_merged_false_when_merge_sha_none():
    ws = Workspace(
        id="ws_1",
        status=WorkspaceStatus.completed.value,
        pr_url="http://github.com/pr/1",
        pr_merge_sha=None,
    )
    assert _pr_has_merged(ws) is False


def test_pr_has_merged_false_when_no_pr_at_all():
    ws = Workspace(
        id="ws_1",
        status=WorkspaceStatus.completed.value,
    )
    assert _pr_has_merged(ws) is False


async def test_gc_execute_calls_worktree_remover(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="c" * 40,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    compose = work_dir / "compose" / workspace_id
    auth = work_dir / "auth" / workspace_id
    _write(worktree / "repo.txt", "repo")
    _write(compose / "compose.yml", "compose")
    _write(auth / "codex" / "auth.json", "auth")
    remover_calls: list[str] = []

    async def _worktree_remover(
        candidate: object,
    ) -> WorkspaceGCWorktreeRemoveResult:
        workspace_id_arg = getattr(candidate, "workspace_id", None)
        remover_calls.append(workspace_id_arg)
        return WorkspaceGCWorktreeRemoveResult(
            status="succeeded",
            reason_code="WORKTREE_REMOVE_SUCCEEDED",
        )

    result = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
        worktree_remover=_worktree_remover,
    )

    assert result.status == "succeeded"
    assert remover_calls == [workspace_id]
    assert result.worktree_removes[workspace_id].status == "succeeded"
    assert result.worktree_removes[workspace_id].reason_code == "WORKTREE_REMOVE_SUCCEEDED"


async def test_gc_execute_releases_resource_reservations(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="d" * 40,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    _write(worktree / "repo.txt", "repo")

    async with session_factory() as session:
        repo = ResourceReservationRepository(session)
        await repo.create(
            workspace_id=workspace_id,
            attempt_id="attempt_1",
            node_id="node_1",
            steady_cpu=1.0,
            steady_memory_gb=2.0,
            peak_cpu=2.0,
            peak_memory_gb=4.0,
            disk_mb=1024,
            phase="steady",
            reserved_at=now - timedelta(hours=300),
        )
        await session.commit()

    result = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
    )

    assert result.status == "succeeded"
    assert workspace_id in result.reservation_releases
    assert result.reservation_releases[workspace_id]["released_count"] >= 1

    async with session_factory() as session:
        stmt = select(ResourceReservation).where(
            ResourceReservation.workspace_id == workspace_id
        )
        rows = list((await session.execute(stmt)).scalars())
        assert all(r.released_at is not None for r in rows)


async def test_gc_dry_run_does_not_remove_worktree_or_release_reservations(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="e" * 40,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    _write(worktree / "repo.txt", "repo")
    remover_calls: list[str] = []

    async def _worktree_remover(
        candidate: object,
    ) -> WorkspaceGCWorktreeRemoveResult:
        remover_calls.append("called")
        return WorkspaceGCWorktreeRemoveResult(
            status="succeeded",
            reason_code="WORKTREE_REMOVE_SUCCEEDED",
        )

    async with session_factory() as session:
        repo = ResourceReservationRepository(session)
        await repo.create(
            workspace_id=workspace_id,
            attempt_id="attempt_2",
            node_id="node_1",
            steady_cpu=1.0,
            steady_memory_gb=2.0,
            peak_cpu=2.0,
            peak_memory_gb=4.0,
            disk_mb=None,
            phase="steady",
            reserved_at=now - timedelta(hours=300),
        )
        await session.commit()

    result = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=False,
        now=now,
        worktree_remover=_worktree_remover,
    )

    assert result.dry_run is True
    assert remover_calls == []
    assert result.worktree_removes == {}
    assert result.reservation_releases == {}
    assert worktree.exists()

    async with session_factory() as session:
        stmt = select(ResourceReservation).where(
            ResourceReservation.workspace_id == workspace_id,
            ResourceReservation.released_at.is_(None),
        )
        active = list((await session.execute(stmt)).scalars())
        assert len(active) == 1


async def test_gc_partial_worktree_remove_failure_still_deletes_other_paths(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="f" * 40,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    compose = work_dir / "compose" / workspace_id
    auth = work_dir / "auth" / workspace_id
    _write(worktree / "repo.txt", "repo")
    _write(compose / "compose.yml", "compose")
    _write(auth / "codex" / "auth.json", "auth")

    async def _failing_worktree_remover(
        candidate: object,
    ) -> WorkspaceGCWorktreeRemoveResult:
        return WorkspaceGCWorktreeRemoveResult(
            status="failed",
            reason_code="GIT_WORKTREE_REMOVE_FAILED",
            error="mirror not accessible",
        )

    result = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
        worktree_remover=_failing_worktree_remover,
    )

    assert result.status == "partial"
    assert result.worktree_removes[workspace_id].status == "failed"
    assert not compose.exists()
    assert not auth.exists()
    assert workspace_id in result.reservation_releases


async def test_gc_reservation_release_failure_does_not_block_other_cleanup(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="g" * 40,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    compose = work_dir / "compose" / workspace_id
    auth = work_dir / "auth" / workspace_id
    _write(worktree / "repo.txt", "repo")
    _write(compose / "compose.yml", "compose")
    _write(auth / "codex" / "auth.json", "auth")

    async def _failing_release(self: object, workspace_id: str, **kwargs: object) -> list[ResourceReservation]:
        raise RuntimeError("db connection lost")

    with patch.object(ResourceReservationRepository, "release_active_for_workspace", _failing_release):
        result = await run_terminal_workspace_gc(
            session_factory,
            work_dir=work_dir,
            min_age_hours=24,
            execute=True,
            now=now,
        )

    assert result.status == "partial"
    assert not worktree.exists()
    assert not compose.exists()
    assert not auth.exists()
    assert workspace_id in result.reservation_releases
    assert result.reservation_releases[workspace_id].get("error") is not None


async def test_gc_idempotent_after_partial_failure(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="h" * 40,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    compose = work_dir / "compose" / workspace_id
    auth = work_dir / "auth" / workspace_id
    _write(worktree / "repo.txt", "repo")
    _write(compose / "compose.yml", "compose")
    _write(auth / "codex" / "auth.json", "auth")

    call_count = 0

    async def _partial_worktree_remover(
        candidate: object,
    ) -> WorkspaceGCWorktreeRemoveResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return WorkspaceGCWorktreeRemoveResult(
                status="failed",
                reason_code="GIT_WORKTREE_REMOVE_FAILED",
                error="lock contention",
            )
        return WorkspaceGCWorktreeRemoveResult(
            status="succeeded",
            reason_code="WORKTREE_REMOVE_SUCCEEDED",
        )

    async with session_factory() as session:
        repo = ResourceReservationRepository(session)
        await repo.create(
            workspace_id=workspace_id,
            attempt_id="attempt_idempotent",
            node_id="node_1",
            steady_cpu=1.0,
            steady_memory_gb=2.0,
            peak_cpu=2.0,
            peak_memory_gb=4.0,
            disk_mb=None,
            phase="steady",
            reserved_at=now - timedelta(hours=300),
        )
        await session.commit()

    first = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
        worktree_remover=_partial_worktree_remover,
    )

    assert first.status == "partial"
    assert first.worktree_removes[workspace_id].status == "failed"

    second = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
        worktree_remover=_partial_worktree_remover,
    )

    assert second.status == "succeeded"
    assert second.worktree_removes[workspace_id].status == "succeeded"
    assert second.reservation_releases[workspace_id]["released_count"] == 0


async def test_gc_result_dict_includes_worktree_removes_and_reservation_releases(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="i" * 40,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    _write(worktree / "repo.txt", "repo")

    async def _worktree_remover(
        candidate: object,
    ) -> WorkspaceGCWorktreeRemoveResult:
        return WorkspaceGCWorktreeRemoveResult(
            status="succeeded",
            reason_code="WORKTREE_REMOVE_SUCCEEDED",
        )

    result = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
        worktree_remover=_worktree_remover,
    )

    payload = result.to_dict()
    assert "worktree_removes" in payload
    wr = payload["worktree_removes"][workspace_id]
    assert wr["status"] == "succeeded"
    assert wr["reason_code"] == "WORKTREE_REMOVE_SUCCEEDED"

    assert "reservation_releases" in payload
    assert isinstance(payload["reservation_releases"], dict)


async def test_plan_completed_pr_not_merged_in_preserved_examples(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha=None,
    )

    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=tmp_path / "service",
        min_age_hours=24,
        now=now,
    )

    assert plan.candidates == []
    preserved_ids = [p.workspace_id for p in plan.preserved]
    assert workspace_id in preserved_ids
    preserved = next(p for p in plan.preserved if p.workspace_id == workspace_id)
    assert preserved.reason_code == "COMPLETED_PR_NOT_MERGED"
    payload = plan.to_dict()
    preserved_payloads = [p for p in payload["preserved"] if p["workspace_id"] == workspace_id]
    assert preserved_payloads[0]["reason_code"] == "COMPLETED_PR_NOT_MERGED"


async def test_workspace_gc_plan_requires_pr_merge_property(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=tmp_path / "service",
        min_age_hours=24,
        now=datetime(2026, 4, 26, 12, tzinfo=UTC),
    )

    assert plan.requires_pr_merge is True
    payload = plan.to_dict()
    assert payload["policy"]["requires_pr_merge"] is True


async def test_single_workspace_gc_calls_worktree_remover(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="j" * 40,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    _write(worktree / "repo.txt", "repo")
    remover_calls: list[str] = []

    async def _worktree_remover(
        candidate: object,
    ) -> WorkspaceGCWorktreeRemoveResult:
        workspace_id_arg = getattr(candidate, "workspace_id", None)
        remover_calls.append(workspace_id_arg)
        return WorkspaceGCWorktreeRemoveResult(
            status="succeeded",
            reason_code="WORKTREE_REMOVE_SUCCEEDED",
        )

    result = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=workspace_id,
        execute=True,
        min_age_hours=24,
        now=now,
        worktree_remover=_worktree_remover,
    )

    assert result.status == "succeeded"
    assert remover_calls == [workspace_id]
    assert result.worktree_removes[workspace_id].status == "succeeded"
