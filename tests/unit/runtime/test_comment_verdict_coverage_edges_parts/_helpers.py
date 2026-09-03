"""Shared git worktree helpers for comment verdict coverage-edge tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def init_git_worktree(worktree: Path) -> None:
    """
    Initialize a Git worktree with a configured test identity and an initial commit containing `src/x.py` with base content.
    """
    subprocess.run(["git", "init"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    (worktree / "src").mkdir()
    target = worktree / "src" / "x.py"
    target.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/x.py"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=worktree, check=True, capture_output=True)


def init_git_worktree_with_dirty_submodule(worktree: Path, *, submodule_name: str = "sub") -> None:
    """
    Create a parent Git repository with a tracked submodule whose checked-out commit is newer than the commit recorded by the parent.
    """
    subprocess.run(["git", "init"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    submodule = worktree / submodule_name
    submodule.mkdir()
    subprocess.run(["git", "init"], cwd=submodule, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=submodule,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=submodule,
        check=True,
        capture_output=True,
    )
    (submodule / "file.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=submodule, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "sub init"], cwd=submodule, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "submodule", "add", f"./{submodule_name}", submodule_name],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "add sub"], cwd=worktree, check=True, capture_output=True
    )
    (submodule / "file.txt").write_text("v2\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=submodule, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "sub v2"], cwd=submodule, check=True, capture_output=True
    )


def replace_tracked_file_with_fifo(worktree: Path, *, path: str = "src/x.py") -> None:
    """Track a file, then replace it with a FIFO at the same path."""
    init_git_worktree(worktree)
    target = worktree / path
    target.unlink()
    os.mkfifo(target, mode=0o644)


def init_git_worktree_file_replaced_by_directory(
    worktree: Path,
    *,
    path: str = "src/x.py",
    child_name: str = "child.txt",
    child_contents: str = "payload\n",
) -> None:
    """Replace the tracked file with a directory containing an untracked child file.
    
    Parameters:
        worktree (Path): Repository worktree to modify.
        path (str): Path of the tracked file to replace.
        child_name (str): Name of the untracked file created inside the replacement directory.
        child_contents (str): Contents written to the untracked child file.
    """
    init_git_worktree(worktree)
    target = worktree / path
    target.unlink()
    replacement = worktree / path
    replacement.mkdir()
    (replacement / child_name).write_text(child_contents, encoding="utf-8")


def init_git_worktree_with_embedded_repo(
    worktree: Path,
    *,
    nested_name: str = "nested",
) -> str:
    """
    Create an untracked nested Git repository with an initial committed file.
    
    Parameters:
        nested_name (str): Name of the nested repository directory.
    
    Returns:
        str: Name of the nested repository directory.
    """
    init_git_worktree(worktree)
    nested = worktree / nested_name
    nested.mkdir()
    subprocess.run(["git", "init"], cwd=nested, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    (nested / "inner.txt").write_text("inner\n", encoding="utf-8")
    subprocess.run(["git", "add", "inner.txt"], cwd=nested, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "nested init"], cwd=nested, check=True, capture_output=True
    )
    return nested_name


def init_git_worktree_with_unborn_embedded_repo(
    worktree: Path,
    *,
    nested_name: str = "nested",
) -> str:
    """
    Create an untracked directory containing a Git repository with staged content and an unborn HEAD.
    
    Parameters:
    	nested_name (str): Name of the nested repository directory.
    
    Returns:
    	str: Name of the nested repository directory.
    """
    init_git_worktree(worktree)
    nested = worktree / nested_name
    nested.mkdir()
    subprocess.run(["git", "init"], cwd=nested, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    (nested / "inner.txt").write_text("inner\n", encoding="utf-8")
    subprocess.run(["git", "add", "inner.txt"], cwd=nested, check=True, capture_output=True)
    return nested_name


def init_git_worktree_with_gitfile_embedded_repo(
    worktree: Path,
    *,
    nested_name: str = "vendor",
    git_dir_name: str | None = None,
) -> str:
    """
    Create an untracked nested Git repository whose `.git` entry points to a separate Git directory.
    
    Parameters:
    	worktree (Path): The worktree containing the nested repository.
    	nested_name (str): Name of the nested repository directory.
    	git_dir_name (str | None): Name of the separate Git directory. Defaults to a name derived from `nested_name`.
    
    Returns:
    	str: The nested repository directory name.
    """
    init_git_worktree(worktree)
    nested = worktree / nested_name
    nested.mkdir()
    git_dir = worktree / (git_dir_name or f".{nested_name}_git")
    subprocess.run(
        ["git", "init", "--separate-git-dir", str(git_dir), str(nested)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    (nested / "inner.txt").write_text("inner\n", encoding="utf-8")
    subprocess.run(["git", "add", "inner.txt"], cwd=nested, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "nested init"], cwd=nested, check=True, capture_output=True
    )
    return nested_name


def init_git_worktree_with_gitfile_inside_outer_git(
    worktree: Path,
    *,
    outer_name: str = "vendor",
    inner_name: str = "sub",
) -> tuple[str, str]:
    """
    Create nested Git repositories with the inner repository stored under the outer repository's Git directory.
    
    Parameters:
    	outer_name (str): Directory name for the outer repository.
    	inner_name (str): Directory name for the inner repository.
    
    Returns:
    	tuple[str, str]: The outer and inner directory names.
    """
    init_git_worktree(worktree)
    outer = worktree / outer_name
    outer.mkdir()
    subprocess.run(["git", "init"], cwd=outer, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=outer,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=outer,
        check=True,
        capture_output=True,
    )
    (outer / "outer.txt").write_text("outer\n", encoding="utf-8")
    subprocess.run(["git", "add", "outer.txt"], cwd=outer, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "outer init"],
        cwd=outer,
        check=True,
        capture_output=True,
    )

    inner = outer / inner_name
    inner.mkdir()
    modules_git = outer / ".git" / "modules" / inner_name
    modules_git.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--separate-git-dir", str(modules_git), str(inner)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=inner,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=inner,
        check=True,
        capture_output=True,
    )
    (inner / "inner.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "inner.txt"], cwd=inner, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "inner init"],
        cwd=inner,
        check=True,
        capture_output=True,
    )
    return outer_name, inner_name


def wire_outer_linked_mirror(
    worktree: Path,
    *,
    mirrors_common: Path,
) -> Path:
    """
    Register a worktree as a linked worktree of a shared Git directory.
    
    Parameters:
        worktree (Path): Worktree whose Git metadata entry is replaced.
        mirrors_common (Path): Shared Git directory for the linked worktree.
    
    Returns:
        Path: Path to the linked worktree metadata directory.
    """
    linked = mirrors_common / "worktrees" / worktree.name
    linked.mkdir(parents=True, exist_ok=True)
    (linked / "commondir").write_text(f"{mirrors_common.resolve()}\n", encoding="utf-8")
    (linked / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {linked}\n", encoding="utf-8")
    return linked
