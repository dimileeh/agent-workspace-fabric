from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.gc import (
    WorkspaceGCCandidate,
    WorkspaceGCPath,
    _default_worktree_remover,
    plan_terminal_workspace_gc,
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
    pr: bool = False,
    pr_merge_sha: str | None = None,
    task_policy: dict[str, object] | None = None,
) -> str:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/repo.git",
            branch_base="development",
            task_title=title,
            task_prompt="p",
            agent="codex",
            test_commands=[],
            task_policy=task_policy,
        )
        workspace.status = status.value
        workspace.updated_at = updated_at
        if pr:
            workspace.pr_url = "https://github.com/example/repo/pull/123"
            workspace.pr_number = 123
            workspace.pr_merge_sha = pr_merge_sha
        await session.commit()
        return workspace.id


@pytest.mark.unit
async def test_default_worktree_remover_succeeds(
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
        pr_merge_sha="p" * 40,
    )
    candidate = WorkspaceGCCandidate(
        workspace_id=workspace_id,
        status=WorkspaceStatus.completed.value,
        updated_at=now,
        age_hours=200,
        reason_code="COMPLETED_PR_RETENTION_EXPIRED",
        worktree=WorkspaceGCPath(
            kind="worktree",
            path=work_dir / "git" / "worktrees" / workspace_id,
            exists=True,
            estimated_bytes=0,
        ),
        compose=WorkspaceGCPath(
            kind="compose",
            path=work_dir / "compose" / workspace_id,
            exists=False,
            estimated_bytes=0,
        ),
        auth=WorkspaceGCPath(
            kind="auth", path=work_dir / "auth" / workspace_id, exists=False, estimated_bytes=0
        ),
    )

    with patch("awf.node.git_manager.GitManager") as mock_gm_cls:
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree = AsyncMock()
        result = await _default_worktree_remover(
            candidate,
            session_factory=session_factory,
            work_dir=work_dir,
        )
        assert result.status == "succeeded"
        assert result.reason_code == "WORKTREE_REMOVE_SUCCEEDED"
        mock_gm.remove_worktree.assert_awaited_once_with(
            workspace_id=workspace_id,
            repo_url="git@github.com:example/repo.git",
        )


@pytest.mark.unit
async def test_gc_candidate_and_default_remover_include_companion_worktrees(
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
        pr_merge_sha="q" * 40,
        task_policy={
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@github.com:example/backend.git",
                    "base_branch": "development",
                }
            ]
        },
    )
    companion_path = work_dir / "git" / "worktrees" / f"{workspace_id}__companion__backend"
    _write(companion_path / ".git", "gitdir")

    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        now=now,
    )

    candidate = next(item for item in plan.candidates if item.workspace_id == workspace_id)
    assert [path.kind for path in candidate.companion_worktrees] == [
        f"companion_worktree:{workspace_id}__companion__backend"
    ]
    assert candidate.to_dict()["estimated_bytes"]["companion_worktrees"] > 0

    with patch("awf.node.git_manager.GitManager") as mock_gm_cls:
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree = AsyncMock()
        result = await _default_worktree_remover(
            candidate,
            session_factory=session_factory,
            work_dir=work_dir,
        )

    assert result.status == "succeeded"
    assert mock_gm.remove_worktree.await_args_list[1].kwargs == {
        "workspace_id": f"{workspace_id}__companion__backend",
        "repo_url": "git@github.com:example/backend.git",
    }


@pytest.mark.unit
async def test_gc_companion_worktree_paths_ignore_name_only_policy_entries(
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
        pr_merge_sha="q" * 40,
        task_policy={"companions": [{"name": "backend"}]},
    )
    companion_path = work_dir / "git" / "worktrees" / f"{workspace_id}__companion__backend"
    _write(companion_path / ".git", "gitdir")

    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        now=now,
    )

    candidate = next(item for item in plan.candidates if item.workspace_id == workspace_id)
    assert candidate.companion_worktrees == ()

    with patch("awf.node.git_manager.GitManager") as mock_gm_cls:
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree = AsyncMock()
        result = await _default_worktree_remover(
            candidate,
            session_factory=session_factory,
            work_dir=work_dir,
        )

    assert result.status == "succeeded"
    mock_gm.remove_worktree.assert_awaited_once_with(
        workspace_id=workspace_id,
        repo_url="git@github.com:example/repo.git",
    )


