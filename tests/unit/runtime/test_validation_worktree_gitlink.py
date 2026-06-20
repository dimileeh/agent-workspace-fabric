"""Real-git gitlink/submodule coverage for validation worktree cleanup.

Split out of ``test_validation_worktree.py`` to keep each test module within the
first-party line-length guardrail. These tests drive real ``git`` subprocesses
(deinitialized submodules, non-UTF-8 gitlinks) rather than the fake command
runner doubles used by the sibling module.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

import awf.runtime.validation_worktree as validation_worktree
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    VALIDATION_WORKTREE_STATUS_FAILED,
    check_validation_worktree_clean,
)

_VALIDATION_STATUS_ARGS = (
    "status",
    "--porcelain=v1",
    "--untracked-files=all",
    "--ignored=matching",
)


@dataclass
class _CommandResultLike:
    """Minimal command-result stand-in for status/revert command assertions."""

    returncode: int
    stdout: str | None
    stderr: str | None
    reason_code: str | None = None

    @property
    def ok(self) -> bool:
        """Return whether the simulated command completed successfully."""
        return self.returncode == 0


def _run_real_git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a real Git command in a temporary test worktree."""
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        check=True,
        capture_output=True,
        text=True,
        errors="surrogateescape",
    )


def _init_worktree_with_deinitialized_submodule(
    tmp_path: Path, *, submodule_name: str = "sub"
) -> Path:
    """Create a real git worktree that contains a deinitialized submodule."""
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True)
    subprocess.run(
        ["git", "init", str(worktree)],
        check=True,
        capture_output=True,
        text=True,
    )
    _run_real_git(worktree, "config", "user.email", "agent@example.com")
    _run_real_git(worktree, "config", "user.name", "AWF Agent")
    submodule = worktree / submodule_name
    submodule.mkdir()
    (submodule / ".git").mkdir()
    (submodule / "file.txt").write_text("x\n", encoding="utf-8")
    _run_real_git(submodule, "init")
    _run_real_git(submodule, "config", "user.email", "agent@example.com")
    _run_real_git(submodule, "config", "user.name", "AWF Agent")
    _run_real_git(submodule, "add", "file.txt")
    _run_real_git(submodule, "commit", "-m", "init")
    _run_real_git(worktree, "submodule", "add", f"./{submodule_name}", submodule_name)
    _run_real_git(worktree, "commit", "-m", "add sub")
    _run_real_git(worktree, "submodule", "deinit", "-f", submodule_name)
    assert not (submodule / ".git").exists()
    assert not any(submodule.iterdir())
    return worktree


@pytest.mark.unit
async def test_check_validation_worktree_clean_fails_when_gitlink_lookup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed gitlink enumeration must make the clean check fail, not lie clean.

    Regression for PR #606 review thread PRRT_kwDOSJAM6s6KHcyk: if ``git ls-tree``
    fails, ``_gitlink_paths`` used to return an empty set. With no gitlink
    boundary, ``_remove_empty_untracked_dirs`` would rmdir a deinitialized
    tracked submodule directory, and the cleanliness decision (made from the
    pre-removal ``git status`` output) would falsely report the tree as clean.
    """
    worktree = _init_worktree_with_deinitialized_submodule(tmp_path)
    submodule = worktree / "sub"

    original_run = subprocess.run

    def fail_ls_tree(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "ls-tree" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=128, stdout="", stderr="ls-tree exploded"
            )
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_ls_tree)

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a clean git status output."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(
        run_git=run_git,
        worktree_path=worktree,
        ignore_all_ignored=True,
        remove_empty_untracked_dirs=True,
    )

    assert check.clean is False
    assert check.reason_code == VALIDATION_WORKTREE_STATUS_FAILED
    assert "gitlink" in check.message.lower()
    assert check.command_stderr == "ls-tree exploded"
    assert submodule.exists()


@pytest.mark.unit
async def test_check_validation_worktree_clean_preserves_dirty_paths_when_gitlink_lookup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gitlink failure with pre-existing dirty paths reports those paths.

    Regression for PR #606 review thread PRRT_kwDOSJAM6s6KHy0I: when
    ``remove_empty_untracked_dirs`` is false and ``_gitlink_paths`` fails, the
    pre-existing dirty paths from ``git status`` must be preserved with
    ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` instead of being replaced by the
    infrastructure ``VALIDATION_WORKTREE_STATUS_FAILED`` failure.
    """
    worktree = _init_worktree_with_deinitialized_submodule(tmp_path)

    original_run = subprocess.run

    def fail_ls_tree(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "ls-tree" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=128, stdout="", stderr="ls-tree exploded"
            )
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_ls_tree)

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a status command reporting a tracked modification."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, " M tracked.py\n", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(
        run_git=run_git,
        worktree_path=worktree,
        ignore_all_ignored=True,
        remove_empty_untracked_dirs=False,
    )

    assert check.clean is False
    assert check.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert "tracked.py" in check.paths


@pytest.mark.unit
def test_gitlink_paths_tolerates_non_utf8_tracked_paths(
    tmp_path: Path,
) -> None:
    """Non-UTF-8 tracked paths must not crash gitlink enumeration.

    Regression for PR #606 review thread PRRT_kwDOSJAM6s6KIO_4:
    ``git ls-tree -z`` emits raw path bytes; decoding them strictly with
    ``subprocess.run(..., text=True)`` raises ``UnicodeDecodeError`` before
    the _GitlinkLookupError path can run. The empty-directory cleanup then
    has no gitlink boundary and may remove tracked directories.
    """
    worktree = _init_worktree_with_deinitialized_submodule(tmp_path, submodule_name="\udcffsub")
    submodule = worktree / "\udcffsub"
    plain_empty_dir = worktree / "generated"
    plain_empty_dir.mkdir()

    gitlink_paths = validation_worktree._gitlink_paths(worktree)

    assert "\udcffsub" in gitlink_paths
    removed = validation_worktree._remove_empty_untracked_dirs(
        worktree_path=worktree,
        ignored_paths=(),
    )

    assert sorted(removed) == ["generated/"]
    assert submodule.exists()
    assert not plain_empty_dir.exists()
