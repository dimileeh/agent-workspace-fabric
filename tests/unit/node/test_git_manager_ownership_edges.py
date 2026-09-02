"""Focused ownership and stale-worktree GitManager edge tests."""

from __future__ import annotations

import hashlib
import os
import stat
import struct
import subprocess
import time
from pathlib import Path

import pytest

import awf.node.git_manager as git_manager
import awf.node.git_manager_ownership as git_manager_ownership
from awf.node.git_manager import GitManager


@pytest.mark.unit
def test_chown_tree_skips_symlink_targets_using_lchown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target"
    target.write_text("target", encoding="utf-8")
    root = tmp_path / "symlink-root"
    root.symlink_to(target)
    linked_target = tmp_path / "linked-target"
    linked_target.write_text("linked-target", encoding="utf-8")
    directory = tmp_path / "worktree"
    directory.mkdir()
    linked_child = directory / "linked-child"
    linked_child.symlink_to(linked_target)
    linked_directory_target = tmp_path / "linked-directory-target"
    linked_directory_target.mkdir()
    linked_child_directory = directory / "linked-child-directory"
    linked_child_directory.symlink_to(linked_directory_target, target_is_directory=True)
    child_file = directory / "file"
    child_file.write_text("file", encoding="utf-8")
    chowned: list[Path] = []
    lchowned: list[Path] = []

    def _record_chown(path: str | bytes, _uid: int, _gid: int) -> None:
        del _uid, _gid
        chowned.append(Path(path))

    def _record_lchown(path: str | bytes, _uid: int, _gid: int) -> None:
        del _uid, _gid
        lchowned.append(Path(path))

    monkeypatch.setattr(git_manager.os, "chown", _record_chown)
    monkeypatch.setattr(git_manager.os, "lchown", _record_lchown)

    git_manager._chown_tree(root, 1000, 1000)  # noqa: SLF001
    git_manager._chown_tree(directory, 1000, 1000)  # noqa: SLF001

    assert set(lchowned) == {root, linked_child, linked_child_directory}
    assert set(chowned) >= {directory, child_file}
    assert target not in chowned
    assert linked_target not in chowned
    assert linked_directory_target not in chowned


@pytest.mark.unit
def test_reclaim_stale_worktree_treats_already_removed_directory_as_success(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "already-removed"

    GitManager._reclaim_stale_worktree(missing)  # noqa: SLF001

    assert not missing.exists()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[core]\n\tfilemode = true\n", False),
        ("[include]\n\tpath = /other/x.inc\n", True),
        ('[includeIf "gitdir:**"]\n\tpath = ../x.inc\n', True),
        ("; [include]\n; path = x\n[user]\n\tname = t\n", False),
        ("[include]\n\t# path = commented\n[user]\n\tname = t\n", False),
        ("[core]\n\tpath = not-an-include\n", False),
        # UTF-8 BOM must not hide a leading [include] (PRRT_kwDOSJAM6s6elA2I).
        ("\ufeff[include]\n\tpath = /other/bom.inc\n", True),
        ('\ufeff[includeIf "gitdir:**"]\n\tpath = ../bom.inc\n', True),
    ],
)
def test_git_config_text_declares_includes(text: str, expected: bool) -> None:
    assert git_manager.git_config_text_declares_includes(text) is expected


