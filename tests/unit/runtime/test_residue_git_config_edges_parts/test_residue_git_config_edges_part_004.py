"""Fail-closed edges of the residue io helpers and index hide-flag clearing."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from awf.common.commands import CommandResult
from awf.node.git_manager import git_env_without_object_lookup_overrides
from awf.runtime import git_index_hide_flags as hide
from awf.runtime.pr_monitor_runner import comment_verdict_residue_io as residue_io
from tests.unit.runtime.test_residue_git_config_edges_parts._layout import (
    git,
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


@pytest.fixture
def dir_fd(tmp_path: Path) -> Iterator[int]:
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield fd
    finally:
        os.close(fd)


# --- residue io --------------------------------------------------------------


@pytest.mark.unit
def test_open_worktree_directory_path_relative_and_resolve_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = tmp_path / "outer"
    (outer / "sub").mkdir(parents=True)

    real_resolve = Path.resolve

    def _resolve(self: Path, *args: object, **kwargs: object) -> Path:
        if _called_from("_open_worktree_directory_path") and self == outer:
            raise PermissionError("resolve denied")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _resolve)
    with residue_io._open_worktree_directory_path(outer / "sub", outer_worktree_path=outer) as fd:
        assert fd is None
    monkeypatch.undo()

    # Relative directories resolve against the CWD: outside the checkout fails
    # closed, inside it opens the pinned descriptor.
    with residue_io._open_worktree_directory_path(Path("sub"), outer_worktree_path=outer) as fd:
        assert fd is None
    monkeypatch.chdir(outer)
    with residue_io._open_worktree_directory_path(Path("sub"), outer_worktree_path=outer) as fd:
        assert fd is not None
        assert Path(f"/proc/self/fd/{fd}").readlink() == (outer / "sub").resolve()


@pytest.mark.unit
def test_git_metadata_relative_parents_absent_io_failures(
    tmp_path: Path, dir_fd: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "info").mkdir()
    real_lstat = os.lstat

    def _lstat_denied(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if path == "info" and "dir_fd" in kwargs:
            raise PermissionError("denied")
        return real_lstat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "lstat", _lstat_denied)
    assert residue_io._git_metadata_relative_parents_absent(dir_fd, "info/exclude") is False
    monkeypatch.undo()

    real_open = os.open

    def _open_denied(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if path == "info" and "dir_fd" in kwargs:
            raise PermissionError("denied")
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", _open_denied)
    assert residue_io._git_metadata_relative_parents_absent(dir_fd, "info/exclude") is False
    monkeypatch.undo()
    assert residue_io._git_metadata_relative_parents_absent(dir_fd, "info/exclude") is False
    assert residue_io._git_metadata_relative_parents_absent(dir_fd, "missing/exclude") is True


@pytest.mark.unit
def test_open_git_metadata_relative_parent_create_parents_edges(
    tmp_path: Path, dir_fd: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    with residue_io._open_git_metadata_relative_parent(
        dir_fd, "info/exclude", create_parents=True
    ) as opened:
        assert opened is not None
        parent_fd, name = opened
        assert name == "exclude"
        assert Path(f"/proc/self/fd/{parent_fd}").readlink() == tmp_path / "info"
    (tmp_path / "info").rmdir()

    real_mkdir = os.mkdir

    def _mkdir_races(path: object, *args: object, **kwargs: object) -> None:
        real_mkdir(path, *args, **kwargs)  # type: ignore[arg-type]
        raise FileExistsError(path)

    monkeypatch.setattr(os, "mkdir", _mkdir_races)
    with residue_io._open_git_metadata_relative_parent(
        dir_fd, "info/exclude", create_parents=True
    ) as opened:
        assert opened is not None
    monkeypatch.undo()
    (tmp_path / "info").rmdir()

    def _mkdir_denied(path: object, *args: object, **kwargs: object) -> None:
        raise PermissionError(path)

    monkeypatch.setattr(os, "mkdir", _mkdir_denied)
    with residue_io._open_git_metadata_relative_parent(
        dir_fd, "info/exclude", create_parents=True
    ) as opened:
        assert opened is None
    monkeypatch.undo()

    monkeypatch.setattr(os, "mkdir", lambda *_a, **_k: None)
    with residue_io._open_git_metadata_relative_parent(
        dir_fd, "info/exclude", create_parents=True
    ) as opened:
        assert opened is None
    monkeypatch.undo()

    (tmp_path / "info").mkdir()
    real_fstat = os.fstat

    def _fstat_not_dir(fd: int) -> os.stat_result:
        st = real_fstat(fd)
        if _called_from("_open_git_metadata_relative_parent"):
            return _fake_stat(st, st_mode=stat.S_IFREG | 0o644)
        return st

    monkeypatch.setattr(os, "fstat", _fstat_not_dir)
    with residue_io._open_git_metadata_relative_parent(dir_fd, "info/exclude") as opened:
        assert opened is None
    monkeypatch.undo()

    def _fstat_denied(fd: int) -> os.stat_result:
        if _called_from("_open_git_metadata_relative_parent"):
            raise OSError("fstat denied")
        return real_fstat(fd)

    monkeypatch.setattr(os, "fstat", _fstat_denied)
    with residue_io._open_git_metadata_relative_parent(dir_fd, "info/exclude") as opened:
        assert opened is None


# --- index hide flags ----------------------------------------------------------


@pytest.mark.unit
def test_parse_ls_files_v_hide_entries_record_shapes() -> None:
    assert hide.parse_ls_files_v_hide_entries([b"h a.txt", b"H", b"h ", b"x b.txt"]) == [
        ("h", "a.txt")
    ]
    assert hide.parse_ls_files_v_hide_entries(b"S c.txt\0h a.txt") == [
        ("h", "a.txt"),
        ("S", "c.txt"),
    ]
    assert hide.parse_ls_files_v_hide_entries("h a.txt\ns b.txt\n") == [
        ("h", "a.txt"),
        ("s", "b.txt"),
    ]


@pytest.mark.unit
def test_parse_index_hide_flags_snapshot_skips_malformed_lines() -> None:
    assert hide.parse_index_hide_flags_snapshot("hx\0h\0x path\0h a.txt\0") == [("h", "a.txt")]


@pytest.fixture
def hidden_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    init_plain_repo(repo)
    git(repo, "update-index", "--assume-unchanged", "file.txt")
    return repo


@pytest.mark.unit
def test_snapshot_index_hide_flags_subprocess_edges(
    hidden_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = git_env_without_object_lookup_overrides()
    assert hide.snapshot_index_hide_flags(worktree_path=hidden_repo, git_env=env) == "h file.txt\0"

    monkeypatch.setattr(hide, "_LS_FILES_V_MAX_STDOUT_BYTES", 1)
    assert hide.snapshot_index_hide_flags(worktree_path=hidden_repo, git_env=env) is None
    monkeypatch.undo()

    def _spawn_fails(*_a: object, **_k: object) -> None:
        raise OSError("git missing")

    monkeypatch.setattr(hide.subprocess, "run", _spawn_fails)
    assert hide.snapshot_index_hide_flags(worktree_path=hidden_repo, git_env=env) is None
    assert (
        hide._run_update_index_clear(
            worktree_path=hidden_repo,
            git_env=env,
            flag_arg="--no-assume-unchanged",
            paths=["file.txt"],
        )
        is False
    )


@pytest.mark.unit
def test_clear_index_hide_flags_git_failures(
    hidden_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = git_env_without_object_lookup_overrides()
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    assert hide.clear_index_hide_flags(worktree_path=not_a_repo, git_env=env) is False
    assert (
        hide._run_update_index_clear(
            worktree_path=not_a_repo,
            git_env=env,
            flag_arg="--no-assume-unchanged",
            paths=["file.txt"],
        )
        is False
    )
    # A remembered snapshot naming a path that is not in the index fails closed.
    assert (
        hide.clear_index_hide_flags(
            worktree_path=hidden_repo, git_env=env, snapshot="h missing.txt\0"
        )
        is False
    )
    monkeypatch.setattr(hide, "clear_index_hide_flags", lambda **_k: False)
    assert hide.snapshot_and_clear_index_hide_flags(worktree_path=hidden_repo, git_env=env) is None
    monkeypatch.undo()
    assert (
        hide.snapshot_and_clear_index_hide_flags(worktree_path=hidden_repo, git_env=env)
        == "h file.txt\0"
    )
    assert hide.snapshot_index_hide_flags(worktree_path=hidden_repo, git_env=env) == ""


@pytest.mark.unit
async def test_clear_index_hide_flags_via_run_git_update_index_results() -> None:
    calls: list[list[str]] = []

    def _runner(update_ok: bool) -> object:
        async def _run(args: list[str]) -> CommandResult:
            calls.append(list(args))
            if "ls-files" in args:
                return CommandResult(returncode=0, stdout="h a.txt\0S b.txt\0", stderr="")
            return CommandResult(returncode=0 if update_ok else 1, stdout="", stderr="")

        return _run

    assert await hide.clear_index_hide_flags_via_run_git(_runner(True)) is True
    assert [c for c in calls if "update-index" in c] == [
        ["--literal-pathspecs", "update-index", "--no-assume-unchanged", "--", "a.txt"],
        ["--literal-pathspecs", "update-index", "--no-skip-worktree", "--", "b.txt"],
    ]
    assert await hide.clear_index_hide_flags_via_run_git(_runner(False)) is False


@pytest.mark.unit
def test_hide_flag_helpers_roundtrip_for_real_flags(hidden_repo: Path) -> None:
    env = git_env_without_object_lookup_overrides()
    git(hidden_repo, "update-index", "--skip-worktree", "file.txt")
    snapshot = hide.snapshot_index_hide_flags(worktree_path=hidden_repo, git_env=env)
    assert snapshot == "s file.txt\0"
    assert hide.clear_index_hide_flags(worktree_path=hidden_repo, git_env=env, snapshot=snapshot)
    listed = subprocess.run(
        ["git", "ls-files", "-v"], cwd=hidden_repo, check=True, capture_output=True, text=True
    ).stdout
    assert listed.strip() == "H file.txt"


@pytest.mark.unit
def test_hide_flag_snapshot_roundtrips_newline_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tracked path containing a newline must survive snapshot -> clear intact.

    Newline-delimited serialization re-split ``evil\\nh decoy`` into two decoy
    paths and left the real entry flagged (Codex PRRT_kwDOSJAM6s6fOdiX).
    """
    evil = "evil\nh decoy2"
    entries = [("h", evil), ("S", "plain.txt")]
    snapshot = hide.format_index_hide_flags_snapshot(entries)
    assert hide.parse_index_hide_flags_snapshot(snapshot) == [("h", evil), ("S", "plain.txt")]
    commands: list[list[str]] = []

    def _run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(list(command))
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(hide.subprocess, "run", _run)
    assert hide.clear_index_hide_flags(worktree_path=Path("/tmp/ws"), git_env={}, snapshot=snapshot)
    assume_paths = [c[c.index("--") + 1 :] for c in commands if "--no-assume-unchanged" in c]
    skip_paths = [c[c.index("--") + 1 :] for c in commands if "--no-skip-worktree" in c]
    assert assume_paths == [[evil]]
    assert skip_paths == [["plain.txt"]]