@pytest.mark.unit
async def test_default_worktree_remover_continues_after_companion_failure(
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
        pr_merge_sha="s" * 40,
        task_policy={
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@github.com:example/backend.git",
                    "base_branch": "development",
                },
                {
                    "name": "web",
                    "repo_url": "git@github.com:example/web.git",
                    "base_branch": "development",
                },
            ]
        },
    )
    candidate = WorkspaceGCCandidate(
        workspace_id=workspace_id,
        status=WorkspaceStatus.completed.value,
        updated_at=now,
        age_hours=200,
        reason_code="COMPLETED_PR_RETENTION_EXPIRED",
        worktree=WorkspaceGCPath(
            kind="worktree",
            path=work_dir / "git" / "worktrees" / workspace_id,
            exists=True,
            estimated_bytes=0,
        ),
        compose=WorkspaceGCPath(
            kind="compose",
            path=work_dir / "compose" / workspace_id,
            exists=False,
            estimated_bytes=0,
        ),
        auth=WorkspaceGCPath(
            kind="auth", path=work_dir / "auth" / workspace_id, exists=False, estimated_bytes=0
        ),
    )

    async def _remove_worktree(*, workspace_id: str, repo_url: str) -> None:
        del repo_url
        if workspace_id.endswith("__companion__backend"):
            raise RuntimeError("backend mirror missing")

    with patch("awf.node.git_manager.GitManager") as mock_gm_cls:
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree = AsyncMock(side_effect=_remove_worktree)
        result = await _default_worktree_remover(
            candidate,
            session_factory=session_factory,
            work_dir=work_dir,
        )

    assert result.status == "partial"
    assert result.reason_code == "GIT_WORKTREE_REMOVE_FAILED"
    assert result.error is not None
    assert "backend mirror missing" in result.error
    assert [target.to_dict() for target in result.target_results] == [
        {
            "worktree_id": workspace_id,
            "status": "succeeded",
            "reason_code": "WORKTREE_REMOVE_SUCCEEDED",
        },
        {
            "worktree_id": f"{workspace_id}__companion__backend",
            "status": "failed",
            "reason_code": "GIT_WORKTREE_REMOVE_FAILED",
            "error": "backend mirror missing",
        },
        {
            "worktree_id": f"{workspace_id}__companion__web",
            "status": "succeeded",
            "reason_code": "WORKTREE_REMOVE_SUCCEEDED",
        },
    ]
    assert [call.kwargs for call in mock_gm.remove_worktree.await_args_list] == [
        {
            "workspace_id": workspace_id,
            "repo_url": "git@github.com:example/repo.git",
        },
        {
            "workspace_id": f"{workspace_id}__companion__backend",
            "repo_url": "git@github.com:example/backend.git",
        },
        {
            "workspace_id": f"{workspace_id}__companion__web",
            "repo_url": "git@github.com:example/web.git",
        },
    ]


@pytest.mark.unit
async def test_default_worktree_remover_missing_companion_noop_does_not_make_partial(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    primary_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="u" * 40,
        task_policy={
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@github.com:example/backend.git",
                    "base_branch": "development",
                }
            ]
        },
    )
    companion_id = f"{primary_id}__companion__backend"
    companion_path = work_dir / "git" / "worktrees" / companion_id
    candidate = WorkspaceGCCandidate(
        workspace_id=primary_id,
        status=WorkspaceStatus.completed.value,
        updated_at=now,
        age_hours=200,
        reason_code="COMPLETED_PR_RETENTION_EXPIRED",
        worktree=WorkspaceGCPath(
            kind="worktree",
            path=work_dir / "git" / "worktrees" / primary_id,
            exists=True,
            estimated_bytes=0,
        ),
        compose=WorkspaceGCPath(
            kind="compose",
            path=work_dir / "compose" / primary_id,
            exists=False,
            estimated_bytes=0,
        ),
        auth=WorkspaceGCPath(
            kind="auth", path=work_dir / "auth" / primary_id, exists=False, estimated_bytes=0
        ),
        companion_worktrees=(
            WorkspaceGCPath(
                kind=f"companion_worktree:{companion_id}",
                path=companion_path,
                exists=False,
                estimated_bytes=0,
            ),
        ),
    )

    async def _remove_worktree(*, workspace_id: str, repo_url: str) -> None:
        del repo_url
        if workspace_id == primary_id:
            raise RuntimeError("primary mirror missing")

    with patch("awf.node.git_manager.GitManager") as mock_gm_cls:
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree = AsyncMock(side_effect=_remove_worktree)
        result = await _default_worktree_remover(
            candidate,
            session_factory=session_factory,
            work_dir=work_dir,
        )

    assert result.status == "failed"
    assert result.reason_code == "GIT_WORKTREE_REMOVE_FAILED"
    assert [target.to_dict() for target in result.target_results] == [
        {
            "worktree_id": primary_id,
            "status": "failed",
            "reason_code": "GIT_WORKTREE_REMOVE_FAILED",
            "error": "primary mirror missing",
        },
        {
            "worktree_id": companion_id,
            "status": "succeeded",
            "reason_code": "WORKTREE_REMOVE_SUCCEEDED",
        },
    ]


