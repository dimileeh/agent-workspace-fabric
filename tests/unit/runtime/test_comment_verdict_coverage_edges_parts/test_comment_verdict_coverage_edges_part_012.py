"""Ignored-dir overflow content-identity regressions (part 12)."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO

import pytest

from awf.runtime.pr_monitor_runner import comment_verdict_residue_io
from tests.unit.runtime.test_comment_verdict_coverage_edges_parts._helpers import (
    init_git_worktree_with_gitfile_embedded_repo,
)


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_regular_file_content_samples_into_hashes_small_file(tmp_path: Path) -> None:
    """Small files are fully folded into the hasher."""
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
def test_hash_regular_file_content_samples_into_hashes_full_multi_chunk(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6e65b4: multi-chunk files hash every byte, not head/tail only."""
    path = tmp_path / "wide.bin"
    sample = comment_verdict_residue_io._WORKTREE_REGULAR_HASH_CHUNK_BYTES
    payload = b"H" * sample + b"M" * sample + b"T" * sample
    path.write_bytes(payload)
    hasher = hashlib.sha256()
    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        assert (
            comment_verdict_residue_io._hash_regular_file_content_samples_into(hasher, fh) is True
        )
    assert hasher.digest() == hashlib.sha256(payload).digest()


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_regular_file_content_samples_into_detects_middle_only_edit(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6e65b4: same-size middle overwrite must change identity."""
    path = tmp_path / "middle.bin"
    sample = comment_verdict_residue_io._WORKTREE_REGULAR_HASH_CHUNK_BYTES
    baseline = b"H" * sample + b"M" * sample + b"T" * sample
    path.write_bytes(baseline)
    hasher_a = hashlib.sha256()
    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        assert (
            comment_verdict_residue_io._hash_regular_file_content_samples_into(hasher_a, fh) is True
        )
    mutated = b"H" * sample + b"X" * sample + b"T" * sample
    st = path.stat()
    path.write_bytes(mutated)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    hasher_b = hashlib.sha256()
    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        assert (
            comment_verdict_residue_io._hash_regular_file_content_samples_into(hasher_b, fh) is True
        )
    assert hasher_a.digest() != hasher_b.digest()
    assert hasher_b.digest() == hashlib.sha256(mutated).digest()


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_regular_file_content_samples_into_overlapping_suffix(tmp_path: Path) -> None:
    """Files spanning more than one chunk still fold the full body."""
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
def test_hash_regular_file_content_samples_into_oversized_uses_head_tail(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6e7oIu: oversized overflow files use bounded head/tail samples."""
    path = tmp_path / "huge.bin"
    sample = comment_verdict_residue_io._WORKTREE_REGULAR_HASH_CHUNK_BYTES
    oversize = comment_verdict_residue_io._WORKTREE_REGULAR_HASH_MAX_FILE_BYTES + sample
    payload = b"H" * sample + b"M" * (oversize - 2 * sample) + b"T" * sample
    path.write_bytes(payload)
    hasher = hashlib.sha256()
    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        assert (
            comment_verdict_residue_io._hash_regular_file_content_samples_into(hasher, fh) is True
        )
    expected = hashlib.sha256()
    expected.update(b"reg-oversized-head-tail\0")
    expected.update(payload[:sample])
    expected.update(payload[-sample:])
    assert hasher.digest() == expected.digest()


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_regular_file_content_samples_into_oversized_short_read_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inconsistent open-time size vs readable bytes must fail closed."""
    path = tmp_path / "huge.bin"
    path.write_bytes(b"x")
    real_fstat = os.fstat
    oversize = comment_verdict_residue_io._WORKTREE_REGULAR_HASH_MAX_FILE_BYTES + 1

    def _oversize(fd: int) -> SimpleNamespace:
        result = real_fstat(fd)
        return SimpleNamespace(
            st_mode=result.st_mode,
            st_size=oversize,
            st_ino=result.st_ino,
            st_dev=result.st_dev,
        )

    hasher = hashlib.sha256()
    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        monkeypatch.setattr(os, "fstat", _oversize)
        assert (
            comment_verdict_residue_io._hash_regular_file_content_samples_into(hasher, fh) is False
        )


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
    """Short reads before the open-time size must fail closed."""
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
    """Read OSError must fail closed."""
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
def test_hash_regular_file_content_samples_into_midstream_deadline_fails_closed(
    tmp_path: Path,
) -> None:
    """Enum deadline exhaustion between content chunks must fail closed."""
    path = tmp_path / "mid-deadline.bin"
    sample = comment_verdict_residue_io._WORKTREE_REGULAR_HASH_CHUNK_BYTES
    path.write_bytes(b"Z" * (sample + 16))
    budget = comment_verdict_residue_io._DirectoryEnumBudget(
        entries_remaining=10,
        deadline=time.monotonic() + 60.0,
        max_depth=8,
    )

    class _ExpireAfterFirstChunk:
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
                    hasher, _ExpireAfterFirstChunk(fh)
                )
                is False
            )
    finally:
        comment_verdict_residue_io._DIRECTORY_ENUM_BUDGET.reset(token)


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
def test_hash_regular_file_content_samples_into_zero_chunk_fails_closed(
    tmp_path: Path,
) -> None:
    """Non-positive chunk size must fail closed before reading."""
    path = tmp_path / "zero-chunk.bin"
    path.write_bytes(b"abc")
    hasher = hashlib.sha256()
    with comment_verdict_residue_io._open_worktree_regular_file(path) as fh:
        assert (
            comment_verdict_residue_io._hash_regular_file_content_samples_into(
                hasher, fh, sample_bytes=0
            )
            is False
        )


@pytest.mark.unit
def test_restore_item_start_reconnects_nested_gitfile_linkage(tmp_path: Path) -> None:
    """PRRT_kwDOSJAM6s6e65b_: nested gitfile retarget must be restored on rollback.

    Config restore alone rewrites the original nested git-dir paths while the
    nested checkout ``.git`` marker still points at a replacement store that
    shares HEAD/config, so parent cleanup can look clean while later probes
    inside the nested checkout follow attacker refs/hooks/remotes.
    """
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_nested_linkage"
    worktree.mkdir()
    nested_name = init_git_worktree_with_gitfile_embedded_repo(worktree, nested_name="vendor")
    nested = worktree / nested_name
    original_gitfile = (nested / ".git").read_text(encoding="utf-8")
    trusted_git_dir = fp_mod._resolve_gitfile_target(nested, original_gitfile)
    assert trusted_git_dir is not None
    nested_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=nested,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert fp_mod.remember_item_start_local_git_configs(worktree) is True
    key = str(worktree.resolve())
    assert key in fp_mod._ITEM_START_NESTED_GIT_LINKAGES
    assert str(nested.resolve()) in fp_mod._ITEM_START_NESTED_GIT_LINKAGES[key]

    evil_checkout = tmp_path / "evil_checkout"
    evil_store = tmp_path / "evil_nested.git"
    subprocess.run(
        ["git", "clone", "--separate-git-dir", str(evil_store), str(nested), str(evil_checkout)],
        check=True,
        capture_output=True,
    )
    # Match trusted local config so a key-only swap still looks metadata-clean.
    (evil_store / "config").write_text(
        (trusted_git_dir / "config").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (nested / ".git").write_text(f"gitdir: {evil_store.resolve()}\n", encoding="utf-8")
    swapped_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=nested,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert swapped_head.lower() == nested_head.lower()

    assert fp_mod.restore_item_start_local_git_configs(worktree) is True
    assert (nested / ".git").read_text(encoding="utf-8") == original_gitfile
    live_git_dir = subprocess.run(
        ["git", "rev-parse", "--absolute-git-dir"],
        cwd=nested,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert Path(live_git_dir).resolve() == trusted_git_dir.resolve()
