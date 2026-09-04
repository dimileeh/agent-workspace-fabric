"""Shared fixtures for item-start Git config snapshot edge tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def git(cwd: Path, *args: str) -> str:
    """Run git in ``cwd`` and return stdout."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def init_plain_repo(path: Path) -> str:
    """Create a plain (non-linked) repository with one commit; return HEAD."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "awf@example.com")
    git(path, "config", "user.name", "AWF Test")
    (path / "file.txt").write_text("base\n", encoding="utf-8")
    git(path, "add", "file.txt")
    git(path, "commit", "-qm", "init")
    return git(path, "rev-parse", "HEAD").strip()


def init_linked_layout(tmp_path: Path, *, name: str = "ws_link") -> tuple[Path, Path, Path, str]:
    """Create ``awf/worktrees/<name>`` linked to ``awf/mirrors/repo.git``.

    Returns ``(worktree, linked_git_dir, mirror, head)``.
    """
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    mirror = layout / "mirrors" / "repo.git"
    worktree = worktrees / name
    worktrees.mkdir(parents=True)
    src = tmp_path / "src_repo"
    head = init_plain_repo(src)
    subprocess.run(
        ["git", "clone", "--bare", str(src), str(mirror)],
        check=True,
        capture_output=True,
    )
    for key, value in (("user.email", "awf@example.com"), ("user.name", "AWF Test")):
        git(mirror, "config", key, value)
    git(mirror, "worktree", "add", str(worktree), "HEAD")
    linked = mirror / "worktrees" / name
    assert (worktree / ".git").is_file()
    assert linked.is_dir()
    return worktree, linked, mirror, head


def key_for(path: Path) -> str:
    """Cache key used by the item-start snapshot module."""
    return str(path.resolve())


def raise_oserror(*_args: object, **_kwargs: object) -> None:
    """Raise a generic OSError (not ENOENT) for monkeypatched os calls."""
    raise PermissionError("denied")


def make_fifo(path: Path) -> None:
    """Create a FIFO at ``path``."""
    os.mkfifo(path)