@pytest.mark.unit
def test_untrusted_nested_git_config_args_override_foreign_excludes_file(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6elh7f: nested probes must ignore agent-set core.excludesFile."""
    nested = tmp_path / "nested"
    nested.mkdir()
    other_ws = tmp_path / "other_ws"
    other_ws.mkdir()
    foreign_excludes = other_ws / "foreign.exclude"
    foreign_excludes.write_text("hidden.txt\n", encoding="utf-8")
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
    (nested / "hidden.txt").write_text("untracked residue\n", encoding="utf-8")
    subprocess.run(
        ["git", "config", "core.excludesFile", str(foreign_excludes)],
        cwd=nested,
        check=True,
        capture_output=True,
    )

    poisoned = subprocess.run(
        ["git", "ls-files", "-o", "--exclude-standard", "-z"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    assert b"hidden.txt" not in poisoned.stdout.split(b"\0")

    sanitized = subprocess.run(
        [
            "git",
            *git_manager.UNTRUSTED_NESTED_GIT_CONFIG_ARGS,
            "ls-files",
            "-o",
            "--exclude-standard",
            "-z",
        ],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    assert b"hidden.txt" in sanitized.stdout.split(b"\0")


@pytest.mark.unit
def test_untrusted_nested_git_config_args_override_core_symlinks_false(
    tmp_path: Path,
) -> None:
    """Review 5093517929: core.symlinks=false must not hide symlink→file typechanges."""
    assert "core.symlinks=true" in git_manager.UNTRUSTED_NESTED_GIT_CONFIG_ARGS

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
    # Record as a symlink first (Variant A): with only local core.symlinks=false,
    # some Git versions hide the later symlink→file typechange when link text matches.
    subprocess.run(
        ["git", "config", "core.symlinks", "true"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    link = nested / "link"
    link.symlink_to("target")
    subprocess.run(["git", "add", "link"], cwd=nested, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "core.symlinks", "false"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    link.unlink()
    link.write_bytes(b"target")

    poisoned = subprocess.run(
        ["git", "diff-files", "--name-only"],
        cwd=nested,
        check=True,
        capture_output=True,
        text=True,
    )
    # Variant A recipe: local core.symlinks=false hides this typechange on Git 2.39.5+.
    assert poisoned.stdout.strip() == ""

    sanitized = subprocess.run(
        [
            "git",
            *git_manager.UNTRUSTED_NESTED_GIT_CONFIG_ARGS,
            "diff-files",
            "--name-only",
        ],
        cwd=nested,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "link" in sanitized.stdout.splitlines()


@pytest.mark.unit
def test_untrusted_nested_probe_config_snapshot_ignores_info_exclude(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6enFGg: snapshot must not honor live .git/info/exclude."""
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
    (nested / "secret.txt").write_text("untracked residue\n", encoding="utf-8")
    info_dir = nested / ".git" / "info"
    info_dir.mkdir(exist_ok=True)
    (info_dir / "exclude").write_text("secret.txt\n", encoding="utf-8")

    poisoned = subprocess.run(
        ["git", "ls-files", "-o", "--exclude-standard", "-z"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    assert b"secret.txt" not in poisoned.stdout.split(b"\0")

    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is not None
        assert not (shadow / "info").is_symlink()
        sanitized = subprocess.run(
            [
                "git",
                "--git-dir",
                str(shadow),
                "--work-tree",
                str(nested),
                *git_manager.UNTRUSTED_NESTED_GIT_CONFIG_ARGS,
                "ls-files",
                "-o",
                "--exclude-standard",
                "-z",
            ],
            check=True,
            capture_output=True,
        )
        assert b"secret.txt" in sanitized.stdout.split(b"\0")


@pytest.mark.unit
def test_git_dir_declares_object_alternates_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alternates probe: absent is clean; present / unreadable fail closed."""
    git_dir = tmp_path / "repo.git"
    git_dir.mkdir()
    fd = git_manager_ownership._open_git_dir_directory_fd(git_dir)
    assert fd is not None
    try:
        assert git_manager_ownership._git_dir_declares_object_alternates(fd) is False
    finally:
        os.close(fd)

    objects = git_dir / "objects"
    objects.mkdir()
    fd = git_manager_ownership._open_git_dir_directory_fd(git_dir)
    assert fd is not None
    try:
        assert git_manager_ownership._git_dir_declares_object_alternates(fd) is False
    finally:
        os.close(fd)

    info = objects / "info"
    info.mkdir()
    fd = git_manager_ownership._open_git_dir_directory_fd(git_dir)
    assert fd is not None
    try:
        assert git_manager_ownership._git_dir_declares_object_alternates(fd) is False
    finally:
        os.close(fd)

    (info / "alternates").write_text("/tmp/foreign-objects\n", encoding="utf-8")
    fd = git_manager_ownership._open_git_dir_directory_fd(git_dir)
    assert fd is not None
    try:
        assert git_manager_ownership._git_dir_declares_object_alternates(fd) is True
    finally:
        os.close(fd)

    # Symlink ``objects`` cannot be opened with O_NOFOLLOW → fail closed.
    poisoned = tmp_path / "poisoned.git"
    poisoned.mkdir()
    (poisoned / "objects").symlink_to(objects, target_is_directory=True)
    fd = git_manager_ownership._open_git_dir_directory_fd(poisoned)
    assert fd is not None
    try:
        assert git_manager_ownership._git_dir_declares_object_alternates(fd) is True
    finally:
        os.close(fd)

    # Symlink ``info`` under a real objects dir → fail closed.
    info_link_repo = tmp_path / "info-link.git"
    info_link_repo.mkdir()
    (info_link_repo / "objects").mkdir()
    (info_link_repo / "objects" / "info").symlink_to(info, target_is_directory=True)
    fd = git_manager_ownership._open_git_dir_directory_fd(info_link_repo)
    assert fd is not None
    try:
        assert git_manager_ownership._git_dir_declares_object_alternates(fd) is True
    finally:
        os.close(fd)

    clean = tmp_path / "clean.git"
    clean.mkdir()
    (clean / "objects").mkdir()
    (clean / "objects" / "info").mkdir()
    fd = git_manager_ownership._open_git_dir_directory_fd(clean)
    assert fd is not None
    real_stat = os.stat
    try:

        def _stat_objects_boom(
            path: str | bytes | os.PathLike[str], *args: object, **kwargs: object
        ) -> os.stat_result:
            if path == "objects":
                raise OSError("stat failed")
            return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "stat", _stat_objects_boom)
        assert git_manager_ownership._git_dir_declares_object_alternates(fd) is True
        monkeypatch.setattr(os, "stat", real_stat)

        def _stat_info_boom(
            path: str | bytes | os.PathLike[str], *args: object, **kwargs: object
        ) -> os.stat_result:
            if path == "info":
                raise OSError("info stat failed")
            return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "stat", _stat_info_boom)
        assert git_manager_ownership._git_dir_declares_object_alternates(fd) is True
        monkeypatch.setattr(os, "stat", real_stat)

        def _stat_alternates_boom(
            path: str | bytes | os.PathLike[str], *args: object, **kwargs: object
        ) -> os.stat_result:
            if path == "alternates":
                raise OSError("alternates stat failed")
            return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "stat", _stat_alternates_boom)
        assert git_manager_ownership._git_dir_declares_object_alternates(fd) is True
    finally:
        monkeypatch.setattr(os, "stat", real_stat)
        os.close(fd)


@pytest.mark.unit
def test_open_git_dir_child_directory_fd_fail_closed_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Child directory openat must reject missing, non-dir, and fstat failures."""
    parent = tmp_path / "parent"
    parent.mkdir()
    parent_fd = git_manager_ownership._open_git_dir_directory_fd(parent)
    assert parent_fd is not None
    try:
        assert git_manager_ownership._open_git_dir_child_directory_fd(parent_fd, "missing") is None
        (parent / "file").write_text("x\n", encoding="utf-8")
        assert git_manager_ownership._open_git_dir_child_directory_fd(parent_fd, "file") is None

        child = parent / "child"
        child.mkdir()
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
        assert git_manager_ownership._open_git_dir_child_directory_fd(parent_fd, "child") is None
        monkeypatch.setattr(os, "fstat", real_fstat)

        def _fstat_raises(fd: int) -> os.stat_result:
            raise OSError("fstat failed")

        monkeypatch.setattr(os, "fstat", _fstat_raises)
        assert git_manager_ownership._open_git_dir_child_directory_fd(parent_fd, "child") is None
        monkeypatch.setattr(os, "fstat", real_fstat)
    finally:
        os.close(parent_fd)


@pytest.mark.unit
def test_untrusted_nested_probe_config_snapshot_rejects_object_alternates(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ep1TL: objects/info/alternates must not cross workspace stores.

    Symlinking the live ``objects`` directory preserves ``info/alternates``. After
    the local commit object is deleted and only exists in a foreign store pointed
    at by that file, sanitized ``rev-parse HEAD^{commit}`` still succeeds —
    foreign object churn can toggle nested fingerprint readability.
    """
    nested = tmp_path / "nested"
    nested.mkdir()
    foreign = tmp_path / "foreign.git"
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
    oid = subprocess.run(
        ["git", "rev-parse", "HEAD^{commit}"],
        cwd=nested,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "init", "--bare", str(foreign)], check=True, capture_output=True)
    prefix, rest = oid[:2], oid[2:]
    local_obj = nested / ".git" / "objects" / prefix / rest
    foreign_obj_dir = foreign / "objects" / prefix
    foreign_obj_dir.mkdir(parents=True, exist_ok=True)
    foreign_obj_dir.joinpath(rest).write_bytes(local_obj.read_bytes())
    local_obj.unlink()
    assert (
        subprocess.run(
            ["git", "rev-parse", "HEAD^{commit}"],
            cwd=nested,
            capture_output=True,
        ).returncode
        != 0
    )
    info_dir = nested / ".git" / "objects" / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    (info_dir / "alternates").write_text(f"{foreign / 'objects'}\n", encoding="utf-8")
    live = subprocess.run(
        ["git", "rev-parse", "HEAD^{commit}"],
        cwd=nested,
        check=True,
        capture_output=True,
        text=True,
    )
    assert live.stdout.strip() == oid

    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is None


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
            assert link.is_symlink(), f"snapshot missing split-index backing link {name}"
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
        assert (shadow / real_name).is_symlink()
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
    try:
        git_manager_ownership._symlink_split_index_backing_files_via_fd(fd, staging)
        assert list(staging.iterdir()) == []
        # Non-split index: still no sharedindex link.
        header = b"DIRC" + struct.pack(">II", 2, 0)
        (git_dir / "index").write_bytes(header + hashlib.sha1(header).digest())
        git_manager_ownership._symlink_split_index_backing_files_via_fd(fd, staging)
        assert list(staging.iterdir()) == []
    finally:
        os.close(fd)


@pytest.mark.unit
def test_untrusted_nested_probe_config_snapshot_survives_git_dir_rename(
    tmp_path: Path,
) -> None:
    """Snapshot object links must not follow a post-materialization ``.git`` rename.

    Pin-fd probes rename the opened git-dir to ``.git.real`` and plant an
    attacker symlink at ``.git``. Absolute staging symlinks into ``.git/...``
    would then resolve through the evil path; links via a held directory fd
    must keep the original objects (PRRT_kwDOSJAM6s6eXrkk family).
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
    """Separate ``commondir`` objects must be linked via a held common-dir fd."""
    nested = tmp_path / "nested"
    nested.mkdir()
    common = tmp_path / "common.git"
    subprocess.run(["git", "init", "--bare", str(common)], check=True, capture_output=True)
    real_git = tmp_path / "linked.git"
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
        objects_target = (shadow / "objects").readlink()
        assert f"/proc/{os.getpid()}/fd/" in str(objects_target)
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
    common = tmp_path / "common.git"
    subprocess.run(["git", "init", "--bare", str(common)], check=True, capture_output=True)
    real_git = tmp_path / "linked.git"
    real_git.mkdir()
    (real_git / "commondir").write_text(f"{common}\n", encoding="utf-8")
    (real_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (real_git / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n\tbare = false\n",
        encoding="utf-8",
    )
    (nested / ".git").write_text(f"gitdir: {real_git}\n", encoding="utf-8")

    real_open = git_manager_ownership._open_git_dir_directory_fd
    calls = {"n": 0}

    def _fail_second(path: Path) -> int | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return real_open(path)
        return None

    monkeypatch.setattr(git_manager_ownership, "_open_git_dir_directory_fd", _fail_second)
    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is None


@pytest.mark.unit
def test_untrusted_nested_repository_local_config_has_includes(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    git_dir = nested / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n\tfilemode = true\n", encoding="utf-8")
    assert git_manager.untrusted_nested_repository_local_config_has_includes(nested) is False

    (git_dir / "config").write_text(
        "[core]\n\tfilemode = true\n[include]\n\tpath = /tmp/x.inc\n",
        encoding="utf-8",
    )
    assert git_manager.untrusted_nested_repository_local_config_has_includes(nested) is True


@pytest.mark.unit
def test_untrusted_nested_repository_local_config_has_includes_utf8_bom(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6elA2I: Git honors BOM-prefixed config; include scan must too."""
    nested = tmp_path / "nested"
    nested.mkdir()
    git_dir = nested / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_bytes(b"\xef\xbb\xbf[include]\n\tpath = /tmp/bom.inc\n")
    assert git_manager.untrusted_nested_repository_local_config_has_includes(nested) is True


@pytest.mark.unit
def test_untrusted_nested_git_dir_symlink_config_fails_closed(tmp_path: Path) -> None:
    git_dir = tmp_path / "git"
    git_dir.mkdir()
    target = tmp_path / "real-config"
    target.write_text("[include]\n\tpath = /tmp/x.inc\n", encoding="utf-8")
    (git_dir / "config").symlink_to(target)
    assert git_manager.untrusted_nested_git_dir_declares_local_includes(git_dir) is True


@pytest.mark.unit
def test_untrusted_nested_repository_include_scan_gitfile_and_commondir(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    real_git = tmp_path / "real.git"
    real_git.mkdir()
    common = tmp_path / "common.git"
    common.mkdir()
    (common / "config").write_text(
        "[include]\n\tpath = /tmp/from-common.inc\n",
        encoding="utf-8",
    )
    (real_git / "commondir").write_text(f"{common}\n", encoding="utf-8")
    (real_git / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    (nested / ".git").write_text(f"gitdir: {real_git}\n", encoding="utf-8")
    assert git_manager.untrusted_nested_repository_local_config_has_includes(nested) is True


@pytest.mark.unit
def test_untrusted_nested_repository_include_scan_config_worktree(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    git_dir = nested / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n\tfilemode = true\n", encoding="utf-8")
    (git_dir / "config.worktree").write_text(
        '[includeIf "gitdir:**"]\n\tpath = /tmp/wt.inc\n',
        encoding="utf-8",
    )
    assert git_manager.untrusted_nested_repository_local_config_has_includes(nested) is True


@pytest.mark.unit
def test_untrusted_nested_repository_include_scan_missing_git_marker(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    assert git_manager.untrusted_nested_repository_local_config_has_includes(nested) is False


@pytest.mark.unit
def test_untrusted_nested_repository_include_scan_relative_gitfile_and_commondir(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    real_git = nested / "real.git"
    real_git.mkdir()
    common = nested / "common.git"
    common.mkdir()
    (common / "config").write_text(
        "[include]\n\tpath = relative-from-common.inc\n",
        encoding="utf-8",
    )
    (real_git / "commondir").write_text("../common.git\n", encoding="utf-8")
    (real_git / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    (nested / ".git").write_text("gitdir: real.git\n", encoding="utf-8")
    assert git_manager.untrusted_nested_repository_local_config_has_includes(nested) is True


@pytest.mark.unit
def test_untrusted_nested_repository_include_scan_invalid_gitfile(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / ".git").write_text("not-a-gitdir-pointer\n", encoding="utf-8")
    assert git_manager.untrusted_nested_repository_local_config_has_includes(nested) is False


@pytest.mark.unit
def test_untrusted_nested_git_dir_nonregular_config_ignored(tmp_path: Path) -> None:
    git_dir = tmp_path / "git"
    git_dir.mkdir()
    (git_dir / "config").mkdir()
    assert git_manager.untrusted_nested_git_dir_declares_local_includes(git_dir) is False


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_read_git_dir_config_text_fifo_does_not_hang(tmp_path: Path) -> None:
    """PRRT_kwDOSJAM6s6elA2N: FIFO after open must fail closed, not block."""
    fifo = tmp_path / "config"
    os.mkfifo(fifo)
    assert git_manager_ownership._read_git_dir_config_text(fifo) is None  # noqa: SLF001


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_read_git_dir_config_text_oversized_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6elA2N: oversize nested config must not be loaded unboundedly."""
    monkeypatch.setattr(git_manager_ownership, "_GIT_DIR_CONFIG_MAX_BYTES", 32)
    path = tmp_path / "config"
    path.write_text("[core]\n\tfilemode = true\n" + ("x" * 64), encoding="utf-8")
    assert git_manager_ownership._read_git_dir_config_text(path) is None  # noqa: SLF001


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_untrusted_nested_oversized_config_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Present regular config that cannot be snapshotted safely fails closed."""
    monkeypatch.setattr(git_manager_ownership, "_GIT_DIR_CONFIG_MAX_BYTES", 32)
    nested = tmp_path / "nested"
    nested.mkdir()
    git_dir = nested / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n\tfilemode = true\n" + ("x" * 64), encoding="utf-8")
    assert git_manager.untrusted_nested_repository_local_config_has_includes(nested) is True


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_read_git_dir_config_text_unstable_size_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6elA2N: mid-read growth must reject the torn snapshot."""
    path = tmp_path / "config"
    path.write_text("[core]\n\tfilemode = true\n", encoding="utf-8")
    real_fstat = os.fstat
    calls = {"n": 0}

    def _growing_fstat(fd: int) -> os.stat_result:
        st = real_fstat(fd)
        calls["n"] += 1
        if calls["n"] == 1:
            return st
        # Pretend the inode grew after the bounded read finished.
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
                st.st_atime_ns,
                st.st_mtime_ns,
                st.st_ctime_ns,
            )
        )

    monkeypatch.setattr(os, "fstat", _growing_fstat)
    assert git_manager_ownership._read_git_dir_config_text(path) is None  # noqa: SLF001


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_read_git_dir_config_text_deadline_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6elA2N: expired wall-time budget must fail closed."""
    path = tmp_path / "config"
    path.write_text("[core]\n\tfilemode = true\n", encoding="utf-8")
    monkeypatch.setattr(git_manager_ownership, "_GIT_DIR_CONFIG_READ_BUDGET_SECONDS", 0.0)
    # Force deadline check to see an already-expired clock after open.
    start = time.monotonic()
    monkeypatch.setattr(
        git_manager_ownership.time,
        "monotonic",
        lambda: start + 1.0,
    )
    assert git_manager_ownership._read_git_dir_config_text(path) is None  # noqa: SLF001


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_read_git_dir_config_text_reads_small_regular_file(tmp_path: Path) -> None:
    path = tmp_path / "config"
    path.write_text("[include]\n\tpath = /tmp/x.inc\n", encoding="utf-8")
    text = git_manager_ownership._read_git_dir_config_text(path)  # noqa: SLF001
    assert text is not None
    assert "include" in text


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_untrusted_nested_unsafe_gitfile_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Oversized / unreadable gitfile pointer must fail closed, not skip the nest."""
    monkeypatch.setattr(git_manager_ownership, "_GIT_DIR_CONFIG_MAX_BYTES", 8)
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / ".git").write_text("gitdir: " + ("x" * 64) + "\n", encoding="utf-8")
    assert git_manager.untrusted_nested_repository_local_config_has_includes(nested) is True


@pytest.mark.unit
def test_read_git_dir_config_text_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.write_text("[core]\n", encoding="utf-8")
    link = tmp_path / "config"
    link.symlink_to(target)
    assert git_manager_ownership._read_git_dir_config_text(link) is None  # noqa: SLF001
    assert stat.S_ISLNK(link.lstat().st_mode)


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_read_git_dir_config_text_short_read_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config"
    path.write_text("[core]\n\tfilemode = true\n", encoding="utf-8")
    monkeypatch.setattr(git_manager_ownership.os, "read", lambda _fd, _n: b"")
    assert git_manager_ownership._read_git_dir_config_text(path) is None  # noqa: SLF001


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_read_git_dir_config_text_read_oserror_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config"
    path.write_text("[core]\n\tfilemode = true\n", encoding="utf-8")

    def _boom(_fd: int, _n: int) -> bytes:
        raise OSError(5, "read failed")

    monkeypatch.setattr(git_manager_ownership.os, "read", _boom)
    assert git_manager_ownership._read_git_dir_config_text(path) is None  # noqa: SLF001


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_untrusted_nested_symlink_commondir_fails_closed(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    real_git = tmp_path / "real.git"
    real_git.mkdir()
    target = tmp_path / "common-target"
    target.write_text("../elsewhere\n", encoding="utf-8")
    (real_git / "commondir").symlink_to(target)
    (real_git / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    (nested / ".git").write_text(f"gitdir: {real_git}\n", encoding="utf-8")
    assert git_manager.untrusted_nested_repository_local_config_has_includes(nested) is True


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_untrusted_nested_oversized_commondir_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(git_manager_ownership, "_GIT_DIR_CONFIG_MAX_BYTES", 8)
    nested = tmp_path / "nested"
    nested.mkdir()
    real_git = tmp_path / "real.git"
    real_git.mkdir()
    (real_git / "commondir").write_text("x" * 64 + "\n", encoding="utf-8")
    (real_git / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    (nested / ".git").write_text(f"gitdir: {real_git}\n", encoding="utf-8")
    assert git_manager.untrusted_nested_repository_local_config_has_includes(nested) is True


@pytest.mark.unit
def test_untrusted_nested_probe_config_snapshot_isolates_live_includes(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6elv_p: shadow git-dir keeps validated config after live poison."""
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
    (nested / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=nested, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c"], cwd=nested, check=True, capture_output=True)
    poison = tmp_path / "poison.inc"
    poison.write_text("broken [[[[\n", encoding="utf-8")

    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is not None
        subprocess.run(
            ["git", "config", "include.path", str(poison)],
            cwd=nested,
            check=True,
            capture_output=True,
        )
        live = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=nested,
            check=False,
            capture_output=True,
        )
        assert live.returncode != 0
        snap = subprocess.run(
            ["git", "--git-dir", str(shadow), "--work-tree", str(nested), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
        )
        assert snap.returncode == 0
        assert snap.stdout.strip()


@pytest.mark.unit
def test_untrusted_nested_probe_config_snapshot_rejects_includes(
    tmp_path: Path,
) -> None:
    """Snapshot materialization must fail closed when local config already has includes."""
    nested = tmp_path / "nested"
    nested.mkdir()
    subprocess.run(["git", "init"], cwd=nested, check=True, capture_output=True)
    poison = tmp_path / "poison.inc"
    poison.write_text("broken [[[[\n", encoding="utf-8")
    subprocess.run(
        ["git", "config", "include.path", str(poison)],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is None


@pytest.mark.unit
def test_untrusted_nested_probe_config_snapshot_preserves_non_utf8_config_bytes(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6emdqr: surrogateescaped config bytes must round-trip into the snapshot."""
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
    (nested / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=nested, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c"], cwd=nested, check=True, capture_output=True)
    config_path = nested / ".git" / "config"
    config_path.write_bytes(config_path.read_bytes() + b"# comment with \xff non-utf8\n")
    assert git_manager.untrusted_nested_repository_local_config_has_includes(nested) is False

    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is not None
        assert b"\xff" in (shadow / "config").read_bytes()
        snap = subprocess.run(
            ["git", "--git-dir", str(shadow), "--work-tree", str(nested), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
        )
        assert snap.returncode == 0
        assert snap.stdout.strip()


@pytest.mark.unit
def test_untrusted_nested_probe_config_snapshot_rejects_symlink_head(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6emN9X: HEAD symlink must not be followed into the probe snapshot."""
    nested = tmp_path / "nested"
    nested.mkdir()
    subprocess.run(["git", "init"], cwd=nested, check=True, capture_output=True)
    foreign = tmp_path / "foreign-workspace-secret"
    foreign.write_text("ref: refs/heads/leaked-from-elsewhere\n", encoding="utf-8")
    head = nested / ".git" / "HEAD"
    head.unlink()
    head.symlink_to(foreign)

    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is None


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_untrusted_nested_probe_config_snapshot_rejects_fifo_head(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6emN9X: HEAD FIFO swap must fail closed without hanging."""
    nested = tmp_path / "nested"
    nested.mkdir()
    subprocess.run(["git", "init"], cwd=nested, check=True, capture_output=True)
    head = nested / ".git" / "HEAD"
    head.unlink()
    os.mkfifo(head)

    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is None


@pytest.mark.unit
def test_untrusted_nested_probe_config_snapshot_absolutizes_relative_core_worktree(
    tmp_path: Path,
) -> None:
    """Relative core.worktree must stay valid after config snapshot (review 5092778260)."""
    nested = tmp_path / "nested"
    redirected = tmp_path / "redirected"
    nested.mkdir()
    redirected.mkdir()
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
    (redirected / "tracked.txt").write_text("y\n", encoding="utf-8")
    subprocess.run(
        ["git", f"--work-tree={redirected}", "add", "tracked.txt"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", f"--work-tree={redirected}", "commit", "-m", "c"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    # Relative to nested/.git → tmp_path/redirected
    subprocess.run(
        ["git", "config", "core.worktree", "../../redirected"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    live = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=nested,
        check=True,
        capture_output=True,
        text=True,
    )
    assert Path(live.stdout.strip()).resolve() == redirected.resolve()

    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is not None
        snap_cfg = (shadow / "config").read_text(encoding="utf-8")
        assert "worktree = /" in snap_cfg or 'worktree = "/' in snap_cfg
        assert "../../redirected" not in snap_cfg
        snap = subprocess.run(
            [
                "git",
                "--git-dir",
                str(shadow),
                "-C",
                str(nested),
                "rev-parse",
                "--show-toplevel",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert snap.returncode == 0, snap.stderr
        assert Path(snap.stdout.strip()).resolve() == redirected.resolve()


@pytest.mark.unit
def test_unquote_git_config_value_strips_trailing_comment_after_quotes() -> None:
    """Quoted values with trailing #/; comments must unquote (Bugbot 5093013087)."""
    assert (
        git_manager_ownership._unquote_git_config_value('"../redirected" # note') == "../redirected"
    )
    assert git_manager_ownership._unquote_git_config_value('"/abs/path" ; note') == "/abs/path"
    assert git_manager_ownership._unquote_git_config_value('"foo\\"bar" # c') == 'foo"bar'
    assert git_manager_ownership._unquote_git_config_value('"a\\nb\\tc"') == "a\nb\tc"
    assert git_manager_ownership._unquote_git_config_value("../rel # note") == "../rel"
    assert git_manager_ownership._unquote_git_config_value('"../rel"') == "../rel"


@pytest.mark.unit
def test_rewrite_relative_core_worktree_for_snapshot_edge_cases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absolutize only relative core.worktree; leave absolute/~; fail closed on OSError."""
    git_dir = tmp_path / "repo" / ".git"
    git_dir.mkdir(parents=True)
    abs_target = (tmp_path / "abs-wt").resolve()

    absolute = f"[core]\n\tworktree = {abs_target}\n"
    assert (
        git_manager_ownership._rewrite_relative_core_worktree_for_snapshot(absolute, git_dir)
        == absolute
    )

    tilde = "[core]\n\tworktree = ~/somewhere\n"
    assert (
        git_manager_ownership._rewrite_relative_core_worktree_for_snapshot(tilde, git_dir) == tilde
    )

    relative = "[core]\n\tworktree = ../wt\n"
    rewritten = git_manager_ownership._rewrite_relative_core_worktree_for_snapshot(
        relative, git_dir
    )
    assert rewritten is not None
    assert str((git_dir / "../wt").resolve()) in rewritten
    assert "../wt" not in rewritten.split("worktree", 1)[1]

    # Quoted relative + trailing comment: absolutize without embedding quotes.
    quoted_rel = '[core]\n\tworktree = "../wt" # note\n'
    rewritten_quoted = git_manager_ownership._rewrite_relative_core_worktree_for_snapshot(
        quoted_rel, git_dir
    )
    assert rewritten_quoted is not None
    assert str((git_dir / "../wt").resolve()) in rewritten_quoted
    assert '"../wt"' not in rewritten_quoted

    # Quoted absolute + trailing comment: leave line verbatim (not relative).
    quoted_abs = f'[core]\n\tworktree = "{abs_target}" ; note\n'
    assert (
        git_manager_ownership._rewrite_relative_core_worktree_for_snapshot(quoted_abs, git_dir)
        == quoted_abs
    )

    def _boom(self: Path, *, strict: bool = False) -> Path:
        del self, strict
        raise OSError("simulated resolve failure")

    monkeypatch.setattr(Path, "resolve", _boom)
    assert (
        git_manager_ownership._rewrite_relative_core_worktree_for_snapshot(
            "[core]\n\tworktree = ../wt\n", git_dir
        )
        is None
    )


@pytest.mark.unit
def test_unquote_and_format_git_config_value_edge_cases() -> None:
    """Cover empty, unclosed, trailing-escape, and quote-needed config tokens."""
    assert git_manager_ownership._unquote_git_config_value("") == ""
    assert git_manager_ownership._unquote_git_config_value("   ") == ""
    # Unclosed quote: return accumulated body (Git keeps reading to EOF).
    assert git_manager_ownership._unquote_git_config_value('"unterminated') == "unterminated"
    # Trailing backslash at end of quoted value keeps the backslash.
    assert git_manager_ownership._unquote_git_config_value('"trailing\\\\') == "trailing\\"
    assert git_manager_ownership._unquote_git_config_value('"ends-with\\') == "ends-with\\"
    # Values with whitespace / comment / quote chars must be re-quoted for write-back.
    assert git_manager_ownership._format_git_config_value("plain") == "plain"
    assert git_manager_ownership._format_git_config_value("has space") == '"has space"'
    assert git_manager_ownership._format_git_config_value('a"b') == '"a\\"b"'
    assert git_manager_ownership._format_git_config_value("a#b") == '"a#b"'
    assert git_manager_ownership._format_git_config_value("a;b") == '"a;b"'
    assert git_manager_ownership._format_git_config_value("a\tb") == '"a\\tb"'
    # Newline alone does not force quoting; when quoting is required, newlines escape.
    assert git_manager_ownership._format_git_config_value("a\nb") == "a\nb"
    assert git_manager_ownership._format_git_config_value("a\nb c") == '"a\\nb c"'
    assert git_manager_ownership._format_git_config_value("a\\b") == '"a\\\\b"'


@pytest.mark.unit
def test_rewrite_relative_core_worktree_preserves_bom_and_newline_styles(
    tmp_path: Path,
) -> None:
    """BOM and CR/CRLF line endings must survive relative worktree absolutization."""
    git_dir = tmp_path / "repo" / ".git"
    git_dir.mkdir(parents=True)
    absolute = str((git_dir / "../wt").resolve())

    bom_lf = "\ufeff[core]\n\tworktree = ../wt\n"
    rewritten_bom = git_manager_ownership._rewrite_relative_core_worktree_for_snapshot(
        bom_lf, git_dir
    )
    assert rewritten_bom is not None
    assert rewritten_bom.startswith("\ufeff")
    assert absolute in rewritten_bom

    crlf = "[core]\r\n\tworktree = ../wt\r\n"
    rewritten_crlf = git_manager_ownership._rewrite_relative_core_worktree_for_snapshot(
        crlf, git_dir
    )
    assert rewritten_crlf is not None
    assert "\r\n" in rewritten_crlf
    assert absolute in rewritten_crlf

    cr_only = "[core]\r\tworktree = ../wt\r"
    rewritten_cr = git_manager_ownership._rewrite_relative_core_worktree_for_snapshot(
        cr_only, git_dir
    )
    assert rewritten_cr is not None
    assert "\r" in rewritten_cr
    assert absolute in rewritten_cr


@pytest.mark.unit
def test_read_git_dir_config_text_fstat_oserror_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First or post-read fstat OSError must fail closed rather than raise."""
    path = tmp_path / "config"
    path.write_text("[core]\n\tfilemode = true\n", encoding="utf-8")
    real_fstat = os.fstat
    calls = {"n": 0}

    def _fstat_first_fails(fd: int) -> os.stat_result:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("first fstat failed")
        return real_fstat(fd)

    monkeypatch.setattr(os, "fstat", _fstat_first_fails)
    assert git_manager_ownership._read_git_dir_config_text(path) is None

    calls["n"] = 0

    def _fstat_second_fails(fd: int) -> os.stat_result:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("post-read fstat failed")
        return real_fstat(fd)

    monkeypatch.setattr(os, "fstat", _fstat_second_fails)
    assert git_manager_ownership._read_git_dir_config_text(path) is None


@pytest.mark.unit
def test_nested_repository_git_dirs_include_scan_fail_closed_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Include-scan must fail closed on odd markers, resolve errors, and bad commondir."""
    nested = tmp_path / "nested"
    nested.mkdir()

    # Non-directory / non-regular `.git` marker → empty scan (not a git repo).
    fifo = nested / ".git"
    os.mkfifo(fifo)
    assert git_manager_ownership._nested_repository_git_dirs_for_include_scan(nested) == ()
    fifo.unlink()

    real_git = tmp_path / "real.git"
    real_git.mkdir()
    (real_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (nested / ".git").write_text(f"gitdir: {real_git}\n", encoding="utf-8")

    real_resolve = Path.resolve

    def _boom(self: Path, *, strict: bool = False) -> Path:
        del self, strict
        raise OSError("resolve failed")

    monkeypatch.setattr(Path, "resolve", _boom)
    assert git_manager_ownership._nested_repository_git_dirs_for_include_scan(nested) == ()
    monkeypatch.setattr(Path, "resolve", real_resolve)

    # Directory commondir is non-regular → keep primary only.
    nested2 = tmp_path / "nested2"
    nested2.mkdir()
    git_dir2 = nested2 / ".git"
    git_dir2.mkdir()
    (git_dir2 / "commondir").mkdir()
    dirs = git_manager_ownership._nested_repository_git_dirs_for_include_scan(nested2)
    assert dirs == (git_dir2.resolve(),)

    # Unreadable/oversized commondir snapshot → fail closed (None).
    nested3 = tmp_path / "nested3"
    nested3.mkdir()
    git_dir3 = nested3 / ".git"
    git_dir3.mkdir()
    (git_dir3 / "commondir").write_text("../common\n", encoding="utf-8")
    monkeypatch.setattr(
        git_manager_ownership,
        "_read_git_dir_config_text",
        lambda _path: None,
    )
    assert git_manager_ownership._nested_repository_git_dirs_for_include_scan(nested3) is None


@pytest.mark.unit
def test_snapshot_git_dir_local_configs_fail_closed_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local config snapshot must reject symlinks, non-files, and unsafe reads."""
    git_dir = tmp_path / "git"
    git_dir.mkdir()
    target = tmp_path / "elsewhere"
    target.write_text("[core]\n", encoding="utf-8")
    (git_dir / "config").symlink_to(target)
    assert git_manager_ownership._snapshot_git_dir_local_configs(git_dir) is None

    git_dir2 = tmp_path / "git2"
    git_dir2.mkdir()
    (git_dir2 / "config").mkdir()
    assert git_manager_ownership._snapshot_git_dir_local_configs(git_dir2) == {}

    git_dir3 = tmp_path / "git3"
    git_dir3.mkdir()
    (git_dir3 / "config").write_text("[core]\n\tfilemode = true\n", encoding="utf-8")
    monkeypatch.setattr(
        git_manager_ownership,
        "_read_git_dir_config_text",
        lambda _path: None,
    )
    assert git_manager_ownership._snapshot_git_dir_local_configs(git_dir3) is None


@pytest.mark.unit
def test_untrusted_nested_probe_config_snapshot_empty_git_dirs_yields_none(
    tmp_path: Path,
) -> None:
    """Missing nested `.git` must yield ``None`` rather than invent a staging dir."""
    nested = tmp_path / "nested"
    nested.mkdir()
    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is None


@pytest.mark.unit
def test_untrusted_nested_probe_config_snapshot_writes_config_worktree(
    tmp_path: Path,
) -> None:
    """Present ``config.worktree`` without includes must be copied into the snapshot."""
    nested = tmp_path / "nested"
    nested.mkdir()
    subprocess.run(["git", "init"], cwd=nested, check=True, capture_output=True)
    git_dir = nested / ".git"
    (git_dir / "config.worktree").write_text(
        "[core]\n\tfilemode = true\n",
        encoding="utf-8",
    )
    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is not None
        assert (shadow / "config.worktree").is_file()
        assert "filemode = true" in (shadow / "config.worktree").read_text(encoding="utf-8")


@pytest.mark.unit
def test_untrusted_nested_probe_config_snapshot_fails_when_rewrite_returns_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed when relative ``core.worktree`` rewrite cannot materialize."""
    nested = tmp_path / "nested"
    nested.mkdir()
    subprocess.run(["git", "init"], cwd=nested, check=True, capture_output=True)
    git_dir = nested / ".git"
    (git_dir / "config.worktree").write_text(
        "[core]\n\tworktree = ../wt\n",
        encoding="utf-8",
    )

    calls = {"n": 0}
    real_rewrite = git_manager_ownership._rewrite_relative_core_worktree_for_snapshot

    def _rewrite(text: str, original_git_dir: Path) -> str | None:
        calls["n"] += 1
        # First call rewrites main config; second is config.worktree — fail that one.
        if calls["n"] >= 2:
            return None
        return real_rewrite(text, original_git_dir)

    monkeypatch.setattr(
        git_manager_ownership,
        "_rewrite_relative_core_worktree_for_snapshot",
        _rewrite,
    )
    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is None

    # Also fail closed when the primary config rewrite itself returns None.
    monkeypatch.setattr(
        git_manager_ownership,
        "_rewrite_relative_core_worktree_for_snapshot",
        lambda _text, _git_dir: None,
    )
    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is None
