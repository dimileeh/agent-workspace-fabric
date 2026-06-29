from __future__ import annotations

import os
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
from awf.node.git_manager import GitOperationError, mirror_path_for_worktree
from awf.service.gc import (
    WorkspaceGCCandidate,
    WorkspaceGCPath,
    _default_worktree_remover,
    plan_terminal_workspace_gc,
)
from awf.service.gc_worktrees import remove_orphan_worktree


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield make_session_factory(engine)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git(args: list[str], cwd: Path) -> None:
    import subprocess

    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_mirror_with_worktree(tmp_path: Path, work_dir: Path, workspace_id: str) -> str:
    import subprocess

    origin = tmp_path / "origin"
    origin.mkdir()
    _git(["init", "-q", "-b", "main"], origin)
    _git(["config", "user.name", "AWF Test"], origin)
    _git(["config", "user.email", "awf@test.local"], origin)
    (origin / "README.md").write_text("initial\n", encoding="utf-8")
    _git(["add", "."], origin)
    _git(["commit", "-q", "-m", "init"], origin)
    repo_url = str(origin)
    mirror = work_dir / "git" / "mirrors" / "repo.git"
    mirror.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "--bare", repo_url, str(mirror)],
        check=True,
        capture_output=True,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    worktree.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "--git-dir", str(mirror), "worktree", "add", str(worktree), "main"],
        check=True,
        capture_output=True,
    )
    return repo_url


