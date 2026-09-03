"""Ownership-edge tests: split-index, fd helpers, and probe pin/rename edges."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import struct
import subprocess
from pathlib import Path

import pytest

import awf.node.git_manager as git_manager
import awf.node.git_manager_ownership as git_manager_ownership


@pytest.mark.unit
def test_untrusted_nested_probe_config_snapshot_retains_split_index_backing(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eo3py: split-index needs ``sharedindex.<oid>`` beside index.

    ``git update-index --split-index`` stores the bulk index in a sibling
    ``sharedindex.<hash>``. Snapshotting only ``index`` makes snapshot-scoped
    ``diff-files`` exit 128 (``index file open failed``), so unchanged nested
    residue scans become unreadable and valid corrections look like mutations.
    """
    nested = tmp_path / "nested"
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
    (nested / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=nested, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "update-index", "--split-index"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    shared = sorted((nested / ".git").glob("sharedindex.*"))
    assert shared, "expected git to materialize a sharedindex.* backing file"
    shared_names = {path.name for path in shared}

    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is not None
        for name in shared_names:
            link = shadow / name
            assert link.is_file(), f"snapshot missing split-index backing file {name}"
            assert not link.is_symlink()
        clean = subprocess.run(
            [
                "git",
                "--git-dir",
                str(shadow),
                "--work-tree",
                str(nested),
                *git_manager.UNTRUSTED_NESTED_GIT_CONFIG_ARGS,
                "diff-files",
            ],
            capture_output=True,
        )
        assert clean.returncode == 0, clean.stderr.decode("utf-8", errors="replace")
        assert clean.stdout == b""

        (nested / "tracked.txt").write_text("mutated\n", encoding="utf-8")
        dirty = subprocess.run(
            [
                "git",
                "--git-dir",
                str(shadow),
                "--work-tree",
                str(nested),
                *git_manager.UNTRUSTED_NESTED_GIT_CONFIG_ARGS,
                "diff-files",
                "--name-only",
                "-z",
            ],
            capture_output=True,
        )
        assert dirty.returncode == 0, dirty.stderr.decode("utf-8", errors="replace")
        assert b"tracked.txt" in dirty.stdout.split(b"\0")


@pytest.mark.unit
def test_untrusted_nested_probe_config_snapshot_links_only_index_referenced_sharedindex(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6epUot: do not enumerate/link every sharedindex.* name.

    Agent-controlled git-dirs can plant unbounded ``sharedindex.<hex>`` decoys.
    Snapshotting must parse the split-index ``link`` extension and link only the
    referenced backing file (not every directory match).
    """
    nested = tmp_path / "nested"
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
    (nested / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=nested, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "update-index", "--split-index"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    git_dir = nested / ".git"
    real_shared = sorted(git_dir.glob("sharedindex.*"))
    assert len(real_shared) == 1
    real_name = real_shared[0].name
    decoys = [f"sharedindex.{i:040x}" for i in range(64)]
    for name in decoys:
        if name == real_name:
            continue
        (git_dir / name).write_bytes(b"decoy")

    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is not None
        linked = sorted(
            path.name for path in shadow.iterdir() if path.name.startswith("sharedindex.")
        )
        assert linked == [real_name]
        assert (shadow / real_name).is_file()
        assert not (shadow / real_name).is_symlink()
        clean = subprocess.run(
            [
                "git",
                "--git-dir",
                str(shadow),
                "--work-tree",
                str(nested),
                *git_manager.UNTRUSTED_NESTED_GIT_CONFIG_ARGS,
                "diff-files",
            ],
            capture_output=True,
        )
        assert clean.returncode == 0, clean.stderr.decode("utf-8", errors="replace")


@pytest.mark.unit
def test_split_index_shared_oid_hex_reads_link_extension() -> None:
    """Parser returns the shared-index OID only from a valid ``link`` extension."""
    header = b"DIRC" + struct.pack(">II", 2, 0)
    non_split = header + hashlib.sha1(header).digest()
    assert git_manager_ownership._split_index_shared_oid_hex(non_split) is None

    oid = bytes.fromhex("0123456789abcdef0123456789abcdef01234567")
    # link extension: signature + size + oid (+ optional ewah bitmaps).
    link_body = oid
    link_ext = b"link" + struct.pack(">I", len(link_body)) + link_body
    body = header + link_ext
    split = body + hashlib.sha1(body).digest()
    assert git_manager_ownership._split_index_shared_oid_hex(split) == oid.hex()

    assert git_manager_ownership._split_index_shared_oid_hex(b"not-an-index") is None
    assert git_manager_ownership._split_index_shared_oid_hex(b"DIRC" + b"\x00" * 8) is None
    bad_ver = b"DIRC" + struct.pack(">II", 99, 0)
    assert (
        git_manager_ownership._split_index_shared_oid_hex(bad_ver + hashlib.sha1(bad_ver).digest())
        is None
    )

    # Non-link extension before link is skipped.
    tree_ext = b"TREE" + struct.pack(">I", 0)
    body2 = header + tree_ext + link_ext
    split2 = body2 + hashlib.sha1(body2).digest()
    assert git_manager_ownership._split_index_shared_oid_hex(split2) == oid.hex()

    # Truncated link body fails closed.
    short_link = b"link" + struct.pack(">I", 4) + b"abcd"
    body3 = header + short_link
    split3 = body3 + hashlib.sha1(body3).digest()
    assert git_manager_ownership._split_index_shared_oid_hex(split3) is None


@pytest.mark.unit
def test_split_index_shared_oid_hex_from_real_v4_split_index(tmp_path: Path) -> None:
    """v4 path-compressed split-index still resolves the ``link`` OID."""
    nested = tmp_path / "nested"
    nested.mkdir()
    subprocess.run(["git", "init"], cwd=nested, check=True, capture_output=True)
    (nested / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=nested, check=True, capture_output=True)
    subprocess.run(
        ["git", "update-index", "--index-version=4"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "update-index", "--split-index"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    shared = sorted((nested / ".git").glob("sharedindex.*"))
    assert len(shared) == 1
    expected = shared[0].name.removeprefix("sharedindex.")
    index_bytes = (nested / ".git" / "index").read_bytes()
    assert git_manager_ownership._split_index_shared_oid_hex(index_bytes) == expected


@pytest.mark.unit
def test_read_git_dir_child_bytes_via_fd_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Index child reads honor size caps and reject non-regular files."""
    git_dir = tmp_path / "git"
    git_dir.mkdir()
    (git_dir / "index").write_bytes(b"x" * 32)
    fd = git_manager_ownership._open_git_dir_directory_fd(git_dir)
    assert fd is not None
    try:
        assert (
            git_manager_ownership._read_git_dir_child_bytes_via_fd(fd, "index", max_bytes=32)
            == b"x" * 32
        )
        assert (
            git_manager_ownership._read_git_dir_child_bytes_via_fd(fd, "index", max_bytes=16)
            is None
        )
        assert (
            git_manager_ownership._read_git_dir_child_bytes_via_fd(fd, "missing", max_bytes=32)
            is None
        )
        (git_dir / "dirchild").mkdir()
        assert (
            git_manager_ownership._read_git_dir_child_bytes_via_fd(fd, "dirchild", max_bytes=32)
            is None
        )
        monkeypatch.setattr(git_manager_ownership, "_GIT_DIR_CONFIG_READ_BUDGET_SECONDS", 0.0)
        monkeypatch.setattr(
            git_manager_ownership.time,
            "monotonic",
            lambda: 1_000_000.0,
        )
        assert (
            git_manager_ownership._read_git_dir_child_bytes_via_fd(fd, "index", max_bytes=32)
            is None
        )
    finally:
        os.close(fd)


@pytest.mark.unit
def test_decode_git_index_varint_edges() -> None:
    """Varint decoder rejects truncate and overflow; accepts multi-byte values."""
    assert git_manager_ownership._decode_git_index_varint(b"", 0) is None
    assert git_manager_ownership._decode_git_index_varint(b"\x00", 0) == (0, 1)
    assert git_manager_ownership._decode_git_index_varint(b"\x7f", 0) == (127, 1)
    assert git_manager_ownership._decode_git_index_varint(b"\x80\x00", 0) == (128, 2)
    assert git_manager_ownership._decode_git_index_varint(b"\x81\x00", 0) == (256, 2)
    assert git_manager_ownership._decode_git_index_varint(b"\x80", 0) is None
    # Continuation with value that sets MSB(val, 7) after increment.
    assert (
        git_manager_ownership._decode_git_index_varint(b"\xff" + b"\x80" * 9 + b"\x00", 0) is None
    )


@pytest.mark.unit
def test_git_index_hash_len_and_skip_entry_edges() -> None:
    """Hash-len detection and entry-skipping fail closed on truncated indexes."""
    assert git_manager_ownership._git_index_hash_len(b"DIRC" + b"\x00" * 8) is None
    assert git_manager_ownership._git_index_hash_len(b"x" * 40) is None

    # SHA-256 trailer is accepted when the digest matches.
    header = b"DIRC" + struct.pack(">II", 2, 0)
    sha256_idx = header + hashlib.sha256(header).digest()
    assert git_manager_ownership._git_index_hash_len(sha256_idx) == 32
    assert git_manager_ownership._split_index_shared_oid_hex(sha256_idx) is None

    oid = bytes.fromhex("0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
    link_ext = b"link" + struct.pack(">I", len(oid)) + oid
    body = header + link_ext
    sha256_split = body + hashlib.sha256(body).digest()
    assert git_manager_ownership._split_index_shared_oid_hex(sha256_split) == oid.hex()

    # entry_count claims an entry but body is empty → skip fails.
    claim = b"DIRC" + struct.pack(">II", 2, 1)
    claim_idx = claim + hashlib.sha1(claim).digest()
    assert git_manager_ownership._split_index_shared_oid_hex(claim_idx) is None

    # Oversized extension payload fails closed.
    bad_ext = b"link" + struct.pack(">I", 500) + b"abcd"
    bad_body = header + bad_ext
    bad_idx = bad_body + hashlib.sha1(bad_body).digest()
    assert git_manager_ownership._split_index_shared_oid_hex(bad_idx) is None

    # v2 long-path (namelen 0xFFF) without NUL fails closed.
    flags = 0x0FFF
    entry = b"\x00" * (40 + 20) + struct.pack(">H", flags) + b"no-nul-here"
    # pad claim so checksum validates over truncated structure
    long_body = b"DIRC" + struct.pack(">II", 2, 1) + entry
    long_idx = long_body + hashlib.sha1(long_body).digest()
    assert (
        git_manager_ownership._skip_git_index_entries(
            long_idx, entry_count=1, version=2, hash_len=20
        )
        is None
    )

    # v2 namelen past body_end.
    flags2 = 0x0005
    entry2 = b"\x00" * (40 + 20) + struct.pack(">H", flags2) + b"ab"  # needs 5+NUL
    body2 = b"DIRC" + struct.pack(">II", 2, 1) + entry2
    idx2 = body2 + hashlib.sha1(body2).digest()
    assert (
        git_manager_ownership._skip_git_index_entries(idx2, entry_count=1, version=2, hash_len=20)
        is None
    )

    # Extended flag on v2 is illegal → fail closed.
    flags3 = 0x4000
    entry3 = b"\x00" * (40 + 20) + struct.pack(">H", flags3)
    body3 = b"DIRC" + struct.pack(">II", 2, 1) + entry3 + b"\x00" * 8
    idx3 = body3 + hashlib.sha1(body3).digest()
    assert (
        git_manager_ownership._skip_git_index_entries(idx3, entry_count=1, version=2, hash_len=20)
        is None
    )

    # v4: bad strip against empty prev path.
    flags4 = 0x0000
    entry4 = b"\x00" * (40 + 20) + struct.pack(">H", flags4) + b"\x01" + b"x\0"
    body4 = b"DIRC" + struct.pack(">II", 4, 1) + entry4
    idx4 = body4 + hashlib.sha1(body4).digest()
    assert (
        git_manager_ownership._skip_git_index_entries(idx4, entry_count=1, version=4, hash_len=20)
        is None
    )

    # v4: missing NUL after varint.
    entry5 = b"\x00" * (40 + 20) + struct.pack(">H", flags4) + b"\x00" + b"nos"
    body5 = b"DIRC" + struct.pack(">II", 4, 1) + entry5
    idx5 = body5 + hashlib.sha1(body5).digest()
    assert (
        git_manager_ownership._skip_git_index_entries(idx5, entry_count=1, version=4, hash_len=20)
        is None
    )

    # v4: varint truncated.
    entry6 = b"\x00" * (40 + 20) + struct.pack(">H", flags4) + b"\x80"
    body6 = b"DIRC" + struct.pack(">II", 4, 1) + entry6
    idx6 = body6 + hashlib.sha1(body6).digest()
    assert (
        git_manager_ownership._skip_git_index_entries(idx6, entry_count=1, version=4, hash_len=20)
        is None
    )

    # v3 extended flags with truncated extended halfword.
    flags7 = 0x4000
    entry7 = b"\x00" * (40 + 20) + struct.pack(">H", flags7)  # no extended bytes
    body7 = b"DIRC" + struct.pack(">II", 3, 1) + entry7
    idx7 = body7 + hashlib.sha1(body7).digest()
    assert (
        git_manager_ownership._skip_git_index_entries(idx7, entry_count=1, version=3, hash_len=20)
        is None
    )


@pytest.mark.unit
def test_read_fd_regular_file_bytes_error_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bounded fd reads fail closed on short reads, OSError, and inode churn."""
    path = tmp_path / "blob"
    path.write_bytes(b"abcdef")
    real_read = git_manager_ownership.os.read
    real_fstat = git_manager_ownership.os.fstat

    fd = os.open(path, os.O_RDONLY)
    try:
        monkeypatch.setattr(
            git_manager_ownership.os,
            "read",
            lambda _fd, _n: (_ for _ in ()).throw(OSError("boom")),
        )
        assert git_manager_ownership._read_fd_regular_file_bytes(fd, max_bytes=64) is None
    finally:
        os.close(fd)
    monkeypatch.setattr(git_manager_ownership.os, "read", real_read)

    fd = os.open(path, os.O_RDONLY)
    try:
        monkeypatch.setattr(git_manager_ownership.os, "read", lambda _fd, _n: b"")
        assert git_manager_ownership._read_fd_regular_file_bytes(fd, max_bytes=64) is None
    finally:
        os.close(fd)
    monkeypatch.setattr(git_manager_ownership.os, "read", real_read)

    fd = os.open(path, os.O_RDONLY)
    try:
        calls = {"n": 0}

        def _fstat(f: int) -> os.stat_result:
            calls["n"] += 1
            if calls["n"] == 1:
                return real_fstat(f)
            raise OSError("fstat-after")

        monkeypatch.setattr(git_manager_ownership.os, "fstat", _fstat)
        assert git_manager_ownership._read_fd_regular_file_bytes(fd, max_bytes=64) is None
    finally:
        os.close(fd)
    monkeypatch.setattr(git_manager_ownership.os, "fstat", real_fstat)

    fd = os.open(path, os.O_RDONLY)
    try:
        calls = {"n": 0}

        def _mutate(f: int) -> os.stat_result:
            calls["n"] += 1
            st = real_fstat(f)
            if calls["n"] == 1:
                return st
            return os.stat_result(
                (
                    st.st_mode,
                    st.st_ino,
                    st.st_dev,
                    st.st_nlink,
                    st.st_uid,
                    st.st_gid,
                    st.st_size + 1,
                    st.st_atime,
                    st.st_mtime,
                    st.st_ctime,
                )
            )

        monkeypatch.setattr(git_manager_ownership.os, "fstat", _mutate)
        assert git_manager_ownership._read_fd_regular_file_bytes(fd, max_bytes=64) is None
    finally:
        os.close(fd)
    monkeypatch.setattr(git_manager_ownership.os, "fstat", real_fstat)

    fd = os.open(path, os.O_RDONLY)
    try:
        monkeypatch.setattr(
            git_manager_ownership.os,
            "fstat",
            lambda _fd: (_ for _ in ()).throw(OSError("open-fstat")),
        )
        assert git_manager_ownership._read_fd_regular_file_bytes(fd, max_bytes=64) is None
    finally:
        os.close(fd)


@pytest.mark.unit
def test_skip_git_index_entries_extended_and_long_path_success() -> None:
    """v3 extended flags and v2 0xFFF paths skip cleanly when well-formed."""
    hash_len = 20
    flags = 0x4000  # extended, namelen 0
    entry = b"\x00" * (40 + hash_len) + struct.pack(">H", flags) + b"\x00\x00" + b"\x00"
    entry += b"\x00" * 7  # pad to 8-byte alignment
    body = b"DIRC" + struct.pack(">II", 3, 1) + entry
    idx = body + hashlib.sha1(body).digest()
    assert git_manager_ownership._skip_git_index_entries(
        idx, entry_count=1, version=3, hash_len=20
    ) == 12 + len(entry)

    flags_long = 0x0FFF
    path = b"very-long-name"
    entry_l = b"\x00" * (40 + hash_len) + struct.pack(">H", flags_long) + path + b"\x00"
    pad = (8 - (len(entry_l) % 8)) % 8
    entry_l += b"\x00" * pad
    body_l = b"DIRC" + struct.pack(">II", 2, 1) + entry_l
    idx_l = body_l + hashlib.sha1(body_l).digest()
    assert git_manager_ownership._skip_git_index_entries(
        idx_l, entry_count=1, version=2, hash_len=20
    ) == 12 + len(entry_l)

    # Padding that would exceed body_end fails closed.
    flags0 = 0x0000
    entry_short = b"\x00" * (40 + hash_len) + struct.pack(">H", flags0) + b"\x00"
    body_s = b"DIRC" + struct.pack(">II", 2, 1) + entry_short
    idx_s = body_s + hashlib.sha1(body_s).digest()
    assert (
        git_manager_ownership._skip_git_index_entries(idx_s, entry_count=1, version=2, hash_len=20)
        is None
    )


@pytest.mark.unit
def test_split_index_shared_oid_hex_bad_checksum() -> None:
    """Valid DIRC header with a bogus trailer fails closed (hash_len None)."""
    bad = b"DIRC" + struct.pack(">II", 2, 0) + b"\x00" * 20
    assert git_manager_ownership._split_index_shared_oid_hex(bad) is None


@pytest.mark.unit
def test_skip_git_index_entries_v4_varint_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v4 path decode failure fails closed."""
    flags = 0x0000
    entry = b"\x00" * (40 + 20) + struct.pack(">H", flags) + b"\x00" + b"f\0"
    body = b"DIRC" + struct.pack(">II", 4, 1) + entry
    idx = body + hashlib.sha1(body).digest()
    monkeypatch.setattr(git_manager_ownership, "_decode_git_index_varint", lambda *_a, **_k: None)
    assert (
        git_manager_ownership._skip_git_index_entries(idx, entry_count=1, version=4, hash_len=20)
        is None
    )


@pytest.mark.unit
def test_symlink_split_index_backing_files_via_fd_skips_unreadable_index(
    tmp_path: Path,
) -> None:
    """Missing/unreadable index yields no sharedindex symlink."""
    git_dir = tmp_path / "git"
    git_dir.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    fd = git_manager_ownership._open_git_dir_directory_fd(git_dir)
    assert fd is not None
    held: list[int] = []
    try:
        assert (
            git_manager_ownership._symlink_split_index_backing_files_via_fd(fd, staging, held)
            is True
        )
        assert list(staging.iterdir()) == []
        # Non-split index: still no sharedindex link.
        header = b"DIRC" + struct.pack(">II", 2, 0)
        (git_dir / "index").write_bytes(header + hashlib.sha1(header).digest())
        assert (
            git_manager_ownership._symlink_split_index_backing_files_via_fd(fd, staging, held)
            is True
        )
        assert list(staging.iterdir()) == []
        assert held == []
    finally:
        os.close(fd)


@pytest.mark.unit
def test_symlink_git_dir_child_via_fd_rejects_symlink_and_wrong_type(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eqQgm: helper fails closed on symlink / type mismatch."""
    git_dir = tmp_path / "git"
    git_dir.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    foreign = tmp_path / "foreign"
    foreign.write_text("x\n", encoding="utf-8")
    (git_dir / "packed-refs").symlink_to(foreign)
    (git_dir / "refs").write_text("not-a-dir\n", encoding="utf-8")
    os.mkfifo(git_dir / "index")
    (git_dir / "objects").mkdir()
    fd = git_manager_ownership._open_git_dir_directory_fd(git_dir)
    assert fd is not None
    held: list[int] = []
    try:
        assert (
            git_manager_ownership._symlink_git_dir_child_via_fd(
                fd, "packed-refs", staging / "packed-refs", held, expect_directory=False
            )
            is False
        )
        assert (
            git_manager_ownership._symlink_git_dir_child_via_fd(
                fd, "refs", staging / "refs", held, expect_directory=True
            )
            is False
        )
        assert (
            git_manager_ownership._symlink_git_dir_child_via_fd(
                fd, "index", staging / "index", held, expect_directory=False
            )
            is False
        )
        assert (
            git_manager_ownership._symlink_git_dir_child_via_fd(
                fd, "missing", staging / "missing", held, expect_directory=False
            )
            is True
        )
        assert not (staging / "missing").exists()
        assert (
            git_manager_ownership._symlink_git_dir_child_via_fd(
                fd, "objects", staging / "objects", held, expect_directory=True
            )
            is True
        )
        assert (staging / "objects").is_symlink()
        assert held
        # Directory leaves are pinned to the child fd, not ``<dir_fd>/<name>``.
        assert str((staging / "objects").readlink()) == f"/proc/{os.getpid()}/fd/{held[-1]}"
    finally:
        for held_fd in held:
            os.close(held_fd)
        os.close(fd)


@pytest.mark.unit
def test_symlink_git_dir_child_via_fd_pins_regular_leaf_against_name_swap(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ercEO / eteRs: staged leaf copies pin validated bytes.

    A post-validation replace of ``index`` with a foreign symlink must not change
    what the staging file contains after the bounded copy completes.
    """
    git_dir = tmp_path / "git"
    git_dir.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    original = b"validated-index-bytes\n"
    (git_dir / "index").write_bytes(original)
    foreign = tmp_path / "foreign-index"
    foreign.write_bytes(b"foreign-index-bytes\n")
    fd = git_manager_ownership._open_git_dir_directory_fd(git_dir)
    assert fd is not None
    held: list[int] = []
    try:
        assert (
            git_manager_ownership._symlink_git_dir_child_via_fd(
                fd, "index", staging / "index", held, expect_directory=False
            )
            is True
        )
        assert held == []
        assert not (staging / "index").is_symlink()
        assert (staging / "index").read_bytes() == original
        (git_dir / "index").unlink()
        (git_dir / "index").symlink_to(foreign)
        assert (staging / "index").read_bytes() == original
    finally:
        for held_fd in held:
            os.close(held_fd)
        os.close(fd)


@pytest.mark.unit
def test_symlink_git_dir_child_via_fd_stat_and_link_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Helper returns False when lstat or symlink_to raises OSError."""
    git_dir = tmp_path / "git"
    git_dir.mkdir()
    (git_dir / "refs").mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    fd = git_manager_ownership._open_git_dir_directory_fd(git_dir)
    assert fd is not None
    held: list[int] = []
    try:
        real_stat = os.stat

        def _stat_boom(
            path: str | bytes | os.PathLike[str], *args: object, **kwargs: object
        ) -> object:
            if path == "refs":
                raise OSError("stat failed")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", _stat_boom)
        assert (
            git_manager_ownership._symlink_git_dir_child_via_fd(
                fd, "refs", staging / "refs", held, expect_directory=True
            )
            is False
        )
        monkeypatch.undo()

        def _link_boom(self: Path, target: object, *_a: object, **_k: object) -> None:
            del self, target
            raise OSError("symlink failed")

        monkeypatch.setattr(Path, "symlink_to", _link_boom)
        assert (
            git_manager_ownership._symlink_git_dir_child_via_fd(
                fd, "refs", staging / "refs2", held, expect_directory=True
            )
            is False
        )
        assert held == []
    finally:
        for held_fd in held:
            os.close(held_fd)
        os.close(fd)


@pytest.mark.unit
def test_symlink_git_dir_child_via_fd_open_and_fstat_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6ercEO: child open/fstat failures fail closed without leaking fds."""
    git_dir = tmp_path / "git"
    git_dir.mkdir()
    (git_dir / "index").write_bytes(b"idx\n")
    (git_dir / "objects").mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    fd = git_manager_ownership._open_git_dir_directory_fd(git_dir)
    assert fd is not None
    held: list[int] = []
    try:
        real_open = os.open

        def _open_boom(
            path: str | bytes | os.PathLike[str], flags: int, *args: object, **kwargs: object
        ) -> int:
            if path == "index":
                raise OSError("open failed")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", _open_boom)
        assert (
            git_manager_ownership._symlink_git_dir_child_via_fd(
                fd, "index", staging / "index", held, expect_directory=False
            )
            is False
        )
        assert held == []
        monkeypatch.undo()

        opened_fds: list[int] = []
        real_fstat = os.fstat

        def _open_track(
            path: str | bytes | os.PathLike[str], flags: int, *args: object, **kwargs: object
        ) -> int:
            child = real_open(path, flags, *args, **kwargs)
            if path == "index":
                opened_fds.append(child)
            return child

        def _fstat_boom(fildes: int) -> os.stat_result:
            if opened_fds and fildes == opened_fds[-1]:
                raise OSError("fstat failed")
            return real_fstat(fildes)

        monkeypatch.setattr(os, "open", _open_track)
        monkeypatch.setattr(os, "fstat", _fstat_boom)
        assert (
            git_manager_ownership._symlink_git_dir_child_via_fd(
                fd, "index", staging / "index-fstat", held, expect_directory=False
            )
            is False
        )
        assert held == []
        assert opened_fds
        # Child fd must be closed on fstat failure (entry gone from /proc/self/fd).
        assert not (Path("/proc/self/fd") / str(opened_fds[-1])).exists()
        monkeypatch.undo()

        opened_fds.clear()
        real_isreg = stat.S_ISREG

        def _isreg_false(mode: int) -> bool:
            # After open, reject the opened inode as non-regular.
            return False if opened_fds else real_isreg(mode)

        monkeypatch.setattr(os, "open", _open_track)
        monkeypatch.setattr(stat, "S_ISREG", _isreg_false)
        assert (
            git_manager_ownership._symlink_git_dir_child_via_fd(
                fd, "index", staging / "index-type", held, expect_directory=False
            )
            is False
        )
        assert held == []
        assert opened_fds
        assert not (Path("/proc/self/fd") / str(opened_fds[-1])).exists()
        monkeypatch.undo()

        def _open_child_fail(dir_fd: int, name: str) -> int | None:
            del dir_fd, name
            return None

        monkeypatch.setattr(
            git_manager_ownership, "_open_git_dir_child_directory_fd", _open_child_fail
        )
        assert (
            git_manager_ownership._symlink_git_dir_child_via_fd(
                fd, "objects", staging / "objects", held, expect_directory=True
            )
            is False
        )
        assert held == []
    finally:
        for held_fd in held:
            os.close(held_fd)
        os.close(fd)


@pytest.mark.unit
def test_symlink_split_index_backing_files_via_fd_rejects_symlink_sharedindex(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eqQgm: sharedindex symlink must fail closed."""
    git_dir = tmp_path / "git"
    git_dir.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    # Minimal split-index with a link extension pointing at sharedindex.<oid>.
    oid = b"\x11" * 20
    oid_hex = oid.hex()
    ext_body = oid
    ext = b"link" + struct.pack(">I", len(ext_body)) + ext_body
    body = b"DIRC" + struct.pack(">II", 2, 0) + ext
    index_bytes = body + hashlib.sha1(body).digest()
    (git_dir / "index").write_bytes(index_bytes)
    foreign = tmp_path / "foreign-shared"
    foreign.write_bytes(b"shared")
    (git_dir / f"sharedindex.{oid_hex}").symlink_to(foreign)
    fd = git_manager_ownership._open_git_dir_directory_fd(git_dir)
    assert fd is not None
    held: list[int] = []
    try:
        assert (
            git_manager_ownership._symlink_split_index_backing_files_via_fd(fd, staging, held)
            is False
        )
        assert list(staging.iterdir()) == []
        assert held == []
    finally:
        os.close(fd)


def _init_marked_nested_repo(root: Path, *, email: str, blob: str) -> str:
    """Create a tiny git repo and return the blob OID for ``blob``."""
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", email], cwd=root, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=root, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "awf.snapshotMarker", email],
        cwd=root,
        check=True,
        capture_output=True,
    )
    (root / "tracked.txt").write_text(blob, encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", blob], cwd=root, check=True, capture_output=True)
    hashed = subprocess.run(
        ["git", "hash-object", "tracked.txt"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return hashed.stdout.strip()


@pytest.mark.unit
def test_untrusted_nested_probe_config_snapshot_keeps_pinned_git_dir_after_root_symlink_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6evMAl: snapshot must not reopen a resolved git-dir pathname.

    After git-dir discovery, swapping the nested root for a symlink to a decoy
    must not divert config/HEAD/objects onto the decoy while the caller still
    holds a descriptor to the original worktree.
    """
    nested = tmp_path / "nested"
    decoy = tmp_path / "decoy"
    original_blob = _init_marked_nested_repo(
        nested, email="original-nested@example.com", blob="original-blob\n"
    )
    decoy_blob = _init_marked_nested_repo(
        decoy, email="decoy-nested@example.com", blob="decoy-blob\n"
    )
    assert original_blob != decoy_blob
    original_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=nested,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    decoy_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=decoy,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    nested_fd = os.open(nested, os.O_RDONLY | os.O_DIRECTORY)
    real_scan = git_manager_ownership._nested_repository_git_dirs_for_include_scan

    def _scan_then_swap(
        nested_root: Path,
        *,
        containment_roots: object = None,
    ) -> object:
        result = real_scan(nested_root, containment_roots=containment_roots)  # type: ignore[arg-type]
        if nested.is_dir() and not nested.is_symlink():
            backup = tmp_path / "nested.bak"
            nested.rename(backup)
            nested.symlink_to(decoy.resolve())
        return result

    monkeypatch.setattr(
        git_manager_ownership,
        "_nested_repository_git_dirs_for_include_scan",
        _scan_then_swap,
    )
    try:
        snapshot_root = Path(f"/proc/self/fd/{nested_fd}")
        with git_manager.untrusted_nested_probe_config_snapshot_git_dir(snapshot_root) as shadow:
            assert shadow is not None
            config = (shadow / "config").read_text(encoding="utf-8")
            assert "original-nested@example.com" in config
            assert "decoy-nested@example.com" not in config
            head = (shadow / "HEAD").read_text(encoding="utf-8")
            assert original_head in head or "ref:" in head
            snap_head = subprocess.run(
                ["git", "--git-dir", str(shadow), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            assert snap_head == original_head
            assert snap_head != decoy_head
            cat = subprocess.run(
                ["git", "--git-dir", str(shadow), "cat-file", "-p", original_blob],
                check=True,
                capture_output=True,
                text=True,
            )
            assert cat.stdout == "original-blob\n"
            decoy_cat = subprocess.run(
                ["git", "--git-dir", str(shadow), "cat-file", "-t", decoy_blob],
                check=False,
                capture_output=True,
            )
            assert decoy_cat.returncode != 0
    finally:
        os.close(nested_fd)


@pytest.mark.unit
def test_pinned_snapshot_helper_fail_closed_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed edges for fd-pinned nested probe snapshot helpers."""
    assert git_manager_ownership._proc_self_fd_number(tmp_path) is None
    assert git_manager_ownership._open_nested_root_directory_fd(tmp_path / "missing") is None

    regular = tmp_path / "regular"
    regular.write_text("x\n", encoding="utf-8")
    regular_fd = os.open(regular, os.O_RDONLY)
    try:
        assert (
            git_manager_ownership._open_nested_root_directory_fd(
                Path(f"/proc/self/fd/{regular_fd}")
            )
            is None
        )
    finally:
        os.close(regular_fd)

    nested = tmp_path / "nested"
    nested.mkdir()
    nested_fd = os.open(nested, os.O_RDONLY | os.O_DIRECTORY)
    try:
        monkeypatch.setattr(os, "dup", lambda _fd: (_ for _ in ()).throw(OSError("dup")))
        assert (
            git_manager_ownership._open_nested_root_directory_fd(Path(f"/proc/self/fd/{nested_fd}"))
            is None
        )
        monkeypatch.undo()

        real_fstat = os.fstat

        def _fstat_oserror(fd: int) -> os.stat_result:
            if fd != nested_fd:
                raise OSError("fstat failed")
            return real_fstat(fd)

        monkeypatch.setattr(os, "fstat", _fstat_oserror)
        assert (
            git_manager_ownership._open_nested_root_directory_fd(Path(f"/proc/self/fd/{nested_fd}"))
            is None
        )
        monkeypatch.undo()

        assert (
            git_manager_ownership._open_relative_directory_from_dir_fd(nested_fd, Path("/abs"))
            is None
        )
        assert (
            git_manager_ownership._open_relative_directory_from_dir_fd(
                nested_fd, Path("missing-child")
            )
            is None
        )
        child = nested / "child"
        child.mkdir()
        walked = git_manager_ownership._open_relative_directory_from_dir_fd(
            nested_fd, Path("./child")
        )
        assert walked is not None
        os.close(walked)
        walked = git_manager_ownership._open_relative_directory_from_dir_fd(nested_fd, Path())
        assert walked is not None
        os.close(walked)

        monkeypatch.setattr(os, "dup", lambda _fd: (_ for _ in ()).throw(OSError("dup")))
        assert (
            git_manager_ownership._open_relative_directory_from_dir_fd(nested_fd, Path("child"))
            is None
        )
        monkeypatch.undo()

        assert (
            git_manager_ownership._open_git_metadata_candidate(
                Path(""),  # noqa: PTH201 - empty path has no parts; Path() is "."
                base_fd=nested_fd,
                containment_roots=(nested,),
            )
            is None
        )
        assert (
            git_manager_ownership._open_git_metadata_candidate(
                Path("../outside"), base_fd=nested_fd, containment_roots=(nested,)
            )
            is None
        )

        outside = tmp_path / "outside.git"
        outside.mkdir()
        assert git_manager_ownership._open_contained_directory_nofollow(outside, (nested,)) is None
        owned = git_manager_ownership._open_contained_directory_nofollow(nested, (nested,))
        assert owned is not None
        os.close(owned)

        real_resolve = Path.resolve

        def _boom(self: Path, *, strict: bool = False) -> Path:
            del strict
            if self == outside:
                raise OSError("unreadable")
            return real_resolve(self)

        monkeypatch.setattr(Path, "resolve", _boom)
        assert git_manager_ownership._open_contained_directory_nofollow(outside, (nested,)) is None
        monkeypatch.undo()

        assert (
            git_manager_ownership._open_nested_probe_git_dir_fds(
                nested_fd, containment_roots=(nested,)
            )
            is None
        )

        fifo = nested / ".git"
        os.mkfifo(fifo)
        assert (
            git_manager_ownership._open_nested_probe_git_dir_fds(
                nested_fd, containment_roots=(nested,)
            )
            is None
        )
        fifo.unlink()

        (nested / ".git").write_text("not-a-gitdir\n", encoding="utf-8")
        assert (
            git_manager_ownership._open_nested_probe_git_dir_fds(
                nested_fd, containment_roots=(nested,)
            )
            is None
        )
        (nested / ".git").unlink()

        git_dir = nested / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git_dir / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
        opened = git_manager_ownership._open_nested_probe_git_dir_fds(
            nested_fd, containment_roots=(tmp_path / "elsewhere",)
        )
        assert opened is None

        opened = git_manager_ownership._open_nested_probe_git_dir_fds(
            nested_fd, containment_roots=(nested,)
        )
        assert opened is not None
        primary_fd, object_fd = opened
        assert primary_fd == object_fd
        snap = git_manager_ownership._snapshot_git_dir_local_configs_via_fd(primary_fd)
        assert snap is not None and "config" in snap
        os.close(primary_fd)

        (git_dir / "config").unlink()
        (git_dir / "config").mkdir()
        primary_fd = git_manager_ownership._open_git_dir_child_directory_fd(nested_fd, ".git")
        assert primary_fd is not None
        assert git_manager_ownership._snapshot_git_dir_local_configs_via_fd(primary_fd) == {}
        (git_dir / "config").rmdir()
        target = tmp_path / "cfg-target"
        target.write_text("[core]\n\tbare = false\n", encoding="utf-8")
        (git_dir / "config").symlink_to(target)
        assert git_manager_ownership._snapshot_git_dir_local_configs_via_fd(primary_fd) is None
        (git_dir / "config").unlink()
        (git_dir / "config").write_text(
            "[include]\n\tpath = /tmp/x.inc\n",
            encoding="utf-8",
        )
        assert git_manager_ownership._snapshot_git_dir_local_configs_via_fd(primary_fd) is None
        (git_dir / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
        monkeypatch.setattr(
            git_manager_ownership,
            "_read_git_dir_child_text_via_fd",
            lambda *_args, **_kwargs: None,
        )
        assert git_manager_ownership._snapshot_git_dir_local_configs_via_fd(primary_fd) is None
        monkeypatch.undo()
        real_stat = os.stat

        def _stat_oserror(
            path: str | bytes | os.PathLike[str], *args: object, **kwargs: object
        ) -> os.stat_result:
            if path == "config":
                raise OSError("stat failed")
            return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "stat", _stat_oserror)
        assert git_manager_ownership._snapshot_git_dir_local_configs_via_fd(primary_fd) == {}
        monkeypatch.undo()
        os.close(primary_fd)

        (git_dir / "commondir").symlink_to(tmp_path / "missing-common")
        assert (
            git_manager_ownership._open_nested_probe_git_dir_fds(
                nested_fd, containment_roots=(nested,)
            )
            is None
        )
        (git_dir / "commondir").unlink()
        (git_dir / "commondir").mkdir()
        opened = git_manager_ownership._open_nested_probe_git_dir_fds(
            nested_fd, containment_roots=(nested,)
        )
        assert opened is not None
        os.close(opened[0])
        (git_dir / "commondir").rmdir()
        (git_dir / "commondir").write_text("   \n", encoding="utf-8")
        opened = git_manager_ownership._open_nested_probe_git_dir_fds(
            nested_fd, containment_roots=(nested,)
        )
        assert opened is not None
        os.close(opened[0])
        (git_dir / "commondir").write_text(f"{tmp_path / 'escape.git'}\n", encoding="utf-8")
        assert (
            git_manager_ownership._open_nested_probe_git_dir_fds(
                nested_fd, containment_roots=(nested,)
            )
            is None
        )
        (git_dir / "commondir").unlink()

        monkeypatch.setattr(
            git_manager_ownership,
            "_open_git_dir_child_directory_fd",
            lambda *_args, **_kwargs: None,
        )
        assert (
            git_manager_ownership._open_nested_probe_git_dir_fds(
                nested_fd, containment_roots=(nested,)
            )
            is None
        )
        monkeypatch.undo()

        shutil.rmtree(git_dir)
        (nested / ".git").write_text("gitdir: \n", encoding="utf-8")
        assert (
            git_manager_ownership._open_nested_probe_git_dir_fds(
                nested_fd, containment_roots=(nested,)
            )
            is None
        )
        (nested / ".git").write_text("gitdir: linked.git\n", encoding="utf-8")
        monkeypatch.setattr(
            git_manager_ownership,
            "_read_git_dir_child_text_via_fd",
            lambda *_args, **_kwargs: None,
        )
        assert (
            git_manager_ownership._open_nested_probe_git_dir_fds(
                nested_fd, containment_roots=(nested,)
            )
            is None
        )
        monkeypatch.undo()
        assert (
            git_manager_ownership._open_nested_probe_git_dir_fds(
                nested_fd, containment_roots=(nested,)
            )
            is None
        )
        (nested / ".git").unlink()
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git_dir / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
        (git_dir / "commondir").write_text("../common.git\n", encoding="utf-8")
        real_stat = os.stat

        def _commondir_oserror(
            path: str | bytes | os.PathLike[str], *args: object, **kwargs: object
        ) -> os.stat_result:
            if path == "commondir":
                raise OSError("commondir unreadable")
            return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "stat", _commondir_oserror)
        assert (
            git_manager_ownership._open_nested_probe_git_dir_fds(
                nested_fd, containment_roots=(nested,)
            )
            is None
        )
        monkeypatch.undo()
        monkeypatch.setattr(
            git_manager_ownership,
            "_read_git_dir_child_text_via_fd",
            lambda _fd, name, **_kwargs: None if name == "commondir" else "ok",
        )
        assert (
            git_manager_ownership._open_nested_probe_git_dir_fds(
                nested_fd, containment_roots=(nested,)
            )
            is None
        )
        monkeypatch.undo()
        (git_dir / "commondir").unlink()
        monkeypatch.setattr(
            git_manager_ownership,
            "_open_git_dir_directory_fd",
            lambda _path: None,
        )
        assert git_manager_ownership._open_contained_directory_nofollow(child, (nested,)) is None
        monkeypatch.undo()
    finally:
        os.close(nested_fd)


@pytest.mark.unit
def test_untrusted_nested_probe_config_snapshot_fail_closed_on_roots_and_common_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshot fails closed when containment roots or common config cannot be used."""
    nested = tmp_path / "nested"
    nested.mkdir()
    subprocess.run(["git", "init"], cwd=nested, check=True, capture_output=True)
    real_roots = git_manager_ownership._nested_git_metadata_containment_roots
    root_calls = {"n": 0}

    def _roots_fail_after_scan(
        nested_root: Path,
        containment_roots: object = None,
    ) -> object:
        root_calls["n"] += 1
        if root_calls["n"] == 1:
            return real_roots(nested_root, containment_roots)  # type: ignore[arg-type]
        return None

    monkeypatch.setattr(
        git_manager_ownership,
        "_nested_git_metadata_containment_roots",
        _roots_fail_after_scan,
    )
    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is None
    monkeypatch.undo()

    common = nested / "common.git"
    subprocess.run(["git", "init", "--bare", str(common)], check=True, capture_output=True)
    real_git = nested / "linked.git"
    real_git.mkdir()
    (real_git / "commondir").write_text(f"{common}\n", encoding="utf-8")
    (real_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (real_git / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n\tbare = false\n",
        encoding="utf-8",
    )
    (common / "config").write_text(
        "[include]\n\tpath = /tmp/from-common.inc\n",
        encoding="utf-8",
    )
    shutil.rmtree(nested / ".git")
    (nested / ".git").write_text(f"gitdir: {real_git}\n", encoding="utf-8")
    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is None


@pytest.mark.unit
def test_untrusted_nested_probe_config_snapshot_survives_git_dir_rename(
    tmp_path: Path,
) -> None:
    """Snapshot object copies must not follow a post-materialization ``.git`` rename.

    Pin-fd probes rename the opened git-dir to ``.git.real`` and plant an
    attacker symlink at ``.git``. Absolute staging pathnames into ``.git/...``
    would then resolve through the evil path; private copies taken through the
    opened git-dir fd must keep the original objects (PRRT_kwDOSJAM6s6eXrkk
    family).
    """
    nested = tmp_path / "nested"
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
    (nested / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=nested, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=nested,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    evil = tmp_path / "evil"
    evil.mkdir()
    subprocess.run(["git", "init"], cwd=evil, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "evil@example.com"],
        cwd=evil,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Evil"],
        cwd=evil,
        check=True,
        capture_output=True,
    )
    (evil / "evil.txt").write_text("evil\n", encoding="utf-8")
    subprocess.run(["git", "add", "evil.txt"], cwd=evil, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "evil"], cwd=evil, check=True, capture_output=True)
    evil_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=evil,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert evil_head != before

    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is not None
        git_marker = nested / ".git"
        git_marker.rename(nested / ".git.real")
        git_marker.symlink_to(evil / ".git")

        after = subprocess.run(
            [
                "git",
                "--git-dir",
                str(shadow),
                "--work-tree",
                str(nested),
                *git_manager.UNTRUSTED_NESTED_GIT_CONFIG_ARGS,
                "rev-parse",
                "HEAD",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert after == before
        assert after != evil_head


@pytest.mark.unit
def test_open_git_dir_directory_fd_fail_closed_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Directory-fd open must fail closed on missing paths and non-directory fstat."""
    missing = tmp_path / "missing-git"
    assert git_manager_ownership._open_git_dir_directory_fd(missing) is None

    regular = tmp_path / "not-a-dir"
    regular.write_text("x\n", encoding="utf-8")
    assert git_manager_ownership._open_git_dir_directory_fd(regular) is None

    real_dir = tmp_path / "real-dir"
    real_dir.mkdir()
    real_fstat = os.fstat

    def _not_dir(fd: int) -> os.stat_result:
        st = real_fstat(fd)
        return os.stat_result(
            (
                stat.S_IFREG | 0o644,
                st.st_ino,
                st.st_dev,
                st.st_nlink,
                st.st_uid,
                st.st_gid,
                st.st_size,
                st.st_atime,
                st.st_mtime,
                st.st_ctime,
            )
        )

    monkeypatch.setattr(os, "fstat", _not_dir)
    assert git_manager_ownership._open_git_dir_directory_fd(real_dir) is None
    monkeypatch.setattr(os, "fstat", real_fstat)

    def _fstat_oserror(fd: int) -> os.stat_result:
        raise OSError("fstat failed")

    monkeypatch.setattr(os, "fstat", _fstat_oserror)
    assert git_manager_ownership._open_git_dir_directory_fd(real_dir) is None


@pytest.mark.unit
def test_untrusted_nested_probe_config_snapshot_fails_when_git_dir_unopenable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshot must fail closed when the primary git-dir cannot be pinned open."""
    nested = tmp_path / "nested"
    nested.mkdir()
    subprocess.run(["git", "init"], cwd=nested, check=True, capture_output=True)
    monkeypatch.setattr(
        git_manager_ownership,
        "_open_git_dir_directory_fd",
        lambda _path: None,
    )
    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is None


@pytest.mark.unit
def test_untrusted_nested_probe_config_snapshot_pins_separate_commondir(
    tmp_path: Path,
) -> None:
    """Separate ``commondir`` objects must be copied via the opened common-dir fd."""
    nested = tmp_path / "nested"
    nested.mkdir()
    common = nested / "common.git"
    subprocess.run(["git", "init", "--bare", str(common)], check=True, capture_output=True)
    real_git = nested / "linked.git"
    real_git.mkdir()
    (real_git / "commondir").write_text(f"{common}\n", encoding="utf-8")
    (real_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (real_git / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n\tbare = false\n",
        encoding="utf-8",
    )
    (nested / ".git").write_text(f"gitdir: {real_git}\n", encoding="utf-8")
    # Seed an object in the common store so the snapshot can resolve it.
    blob = subprocess.run(
        ["git", "--git-dir", str(common), "hash-object", "-w", "--stdin"],
        input=b"commondir-blob\n",
        check=True,
        capture_output=True,
    ).stdout.strip()

    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is not None
        assert (shadow / "objects").is_dir()
        assert not (shadow / "objects").is_symlink()
        assert not (shadow / "objects" / "info").exists()
        # Fan-out / pack dirs are materialized; leaf files are private copies
        # (PRRT_kwDOSJAM6s6eq1r3 / PRRT_kwDOSJAM6s6eteRs).
        leaf_files = [
            p for p in (shadow / "objects").rglob("*") if p.is_file() and not p.is_symlink()
        ]
        assert leaf_files
        assert not any(p.is_symlink() for p in (shadow / "objects").rglob("*") if p.is_file())
        cat = subprocess.run(
            [
                "git",
                "--git-dir",
                str(shadow),
                "cat-file",
                "-t",
                blob.decode("ascii"),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert cat.stdout.strip() == "blob"


@pytest.mark.unit
def test_untrusted_nested_probe_config_snapshot_fails_when_commondir_unopenable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshot must fail closed when a separate common-dir cannot be opened."""
    nested = tmp_path / "nested"
    nested.mkdir()
    common = nested / "common.git"
    subprocess.run(["git", "init", "--bare", str(common)], check=True, capture_output=True)
    real_git = nested / "linked.git"
    real_git.mkdir()
    (real_git / "commondir").write_text(f"{common}\n", encoding="utf-8")
    (real_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (real_git / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n\tbare = false\n",
        encoding="utf-8",
    )
    (nested / ".git").write_text(f"gitdir: {real_git}\n", encoding="utf-8")

    real_open = git_manager_ownership._open_relative_directory_from_dir_fd
    calls = {"n": 0}

    def _fail_second(dir_fd: int, relative: Path) -> int | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return real_open(dir_fd, relative)
        return None

    monkeypatch.setattr(
        git_manager_ownership,
        "_open_relative_directory_from_dir_fd",
        _fail_second,
    )
    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is None
