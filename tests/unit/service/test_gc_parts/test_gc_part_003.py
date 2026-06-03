from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import awf.service.gc as gc
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.gc import (
    WorkspaceGCComposeTeardownResult,
    WorkspaceGCWorktreeRemoveResult,
    run_terminal_workspace_gc,
    run_workspace_filesystem_gc,
)

"""Terminal workspace filesystem GC tests."""


@pytest.fixture(autouse=True)
def _mock_default_worktree_remover():
    with patch(
        "awf.service.gc._default_worktree_remover",
        new=AsyncMock(
            return_value=WorkspaceGCWorktreeRemoveResult(
                status="succeeded",
                reason_code="WORKTREE_REMOVE_SUCCEEDED",
            )
        ),
    ):
        yield


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


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.mark.unit
async def test_single_workspace_gc_preserves_logs_and_artifacts_after_retention(
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
    compose = work_dir / "compose" / workspace_id
    auth = work_dir / "auth" / workspace_id
    log_file = work_dir / "logs" / workspace_id / "agent.log"
    artifact_file = work_dir / "artifacts" / workspace_id / "summary.json"
    _write(worktree / "repo.txt", "repo")
    _write(compose / "compose.yml", "compose")
    _write(auth / "codex" / "auth.json", "auth")
    _write(log_file, "durable log")
    _write(artifact_file, '{"status": "kept"}')

    result = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=workspace_id,
        execute=True,
        min_age_hours=24,
        now=now,
    )

    assert result.status == "succeeded"
    assert set(result.deleted_paths) == {worktree, compose, auth}
    assert not worktree.exists()
    assert not compose.exists()
    assert not auth.exists()
    assert log_file.exists()
    assert artifact_file.exists()


@pytest.mark.unit
async def test_execute_gc_deletes_workspace_paths_in_threads(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="a" * 40,
    )
    _write(work_dir / "git" / "worktrees" / workspace_id / "repo.txt", "repo")
    _write(work_dir / "compose" / workspace_id / "compose.yml", "compose")
    _write(work_dir / "auth" / workspace_id / "codex" / "auth.json", "auth")
    to_thread_calls: list[str] = []

    async def _record_to_thread(
        func: Callable[..., object],
        /,
        *args: object,
        **kwargs: object,
    ) -> object:
        to_thread_calls.append(func.__name__)
        return func(*args, **kwargs)

    monkeypatch.setattr(gc.asyncio, "to_thread", _record_to_thread)

    result = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
    )

    assert result.status == "succeeded"
    assert [outcome.kind for outcome in result.path_outcomes] == [
        "worktree",
        "compose",
        "auth",
    ]
    assert to_thread_calls == [
        "_classify_workspace_for_gc",
        "_delete_gc_path_outcome",
        "_delete_gc_path_outcome",
        "_delete_gc_path_outcome",
    ]


@pytest.mark.unit
async def test_cleanup_is_idempotent_after_partial_compose_failure(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    compose_slug = "stored"
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        compose_file_path=str(work_dir / "compose" / compose_slug / "compose.yml"),
        pr=True,
        pr_merge_sha="a" * 40,
    )
    compose = work_dir / "compose" / compose_slug
    worktree = work_dir / "git" / "worktrees" / workspace_id
    auth = work_dir / "auth" / workspace_id
    _write(worktree / "repo.txt", "repo")
    _write(compose / "compose.yml", "compose")
    _write(auth / "codex" / "auth.json", "auth")
    calls = 0

    async def _compose_teardown(_candidate: object) -> WorkspaceGCComposeTeardownResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return WorkspaceGCComposeTeardownResult(
                status="failed",
                reason_code="DOCKER_COMPOSE_DOWN_FAILED",
                error="network still in use",
            )
        return WorkspaceGCComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
        )

    first = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
        compose_teardown=_compose_teardown,
    )
    first_payload = first.to_dict()

    assert first.status == "partial"
    assert first.reason_code == "CLEANUP_EXECUTION_PARTIAL"
    assert first_payload["candidates"][0]["compose_teardown"]["reason_code"] == (
        "DOCKER_COMPOSE_DOWN_FAILED"
    )
    assert {data["status"] for data in first_payload["candidates"][0]["paths"].values()} == {
        "skipped"
    }
    assert {data["reason_code"] for data in first_payload["candidates"][0]["paths"].values()} == {
        "DOCKER_COMPOSE_DOWN_FAILED"
    }
    assert worktree.exists()
    assert compose.exists()
    assert auth.exists()

    second = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
        compose_teardown=_compose_teardown,
    )

    assert second.status == "succeeded"
    assert set(second.deleted_paths) == {worktree, compose, auth}
    assert not worktree.exists()
    assert not compose.exists()
    assert not auth.exists()

    third = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
        compose_teardown=_compose_teardown,
    )
    third_payload = third.to_dict()

    assert third.status == "succeeded"
    assert third.deleted_paths == []
    assert third.delete_errors == []
    assert {data["status"] for data in third_payload["candidates"][0]["paths"].values()} == {
        "already_removed"
    }
    assert third.path_outcomes[0].to_dict()["reason_code"] == "PATH_ALREADY_REMOVED"
