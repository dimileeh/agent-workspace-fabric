"""Fail-closed edges of the residue fingerprint and nested-checkout scans."""

from __future__ import annotations

import contextlib
import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult
from awf.node import git_manager_ownership
from awf.node.git_manager import git_env_without_object_lookup_overrides
from awf.runtime import git_index_hide_flags
from awf.runtime.pr_monitor_runner import comment_verdict_residue as residue
from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp
from awf.runtime.pr_monitor_runner import comment_verdict_residue_io as residue_io
from awf.runtime.pr_monitor_runner import comment_verdict_residue_nested as nested
from tests.unit.runtime.test_residue_git_config_edges_parts._layout import (
    init_plain_repo,
    make_fifo,
)


def _called_from(name: str) -> bool:
    return sys._getframe(2).f_code.co_name == name


def _fake_stat(template: os.stat_result, **overrides: int) -> os.stat_result:
    fields = list(template)
    names = ["st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid", "st_size"]
    for name, value in overrides.items():
        fields[names.index(name)] = value
    return os.stat_result(tuple(fields))


def _fd_path(fd: int) -> str:
    return str(Path(f"/proc/self/fd/{fd}").readlink())


def _patch_fstat_not_dir(
    monkeypatch: pytest.MonkeyPatch, suffix: str, *, caller: str | None = None
) -> None:
    real = os.fstat

    def _fstat(fd: int) -> os.stat_result:
        st = real(fd)
        if _fd_path(fd).endswith(suffix) and (caller is None or _called_from(caller)):
            return _fake_stat(st, st_mode=stat.S_IFREG | 0o644)
        return st

    monkeypatch.setattr(os, "fstat", _fstat)


