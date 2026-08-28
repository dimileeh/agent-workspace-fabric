"""GitManager ensure_worktree linked-checkout usability regressions."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf.node.git_manager import GitManager, _worktree_checkout_is_usable


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def origin_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(["init", "-q", "-b", "development"], repo)
    _git(["config", "user.name", "AWF Test"], repo)
    _git(["config", "user.email", "awf@test.local"], repo)
    (repo / "README.md").write_text("first\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    return repo


@pytest.fixture
def manager(tmp_path: Path) -> GitManager:
    work_dir = tmp_path / "awf-work"
    work_dir.mkdir()
    return GitManager(work_dir)


@pytest.mark.unit
async def test_ensure_worktree_recreates_when_linked_git_dir_missing(
    manager: GitManager, origin_repo: Path
) -> None:
    """Checkout dir survives but mirror-side admin dir is gone → reconstruct."""
    import shutil

    layout = await manager.add_worktree(
        workspace_id="ws_ensure_dangling",
        repo_url=str(origin_repo),
        base_branch="development",
        new_branch="awf/ws_ensure_dangling",
    )
    linked_git_dir = layout.mirror_path / "worktrees" / "ws_ensure_dangling"
    assert linked_git_dir.is_dir()
    assert _worktree_checkout_is_usable(layout.worktree_path)

    shutil.rmtree(linked_git_dir)
    assert layout.worktree_path.is_dir()
    assert (layout.worktree_path / ".git").is_file()
    assert not _worktree_checkout_is_usable(layout.worktree_path)

    restored = await manager.ensure_worktree(
        workspace_id="ws_ensure_dangling",
        repo_url=str(origin_repo),
        base_branch="development",
        new_branch="awf/ws_ensure_dangling",
    )
    assert restored.worktree_path.is_dir()
    assert _worktree_checkout_is_usable(restored.worktree_path)
    sha = await manager.head_sha(workspace_id="ws_ensure_dangling")
    assert len(sha) == 40


@pytest.mark.unit
def test_worktree_checkout_usable_requires_reciprocal_registration(tmp_path: Path) -> None:
    worktree = tmp_path / "worktrees" / "ws_reciprocal"
    linked = tmp_path / "mirrors" / "repo.git" / "worktrees" / "ws_reciprocal"
    worktree.mkdir(parents=True)
    linked.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {linked}\n", encoding="utf-8")
    # Back-ref points at a different checkout → not usable.
    other = tmp_path / "worktrees" / "other" / ".git"
    other.parent.mkdir(parents=True)
    (linked / "gitdir").write_text(f"{other}\n", encoding="utf-8")
    assert not _worktree_checkout_is_usable(worktree)

    (linked / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    assert _worktree_checkout_is_usable(worktree)

    (linked / "gitdir").unlink()
    assert not _worktree_checkout_is_usable(worktree)


@pytest.mark.unit
def test_worktree_checkout_rejects_standalone_git_directory(tmp_path: Path) -> None:
    """A normal clone (.git dir) must not count as a usable managed worktree."""
    worktree = tmp_path / "worktrees" / "ws_standalone"
    worktree.mkdir(parents=True)
    (worktree / ".git").mkdir()
    assert not _worktree_checkout_is_usable(worktree)


@pytest.mark.unit
async def test_ensure_worktree_recreates_when_standalone_clone_occupies_path(
    manager: GitManager, origin_repo: Path
) -> None:
    """Incomplete restore replaced linked checkout with a plain clone → reconstruct."""
    import shutil

    workspace_id = "ws_ensure_standalone"
    layout = await manager.add_worktree(
        workspace_id=workspace_id,
        repo_url=str(origin_repo),
        base_branch="development",
        new_branch=f"awf/{workspace_id}",
    )
    assert _worktree_checkout_is_usable(layout.worktree_path)

    # Simulate an incomplete external restore: wipe the linked checkout and leave
    # an unrelated standalone clone at the managed path.
    shutil.rmtree(layout.worktree_path)
    layout.worktree_path.mkdir(parents=True)
    _git(["init", "-q", "-b", "development"], layout.worktree_path)
    _git(["config", "user.name", "AWF Test"], layout.worktree_path)
    _git(["config", "user.email", "awf@test.local"], layout.worktree_path)
    (layout.worktree_path / "README.md").write_text("orphan clone\n")
    _git(["add", "."], layout.worktree_path)
    _git(["commit", "-q", "-m", "orphan"], layout.worktree_path)
    assert (layout.worktree_path / ".git").is_dir()
    assert not _worktree_checkout_is_usable(layout.worktree_path)

    restored = await manager.ensure_worktree(
        workspace_id=workspace_id,
        repo_url=str(origin_repo),
        base_branch="development",
        new_branch=f"awf/{workspace_id}",
    )
    assert restored.worktree_path.is_dir()
    assert (restored.worktree_path / ".git").is_file()
    assert _worktree_checkout_is_usable(restored.worktree_path)
    sha = await manager.head_sha(workspace_id=workspace_id)
    assert len(sha) == 40
