"""Object-store enum budget tests for nested probe snapshot materialization."""

from __future__ import annotations

import contextlib
import os
import struct
import time
import zlib
from pathlib import Path

import pytest

import awf.node.git_manager_ownership as git_manager_ownership


@pytest.mark.unit
def test_symlink_object_store_tree_via_fd_rejects_entry_flood(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6eq1r7: aggregate entry cap must fail closed mid-stream."""
    root = tmp_path / "objects"
    root.mkdir()
    for i in range(5):
        (root / f"flood-{i:04d}").write_bytes(b"x")
    staging = tmp_path / "staging"
    staging.mkdir()
    held: list[int] = []
    monkeypatch.setattr(git_manager_ownership, "_OBJECT_STORE_ENUM_AGGREGATE_MAX_ENTRIES", 3)
    fd = git_manager_ownership._open_git_dir_directory_fd(root)
    assert fd is not None
    try:
        assert git_manager_ownership._symlink_object_store_tree_via_fd(fd, staging, held) is False
    finally:
        for held_fd in held:
            os.close(held_fd)
        os.close(fd)


@pytest.mark.unit
def test_symlink_object_store_tree_via_fd_rejects_excessive_depth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bugbot 5094985052: deep objects trees must fail closed, not RecursionError."""
    root = tmp_path / "objects"
    root.mkdir()
    cursor = root
    for i in range(5):
        cursor = cursor / f"d{i}"
        cursor.mkdir()
    (cursor / "leaf").write_bytes(b"obj")
    staging = tmp_path / "staging"
    staging.mkdir()
    held: list[int] = []
    monkeypatch.setattr(git_manager_ownership, "_OBJECT_STORE_ENUM_MAX_DEPTH", 2)
    fd = git_manager_ownership._open_git_dir_directory_fd(root)
    assert fd is not None
    try:
        assert git_manager_ownership._symlink_object_store_tree_via_fd(fd, staging, held) is False
    finally:
        for held_fd in held:
            os.close(held_fd)
        os.close(fd)


@pytest.mark.unit
def test_symlink_object_store_tree_via_fd_rejects_past_deadline(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eq1r7: wall-time budget must fail closed before enumeration."""
    root = tmp_path / "objects"
    root.mkdir()
    (root / "ab").mkdir()
    (root / "ab" / "cdef").write_bytes(b"obj")
    staging = tmp_path / "staging"
    staging.mkdir()
    held: list[int] = []
    budget = git_manager_ownership._ObjectStoreEnumBudget(
        entries_remaining=100_000,
        deadline=time.monotonic() - 1.0,
        max_depth=git_manager_ownership._OBJECT_STORE_ENUM_MAX_DEPTH,
    )
    fd = git_manager_ownership._open_git_dir_directory_fd(root)
    assert fd is not None
    try:
        assert (
            git_manager_ownership._symlink_object_store_tree_via_fd(
                fd, staging, held, budget=budget
            )
            is False
        )
    finally:
        for held_fd in held:
            os.close(held_fd)
        os.close(fd)


@pytest.mark.unit
def test_symlink_object_store_tree_via_fd_rejects_mid_scan_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wall-time budget is re-checked while streaming entries (skips . / ..)."""
    root = tmp_path / "objects"
    root.mkdir()
    (root / "keep").write_bytes(b"x")
    (root / "late").write_bytes(b"y")
    staging = tmp_path / "staging"
    staging.mkdir()
    held: list[int] = []

    class _Entry:
        def __init__(self, name: str) -> None:
            self.name = name

    @contextlib.contextmanager
    def _scandir_two(_path: str | bytes | os.PathLike[str]) -> object:
        yield [_Entry("."), _Entry(".."), _Entry("keep"), _Entry("late")]

    clock = {"t": 1000.0}
    real_stat = os.stat

    def _stat_expire_after_keep(
        path: str | bytes | os.PathLike[str], *args: object, **kwargs: object
    ) -> os.stat_result:
        result = real_stat(path, *args, **kwargs)  # type: ignore[arg-type]
        if path == "keep":
            clock["t"] = 1001.0
        return result

    monkeypatch.setattr(os, "scandir", _scandir_two)
    monkeypatch.setattr(git_manager_ownership.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(os, "stat", _stat_expire_after_keep)
    budget = git_manager_ownership._ObjectStoreEnumBudget(
        entries_remaining=100_000,
        deadline=1000.5,
        max_depth=git_manager_ownership._OBJECT_STORE_ENUM_MAX_DEPTH,
    )
    fd = git_manager_ownership._open_git_dir_directory_fd(root)
    assert fd is not None
    try:
        assert (
            git_manager_ownership._symlink_object_store_tree_via_fd(
                fd, staging, held, budget=budget
            )
            is False
        )
    finally:
        for held_fd in held:
            os.close(held_fd)
        os.close(fd)


@pytest.mark.unit
def test_symlink_nested_probe_objects_store_honors_shared_entry_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Top-level objects materialization shares one aggregate entry budget."""
    git_dir = tmp_path / "repo.git"
    objects = git_dir / "objects"
    objects.mkdir(parents=True)
    for i in range(4):
        (objects / f"n{i:02d}").write_bytes(b"o")
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(git_manager_ownership, "_OBJECT_STORE_ENUM_AGGREGATE_MAX_ENTRIES", 2)
    fd = git_manager_ownership._open_git_dir_directory_fd(git_dir)
    assert fd is not None
    try:
        ok, held = git_manager_ownership._symlink_nested_probe_objects_store_via_fd(fd, staging)
        assert ok is False
        assert held == []
    finally:
        os.close(fd)


def _open_fd_count() -> int:
    return sum(1 for _ in Path("/proc/self/fd").iterdir())


@pytest.mark.unit
def test_nested_probe_objects_store_copies_leaves_without_retaining_fds(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eteRs: do not hold one descriptor per object leaf.

    Enumeration still permits many leaves, but staging must use bounded copies so
    a nested store cannot push the shared control-plane process past NOFILE.
    """
    git_dir = tmp_path / "repo.git"
    objects = git_dir / "objects"
    objects.mkdir(parents=True)
    leaf_count = 300
    for i in range(leaf_count):
        fanout = objects / f"{i % 256:02x}"
        fanout.mkdir(exist_ok=True)
        (fanout / f"{i:08x}").write_bytes(f"obj-{i}".encode())
    staging = tmp_path / "staging"
    staging.mkdir()
    fd = git_manager_ownership._open_git_dir_directory_fd(git_dir)
    assert fd is not None
    before = _open_fd_count()
    try:
        ok, held = git_manager_ownership._symlink_nested_probe_objects_store_via_fd(fd, staging)
        assert ok is True
        assert held == []
        after = _open_fd_count()
        # Only the caller's git-dir fd should remain from this helper; leaf copies
        # must not pin hundreds of descriptors across the snapshot lifetime.
        assert after - before <= 2
        staged_leaves = [p for p in (staging / "objects").rglob("*") if p.is_file()]
        assert len(staged_leaves) == leaf_count
        assert all(not p.is_symlink() for p in staged_leaves)
        sample = staging / "objects" / "00" / f"{0:08x}"
        assert sample.read_bytes() == b"obj-0"
        # Live swap after copy must not rewrite the private staging bytes.
        (objects / "00" / f"{0:08x}").unlink()
        (objects / "00" / f"{0:08x}").write_bytes(b"swapped")
        assert sample.read_bytes() == b"obj-0"
    finally:
        os.close(fd)


@pytest.mark.unit
def test_copy_opened_regular_file_rejects_oversized_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6eteRs: oversized object leaves fail closed instead of copying."""
    src = tmp_path / "big"
    src.write_bytes(b"abcdef")
    dest = tmp_path / "out"
    monkeypatch.setattr(git_manager_ownership, "_OBJECT_STORE_LEAF_COPY_MAX_BYTES", 4)
    fd = os.open(src, os.O_RDONLY)
    try:
        assert (
            git_manager_ownership._copy_opened_regular_file_to_path(
                fd, dest, max_bytes=git_manager_ownership._OBJECT_STORE_LEAF_COPY_MAX_BYTES
            )
            is False
        )
        assert not dest.exists()
    finally:
        os.close(fd)


@pytest.mark.unit
def test_copy_opened_regular_file_rejects_high_ratio_loose_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6evsX8: compressed size alone must not admit huge inflate.

    A forged loose object can declare a payload larger than the leaf max while
    occupying only tens of compressed bytes; staging must fail closed before
    nested ``git diff --cached`` inflates it.
    """
    declared = 128 * 1024 * 1024
    framed = f"commit {declared}\0".encode() + b"x"
    compressed = zlib.compress(framed)
    assert len(compressed) < 1024
    src = tmp_path / "loose"
    src.write_bytes(compressed)
    dest = tmp_path / "out"
    monkeypatch.setattr(git_manager_ownership, "_OBJECT_STORE_LEAF_COPY_MAX_BYTES", 64 * 1024)
    fd = os.open(src, os.O_RDONLY)
    try:
        assert (
            git_manager_ownership._copy_opened_regular_file_to_path(
                fd,
                dest,
                max_bytes=git_manager_ownership._OBJECT_STORE_LEAF_COPY_MAX_BYTES,
                validate_git_loose_object=True,
            )
            is False
        )
        assert not dest.exists()
    finally:
        os.close(fd)


@pytest.mark.unit
def test_copy_opened_regular_file_allows_bounded_loose_object(tmp_path: Path) -> None:
    """Valid small loose objects still stage when inflate validation is on."""
    body = b""
    framed = f"tree {len(body)}\0".encode() + body
    compressed = zlib.compress(framed)
    src = tmp_path / "loose"
    src.write_bytes(compressed)
    dest = tmp_path / "out"
    fd = os.open(src, os.O_RDONLY)
    try:
        assert (
            git_manager_ownership._copy_opened_regular_file_to_path(
                fd, dest, validate_git_loose_object=True
            )
            is True
        )
        assert dest.read_bytes() == compressed
    finally:
        os.close(fd)


@pytest.mark.unit
def test_copy_opened_regular_file_skips_inflate_check_for_non_loose(
    tmp_path: Path,
) -> None:
    """Pack/idx-like bytes are not parseable loose objects; compressed cap still applies."""
    src = tmp_path / "pack"
    src.write_bytes(b"PACK" + b"\0" * 32)
    dest = tmp_path / "out"
    fd = os.open(src, os.O_RDONLY)
    try:
        assert (
            git_manager_ownership._copy_opened_regular_file_to_path(
                fd, dest, validate_git_loose_object=True
            )
            is True
        )
        assert dest.read_bytes() == src.read_bytes()
    finally:
        os.close(fd)


@pytest.mark.unit
def test_symlink_object_store_tree_rejects_high_ratio_loose_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Object-store walks enable loose-object inflate validation end-to-end."""
    declared = 128 * 1024 * 1024
    framed = f"blob {declared}\0".encode() + b"y"
    compressed = zlib.compress(framed)
    root = tmp_path / "objects"
    fanout = root / "ab"
    fanout.mkdir(parents=True)
    (fanout / "cdef").write_bytes(compressed)
    staging = tmp_path / "staging"
    staging.mkdir()
    held: list[int] = []
    monkeypatch.setattr(git_manager_ownership, "_OBJECT_STORE_LEAF_COPY_MAX_BYTES", 64 * 1024)
    fd = git_manager_ownership._open_git_dir_directory_fd(root)
    assert fd is not None
    try:
        assert git_manager_ownership._symlink_object_store_tree_via_fd(fd, staging, held) is False
        assert not (staging / "ab" / "cdef").exists()
    finally:
        for held_fd in held:
            os.close(held_fd)
        os.close(fd)


@pytest.mark.unit
def test_parse_git_loose_object_header_declared_size_edges() -> None:
    """Loose-object header parser accepts only type + decimal size."""
    assert git_manager_ownership._parse_git_loose_object_header_declared_size(b"commit 12") == 12
    assert git_manager_ownership._parse_git_loose_object_header_declared_size(b"nospace") is None
    assert git_manager_ownership._parse_git_loose_object_header_declared_size(b"evil 1") is None
    assert git_manager_ownership._parse_git_loose_object_header_declared_size(b"blob xyz") is None


@pytest.mark.unit
def test_git_loose_object_declared_size_from_fd_rejects_corrupt_and_overlong(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Peek fails closed on corrupt zlib and headers without a timely NUL."""
    bad = tmp_path / "bad"
    bad.write_bytes(b"not-zlib")
    fd = os.open(bad, os.O_RDONLY)
    try:
        assert git_manager_ownership._git_loose_object_declared_size_from_fd(fd) is None
    finally:
        os.close(fd)

    # Inflate produces many bytes before NUL → overlong header.
    long_header = b"blob " + (b"1" * 80) + b"\0"
    long_path = tmp_path / "long"
    long_path.write_bytes(zlib.compress(long_header))
    fd = os.open(long_path, os.O_RDONLY)
    try:
        assert git_manager_ownership._git_loose_object_declared_size_from_fd(fd) is None
    finally:
        os.close(fd)

    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    fd = os.open(empty, os.O_RDONLY)
    try:
        assert git_manager_ownership._git_loose_object_declared_size_from_fd(fd) is None
    finally:
        os.close(fd)

    src = tmp_path / "ok"
    src.write_bytes(zlib.compress(b"blob 1\0x"))
    fd = os.open(src, os.O_RDONLY)
    try:

        def _read_boom(_fd: int, _n: int) -> bytes:
            raise OSError("read failed")

        monkeypatch.setattr(os, "read", _read_boom)
        assert git_manager_ownership._git_loose_object_declared_size_from_fd(fd) is None
    finally:
        os.close(fd)


def _zlib_loose_object_with_empty_deflate_blocks(
    payload: bytes, *, empty_block_count: int
) -> bytes:
    """Build a valid zlib stream with many empty non-final DEFLATE blocks first."""
    empty_blocks = b"\x00\x00\x00\xff\xff" * empty_block_count
    final_block = (
        b"\x01"
        + struct.pack("<H", len(payload))
        + struct.pack("<H", 0xFFFF ^ len(payload))
        + payload
    )
    return b"\x78\x9c" + empty_blocks + final_block + struct.pack(">I", zlib.adler32(payload))


@pytest.mark.unit
def test_git_loose_object_declared_size_from_fd_rejects_empty_deflate_block_flood(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ewJZe: empty DEFLATE blocks must not bypass peek budgets.

    A valid zlib stream can place the ``blob <size>\\0`` header after many empty
    stored blocks. Without a compressed-byte cap the peek would scan unbounded
    input before the copy deadline starts.
    """
    payload = b"blob 1\0x"
    stream = _zlib_loose_object_with_empty_deflate_blocks(payload, empty_block_count=20_000)
    assert zlib.decompress(stream) == payload
    assert len(stream) > git_manager_ownership._GIT_LOOSE_OBJECT_PEEK_COMPRESSED_MAX_BYTES
    src = tmp_path / "padded"
    src.write_bytes(stream)
    fd = os.open(src, os.O_RDONLY)
    try:
        assert git_manager_ownership._git_loose_object_declared_size_from_fd(fd) is None
    finally:
        os.close(fd)


@pytest.mark.unit
def test_git_loose_object_declared_size_from_fd_rejects_when_peek_deadline_elapses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6ewJZe: peek must honor its wall-time budget."""
    src = tmp_path / "ok"
    src.write_bytes(zlib.compress(b"blob 1\0x"))
    fd = os.open(src, os.O_RDONLY)
    try:
        clock = {"now": 1000.0}

        def _monotonic() -> float:
            # Advance past the deadline on the first loop check.
            clock["now"] += 10.0
            return clock["now"]

        monkeypatch.setattr(git_manager_ownership.time, "monotonic", _monotonic)
        assert (
            git_manager_ownership._git_loose_object_declared_size_from_fd(fd, budget_seconds=1.0)
            is None
        )
    finally:
        os.close(fd)


@pytest.mark.unit
def test_copy_opened_regular_file_fails_closed_when_lseek_after_peek_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a loose-object peek, rewind failure must not proceed to copy."""
    framed = b"blob 1\0x"
    src = tmp_path / "loose"
    src.write_bytes(zlib.compress(framed))
    dest = tmp_path / "out"
    fd = os.open(src, os.O_RDONLY)
    try:
        real_lseek = os.lseek

        def _lseek_boom(f: int, pos: int, how: int) -> int:
            if how == os.SEEK_SET and pos == 0:
                raise OSError("lseek failed")
            return real_lseek(f, pos, how)

        monkeypatch.setattr(os, "lseek", _lseek_boom)
        assert (
            git_manager_ownership._copy_opened_regular_file_to_path(
                fd, dest, validate_git_loose_object=True
            )
            is False
        )
        assert not dest.exists()
    finally:
        os.close(fd)
