"""Ignored-dir overflow content-sample identity regressions (part 12)."""

from __future__ import annotations

import hashlib
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO

import pytest

from awf.runtime.pr_monitor_runner import comment_verdict_residue_io


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_regular_file_content_samples_into_hashes_small_file(tmp_path: Path) -> None:
    """Files within the sample window are fully folded into the hasher."""
    path = tmp_path / "small.bin"
    payload = b"sample-bytes"
    path.write_bytes(payload)
    hasher = hashlib.sha256()
    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        assert (
            comment_verdict_residue_io._hash_regular_file_content_samples_into(hasher, fh) is True
        )
    assert hasher.digest() == hashlib.sha256(payload).digest()


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_regular_file_content_samples_into_includes_tail_window(tmp_path: Path) -> None:
    """Larger files contribute a non-overlapping head and tail window."""
    path = tmp_path / "wide.bin"
    sample = comment_verdict_residue_io._WORKTREE_REGULAR_HASH_CHUNK_BYTES
    payload = b"H" * sample + b"M" * sample + b"T" * sample
    path.write_bytes(payload)
    hasher = hashlib.sha256()
    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        assert (
            comment_verdict_residue_io._hash_regular_file_content_samples_into(hasher, fh) is True
        )
    expected = hashlib.sha256(b"H" * sample + b"T" * sample).digest()
    assert hasher.digest() == expected


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_regular_file_content_samples_into_overlapping_suffix(tmp_path: Path) -> None:
    """When size is between one and two sample windows, unread suffix is folded."""
    path = tmp_path / "overlap.bin"
    sample = comment_verdict_residue_io._WORKTREE_REGULAR_HASH_CHUNK_BYTES
    payload = b"A" * sample + b"B" * (sample // 2)
    path.write_bytes(payload)
    hasher = hashlib.sha256()
    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        assert (
            comment_verdict_residue_io._hash_regular_file_content_samples_into(hasher, fh) is True
        )
    assert hasher.digest() == hashlib.sha256(payload).digest()


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_regular_file_content_samples_into_enum_deadline_fails_closed(
    tmp_path: Path,
) -> None:
    """Directory-enum wall-clock exhaustion must fail closed before reading."""
    path = tmp_path / "deadline.bin"
    path.write_bytes(b"data")
    budget = comment_verdict_residue_io._DirectoryEnumBudget(
        entries_remaining=10,
        deadline=time.monotonic() - 1.0,
        max_depth=8,
    )
    token = comment_verdict_residue_io._DIRECTORY_ENUM_BUDGET.set(budget)
    try:
        hasher = hashlib.sha256()
        with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
            assert (
                comment_verdict_residue_io._hash_regular_file_content_samples_into(hasher, fh)
                is False
            )
    finally:
        comment_verdict_residue_io._DIRECTORY_ENUM_BUDGET.reset(token)


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_regular_file_content_samples_into_fstat_errors_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Open-time and post-read fstat OSError paths must fail closed."""
    path = tmp_path / "fstat.bin"
    path.write_bytes(b"abc")
    real_fstat = os.fstat

    hasher = hashlib.sha256()
    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:

        def _boom_once(fd: int) -> os.stat_result:
            raise OSError("fstat boom")

        monkeypatch.setattr(os, "fstat", _boom_once)
        assert (
            comment_verdict_residue_io._hash_regular_file_content_samples_into(hasher, fh) is False
        )
    monkeypatch.setattr(os, "fstat", real_fstat)

    calls = {"n": 0}

    def _boom_after(fd: int) -> os.stat_result:
        calls["n"] += 1
        if calls["n"] == 1:
            return real_fstat(fd)
        raise OSError("fstat after boom")

    hasher2 = hashlib.sha256()
    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        monkeypatch.setattr(os, "fstat", _boom_after)
        assert (
            comment_verdict_residue_io._hash_regular_file_content_samples_into(hasher2, fh) is False
        )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_regular_file_content_samples_into_short_read_fails_closed(
    tmp_path: Path,
) -> None:
    """Short head reads before the open-time size must fail closed."""
    path = tmp_path / "short-sample.bin"
    path.write_bytes(b"abcdef")

    class _ShortReader:
        def __init__(self, fh: BinaryIO) -> None:
            self._fh = fh

        def fileno(self) -> int:
            return self._fh.fileno()

        def read(self, size: int = -1) -> bytes:
            return b""

        def seek(self, offset: int, whence: int = 0) -> int:
            return self._fh.seek(offset, whence)

    hasher = hashlib.sha256()
    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        assert (
            comment_verdict_residue_io._hash_regular_file_content_samples_into(
                hasher, _ShortReader(fh)
            )
            is False
        )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_regular_file_content_samples_into_read_oserror_fails_closed(
    tmp_path: Path,
) -> None:
    """Head-read OSError must fail closed."""
    path = tmp_path / "read-boom.bin"
    path.write_bytes(b"abcdef")

    class _ReadBoom:
        def __init__(self, fh: BinaryIO) -> None:
            self._fh = fh

        def fileno(self) -> int:
            return self._fh.fileno()

        def read(self, size: int = -1) -> bytes:
            raise OSError("read boom")

        def seek(self, offset: int, whence: int = 0) -> int:
            return self._fh.seek(offset, whence)

    hasher = hashlib.sha256()
    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        assert (
            comment_verdict_residue_io._hash_regular_file_content_samples_into(
                hasher, _ReadBoom(fh)
            )
            is False
        )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_regular_file_content_samples_into_tail_errors_fail_closed(
    tmp_path: Path,
) -> None:
    """Tail seek/read failures and short tails must fail closed."""
    path = tmp_path / "tail-boom.bin"
    sample = comment_verdict_residue_io._WORKTREE_REGULAR_HASH_CHUNK_BYTES
    path.write_bytes(b"X" * (sample + 8))

    class _SeekBoom:
        def __init__(self, fh: BinaryIO) -> None:
            self._fh = fh

        def fileno(self) -> int:
            return self._fh.fileno()

        def read(self, size: int = -1) -> bytes:
            return self._fh.read(size)

        def seek(self, offset: int, whence: int = 0) -> int:
            raise OSError("seek boom")

    hasher = hashlib.sha256()
    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        assert (
            comment_verdict_residue_io._hash_regular_file_content_samples_into(
                hasher, _SeekBoom(fh)
            )
            is False
        )

    class _ShortTail:
        def __init__(self, fh: BinaryIO) -> None:
            self._fh = fh
            self._reads = 0

        def fileno(self) -> int:
            return self._fh.fileno()

        def read(self, size: int = -1) -> bytes:
            self._reads += 1
            if self._reads == 1:
                return self._fh.read(size)
            return b""

        def seek(self, offset: int, whence: int = 0) -> int:
            return self._fh.seek(offset, whence)

    hasher2 = hashlib.sha256()
    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        assert (
            comment_verdict_residue_io._hash_regular_file_content_samples_into(
                hasher2, _ShortTail(fh)
            )
            is False
        )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_regular_file_content_samples_into_identity_change_fails_closed(
    tmp_path: Path,
) -> None:
    """Post-sample size change must fail closed."""
    path = tmp_path / "identity.bin"
    path.write_bytes(b"seed")

    class _GrowAfterRead:
        def __init__(self, fh: BinaryIO, target: Path) -> None:
            self._fh = fh
            self._target = target

        def fileno(self) -> int:
            return self._fh.fileno()

        def read(self, size: int = -1) -> bytes:
            payload = self._fh.read(size)
            self._target.write_bytes(b"seed!")
            return payload

        def seek(self, offset: int, whence: int = 0) -> int:
            return self._fh.seek(offset, whence)

    hasher = hashlib.sha256()
    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        assert (
            comment_verdict_residue_io._hash_regular_file_content_samples_into(
                hasher, _GrowAfterRead(fh, path)
            )
            is False
        )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_regular_file_content_samples_into_non_regular_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-regular or negative open-time size must fail closed."""
    path = tmp_path / "nonreg.bin"
    path.write_bytes(b"x")
    real_fstat = os.fstat

    def _dir_mode(fd: int) -> SimpleNamespace:
        result = real_fstat(fd)
        return SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o755,
            st_size=result.st_size,
            st_ino=result.st_ino,
            st_dev=result.st_dev,
        )

    hasher = hashlib.sha256()
    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        monkeypatch.setattr(os, "fstat", _dir_mode)
        assert (
            comment_verdict_residue_io._hash_regular_file_content_samples_into(hasher, fh) is False
        )
    monkeypatch.setattr(os, "fstat", real_fstat)

    def _neg_size(fd: int) -> SimpleNamespace:
        result = real_fstat(fd)
        return SimpleNamespace(
            st_mode=result.st_mode,
            st_size=-1,
            st_ino=result.st_ino,
            st_dev=result.st_dev,
        )

    hasher2 = hashlib.sha256()
    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        monkeypatch.setattr(os, "fstat", _neg_size)
        assert (
            comment_verdict_residue_io._hash_regular_file_content_samples_into(hasher2, fh) is False
        )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_regular_file_content_samples_into_tail_deadline_fails_closed(
    tmp_path: Path,
) -> None:
    """Enum deadline exhaustion between head and tail samples must fail closed."""
    path = tmp_path / "tail-deadline.bin"
    sample = comment_verdict_residue_io._WORKTREE_REGULAR_HASH_CHUNK_BYTES
    path.write_bytes(b"Z" * (sample + 16))
    budget = comment_verdict_residue_io._DirectoryEnumBudget(
        entries_remaining=10,
        deadline=time.monotonic() + 60.0,
        max_depth=8,
    )

    class _ExpireAfterHead:
        def __init__(self, fh: BinaryIO) -> None:
            self._fh = fh

        def fileno(self) -> int:
            return self._fh.fileno()

        def read(self, size: int = -1) -> bytes:
            payload = self._fh.read(size)
            budget.deadline = time.monotonic() - 1.0
            return payload

        def seek(self, offset: int, whence: int = 0) -> int:
            return self._fh.seek(offset, whence)

    token = comment_verdict_residue_io._DIRECTORY_ENUM_BUDGET.set(budget)
    try:
        hasher = hashlib.sha256()
        with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
            assert (
                comment_verdict_residue_io._hash_regular_file_content_samples_into(
                    hasher, _ExpireAfterHead(fh)
                )
                is False
            )
    finally:
        comment_verdict_residue_io._DIRECTORY_ENUM_BUDGET.reset(token)
