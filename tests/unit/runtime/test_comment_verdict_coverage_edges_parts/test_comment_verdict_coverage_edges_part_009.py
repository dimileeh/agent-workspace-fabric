"""Focused regressions for open_git_dir_path_at caller-fd and metadata-root guards."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from awf.runtime.pr_monitor_runner import comment_verdict_residue, comment_verdict_residue_nested
from tests.unit.runtime.test_comment_verdict_coverage_edges_parts._helpers import (
    wire_outer_linked_mirror,
)


@pytest.mark.unit
def test_open_git_dir_path_at_does_not_close_caller_fd(tmp_path: Path) -> None:
    """Bugbot 5085949873: relative gitfile paths must not close the caller's dir fd."""
    worktree = tmp_path / "ws_gitfile_dot"
    worktree.mkdir()
    dir_fd = os.open(worktree, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        target_fd = comment_verdict_residue_nested._open_git_dir_path_at(
            dir_fd,
            Path(),
            outer_worktree_path=worktree,
        )
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
        target_fd = comment_verdict_residue_nested._open_git_dir_path_at(
            dir_fd,
            Path("not-a-dir"),
            outer_worktree_path=worktree,
        )
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
        with comment_verdict_residue._open_nested_git_dir_gitfile_target_at(
            dir_fd,
            outer_worktree_path=worktree,
        ) as opened:
            assert opened is None
        assert stat.S_ISDIR(os.fstat(dir_fd).st_mode)
    finally:
        os.close(dir_fd)


@pytest.mark.unit
def test_open_git_dir_path_at_rejects_absolute_cross_workspace_metadata(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ebFe3: absolute gitfile targets must stay in approved roots."""
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    mirrors = layout / "mirrors"
    worktrees.mkdir(parents=True)
    mirrors.mkdir()
    worktree = worktrees / "ws_a"
    other = worktrees / "ws_b"
    worktree.mkdir()
    other.mkdir()
    other_git = other / ".git"
    other_git.mkdir()
    (other_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    nested = worktree / "vendor"
    nested.mkdir()
    dir_fd = os.open(nested, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        assert (
            comment_verdict_residue_nested._open_git_dir_path_at(
                dir_fd,
                other_git,
                outer_worktree_path=worktree,
            )
            is None
        )
    finally:
        os.close(dir_fd)


@pytest.mark.unit
def test_open_git_dir_path_at_rejects_parent_escaping_relative_metadata(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ebFe3: relative .. gitfile targets must not escape approved roots."""
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    worktrees.mkdir(parents=True)
    worktree = worktrees / "ws_a"
    other = worktrees / "ws_b"
    worktree.mkdir()
    other.mkdir()
    other_git = other / ".git"
    other_git.mkdir()

    nested = worktree / "vendor"
    nested.mkdir()
    dir_fd = os.open(nested, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        assert (
            comment_verdict_residue_nested._open_git_dir_path_at(
                dir_fd,
                Path("../../ws_b/.git"),
                outer_worktree_path=worktree,
            )
            is None
        )
    finally:
        os.close(dir_fd)


@pytest.mark.unit
def test_open_git_dir_path_at_allows_in_worktree_and_mirrors_metadata(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ebFe3: in-checkout and this worktree's mirror git dirs remain openable."""
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    mirrors_common = layout / "mirrors" / "repo.git"
    linked = mirrors_common / "worktrees" / "ws_a"
    worktrees.mkdir(parents=True)
    linked.mkdir(parents=True)
    worktree = worktrees / "ws_a"
    worktree.mkdir()
    wire_outer_linked_mirror(worktree, mirrors_common=mirrors_common)
    in_tree = worktree / ".vendor_git"
    in_tree.mkdir()
    (linked / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    nested = worktree / "vendor"
    nested.mkdir()
    dir_fd = os.open(nested, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        for candidate in (in_tree, Path("../.vendor_git"), linked):
            target_fd = comment_verdict_residue_nested._open_git_dir_path_at(
                dir_fd,
                candidate,
                outer_worktree_path=worktree,
            )
            assert target_fd is not None
            os.close(target_fd)
    finally:
        os.close(dir_fd)


@pytest.mark.unit
def test_open_git_dir_path_at_rejects_sibling_repo_mirror_metadata(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ecze8: nested probes must not admit other repos under mirrors/."""
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    own_mirror = layout / "mirrors" / "repo.git"
    other_mirror = layout / "mirrors" / "other.git" / "worktrees" / "ws_other"
    worktrees.mkdir(parents=True)
    other_mirror.mkdir(parents=True)
    worktree = worktrees / "ws_a"
    worktree.mkdir()
    wire_outer_linked_mirror(worktree, mirrors_common=own_mirror)
    (other_mirror / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    nested = worktree / "vendor"
    nested.mkdir()
    dir_fd = os.open(nested, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        assert (
            comment_verdict_residue_nested._open_git_dir_path_at(
                dir_fd,
                other_mirror,
                outer_worktree_path=worktree,
            )
            is None
        )
    finally:
        os.close(dir_fd)