@pytest.mark.unit
async def test_default_worktree_remover_skips_when_no_repo_url(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=200),
    )
    async with session_factory() as session:
        ws = await session.get(Workspace, workspace_id)
        assert ws is not None
        ws.repo_url = ""
        await session.commit()

    candidate = WorkspaceGCCandidate(
        workspace_id=workspace_id,
        status=WorkspaceStatus.failed.value,
        updated_at=now,
        age_hours=200,
        reason_code="FAILED_WORKSPACE_NO_WORK",
        worktree=WorkspaceGCPath(
            kind="worktree",
            path=work_dir / "git" / "worktrees" / workspace_id,
            exists=True,
            estimated_bytes=0,
        ),
        compose=WorkspaceGCPath(
            kind="compose",
            path=work_dir / "compose" / workspace_id,
            exists=False,
            estimated_bytes=0,
        ),
        auth=WorkspaceGCPath(
            kind="auth", path=work_dir / "auth" / workspace_id, exists=False, estimated_bytes=0
        ),
    )

    result = await _default_worktree_remover(
        candidate,
        session_factory=session_factory,
        work_dir=work_dir,
    )
    assert result.status == "skipped"
    assert result.reason_code == "NO_REPO_URL"


@pytest.mark.unit
async def test_default_worktree_remover_skips_existing_plain_directory(
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
        pr_merge_sha="r" * 40,
    )
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    worktree_path.mkdir(parents=True)
    candidate = WorkspaceGCCandidate(
        workspace_id=workspace_id,
        status=WorkspaceStatus.completed.value,
        updated_at=now,
        age_hours=200,
        reason_code="COMPLETED_PR_RETENTION_EXPIRED",
        worktree=WorkspaceGCPath(
            kind="worktree",
            path=worktree_path,
            exists=True,
            estimated_bytes=0,
        ),
        compose=WorkspaceGCPath(
            kind="compose",
            path=work_dir / "compose" / workspace_id,
            exists=False,
            estimated_bytes=0,
        ),
        auth=WorkspaceGCPath(
            kind="auth", path=work_dir / "auth" / workspace_id, exists=False, estimated_bytes=0
        ),
    )

    result = await _default_worktree_remover(
        candidate,
        session_factory=session_factory,
        work_dir=work_dir,
    )

    assert result.status == "skipped"
    assert result.reason_code == "WORKTREE_NOT_GIT_MANAGED"


@pytest.mark.unit
async def test_default_worktree_remover_removes_companion_when_primary_plain_directory(
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
        pr_merge_sha="t" * 40,
        task_policy={
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@github.com:example/backend.git",
                    "base_branch": "development",
                }
            ]
        },
    )
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    companion_path = work_dir / "git" / "worktrees" / f"{workspace_id}__companion__backend"
    worktree_path.mkdir(parents=True)
    _write(companion_path / ".git", "gitdir")
    candidate = WorkspaceGCCandidate(
        workspace_id=workspace_id,
        status=WorkspaceStatus.completed.value,
        updated_at=now,
        age_hours=200,
        reason_code="COMPLETED_PR_RETENTION_EXPIRED",
        worktree=WorkspaceGCPath(
            kind="worktree",
            path=worktree_path,
            exists=True,
            estimated_bytes=0,
        ),
        compose=WorkspaceGCPath(
            kind="compose",
            path=work_dir / "compose" / workspace_id,
            exists=False,
            estimated_bytes=0,
        ),
        auth=WorkspaceGCPath(
            kind="auth", path=work_dir / "auth" / workspace_id, exists=False, estimated_bytes=0
        ),
        companion_worktrees=(
            WorkspaceGCPath(
                kind=f"companion_worktree:{companion_path.name}",
                path=companion_path,
                exists=True,
                estimated_bytes=0,
            ),
        ),
    )

    with patch("awf.node.git_manager.GitManager") as mock_gm_cls:
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree = AsyncMock()
        result = await _default_worktree_remover(
            candidate,
            session_factory=session_factory,
            work_dir=work_dir,
        )

    assert result.status == "succeeded"
    assert result.reason_code == "WORKTREE_REMOVE_SUCCEEDED"
    mock_gm.remove_worktree.assert_awaited_once_with(
        workspace_id=f"{workspace_id}__companion__backend",
        repo_url="git@github.com:example/backend.git",
    )


@pytest.mark.unit
async def test_default_worktree_remover_handles_git_error(
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
        pr_merge_sha="q" * 40,
    )
    candidate = WorkspaceGCCandidate(
        workspace_id=workspace_id,
        status=WorkspaceStatus.completed.value,
        updated_at=now,
        age_hours=200,
        reason_code="COMPLETED_PR_RETENTION_EXPIRED",
        worktree=WorkspaceGCPath(
            kind="worktree",
            path=work_dir / "git" / "worktrees" / workspace_id,
            exists=True,
            estimated_bytes=0,
        ),
        compose=WorkspaceGCPath(
            kind="compose",
            path=work_dir / "compose" / workspace_id,
            exists=False,
            estimated_bytes=0,
        ),
        auth=WorkspaceGCPath(
            kind="auth", path=work_dir / "auth" / workspace_id, exists=False, estimated_bytes=0
        ),
    )

    with patch("awf.node.git_manager.GitManager") as mock_gm_cls:
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree = AsyncMock(side_effect=RuntimeError("mirror missing"))
        result = await _default_worktree_remover(
            candidate,
            session_factory=session_factory,
            work_dir=work_dir,
        )
        assert result.status == "failed"
        assert result.reason_code == "GIT_WORKTREE_REMOVE_FAILED"
        assert "mirror missing" in result.error
