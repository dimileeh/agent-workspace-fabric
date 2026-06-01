"""Unit tests for validation worktree ignored-path signatures."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from awf.runtime.validation_worktree import (
    _hash_file_contents,
    _snapshot_ignored_path_signatures,
)


@pytest.mark.unit
def test_hash_file_contents_regular_file_has_stable_digest(tmp_path: Path) -> None:
    """Regular files are hashed from content when computing ignored-file signatures."""
    file_path = tmp_path / "data.txt"
    file_path.write_bytes(b"signature me\n")

    assert _hash_file_contents(file_path) == hashlib.sha256(b"signature me\n").hexdigest()


@pytest.mark.unit
def test_ignored_snapshot_signatures_bound_regular_file_content_hashing(
    tmp_path: Path,
) -> None:
    """Ignored snapshot signatures must cap content reads for large dependency trees."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    first = worktree / "first.bin"
    second = worktree / "second.bin"
    first.write_bytes(b"1234")
    second.write_bytes(b"56789")

    signatures = _snapshot_ignored_path_signatures(
        worktree_path=worktree,
        snapshot_paths=("first.bin", "second.bin"),
        max_content_hash_bytes=4,
    )

    assert signatures[0] == ("first.bin", hashlib.sha256(b"1234").hexdigest())
    assert signatures[1][0] == "second.bin"
    assert signatures[1][1].startswith("metadata:")
    assert signatures[1][1] != hashlib.sha256(b"56789").hexdigest()


@pytest.mark.unit
def test_hash_file_contents_symlink_encodes_target(tmp_path: Path) -> None:
    """Symlink entries are represented by their target path, not target contents."""
    target = tmp_path / "target.txt"
    target.write_text("target", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    assert _hash_file_contents(link) == f"symlink:{link.readlink()}"


@pytest.mark.unit
def test_hash_file_contents_special_file_encodes_metadata(tmp_path: Path) -> None:
    """Special files are represented by stable metadata instead of being opened."""
    special = tmp_path / "fifo"
    if not hasattr(os, "mkfifo"):
        pytest.skip("os.mkfifo is not available")
    os.mkfifo(special)
    stats = special.lstat()
    expected = (
        f"special:{stats.st_mode:o}:{stats.st_dev}:{stats.st_ino}:"
        f"{stats.st_size}:{stats.st_mtime_ns}"
    )

    assert _hash_file_contents(special) == expected
