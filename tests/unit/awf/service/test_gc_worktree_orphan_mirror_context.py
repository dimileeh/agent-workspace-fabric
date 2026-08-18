from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from awf.node import git_manager as git_module
from awf.node.git_manager import GitOperationError, mirror_path_for_worktree
from awf.service.gc_worktrees import remove_orphan_worktree
from tests.unit.awf.service.gc_worktree_test_helpers import (
    _make_mirror_with_worktree,
    _make_synthetic_mirror_link,
    _write,
)


@pytest.mark.unit
async def test_remove_orphan_worktree_reports_already_removed_path(
    tmp_path: Path,
) -> None:
    """Verify remove orphan worktree reports already removed path."""
    work_dir = tmp_path / "service"
    workspace_id = "ws_removed"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id

    with patch("awf.node.git_manager.GitManager") as mock_gm_cls:
        result = await remove_orphan_worktree(
            workspace_id=workspace_id,
            path=worktree_path,
            work_dir=work_dir,
        )

    assert result.status == "skipped"
    assert result.reason_code == "PATH_ALREADY_REMOVED"
    assert [target.to_dict() for target in result.target_results] == [
        {
            "worktree_id": workspace_id,
            "status": "skipped",
            "reason_code": "PATH_ALREADY_REMOVED",
        }
    ]
    mock_gm_cls.assert_not_called()


@pytest.mark.unit
async def test_remove_orphan_worktree_skips_existing_non_git_directory(
    tmp_path: Path,
) -> None:
    """Verify remove orphan worktree skips existing non git directory."""
    work_dir = tmp_path / "service"
    workspace_id = "ws_non_git"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    worktree_path.mkdir(parents=True)
    (worktree_path / "artifact.txt").write_text("salvage evidence\n", encoding="utf-8")

    with patch("awf.node.git_manager.GitManager") as mock_gm_cls:
        result = await remove_orphan_worktree(
            workspace_id=workspace_id,
            path=worktree_path,
            work_dir=work_dir,
        )

    assert result.status == "skipped"
    assert result.reason_code == "WORKTREE_NOT_GIT_MANAGED"
    assert [target.to_dict() for target in result.target_results] == [
        {
            "worktree_id": workspace_id,
            "status": "skipped",
            "reason_code": "WORKTREE_NOT_GIT_MANAGED",
        }
    ]
    assert worktree_path.exists()
    mock_gm_cls.assert_not_called()


@pytest.mark.unit
async def test_remove_orphan_worktree_uses_companion_path_name_as_worktree_id(
    tmp_path: Path,
) -> None:
    """Verify remove orphan worktree uses companion path name as worktree id."""
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
    """Verify remove orphan worktree uses mirror registry when gitfile is missing."""
    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    repo_url = "git@github.com:example/repo.git"
    mirror = work_dir / "git" / "mirrors" / "repo.git"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    _make_synthetic_mirror_link(
        mirror=mirror,
        worktree=worktree_path,
        repo_url=repo_url,
        include_worktree_gitfile=False,
    )
    resolved_mirror = mirror.resolve()

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
async def test_remove_orphan_worktree_registry_probe_ignores_git_object_lookup_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify remove orphan worktree registry probe ignores git object lookup env."""
    work_dir = tmp_path / "service"
    workspace_id = "ws_poisoned_git_env"
    mirror = work_dir / "git" / "mirrors" / "repo.git"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    _make_synthetic_mirror_link(
        mirror=mirror,
        worktree=worktree_path,
        include_worktree_gitfile=False,
    )
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(tmp_path / "missing-objects"))
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(tmp_path / "missing-alternates"))

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


@pytest.mark.unit
async def test_remove_orphan_worktree_registry_probe_ignores_git_work_tree_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify remove orphan worktree registry probe ignores git work tree env."""
    work_dir = tmp_path / "service"
    workspace_id = "ws_poisoned_git_work_tree"
    mirror = work_dir / "git" / "mirrors" / "repo.git"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    _make_synthetic_mirror_link(
        mirror=mirror,
        worktree=worktree_path,
        include_worktree_gitfile=False,
    )
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "unrelated-worktree"))

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


