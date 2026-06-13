"""Tests for AWF-agent-runtime ``.claude/agent-memory/`` dirty-tree suppression.

Reviewer subagents (bug-hunter/bug-validator/bug-judge) write their memory into
``.claude/agent-memory/<agent>/`` inside the validation worktree. Those untracked
artifacts are never part of the PR, so the dirty-tree guard must treat them as
clean — unconditionally, without depending on the target repo's ``.gitignore``.
Tracked memory (some users commit theirs) must stay fully visible. See #573.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf.common.commands import CommandResult
from awf.runtime.validation_worktree import check_validation_worktree_clean


def _git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a real git command in a temporary worktree."""
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_real_worktree(tmp_path: Path) -> Path:
    """Create a real git repo with a single tracked commit."""
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    _git(worktree, "init", "-q")
    _git(worktree, "config", "user.email", "awf@example.com")
    _git(worktree, "config", "user.name", "AWF Test")
    (worktree / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    _git(worktree, "add", "tracked.py")
    _git(worktree, "commit", "-qm", "initial")
    return worktree


def _real_run_git(worktree: Path):
    """Return an async ``run_git`` bound to a real worktree."""

    async def run_git(args: list[str]) -> CommandResult:
        proc = subprocess.run(
            ["git", "-C", str(worktree), *args],
            capture_output=True,
            text=True,
        )
        return CommandResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    return run_git


def _write(worktree: Path, relative: str, content: str = "memory\n") -> None:
    """Create a file (and parents) under the worktree."""
    target = worktree / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


@pytest.mark.unit
async def test_untracked_agent_memory_only_is_clean(tmp_path: Path) -> None:
    """Untracked agent-memory alone makes the tree clean (the #573 fix).

    Runs with the default ``ignore_all_ignored=False`` to prove the suppression
    is unconditional and not gated on that flag.
    """
    worktree = _init_real_worktree(tmp_path)
    _write(worktree, ".claude/agent-memory/bug-validator/notes.md")
    run_git = _real_run_git(worktree)

    check = await check_validation_worktree_clean(run_git=run_git, worktree_path=worktree)

    assert check.clean is True
    assert check.paths == ()
    assert check.untracked_paths == ()


@pytest.mark.unit
async def test_untracked_agent_memory_plus_real_file_reports_only_real(tmp_path: Path) -> None:
    """A real untracked artifact still trips the guard; memory is suppressed."""
    worktree = _init_real_worktree(tmp_path)
    _write(worktree, ".claude/agent-memory/bug-hunter/x.md")
    _write(worktree, "src/foo.py", "print('x')\n")
    run_git = _real_run_git(worktree)

    check = await check_validation_worktree_clean(run_git=run_git, worktree_path=worktree)

    assert check.clean is False
    assert "src/foo.py" in check.untracked_paths
    assert not any("agent-memory" in path for path in check.paths)
    assert not any("agent-memory" in path for path in check.untracked_paths)


@pytest.mark.unit
async def test_tracked_modified_agent_memory_still_dirty(tmp_path: Path) -> None:
    """Tracked (committed) memory that is modified stays visible/blocking."""
    worktree = _init_real_worktree(tmp_path)
    _write(worktree, ".claude/agent-memory/notes.md", "original\n")
    _git(worktree, "add", ".claude/agent-memory/notes.md")
    _git(worktree, "commit", "-qm", "commit memory")
    (worktree / ".claude" / "agent-memory" / "notes.md").write_text("modified\n", encoding="utf-8")
    run_git = _real_run_git(worktree)

    check = await check_validation_worktree_clean(run_git=run_git, worktree_path=worktree)

    assert check.clean is False
    assert ".claude/agent-memory/notes.md" in check.paths


@pytest.mark.unit
async def test_sibling_prefix_directory_is_not_suppressed(tmp_path: Path) -> None:
    """``.claude/agent-memory-archive/`` shares a prefix but is a different root."""
    worktree = _init_real_worktree(tmp_path)
    _write(worktree, ".claude/agent-memory-archive/x.md")
    run_git = _real_run_git(worktree)

    check = await check_validation_worktree_clean(run_git=run_git, worktree_path=worktree)

    assert check.clean is False
    assert ".claude/agent-memory-archive/x.md" in check.untracked_paths


@pytest.mark.unit
async def test_nested_agent_memory_paths_all_suppressed(tmp_path: Path) -> None:
    """Deeply nested untracked memory paths are all treated as clean."""
    worktree = _init_real_worktree(tmp_path)
    _write(worktree, ".claude/agent-memory/bug-hunter/notes.md")
    _write(worktree, ".claude/agent-memory/bug-judge/a/b.md")
    run_git = _real_run_git(worktree)

    check = await check_validation_worktree_clean(run_git=run_git, worktree_path=worktree)

    assert check.clean is True
    assert check.paths == ()
    assert check.untracked_paths == ()


@pytest.mark.unit
async def test_empty_untracked_agent_memory_dir_is_clean(tmp_path: Path) -> None:
    """An empty agent-memory directory (no file yet) is still suppressed.

    The agent runtime can create ``.claude/agent-memory/<agent>/`` before it
    writes any file. git status omits empty directories, so the empty-dir
    snapshot is what surfaces them — and it must honour the AWF runtime roots,
    or the parents (``.claude/agent-memory/``, ``.claude/``) get reported dirty.
    """
    worktree = _init_real_worktree(tmp_path)
    (worktree / ".claude" / "agent-memory" / "bug-hunter").mkdir(parents=True)
    run_git = _real_run_git(worktree)

    check = await check_validation_worktree_clean(run_git=run_git, worktree_path=worktree)

    assert check.clean is True
    assert check.paths == ()
    assert check.untracked_paths == ()


@pytest.mark.unit
async def test_gitignored_agent_memory_unchanged(tmp_path: Path) -> None:
    """A target repo that already gitignores agent-memory keeps working."""
    worktree = _init_real_worktree(tmp_path)
    (worktree / ".gitignore").write_text(".claude/agent-memory/\n", encoding="utf-8")
    _git(worktree, "add", ".gitignore")
    _git(worktree, "commit", "-qm", "ignore agent memory")
    _write(worktree, ".claude/agent-memory/bug-validator/notes.md")
    run_git = _real_run_git(worktree)

    check = await check_validation_worktree_clean(
        run_git=run_git, worktree_path=worktree, ignore_all_ignored=True
    )

    assert check.clean is True
