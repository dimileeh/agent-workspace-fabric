"""Focused coverage for race-safe validation worktree directory cleanup."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.runtime import validation_worktree


@pytest.mark.unit
def test_is_directory_returns_false_when_lstat_fails(tmp_path: Path) -> None:
    """A path that disappears before lstat is not treated as a cleanup directory."""

    assert validation_worktree._is_directory(tmp_path / "missing") is False


@pytest.mark.unit
def test_is_tracked_gitlink_rejects_paths_outside_worktree(tmp_path: Path) -> None:
    """Gitlink checks never invoke Git for paths outside the validation worktree."""

    worktree = tmp_path / "worktree"
    worktree.mkdir()

    assert validation_worktree._is_tracked_gitlink(worktree, tmp_path / "sibling") is False


def _stub_git_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(validation_worktree, "_gitlink_paths", lambda _root: frozenset())
    monkeypatch.setattr(
        validation_worktree,
        "_ignored_paths",
        lambda _root, _paths: frozenset(),
    )


@pytest.mark.unit
def test_remove_empty_untracked_dirs_tolerates_directory_scan_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent directory read failure leaves the worktree untouched."""

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _stub_git_helpers(monkeypatch)
    real_iterdir = Path.iterdir

    def _iterdir(path: Path):  # type: ignore[no-untyped-def]
        if path == worktree:
            raise OSError("directory disappeared")
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", _iterdir)

    assert (
        validation_worktree._remove_empty_untracked_dirs(
            worktree_path=worktree,
            ignored_paths=(),
        )
        == ()
    )


@pytest.mark.unit
def test_remove_empty_untracked_dirs_ignores_child_outside_relative_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child that cannot be relativized is treated as a traversal boundary."""

    worktree = tmp_path / "worktree"
    child = worktree / "child"
    child.mkdir(parents=True)
    _stub_git_helpers(monkeypatch)
    real_relative_to = Path.relative_to

    def _relative_to(path: Path, other: Path, *extra: object) -> Path:
        if path == child:
            raise ValueError("outside boundary")
        return real_relative_to(path, other, *extra)

    monkeypatch.setattr(Path, "relative_to", _relative_to)

    assert (
        validation_worktree._remove_empty_untracked_dirs(
            worktree_path=worktree,
            ignored_paths=(),
        )
        == ()
    )


@pytest.mark.unit
def test_remove_empty_untracked_dirs_tolerates_candidate_relativize_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directory moved after traversal is omitted from cleanup candidates."""

    worktree = tmp_path / "worktree"
    child = worktree / "child"
    child.mkdir(parents=True)
    _stub_git_helpers(monkeypatch)
    real_relative_to = Path.relative_to
    child_calls = 0

    def _relative_to(path: Path, other: Path, *extra: object) -> Path:
        nonlocal child_calls
        if path == child:
            child_calls += 1
            if child_calls == 2:
                raise ValueError("moved during traversal")
        return real_relative_to(path, other, *extra)

    monkeypatch.setattr(Path, "relative_to", _relative_to)

    assert (
        validation_worktree._remove_empty_untracked_dirs(
            worktree_path=worktree,
            ignored_paths=(),
        )
        == ()
    )


@pytest.mark.unit
def test_remove_empty_untracked_dirs_tolerates_remove_races(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """File-not-found and non-empty races do not fail empty-directory cleanup."""

    worktree = tmp_path / "worktree"
    child = worktree / "child"
    child.mkdir(parents=True)
    _stub_git_helpers(monkeypatch)
    real_rmdir = Path.rmdir

    def _rmdir(path: Path) -> None:
        if path == child:
            raise FileNotFoundError("already removed")
        real_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", _rmdir)

    assert (
        validation_worktree._remove_empty_untracked_dirs(
            worktree_path=worktree,
            ignored_paths=(),
        )
        == ()
    )
