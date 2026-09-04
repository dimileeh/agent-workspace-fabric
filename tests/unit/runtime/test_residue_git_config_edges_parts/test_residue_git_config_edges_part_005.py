"""Fail-closed edges of the git-dir ownership snapshot and store helpers."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from awf.node import git_manager_ownership as go
from awf.node import git_manager_ownership_store as store
from tests.unit.runtime.test_residue_git_config_edges_parts._layout import (
    init_plain_repo,
)


def _called_from(name: str) -> bool:
    return sys._getframe(2).f_code.co_name == name


def _fake_stat(template: os.stat_result, **overrides: int) -> os.stat_result:
    fields = list(template)
    names = ["st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid", "st_size"]
    for name, value in overrides.items():
        fields[names.index(name)] = value
    return os.stat_result(tuple(fields))


def _patch_path_method(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    should_fail: Callable[[Path], bool],
) -> None:
    real = getattr(Path, name)

    def _wrapped(self: Path, *args: object, **kwargs: object) -> object:
        if should_fail(self):
            raise PermissionError(f"{name} denied: {self}")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, name, _wrapped)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    init_plain_repo(path)
    return path


@pytest.fixture
def git_dir_fd(repo: Path) -> Iterator[int]:
    fd = os.open(repo / ".git", os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield fd
    finally:
        os.close(fd)


# --- config snapshot budget and readers ------------------------------------------


@pytest.mark.unit
def test_residue_git_config_snapshot_budget_nests_without_replacing() -> None:
    with go._residue_git_config_snapshot_budget():
        outer = go._GIT_CONFIG_SNAPSHOT_BUDGET.get()
        with go._residue_git_config_snapshot_budget():
            assert go._GIT_CONFIG_SNAPSHOT_BUDGET.get() is outer
    assert go._GIT_CONFIG_SNAPSHOT_BUDGET.get() is None


@pytest.mark.unit
def test_config_readers_fail_closed_past_snapshot_deadline(repo: Path) -> None:
    budget = go._GitConfigSnapshotBudget(bytes_remaining=1 << 30, deadline=time.monotonic() - 1.0)
    token = go._GIT_CONFIG_SNAPSHOT_BUDGET.set(budget)
    try:
        assert go._read_git_dir_config_text(repo / ".git" / "config") is None
        fd = os.open(repo / ".git" / "config", os.O_RDONLY)
        try:
            assert (
                go._read_fd_regular_file_bytes(
                    fd, max_bytes=1 << 20, apply_config_snapshot_budget=True
                )
                is None
            )
        finally:
            os.close(fd)
    finally:
        go._GIT_CONFIG_SNAPSHOT_BUDGET.reset(token)


@pytest.mark.unit
def test_snapshot_git_dir_info_exclude_fail_closed_edges(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_dir = repo / ".git"
    assert go._snapshot_git_dir_info_exclude(git_dir) not in (None, {})

    _patch_path_method(monkeypatch, "lstat", lambda p: p.name == "info")
    assert go._snapshot_git_dir_info_exclude(git_dir) is None
    monkeypatch.undo()

    _patch_path_method(monkeypatch, "lstat", lambda p: p.name == "exclude")
    assert go._snapshot_git_dir_info_exclude(git_dir) is None
    monkeypatch.undo()

    monkeypatch.setattr(go, "_read_git_dir_config_text", lambda _p: None)
    assert go._snapshot_git_dir_info_exclude(git_dir) is None


@pytest.mark.unit
def test_nested_repository_git_dirs_containment_and_empty_commondir(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_dir = repo / ".git"
    (git_dir / "commondir").write_text("\n", encoding="utf-8")
    assert go._nested_repository_git_dirs_for_include_scan(repo) == (git_dir.resolve(),)
    (git_dir / "commondir").unlink()

    real_resolve = Path.resolve

    def _resolve(self: Path, *args: object, **kwargs: object) -> Path:
        if _called_from("_nested_git_metadata_containment_roots"):
            raise PermissionError("resolve denied")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _resolve)
    assert go._nested_repository_git_dirs_for_include_scan(repo) is None


@pytest.mark.unit
def test_snapshot_git_dir_local_configs_rejects_includes(repo: Path) -> None:
    (repo / ".git" / "config").write_text("[include]\n\tpath = /tmp/x\n", encoding="utf-8")
    assert go._snapshot_git_dir_local_configs(repo / ".git") is None


# --- bounded leaf copies ---------------------------------------------------------


@pytest.mark.unit
def test_copy_opened_regular_file_io_and_budget_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload" * 100)
    dest = tmp_path / "dest.bin"

    def _copy(**kwargs: object) -> bool:
        fd = os.open(source, os.O_RDONLY)
        try:
            return go._copy_opened_regular_file_to_path(fd, dest, **kwargs)  # type: ignore[arg-type]
        finally:
            os.close(fd)

    assert _copy(budget_seconds=0.0) is False
    assert not dest.exists()
    assert (
        go._copy_opened_regular_file_to_path(
            os.open(source, os.O_RDONLY), tmp_path / "missing" / "x"
        )
        is False
    )

    real_read = os.read

    def _read_denied(fd: int, n: int) -> bytes:
        if _called_from("_copy_opened_regular_file_to_path"):
            raise OSError("read denied")
        return real_read(fd, n)

    monkeypatch.setattr(os, "read", _read_denied)
    assert _copy() is False and not dest.exists()
    monkeypatch.undo()

    def _read_short(fd: int, n: int) -> bytes:
        if _called_from("_copy_opened_regular_file_to_path"):
            return b""
        return real_read(fd, n)

    monkeypatch.setattr(os, "read", _read_short)
    assert _copy() is False and not dest.exists()
    monkeypatch.undo()

    real_write = os.write

    def _write_denied(fd: int, data: object) -> int:
        if _called_from("_copy_opened_regular_file_to_path"):
            raise OSError("write denied")
        return real_write(fd, data)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "write", _write_denied)
    assert _copy() is False and not dest.exists()
    monkeypatch.undo()

    monkeypatch.setattr(
        os,
        "write",
        lambda fd, data: (
            0 if _called_from("_copy_opened_regular_file_to_path") else real_write(fd, data)
        ),
    )
    assert _copy() is False and not dest.exists()
    monkeypatch.undo()

    # Deadline expiring between the read and the write.
    real_monotonic = time.monotonic
    ticks = iter([1000.0, 1000.0, 5000.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks, real_monotonic()))
    assert _copy(budget_seconds=1.0) is False and not dest.exists()
    monkeypatch.undo()

    real_fstat = os.fstat
    calls = {"n": 0}

    def _fstat_second_fails(fd: int) -> os.stat_result:
        st = real_fstat(fd)
        if _called_from("_copy_opened_regular_file_to_path"):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("fstat denied")
        return st

    monkeypatch.setattr(os, "fstat", _fstat_second_fails)
    assert _copy() is False and not dest.exists()
    monkeypatch.undo()

    calls["n"] = 0

    def _fstat_second_unstable(fd: int) -> os.stat_result:
        st = real_fstat(fd)
        if _called_from("_copy_opened_regular_file_to_path"):
            calls["n"] += 1
            if calls["n"] == 2:
                return _fake_stat(st, st_size=st.st_size + 1)
        return st

    monkeypatch.setattr(os, "fstat", _fstat_second_unstable)
    assert _copy() is False and not dest.exists()
    monkeypatch.undo()
    assert _copy() is True and dest.read_bytes() == b"payload" * 100


# --- nested probe stores -----------------------------------------------------------


@pytest.mark.unit
def test_nested_probe_store_materializers_propagate_and_fail_closed(
    repo: Path, git_dir_fd: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()

    def _boom(*_a: object, **_k: object) -> bool:
        raise RuntimeError("materializer crashed")

    monkeypatch.setattr(go, "_symlink_object_store_tree_via_fd", _boom)
    with pytest.raises(RuntimeError):
        store._symlink_nested_probe_objects_store_via_fd(git_dir_fd, staging)
    with pytest.raises(RuntimeError):
        store._symlink_nested_probe_refs_store_via_fd(git_dir_fd, staging)
    monkeypatch.undo()

    monkeypatch.setattr(go, "_open_git_dir_child_directory_fd", lambda *_a: None)
    assert store._symlink_nested_probe_refs_store_via_fd(git_dir_fd, tmp_path / "s2") == (False, [])
    monkeypatch.undo()

    real_stat = os.stat

    def _stat_denied(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if path == "refs" and "dir_fd" in kwargs:
            raise PermissionError("denied")
        return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "stat", _stat_denied)
    assert store._symlink_nested_probe_refs_store_via_fd(git_dir_fd, tmp_path / "s3") == (False, [])
    monkeypatch.undo()

    blocked = tmp_path / "s4"
    blocked.mkdir()
    (blocked / "refs").write_text("", encoding="utf-8")
    assert store._symlink_nested_probe_refs_store_via_fd(git_dir_fd, blocked) == (False, [])

    bare = tmp_path / "bare"
    bare.mkdir()
    bare_fd = os.open(bare, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert store._symlink_nested_probe_refs_store_via_fd(bare_fd, tmp_path / "s5") == (True, [])
    finally:
        os.close(bare_fd)


@pytest.mark.unit
def test_rewrite_relative_core_worktree_same_line_and_cr_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = "[core]\r\tworktree = ../wt\r"
    rewritten = store._rewrite_relative_core_worktree_for_snapshot(text, tmp_path)
    assert rewritten is not None and str((tmp_path / "../wt").resolve()) in rewritten

    same_line_other = "[core] bare = false\n"
    assert (
        store._rewrite_relative_core_worktree_for_snapshot(same_line_other, tmp_path)
        == same_line_other
    )
    same_line_abs = "[core] worktree = /abs/wt\n"
    assert (
        store._rewrite_relative_core_worktree_for_snapshot(same_line_abs, tmp_path) == same_line_abs
    )

    real_resolve = Path.resolve

    def _resolve(self: Path, *args: object, **kwargs: object) -> Path:
        if _called_from("_rewrite_relative_core_worktree_for_snapshot"):
            raise PermissionError("resolve denied")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _resolve)
    assert (
        store._rewrite_relative_core_worktree_for_snapshot("[core] worktree = ../wt\n", tmp_path)
        is None
    )


@pytest.mark.unit
def test_untrusted_nested_probe_snapshot_split_index_and_held_fds(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with store.untrusted_nested_probe_config_snapshot_git_dir(repo) as shadow:
        assert shadow is not None and (shadow / "HEAD").exists()

    monkeypatch.setattr(go, "_symlink_split_index_backing_files_via_fd", lambda *_a: False)
    with store.untrusted_nested_probe_config_snapshot_git_dir(repo) as shadow:
        assert shadow is None
    monkeypatch.undo()

    held: list[int] = []

    def _materialize_with_held(object_fd: int, _staging: Path) -> tuple[bool, list[int]]:
        dup = os.dup(object_fd)
        held.append(dup)
        return True, [dup]

    def _link_child(
        dir_fd: int, _name: str, _dest: Path, held_fds: list[int], **_k: object
    ) -> bool:
        dup = os.dup(dir_fd)
        held.append(dup)
        held_fds.append(dup)
        return True

    monkeypatch.setattr(go, "_symlink_nested_probe_objects_store_via_fd", _materialize_with_held)
    monkeypatch.setattr(go, "_symlink_nested_probe_refs_store_via_fd", _materialize_with_held)
    monkeypatch.setattr(go, "_symlink_git_dir_child_via_fd", _link_child)
    with store.untrusted_nested_probe_config_snapshot_git_dir(repo) as shadow:
        assert shadow is not None
    assert len(held) >= 3
    for fd in held:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.unit
def test_config_readers_fail_closed_when_deadline_passes_mid_read(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared deadline is re-checked between chunks, not only at admission."""
    budget = go._GitConfigSnapshotBudget(bytes_remaining=1 << 30, deadline=1000.0)
    token = go._GIT_CONFIG_SNAPSHOT_BUDGET.set(budget)
    real_monotonic = time.monotonic
    try:
        # admission, per-read deadline base, per-read check, shared-deadline check
        ticks = iter([999.0, 999.0, 999.0, 1001.0])
        monkeypatch.setattr(time, "monotonic", lambda: next(ticks, real_monotonic()))
        assert go._read_git_dir_config_text(repo / ".git" / "config") is None
        monkeypatch.undo()

        # admission, per-read deadline base, per-read check, shared-deadline check
        ticks = iter([999.0, 999.0, 999.0, 1001.0])
        monkeypatch.setattr(time, "monotonic", lambda: next(ticks, real_monotonic()))
        fd = os.open(repo / ".git" / "config", os.O_RDONLY)
        try:
            assert (
                go._read_fd_regular_file_bytes(
                    fd, max_bytes=1 << 20, apply_config_snapshot_budget=True
                )
                is None
            )
        finally:
            os.close(fd)
    finally:
        go._GIT_CONFIG_SNAPSHOT_BUDGET.reset(token)


@pytest.mark.unit
def test_snapshot_git_dir_info_files_include_attributes(repo: Path) -> None:
    """``info/attributes`` is fingerprinted beside ``info/exclude`` (Codex PRRT_kwDOSJAM6s6fOdia)."""
    git_dir = repo / ".git"
    baseline = go._snapshot_git_dir_info_exclude(git_dir)
    assert baseline is not None and "info/attributes" not in baseline
    (git_dir / "info" / "attributes").write_text("*.txt text eol=lf\n", encoding="utf-8")
    snapshot = go._snapshot_git_dir_info_exclude(git_dir)
    assert snapshot is not None
    assert snapshot["info/attributes"] == "*.txt text eol=lf\n"
    assert snapshot["info/exclude"] == baseline["info/exclude"]
    assert go._snapshot_git_dir_local_configs(git_dir) == {
        "config": (git_dir / "config").read_text(encoding="utf-8"),
        **snapshot,
    }
    (git_dir / "info" / "attributes").unlink()
    (git_dir / "info" / "attributes").symlink_to("exclude")
    assert go._snapshot_git_dir_info_exclude(git_dir) is None
