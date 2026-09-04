"""Status-edge coverage for validation worktree clean checks."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import awf.runtime.validation_worktree as validation_worktree
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    check_validation_worktree_clean,
)
from tests.unit.runtime.test_validation_worktree import (
    _VALIDATION_STATUS_ARGS,
    _CommandResultLike,
    _core_symlinks_get_result,
    _init_fake_worktree,
    _run_real_git,
)


@pytest.mark.unit
async def test_check_validation_worktree_clean_treats_clean_unborn_head_as_clean(
    tmp_path: Path,
) -> None:
    """A clean repository with unborn HEAD has no tracked gitlinks to enumerate."""
    worktree = tmp_path / "unborn-worktree"
    worktree.mkdir(parents=True)
    subprocess.run(
        ["git", "init", str(worktree)],
        check=True,
        capture_output=True,
        text=True,
    )

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate Git status succeeding with no tracked or untracked paths."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "", None)
        handled = _core_symlinks_get_result(args)
        if handled is not None:
            return handled
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(run_git=run_git, worktree_path=worktree)

    assert check.clean is True
    assert check.reason_code is None


@pytest.mark.unit
def test_remove_empty_untracked_dirs_treats_worktree_git_dir_as_boundary(
    tmp_path: Path,
) -> None:
    """The worktree's own `.git` directory must never be removed or reported.

    A real git repository creates an empty `.git/branches/`, `.git/objects/pack/`,
    `.git/objects/info/`, and `.git/refs/tags/` immediately after ``git init``.
    These are part of git's internal machinery, not untracked side effects, and
    must not be surfaced as dirty by empty-directory cleanup or snapshot logic.
    """
    worktree = tmp_path / "real-worktree"
    worktree.mkdir(parents=True)
    subprocess.run(
        ["git", "init", str(worktree)],
        check=True,
        capture_output=True,
        text=True,
    )
    _run_real_git(worktree, "config", "user.email", "agent@example.com")
    _run_real_git(worktree, "config", "user.name", "AWF Agent")
    # ``ls-tree HEAD`` requires an actual commit to resolve HEAD. An unborn
    # branch makes git fail with "Not a valid object name HEAD".
    _run_real_git(worktree, "commit", "--allow-empty", "-m", "init")
    plain_empty_dir = worktree / "generated"
    plain_empty_dir.mkdir()

    removed = validation_worktree._remove_empty_untracked_dirs(
        worktree_path=worktree,
        ignored_paths=(),
    )

    assert sorted(removed) == ["generated/"]
    assert not plain_empty_dir.exists()
    assert (worktree / ".git").exists()


@pytest.mark.unit
def test_snapshot_empty_untracked_dirs_treats_worktree_git_dir_as_boundary(
    tmp_path: Path,
) -> None:
    """The worktree's own `.git` directory must not expose empty internal dirs."""
    worktree = tmp_path / "real-worktree"
    worktree.mkdir(parents=True)
    subprocess.run(
        ["git", "init", str(worktree)],
        check=True,
        capture_output=True,
        text=True,
    )
    _run_real_git(worktree, "config", "user.email", "agent@example.com")
    _run_real_git(worktree, "config", "user.name", "AWF Agent")
    # ``ls-tree HEAD`` requires an actual commit to resolve HEAD. An unborn
    # branch makes git fail with "Not a valid object name HEAD".
    _run_real_git(worktree, "commit", "--allow-empty", "-m", "init")
    plain_empty_dir = worktree / "generated"
    plain_empty_dir.mkdir()

    empty_dirs = validation_worktree._snapshot_empty_untracked_dirs(
        worktree_path=worktree,
        ignored_paths=(),
    )

    assert sorted(empty_dirs) == ["generated/"]
    assert (worktree / ".git").exists()


@pytest.mark.unit
async def test_check_validation_worktree_clean_reports_tracked_path_under_ignored_root(
    tmp_path: Path,
) -> None:
    """Tracked edits inside ignored roots must not be hidden as ignored setup state."""
    worktree = _init_fake_worktree(tmp_path)

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a tracked edit below an ignored root."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, " M .venv/tracked.py\n!! .venv/\n", None)
        handled = _core_symlinks_get_result(args)
        if handled is not None:
            return handled
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(
        run_git=run_git,
        worktree_path=worktree,
        ignore_all_ignored=True,
    )

    assert check.clean is False
    assert check.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert check.paths == (".venv/tracked.py",)
    assert check.untracked_paths == ()
    assert check.tracked_paths == (".venv/tracked.py",)
    assert check.ignored_paths == (".venv/",)


@pytest.mark.unit
def test_repo_gitignore_ignores_plan_artifacts_at_any_depth(tmp_path: Path) -> None:
    """The repo ``.gitignore`` ignores plan artifacts at any depth, keeping the README (#620).

    Regression for the recurring dirty-tree failure: the root-anchored
    ``/docs/awf-plans/*`` rule missed a nested ``apps/console/docs/awf-plans/``
    copy. The de-anchored ``**/docs/awf-plans/*`` rule must ignore root AND
    nested artifacts while still tracking the canonical
    ``docs/awf-plans/README.md`` and leaving the sibling
    ``docs/awf-plans-archive`` untouched. Asserts against the real repo-root
    ``.gitignore`` so the rule itself is covered.
    """
    repo_gitignore = Path(__file__).resolve().parents[3] / ".gitignore"
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    _run_real_git(repo, "config", "user.email", "agent@example.com")
    _run_real_git(repo, "config", "user.name", "AWF Agent")
    (repo / ".gitignore").write_text(
        repo_gitignore.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    for relative in (
        "docs/awf-plans/ws_root.md",
        "docs/awf-plans/README.md",
        "apps/console/docs/awf-plans/ws_nested.md",
        "deep/a/b/docs/awf-plans/x.json",
        "docs/awf-plans-archive/keep.md",
    ):
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")

    def is_ignored(relative: str) -> bool:
        """Return whether the real repo `.gitignore` ignores ``relative``."""
        result = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "-q", relative],
            capture_output=True,
        )
        return result.returncode == 0

    # (a) nested artifact ignored at any depth, (b) root artifact ignored.
    assert is_ignored("apps/console/docs/awf-plans/ws_nested.md")
    assert is_ignored("deep/a/b/docs/awf-plans/x.json")
    assert is_ignored("docs/awf-plans/ws_root.md")
    # (c) README stays tracked; the sibling archive dir is not over-matched.
    assert not is_ignored("docs/awf-plans/README.md")
    assert not is_ignored("docs/awf-plans-archive/keep.md")

    _run_real_git(repo, "add", "-A")
    staged = _run_real_git(repo, "diff", "--cached", "--name-only").stdout.split()
    assert "docs/awf-plans/README.md" in staged
    assert "docs/awf-plans-archive/keep.md" in staged
    assert "docs/awf-plans/ws_root.md" not in staged
    assert "apps/console/docs/awf-plans/ws_nested.md" not in staged
    assert "deep/a/b/docs/awf-plans/x.json" not in staged
