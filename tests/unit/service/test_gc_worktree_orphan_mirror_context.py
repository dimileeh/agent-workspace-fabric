from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from awf.node.git_manager import GitOperationError, mirror_path_for_worktree
from awf.service.gc_worktrees import remove_orphan_worktree


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
async def test_remove_orphan_worktree_preserves_git_operation_error_reason_code(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    mirror = work_dir / "git" / "mirrors" / "repo.git"
    worktree_path.mkdir(parents=True)
    git_error = GitOperationError(
        operation="worktree.remove",
        returncode=128,
        stdout="",
        stderr="git worktree prune failed",
        reason_code="GIT_WORKTREE_PRUNE_FAILED",
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
        result = await remove_orphan_worktree(
            workspace_id=workspace_id,
            path=worktree_path,
            work_dir=work_dir,
        )

    assert result.status == "failed"
    assert result.reason_code == "GIT_WORKTREE_PRUNE_FAILED"
    assert [target.to_dict() for target in result.target_results] == [
        {
            "worktree_id": workspace_id,
            "status": "failed",
            "reason_code": "GIT_WORKTREE_PRUNE_FAILED",
            "error": str(git_error),
        }
    ]


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
