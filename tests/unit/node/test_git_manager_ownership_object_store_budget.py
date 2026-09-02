"""Object-store enum budget tests for nested probe snapshot materialization."""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

import pytest

import awf.node.git_manager_ownership as git_manager_ownership


@pytest.mark.unit
def test_symlink_object_store_tree_via_fd_rejects_entry_flood(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6eq1r7: aggregate entry cap must fail closed mid-stream."""
    root = tmp_path / "objects"
    root.mkdir()
    for i in range(5):
        (root / f"flood-{i:04d}").write_bytes(b"x")
    staging = tmp_path / "staging"
    staging.mkdir()
    held: list[int] = []
    monkeypatch.setattr(git_manager_ownership, "_OBJECT_STORE_ENUM_AGGREGATE_MAX_ENTRIES", 3)
    fd = git_manager_ownership._open_git_dir_directory_fd(root)
    assert fd is not None
    try:
        assert git_manager_ownership._symlink_object_store_tree_via_fd(fd, staging, held) is False
    finally:
        for held_fd in held:
            os.close(held_fd)
        os.close(fd)


@pytest.mark.unit
def test_symlink_object_store_tree_via_fd_rejects_excessive_depth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bugbot 5094985052: deep objects trees must fail closed, not RecursionError."""
    root = tmp_path / "objects"
    root.mkdir()
    cursor = root
    for i in range(5):
        cursor = cursor / f"d{i}"
        cursor.mkdir()
    (cursor / "leaf").write_bytes(b"obj")
    staging = tmp_path / "staging"
    staging.mkdir()
    held: list[int] = []
    monkeypatch.setattr(git_manager_ownership, "_OBJECT_STORE_ENUM_MAX_DEPTH", 2)
    fd = git_manager_ownership._open_git_dir_directory_fd(root)
    assert fd is not None
    try:
        assert git_manager_ownership._symlink_object_store_tree_via_fd(fd, staging, held) is False
    finally:
        for held_fd in held:
            os.close(held_fd)
        os.close(fd)


@pytest.mark.unit
def test_symlink_object_store_tree_via_fd_rejects_past_deadline(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eq1r7: wall-time budget must fail closed before enumeration."""
    root = tmp_path / "objects"
    root.mkdir()
    (root / "ab").mkdir()
    (root / "ab" / "cdef").write_bytes(b"obj")
    staging = tmp_path / "staging"
    staging.mkdir()
    held: list[int] = []
    budget = git_manager_ownership._ObjectStoreEnumBudget(
        entries_remaining=100_000,
        deadline=time.monotonic() - 1.0,
        max_depth=git_manager_ownership._OBJECT_STORE_ENUM_MAX_DEPTH,
    )
    fd = git_manager_ownership._open_git_dir_directory_fd(root)
    assert fd is not None
    try:
        assert (
            git_manager_ownership._symlink_object_store_tree_via_fd(
                fd, staging, held, budget=budget
            )
            is False
        )
    finally:
        for held_fd in held:
            os.close(held_fd)
        os.close(fd)


@pytest.mark.unit
def test_symlink_object_store_tree_via_fd_rejects_mid_scan_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wall-time budget is re-checked while streaming entries (skips . / ..)."""
    root = tmp_path / "objects"
    root.mkdir()
    (root / "keep").write_bytes(b"x")
    (root / "late").write_bytes(b"y")
    staging = tmp_path / "staging"
    staging.mkdir()
    held: list[int] = []

    class _Entry:
        def __init__(self, name: str) -> None:
            self.name = name

    @contextlib.contextmanager
    def _scandir_two(_path: str | bytes | os.PathLike[str]) -> object:
        yield [_Entry("."), _Entry(".."), _Entry("keep"), _Entry("late")]

    clock = {"t": 1000.0}
    real_stat = os.stat

    def _stat_expire_after_keep(
        path: str | bytes | os.PathLike[str], *args: object, **kwargs: object
    ) -> os.stat_result:
        result = real_stat(path, *args, **kwargs)  # type: ignore[arg-type]
        if path == "keep":
            clock["t"] = 1001.0
        return result

    monkeypatch.setattr(os, "scandir", _scandir_two)
    monkeypatch.setattr(git_manager_ownership.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(os, "stat", _stat_expire_after_keep)
    budget = git_manager_ownership._ObjectStoreEnumBudget(
        entries_remaining=100_000,
        deadline=1000.5,
        max_depth=git_manager_ownership._OBJECT_STORE_ENUM_MAX_DEPTH,
    )
    fd = git_manager_ownership._open_git_dir_directory_fd(root)
    assert fd is not None
    try:
        assert (
            git_manager_ownership._symlink_object_store_tree_via_fd(
                fd, staging, held, budget=budget
            )
            is False
        )
    finally:
        for held_fd in held:
            os.close(held_fd)
        os.close(fd)


@pytest.mark.unit
def test_symlink_nested_probe_objects_store_honors_shared_entry_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Top-level objects materialization shares one aggregate entry budget."""
    git_dir = tmp_path / "repo.git"
    objects = git_dir / "objects"
    objects.mkdir(parents=True)
    for i in range(4):
        (objects / f"n{i:02d}").write_bytes(b"o")
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(git_manager_ownership, "_OBJECT_STORE_ENUM_AGGREGATE_MAX_ENTRIES", 2)
    fd = git_manager_ownership._open_git_dir_directory_fd(git_dir)
    assert fd is not None
    try:
        ok, held = git_manager_ownership._symlink_nested_probe_objects_store_via_fd(fd, staging)
        assert ok is False
        assert held == []
    finally:
        os.close(fd)
