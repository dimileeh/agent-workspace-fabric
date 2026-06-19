"""Unit tests for wildcard-ignored empty directory handling in validation worktrees."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

import awf.runtime.validation_worktree as validation_worktree
from awf.runtime.validation_worktree import (
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
    """Minimal command-result stand-in for status command assertions."""

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


def test_remove_empty_untracked_dirs_preserves_wildcard_ignored_empty_dir_when_check_ignore_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing check-ignore probe must not be treated as "not ignored".

    Regression for PR #606 review thread PRRT_kwDOSJAM6s6KIgh1: when
    ``git check-ignore`` fails with a non-zero, non-1 exit, the helper must not
    conclude the directory is unignored and remove it.
    """
    worktree = _init_real_worktree_with_gitignore(tmp_path)
    ignored_dir = worktree / "cache"
    ignored_dir.mkdir()

    original_run = subprocess.run

    def fail_check_ignore(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "check-ignore" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=128, stdout="", stderr="check-ignore exploded"
            )
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_check_ignore)

    with pytest.raises(validation_worktree._IgnoreCheckError):
        validation_worktree._remove_empty_untracked_dirs(
            worktree_path=worktree,
            ignored_paths=(),
        )

    assert ignored_dir.exists()


@pytest.mark.unit
def test_snapshot_empty_untracked_dirs_preserves_wildcard_ignored_empty_dir_when_check_ignore_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing check-ignore probe must not surface a wildcard-ignored empty dir as dirty."""
    worktree = _init_real_worktree_with_gitignore(tmp_path)
    ignored_dir = worktree / "cache"
    ignored_dir.mkdir()

    original_run = subprocess.run

    def fail_check_ignore(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "check-ignore" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=128, stdout="", stderr="check-ignore exploded"
            )
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_check_ignore)

    with pytest.raises(validation_worktree._IgnoreCheckError):
        validation_worktree._snapshot_empty_untracked_dirs(
            worktree_path=worktree,
            ignored_paths=(),
            ignore_check_ignored_empty_dirs=True,
        )

    assert ignored_dir.exists()


@pytest.mark.unit
async def test_check_validation_worktree_clean_fails_when_check_ignore_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing check-ignore probe must make the clean check fail, not lie clean."""
    worktree = _init_real_worktree_with_gitignore(tmp_path)
    ignored_dir = worktree / "cache"
    ignored_dir.mkdir()

    original_run = subprocess.run

    def fail_check_ignore(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "check-ignore" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=128, stdout="", stderr="check-ignore exploded"
            )
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_check_ignore)

    check = await check_validation_worktree_clean(
        run_git=_run_git_in_real_worktree(worktree),
        worktree_path=worktree,
        ignore_all_ignored=True,
        remove_empty_untracked_dirs=True,
    )

    assert check.clean is False
    assert check.reason_code == VALIDATION_WORKTREE_STATUS_FAILED
    assert "check-ignore" in check.message.lower()
    assert check.command_stderr == "check-ignore exploded"
    assert ignored_dir.exists()


@pytest.mark.unit
def _init_real_worktree_with_gitignore(
    tmp_path: Path,
    gitignore_content: str = "cache/**\n",
    submodule_name: str = "sub",
) -> Path:
    """Create a real git worktree with a committed .gitignore file.

    Tests that need gitignore rules to be evaluated by ``git check-ignore``
    require an actual worktree (not a fake ``.git`` pointer) because ignore
    rules are read from the worktree's index and working tree.
    """
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
    (worktree / ".gitignore").write_text(gitignore_content, encoding="utf-8")
    _run_real_git(worktree, "add", ".gitignore")
    _run_real_git(worktree, "commit", "-m", "add gitignore")
    return worktree


@pytest.mark.unit
def test_remove_empty_untracked_dirs_preserves_dash_prefixed_wildcard_ignored_empty_dir(
    tmp_path: Path,
) -> None:
    """Dash-prefixed empty directories ignored by wildcards must be preserved.

    Regression for PR #606 review thread PRRT_kwDOSJAM6s6KIlX8: without ``--``
    before the pathname, ``git check-ignore --no-index -cache/`` treats the
    dash-prefixed path as an option and exits 129, causing the directory to be
    removed and the worktree to be reported as clean.
    """
    worktree = _init_real_worktree_with_gitignore(tmp_path, gitignore_content="-cache/**\n")
    ignored_dir = worktree / "-cache"
    ignored_dir.mkdir()

    removed = validation_worktree._remove_empty_untracked_dirs(
        worktree_path=worktree,
        ignored_paths=(),
    )

    assert sorted(removed) == []
    assert ignored_dir.exists()
    status = _run_real_git(
        worktree,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--ignored=matching",
    )
    assert status.stdout == ""


@pytest.mark.unit
def test_snapshot_empty_untracked_dirs_preserves_dash_prefixed_wildcard_ignored_empty_dir(
    tmp_path: Path,
) -> None:
    """Opted-in wildcard ignore checks must handle dash-prefixed empty directories."""
    worktree = _init_real_worktree_with_gitignore(tmp_path, gitignore_content="-cache/**\n")
    ignored_dir = worktree / "-cache"
    ignored_dir.mkdir()

    empty_dirs = validation_worktree._snapshot_empty_untracked_dirs(
        worktree_path=worktree,
        ignored_paths=(),
        ignore_check_ignored_empty_dirs=True,
    )

    assert sorted(empty_dirs) == []
    assert ignored_dir.exists()