async def _workspace(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: WorkspaceStatus,
    updated_at: datetime,
    repo_url: str = "git@github.com:example/repo.git",
    title: str = "gc candidate",
    pr: bool = False,
    pr_merge_sha: str | None = None,
    task_policy: dict[str, object] | None = None,
) -> str:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url=repo_url,
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
async def test_default_worktree_remover_reports_primary_metadata_probe_failure(
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
        pr_merge_sha="m" * 40,
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
    probe_error = GitOperationError(
        operation="worktree.hooks_path_probe",
        returncode=1,
        stdout="",
        stderr="empty linked-worktree gitdir back-reference",
        reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
    )

    with (
        patch(
            "awf.service.gc_worktrees.git_context_mirror_path_for_worktree",
            side_effect=probe_error,
        ),
        patch("awf.node.git_manager.GitManager") as mock_gm_cls,
    ):
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree = AsyncMock()
        result = await _default_worktree_remover(
            candidate,
            session_factory=session_factory,
            work_dir=work_dir,
        )

    assert result.status == "failed"
    assert result.reason_code == "GIT_WORKTREE_REMOVE_FAILED"
    assert "empty linked-worktree gitdir back-reference" in (result.error or "")
    assert [target.to_dict() for target in result.target_results] == [
        {
            "worktree_id": workspace_id,
            "status": "failed",
            "reason_code": "MIRROR_HOOKS_PATH_REPAIR_FAILED",
            "error": str(probe_error),
        }
    ]
    mock_gm.remove_worktree.assert_not_awaited()


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
async def test_default_worktree_remover_reports_companion_metadata_probe_failure(
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
        pr_merge_sha="n" * 40,
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
    companion_id = f"{workspace_id}__companion__backend"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    companion_path = work_dir / "git" / "worktrees" / companion_id
    _write(worktree_path / ".git", "gitdir")
    companion_path.mkdir(parents=True)
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
                kind=f"companion_worktree:{companion_id}",
                path=companion_path,
                exists=True,
                estimated_bytes=0,
            ),
        ),
    )
    probe_error = GitOperationError(
        operation="worktree.hooks_path_probe",
        returncode=1,
        stdout="",
        stderr="empty linked-worktree gitdir back-reference",
        reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
    )

    with (
        patch(
            "awf.service.gc_worktrees.git_context_mirror_path_for_worktree",
            side_effect=probe_error,
        ),
        patch("awf.node.git_manager.GitManager") as mock_gm_cls,
    ):
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree = AsyncMock()
        result = await _default_worktree_remover(
            candidate,
            session_factory=session_factory,
            work_dir=work_dir,
        )

    assert result.status == "partial"
    assert result.reason_code == "GIT_WORKTREE_REMOVE_FAILED"
    assert [target.to_dict() for target in result.target_results] == [
        {
            "worktree_id": companion_id,
            "status": "failed",
            "reason_code": "MIRROR_HOOKS_PATH_REPAIR_FAILED",
            "error": str(probe_error),
        },
        {
            "worktree_id": workspace_id,
            "status": "succeeded",
            "reason_code": "WORKTREE_REMOVE_SUCCEEDED",
        },
    ]
    mock_gm.remove_worktree.assert_awaited_once_with(
        workspace_id=workspace_id,
        repo_url="git@github.com:example/repo.git",
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


@pytest.mark.unit
async def test_default_worktree_remover_uses_registry_mirror_when_gitfile_is_missing(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    import subprocess

    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    original_repo_url = "./relative-origin"
    rewritten_repo_url = str(tmp_path / "relative-origin")
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        repo_url=original_repo_url,
        pr=True,
        pr_merge_sha="p" * 40,
    )
    mirror = work_dir / "git" / "mirrors" / "relative-origin-original.git"
    subprocess.run(["git", "init", "--bare", str(mirror)], check=True, capture_output=True)
    subprocess.run(
        ["git", "--git-dir", str(mirror), "config", "remote.origin.url", rewritten_repo_url],
        check=True,
        capture_output=True,
    )
    assert rewritten_repo_url != original_repo_url
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    worktree_path.mkdir(parents=True)
    linked_git_dir = mirror / "worktrees" / workspace_id
    linked_git_dir.mkdir(parents=True)
    (linked_git_dir / "gitdir").write_text(str(worktree_path / ".git"), encoding="utf-8")
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

    with patch("awf.node.git_manager.GitManager") as mock_gm_cls:
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree_from_mirror = AsyncMock()
        mock_gm.remove_worktree = AsyncMock()
        result = await _default_worktree_remover(
            candidate,
            session_factory=session_factory,
            work_dir=work_dir,
        )

    assert result.status == "succeeded"
    assert result.reason_code == "WORKTREE_REMOVE_SUCCEEDED"
    mock_gm.remove_worktree_from_mirror.assert_awaited_once_with(
        workspace_id=workspace_id,
        mirror_path=mirror.resolve(),
    )
    mock_gm.remove_worktree.assert_not_awaited()


@pytest.mark.unit
async def test_remove_orphan_worktree_uses_resolved_linked_mirror(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    _make_mirror_with_worktree(tmp_path, work_dir, workspace_id)
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    mirror_path = mirror_path_for_worktree(worktree_path)
    assert mirror_path is not None

    with patch("awf.node.git_manager.GitManager") as mock_gm_cls:
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree_from_mirror = AsyncMock()
        result = await remove_orphan_worktree(
            workspace_id=workspace_id,
            path=worktree_path,
            work_dir=work_dir,
        )

    assert result.status == "succeeded"
    assert result.reason_code == "WORKTREE_REMOVE_SUCCEEDED"
    mock_gm.remove_worktree_from_mirror.assert_awaited_once_with(
        workspace_id=workspace_id,
        mirror_path=mirror_path,
    )


@pytest.mark.unit
async def test_remove_orphan_worktree_prefers_valid_linked_mirror_over_duplicate_registry(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    linked_mirror = work_dir / "git" / "mirrors" / "linked.git"
    duplicate_mirror = work_dir / "git" / "mirrors" / "duplicate.git"
    linked_git_dir = linked_mirror / "worktrees" / workspace_id
    duplicate_git_dir = duplicate_mirror / "worktrees" / workspace_id
    linked_git_dir.mkdir(parents=True)
    duplicate_git_dir.mkdir(parents=True)
    _write(worktree_path / ".git", f"gitdir: {linked_git_dir}\n")
    for git_dir in (linked_git_dir, duplicate_git_dir):
        (git_dir / "gitdir").write_text(str(worktree_path / ".git"), encoding="utf-8")
    os.utime(linked_git_dir, ns=(1, 1))
    os.utime(duplicate_git_dir, ns=(2, 2))

    with patch("awf.node.git_manager.GitManager") as mock_gm_cls:
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree_from_mirror = AsyncMock()
        result = await remove_orphan_worktree(
            workspace_id=workspace_id,
            path=worktree_path,
            work_dir=work_dir,
        )

    assert result.status == "succeeded"
    assert result.reason_code == "WORKTREE_REMOVE_SUCCEEDED"
    mock_gm.remove_worktree_from_mirror.assert_awaited_once_with(
        workspace_id=workspace_id,
        mirror_path=linked_mirror.resolve(),
    )


@pytest.mark.unit
async def test_remove_orphan_worktree_ignores_malformed_duplicate_registry_for_linked_mirror(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    linked_mirror = work_dir / "git" / "mirrors" / "linked.git"
    duplicate_mirror = work_dir / "git" / "mirrors" / "duplicate.git"
    linked_git_dir = linked_mirror / "worktrees" / workspace_id
    duplicate_git_dir = duplicate_mirror / "worktrees" / workspace_id
    linked_git_dir.mkdir(parents=True)
    duplicate_git_dir.mkdir(parents=True)
    _write(worktree_path / ".git", f"gitdir: {linked_git_dir}\n")
    (duplicate_git_dir / "gitdir").write_text("", encoding="utf-8")

    with patch("awf.node.git_manager.GitManager") as mock_gm_cls:
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree_from_mirror = AsyncMock()
        result = await remove_orphan_worktree(
            workspace_id=workspace_id,
            path=worktree_path,
            work_dir=work_dir,
        )

    assert result.status == "succeeded"
    assert result.reason_code == "WORKTREE_REMOVE_SUCCEEDED"
    mock_gm.remove_worktree_from_mirror.assert_awaited_once_with(
        workspace_id=workspace_id,
        mirror_path=linked_mirror.resolve(),
    )


@pytest.mark.unit
async def test_remove_orphan_worktree_uses_managed_linked_mirror_without_registry(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    mirror_path = work_dir / "git" / "mirrors" / "repo.git"
    linked_git_dir = mirror_path / "worktrees" / workspace_id
    mirror_path.mkdir(parents=True)
    _write(worktree_path / ".git", f"gitdir: {linked_git_dir}\n")

    with patch("awf.node.git_manager.GitManager") as mock_gm_cls:
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree_from_mirror = AsyncMock()
        result = await remove_orphan_worktree(
            workspace_id=workspace_id,
            path=worktree_path,
            work_dir=work_dir,
        )

    assert result.status == "succeeded"
    assert result.reason_code == "WORKTREE_REMOVE_SUCCEEDED"
    mock_gm.remove_worktree_from_mirror.assert_awaited_once_with(
        workspace_id=workspace_id,
        mirror_path=mirror_path.resolve(),
    )


@pytest.mark.unit
async def test_remove_orphan_worktree_fails_closed_for_external_linked_mirror(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    external_mirror = tmp_path / "external" / "repo.git"
    external_linked_git_dir = external_mirror / "worktrees" / workspace_id
    external_linked_git_dir.mkdir(parents=True)
    _write(worktree_path / ".git", f"gitdir: {external_linked_git_dir}\n")

    with patch("awf.node.git_manager.GitManager") as mock_gm_cls:
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree_from_mirror = AsyncMock()
        result = await remove_orphan_worktree(
            workspace_id=workspace_id,
            path=worktree_path,
            work_dir=work_dir,
        )

    assert result.status == "failed"
    assert result.reason_code == "ORPHAN_WORKTREE_GIT_CONTEXT_UNRESOLVED"
    assert result.error is not None
    assert "could not resolve mirror" in result.error
    mock_gm.remove_worktree_from_mirror.assert_not_awaited()


@pytest.mark.unit
async def test_remove_orphan_worktree_falls_back_when_linked_mirror_registry_mismatches(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    correct_mirror = work_dir / "git" / "mirrors" / "correct.git"
    wrong_mirror = work_dir / "git" / "mirrors" / "wrong.git"
    wrong_linked_git_dir = wrong_mirror / "worktrees" / workspace_id
    correct_linked_git_dir = correct_mirror / "worktrees" / workspace_id
    wrong_linked_git_dir.mkdir(parents=True)
    correct_linked_git_dir.mkdir(parents=True)
    _write(worktree_path / ".git", f"gitdir: {wrong_linked_git_dir}\n")
    (correct_linked_git_dir / "gitdir").write_text(str(worktree_path / ".git"), encoding="utf-8")

    with patch("awf.node.git_manager.GitManager") as mock_gm_cls:
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree_from_mirror = AsyncMock()
        result = await remove_orphan_worktree(
            workspace_id=workspace_id,
            path=worktree_path,
            work_dir=work_dir,
        )

    assert result.status == "succeeded"
    assert result.reason_code == "WORKTREE_REMOVE_SUCCEEDED"
    mock_gm.remove_worktree_from_mirror.assert_awaited_once_with(
        workspace_id=workspace_id,
        mirror_path=correct_mirror.resolve(),
    )


@pytest.mark.unit
async def test_remove_orphan_worktree_uses_companion_path_name_as_worktree_id(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    parent_workspace_id = "ws_parent"
    companion_id = f"{parent_workspace_id}__companion__backend"
    _make_mirror_with_worktree(tmp_path, work_dir, companion_id)
    worktree_path = work_dir / "git" / "worktrees" / companion_id
    mirror_path = mirror_path_for_worktree(worktree_path)
    assert mirror_path is not None

    with patch("awf.node.git_manager.GitManager") as mock_gm_cls:
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree_from_mirror = AsyncMock()
        result = await remove_orphan_worktree(
            workspace_id=parent_workspace_id,
            path=worktree_path,
            work_dir=work_dir,
        )

    assert result.status == "succeeded"
    assert result.reason_code == "WORKTREE_REMOVE_SUCCEEDED"
    assert [target.to_dict() for target in result.target_results] == [
        {
            "worktree_id": companion_id,
            "status": "succeeded",
            "reason_code": "WORKTREE_REMOVE_SUCCEEDED",
        }
    ]
    mock_gm.remove_worktree_from_mirror.assert_awaited_once_with(
        workspace_id=companion_id,
        mirror_path=mirror_path,
    )


@pytest.mark.unit
async def test_remove_orphan_worktree_uses_mirror_registry_when_gitfile_is_missing(
    tmp_path: Path,
) -> None:
    import subprocess

    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    repo_url = "git@github.com:example/repo.git"
    mirror = work_dir / "git" / "mirrors" / "repo.git"
    subprocess.run(["git", "init", "--bare", str(mirror)], check=True, capture_output=True)
    subprocess.run(
        ["git", "--git-dir", str(mirror), "config", "remote.origin.url", repo_url],
        check=True,
        capture_output=True,
    )
    resolved_mirror = mirror.resolve()
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    worktree_path.mkdir(parents=True)
    linked_git_dir = mirror / "worktrees" / workspace_id
    linked_git_dir.mkdir(parents=True)
    (linked_git_dir / "gitdir").write_text(str(worktree_path / ".git"), encoding="utf-8")

    with patch("awf.node.git_manager.GitManager") as mock_gm_cls:
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree_from_mirror = AsyncMock()
        result = await remove_orphan_worktree(
            workspace_id=workspace_id,
            path=worktree_path,
            work_dir=work_dir,
        )

    assert result.status == "succeeded"
    assert result.reason_code == "WORKTREE_REMOVE_SUCCEEDED"
    mock_gm.remove_worktree_from_mirror.assert_awaited_once_with(
        workspace_id=workspace_id,
        mirror_path=resolved_mirror,
    )
    assert worktree_path.exists()


@pytest.mark.unit
async def test_remove_orphan_worktree_does_not_rehash_rewritten_origin_url(
    tmp_path: Path,
) -> None:
    import subprocess

    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    original_repo_url = "./relative-origin"
    rewritten_repo_url = str(tmp_path / "relative-origin")
    mirror = work_dir / "git" / "mirrors" / "relative-origin-original.git"
    subprocess.run(["git", "init", "--bare", str(mirror)], check=True, capture_output=True)
    subprocess.run(
        ["git", "--git-dir", str(mirror), "config", "remote.origin.url", rewritten_repo_url],
        check=True,
        capture_output=True,
    )
    assert rewritten_repo_url != original_repo_url

    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    worktree_path.mkdir(parents=True)
    linked_git_dir = mirror / "worktrees" / workspace_id
    linked_git_dir.mkdir(parents=True)
    (linked_git_dir / "gitdir").write_text(str(worktree_path / ".git"), encoding="utf-8")

    with patch("awf.node.git_manager.GitManager") as mock_gm_cls:
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree_from_mirror = AsyncMock()
        result = await remove_orphan_worktree(
            workspace_id=workspace_id,
            path=worktree_path,
            work_dir=work_dir,
        )

    assert result.status == "succeeded"
    assert result.reason_code == "WORKTREE_REMOVE_SUCCEEDED"
    mock_gm.remove_worktree_from_mirror.assert_awaited_once_with(
        workspace_id=workspace_id,
        mirror_path=mirror.resolve(),
    )
    mock_gm.remove_worktree.assert_not_called()


@pytest.mark.unit
async def test_remove_orphan_worktree_reports_metadata_probe_failure(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    worktree_path.mkdir(parents=True)
    probe_error = GitOperationError(
        operation="worktree.hooks_path_probe",
        returncode=1,
        stdout="",
        stderr="empty linked-worktree gitdir back-reference",
        reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
    )

    with (
        patch(
            "awf.service.gc_worktrees.git_context_mirror_path_for_worktree",
            side_effect=[None, probe_error],
        ),
        patch("awf.node.git_manager.GitManager") as mock_gm_cls,
    ):
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree_from_mirror = AsyncMock()
        result = await remove_orphan_worktree(
            workspace_id=workspace_id,
            path=worktree_path,
            work_dir=work_dir,
        )

    assert result.status == "failed"
    assert result.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
    assert result.error == str(probe_error)
    assert [target.to_dict() for target in result.target_results] == [
        {
            "worktree_id": workspace_id,
            "status": "failed",
            "reason_code": "MIRROR_HOOKS_PATH_REPAIR_FAILED",
            "error": str(probe_error),
        }
    ]
    mock_gm.remove_worktree_from_mirror.assert_not_awaited()


@pytest.mark.unit
async def test_remove_orphan_worktree_reports_unexpected_metadata_probe_failure(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    worktree_path.mkdir(parents=True)
    probe_error = RuntimeError("symlink loop from linked worktree gitdir")

    with (
        patch(
            "awf.service.gc_worktrees.git_context_mirror_path_for_worktree",
            side_effect=probe_error,
        ),
        patch("awf.node.git_manager.GitManager") as mock_gm_cls,
    ):
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree_from_mirror = AsyncMock()
        result = await remove_orphan_worktree(
            workspace_id=workspace_id,
            path=worktree_path,
            work_dir=work_dir,
        )

    assert result.status == "failed"
    assert result.reason_code == "ORPHAN_WORKTREE_GIT_CONTEXT_PROBE_FAILED"
    assert result.error == "symlink loop from linked worktree gitdir"
    assert [target.to_dict() for target in result.target_results] == [
        {
            "worktree_id": workspace_id,
            "status": "failed",
            "reason_code": "ORPHAN_WORKTREE_GIT_CONTEXT_PROBE_FAILED",
            "error": "symlink loop from linked worktree gitdir",
        }
    ]
    mock_gm.remove_worktree_from_mirror.assert_not_awaited()


@pytest.mark.unit
async def test_remove_orphan_worktree_wraps_linked_git_resolution_failure(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    worktree_path.mkdir(parents=True)

    with (
        patch(
            "awf.node.git_manager.mirror_path_for_worktree",
            side_effect=RuntimeError("symlink loop from linked worktree gitdir"),
        ),
        patch("awf.node.git_manager.GitManager") as mock_gm_cls,
    ):
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree_from_mirror = AsyncMock()
        result = await remove_orphan_worktree(
            workspace_id=workspace_id,
            path=worktree_path,
            work_dir=work_dir,
        )

    assert result.status == "failed"
    assert result.reason_code == "WORKTREE_GIT_CONTEXT_RESOLUTION_FAILED"
    assert result.error is not None
    assert "could not resolve linked git context" in result.error
    assert "symlink loop from linked worktree gitdir" in result.error
    assert [target.to_dict() for target in result.target_results] == [
        {
            "worktree_id": workspace_id,
            "status": "failed",
            "reason_code": "WORKTREE_GIT_CONTEXT_RESOLUTION_FAILED",
            "error": result.error,
        }
    ]
    mock_gm.remove_worktree_from_mirror.assert_not_awaited()


@pytest.mark.unit
async def test_remove_orphan_worktree_fails_loudly_when_mirror_context_unresolved(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    worktree_path = work_dir / "git" / "worktrees" / "ws_rowless"
    _write(worktree_path / ".git", "[core]\n\trepositoryformatversion = 0\n")

    result = await remove_orphan_worktree(
        workspace_id="ws_rowless",
        path=worktree_path,
        work_dir=work_dir,
    )

    assert result.status == "failed"
    assert result.reason_code == "ORPHAN_WORKTREE_GIT_CONTEXT_UNRESOLVED"
    assert result.error is not None
    assert "could not resolve mirror" in result.error
    assert worktree_path.exists()
