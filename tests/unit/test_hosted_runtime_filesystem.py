"""Nonroot Core and the agent share only managed checkout metadata by group."""

import os
import stat

import pytest

from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.runtime_filesystem import share_checkout


def test_scoped_group_access_preserves_owner_executable_and_external_symlink(tmp_path):
    root = tmp_path / "git"
    mirror = root / "mirrors/repo.git"
    worktree = root / "worktrees/ws_test"
    mirror.mkdir(parents=True)
    worktree.mkdir(parents=True)
    (mirror / "objects").mkdir()
    (mirror / "refs").mkdir()
    (worktree / "script").write_text("synthetic")
    (worktree / "script").chmod(0o700)
    outside = tmp_path / "outside"
    outside.write_text("unchanged")
    outside.chmod(0o600)
    (worktree / "link").symlink_to(outside)
    share_checkout(root, mirror, worktree, os.getegid())
    assert stat.S_IMODE((worktree / "script").stat().st_mode) == 0o770
    assert stat.S_IMODE(worktree.stat().st_mode) == 0o2770
    assert stat.S_IMODE(outside.stat().st_mode) == 0o600
    assert (worktree / "script").stat().st_uid == os.geteuid()


def test_checkout_share_rejects_unmanaged_or_symlink_parent(tmp_path):
    root = tmp_path / "git"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "worktrees").symlink_to(outside)
    with pytest.raises(MaterialDeniedError):
        share_checkout(root, outside, outside, os.getegid())
    with pytest.raises(MaterialDeniedError):
        share_checkout(root, root / "mirrors/repo.git", root / "worktrees/ws_test", os.getegid())
