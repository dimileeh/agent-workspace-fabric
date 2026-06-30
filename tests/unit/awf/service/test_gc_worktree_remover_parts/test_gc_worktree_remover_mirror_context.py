from __future__ import annotations

import os
import subprocess
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
    _git_bare_probe_env,
    _has_stale_managed_linked_mirror,
    _is_bare_git_repository,
    _managed_mirror_path,
    _mirror_registry_points_to_worktree,
    git_context_mirror_path_for_worktree,
    is_existing_non_git_worktree,
    remove_orphan_worktree,
)
from tests.unit.awf.service.gc_worktree_test_helpers import (
    _make_mirror_with_worktree,
    _make_synthetic_mirror_link,
    _write,
)
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
    assert rewritten_repo_url != original_repo_url
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    _make_synthetic_mirror_link(
        mirror=mirror,
        worktree=worktree_path,
        repo_url=rewritten_repo_url,
        include_worktree_gitfile=False,
    )
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
async def test_default_worktree_remover_uses_linked_mirror_when_gitfile_is_present(
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
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    _make_synthetic_mirror_link(
        mirror=mirror,
        worktree=worktree_path,
        repo_url=rewritten_repo_url,
        include_worktree_gitfile=True,
    )
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
async def test_default_worktree_remover_skips_existing_standalone_git_directory(
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
    )
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    worktree_path.mkdir(parents=True)
    (worktree_path / ".git").mkdir()
    (worktree_path / ".git" / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n",
        encoding="utf-8",
    )
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

    assert result.status == "skipped"
    assert result.reason_code == "WORKTREE_NOT_GIT_MANAGED"
    assert is_existing_non_git_worktree(worktree_path, work_dir=work_dir) is True
    mock_gm.remove_worktree_from_mirror.assert_not_awaited()
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
    _make_synthetic_mirror_link(mirror=linked_mirror, worktree=worktree_path)
    _make_synthetic_mirror_link(
        mirror=duplicate_mirror,
        worktree=worktree_path,
        include_worktree_gitfile=False,
    )
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
    duplicate_git_dir = duplicate_mirror / "worktrees" / workspace_id
    _make_synthetic_mirror_link(mirror=linked_mirror, worktree=worktree_path)
    duplicate_git_dir.mkdir(parents=True)
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
async def test_remove_orphan_worktree_ignores_malformed_duplicate_registry_before_valid_match(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    malformed_mirror = work_dir / "git" / "mirrors" / "a-malformed.git"
    valid_mirror = work_dir / "git" / "mirrors" / "z-valid.git"
    malformed_git_dir = malformed_mirror / "worktrees" / workspace_id
    _make_synthetic_mirror_link(
        mirror=valid_mirror,
        worktree=worktree_path,
        include_worktree_gitfile=False,
    )
    malformed_git_dir.mkdir(parents=True)
    (malformed_git_dir / "gitdir").write_text("", encoding="utf-8")

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
        mirror_path=valid_mirror.resolve(),
    )


@pytest.mark.unit
def test_git_context_mirror_path_fails_closed_when_registry_fails_before_linked_validation(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    linked_mirror = work_dir / "git" / "mirrors" / "z-linked.git"
    malformed_mirror = work_dir / "git" / "mirrors" / "a-malformed.git"
    linked_git_dir = linked_mirror / "worktrees" / workspace_id
    malformed_git_dir = malformed_mirror / "worktrees" / workspace_id
    linked_git_dir.mkdir(parents=True)
    malformed_git_dir.mkdir(parents=True)
    _write(worktree_path / ".git", f"gitdir: {linked_git_dir}\n")
    (malformed_git_dir / "gitdir").write_text("", encoding="utf-8")

    assert git_context_mirror_path_for_worktree(worktree_path, work_dir=work_dir) is None


@pytest.mark.unit
def test_git_context_mirror_path_fails_closed_when_bare_probe_fails(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    mirror_path = work_dir / "git" / "mirrors" / "repo.git"
    _make_synthetic_mirror_link(
        mirror=mirror_path,
        worktree=worktree_path,
        include_worktree_gitfile=False,
    )

    probe_error = GitOperationError(
        operation="worktree.git_context_probe",
        returncode=128,
        stdout="",
        stderr="fatal: cannot inspect mirror",
        reason_code="WORKTREE_GIT_CONTEXT_RESOLUTION_FAILED",
    )

    def _managed_bare_mirror_side_effect(
        path: Path | None,
        mirrors_root: Path,
    ) -> Path | None:
        if path is None:
            return None
        raise probe_error

    with (
        patch(
            "awf.service.gc_worktrees._managed_bare_mirror_path",
            side_effect=_managed_bare_mirror_side_effect,
        ),
        pytest.raises(GitOperationError) as raised,
    ):
        git_context_mirror_path_for_worktree(worktree_path, work_dir=work_dir)

    assert raised.value.reason_code == "WORKTREE_GIT_CONTEXT_RESOLUTION_FAILED"
    assert "fatal: cannot inspect mirror" in raised.value.stderr


@pytest.mark.unit
def test_git_context_mirror_path_keeps_linked_registry_when_fallback_probe_fails(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    linked_mirror = work_dir / "git" / "mirrors" / "linked.git"
    _make_synthetic_mirror_link(mirror=linked_mirror, worktree=worktree_path)

    probe_results = [
        subprocess.CompletedProcess(args=["git"], returncode=0, stdout="true\n", stderr=""),
        subprocess.CompletedProcess(
            args=["git"], returncode=128, stdout="", stderr="fallback failed"
        ),
    ]

    with patch("awf.service.gc_worktrees.subprocess.run", side_effect=probe_results):
        assert git_context_mirror_path_for_worktree(worktree_path, work_dir=work_dir) == (
            linked_mirror.resolve()
        )


@pytest.mark.unit
def test_git_context_mirror_path_keeps_linked_registry_when_fallback_resolution_fails(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    linked_mirror = work_dir / "git" / "mirrors" / "linked.git"
    _make_synthetic_mirror_link(mirror=linked_mirror, worktree=worktree_path)
    fallback_error = GitOperationError(
        operation="worktree.git_context_probe",
        returncode=1,
        stdout="",
        stderr="fallback resolution failed",
        reason_code="WORKTREE_GIT_CONTEXT_RESOLUTION_FAILED",
    )

    with (
        patch(
            "awf.service.gc_worktrees._managed_bare_mirror_path",
            side_effect=(linked_mirror.resolve(), fallback_error),
        ),
        patch(
            "awf.service.gc_worktrees._mirror_registry_points_to_worktree",
            return_value=True,
        ),
    ):
        assert git_context_mirror_path_for_worktree(worktree_path, work_dir=work_dir) == (
            linked_mirror.resolve()
        )


@pytest.mark.unit
def test_bare_git_repository_fail_closed_surfaces_probe_start_failure(
    tmp_path: Path,
) -> None:
    mirror_path = tmp_path / "service" / "git" / "mirrors" / "repo.git"
    _write(mirror_path / "config", "[core]\n\tbare = true\n")
    _write(mirror_path / "HEAD", "ref: refs/heads/main\n")
    (mirror_path / "objects").mkdir(parents=True)
    (mirror_path / "refs").mkdir()

    with (
        patch(
            "awf.service.gc_worktrees.subprocess.run",
            side_effect=OSError("git unavailable"),
        ),
        pytest.raises(GitOperationError) as raised,
    ):
        _is_bare_git_repository(mirror_path, fail_closed=True)

    assert raised.value.reason_code == "WORKTREE_GIT_CONTEXT_RESOLUTION_FAILED"
    assert f"could not probe bare mirror {mirror_path}" in raised.value.stderr
    assert "git unavailable" in raised.value.stderr


@pytest.mark.unit
def test_bare_git_repository_fail_closed_surfaces_probe_timeout(
    tmp_path: Path,
) -> None:
    mirror_path = tmp_path / "service" / "git" / "mirrors" / "repo.git"
    _write(mirror_path / "config", "[core]\n\tbare = true\n")
    _write(mirror_path / "HEAD", "ref: refs/heads/main\n")
    (mirror_path / "objects").mkdir(parents=True)
    (mirror_path / "refs").mkdir()

    with (
        patch(
            "awf.service.gc_worktrees.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=5),
        ),
        pytest.raises(GitOperationError) as raised,
    ):
        _is_bare_git_repository(mirror_path, fail_closed=True)

    assert raised.value.reason_code == "WORKTREE_GIT_CONTEXT_RESOLUTION_FAILED"
    assert "timed out after 5s" in raised.value.stderr
    assert str(mirror_path) in raised.value.stderr


@pytest.mark.unit
def test_bare_git_repository_returns_false_on_probe_timeout_when_not_fail_closed(
    tmp_path: Path,
) -> None:
    mirror_path = tmp_path / "service" / "git" / "mirrors" / "repo.git"
    _write(mirror_path / "config", "[core]\n\tbare = true\n")
    _write(mirror_path / "HEAD", "ref: refs/heads/main\n")
    (mirror_path / "objects").mkdir(parents=True)
    (mirror_path / "refs").mkdir()

    with patch(
        "awf.service.gc_worktrees.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=5),
    ):
        assert _is_bare_git_repository(mirror_path, fail_closed=False) is False


@pytest.mark.unit
def test_bare_git_repository_returns_false_on_probe_start_failure_when_not_fail_closed(
    tmp_path: Path,
) -> None:
    mirror_path = tmp_path / "service" / "git" / "mirrors" / "repo.git"
    _write(mirror_path / "config", "[core]\n\tbare = true\n")
    _write(mirror_path / "HEAD", "ref: refs/heads/main\n")
    (mirror_path / "objects").mkdir(parents=True)
    (mirror_path / "refs").mkdir()

    with patch(
        "awf.service.gc_worktrees.subprocess.run",
        side_effect=OSError("git unavailable"),
    ):
        assert _is_bare_git_repository(mirror_path, fail_closed=False) is False


@pytest.mark.unit
def test_git_context_mirror_path_uses_registry_fallback_when_metadata_match_is_not_bare(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    valid_mirror = work_dir / "git" / "mirrors" / "valid.git"
    non_bare_mirror = work_dir / "git" / "mirrors" / "non-bare"
    valid_git_dir = valid_mirror / "worktrees" / workspace_id
    non_bare_git_dir = non_bare_mirror / "worktrees" / workspace_id
    _make_synthetic_mirror_link(
        mirror=valid_mirror,
        worktree=worktree_path,
        include_worktree_gitfile=False,
    )
    non_bare_git_dir.mkdir(parents=True)
    (non_bare_git_dir / "gitdir").write_text(str(worktree_path / ".git"), encoding="utf-8")
    os.utime(valid_git_dir, ns=(1, 1))
    os.utime(non_bare_git_dir, ns=(2, 2))

    assert git_context_mirror_path_for_worktree(worktree_path, work_dir=work_dir) == (
        valid_mirror.resolve()
    )


@pytest.mark.unit
def test_git_context_mirror_path_prefers_registered_mirror_when_linked_registry_mismatches(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    linked_mirror = work_dir / "git" / "mirrors" / "linked.git"
    registered_mirror = work_dir / "git" / "mirrors" / "registered.git"
    linked_git_dir = linked_mirror / "worktrees" / workspace_id
    registered_git_dir = registered_mirror / "worktrees" / workspace_id
    _make_synthetic_mirror_link(mirror=linked_mirror, worktree=worktree_path)
    _make_synthetic_mirror_link(
        mirror=registered_mirror,
        worktree=worktree_path,
        include_worktree_gitfile=False,
    )
    (linked_git_dir / "gitdir").write_text(
        str(work_dir / "git" / "worktrees" / "other" / ".git"),
        encoding="utf-8",
    )
    os.utime(linked_git_dir, ns=(2, 2))
    os.utime(registered_git_dir, ns=(1, 1))

    assert git_context_mirror_path_for_worktree(worktree_path, work_dir=work_dir) == (
        registered_mirror.resolve()
    )


@pytest.mark.unit
async def test_remove_orphan_worktree_uses_managed_linked_mirror_without_registry(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    mirror_path = work_dir / "git" / "mirrors" / "repo.git"
    _make_synthetic_mirror_link(mirror=mirror_path, worktree=worktree_path)

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
async def test_remove_orphan_worktree_skips_existing_non_git_linked_mirror_directory(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    recreated_mirror = work_dir / "git" / "mirrors" / "recreated.git"
    recreated_linked_git_dir = recreated_mirror / "worktrees" / workspace_id
    recreated_mirror.mkdir(parents=True)
    _write(worktree_path / ".git", f"gitdir: {recreated_linked_git_dir}\n")

    with patch("awf.node.git_manager.GitManager") as mock_gm_cls:
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree_from_mirror = AsyncMock()
        result = await remove_orphan_worktree(
            workspace_id=workspace_id,
            path=worktree_path,
            work_dir=work_dir,
        )

    assert result.status == "skipped"
    assert result.reason_code == "WORKTREE_NOT_GIT_MANAGED"
    assert is_existing_non_git_worktree(worktree_path, work_dir=work_dir) is True
    mock_gm.remove_worktree_from_mirror.assert_not_awaited()


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
    _make_synthetic_mirror_link(
        mirror=correct_mirror,
        worktree=worktree_path,
        include_worktree_gitfile=False,
    )
    wrong_linked_git_dir.mkdir(parents=True)
    _write(worktree_path / ".git", f"gitdir: {wrong_linked_git_dir}\n")
    assert correct_linked_git_dir.is_dir()

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
    _make_synthetic_mirror_link(
        mirror=correct_mirror,
        worktree=worktree_path,
        include_worktree_gitfile=False,
    )
    _write(worktree_path / ".git", f"gitdir: {stale_linked_git_dir}\n")
    assert correct_linked_git_dir.is_dir()

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
def test_mirror_registry_probe_returns_false_when_link_is_missing(
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / "service" / "git" / "worktrees" / "ws_rowless"
    mirror_path = tmp_path / "service" / "git" / "mirrors" / "repo.git"

    assert _mirror_registry_points_to_worktree(mirror_path, worktree_path) is False


@pytest.mark.unit
def test_mirror_registry_probe_returns_false_for_malformed_back_reference(
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / "service" / "git" / "worktrees" / "ws_rowless"
    mirror_path = tmp_path / "service" / "git" / "mirrors" / "repo.git"
    linked_git_dir = mirror_path / "worktrees" / worktree_path.name
    linked_git_dir.mkdir(parents=True)
    _write(linked_git_dir / "gitdir", "")

    assert _mirror_registry_points_to_worktree(mirror_path, worktree_path) is False


@pytest.mark.unit
def test_bare_probe_env_strips_git_object_lookup_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_DIR", "/tmp/foreign.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/tmp/foreign-worktree")
    monkeypatch.setenv("GIT_COMMON_DIR", "/tmp/foreign-common")
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/foreign-objects")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/foreign-alternates")
    monkeypatch.setenv("GIT_INDEX_FILE", "/tmp/foreign-index")

    env = _git_bare_probe_env()

    assert "GIT_DIR" not in env
    assert "GIT_WORK_TREE" not in env
    assert "GIT_COMMON_DIR" not in env
    assert "GIT_OBJECT_DIRECTORY" not in env
    assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in env
    assert "GIT_INDEX_FILE" not in env


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
def test_managed_mirror_path_normalizes_fallback_before_root_check(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mirrors_root = tmp_path / "service" / "git" / "mirrors"
    mirror_path = mirrors_root / ".." / "outside.git"
    original_resolve = Path.resolve

    def resolve_or_fail(path: Path, *args: object, **kwargs: object) -> Path:
        if path in {mirror_path, mirrors_root}:
            raise OSError("synthetic resolve failure")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_or_fail)

    assert _managed_mirror_path(mirror_path, mirrors_root) is None


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


@pytest.mark.unit
def test_existing_directory_without_git_entry_ignores_malformed_registry(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    workspace_id = "ws_salvage"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    malformed_git_dir = work_dir / "git" / "mirrors" / "malformed.git" / "worktrees" / workspace_id
    worktree_path.mkdir(parents=True)
    malformed_git_dir.mkdir(parents=True)
    (malformed_git_dir / "gitdir").write_text("", encoding="utf-8")

    assert is_existing_non_git_worktree(worktree_path, work_dir=work_dir) is True


@pytest.mark.unit
async def test_remove_orphan_worktree_skips_stale_managed_linked_mirror(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    workspace_id = "ws_rowless"
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    deleted_mirror = work_dir / "git" / "mirrors" / "deleted.git"
    deleted_linked_git_dir = deleted_mirror / "worktrees" / workspace_id
    _write(worktree_path / ".git", f"gitdir: {deleted_linked_git_dir}\n")

    with patch("awf.node.git_manager.GitManager") as mock_gm_cls:
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree_from_mirror = AsyncMock()
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
    assert is_existing_non_git_worktree(worktree_path, work_dir=work_dir) is True
    mock_gm.remove_worktree_from_mirror.assert_not_awaited()


@pytest.mark.unit
def test_stale_managed_linked_mirror_probe_errors_are_not_stale(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    worktree_path = work_dir / "git" / "worktrees" / "ws_rowless"

    with patch(
        "awf.node.git_manager.mirror_path_for_worktree",
        side_effect=RuntimeError("synthetic gitfile loop"),
    ):
        assert _has_stale_managed_linked_mirror(worktree_path, work_dir=work_dir) is False
