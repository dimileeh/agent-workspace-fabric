"""Worktree activity probe backing the idle watchdog (issue #932).

"Liveness" for a print-mode agent is "the worktree moved", so the probe answers
one question: has anything under the workspace worktree changed since the last
probe? It excludes nothing the agent could legitimately write (``.git``
included) and, because a linked worktree keeps HEAD/index outside the worktree,
it also watches the resolved git dir's ``HEAD`` / ``index`` / ``logs/HEAD``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from awf.adapters.worktree_activity import (
    WorktreeActivityProbe,
    make_worktree_activity_probe,
)


def _age(path: Path, *, seconds: float = 3600.0) -> None:
    """Push ``path``'s mtime into the past so it predates any probe baseline."""
    stat_result = path.stat()
    os.utime(path, (stat_result.st_atime - seconds, stat_result.st_mtime - seconds))


def _age_tree(root: Path) -> None:
    for current, dirnames, filenames in os.walk(root):
        for name in filenames:
            _age(Path(current) / name)
        for name in dirnames:
            _age(Path(current) / name)
    _age(root)


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    root = tmp_path / "ws_probe"
    (root / "src" / "nested").mkdir(parents=True)
    (root / "src" / "nested" / "module.py").write_text("x = 1\n", encoding="utf-8")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _age_tree(root)
    return root


@pytest.mark.unit
async def test_quiet_worktree_reports_no_activity(worktree: Path) -> None:
    probe = WorktreeActivityProbe(worktree)

    assert await probe() is False
    assert await probe() is False


@pytest.mark.unit
async def test_modified_file_reports_activity_once(worktree: Path) -> None:
    """A single change is reported once; the baseline then advances."""
    probe = WorktreeActivityProbe(worktree)
    assert await probe() is False

    (worktree / "README.md").write_text("hello again\n", encoding="utf-8")

    assert await probe() is True
    assert await probe() is False


@pytest.mark.unit
async def test_created_file_in_nested_directory_reports_activity(worktree: Path) -> None:
    probe = WorktreeActivityProbe(worktree)
    assert await probe() is False

    (worktree / "src" / "nested" / "new_module.py").write_text("y = 2\n", encoding="utf-8")

    assert await probe() is True


@pytest.mark.unit
async def test_deleted_file_reports_activity(worktree: Path) -> None:
    """A delete only bumps the containing directory's mtime — dirs are stat-ed too."""
    probe = WorktreeActivityProbe(worktree)
    assert await probe() is False

    (worktree / "src" / "nested" / "module.py").unlink()

    assert await probe() is True


@pytest.mark.unit
async def test_linked_worktree_git_dir_head_and_index_are_watched(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """Only Git state moved: the gitfile-resolved git dir must still count."""
    git_dir = tmp_path / "mirror.git" / "worktrees" / "ws_probe"
    (git_dir / "logs").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/awf/ws\n", encoding="utf-8")
    (git_dir / "index").write_bytes(b"DIRC")
    (git_dir / "logs" / "HEAD").write_text("reflog\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(git_dir)

    probe = WorktreeActivityProbe(worktree)
    assert await probe() is False

    (git_dir / "index").write_bytes(b"DIRC-updated")
    assert await probe() is True

    (git_dir / "HEAD").write_text("ref: refs/heads/other\n", encoding="utf-8")
    assert await probe() is True

    (git_dir / "logs" / "HEAD").write_text("reflog\nmore\n", encoding="utf-8")
    assert await probe() is True


@pytest.mark.unit
async def test_relative_gitdir_pointer_resolves_against_the_worktree(
    tmp_path: Path,
    worktree: Path,
) -> None:
    git_dir = tmp_path / "relative.git"
    git_dir.mkdir()
    (git_dir / "index").write_bytes(b"DIRC")
    relative = os.path.relpath(git_dir, worktree)
    (worktree / ".git").write_text(f"gitdir: {relative}\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(git_dir)

    probe = WorktreeActivityProbe(worktree)
    assert await probe() is False

    (git_dir / "index").write_bytes(b"DIRC-updated")
    assert await probe() is True


@pytest.mark.unit
@pytest.mark.parametrize("gitfile_body", ["", "gitdir:\n", "not a gitfile\n"])
async def test_unusable_gitfile_falls_back_to_the_worktree_walk(
    worktree: Path,
    gitfile_body: str,
) -> None:
    (worktree / ".git").write_text(gitfile_body, encoding="utf-8")
    _age_tree(worktree)

    probe = WorktreeActivityProbe(worktree)
    assert await probe() is False

    (worktree / "README.md").write_text("changed\n", encoding="utf-8")
    assert await probe() is True


@pytest.mark.unit
async def test_git_directory_is_walked_like_any_other_path(worktree: Path) -> None:
    """A real ``.git`` directory is not excluded — the agent may write there."""
    git_dir = worktree / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/awf/ws\n", encoding="utf-8")
    _age_tree(worktree)

    probe = WorktreeActivityProbe(worktree)
    assert await probe() is False

    (git_dir / "HEAD").write_text("ref: refs/heads/other\n", encoding="utf-8")
    assert await probe() is True


@pytest.mark.unit
async def test_entry_budget_stops_the_walk(worktree: Path) -> None:
    """A bounded walk gives up rather than scanning an unbounded tree."""
    probe = WorktreeActivityProbe(worktree, max_entries=1)
    assert await probe() is False

    for index in range(5):
        (worktree / "src" / "nested" / f"extra_{index}.py").write_text("z\n", encoding="utf-8")

    # The budget is exhausted before every entry is inspected, so the probe
    # fails closed (no activity) instead of scanning without bound.
    assert await probe() is False


@pytest.mark.unit
async def test_unreadable_entries_are_skipped_not_raised(
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_scandir = os.scandir

    class _RaisingEntry:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self.path = entry.path
            self.name = entry.name

        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            del follow_symlinks
            raise PermissionError("stat denied")

        def is_dir(self, *, follow_symlinks: bool = True) -> bool:
            del follow_symlinks
            return False

    class _PartiallyBrokenScandir:
        def __init__(self, path: str) -> None:
            self._inner = real_scandir(path)

        def __enter__(self) -> list[object]:
            return [_RaisingEntry(entry) for entry in self._inner]

        def __exit__(self, *_exc: object) -> None:
            self._inner.close()

    monkeypatch.setattr(os, "scandir", _PartiallyBrokenScandir)

    probe = WorktreeActivityProbe(worktree)
    assert await probe() is False


@pytest.mark.unit
async def test_unreadable_directory_is_skipped(
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _deny(_path: str) -> object:
        raise PermissionError("scandir denied")

    monkeypatch.setattr(os, "scandir", _deny)

    probe = WorktreeActivityProbe(worktree)
    assert await probe() is False


@pytest.mark.unit
def test_make_probe_returns_none_without_a_usable_path(tmp_path: Path) -> None:
    assert make_worktree_activity_probe(None) is None
    assert make_worktree_activity_probe(tmp_path / "missing") is None
    assert make_worktree_activity_probe(tmp_path) is not None
