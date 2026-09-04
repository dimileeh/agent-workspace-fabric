"""Fail-closed edges of the item-start Git config snapshot helpers (part 2).

Covers the fresh-inode config writers, snapshot restore, the outer git-dir
resolution, trusted git-dir materialization and the trusted HEAD probe.
"""

from __future__ import annotations

import contextlib
import os
import stat
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult
from awf.node import git_manager_ownership
from awf.runtime.pr_monitor_runner import (
    comment_verdict_residue_fingerprint_git_config as gc,
)
from awf.runtime.pr_monitor_runner import comment_verdict_residue_io as residue_io
from awf.runtime.pr_monitor_runner import comment_verdict_residue_nested as residue_nested
from tests.unit.runtime.test_residue_git_config_edges_parts._layout import (
    init_linked_layout,
    init_plain_repo,
    key_for,
)


def _fake_stat(template: os.stat_result, **overrides: int) -> os.stat_result:
    """Return a copy of ``template`` with selected fields replaced."""
    fields = list(template)
    names = ["st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid", "st_size"]
    for name, value in overrides.items():
        fields[names.index(name)] = value
    return os.stat_result(tuple(fields))


class _Sequenced:
    """Wrap an ``os`` function; per-call overrides are exceptions or result transforms."""

    def __init__(self, real: Callable[..., object], overrides: dict[int, object]) -> None:
        self._real = real
        self._overrides = overrides
        self.calls = 0

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.calls += 1
        override = self._overrides.get(self.calls)
        if isinstance(override, BaseException):
            raise override
        result = self._real(*args, **kwargs)
        if callable(override):
            return override(result)
        return result


def _sequence(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    overrides: dict[int, object],
) -> _Sequenced:
    wrapped = _Sequenced(getattr(os, name), overrides)
    monkeypatch.setattr(os, name, wrapped)
    return wrapped


def _patch_path_method(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    should_fail: Callable[[Path], bool],
    *,
    fail_on_call: int | None = None,
) -> None:
    """Make ``Path.<name>`` raise for selected paths (optionally only the Nth match)."""
    real = getattr(Path, name)
    matches = {"n": 0}

    def _wrapped(self: Path, *args: object, **kwargs: object) -> object:
        if should_fail(self):
            matches["n"] += 1
            if fail_on_call is None or matches["n"] == fail_on_call:
                raise PermissionError(f"{name} denied: {self}")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, name, _wrapped)


@pytest.fixture
def git_dir_fd(tmp_path: Path) -> Iterator[int]:
    """A pinned directory descriptor for ``tmp_path``."""
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield fd
    finally:
        os.close(fd)


# --- fresh-inode writers ----------------------------------------------------


@pytest.mark.unit
def test_write_local_git_config_file_fstat_and_write_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "config"
    _sequence(monkeypatch, "fstat", {1: OSError("fstat denied")})
    assert gc._write_local_git_config_file(target, "[core]\n") is False
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []
    monkeypatch.undo()
    _sequence(monkeypatch, "write", {1: OSError("write denied")})
    assert gc._write_local_git_config_file(target, "[core]\n") is False
    assert list(tmp_path.iterdir()) == []


