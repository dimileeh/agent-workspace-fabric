"""Focused regressions for nested git-dir / worktree pin residue probes."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from awf.node.git_manager import git_env_without_object_lookup_overrides
from awf.runtime.pr_monitor_runner import comment_verdict_residue
from tests.unit.runtime.test_comment_verdict_coverage_edges_parts._helpers import (
    init_git_worktree,
    init_git_worktree_with_embedded_repo,
    init_git_worktree_with_gitfile_inside_outer_git,
)

_git_env = git_env_without_object_lookup_overrides


@pytest.mark.unit
def test_nested_git_probe_pins_to_git_reported_worktree_root(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eWr9f: nested probes must hash Git's worktree, not decoy paths."""
    worktree = tmp_path / "ws_redirected_nested"
    worktree.mkdir()
    init_git_worktree(worktree)
    nested_name = "vendor"
    nested_root = worktree / nested_name
    redirected_root = worktree / "actual"
    nested_root.mkdir()
    redirected_root.mkdir()
    subprocess.run(["git", "init"], cwd=nested_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    tracked = redirected_root / "f"
    tracked.write_text("tracked\n", encoding="utf-8")
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(nested_root / ".git"),
            "--work-tree",
            str(redirected_root),
            "add",
            "f",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(nested_root / ".git"),
            "--work-tree",
            str(redirected_root),
            "commit",
            "-m",
            "nested init",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "core.worktree", str(redirected_root.resolve())],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    (nested_root / "f").write_text("decoy\n", encoding="utf-8")
    tracked.write_text("modified\n", encoding="utf-8")

    before = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_name,
        git_env=_git_env(),
    )
    tracked.write_text("modified again\n", encoding="utf-8")
    after = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_name,
        git_env=_git_env(),
    )

    assert before is not None
    assert after is not None
    assert before != after


@pytest.mark.unit
def test_nested_git_probe_pins_git_dir_when_worktree_redirects_inside_outer(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eW4-V: redirected probes must keep the embedded git-dir pinned."""
    worktree = tmp_path / "ws_redirected_git_dir"
    worktree.mkdir()
    init_git_worktree(worktree)
    nested_name = "vendor"
    nested_root = worktree / nested_name
    redirected_root = worktree / "actual"
    nested_root.mkdir()
    redirected_root.mkdir()
    subprocess.run(["git", "init"], cwd=nested_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    tracked = redirected_root / "f"
    tracked.write_text("tracked\n", encoding="utf-8")
    git_dir = nested_root / ".git"
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(git_dir),
            "--work-tree",
            str(redirected_root),
            "add",
            "f",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(git_dir),
            "--work-tree",
            str(redirected_root),
            "commit",
            "-m",
            "nested init",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "core.worktree", str(redirected_root.resolve())],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )

    before = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_name,
        git_env=_git_env(),
    )
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(git_dir),
            "--work-tree",
            str(redirected_root),
            "commit",
            "--allow-empty",
            "-m",
            "nested empty",
        ],
        check=True,
        capture_output=True,
    )
    after = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_name,
        git_env=_git_env(),
    )

    assert before is not None
    assert after is not None
    assert before != after


@pytest.mark.unit
def test_nested_git_probe_ignores_poisoned_local_fsmonitor(tmp_path: Path) -> None:
    """PRRT_kwDOSJAM6s6eV4s0: embedded repo local config must not execute during probes."""
    worktree = tmp_path / "ws_nested_fsmonitor"
    worktree.mkdir()
    nested_path = init_git_worktree_with_embedded_repo(worktree)
    nested_root = worktree / nested_path
    sentinel = tmp_path / "fsmonitor_ran"
    sentinel_script = tmp_path / "evil_fsmonitor.sh"
    sentinel_script.write_text(f"#!/bin/sh\ntouch {sentinel}\n", encoding="utf-8")
    sentinel_script.chmod(0o755)
    subprocess.run(
        ["git", "config", "core.fsmonitor", str(sentinel_script)],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )

    result = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_path,
        git_env=_git_env(),
    )

    assert result is not None
    assert not sentinel.exists()


@pytest.mark.unit
def test_nested_git_probe_ignores_committed_gitattributes_clean_filter(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eWICC: nested probes must not run .gitattributes clean filters."""
    worktree = tmp_path / "ws_nested_gitattributes_filter"
    worktree.mkdir()
    nested_path = "nested"
    nested_root = worktree / nested_path
    nested_root.mkdir()
    sentinel = tmp_path / "clean_filter_ran"
    sentinel_script = tmp_path / "evil_clean.sh"
    sentinel_script.write_text(f"#!/bin/sh\ntouch {sentinel}\n", encoding="utf-8")
    sentinel_script.chmod(0o755)
    subprocess.run(["git", "init"], cwd=nested_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "filter.evil.clean", str(sentinel_script)],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    (nested_root / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
    (nested_root / "inner.txt").write_text("inner\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=nested_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "nested init with filter"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    assert sentinel.exists(), "setup must install a committed filter driver"
    sentinel.unlink()
    (nested_root / "inner.txt").write_text("modified\n", encoding="utf-8")

    result = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_path,
        git_env=_git_env(),
    )

    assert result is not None
    assert not sentinel.exists()


@pytest.mark.unit
def test_nested_git_probe_ignores_lazy_fetch_ext_transport(tmp_path: Path) -> None:
    """PRRT_kwDOSJAM6s6eXXaD: staged probes must not run ext:: promisor lazy-fetch helpers."""
    worktree = tmp_path / "ws_nested_lazy_fetch_ext"
    worktree.mkdir()
    nested_path = init_git_worktree_with_embedded_repo(worktree)
    nested_root = worktree / nested_path
    sentinel = tmp_path / "lazy_fetch_ext_ran"
    helper_script = tmp_path / "evil_ext.sh"
    helper_script.write_text(f"#!/bin/sh\ntouch {sentinel}\nexit 1\n", encoding="utf-8")
    helper_script.chmod(0o755)
    subprocess.run(
        ["git", "config", "protocol.ext.allow", "always"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "remote.origin.promisor", "true"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "remote.origin.url", f"ext::{helper_script} %S"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=nested_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tree_obj = nested_root / ".git" / "objects" / tree[:2] / tree[2:]
    tree_obj.unlink()

    result = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_path,
        git_env=_git_env(),
    )

    assert result is None
    assert not sentinel.exists()


@pytest.mark.unit
def test_nested_git_probe_discovers_inner_repo_while_outer_pin_active(
    tmp_path: Path,
) -> None:
    """Bugbot 5085458675: inner nested-repo discovery must not inherit outer git-dir pin."""
    worktree = tmp_path / "ws_nested_inside_nested"
    worktree.mkdir()
    init_git_worktree(worktree)
    vendor_name = "vendor"
    vendor_root = worktree / vendor_name
    vendor_root.mkdir()
    subprocess.run(["git", "init"], cwd=vendor_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=vendor_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=vendor_root,
        check=True,
        capture_output=True,
    )
    (vendor_root / "outer.txt").write_text("outer\n", encoding="utf-8")
    subprocess.run(["git", "add", "outer.txt"], cwd=vendor_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "vendor init"],
        cwd=vendor_root,
        check=True,
        capture_output=True,
    )

    inner_name = "sub"
    inner_root = vendor_root / inner_name
    inner_root.mkdir()
    subprocess.run(["git", "init"], cwd=inner_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=inner_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=inner_root,
        check=True,
        capture_output=True,
    )
    (inner_root / "inner.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "inner.txt"], cwd=inner_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "inner init"],
        cwd=inner_root,
        check=True,
        capture_output=True,
    )

    before = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=vendor_name,
        git_env=_git_env(),
    )
    (inner_root / "inner.txt").write_text("v2\n", encoding="utf-8")
    after = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=vendor_name,
        git_env=_git_env(),
    )

    assert before is not None
    assert after is not None
    assert before != after


