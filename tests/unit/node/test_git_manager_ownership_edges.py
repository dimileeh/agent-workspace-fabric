"""Focused ownership and stale-worktree GitManager edge tests."""

from __future__ import annotations

import os
import stat
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