@pytest.mark.unit
async def test_remove_orphan_worktree_registry_probe_ignores_git_common_dir_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify remove orphan worktree registry probe ignores git common dir env."""
    work_dir = tmp_path / "service"
    workspace_id = "ws_poisoned_git_common_dir"
    mirror = work_dir / "git" / "mirrors" / "repo.git"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    _make_synthetic_mirror_link(
        mirror=mirror,
        worktree=worktree_path,
        include_worktree_gitfile=False,
    )
    monkeypatch.setenv("GIT_COMMON_DIR", str(tmp_path / "missing-common-dir"))

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


@pytest.mark.unit
async def test_remove_orphan_worktree_does_not_rehash_rewritten_origin_url(
    tmp_path: Path,
) -> None:
    """Verify remove orphan worktree does not rehash rewritten origin url."""
    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    original_repo_url = "./relative-origin"
    rewritten_repo_url = str(tmp_path / "relative-origin")
    mirror = work_dir / "git" / "mirrors" / "relative-origin-original.git"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    _make_synthetic_mirror_link(
        mirror=mirror,
        worktree=worktree_path,
        repo_url=rewritten_repo_url,
        include_worktree_gitfile=False,
    )
    assert rewritten_repo_url != original_repo_url

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
    """Verify remove orphan worktree preserves git operation error reason code."""
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
    """Verify remove orphan worktree reports metadata probe failure."""
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
async def test_remove_orphan_worktree_propagates_unexpected_remove_failure(
    tmp_path: Path,
) -> None:
    """Verify remove orphan worktree propagates unexpected remove failure."""
    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    mirror = work_dir / "git" / "mirrors" / "repo.git"
    worktree_path.mkdir(parents=True)
    remove_error = RuntimeError("filesystem refused worktree metadata cleanup")

    with (
        patch(
            "awf.service.gc_worktrees.git_context_mirror_path_for_worktree",
            return_value=mirror,
        ),
        patch("awf.node.git_manager.GitManager") as mock_gm_cls,
        pytest.raises(RuntimeError, match="filesystem refused worktree metadata cleanup"),
    ):
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree_from_mirror = AsyncMock(side_effect=remove_error)
        await remove_orphan_worktree(
            workspace_id=workspace_id,
            path=worktree_path,
            work_dir=work_dir,
        )


@pytest.mark.unit
async def test_remove_orphan_worktree_propagates_unexpected_metadata_probe_failure(
    tmp_path: Path,
) -> None:
    """Verify remove orphan worktree propagates unexpected metadata probe failure."""
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
        pytest.raises(RuntimeError, match="symlink loop from linked worktree gitdir"),
    ):
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree_from_mirror = AsyncMock()
        await remove_orphan_worktree(
            workspace_id=workspace_id,
            path=worktree_path,
            work_dir=work_dir,
        )


@pytest.mark.unit
async def test_remove_orphan_worktree_wraps_linked_git_resolution_failure(
    tmp_path: Path,
) -> None:
    """Verify remove orphan worktree wraps linked git resolution failure."""
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
    """Verify remove orphan worktree fails loudly when mirror context unresolved."""
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


@pytest.mark.unit
@pytest.mark.parametrize(
    "workspace_id",
    ["ws_legacy:name", "ws_legacy name", "ws_legacy@host"],
    ids=["colon", "space", "at-sign"],
)
async def test_remove_orphan_worktree_reaps_legacy_path_safe_directory_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workspace_id: str,
) -> None:
    """A row-less orphan named outside the generated id grammar still reaps.

    ``scan_managed_worktrees`` classifies every directory whose name starts with
    ``ws_``, so a legacy/synthetic directory reaches ``GitManager``'s worktree-path
    sink. When the sink rejected it, ``_reap_worktrees`` recorded a failed reap and
    skipped its filesystem-deletion fallback, so the directory could never be
    reclaimed. Names that are safe single path components must go through.
    """
    work_dir = tmp_path / "service"
    _make_mirror_with_worktree(tmp_path, work_dir, workspace_id)
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    calls: list[list[str]] = []

    async def _record(
        self: git_module.GitManager, args: list[str], *, operation: str
    ) -> git_module.GitResult:
        calls.append(args)
        return git_module.GitResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(git_module.GitManager, "_run", _record)

    result = await remove_orphan_worktree(
        workspace_id=workspace_id,
        path=worktree_path,
        work_dir=work_dir,
    )

    assert result.status == "succeeded"
    assert result.reason_code == "WORKTREE_REMOVE_SUCCEEDED"
    assert [target.to_dict() for target in result.target_results] == [
        {
            "worktree_id": workspace_id,
            "status": "succeeded",
            "reason_code": "WORKTREE_REMOVE_SUCCEEDED",
        }
    ]
    # The real (unmocked) GitManager ran the removal against the exact directory.
    assert ["worktree", "remove", "--force", str(worktree_path)] == calls[0][-4:]
