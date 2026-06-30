from __future__ import annotations

from pathlib import Path


def _write(path: Path, content: str) -> None:
    """Test helper for write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_mirror_with_worktree(tmp_path: Path, work_dir: Path, workspace_id: str) -> str:
    """Test helper for make mirror with worktree."""
    origin = tmp_path / "origin"
    origin.mkdir()
    repo_url = str(origin)
    mirror = work_dir / "git" / "mirrors" / "repo.git"
    worktree = work_dir / "git" / "worktrees" / workspace_id
    _make_synthetic_mirror_link(mirror=mirror, worktree=worktree)
    return repo_url


def _make_synthetic_mirror_link(
    *,
    mirror: Path,
    worktree: Path,
    repo_url: str = "git@github.com:example/repo.git",
    include_worktree_gitfile: bool = True,
) -> None:
    """Test helper for make synthetic mirror link."""
    linked_git_dir = mirror / "worktrees" / worktree.name
    linked_git_dir.mkdir(parents=True)
    worktree.mkdir(parents=True, exist_ok=True)
    _write(
        mirror / "config",
        f'[core]\n\tbare = true\n[remote "origin"]\n\turl = {repo_url}\n',
    )
    _write(mirror / "HEAD", "ref: refs/heads/main\n")
    (mirror / "objects").mkdir(exist_ok=True)
    (mirror / "refs").mkdir(exist_ok=True)
    if include_worktree_gitfile:
        _write(worktree / ".git", f"gitdir: {linked_git_dir}\n")
    (linked_git_dir / "gitdir").write_text(str(worktree / ".git"), encoding="utf-8")
