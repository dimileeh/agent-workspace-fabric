"""Unit tests for validation worktree cleanup helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    VALIDATION_WORKTREE_CLEANUP_FAILED,
    cleanup_validation_worktree_side_effects,
    check_validation_worktree_clean,
)


@dataclass
class _CommandResultLike:
    returncode: int
    stdout: str | None
    stderr: str | None
    reason_code: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@pytest.mark.unit
async def test_check_validation_worktree_clean_handles_none_stdout_as_clean(tmp_path: Path) -> None:
    """A git status result with ``None`` stdout should behave as a clean worktree."""

    worktree = _init_fake_worktree(tmp_path)

    async def run_git(args: list[str]) -> _CommandResultLike:
        if args == ["status", "--porcelain=v1", "--untracked-files=all"]:
            return _CommandResultLike(0, None, "")
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(run_git=run_git, worktree_path=worktree)

    assert check.clean is True
    assert check.reason_code is None


@pytest.mark.unit
async def test_check_validation_worktree_clean_treats_untracked_paths_as_dirty(tmp_path: Path) -> None:
    """Untracked files are pre-existing dirt and should be rejected by the guard."""

    worktree = _init_fake_worktree(tmp_path)

    async def run_git(args: list[str]) -> _CommandResultLike:
        if args == ["status", "--porcelain=v1", "--untracked-files=all"]:
            return _CommandResultLike(0, "?? untracked.py\n", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(run_git=run_git, worktree_path=worktree)

    assert check.clean is False
    assert check.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert check.paths == ("untracked.py",)
    assert check.untracked_paths == ("untracked.py",)


def _init_fake_worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / ".git").write_text("gitdir: /tmp/fake.git\n", encoding="utf-8")
    return worktree


@pytest.mark.unit
async def test_cleanup_validation_worktree_restores_tracked_files_with_none_stderr(tmp_path: Path) -> None:
    """A failed git restore should not crash if stderr is None."""

    worktree = _init_fake_worktree(tmp_path)

    async def run_git(args: list[str]) -> _CommandResultLike:
        if args == ["status", "--porcelain=v1", "--untracked-files=all"]:
            return _CommandResultLike(0, " M tracked.py\n", "")
        if args[:1] == ["restore"]:
            return _CommandResultLike(1, "", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(run_git=run_git, worktree_path=worktree)

    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert cleanup.cleanup_command == "git restore"
    assert cleanup.cleanup_stderr == ""


@pytest.mark.unit
async def test_cleanup_validation_worktree_cleans_untracked_files_with_none_stderr(tmp_path: Path) -> None:
    """A failed git clean should not crash if stderr is None."""

    worktree = _init_fake_worktree(tmp_path)

    async def run_git(args: list[str]) -> _CommandResultLike:
        if args == ["status", "--porcelain=v1", "--untracked-files=all"]:
            return _CommandResultLike(0, "?? untracked.py\n", "")
        if args[:1] == ["clean"]:
            return _CommandResultLike(1, "", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(run_git=run_git, worktree_path=worktree)

    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert cleanup.cleanup_command == "git clean"
    assert cleanup.cleanup_stderr == ""
