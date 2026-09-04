"""Fail-closed edges of the item-start Git config snapshot helpers (part 1).

Covers packed-refs scanning, HEAD identity fields, gitfile / commondir text
helpers, and the item-start cache predicates and pins.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from awf.node import git_manager_ownership
from awf.runtime.pr_monitor_runner import (
    comment_verdict_residue_fingerprint_git_config as gc,
)
from awf.runtime.pr_monitor_runner import comment_verdict_residue_io as residue_io
from tests.unit.runtime.test_residue_git_config_edges_parts._layout import (
    init_linked_layout,
    init_plain_repo,
    key_for,
    make_fifo,
)


def _patch_path_method(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    should_fail: Callable[[Path], bool],
) -> None:
    """Make ``Path.<name>`` raise ``PermissionError`` for selected paths."""
    real = getattr(Path, name)

    def _wrapped(self: Path, *args: object, **kwargs: object) -> object:
        if should_fail(self):
            raise PermissionError(f"{name} denied: {self}")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, name, _wrapped)


def _fstat_failing_on_call(monkeypatch: pytest.MonkeyPatch, failing_call: int) -> None:
    """Make the ``failing_call``-th ``os.fstat`` (1-based) raise ``OSError``."""
    real = os.fstat
    calls = {"n": 0}

    def _wrapped(fd: int) -> os.stat_result:
        calls["n"] += 1
        if calls["n"] == failing_call:
            raise OSError("fstat denied")
        return real(fd)

    monkeypatch.setattr(os, "fstat", _wrapped)


# --- packed-refs -----------------------------------------------------------


@pytest.mark.unit
def test_packed_refs_tip_from_line_rejects_malformed_columns() -> None:
    assert gc._packed_refs_tip_from_line(b"abc refs/heads/main extra", "refs/heads/main") is None
    assert gc._packed_refs_tip_from_line(b"# pack-refs with: peeled", "refs/heads/main") is None
    assert gc._packed_refs_tip_from_line(b"^deadbeef", "refs/heads/main") is None
    assert gc._packed_refs_tip_from_line(b"abc refs/heads/other", "refs/heads/main") is None
    assert gc._packed_refs_tip_from_line(b"abc refs/heads/main", "refs/heads/main") == "abc\n"


@pytest.mark.unit
def test_read_packed_refs_tip_missing_file_is_empty(tmp_path: Path) -> None:
    assert gc._read_packed_refs_tip_for_name(tmp_path / "packed-refs", "refs/heads/main") == ""


@pytest.mark.unit
def test_read_packed_refs_tip_non_regular_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "packed-refs").mkdir()
    assert gc._read_packed_refs_tip_for_name(tmp_path / "packed-refs", "refs/heads/main") is None


@pytest.mark.unit
def test_read_packed_refs_tip_fstat_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packed = tmp_path / "packed-refs"
    packed.write_text("abc refs/heads/main\n", encoding="utf-8")
    _fstat_failing_on_call(monkeypatch, 1)
    assert gc._read_packed_refs_tip_for_name(packed, "refs/heads/main") is None


@pytest.mark.unit
def test_read_packed_refs_tip_read_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packed = tmp_path / "packed-refs"
    packed.write_text("abc refs/heads/main\n", encoding="utf-8")

    def _read_fails(_fd: int, _n: int) -> bytes:
        raise OSError("read denied")

    monkeypatch.setattr(os, "read", _read_fails)
    assert gc._read_packed_refs_tip_for_name(packed, "refs/heads/main") is None


@pytest.mark.unit
def test_read_packed_refs_tip_short_read_stops_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packed = tmp_path / "packed-refs"
    packed.write_text("abc refs/heads/main\n", encoding="utf-8")
    monkeypatch.setattr(os, "read", lambda _fd, _n: b"")
    # Nothing scanned: ref absent, file stable -> empty tip.
    assert gc._read_packed_refs_tip_for_name(packed, "refs/heads/main") == ""


@pytest.mark.unit
def test_read_packed_refs_tip_trailing_partial_line_without_ref(tmp_path: Path) -> None:
    packed = tmp_path / "packed-refs"
    packed.write_text("abc refs/heads/main\ndef refs/heads/other", encoding="utf-8")
    assert gc._read_packed_refs_tip_for_name(packed, "refs/heads/missing") == ""
    assert gc._read_packed_refs_tip_for_name(packed, "refs/heads/other") == "def\n"


@pytest.mark.unit
def test_read_packed_refs_tip_unstable_file_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packed = tmp_path / "packed-refs"
    packed.write_text("abc refs/heads/main\n", encoding="utf-8")
    # First fstat is the open-time stat; the second is the stability re-check.
    _fstat_failing_on_call(monkeypatch, 2)
    assert gc._read_packed_refs_tip_for_name(packed, "refs/heads/missing") is None


# --- HEAD identity ---------------------------------------------------------


@pytest.mark.unit
def test_head_identity_lstat_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_dir = tmp_path / "repo" / ".git"
    init_plain_repo(tmp_path / "repo")
    _patch_path_method(monkeypatch, "lstat", lambda p: p.name == "HEAD")
    assert gc._snapshot_git_dir_head_identity_fields(git_dir) is None


@pytest.mark.unit
def test_head_identity_unreadable_head_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_dir = tmp_path / "repo" / ".git"
    init_plain_repo(tmp_path / "repo")
    monkeypatch.setattr(git_manager_ownership, "_read_git_dir_config_text", lambda _p: None)
    assert gc._snapshot_git_dir_head_identity_fields(git_dir) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "head_text",
    ["ref:\n", "ref: heads/main\n", "ref: refs/heads/\n", "ref: refs\\heads\n", "ref: refs/../x\n"],
)
def test_head_identity_rejects_unsafe_symbolic_refs(tmp_path: Path, head_text: str) -> None:
    git_dir = tmp_path / "repo" / ".git"
    init_plain_repo(tmp_path / "repo")
    (git_dir / "HEAD").write_text(head_text, encoding="utf-8")
    assert gc._snapshot_git_dir_head_identity_fields(git_dir) is None


@pytest.mark.unit
def test_head_identity_loose_ref_lstat_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    init_plain_repo(repo)
    branch = gc._read_git_dir_config_text_name = None  # noqa: F841 - keep helper import shape
    head_ref = (repo / ".git" / "HEAD").read_text(encoding="utf-8").split("ref:")[1].strip()
    leaf = Path(head_ref).name
    _patch_path_method(monkeypatch, "lstat", lambda p: p.name == leaf and "refs" in p.parts)
    assert gc._snapshot_git_dir_head_identity_fields(repo / ".git") is None


@pytest.mark.unit
def test_head_identity_symlinked_loose_ref_fails_closed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_plain_repo(repo)
    head_ref = (repo / ".git" / "HEAD").read_text(encoding="utf-8").split("ref:")[1].strip()
    loose = repo / ".git" / Path(*Path(head_ref).parts)
    tip = loose.read_text(encoding="utf-8")
    (repo / ".git" / "real_tip").write_text(tip, encoding="utf-8")
    loose.unlink()
    loose.symlink_to(repo / ".git" / "real_tip")
    assert gc._snapshot_git_dir_head_identity_fields(repo / ".git") is None


@pytest.mark.unit
def test_head_identity_unreadable_loose_tip_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    init_plain_repo(repo)
    real = git_manager_ownership._read_git_dir_config_text

    def _only_head(path: Path) -> str | None:
        return real(path) if path.name == "HEAD" else None

    monkeypatch.setattr(git_manager_ownership, "_read_git_dir_config_text", _only_head)
    assert gc._snapshot_git_dir_head_identity_fields(repo / ".git") is None


# --- gitfile text ----------------------------------------------------------


@pytest.mark.unit
def test_snapshot_outer_gitfile_text_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worktree = tmp_path / "ws"
    worktree.mkdir()
    assert gc._snapshot_outer_gitfile_text(worktree) == (True, None)

    make_fifo(worktree / ".git")
    assert gc._snapshot_outer_gitfile_text(worktree) == (False, None)
    (worktree / ".git").unlink()

    (worktree / ".git").write_text("gitdir: /tmp/x\n", encoding="utf-8")
    monkeypatch.setattr(git_manager_ownership, "_read_git_dir_config_text", lambda _p: None)
    assert gc._snapshot_outer_gitfile_text(worktree) == (False, None)
    monkeypatch.undo()

    _patch_path_method(monkeypatch, "lstat", lambda p: p.name == ".git")
    assert gc._snapshot_outer_gitfile_text(worktree) == (False, None)


@pytest.mark.unit
def test_resolve_gitfile_target_resolve_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_path_method(monkeypatch, "resolve", lambda p: p.name == "linked")
    assert gc._resolve_gitfile_target(tmp_path, "gitdir: linked\n") is None


@pytest.mark.unit
def test_gitfile_target_path_without_follow_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert gc._gitfile_target_path_without_follow(tmp_path, "not a gitfile\n") is None
    assert gc._gitfile_target_path_without_follow(tmp_path, "gitdir:   \n") is None
    relative = gc._gitfile_target_path_without_follow(tmp_path, "gitdir: ../linked\n")
    assert relative == Path(os.path.normpath(tmp_path.resolve() / "../linked"))
    _patch_path_method(monkeypatch, "resolve", lambda p: p == tmp_path)
    assert gc._gitfile_target_path_without_follow(tmp_path, "gitdir: ../linked\n") is None


# --- cache predicates and pins ----------------------------------------------


@pytest.mark.unit
def test_cache_predicates_missing_path_and_resolve_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing"
    assert gc.item_start_has_gitfile_linkage(missing) is False
    assert gc.item_start_has_commondir(missing) is False
    assert gc.item_start_has_local_git_config_snapshot(missing) is False
    assert gc.restore_item_start_local_git_configs(missing) is True
    assert gc.remember_item_start_local_git_configs(missing) is True
    assert gc._item_start_layout_mirror_root(missing) is None
    with gc.hold_item_start_pinned_git_dir(missing) as pinned:
        assert pinned is None
    with gc.hold_item_start_pinned_common_dir(missing) as pinned:
        assert pinned is None
    with gc.item_start_trusted_head_probe_git_dir(missing) as probe:
        assert probe is None

    present = tmp_path / "present"
    present.mkdir()
    _patch_path_method(monkeypatch, "resolve", lambda p: p == present)
    assert gc.item_start_has_gitfile_linkage(present) is False
    assert gc.item_start_has_commondir(present) is False
    assert gc.item_start_has_local_git_config_snapshot(present) is False
    assert gc.restore_item_start_local_git_configs(present) is False
    assert gc.remember_item_start_local_git_configs(present) is False
    assert gc._restore_item_start_commondir(present) is False
    assert gc._item_start_layout_mirror_root(present) is None
    with gc.hold_item_start_pinned_git_dir(present) as pinned:
        assert pinned is None
    with gc.hold_item_start_pinned_common_dir(present) as pinned:
        assert pinned is None


@pytest.mark.unit
def test_pinned_git_dir_rejects_malformed_remembered_linkage(tmp_path: Path) -> None:
    worktree = tmp_path / "ws"
    worktree.mkdir()
    gc._ITEM_START_GIT_LINKAGE[key_for(worktree)] = "garbage\n"
    with gc.hold_item_start_pinned_git_dir(worktree) as pinned:
        assert pinned is None
    assert gc.item_start_pinned_git_dir(worktree) is None
    assert gc._snapshot_linked_commondir_text(worktree, "garbage\n") == (False, None)
    gc._ITEM_START_COMMONDIR[key_for(worktree)] = "../.."
    with gc.hold_item_start_pinned_common_dir(worktree) as pinned:
        assert pinned is None
    assert gc._restore_item_start_commondir(worktree) is False


@pytest.mark.unit
def test_pinned_git_dir_readlink_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worktree, linked, _mirror, _head = init_linked_layout(tmp_path)
    assert gc.remember_item_start_local_git_configs(worktree)
    assert gc.item_start_pinned_git_dir(worktree) == linked.resolve()
    _patch_path_method(monkeypatch, "readlink", lambda p: str(p).startswith("/proc/"))
    assert gc.item_start_pinned_git_dir(worktree) is None


@pytest.mark.unit
def test_commondir_target_path_without_follow_edges(tmp_path: Path) -> None:
    assert gc._commondir_target_path_without_follow(tmp_path, "  \n") is None
    assert gc._commondir_target_path_without_follow(tmp_path, "/abs/common\n") == Path(
        "/abs/common"
    )
    assert gc._commondir_target_path_without_follow(tmp_path, "../..\n") == Path(
        os.path.normpath(tmp_path / "../..")
    )


@pytest.mark.unit
def test_layout_mirror_root_rejects_foreign_and_root_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree, _linked, mirror, _head = init_linked_layout(tmp_path)
    key = key_for(worktree)
    mirrors = mirror.parent

    gc._ITEM_START_GIT_LINKAGE[key] = f"gitdir: {tmp_path / 'elsewhere' / 'worktrees' / 'x'}\n"
    assert gc._item_start_layout_mirror_root(worktree) is None

    gc._ITEM_START_GIT_LINKAGE[key] = f"gitdir: {mirrors / 'worktrees' / 'x'}\n"
    assert gc._item_start_layout_mirror_root(worktree) is None

    gc._ITEM_START_GIT_LINKAGE[key] = f"gitdir: {mirror / 'worktrees' / 'ws_link'}\n"
    assert gc._item_start_layout_mirror_root(worktree) == mirror

    _patch_path_method(monkeypatch, "resolve", lambda p: p.name == "mirrors")
    assert gc._item_start_layout_mirror_root(worktree) is None
    monkeypatch.undo()
    _patch_path_method(monkeypatch, "resolve", lambda p: p.name == "repo.git")
    assert gc._item_start_layout_mirror_root(worktree) is None


@pytest.mark.unit
def test_snapshot_linked_commondir_text_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree, linked, _mirror, _head = init_linked_layout(tmp_path)
    linkage = f"gitdir: {linked}\n"
    assert gc._snapshot_linked_commondir_text(worktree, None) == (True, None)
    ok, text = gc._snapshot_linked_commondir_text(worktree, linkage)
    assert ok and text

    outside = tmp_path / "outside"
    outside.mkdir()
    assert gc._snapshot_linked_commondir_text(worktree, f"gitdir: {outside}\n") == (False, None)

    (linked / "commondir").write_text("", encoding="utf-8")
    assert gc._snapshot_linked_commondir_text(worktree, linkage) == (True, None)
    (linked / "commondir").write_text("../..\n", encoding="utf-8")

    monkeypatch.setattr(residue_io, "_read_worktree_regular_text_at", lambda *_a, **_k: None)
    assert gc._snapshot_linked_commondir_text(worktree, linkage) == (False, None)
    monkeypatch.undo()

    real_lstat = os.lstat

    def _lstat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if path == "commondir" and "dir_fd" in kwargs:
            raise PermissionError("denied")
        return real_lstat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "lstat", _lstat)
    assert gc._snapshot_linked_commondir_text(worktree, linkage) == (False, None)


@pytest.mark.unit
def test_pinned_common_dir_open_failure_and_missing_linkage(tmp_path: Path) -> None:
    worktree, linked, _mirror, _head = init_linked_layout(tmp_path)
    key = key_for(worktree)
    gc._ITEM_START_GIT_LINKAGE[key] = f"gitdir: {linked}\n"
    gc._ITEM_START_COMMONDIR[key] = str(tmp_path / "outside")
    with gc.hold_item_start_pinned_common_dir(worktree) as pinned:
        assert pinned is None

    gc._ITEM_START_GIT_LINKAGE.pop(key)
    assert gc._restore_item_start_commondir(worktree) is False
    with gc.hold_item_start_pinned_common_dir(worktree) as pinned:
        assert pinned is None


@pytest.mark.unit
def test_restore_commondir_open_failure(tmp_path: Path) -> None:
    worktree, _linked, _mirror, _head = init_linked_layout(tmp_path)
    key = key_for(worktree)
    gc._ITEM_START_GIT_LINKAGE[key] = f"gitdir: {tmp_path / 'outside'}\n"
    gc._ITEM_START_COMMONDIR[key] = "../.."
    assert gc._restore_item_start_commondir(worktree) is False


@pytest.mark.unit
def test_snapshot_nested_gitfile_linkages_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    make_fifo(nested / ".git")
    assert gc._snapshot_nested_gitfile_linkages((nested,)) is None
    (nested / ".git").unlink()
    (nested / ".git").write_text("gitdir: /tmp/x\n", encoding="utf-8")
    _patch_path_method(monkeypatch, "resolve", lambda p: p == nested)
    assert gc._snapshot_nested_gitfile_linkages((nested,)) is None
    monkeypatch.undo()
    assert gc._snapshot_nested_gitfile_linkages((nested,)) == {
        str(nested.resolve()): "gitdir: /tmp/x\n"
    }
    assert stat.S_ISREG((nested / ".git").lstat().st_mode)