def _patch_open_denied(monkeypatch: pytest.MonkeyPatch, *, name: str, require_dir_fd: bool) -> None:
    real = os.open

    def _open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        matches = Path(str(path)).name == name if isinstance(path, Path) else path == name
        if matches and (("dir_fd" in kwargs) == require_dir_fd):
            raise PermissionError(f"open denied: {path}")
        return real(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", _open)


# --- ignored-directory metadata identity ------------------------------------


def _meta(worktree: Path, path: str = "vendor") -> str | None:
    return fp._hash_ignored_directory_metadata_residue(
        worktree_path=worktree,
        path=path,
        git_env=git_env_without_object_lookup_overrides(),
    )


@pytest.fixture
def vendor_worktree(tmp_path: Path) -> tuple[Path, Path]:
    worktree = tmp_path / "ws"
    vendor = worktree / "vendor"
    vendor.mkdir(parents=True)
    (vendor / "f.txt").write_text("payload\n", encoding="utf-8")
    return worktree, vendor


@pytest.mark.unit
def test_ignored_dir_metadata_non_directory_and_budget_edges(
    vendor_worktree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree, _vendor = vendor_worktree
    assert _meta(worktree, "vendor/f.txt") is None

    monkeypatch.setattr(residue_io, "_directory_enum_allows_descent", lambda _d: False)
    assert _meta(worktree) is None
    monkeypatch.undo()

    monkeypatch.setattr(residue_io, "_sorted_worktree_directory_entry_names", lambda _fd: None)
    assert _meta(worktree) is None
    monkeypatch.undo()

    monkeypatch.setattr(residue_io, "_worktree_entry_kind_at", lambda _fd, _n: None)
    assert _meta(worktree) is None


@pytest.mark.unit
def test_ignored_dir_metadata_child_directory_edges(
    vendor_worktree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree, vendor = vendor_worktree
    sub = vendor / "sub"
    sub.mkdir()
    (sub / "inner.txt").write_text("inner\n", encoding="utf-8")
    baseline = _meta(worktree)
    assert baseline is not None
    (sub / "inner.txt").write_text("changed\n", encoding="utf-8")
    assert _meta(worktree) not in {None, baseline}

    _patch_open_denied(monkeypatch, name="sub", require_dir_fd=True)
    assert _meta(worktree) is None
    monkeypatch.undo()

    # Nested descent fails closed once the depth budget is exhausted.
    monkeypatch.setattr(residue_io, "_directory_enum_allows_descent", lambda depth: depth < 1)
    assert _meta(worktree) is None
    monkeypatch.undo()

    _patch_fstat_not_dir(monkeypatch, "/sub")
    assert _meta(worktree) is None
    monkeypatch.undo()

    (sub / ".git").write_text("gitdir: /nonexistent\n", encoding="utf-8")
    monkeypatch.setattr(residue, "_git_nested_worktree_commit_at", lambda **_k: None)
    assert _meta(worktree) is None
    monkeypatch.undo()
    monkeypatch.setattr(residue, "_git_nested_worktree_commit_at", lambda **_k: "nested-id")
    assert _meta(worktree) is not None


@pytest.mark.unit
def test_ignored_dir_metadata_leaf_edges(
    vendor_worktree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree, vendor = vendor_worktree
    real_lstat = os.lstat

    def _lstat_denied(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if path == "f.txt" and "dir_fd" in kwargs and _called_from("_hash_at"):
            raise PermissionError("denied")
        return real_lstat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "lstat", _lstat_denied)
    assert _meta(worktree) is None
    monkeypatch.undo()

    monkeypatch.setattr(residue_io, "_hash_regular_file_content_samples_into", lambda *_a: False)
    assert _meta(worktree) is None
    monkeypatch.undo()

    @contextlib.contextmanager
    def _open_fails(*_a: object, **_k: object) -> Iterator[None]:
        raise OSError("open denied")
        yield  # pragma: no cover

    monkeypatch.setattr(residue_io, "_open_worktree_regular_file_at", _open_fails)
    assert _meta(worktree) is None
    monkeypatch.undo()

    (vendor / "link").symlink_to("f.txt")
    with_link = _meta(worktree)
    assert with_link is not None
    (vendor / "link").unlink()
    (vendor / "link").symlink_to("other")
    assert _meta(worktree) not in {None, with_link}

    real_readlink = os.readlink

    def _readlink_denied(path: object, *args: object, **kwargs: object) -> str:
        if path == "link" and "dir_fd" in kwargs:
            raise PermissionError("denied")
        return real_readlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "readlink", _readlink_denied)
    assert _meta(worktree) is None
    monkeypatch.undo()

    make_fifo(vendor / "pipe")
    assert _meta(worktree) is not None


@pytest.mark.unit
def test_ignored_residue_identity_fails_closed_when_both_digests_fail(
    vendor_worktree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree, _vendor = vendor_worktree
    monkeypatch.setattr(residue, "_hash_worktree_directory_residue", lambda **_k: None)
    monkeypatch.setattr(fp, "_hash_ignored_directory_metadata_residue", lambda **_k: None)
    assert (
        fp._hash_ignored_residue_identity(
            worktree_path=worktree,
            ignored_paths=["vendor/"],
            git_env=dict(git_env_without_object_lookup_overrides()),
        )
        is None
    )


@pytest.mark.unit
def test_ignored_paths_from_status_skips_blank_entries() -> None:
    assert fp._ignored_paths_from_status_stdout("!! \n!! vendor/\n", is_z=False) == ["vendor/"]


@pytest.mark.unit
async def test_fingerprint_with_git_metadata_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(_p: Path) -> None:
        raise OSError("snapshot spawn failed")

    monkeypatch.setattr(fp, "_snapshot_worktree_local_git_configs", _raise)
    assert await fp._fingerprint_with_git_metadata(tmp_path, "") is None
    monkeypatch.setattr(fp, "_snapshot_worktree_local_git_configs", lambda _p: None)
    assert await fp._fingerprint_with_git_metadata(tmp_path, "") is None


# --- correction residue fingerprint --------------------------------------------


def _runner_for(stdout: str) -> SimpleNamespace:
    async def _run(_cmd: list[str], **_kwargs: object) -> CommandResult:
        return CommandResult(returncode=0, stdout=stdout, stderr="")

    return SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))


@pytest.fixture
def plain_worktree_doubles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        git_index_hide_flags, "snapshot_and_clear_index_hide_flags", lambda **_k: ""
    )
    monkeypatch.setattr(nested, "_ignored_worktree_relative_paths", lambda _w, _p: frozenset())


@pytest.mark.unit
async def test_residue_fingerprint_hide_flag_spawn_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "ws"
    worktree.mkdir()

    def _spawn_fails(**_k: object) -> None:
        raise OSError("git spawn failed")

    monkeypatch.setattr(git_index_hide_flags, "snapshot_and_clear_index_hide_flags", _spawn_fails)
    assert (
        await fp._read_correction_pr_worthy_residue_fingerprint(
            _runner_for(""), workspace_id="ws", worktree_path=worktree
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.usefixtures("plain_worktree_doubles")
async def test_residue_fingerprint_ignored_identity_failures_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "ws"
    (worktree / "vendor").mkdir(parents=True)

    def _ignored_raises(**_k: object) -> None:
        raise OSError("ignored hash failed")

    # Porcelain ``!!`` entries count as changed paths, so the tracked/untracked
    # digests run before the ignored identity; stub them for the plain-dir double.
    monkeypatch.setattr(
        residue, "_hash_tracked_residue_staged_and_unstaged", lambda **_k: ("s", "u")
    )
    monkeypatch.setattr(residue, "_hash_untracked_residue_paths", lambda **_k: "x")
    monkeypatch.setattr(fp, "_hash_ignored_residue_identity", _ignored_raises)
    (worktree / "new.py").write_text("x\n", encoding="utf-8")
    assert (
        await fp._read_correction_pr_worthy_residue_fingerprint(
            _runner_for("?? new.py\n!! vendor/\n"), workspace_id="ws", worktree_path=worktree
        )
        is None
    )
    monkeypatch.setattr(fp, "_hash_ignored_residue_identity", lambda **_k: None)
    assert (
        await fp._read_correction_pr_worthy_residue_fingerprint(
            _runner_for("?? new.py\n!! vendor/\n"), workspace_id="ws", worktree_path=worktree
        )
        is None
    )
    monkeypatch.setattr(fp, "_hash_ignored_residue_identity", lambda **_k: "ignored-digest")
    fingerprint = await fp._read_correction_pr_worthy_residue_fingerprint(
        _runner_for("?? new.py\n!! vendor/\n"), workspace_id="ws", worktree_path=worktree
    )
    assert fingerprint is not None and "ignored:ignored-digest" in fingerprint


# --- nested checkout scans --------------------------------------------------


class _FakeEntry:
    def __init__(self, path: Path, *, is_dir_error: bool = False) -> None:
        self.name = path.name
        self.path = str(path)
        self._is_dir_error = is_dir_error

    def is_symlink(self) -> bool:
        return False

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        if self._is_dir_error:
            raise OSError("is_dir denied")
        return Path(self.path).is_dir()


def _patch_scandir_for(
    monkeypatch: pytest.MonkeyPatch, target: Path, entries: list[_FakeEntry] | Exception
) -> None:
    real = os.scandir

    def _scandir(path: object = ".") -> object:
        if Path(str(path)) == target:
            if isinstance(entries, Exception):
                raise entries

            @contextlib.contextmanager
            def _cm() -> Iterator[list[_FakeEntry]]:
                yield entries

            return _cm()
        return real(path)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "scandir", _scandir)


def _formal_store(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config").write_text("[core]\n", encoding="utf-8")
    (path / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (path / "objects").mkdir(exist_ok=True)
    (path / "refs").mkdir(exist_ok=True)
    return path


@pytest.mark.unit
def test_objects_hex_shard_children_budget_and_containment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shard = tmp_path / "objects" / "ab"
    (shard / "foo").mkdir(parents=True)
    calls = {"n": 0}

    def _descent(_depth: int) -> bool:
        calls["n"] += 1
        return calls["n"] == 1

    monkeypatch.setattr(residue_io, "_directory_enum_allows_descent", _descent)
    assert (
        nested._subdirectory_children_under_objects_hex_shard(shard, depth=0, roots=(tmp_path,))
        is None
    )
    monkeypatch.undo()
    monkeypatch.setattr(
        git_manager_ownership, "_resolved_git_metadata_within_roots", lambda *_a: None
    )
    assert (
        nested._subdirectory_children_under_objects_hex_shard(shard, depth=0, roots=(tmp_path,))
        is None
    )


@pytest.mark.unit
def test_module_git_dirs_under_walk_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git_dir = tmp_path / "git_dir"
    modules = git_dir / "modules"
    roots = (tmp_path,)

    git_dir.mkdir()
    (git_dir / "modules").write_text("", encoding="utf-8")
    assert nested._module_git_dirs_under(git_dir, roots=roots) == ()
    (git_dir / "modules").unlink()
    _formal_store(modules / "sub")

    real_lstat = Path.lstat
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda self: (
            (_ for _ in ()).throw(PermissionError("denied"))
            if self.name == "modules"
            else real_lstat(self)
        ),
    )
    assert nested._module_git_dirs_under(git_dir, roots=roots) is None
    monkeypatch.undo()

    (modules / "link").symlink_to("sub")
    assert nested._module_git_dirs_under(git_dir, roots=roots) is None
    (modules / "link").unlink()

    _patch_scandir_for(monkeypatch, modules, [_FakeEntry(modules / "sub", is_dir_error=True)])
    assert nested._module_git_dirs_under(git_dir, roots=roots) is None
    monkeypatch.undo()

    _patch_scandir_for(monkeypatch, modules, PermissionError("scandir denied"))
    assert nested._module_git_dirs_under(git_dir, roots=roots) is None
    monkeypatch.undo()

    monkeypatch.setattr(
        git_manager_ownership, "_resolved_git_metadata_within_roots", lambda *_a: None
    )
    assert nested._module_git_dirs_under(git_dir, roots=roots) is None
    monkeypatch.undo()

    assert nested._module_git_dirs_under(git_dir, roots=roots) == ((modules / "sub").resolve(),)


@pytest.mark.unit
def test_module_git_dirs_under_hex_shard_descent_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_dir = tmp_path / "git_dir"
    store = _formal_store(git_dir / "modules" / "sub")
    shard = store / "objects" / "ab"
    shard.mkdir()
    roots = (tmp_path,)

    monkeypatch.setattr(
        nested, "_subdirectory_children_under_objects_hex_shard", lambda *_a, **_k: None
    )
    assert nested._module_git_dirs_under(git_dir, roots=roots) is None
    monkeypatch.undo()

    # Slash-named formal store under the shard whose own walk fails on a symlink.
    foo = _formal_store(shard / "foo")
    (foo / "link").symlink_to("config")
    assert nested._module_git_dirs_under(git_dir, roots=roots) is None
    (foo / "link").unlink()
    assert nested._module_git_dirs_under(git_dir, roots=roots) == (
        store.resolve(),
        foo.resolve(),
    )

    # Plain grouping directory under the shard whose walk fails on a symlink.
    bar = shard / "bar"
    bar.mkdir()
    (bar / "link").symlink_to("..")
    assert nested._module_git_dirs_under(git_dir, roots=roots) is None


@pytest.mark.unit
def test_nested_worktree_roots_walk_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worktree = tmp_path / "ws"
    init_plain_repo(worktree)
    (worktree / ".gitignore").write_text("vendor/\n", encoding="utf-8")
    src = worktree / "src"
    src.mkdir()
    vendor = worktree / "vendor"
    vendor.mkdir()
    (vendor / ".git").write_text("gitdir: /nonexistent\n", encoding="utf-8")
    (vendor / "inner").mkdir()
    init_plain_repo(vendor / "inner" / "embedded")

    found = nested._nested_worktree_roots_with_git_markers(worktree)
    assert found is not None
    assert vendor in found and (vendor / "inner" / "embedded") in found

    monkeypatch.setattr(residue_io, "_directory_enum_allows_descent", lambda _d: False)
    assert nested._nested_worktree_roots_with_git_markers(worktree) is None
    monkeypatch.undo()

    monkeypatch.setattr(residue_io, "_worktree_entry_kind_at", lambda _fd, _n: None)
    assert nested._nested_worktree_roots_with_git_markers(worktree) is None
    monkeypatch.undo()

    _patch_open_denied(monkeypatch, name="vendor", require_dir_fd=True)
    assert nested._nested_worktree_roots_with_git_markers(worktree) is None
    monkeypatch.undo()

    _patch_fstat_not_dir(monkeypatch, "/vendor", caller="_walk_children")
    assert nested._nested_worktree_roots_with_git_markers(worktree) is None
    monkeypatch.undo()

    _patch_open_denied(monkeypatch, name="src", require_dir_fd=True)
    assert nested._nested_worktree_roots_with_git_markers(worktree) is None
    monkeypatch.undo()

    _patch_fstat_not_dir(monkeypatch, "/src", caller="_walk_children")
    assert nested._nested_worktree_roots_with_git_markers(worktree) is None
    monkeypatch.undo()

    _patch_open_denied(monkeypatch, name="ws", require_dir_fd=False)
    assert nested._nested_worktree_roots_with_git_markers(worktree) is None
    monkeypatch.undo()

    _patch_fstat_not_dir(monkeypatch, "/ws", caller="_nested_worktree_roots_with_git_markers")
    assert nested._nested_worktree_roots_with_git_markers(worktree) is None
    monkeypatch.undo()

    # Ignored roots are best-effort in the second phase.
    _patch_open_denied(monkeypatch, name="vendor", require_dir_fd=False)
    assert nested._nested_worktree_roots_with_git_markers(worktree) == (vendor,)
    monkeypatch.undo()

    _patch_fstat_not_dir(monkeypatch, "/vendor", caller="_nested_worktree_roots_with_git_markers")
    assert nested._nested_worktree_roots_with_git_markers(worktree) == (vendor,)