@pytest.mark.unit
def test_nested_gitfile_inside_outer_git_dir_detects_inner_mutations(
    tmp_path: Path,
) -> None:
    """Bugbot 5085822106: inner gitfile repos must not inherit the outer marker fd."""
    worktree = tmp_path / "ws_gitfile_inside_outer_git"
    worktree.mkdir()
    outer_name, inner_name = init_git_worktree_with_gitfile_inside_outer_git(worktree)
    outer_root = worktree / outer_name
    inner_root = outer_root / inner_name

    before = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=outer_name,
        git_env=_git_env(),
    )
    (inner_root / "inner.txt").write_text("v2\n", encoding="utf-8")
    after = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=outer_name,
        git_env=_git_env(),
    )

    assert before is not None
    assert after is not None
    assert before != after


@pytest.mark.unit
def test_open_git_dir_path_at_does_not_close_caller_fd(tmp_path: Path) -> None:
    """Bugbot 5085949873: relative gitfile paths must not close the caller's dir fd."""
    worktree = tmp_path / "ws_gitfile_dot"
    worktree.mkdir()
    dir_fd = os.open(worktree, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        target_fd = comment_verdict_residue._open_git_dir_path_at(dir_fd, Path())
        assert target_fd is not None
        assert target_fd != dir_fd
        os.close(target_fd)
        assert stat.S_ISDIR(os.fstat(dir_fd).st_mode)
    finally:
        os.close(dir_fd)


@pytest.mark.unit
def test_open_git_dir_path_at_non_directory_does_not_close_caller_fd(
    tmp_path: Path,
) -> None:
    """Bugbot 5085949873: failed opens must not close an unowned caller fd."""
    worktree = tmp_path / "ws_gitfile_file"
    worktree.mkdir()
    (worktree / "not-a-dir").write_text("x\n", encoding="utf-8")
    dir_fd = os.open(worktree, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        target_fd = comment_verdict_residue._open_git_dir_path_at(dir_fd, Path("not-a-dir"))
        assert target_fd is None
        assert stat.S_ISDIR(os.fstat(dir_fd).st_mode)
    finally:
        os.close(dir_fd)


@pytest.mark.unit
def test_open_nested_git_dir_gitfile_target_at_non_dir_does_not_close_caller_fd(
    tmp_path: Path,
) -> None:
    """Bugbot 5085949873: non-directory gitfile targets must not close the worktree fd."""
    worktree = tmp_path / "ws_gitdir_file"
    worktree.mkdir()
    nested = worktree / "vendor"
    nested.mkdir()
    (nested / "not-a-dir").write_text("x\n", encoding="utf-8")
    (nested / ".git").write_text("gitdir: not-a-dir\n", encoding="utf-8")
    dir_fd = os.open(nested, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        with comment_verdict_residue._open_nested_git_dir_gitfile_target_at(dir_fd) as target_fd:
            assert target_fd is None
        assert stat.S_ISDIR(os.fstat(dir_fd).st_mode)
    finally:
        os.close(dir_fd)
