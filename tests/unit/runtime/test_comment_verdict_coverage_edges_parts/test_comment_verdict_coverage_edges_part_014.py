"""Ignored-dir metadata fallback overflow regressions (part 14)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.unit.runtime.test_comment_verdict_coverage_edges_parts._helpers import (
    init_git_worktree,
)


@pytest.mark.unit
def test_ignored_dir_metadata_fallback_detects_middle_only_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6e65b4: overflow identity must see middle-only same-size edits.

    Head/tail sampling left an uncovered middle on files larger than two chunk
    windows. A correction that rewrites only that middle while restoring
    ``mtime_ns`` must still change the ignored-dir fingerprint.
    """
    from awf.node.git_manager import git_env_without_object_lookup_overrides
    from awf.runtime.pr_monitor_runner import comment_verdict_residue as residue
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_io as io_mod

    worktree = tmp_path / "ws_ignored_middle_only"
    worktree.mkdir()
    init_git_worktree(worktree)
    vendor = worktree / "vendor"
    vendor.mkdir()
    sample = io_mod._WORKTREE_REGULAR_HASH_CHUNK_BYTES
    target = vendor / "blob.bin"
    baseline = b"H" * sample + b"M" * sample + b"T" * sample
    target.write_bytes(baseline)
    (vendor / "other.txt").write_text("pad\n", encoding="utf-8")

    monkeypatch.setattr(
        residue,
        "_hash_worktree_directory_residue",
        lambda **_kwargs: None,
    )
    git_env = git_env_without_object_lookup_overrides()
    start = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["vendor/"],
        git_env=git_env,
    )
    assert start is not None

    st = target.stat()
    target.write_bytes(b"H" * sample + b"X" * sample + b"T" * sample)
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
    mutated = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["vendor/"],
        git_env=git_env,
    )
    assert mutated is not None and mutated != start


@pytest.mark.unit
@pytest.mark.timeout(60)
def test_ignored_dir_metadata_fallback_detects_oversized_middle_only_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6fF6Nb: >8 MiB ignored blobs must not hide middle-only edits."""
    from awf.node.git_manager import git_env_without_object_lookup_overrides
    from awf.runtime.pr_monitor_runner import comment_verdict_residue as residue
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_io as io_mod

    worktree = tmp_path / "ws_ignored_oversized_middle"
    worktree.mkdir()
    init_git_worktree(worktree)
    vendor = worktree / "vendor"
    vendor.mkdir()
    sample = io_mod._WORKTREE_REGULAR_HASH_CHUNK_BYTES
    oversize = io_mod._WORKTREE_REGULAR_HASH_MAX_FILE_BYTES + sample
    mid = oversize - 2 * sample
    target = vendor / "large.bin"
    target.write_bytes(b"H" * sample + b"M" * mid + b"T" * sample)
    (vendor / "other.txt").write_text("pad\n", encoding="utf-8")

    monkeypatch.setattr(
        residue,
        "_hash_worktree_directory_residue",
        lambda **_kwargs: None,
    )
    git_env = git_env_without_object_lookup_overrides()
    start = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["vendor/"],
        git_env=git_env,
    )
    assert start is not None

    st = target.stat()
    target.write_bytes(b"H" * sample + b"X" * mid + b"T" * sample)
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
    mutated = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["vendor/"],
        git_env=git_env,
    )
    assert mutated is not None and mutated != start


@pytest.mark.unit
def test_ignored_dir_metadata_fallback_includes_nested_checkout_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6e5mkg: metadata fallback must not collapse nested-git to a marker.

    When content hashing fails closed on budget, ignored-dir metadata still has to
    incorporate nested HEAD/staged/unstaged/untracked identity. A presence-only
    ``nested-git`` marker would leave edits inside the nested checkout invisible
    to mutation comparison.
    """
    from awf.node.git_manager import git_env_without_object_lookup_overrides
    from awf.runtime.pr_monitor_runner import comment_verdict_residue as residue
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_ignored_nested_meta"
    worktree.mkdir()
    init_git_worktree(worktree)
    vendor = worktree / "vendor"
    vendor.mkdir()
    (vendor / "pad.txt").write_text("pad\n", encoding="utf-8")
    nested = vendor / "embedded"
    nested.mkdir()
    subprocess.run(["git", "init"], cwd=nested, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    (nested / "inner.txt").write_text("inner-v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "inner.txt"], cwd=nested, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "nested init"], cwd=nested, check=True, capture_output=True
    )

    monkeypatch.setattr(
        residue,
        "_hash_worktree_directory_residue",
        lambda **_kwargs: None,
    )
    git_env = git_env_without_object_lookup_overrides()
    baseline = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["vendor/"],
        git_env=git_env,
    )
    assert baseline is not None

    (nested / "inner.txt").write_text("inner-v2\n", encoding="utf-8")
    subprocess.run(["git", "add", "inner.txt"], cwd=nested, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "nested mutate"], cwd=nested, check=True, capture_output=True
    )
    mutated = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["vendor/"],
        git_env=git_env,
    )
    assert mutated is not None and mutated != baseline


@pytest.mark.unit
def test_ignored_dir_metadata_rejects_intermediate_symlink(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6e5o6e: metadata walk must not follow intermediate dir symlinks.

    Pathname ``os.open(byte_root/path, O_NOFOLLOW)`` only protects the final
    component. After Git reports ``mid/vendor/``, replacing ``mid/`` with a
    symlink must not let the overflow metadata fallback hash an outside tree.
    """
    import stat as stat_mod

    from awf.node.git_manager import git_env_without_object_lookup_overrides
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_io as io_mod

    worktree = tmp_path / "ws_ignored_meta_intermediate"
    worktree.mkdir()
    init_git_worktree(worktree)
    mid = worktree / "mid"
    vendor = mid / "vendor"
    vendor.mkdir(parents=True)
    (vendor / "inside.txt").write_text("inside\n", encoding="utf-8")

    outside = tmp_path / "outside_ignored_host"
    (outside / "vendor").mkdir(parents=True)
    (outside / "vendor" / "inside.txt").write_text("OUTSIDE-SECRET\n", encoding="utf-8")

    git_env = git_env_without_object_lookup_overrides()
    before = fp_mod._hash_ignored_directory_metadata_residue(
        worktree_path=worktree,
        path="mid/vendor",
        git_env=git_env,
    )
    assert before is not None

    backup = worktree / "mid.real"
    mid.rename(backup)
    mid.symlink_to(outside)

    # Full-path O_NOFOLLOW still follows the intermediate symlink (the defect).
    flags = io_mod._WORKTREE_DIRECTORY_OPEN_FLAGS
    leak_fd = os.open(worktree / "mid" / "vendor", flags)
    try:
        assert stat_mod.S_ISDIR(os.fstat(leak_fd).st_mode)
        names = io_mod._sorted_worktree_directory_entry_names(leak_fd)
        assert names == ["inside.txt"]
        with io_mod._open_worktree_regular_file_at(leak_fd, "inside.txt") as fh:
            assert fh.read() == b"OUTSIDE-SECRET\n"
    finally:
        os.close(leak_fd)

    assert (
        fp_mod._hash_ignored_directory_metadata_residue(
            worktree_path=worktree,
            path="mid/vendor",
            git_env=git_env,
        )
        is None
    )
