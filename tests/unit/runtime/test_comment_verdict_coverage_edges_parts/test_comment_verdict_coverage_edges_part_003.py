"""Focused regressions for late residue-fingerprint gaps (post-#906 audit)."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO

import pytest

from awf.node.git_manager import git_env_without_object_lookup_overrides
from awf.runtime.pr_monitor_runner import (
    comment_verdict_residue,
    comment_verdict_residue_io,
    comment_verdict_residue_nested,
)
from tests.unit.runtime.test_comment_verdict_coverage_edges_parts._helpers import (
    init_git_worktree,
    init_git_worktree_file_replaced_by_directory,
    init_git_worktree_with_embedded_repo,
    init_git_worktree_with_gitfile_embedded_repo,
)

_git_env = git_env_without_object_lookup_overrides


class _NeverEofReader:
    """File-like stand-in that always returns a full chunk (simulates a live appender)."""

    def __init__(self, fh: BinaryIO) -> None:
        self._fh = fh

    def fileno(self) -> int:
        return self._fh.fileno()

    def read(self, size: int = -1) -> bytes:
        n = 65536 if size is None or size < 0 else size
        return b"x" * n


class _AppendAfterRead:
    """File-like stand-in that grows the underlying inode after each successful read."""

    def __init__(self, fh: BinaryIO, path: Path) -> None:
        self._fh = fh
        self._path = path

    def fileno(self) -> int:
        return self._fh.fileno()

    def read(self, size: int = -1) -> bytes:
        data = self._fh.read(size)
        if data:
            with self._path.open("ab") as appender:
                appender.write(b"G" * 64)
        return data


class _OverwriteSameSizeAfterFirstChunk:
    """Overwrite the unread tail in place after the first chunk (same size/inode)."""

    def __init__(self, fh: BinaryIO, path: Path) -> None:
        self._fh = fh
        self._path = path
        self._reads = 0

    def fileno(self) -> int:
        return self._fh.fileno()

    def read(self, size: int = -1) -> bytes:
        data = self._fh.read(size)
        self._reads += 1
        if self._reads == 1 and data:
            total = self._path.stat().st_size
            tail = total - len(data)
            if tail > 0:
                with self._path.open("r+b") as writer:
                    writer.seek(len(data))
                    writer.write(b"B" * tail)
        return data


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_opened_regular_file_into_stable_snapshot(tmp_path: Path) -> None:
    """Bounded regular-file hashing must match the bytes present at open."""
    path = tmp_path / "stable.bin"
    payload = b"hello-residue"
    path.write_bytes(payload)
    hasher = hashlib.sha256()
    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        assert comment_verdict_residue_io._hash_opened_regular_file_into(hasher, fh) is True
    assert hasher.digest() == hashlib.sha256(payload).digest()


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_opened_regular_file_into_never_eof_reader_stays_bounded(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ecabJ: size snapshot must stop a never-EOF appender stand-in."""
    path = tmp_path / "growing.bin"
    path.write_bytes(b"abcd")
    hasher = hashlib.sha256()
    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        wrapped = _NeverEofReader(fh)
        assert comment_verdict_residue_io._hash_opened_regular_file_into(hasher, wrapped) is True
    assert hasher.digest() == hashlib.sha256(b"xxxx").digest()


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_opened_regular_file_into_growth_during_read_fails_closed(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ecabJ: revalidate must fail closed when the inode grows mid-hash."""
    path = tmp_path / "churn.bin"
    path.write_bytes(b"seed")
    hasher = hashlib.sha256()
    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        wrapped = _AppendAfterRead(fh, path)
        assert comment_verdict_residue_io._hash_opened_regular_file_into(hasher, wrapped) is False


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_opened_regular_file_into_same_size_overwrite_fails_closed(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ej31I: same-size in-place overwrite must not accept a torn snapshot."""
    path = tmp_path / "torn.bin"
    chunk = comment_verdict_residue_io._WORKTREE_REGULAR_HASH_CHUNK_BYTES
    path.write_bytes(b"A" * chunk + b"A" * chunk)
    hasher = hashlib.sha256()
    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        wrapped = _OverwriteSameSizeAfterFirstChunk(fh, path)
        assert comment_verdict_residue_io._hash_opened_regular_file_into(hasher, wrapped) is False


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_opened_regular_file_into_short_read_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Short reads before the open-time size must fail closed."""
    path = tmp_path / "short.bin"
    path.write_bytes(b"abcdef")
    hasher = hashlib.sha256()

    class _ShortReader:
        def __init__(self, fh: BinaryIO) -> None:
            self._fh = fh

        def fileno(self) -> int:
            return self._fh.fileno()

        def read(self, size: int = -1) -> bytes:
            return b""

    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        assert (
            comment_verdict_residue_io._hash_opened_regular_file_into(hasher, _ShortReader(fh))
            is False
        )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_opened_regular_file_into_fstat_errors_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fstat failures before or after the snapshot read must fail closed."""
    path = tmp_path / "fstat.bin"
    path.write_bytes(b"data")
    real_fstat = os.fstat

    hasher = hashlib.sha256()
    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        target_fd = fh.fileno()

        def _boom(fd: int) -> os.stat_result:
            if fd == target_fd:
                raise OSError(errno.EBADF, "fstat failed")
            return real_fstat(fd)

        monkeypatch.setattr(os, "fstat", _boom)
        assert comment_verdict_residue_io._hash_opened_regular_file_into(hasher, fh) is False

    monkeypatch.setattr(os, "fstat", real_fstat)

    hasher2 = hashlib.sha256()
    calls = {"n": 0}

    def _fail_after_read(fd: int) -> os.stat_result:
        result = real_fstat(fd)
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError(errno.EBADF, "revalidate fstat failed")
        return result

    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        monkeypatch.setattr(os, "fstat", _fail_after_read)
        assert comment_verdict_residue_io._hash_opened_regular_file_into(hasher2, fh) is False


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_opened_regular_file_into_read_oserror_fails_closed(
    tmp_path: Path,
) -> None:
    """Read errors while consuming the size-bounded snapshot must fail closed."""
    path = tmp_path / "readerr.bin"
    path.write_bytes(b"payload")
    hasher = hashlib.sha256()

    class _ReadBoom:
        def __init__(self, fh: BinaryIO) -> None:
            self._fh = fh

        def fileno(self) -> int:
            return self._fh.fileno()

        def read(self, size: int = -1) -> bytes:
            raise OSError(errno.EIO, "read failed")

    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        assert (
            comment_verdict_residue_io._hash_opened_regular_file_into(hasher, _ReadBoom(fh))
            is False
        )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_opened_regular_file_into_non_regular_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opened inodes that are no longer regular must fail closed."""
    path = tmp_path / "notreg.bin"
    path.write_bytes(b"x")
    hasher = hashlib.sha256()
    real_fstat = os.fstat

    def _dir_mode(fd: int) -> SimpleNamespace:
        result = real_fstat(fd)
        return SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o755,
            st_size=result.st_size,
            st_ino=result.st_ino,
            st_dev=result.st_dev,
        )

    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        monkeypatch.setattr(os, "fstat", _dir_mode)
        assert comment_verdict_residue_io._hash_opened_regular_file_into(hasher, fh) is False


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_opened_regular_file_into_rejects_attacker_sized_sparse_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6edfu4: st_size alone is not a safe bound (truncate/sparse)."""
    monkeypatch.setattr(
        comment_verdict_residue_io,
        "_WORKTREE_REGULAR_HASH_MAX_FILE_BYTES",
        64,
    )
    path = tmp_path / "sparse.bin"
    path.write_bytes(b"")
    os.truncate(path, 65)
    hasher = hashlib.sha256()
    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        assert comment_verdict_residue_io._hash_opened_regular_file_into(hasher, fh) is False


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_opened_regular_file_into_aggregate_byte_budget_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6edfu4: aggregate hash bytes across one fingerprint must cap."""
    monkeypatch.setattr(
        comment_verdict_residue_io,
        "_WORKTREE_REGULAR_HASH_MAX_FILE_BYTES",
        64,
    )
    monkeypatch.setattr(
        comment_verdict_residue_io,
        "_WORKTREE_REGULAR_HASH_AGGREGATE_MAX_BYTES",
        48,
    )
    first = tmp_path / "a.bin"
    second = tmp_path / "b.bin"
    first.write_bytes(b"x" * 32)
    second.write_bytes(b"y" * 32)
    with comment_verdict_residue_io._residue_regular_hash_budget():
        hasher_a = hashlib.sha256()
        with comment_verdict_residue_io._open_worktree_regular_file(first) as fh:
            assert comment_verdict_residue_io._hash_opened_regular_file_into(hasher_a, fh) is True
        hasher_b = hashlib.sha256()
        with comment_verdict_residue_io._open_worktree_regular_file(second) as fh:
            assert comment_verdict_residue_io._hash_opened_regular_file_into(hasher_b, fh) is False


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_opened_regular_file_into_deadline_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6edfu4: wall-time budget must fail closed mid-hash."""
    monkeypatch.setattr(
        comment_verdict_residue_io,
        "_WORKTREE_REGULAR_HASH_CHUNK_BYTES",
        4,
    )
    monkeypatch.setattr(
        comment_verdict_residue_io,
        "_WORKTREE_REGULAR_HASH_BUDGET_SECONDS",
        30.0,
    )
    path = tmp_path / "slow.bin"
    path.write_bytes(b"abcdefghijklmnop")
    clock = {"now": 1000.0}

    def _monotonic() -> float:
        return clock["now"]

    monkeypatch.setattr(comment_verdict_residue_io.time, "monotonic", _monotonic)
    with comment_verdict_residue_io._residue_regular_hash_budget():
        clock["now"] = 1000.0
        hasher = hashlib.sha256()

        class _DeadlineAfterFirstChunk:
            def __init__(self, fh: BinaryIO) -> None:
                self._fh = fh

            def fileno(self) -> int:
                return self._fh.fileno()

            def read(self, size: int = -1) -> bytes:
                data = self._fh.read(size)
                clock["now"] = 1000.0 + 31.0
                return data

        with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
            assert (
                comment_verdict_residue_io._hash_opened_regular_file_into(
                    hasher, _DeadlineAfterFirstChunk(fh)
                )
                is False
            )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_opened_regular_file_into_preexisting_deadline_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deadline already expired before the first read must fail closed."""
    path = tmp_path / "expired.bin"
    path.write_bytes(b"payload")
    clock = {"now": 5000.0}
    monkeypatch.setattr(
        comment_verdict_residue_io.time,
        "monotonic",
        lambda: clock["now"],
    )
    monkeypatch.setattr(
        comment_verdict_residue_io,
        "_WORKTREE_REGULAR_HASH_BUDGET_SECONDS",
        1.0,
    )
    with comment_verdict_residue_io._residue_regular_hash_budget():
        clock["now"] = 5000.0 + 2.0
        hasher = hashlib.sha256()
        with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
            assert comment_verdict_residue_io._hash_opened_regular_file_into(hasher, fh) is False


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_nested_scan_budget_activates_regular_hash_aggregate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fingerprint nested-scan budget must install the regular-file hash caps."""
    monkeypatch.setattr(
        comment_verdict_residue_io,
        "_WORKTREE_REGULAR_HASH_MAX_FILE_BYTES",
        64,
    )
    monkeypatch.setattr(
        comment_verdict_residue_io,
        "_WORKTREE_REGULAR_HASH_AGGREGATE_MAX_BYTES",
        48,
    )
    first = tmp_path / "scan_a.bin"
    second = tmp_path / "scan_b.bin"
    first.write_bytes(b"x" * 32)
    second.write_bytes(b"y" * 32)
    with comment_verdict_residue._residue_fingerprint_nested_scan_budget():
        hasher_a = hashlib.sha256()
        with comment_verdict_residue_io._open_worktree_regular_file(first) as fh:
            assert comment_verdict_residue_io._hash_opened_regular_file_into(hasher_a, fh) is True
        hasher_b = hashlib.sha256()
        with comment_verdict_residue_io._open_worktree_regular_file(second) as fh:
            assert comment_verdict_residue_io._hash_opened_regular_file_into(hasher_b, fh) is False


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_residue_regular_hash_budget_nested_reentry_preserves_outer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-entering the hash budget must not reset the outer aggregate counter."""
    monkeypatch.setattr(
        comment_verdict_residue_io,
        "_WORKTREE_REGULAR_HASH_MAX_FILE_BYTES",
        64,
    )
    monkeypatch.setattr(
        comment_verdict_residue_io,
        "_WORKTREE_REGULAR_HASH_AGGREGATE_MAX_BYTES",
        48,
    )
    first = tmp_path / "outer.bin"
    second = tmp_path / "inner.bin"
    first.write_bytes(b"x" * 32)
    second.write_bytes(b"y" * 32)
    with comment_verdict_residue_io._residue_regular_hash_budget():
        hasher_a = hashlib.sha256()
        with comment_verdict_residue_io._open_worktree_regular_file(first) as fh:
            assert comment_verdict_residue_io._hash_opened_regular_file_into(hasher_a, fh) is True
        with comment_verdict_residue_io._residue_regular_hash_budget():
            hasher_b = hashlib.sha256()
            with comment_verdict_residue_io._open_worktree_regular_file(second) as fh:
                assert (
                    comment_verdict_residue_io._hash_opened_regular_file_into(hasher_b, fh) is False
                )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_nested_scan_budget_nested_reentry_keeps_single_hash_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inner nested-scan scopes must reuse the outermost regular-hash budget."""
    monkeypatch.setattr(
        comment_verdict_residue_io,
        "_WORKTREE_REGULAR_HASH_MAX_FILE_BYTES",
        64,
    )
    monkeypatch.setattr(
        comment_verdict_residue_io,
        "_WORKTREE_REGULAR_HASH_AGGREGATE_MAX_BYTES",
        48,
    )
    first = tmp_path / "nest_a.bin"
    second = tmp_path / "nest_b.bin"
    first.write_bytes(b"x" * 32)
    second.write_bytes(b"y" * 32)
    with comment_verdict_residue._residue_fingerprint_nested_scan_budget():
        hasher_a = hashlib.sha256()
        with comment_verdict_residue_io._open_worktree_regular_file(first) as fh:
            assert comment_verdict_residue_io._hash_opened_regular_file_into(hasher_a, fh) is True
        with comment_verdict_residue._residue_fingerprint_nested_scan_budget():
            hasher_b = hashlib.sha256()
            with comment_verdict_residue_io._open_worktree_regular_file(second) as fh:
                assert (
                    comment_verdict_residue_io._hash_opened_regular_file_into(hasher_b, fh) is False
                )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_worktree_directory_residue_wide_empty_tree_entry_budget_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6eeAsN: wide empty directory trees must hit the entry budget."""
    monkeypatch.setattr(
        comment_verdict_residue_io,
        "_WORKTREE_DIRECTORY_ENUM_AGGREGATE_MAX_ENTRIES",
        8,
    )
    worktree = tmp_path / "ws_wide_empty"
    worktree.mkdir()
    init_git_worktree_file_replaced_by_directory(worktree)
    target = worktree / "src" / "x.py"
    for child in target.iterdir():
        if child.is_file():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    for index in range(20):
        (target / f"empty_{index:02d}").mkdir()

    result = comment_verdict_residue._hash_worktree_directory_residue(
        worktree_path=worktree,
        path="src/x.py",
        git_env=_git_env,
    )

    assert result is None


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_worktree_directory_residue_deep_empty_tree_depth_budget_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6eeAsN: deep empty directory chains must hit the depth budget."""
    monkeypatch.setattr(
        comment_verdict_residue_io,
        "_WORKTREE_DIRECTORY_ENUM_MAX_DEPTH",
        3,
    )
    worktree = tmp_path / "ws_deep_empty"
    worktree.mkdir()
    init_git_worktree_file_replaced_by_directory(worktree)
    target = worktree / "src" / "x.py"
    for child in target.iterdir():
        if child.is_file():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    cursor = target
    for index in range(8):
        cursor = cursor / f"d{index}"
        cursor.mkdir()

    result = comment_verdict_residue._hash_worktree_directory_residue(
        worktree_path=worktree,
        path="src/x.py",
        git_env=_git_env,
    )

    assert result is None


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_worktree_directory_residue_enum_deadline_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6eeAsN: directory enumeration must honor wall-time budget."""
    monkeypatch.setattr(
        comment_verdict_residue_io,
        "_WORKTREE_DIRECTORY_ENUM_BUDGET_SECONDS",
        30.0,
    )
    worktree = tmp_path / "ws_enum_deadline"
    worktree.mkdir()
    init_git_worktree_file_replaced_by_directory(worktree)
    clock = {"now": 1000.0}

    def _monotonic() -> float:
        return clock["now"]

    monkeypatch.setattr(comment_verdict_residue_io.time, "monotonic", _monotonic)
    with comment_verdict_residue_io._residue_directory_enum_budget():
        clock["now"] = 1000.0 + 31.0
        result = comment_verdict_residue._hash_worktree_directory_residue(
            worktree_path=worktree,
            path="src/x.py",
            git_env=_git_env,
        )

    assert result is None


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_nested_scan_budget_installs_directory_enum_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fingerprint nested-scan budget must install directory enumeration caps."""
    monkeypatch.setattr(
        comment_verdict_residue_io,
        "_WORKTREE_DIRECTORY_ENUM_AGGREGATE_MAX_ENTRIES",
        8,
    )
    worktree = tmp_path / "ws_scan_dir_budget"
    worktree.mkdir()
    init_git_worktree_file_replaced_by_directory(worktree)
    target = worktree / "src" / "x.py"
    for child in target.iterdir():
        if child.is_file():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    for index in range(20):
        (target / f"empty_{index:02d}").mkdir()

    with (
        comment_verdict_residue._residue_fingerprint_nested_scan_budget(),
        comment_verdict_residue._residue_fingerprint_nested_scan_budget(),
    ):
        # Nested reentry must reuse the outermost enum budget.
        result = comment_verdict_residue._hash_worktree_directory_residue(
            worktree_path=worktree,
            path="src/x.py",
            git_env=_git_env,
        )

    assert result is None


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_digest_worktree_entry_bytes_at_never_eof_reader_stays_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Directory-entry regular hashing must not hang on a live appender stand-in."""
    worktree = tmp_path / "ws_dir_growing"
    worktree.mkdir()
    init_git_worktree_file_replaced_by_directory(worktree)
    child = worktree / "src" / "x.py" / "child.txt"
    child.write_bytes(b"base\n")

    real_open_at = comment_verdict_residue._open_worktree_regular_file_at

    @contextlib.contextmanager
    def _open_never_eof(dir_fd: int, name: str) -> Iterator[BinaryIO]:
        with real_open_at(dir_fd, name) as fh:
            yield _NeverEofReader(fh)  # type: ignore[misc]

    monkeypatch.setattr(
        comment_verdict_residue,
        "_open_worktree_regular_file_at",
        _open_never_eof,
    )

    result = comment_verdict_residue._hash_worktree_directory_residue(
        worktree_path=worktree,
        path="src/x.py",
        git_env=_git_env,
    )

    assert result is not None


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_digest_worktree_entry_bytes_growth_during_hash_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path-based regular hashing must fail closed when the inode grows mid-hash."""
    worktree = tmp_path / "ws_path_growing"
    worktree.mkdir()
    init_git_worktree(worktree)
    target = worktree / "src" / "x.py"
    target.write_bytes(b"seed")

    real_open = comment_verdict_residue._open_worktree_regular_file_under_root

    @contextlib.contextmanager
    def _open_growing(
        root: Path,
        path: str,
        *,
        root_dir_fd: int | None = None,
    ) -> Iterator[BinaryIO]:
        target = root / path
        with real_open(root, path, root_dir_fd=root_dir_fd) as fh:
            yield _AppendAfterRead(fh, target)  # type: ignore[misc]

    monkeypatch.setattr(
        comment_verdict_residue,
        "_open_worktree_regular_file_under_root",
        _open_growing,
    )

    result = comment_verdict_residue._digest_worktree_entry_bytes(
        worktree_path=worktree,
        path="src/x.py",
        git_env=_git_env,
    )

    assert result is None


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_git_worktree_blob_sha_regular_file_never_eof_reader_stays_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6ef8Fm: outer blob SHA must not hang on a never-EOF appender."""
    worktree = tmp_path / "ws_blob_never_eof"
    worktree.mkdir()
    init_git_worktree(worktree)
    target = worktree / "src" / "x.py"
    target.write_bytes(b"abcd")

    real_open = comment_verdict_residue._open_worktree_regular_file_under_root

    @contextlib.contextmanager
    def _open_never_eof(
        root: Path,
        path: str,
        *,
        root_dir_fd: int | None = None,
    ) -> Iterator[BinaryIO]:
        with real_open(root, path, root_dir_fd=root_dir_fd) as fh:
            yield _NeverEofReader(fh)  # type: ignore[misc]

    monkeypatch.setattr(
        comment_verdict_residue,
        "_open_worktree_regular_file_under_root",
        _open_never_eof,
    )

    sha = comment_verdict_residue._git_worktree_blob_sha(
        worktree_path=worktree,
        path="src/x.py",
        git_env=_git_env(),
    )
    expected = (
        subprocess.run(
            ["git", "hash-object", "--stdin"],
            input=b"xxxx",
            capture_output=True,
            check=True,
            cwd=worktree,
        )
        .stdout.decode()
        .strip()
    )
    assert sha == expected


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_git_worktree_blob_sha_regular_file_growth_during_snapshot_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6ef8Fm: mid-snapshot growth must fail closed, not hang."""
    worktree = tmp_path / "ws_blob_growing"
    worktree.mkdir()
    init_git_worktree(worktree)
    target = worktree / "src" / "x.py"
    target.write_bytes(b"seed")

    real_open = comment_verdict_residue._open_worktree_regular_file_under_root

    @contextlib.contextmanager
    def _open_growing(
        root: Path,
        path: str,
        *,
        root_dir_fd: int | None = None,
    ) -> Iterator[BinaryIO]:
        leaf = root / path
        with real_open(root, path, root_dir_fd=root_dir_fd) as fh:
            yield _AppendAfterRead(fh, leaf)  # type: ignore[misc]

    monkeypatch.setattr(
        comment_verdict_residue,
        "_open_worktree_regular_file_under_root",
        _open_growing,
    )

    assert (
        comment_verdict_residue._git_worktree_blob_sha(
            worktree_path=worktree,
            path="src/x.py",
            git_env=_git_env(),
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_digest_worktree_entry_bytes_regular_classified_fifo_fails_closed_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6eVygp: reopen after lstat must not block on a swapped FIFO."""
    worktree = tmp_path / "ws_fifo_toctou"
    worktree.mkdir()
    init_git_worktree(worktree)
    fifo_path = worktree / "src" / "x.py"
    fifo_path.unlink()
    os.mkfifo(fifo_path, mode=0o644)

    real_kind = comment_verdict_residue._worktree_entry_kind

    def _regular_then_fifo(candidate: Path) -> tuple[str, int] | None:
        info = real_kind(candidate)
        if info is not None and info[0] == "fifo":
            return ("regular", 0o100644)
        return info

    monkeypatch.setattr(comment_verdict_residue, "_worktree_entry_kind", _regular_then_fifo)

    result = comment_verdict_residue._digest_worktree_entry_bytes(
        worktree_path=worktree,
        path="src/x.py",
        git_env=_git_env,
    )

    assert result is None


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_worktree_directory_residue_directory_to_symlink_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6eXOzE: directory scandir must not follow a swapped symlink."""
    worktree = tmp_path / "ws_dir_toctou"
    worktree.mkdir()
    init_git_worktree_file_replaced_by_directory(worktree)
    candidate = worktree / "src" / "x.py"

    real_kind = comment_verdict_residue._worktree_entry_kind

    def _directory_then_symlink(path: Path) -> tuple[str, int] | None:
        info = real_kind(path)
        if info is None or path != candidate:
            return info
        if info[0] == "directory":
            backup = path.parent / f"{path.name}.bak"
            path.rename(backup)
            outside = tmp_path / "outside"
            outside.mkdir(exist_ok=True)
            (outside / "child.txt").write_text("evil\n", encoding="utf-8")
            path.symlink_to(outside)
            return ("directory", 0o040755)
        if info[0] == "symlink":
            return ("directory", 0o040755)
        return info

    monkeypatch.setattr(comment_verdict_residue, "_worktree_entry_kind", _directory_then_symlink)

    result = comment_verdict_residue._hash_worktree_directory_residue(
        worktree_path=worktree,
        path="src/x.py",
        git_env=_git_env,
    )

    assert result is None


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_worktree_directory_residue_child_digest_uses_dir_fd_not_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Directory child hashing must not re-enter the tree by pathname after open."""
    worktree = tmp_path / "ws_dir_child_fd"
    worktree.mkdir()
    init_git_worktree_file_replaced_by_directory(worktree)

    def _forbid_path_digest(**kwargs: object) -> bytes:
        raise AssertionError("directory walk must not call path-based _digest_worktree_entry_bytes")

    monkeypatch.setattr(
        comment_verdict_residue,
        "_digest_worktree_entry_bytes",
        _forbid_path_digest,
    )

    result = comment_verdict_residue._hash_worktree_directory_residue(
        worktree_path=worktree,
        path="src/x.py",
        git_env=_git_env,
    )

    assert result is not None


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_worktree_directory_residue_nested_git_uses_dir_fd_not_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nested-repo probes inside a pinned directory must not re-enter by pathname."""
    worktree = tmp_path / "ws_dir_nested_fd"
    worktree.mkdir()
    init_git_worktree(worktree)
    target = worktree / "src" / "x.py"
    target.unlink()
    replacement = worktree / "src" / "x.py"
    replacement.mkdir()
    nested = replacement / "embedded"
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
    (nested / "inner.txt").write_text("inner\n", encoding="utf-8")
    subprocess.run(["git", "add", "inner.txt"], cwd=nested, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=nested, check=True, capture_output=True)

    def _forbid_path_nested(**kwargs: object) -> str:
        raise AssertionError("directory walk must not call path-based _git_nested_worktree_commit")

    monkeypatch.setattr(
        comment_verdict_residue,
        "_git_nested_worktree_commit",
        _forbid_path_nested,
    )

    result = comment_verdict_residue._hash_worktree_directory_residue(
        worktree_path=worktree,
        path="src/x.py",
        git_env=_git_env(),
    )

    assert result is not None


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_digest_worktree_entry_bytes_nested_git_uses_dir_fd_not_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6eXjoh: top-level nested-repo probes must not re-enter by pathname."""
    worktree = tmp_path / "ws_top_level_nested_fd"
    worktree.mkdir()
    nested_path = init_git_worktree_with_embedded_repo(worktree)

    def _forbid_path_git_dir(_nested_root: Path) -> Path | None:
        raise AssertionError(
            "top-level nested-git digest must not call path-based _nested_git_probe_git_dir"
        )

    monkeypatch.setattr(
        comment_verdict_residue_nested,
        "_nested_git_probe_git_dir",
        _forbid_path_git_dir,
    )

    result = comment_verdict_residue._digest_worktree_entry_bytes(
        worktree_path=worktree,
        path=nested_path,
        git_env=_git_env(),
    )

    assert result is not None


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_git_nested_worktree_commit_at_keeps_proc_fd_path_for_git_probes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6eXrkh: nested git probes must stay on /proc/self/fd/<fd>, not readlink."""
    worktree = tmp_path / "ws_nested_proc_fd"
    worktree.mkdir()
    nested_name = init_git_worktree_with_embedded_repo(worktree, nested_name="vendor")

    before_swap = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_name,
        git_env=_git_env(),
    )
    assert before_swap is not None

    captured_roots: list[Path] = []
    real_probe_root = comment_verdict_residue._nested_git_probe_worktree_root

    def _capture_probe_root(**kwargs: object) -> Path | None:
        nested_root = kwargs["nested_root"]
        assert isinstance(nested_root, Path)
        captured_roots.append(nested_root)
        return real_probe_root(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        comment_verdict_residue,
        "_nested_git_probe_worktree_root",
        _capture_probe_root,
    )

    with comment_verdict_residue._open_worktree_directory(worktree, nested_name) as dir_fd:
        pinned_path = Path(f"/proc/self/fd/{dir_fd}").readlink()
        backup = pinned_path.parent / f"{pinned_path.name}.bak"
        pinned_path.rename(backup)

        evil = tmp_path / "evil_nested"
        evil.mkdir()
        subprocess.run(["git", "init"], cwd=evil, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=evil,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=evil,
            check=True,
            capture_output=True,
        )
        (evil / "evil.txt").write_text("evil\n", encoding="utf-8")
        subprocess.run(["git", "add", "evil.txt"], cwd=evil, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "evil"], cwd=evil, check=True, capture_output=True)
        evil.rename(pinned_path)

        after_swap = comment_verdict_residue._git_nested_worktree_commit_at(
            dir_fd=dir_fd,
            git_env=_git_env(),
            outer_worktree_path=worktree,
        )

    # Single post-snapshot discovery call (PRRT_kwDOSJAM6s6ewpcq); pre-snapshot
    # rev-parse against live config was removed.
    assert captured_roots == [backup]
    assert after_swap == before_swap


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_nested_config_snapshot_uses_retained_dir_fd_not_stale_pathname(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6eqQgs: snapshot via /proc/self/fd/<fd>, not a mutable pathname.

    A rename + decoy after pathname refresh must not divert config snapshot /
    residue digests onto the decoy while the held dir_fd still points at the
    original embedded repository.
    """
    worktree = tmp_path / "ws_nested_snapshot_fd"
    worktree.mkdir()
    nested_name = init_git_worktree_with_embedded_repo(worktree, nested_name="vendor")

    before = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_name,
        git_env=_git_env(),
    )
    assert before is not None

    decoy_src = tmp_path / "decoy_src"
    decoy_src.mkdir()
    subprocess.run(["git", "init"], cwd=decoy_src, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=decoy_src,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=decoy_src,
        check=True,
        capture_output=True,
    )
    (decoy_src / "decoy.txt").write_text("decoy\n", encoding="utf-8")
    subprocess.run(["git", "add", "decoy.txt"], cwd=decoy_src, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "decoy"], cwd=decoy_src, check=True, capture_output=True)

    # Measure the fingerprint a pathname-following snapshot would attribute to the decoy.
    nested_path = worktree / nested_name
    backup_for_measure = worktree / f"{nested_name}.measure"
    nested_path.rename(backup_for_measure)
    decoy_src.rename(nested_path)
    decoy_fp = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_name,
        git_env=_git_env(),
    )
    nested_path.rename(decoy_src)
    backup_for_measure.rename(nested_path)
    assert decoy_fp is not None
    assert decoy_fp != before

    real_snapshot = comment_verdict_residue.untrusted_nested_probe_config_snapshot_git_dir
    captured_roots: list[Path] = []
    swapped = {"done": False}

    @contextlib.contextmanager
    def _swap_pathname_then_snapshot(
        nested_root: Path,
        **kwargs: object,
    ) -> Iterator[Path | None]:
        captured_roots.append(nested_root)
        assert str(nested_root).startswith("/proc/self/fd/"), nested_root
        if not swapped["done"]:
            pinned = nested_root.readlink()
            backup = pinned.parent / f"{pinned.name}.bak"
            pinned.rename(backup)
            decoy_src.rename(pinned)
            # Correction residue on the original inode must remain visible.
            (backup / "mutation.txt").write_text("mutated\n", encoding="utf-8")
            swapped["done"] = True
        with real_snapshot(nested_root, **kwargs) as shadow:  # type: ignore[arg-type]
            yield shadow

    monkeypatch.setattr(
        comment_verdict_residue,
        "untrusted_nested_probe_config_snapshot_git_dir",
        _swap_pathname_then_snapshot,
    )

    with comment_verdict_residue._open_worktree_directory(worktree, nested_name) as dir_fd:
        after = comment_verdict_residue._git_nested_worktree_commit_at(
            dir_fd=dir_fd,
            git_env=_git_env(),
            outer_worktree_path=worktree,
        )

    assert swapped["done"] is True
    assert len(captured_roots) == 1
    assert str(captured_roots[0]) == f"/proc/self/fd/{captured_roots[0].name}"
    assert after is not None
    assert after != decoy_fp
    assert after != before


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_git_nested_worktree_commit_at_pins_git_dir_marker_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6eXrkk: nested git-dir probes must pin the opened ``.git`` directory fd."""
    worktree = tmp_path / "ws_nested_git_marker_fd"
    worktree.mkdir()
    nested_name = init_git_worktree_with_embedded_repo(worktree, nested_name="vendor")

    before_swap = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_name,
        git_env=_git_env(),
    )
    assert before_swap is not None
    before_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree / nested_name,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    evil_repo = tmp_path / "evil"
    evil_repo.mkdir()
    subprocess.run(["git", "init"], cwd=evil_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=evil_repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=evil_repo,
        check=True,
        capture_output=True,
    )
    (evil_repo / "evil.txt").write_text("evil\n", encoding="utf-8")
    subprocess.run(["git", "add", "evil.txt"], cwd=evil_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "evil"], cwd=evil_repo, check=True, capture_output=True)
    evil_git = evil_repo / ".git"

    captured_git_dirs: list[Path] = []
    captured_cmds: list[list[str]] = []
    real_pinned_probe = comment_verdict_residue._pinned_nested_git_probe
    real_git_cmd = comment_verdict_residue._git_command_for_residue_probe

    def _capture_git_cmd(worktree_path: Path, *args: str) -> list[str]:
        cmd = real_git_cmd(worktree_path, *args)
        captured_cmds.append(cmd)
        return cmd

    @contextlib.contextmanager
    def _capture_git_dir_pin(git_dir: Path, worktree_path: Path) -> Iterator[None]:
        captured_git_dirs.append(git_dir)
        with real_pinned_probe(git_dir, worktree_path):
            yield

    monkeypatch.setattr(
        comment_verdict_residue,
        "_pinned_nested_git_probe",
        _capture_git_dir_pin,
    )
    monkeypatch.setattr(
        comment_verdict_residue,
        "_git_command_for_residue_probe",
        _capture_git_cmd,
    )

    real_open = os.open
    swap_done = False
    git_marker_dir_opens = {"n": 0}

    def _open_swap_git_marker(
        name: str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swap_done
        # shutil.rmtree and other callers may pass dir_fd=None explicitly.
        if dir_fd is not None and dir_fd >= 0:
            opened = real_open(name, flags, mode, dir_fd=dir_fd)
        else:
            opened = real_open(name, flags, mode)
        if (
            not swap_done
            and dir_fd is not None
            and dir_fd >= 0
            and name == ".git"
            and flags & os.O_DIRECTORY
        ):
            # Snapshot openat()s ``.git`` before the pin path (PRRT_kwDOSJAM6s6evMAl).
            # Swap after the pin open so the retained pin fd stays on the original.
            git_marker_dir_opens["n"] += 1
            if git_marker_dir_opens["n"] < 2:
                return opened
            proc_root = Path(f"/proc/self/fd/{dir_fd}").readlink()
            git_path = proc_root / ".git"
            backup = proc_root / ".git.real"
            git_path.rename(backup)
            git_path.symlink_to(evil_git)
            swap_done = True
        return opened

    monkeypatch.setattr(os, "open", _open_swap_git_marker)

    with comment_verdict_residue._open_worktree_directory(worktree, nested_name) as dir_fd:
        assert (
            comment_verdict_residue._git_nested_worktree_commit_at(
                dir_fd=dir_fd,
                git_env=_git_env(),
                outer_worktree_path=worktree,
            )
            is not None
        )

    assert swap_done
    assert len(captured_git_dirs) == 1
    assert str(captured_git_dirs[0]).endswith(".git.real")
    pinned_git_dirs = [
        cmd[cmd.index("--git-dir") + 1] for cmd in captured_cmds if "--git-dir" in cmd
    ]
    assert pinned_git_dirs
    assert all(Path(path).resolve() != evil_git.resolve() for path in pinned_git_dirs)
    head_cmds = [cmd for cmd in captured_cmds if cmd[-1:] == ["HEAD"] and "rev-parse" in cmd]
    assert len(head_cmds) == 1
    # Snapshot staging ``--git-dir`` is removed when the probe ends; verify HEAD
    # via the pinned opened git-dir path that survived the TOCTOU rename.
    after_head = subprocess.run(
        [
            "git",
            "--git-dir",
            str(captured_git_dirs[0]),
            "--work-tree",
            str(worktree / nested_name),
            "rev-parse",
            "HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=dict(_git_env()),
    ).stdout.strip()
    assert after_head == before_head


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_git_nested_worktree_commit_at_pins_gitfile_target_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6eX7EK: nested gitfile targets must pin the opened git-dir fd."""
    worktree = tmp_path / "ws_nested_gitfile_target_fd"
    worktree.mkdir()
    nested_name = init_git_worktree_with_gitfile_embedded_repo(
        worktree,
        nested_name="vendor",
        git_dir_name=".vendor_git",
    )

    before_swap = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_name,
        git_env=_git_env(),
    )
    assert before_swap is not None
    before_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree / nested_name,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    evil_repo = tmp_path / "evil"
    evil_repo.mkdir()
    subprocess.run(["git", "init"], cwd=evil_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=evil_repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=evil_repo,
        check=True,
        capture_output=True,
    )
    (evil_repo / "evil.txt").write_text("evil\n", encoding="utf-8")
    subprocess.run(["git", "add", "evil.txt"], cwd=evil_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "evil"], cwd=evil_repo, check=True, capture_output=True)
    evil_git = evil_repo / ".git"

    captured_git_dirs: list[Path] = []
    captured_cmds: list[list[str]] = []
    real_pinned_probe = comment_verdict_residue._pinned_nested_git_probe
    real_git_cmd = comment_verdict_residue._git_command_for_residue_probe

    def _capture_git_cmd(worktree_path: Path, *args: str) -> list[str]:
        cmd = real_git_cmd(worktree_path, *args)
        captured_cmds.append(cmd)
        return cmd

    @contextlib.contextmanager
    def _capture_git_dir_pin(git_dir: Path, worktree_path: Path) -> Iterator[None]:
        captured_git_dirs.append(git_dir)
        with real_pinned_probe(git_dir, worktree_path):
            yield

    monkeypatch.setattr(
        comment_verdict_residue,
        "_pinned_nested_git_probe",
        _capture_git_dir_pin,
    )
    monkeypatch.setattr(
        comment_verdict_residue,
        "_git_command_for_residue_probe",
        _capture_git_cmd,
    )

    real_open = os.open
    swap_done = False
    vendor_git_dir_opens = {"n": 0}

    def _open_swap_gitfile_target(
        name: str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swap_done
        # shutil.rmtree and other callers may pass dir_fd=None explicitly.
        if dir_fd is not None and dir_fd >= 0:
            opened = real_open(name, flags, mode, dir_fd=dir_fd)
        else:
            opened = real_open(name, flags, mode)
        if (
            not swap_done
            and dir_fd is not None
            and dir_fd >= 0
            and name == ".vendor_git"
            and flags & os.O_DIRECTORY
        ):
            # Snapshot now openat()s the gitfile target before the pin path
            # (PRRT_kwDOSJAM6s6evMAl). Swap after the pin open so the retained
            # pin fd still refers to the original git-dir inode.
            vendor_git_dir_opens["n"] += 1
            if vendor_git_dir_opens["n"] < 2:
                return opened
            proc_target = Path(f"/proc/self/fd/{opened}").readlink()
            backup = proc_target.parent / f"{proc_target.name}.real"
            proc_target.rename(backup)
            proc_target.symlink_to(evil_git)
            swap_done = True
        return opened

    monkeypatch.setattr(os, "open", _open_swap_gitfile_target)

    with comment_verdict_residue._open_worktree_directory(worktree, nested_name) as dir_fd:
        assert (
            comment_verdict_residue._git_nested_worktree_commit_at(
                dir_fd=dir_fd,
                git_env=_git_env(),
                outer_worktree_path=worktree,
            )
            is not None
        )

    assert swap_done
    assert len(captured_git_dirs) == 1
    assert str(captured_git_dirs[0]).endswith(".vendor_git.real")
    pinned_git_dirs = [
        cmd[cmd.index("--git-dir") + 1] for cmd in captured_cmds if "--git-dir" in cmd
    ]
    assert pinned_git_dirs
    assert all(Path(path).resolve() != evil_git.resolve() for path in pinned_git_dirs)
    head_cmds = [cmd for cmd in captured_cmds if cmd[-1:] == ["HEAD"] and "rev-parse" in cmd]
    assert len(head_cmds) == 1
    # Snapshot staging ``--git-dir`` is removed when the probe ends; verify HEAD
    # via the pinned opened git-dir path that survived the TOCTOU rename.
    after_head = subprocess.run(
        [
            "git",
            "--git-dir",
            str(captured_git_dirs[0]),
            "--work-tree",
            str(worktree / nested_name),
            "rev-parse",
            "HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=dict(_git_env()),
    ).stdout.strip()
    assert after_head == before_head
