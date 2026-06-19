"""Focused ownership and stale-worktree GitManager edge tests."""

from __future__ import annotations

from pathlib import Path

import pytest

import awf.node.git_manager as git_manager
from awf.node.git_manager import GitManager


@pytest.mark.unit
def test_chown_tree_skips_symlink_targets_using_lchown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target"
    target.write_text("target", encoding="utf-8")
    root = tmp_path / "symlink-root"
    root.symlink_to(target)
    linked_target = tmp_path / "linked-target"
    linked_target.write_text("linked-target", encoding="utf-8")
    directory = tmp_path / "worktree"
    directory.mkdir()
    linked_child = directory / "linked-child"
    linked_child.symlink_to(linked_target)
    linked_directory_target = tmp_path / "linked-directory-target"
    linked_directory_target.mkdir()
    linked_child_directory = directory / "linked-child-directory"
    linked_child_directory.symlink_to(linked_directory_target, target_is_directory=True)
    child_file = directory / "file"
    child_file.write_text("file", encoding="utf-8")
    chowned: list[Path] = []
    lchowned: list[Path] = []

    def _record_chown(path: str | bytes, _uid: int, _gid: int) -> None:
        del _uid, _gid
        chowned.append(Path(path))

    def _record_lchown(path: str | bytes, _uid: int, _gid: int) -> None:
        del _uid, _gid
        lchowned.append(Path(path))

    monkeypatch.setattr(git_manager.os, "chown", _record_chown)
    monkeypatch.setattr(git_manager.os, "lchown", _record_lchown)

    git_manager._chown_tree(root, 1000, 1000)  # noqa: SLF001
    git_manager._chown_tree(directory, 1000, 1000)  # noqa: SLF001

    assert set(lchowned) == {root, linked_child, linked_child_directory}
    assert set(chowned) >= {directory, child_file}
    assert target not in chowned
    assert linked_target not in chowned
    assert linked_directory_target not in chowned


@pytest.mark.unit
def test_reclaim_stale_worktree_treats_already_removed_directory_as_success(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "already-removed"

    GitManager._reclaim_stale_worktree(missing)  # noqa: SLF001

    assert not missing.exists()