@pytest.mark.unit
async def test_check_validation_worktree_clean_preserves_dash_prefixed_wildcard_ignored_empty_dir(
    tmp_path: Path,
) -> None:
    """Pre-push guard treats a dash-prefixed wildcard-ignored empty dir as clean."""
    worktree = _init_real_worktree_with_gitignore(tmp_path, gitignore_content="-cache/**\n")
    ignored_dir = worktree / "-cache"
    ignored_dir.mkdir()

    check = await check_validation_worktree_clean(
        run_git=_run_git_in_real_worktree(worktree),
        worktree_path=worktree,
        ignore_all_ignored=True,
        remove_empty_untracked_dirs=True,
    )

    assert check.clean is True
    assert check.reason_code is None
    assert check.paths == ()
    assert check.untracked_paths == ()
    assert ignored_dir.exists()


@pytest.mark.unit
def test_remove_empty_untracked_dirs_preserves_wildcard_ignored_empty_dir(
    tmp_path: Path,
) -> None:
    """Empty directories ignored by wildcard rules must not be removed.

    Regression for PR #606 review thread PRRT_kwDOSJAM6s6KH8Na:
    ``git status --ignored=matching`` does not print empty directories that
    match a pattern such as ``cache/**``. Without a direct ``git check-ignore``
    probe, ``_remove_empty_untracked_dirs`` would rmdir the directory and
    report the worktree clean.
    """
    worktree = _init_real_worktree_with_gitignore(tmp_path)
    ignored_dir = worktree / "cache"
    ignored_dir.mkdir()

    removed = validation_worktree._remove_empty_untracked_dirs(
        worktree_path=worktree,
        ignored_paths=(),
    )

    assert sorted(removed) == []
    assert ignored_dir.exists()
    status = _run_real_git(
        worktree,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--ignored=matching",
    )
    assert status.stdout == ""


@pytest.mark.unit
def test_snapshot_empty_untracked_dirs_preserves_wildcard_ignored_empty_dir(
    tmp_path: Path,
) -> None:
    """Opted-in wildcard ignore checks must not surface ignored empty directories."""
    worktree = _init_real_worktree_with_gitignore(tmp_path)
    ignored_dir = worktree / "cache"
    ignored_dir.mkdir()

    empty_dirs = validation_worktree._snapshot_empty_untracked_dirs(
        worktree_path=worktree,
        ignored_paths=(),
        ignore_check_ignored_empty_dirs=True,
    )

    assert sorted(empty_dirs) == []
    assert ignored_dir.exists()


@pytest.mark.unit
async def test_check_validation_worktree_clean_reports_wildcard_ignored_empty_dir_by_default(
    tmp_path: Path,
) -> None:
    """Default clean checks keep ignored empty directories dirty."""
    worktree = _init_real_worktree_with_gitignore(tmp_path)
    ignored_dir = worktree / "cache"
    ignored_dir.mkdir()

    check = await check_validation_worktree_clean(
        run_git=_run_git_in_real_worktree(worktree),
        worktree_path=worktree,
    )

    assert check.clean is False
    assert check.reason_code == validation_worktree.VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert check.paths == ("cache/",)
    assert check.untracked_paths == ("cache/",)
    assert check.ignored_paths == ()
    assert ignored_dir.exists()


@pytest.mark.unit
async def test_check_validation_worktree_clean_ignores_wildcard_ignored_empty_dir_when_opted_in(
    tmp_path: Path,
) -> None:
    """ignore_all_ignored keeps wildcard-ignored empty dirs clean in snapshot mode."""
    worktree = _init_real_worktree_with_gitignore(tmp_path)
    ignored_dir = worktree / "cache"
    ignored_dir.mkdir()

    check = await check_validation_worktree_clean(
        run_git=_run_git_in_real_worktree(worktree),
        worktree_path=worktree,
        ignore_all_ignored=True,
    )

    assert check.clean is True
    assert check.reason_code is None
    assert check.paths == ()
    assert check.untracked_paths == ()
    assert check.ignored_paths == ()
    assert ignored_dir.exists()


@pytest.mark.unit
async def test_check_validation_worktree_clean_preserves_wildcard_ignored_empty_dir(
    tmp_path: Path,
) -> None:
    """Pre-push guard treats a wildcard-ignored empty dir as clean."""
    worktree = _init_real_worktree_with_gitignore(tmp_path)
    ignored_dir = worktree / "cache"
    ignored_dir.mkdir()

    check = await check_validation_worktree_clean(
        run_git=_run_git_in_real_worktree(worktree),
        worktree_path=worktree,
        ignore_all_ignored=True,
        remove_empty_untracked_dirs=True,
    )

    assert check.clean is True
    assert check.reason_code is None
    assert check.paths == ()
    assert check.untracked_paths == ()
    assert ignored_dir.exists()


def _run_git_in_real_worktree(worktree: Path):
    """Return a GitRunner that executes real git commands in ``worktree``."""

    async def run_git(args: list[str]) -> _CommandResultLike:
        result = _run_real_git(worktree, *args)
        return _CommandResultLike(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    return run_git
