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
from awf.node.git_manager import GitOperationError
from awf.service.gc import (
    WorkspaceGCCandidate,
    WorkspaceGCPath,
    _default_worktree_remover,
    plan_terminal_workspace_gc,
)
from tests.unit.awf.service.gc_worktree_test_helpers import _write


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

    remove_error = GitOperationError(
        operation="worktree.remove",
        returncode=1,
        stdout="",
        stderr="backend mirror missing",
        reason_code="GIT_WORKTREE_REMOVE_FAILED",
    )

    async def _remove_worktree(*, workspace_id: str, repo_url: str) -> None:
        del repo_url
        if workspace_id.endswith("__companion__backend"):
            raise remove_error

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
            "error": str(remove_error),
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

    remove_error = GitOperationError(
        operation="worktree.remove",
        returncode=1,
        stdout="",
        stderr="primary mirror missing",
        reason_code="GIT_WORKTREE_REMOVE_FAILED",
    )

    async def _remove_worktree(*, workspace_id: str, repo_url: str) -> None:
        del repo_url
        if workspace_id == primary_id:
            raise remove_error

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
            "error": str(remove_error),
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
    assert result.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
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

    def _probe_side_effect(path: Path, *, work_dir: Path) -> None:
        del work_dir
        if path == companion_path:
            raise probe_error

    with (
        patch(
            "awf.service.gc_worktrees.git_context_mirror_path_for_worktree",
            side_effect=_probe_side_effect,
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
    assert result.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
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
        mock_gm.remove_worktree = AsyncMock(
            side_effect=GitOperationError(
                operation="worktree.remove",
                returncode=1,
                stdout="",
                stderr="mirror missing",
                reason_code="GIT_WORKTREE_REMOVE_FAILED",
            )
        )
        result = await _default_worktree_remover(
            candidate,
            session_factory=session_factory,
            work_dir=work_dir,
        )
        assert result.status == "failed"
        assert result.reason_code == "GIT_WORKTREE_REMOVE_FAILED"
        assert "mirror missing" in result.error


@pytest.mark.unit
async def test_default_worktree_remover_preserves_git_operation_error_reason_code(
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
    git_error = GitOperationError(
        operation="worktree.remove",
        returncode=128,
        stdout="",
        stderr="git worktree list failed",
        reason_code="GIT_WORKTREE_LIST_FAILED",
    )

    with patch("awf.node.git_manager.GitManager") as mock_gm_cls:
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree = AsyncMock(side_effect=git_error)
        result = await _default_worktree_remover(
            candidate,
            session_factory=session_factory,
            work_dir=work_dir,
        )

    assert result.status == "failed"
    assert result.reason_code == "GIT_WORKTREE_LIST_FAILED"
    assert [target.to_dict() for target in result.target_results] == [
        {
            "worktree_id": workspace_id,
            "status": "failed",
            "reason_code": "GIT_WORKTREE_LIST_FAILED",
            "error": str(git_error),
        }
    ]
