from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.db.enums import WorkspaceStatus
from awf.db.session import make_session_factory
from awf.node.git_manager import GitOperationError, mirror_path_for_worktree
from awf.service.gc import WorkspaceGCCandidate, WorkspaceGCPath, _default_worktree_remover
from awf.service.gc_worktrees import (
    _managed_mirror_path,
    _mirror_registry_points_to_worktree,
    is_existing_non_git_worktree,
    remove_orphan_worktree,
)
from tests.unit.awf.service.gc_worktree_test_helpers import _make_mirror_with_worktree, _write
from tests.unit.service.test_gc_worktree_remover import _workspace


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield make_session_factory(engine)


@pytest.mark.unit
async def test_default_worktree_remover_uses_registry_mirror_when_gitfile_is_missing(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
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
    mirror.mkdir(parents=True)
    _write(
        mirror / "config",
        f'[remote "origin"]\n\turl = {rewritten_repo_url}\n',
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
async def test_default_worktree_remover_preserves_mirror_git_operation_error_reason_code(
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
    mirror = work_dir / "git" / "mirrors" / "repo.git"
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
    git_error = GitOperationError(
        operation="worktree.remove",
        returncode=128,
        stdout="",
        stderr="git worktree remove failed",
        reason_code="GIT_WORKTREE_REMOVE_LOCK_FAILED",
    )

    with (
        patch(
            "awf.service.gc_worktrees.git_context_mirror_path_for_worktree",
            return_value=mirror,
        ),
        patch("awf.node.git_manager.GitManager") as mock_gm_cls,
    ):
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree_from_mirror = AsyncMock(side_effect=git_error)
        result = await _default_worktree_remover(
            candidate,
            session_factory=session_factory,
            work_dir=work_dir,
        )

    assert result.status == "failed"
    assert result.reason_code == "GIT_WORKTREE_REMOVE_LOCK_FAILED"
    assert [target.to_dict() for target in result.target_results] == [
        {
            "worktree_id": workspace_id,
            "status": "failed",
            "reason_code": "GIT_WORKTREE_REMOVE_LOCK_FAILED",
            "error": str(git_error),
        }
    ]


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
async def test_remove_orphan_worktree_falls_back_when_linked_metadata_dir_is_missing(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    stale_mirror = work_dir / "git" / "mirrors" / "stale.git"
    correct_mirror = work_dir / "git" / "mirrors" / "correct.git"
    stale_linked_git_dir = stale_mirror / "worktrees" / workspace_id
    correct_linked_git_dir = correct_mirror / "worktrees" / workspace_id
    correct_linked_git_dir.mkdir(parents=True)
    _write(worktree_path / ".git", f"gitdir: {stale_linked_git_dir}\n")
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
def test_mirror_registry_probe_uses_absolute_worktree_when_resolve_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / "service" / "git" / "worktrees" / "ws_rowless"
    mirror_path = tmp_path / "service" / "git" / "mirrors" / "repo.git"
    linked_git_dir = mirror_path / "worktrees" / worktree_path.name
    linked_git_dir.mkdir(parents=True)
    _write(linked_git_dir / "gitdir", str(worktree_path.absolute() / ".git"))
    original_resolve = Path.resolve

    def resolve_or_fail(path: Path, *args: object, **kwargs: object) -> Path:
        if path == worktree_path:
            raise OSError("synthetic resolve failure")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_or_fail)

    assert _mirror_registry_points_to_worktree(mirror_path, worktree_path) is True


@pytest.mark.unit
def test_managed_mirror_path_uses_absolute_paths_when_resolve_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mirrors_root = tmp_path / "service" / "git" / "mirrors"
    mirror_path = mirrors_root / "repo.git"
    original_resolve = Path.resolve

    def resolve_or_fail(path: Path, *args: object, **kwargs: object) -> Path:
        if path in {mirror_path, mirrors_root}:
            raise OSError("synthetic resolve failure")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_or_fail)

    assert _managed_mirror_path(mirror_path, mirrors_root) == mirror_path.absolute()


@pytest.mark.unit
def test_missing_path_is_not_existing_non_git_worktree(tmp_path: Path) -> None:
    assert (
        is_existing_non_git_worktree(tmp_path / "service" / "git" / "worktrees" / "ws_missing")
        is False
    )


@pytest.mark.unit
def test_existing_directory_without_work_dir_context_is_non_git_worktree(tmp_path: Path) -> None:
    worktree_path = tmp_path / "service" / "git" / "worktrees" / "ws_salvage"
    worktree_path.mkdir(parents=True)

    assert is_existing_non_git_worktree(worktree_path) is True


@pytest.mark.unit
def test_managed_git_context_is_not_existing_non_git_worktree(tmp_path: Path) -> None:
    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    _make_mirror_with_worktree(tmp_path, work_dir, workspace_id)

    assert (
        is_existing_non_git_worktree(
            work_dir / "git" / "worktrees" / workspace_id,
            work_dir=work_dir,
        )
        is False
    )


@pytest.mark.unit
def test_existing_directory_without_git_context_is_non_git_worktree(tmp_path: Path) -> None:
    work_dir = tmp_path / "service"
    worktree_path = work_dir / "git" / "worktrees" / "ws_salvage"
    worktree_path.mkdir(parents=True)

    assert is_existing_non_git_worktree(worktree_path, work_dir=work_dir) is True