@pytest.mark.unit
def test_write_local_git_config_file_at_open_and_io_failures(
    tmp_path: Path, git_dir_fd: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = os.open

    def _open_tmp_fails(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if "dir_fd" in kwargs and flags & os.O_CREAT:
            raise PermissionError("create denied")
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", _open_tmp_fails)
    assert gc._write_local_git_config_file_at(git_dir_fd, "config", "x\n") is False
    monkeypatch.undo()

    _sequence(monkeypatch, "fstat", {1: OSError("fstat denied")})
    assert gc._write_local_git_config_file_at(git_dir_fd, "config", "x\n") is False
    assert list(tmp_path.iterdir()) == []
    monkeypatch.undo()

    _sequence(monkeypatch, "write", {1: OSError("write denied")})
    assert gc._write_local_git_config_file_at(git_dir_fd, "config", "x\n") is False
    assert list(tmp_path.iterdir()) == []


@pytest.mark.unit
def test_write_local_git_config_file_at_pre_replace_verification_failures(
    tmp_path: Path, git_dir_fd: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Verify-open of the temp entry fails.
    real_open = os.open

    def _verify_open_fails(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if "dir_fd" in kwargs and not flags & os.O_WRONLY:
            raise PermissionError("verify denied")
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", _verify_open_fails)
    assert gc._write_local_git_config_file_at(git_dir_fd, "config", "x\n") is False
    assert list(tmp_path.iterdir()) == []
    monkeypatch.undo()

    # Second fstat is the verify-fstat: not regular / wrong inode / wrong size.
    for override in (
        lambda st: _fake_stat(st, st_mode=stat.S_IFDIR | 0o755),
        lambda st: _fake_stat(st, st_ino=st.st_ino + 1),
        lambda st: _fake_stat(st, st_size=st.st_size + 1),
    ):
        _sequence(monkeypatch, "fstat", {2: override})
        assert gc._write_local_git_config_file_at(git_dir_fd, "config", "x\n") is False
        assert list(tmp_path.iterdir()) == []
        monkeypatch.undo()


@pytest.mark.unit
def test_write_local_git_config_file_at_post_replace_verification_failures(
    tmp_path: Path, git_dir_fd: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sequence(monkeypatch, "replace", {1: OSError("replace denied")})
    assert gc._write_local_git_config_file_at(git_dir_fd, "config", "x\n") is False
    assert list(tmp_path.iterdir()) == []
    monkeypatch.undo()

    for override in (
        OSError("lstat denied"),
        lambda st: _fake_stat(st, st_mode=stat.S_IFDIR | 0o755),
        lambda st: _fake_stat(st, st_ino=st.st_ino + 1),
    ):
        _sequence(monkeypatch, "lstat", {1: override})
        assert gc._write_local_git_config_file_at(git_dir_fd, "config", "x\n") is False
        monkeypatch.undo()
        (tmp_path / "config").unlink(missing_ok=True)

    # Destination bytes differ on the post-replace read (second verify read).
    _sequence(monkeypatch, "read", {2: lambda _got: b"y\n"})
    assert gc._write_local_git_config_file_at(git_dir_fd, "config", "x\n") is False
    monkeypatch.undo()
    assert gc._write_local_git_config_file_at(git_dir_fd, "config", "x\n") is True
    assert (tmp_path / "config").read_text(encoding="utf-8") == "x\n"


# --- restore paths -----------------------------------------------------------


@pytest.mark.unit
def test_restore_nested_git_linkages_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = tmp_path / "outer"
    nested = outer / "nested"
    nested.mkdir(parents=True)
    monkeypatch.setattr(gc, "_write_local_git_config_file_at", lambda *_a: False)
    assert (
        gc._restore_nested_git_linkages({str(nested): "gitdir: x\n"}, outer_worktree_path=outer)
        is False
    )


def _called_from(name: str) -> bool:
    """True when the monkeypatched os/Path call was made directly by function ``name``."""
    frame = sys._getframe(2)
    return frame.f_code.co_name == name


@pytest.mark.unit
def test_open_snapshotted_git_dir_root_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = tmp_path / "ws"
    outer.mkdir()
    git_dir = outer.resolve()
    opener = "_open_snapshotted_git_dir_for_restore"

    real_resolve = Path.resolve

    def _resolve(self: Path, *args: object, **kwargs: object) -> Path:
        if _called_from(opener):
            raise PermissionError("resolve denied")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _resolve)
    with gc._open_snapshotted_git_dir_for_restore(git_dir, outer_worktree_path=outer) as fd:
        assert fd is None
    monkeypatch.undo()

    real_open = os.open

    def _root_open_fails(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if _called_from(opener):
            raise PermissionError("open denied")
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", _root_open_fails)
    with gc._open_snapshotted_git_dir_for_restore(git_dir, outer_worktree_path=outer) as fd:
        assert fd is None
    monkeypatch.undo()

    real_fstat = os.fstat

    def _root_fstat_not_dir(fd: int) -> os.stat_result:
        st = real_fstat(fd)
        if _called_from(opener):
            return _fake_stat(st, st_mode=stat.S_IFREG | 0o644)
        return st

    monkeypatch.setattr(os, "fstat", _root_fstat_not_dir)
    with gc._open_snapshotted_git_dir_for_restore(git_dir, outer_worktree_path=outer) as fd:
        assert fd is None
    monkeypatch.undo()

    with gc._open_snapshotted_git_dir_for_restore(git_dir, outer_worktree_path=outer) as fd:
        assert fd is not None
    with gc._open_snapshotted_git_dir_for_restore(
        Path("relative"), outer_worktree_path=outer
    ) as fd:
        assert fd is None


@pytest.mark.unit
def test_restore_worktree_local_git_configs_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    init_plain_repo(repo)
    git_dir = repo / ".git"
    key = str(git_dir.resolve())
    config_text = (git_dir / "config").read_text(encoding="utf-8")

    @contextlib.contextmanager
    def _never_opens(*_a: object, **_k: object) -> Iterator[None]:
        yield None

    monkeypatch.setattr(residue_io, "_open_git_metadata_relative_parent", _never_opens)
    assert (
        gc._restore_worktree_local_git_configs(
            {key: {"config": config_text}}, outer_worktree_path=repo
        )
        is False
    )
    monkeypatch.undo()

    monkeypatch.setattr(gc, "_write_local_git_config_file_at", lambda *_a: False)
    assert (
        gc._restore_worktree_local_git_configs(
            {key: {"config": config_text}}, outer_worktree_path=repo
        )
        is False
    )
    monkeypatch.undo()

    real_lstat = os.lstat

    def _lstat_denied(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if path == "config.worktree" and "dir_fd" in kwargs:
            raise PermissionError("denied")
        return real_lstat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "lstat", _lstat_denied)
    assert (
        gc._restore_worktree_local_git_configs(
            {key: {"config": config_text}}, outer_worktree_path=repo
        )
        is False
    )
    monkeypatch.undo()

    (git_dir / "config.worktree").write_text("[core]\n", encoding="utf-8")
    real_unlink = os.unlink

    def _unlink_missing(path: object, *args: object, **kwargs: object) -> None:
        if path == "config.worktree":
            raise FileNotFoundError(path)
        real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "unlink", _unlink_missing)
    assert (
        gc._restore_worktree_local_git_configs(
            {key: {"config": config_text}}, outer_worktree_path=repo
        )
        is True
    )
    monkeypatch.undo()

    def _unlink_denied(path: object, *args: object, **kwargs: object) -> None:
        if path == "config.worktree":
            raise PermissionError(path)
        real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "unlink", _unlink_denied)
    assert (
        gc._restore_worktree_local_git_configs(
            {key: {"config": config_text}}, outer_worktree_path=repo
        )
        is False
    )
    monkeypatch.undo()

    (git_dir / "config.worktree").unlink()
    (git_dir / "config.worktree").mkdir()
    assert (
        gc._restore_worktree_local_git_configs(
            {key: {"config": config_text}}, outer_worktree_path=repo
        )
        is False
    )


@pytest.mark.unit
def test_restore_item_start_local_git_configs_linkage_and_commondir_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree, linked, _mirror, _head = init_linked_layout(tmp_path)
    assert gc.remember_item_start_local_git_configs(worktree)
    monkeypatch.setattr(gc, "_restore_worktree_git_linkage", lambda *_a: False)
    assert gc.restore_item_start_local_git_configs(worktree) is False
    monkeypatch.undo()
    monkeypatch.setattr(gc, "_restore_item_start_commondir", lambda *_a: False)
    assert gc.restore_item_start_local_git_configs(worktree) is False
    monkeypatch.undo()
    assert gc.restore_item_start_local_git_configs(worktree) is True
    assert (linked / "commondir").read_text(encoding="utf-8").strip() == "../.."


# --- outer git-dir resolution ------------------------------------------------


@pytest.mark.unit
def test_item_start_outer_git_dir_and_snapshot_coverage_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gitfile_ws = tmp_path / "gitfile_ws"
    gitfile_ws.mkdir()
    (gitfile_ws / ".git").write_text("gitdir: /nonexistent\n", encoding="utf-8")
    assert gc._item_start_outer_git_dir(gitfile_ws) is None
    gc._ITEM_START_LOCAL_GIT_CONFIGS[key_for(gitfile_ws)] = {"x": {}}
    assert gc.item_start_snapshot_covers_outer_git_dir(gitfile_ws) is False
    with gc.item_start_trusted_head_probe_git_dir(gitfile_ws) as probe:
        assert probe is None

    repo = tmp_path / "repo"
    init_plain_repo(repo)
    key = key_for(repo)
    gc._ITEM_START_LOCAL_GIT_CONFIGS[key] = {}
    assert gc.item_start_snapshot_covers_outer_git_dir(repo) is False

    gc._ITEM_START_LOCAL_GIT_CONFIGS[key] = {"other": {}}
    _patch_path_method(monkeypatch, "resolve", lambda p: p.name == ".git", fail_on_call=1)
    assert gc._item_start_outer_git_dir(repo) is None
    monkeypatch.undo()
    _patch_path_method(monkeypatch, "resolve", lambda p: p.name == ".git", fail_on_call=2)
    assert gc.item_start_snapshot_covers_outer_git_dir(repo) is False
    monkeypatch.undo()

    with gc.item_start_trusted_head_probe_git_dir(repo) as probe:
        assert probe is None  # snapshot lacks the live git-dir key
    _patch_path_method(monkeypatch, "resolve", lambda p: p == repo, fail_on_call=2)
    with gc.item_start_trusted_head_probe_git_dir(repo) as probe:
        assert probe is None
    monkeypatch.undo()
    _patch_path_method(monkeypatch, "resolve", lambda p: p.name == ".git", fail_on_call=2)
    with gc.item_start_trusted_head_probe_git_dir(repo) as probe:
        assert probe is None


# --- trusted config staging --------------------------------------------------


@pytest.mark.unit
def test_write_trusted_local_configs_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    origin = tmp_path / "origin"
    origin.mkdir()
    monkeypatch.setattr(
        git_manager_ownership, "_rewrite_relative_core_worktree_for_snapshot", lambda *_a: None
    )
    assert gc._write_trusted_local_configs(staging, {}, original_git_dir=origin) is False
    monkeypatch.undo()

    assert (
        gc._write_trusted_local_configs(tmp_path / "missing", {}, original_git_dir=origin) is False
    )

    configs = {"config": "[core]\n", "config.worktree": "[core]\n\tbare = false\n"}
    assert gc._write_trusted_local_configs(staging, configs, original_git_dir=origin) is True
    assert (staging / "config.worktree").read_text(encoding="utf-8") == configs["config.worktree"]

    real = git_manager_ownership._rewrite_relative_core_worktree_for_snapshot
    monkeypatch.setattr(
        git_manager_ownership,
        "_rewrite_relative_core_worktree_for_snapshot",
        lambda text, gd: None if "bare" in text else real(text, gd),
    )
    assert gc._write_trusted_local_configs(staging, configs, original_git_dir=origin) is False
    monkeypatch.undo()

    _patch_path_method(monkeypatch, "write_bytes", lambda p: p.name == "config.worktree")
    assert gc._write_trusted_local_configs(staging, configs, original_git_dir=origin) is False


@pytest.mark.unit
def test_materialize_trusted_git_dir_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    init_plain_repo(repo)
    live = repo / ".git"
    configs = {"config": (live / "config").read_text(encoding="utf-8")}

    def _fresh_staging(name: str) -> Path:
        staging = tmp_path / name
        staging.mkdir()
        return staging

    def _materialize(staging: Path, **kwargs: object) -> bool:
        return gc._materialize_trusted_git_dir_from_live(
            live_git_dir=live, configs=configs, staging=staging, **kwargs
        )

    assert _materialize(tmp_path / "missing") is False

    monkeypatch.setattr(git_manager_ownership, "_read_git_dir_config_text", lambda _p: None)
    assert _materialize(_fresh_staging("s1")) is False
    monkeypatch.undo()

    _patch_path_method(monkeypatch, "write_bytes", lambda p: p.name == "HEAD")
    assert _materialize(_fresh_staging("s2")) is False
    monkeypatch.undo()

    monkeypatch.setattr(git_manager_ownership, "_open_git_dir_directory_fd", lambda _p: None)
    assert _materialize(_fresh_staging("s3")) is False
    monkeypatch.undo()

    real_stat = os.stat

    def _stat_denied(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if path in {"objects", "refs"} and "dir_fd" in kwargs:
            raise PermissionError(path)
        return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "stat", _stat_denied)
    assert _materialize(_fresh_staging("s4")) is False
    monkeypatch.undo()

    s5 = _fresh_staging("s5")
    (s5 / "objects").write_text("", encoding="utf-8")
    assert _materialize(s5) is False

    def _refs_denied(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if path == "refs" and "dir_fd" in kwargs:
            raise PermissionError(path)
        return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "stat", _refs_denied)
    assert _materialize(_fresh_staging("s6")) is False
    monkeypatch.undo()

    held = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(
        git_manager_ownership,
        "_symlink_nested_probe_refs_store_via_fd",
        lambda *_a: (False, [held]),
    )
    assert _materialize(_fresh_staging("s7")) is False
    with pytest.raises(OSError):
        os.fstat(held)  # closed by the materializer
    monkeypatch.undo()

    # Missing store directories fail closed for outer probes.
    bare = tmp_path / "bare_stub"
    bare.mkdir()
    (bare / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (bare / "config").write_text("[core]\n", encoding="utf-8")
    s8 = _fresh_staging("s8")
    assert (
        gc._materialize_trusted_git_dir_from_live(
            live_git_dir=bare, configs={"config": "[core]\n"}, staging=s8
        )
        is False
    )
    (bare / "objects").mkdir()
    s9 = _fresh_staging("s9")
    assert (
        gc._materialize_trusted_git_dir_from_live(
            live_git_dir=bare, configs={"config": "[core]\n"}, staging=s9
        )
        is False
    )
    (bare / "refs").mkdir()
    s10 = _fresh_staging("s10")
    assert (
        gc._materialize_trusted_git_dir_from_live(
            live_git_dir=bare, configs={"config": "[core]\n"}, staging=s10
        )
        is True
    )


# --- trusted HEAD probe ------------------------------------------------------


def _remembered_plain_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    init_plain_repo(repo)
    assert gc.remember_item_start_local_git_configs(repo)
    return repo, key_for(repo)


@pytest.mark.unit
def test_trusted_head_probe_plain_repo_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, key = _remembered_plain_repo(tmp_path)
    with gc.item_start_trusted_head_probe_git_dir(repo) as probe:
        assert probe is not None and (probe / "HEAD").exists()

    _patch_path_method(monkeypatch, "mkdir", lambda p: p.name == "git")
    with gc.item_start_trusted_head_probe_git_dir(repo) as probe:
        assert probe is None
    monkeypatch.undo()

    # Empty commondir text fails closed.
    gc._ITEM_START_COMMONDIR[key] = " "
    with gc.item_start_trusted_head_probe_git_dir(repo) as probe:
        assert probe is None
    with gc.hold_item_start_pinned_common_dir(repo) as pinned:
        assert pinned is None
    gc._ITEM_START_COMMONDIR.pop(key)

    # Live empty commondir file behaves the same.
    (repo / ".git" / "commondir").write_text("", encoding="utf-8")
    with gc.item_start_trusted_head_probe_git_dir(repo) as probe:
        assert probe is None
    (repo / ".git" / "commondir").unlink()


@pytest.mark.unit
def test_trusted_head_probe_linked_common_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, key = _remembered_plain_repo(tmp_path)
    common = tmp_path / "common"
    common.mkdir()
    (common / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (common / "config").write_text("[core]\n", encoding="utf-8")
    gc._ITEM_START_COMMONDIR[key] = str(common)

    # Snapshot lacks the common git-dir key.
    with gc.item_start_trusted_head_probe_git_dir(repo) as probe:
        assert probe is None

    snapshot = gc._ITEM_START_LOCAL_GIT_CONFIGS[key]
    snapshot[str(common.resolve())] = {"config": "[core]\n"}

    _patch_path_method(monkeypatch, "resolve", lambda p: p == common)
    with gc.item_start_trusted_head_probe_git_dir(repo) as probe:
        assert probe is None
    monkeypatch.undo()

    _patch_path_method(monkeypatch, "mkdir", lambda p: p.name == "common")
    with gc.item_start_trusted_head_probe_git_dir(repo) as probe:
        assert probe is None
    monkeypatch.undo()

    # Common store lacks objects/refs -> materialization fails closed.
    with gc.item_start_trusted_head_probe_git_dir(repo) as probe:
        assert probe is None
    (common / "objects").mkdir()
    (common / "refs").mkdir()

    _patch_path_method(monkeypatch, "write_bytes", lambda p: p.parent.name == "worktree")
    with gc.item_start_trusted_head_probe_git_dir(repo) as probe:
        assert probe is None
    monkeypatch.undo()

    real_read = git_manager_ownership._read_git_dir_config_text
    monkeypatch.setattr(
        git_manager_ownership,
        "_read_git_dir_config_text",
        lambda p: None if p.name == "HEAD" else real_read(p),
    )
    with gc.item_start_trusted_head_probe_git_dir(repo) as probe:
        assert probe is None
    monkeypatch.undo()

    _patch_path_method(
        monkeypatch, "write_bytes", lambda p: p.name == "HEAD" and p.parent.name == "worktree"
    )
    with gc.item_start_trusted_head_probe_git_dir(repo) as probe:
        assert probe is None
    monkeypatch.undo()

    with gc.item_start_trusted_head_probe_git_dir(repo) as probe:
        assert probe is not None
        assert (probe / "commondir").read_text(encoding="utf-8").strip().endswith("common")


@pytest.mark.unit
async def test_rev_parse_head_via_item_start_trust_git_failure(tmp_path: Path) -> None:
    repo, _key = _remembered_plain_repo(tmp_path)

    async def _run(*_a: object, **_k: object) -> CommandResult:
        return CommandResult(returncode=128, stdout="", stderr="fatal")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))
    assert await gc.rev_parse_head_via_item_start_trust(runner, repo) is None  # type: ignore[arg-type]


@pytest.mark.unit
async def test_read_protocol_attempt_start_head_uninspectable_callable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _rev_parse(_path: Path) -> str:
        return "a" * 40

    def _no_signature(_obj: object) -> object:
        raise ValueError("no signature")

    monkeypatch.setattr(gc.inspect, "signature", _no_signature)
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=None))
    result = await gc.read_protocol_attempt_start_head(
        runner,  # type: ignore[arg-type]
        worktree_path=tmp_path,
        rev_parse_head=_rev_parse,
    )
    assert result == "a" * 40


# --- worktree config snapshot walk -------------------------------------------


@pytest.mark.unit
def test_snapshot_worktree_local_git_configs_fail_closed_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    init_plain_repo(repo)
    nested = repo / "nested"
    init_plain_repo(nested)
    assert gc._snapshot_worktree_local_git_configs(repo) is not None

    monkeypatch.setattr(residue_nested, "_approved_git_metadata_roots", lambda _p: ())
    assert gc._snapshot_worktree_local_git_configs(repo) is None
    monkeypatch.undo()

    monkeypatch.setattr(gc, "_module_git_dirs_under", lambda *_a, **_k: None)
    assert gc._snapshot_worktree_local_git_configs(repo) is None
    monkeypatch.undo()

    monkeypatch.setattr(gc, "_nested_worktree_roots_with_git_markers", lambda _p: None)
    assert gc._snapshot_worktree_local_git_configs(repo) is None
    monkeypatch.undo()

    monkeypatch.setattr(gc, "_snapshot_nested_gitfile_linkages", lambda _r: None)
    assert gc._snapshot_worktree_local_git_configs(repo, nested_linkages_out={}) is None
    monkeypatch.undo()

    real_scan = git_manager_ownership._nested_repository_git_dirs_for_include_scan
    monkeypatch.setattr(
        git_manager_ownership,
        "_nested_repository_git_dirs_for_include_scan",
        lambda root, **kw: None if root == nested else real_scan(root, **kw),
    )
    assert gc._snapshot_worktree_local_git_configs(repo) is None
    monkeypatch.undo()

    real_modules = gc._module_git_dirs_under
    monkeypatch.setattr(
        gc,
        "_module_git_dirs_under",
        lambda gd, **kw: None if gd == (nested / ".git").resolve() else real_modules(gd, **kw),
    )
    assert gc._snapshot_worktree_local_git_configs(repo) is None
    monkeypatch.undo()

    monkeypatch.setattr(git_manager_ownership, "_snapshot_git_dir_local_configs", lambda _p: None)
    assert gc._snapshot_worktree_local_git_configs(repo) is None
    monkeypatch.undo()

    monkeypatch.setattr(gc, "_snapshot_git_dir_head_identity_fields", lambda _p: None)
    assert gc._snapshot_worktree_local_git_configs(repo) is None
    monkeypatch.undo()

    # Fail the git-dir key resolve that follows a successful head-identity read.
    real_head_fields = gc._snapshot_git_dir_head_identity_fields

    def _arm_resolve_failure(git_dir: Path) -> dict[str, str] | None:
        fields = real_head_fields(git_dir)
        _patch_path_method(monkeypatch, "resolve", lambda p: p == git_dir)
        return fields

    monkeypatch.setattr(gc, "_snapshot_git_dir_head_identity_fields", _arm_resolve_failure)
    assert gc._snapshot_worktree_local_git_configs(repo) is None


@pytest.mark.unit
def test_pinned_common_dir_rejects_blank_remembered_commondir(tmp_path: Path) -> None:
    worktree, linked, _mirror, _head = init_linked_layout(tmp_path)
    key = key_for(worktree)
    gc._ITEM_START_GIT_LINKAGE[key] = f"gitdir: {linked}\n"
    gc._ITEM_START_COMMONDIR[key] = " "
    with gc.hold_item_start_pinned_common_dir(worktree) as pinned:
        assert pinned is None


@pytest.mark.unit
def test_remember_fails_closed_when_commondir_snapshot_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree, _linked, _mirror, _head = init_linked_layout(tmp_path)
    monkeypatch.setattr(gc, "_snapshot_linked_commondir_text", lambda *_a: (False, None))
    assert gc.remember_item_start_local_git_configs(worktree) is False
    assert key_for(worktree) not in gc._ITEM_START_LOCAL_GIT_CONFIGS


@pytest.mark.unit
def test_restore_item_start_configs_reverts_info_attributes(tmp_path: Path) -> None:
    """Correction-written ``info/attributes`` is removed or rewound on rollback."""
    repo = tmp_path / "repo"
    init_plain_repo(repo)
    attributes = repo / ".git" / "info" / "attributes"
    assert gc.remember_item_start_local_git_configs(repo)

    attributes.write_text("* filter=exfil\n", encoding="utf-8")
    assert gc.restore_item_start_local_git_configs(repo) is True
    assert not attributes.exists()

    attributes.write_text("*.txt text eol=lf\n", encoding="utf-8")
    assert gc.remember_item_start_local_git_configs(repo)
    attributes.write_text("* filter=exfil\n", encoding="utf-8")
    assert gc.restore_item_start_local_git_configs(repo) is True
    assert attributes.read_text(encoding="utf-8") == "*.txt text eol=lf\n"
