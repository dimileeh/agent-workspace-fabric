"""Focused ownership and stale-worktree GitManager edge tests."""

from __future__ import annotations

import contextlib
import os
import stat
import subprocess
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
def test_untrusted_nested_git_config_args_override_diff_order_file_fifo(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6esEnZ: nested staged probes must ignore agent-set diff.orderFile."""
    assert f"diff.orderFile={os.devnull}" in git_manager.UNTRUSTED_NESTED_GIT_CONFIG_ARGS

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
    (nested / "tracked.txt").write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=nested, check=True, capture_output=True)

    order_fifo = tmp_path / "order.fifo"
    os.mkfifo(order_fifo)
    subprocess.run(
        ["git", "config", "diff.orderFile", str(order_fifo)],
        cwd=nested,
        check=True,
        capture_output=True,
    )

    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=nested,
            capture_output=True,
            timeout=1,
        )

    sanitized = subprocess.run(
        [
            "git",
            *git_manager.UNTRUSTED_NESTED_GIT_CONFIG_ARGS,
            "diff",
            "--cached",
            "--name-only",
        ],
        cwd=nested,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert "tracked.txt" in sanitized.stdout.splitlines()


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
def test_symlink_nested_probe_objects_store_via_fd_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Objects snapshot materializes dirs, skips info, and fails closed on bad stores."""
    git_dir = tmp_path / "repo.git"
    git_dir.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    fd = git_manager_ownership._open_git_dir_directory_fd(git_dir)
    assert fd is not None
    try:
        ok, held = git_manager_ownership._symlink_nested_probe_objects_store_via_fd(fd, staging)
        assert ok is True
        assert held == []
        assert not (staging / "objects").exists()
    finally:
        os.close(fd)

    objects = git_dir / "objects"
    objects.mkdir()
    (objects / "pack").mkdir()
    (objects / "pack" / "pack-deadbeef.pack").write_bytes(b"PACK")
    (objects / "ab").mkdir()
    (objects / "ab" / "cdef").write_bytes(b"obj")
    info = objects / "info"
    info.mkdir()
    (info / "alternates").write_text("/tmp/foreign\n", encoding="utf-8")
    staging2 = tmp_path / "staging2"
    staging2.mkdir()
    fd = git_manager_ownership._open_git_dir_directory_fd(git_dir)
    assert fd is not None
    try:
        ok, held = git_manager_ownership._symlink_nested_probe_objects_store_via_fd(fd, staging2)
        assert ok is True
        assert held
        try:
            assert (staging2 / "objects").is_dir()
            assert not (staging2 / "objects").is_symlink()
            assert (staging2 / "objects" / "pack").is_dir()
            assert not (staging2 / "objects" / "pack").is_symlink()
            assert (staging2 / "objects" / "pack" / "pack-deadbeef.pack").is_symlink()
            assert (staging2 / "objects" / "ab").is_dir()
            assert not (staging2 / "objects" / "ab").is_symlink()
            assert (staging2 / "objects" / "ab" / "cdef").is_symlink()
            assert not (staging2 / "objects" / "info").exists()
        finally:
            for held_fd in held:
                os.close(held_fd)
    finally:
        os.close(fd)

    poisoned = tmp_path / "poisoned.git"
    poisoned.mkdir()
    (poisoned / "objects").symlink_to(objects, target_is_directory=True)
    staging3 = tmp_path / "staging3"
    staging3.mkdir()
    fd = git_manager_ownership._open_git_dir_directory_fd(poisoned)
    assert fd is not None
    try:
        ok, held = git_manager_ownership._symlink_nested_probe_objects_store_via_fd(fd, staging3)
        assert ok is False
        assert held == []
    finally:
        os.close(fd)

    clean = tmp_path / "clean.git"
    clean.mkdir()
    (clean / "objects").mkdir()
    staging4 = tmp_path / "staging4"
    staging4.mkdir()
    fd = git_manager_ownership._open_git_dir_directory_fd(clean)
    assert fd is not None
    real_stat = os.stat
    real_scandir = os.scandir
    try:

        def _stat_objects_boom(
            path: str | bytes | os.PathLike[str], *args: object, **kwargs: object
        ) -> os.stat_result:
            if path == "objects":
                raise OSError("stat failed")
            return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "stat", _stat_objects_boom)
        ok, held = git_manager_ownership._symlink_nested_probe_objects_store_via_fd(fd, staging4)
        assert ok is False and held == []
        monkeypatch.setattr(os, "stat", real_stat)

        def _scandir_boom(path: str | bytes | os.PathLike[str]) -> object:
            raise OSError("scandir failed")

        monkeypatch.setattr(os, "scandir", _scandir_boom)
        ok, held = git_manager_ownership._symlink_nested_probe_objects_store_via_fd(fd, staging4)
        assert ok is False and held == []
        monkeypatch.setattr(os, "scandir", real_scandir)

        child = clean / "objects" / "badlink"
        child.symlink_to("/tmp/elsewhere")
        ok, held = git_manager_ownership._symlink_nested_probe_objects_store_via_fd(fd, staging4)
        assert ok is False and held == []
        child.unlink()

        staging_file = tmp_path / "staging_file_as_dir"
        staging_file.write_text("not-a-dir\n", encoding="utf-8")
        ok, held = git_manager_ownership._symlink_nested_probe_objects_store_via_fd(
            fd, staging_file
        )
        assert ok is False and held == []
    finally:
        monkeypatch.setattr(os, "stat", real_stat)
        monkeypatch.setattr(os, "scandir", real_scandir)
        os.close(fd)


@pytest.mark.unit
def test_symlink_nested_probe_objects_store_rejects_nested_loose_object_symlink(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eq1r3: fan-out dirs must not hide nested loose-object symlinks.

    An ordinary ``objects/ab`` directory that contains a symlinked loose object
    must fail closed. Linking the whole fan-out would expose the live subtree and
    let Git follow the foreign object for fingerprint probes.
    """
    git_dir = tmp_path / "repo.git"
    git_dir.mkdir()
    objects = git_dir / "objects"
    fanout = objects / "ab"
    fanout.mkdir(parents=True)
    foreign = tmp_path / "foreign-object"
    foreign.write_bytes(b"foreign")
    (fanout / "cdef0123456789").symlink_to(foreign)
    staging = tmp_path / "staging"
    staging.mkdir()
    fd = git_manager_ownership._open_git_dir_directory_fd(git_dir)
    assert fd is not None
    try:
        ok, held = git_manager_ownership._symlink_nested_probe_objects_store_via_fd(fd, staging)
        assert ok is False
        assert held == []
    finally:
        os.close(fd)


@pytest.mark.unit
def test_symlink_nested_probe_refs_store_rejects_nested_loose_ref_symlink(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ercEL: ordinary refs dirs must not hide nested loose-ref symlinks.

    An ordinary ``refs/heads`` directory that contains a symlinked loose ref must
    fail closed. Linking the whole ``refs`` tree would expose the live subtree and
    let Git follow the foreign ref for nested HEAD attribution.
    """
    git_dir = tmp_path / "repo.git"
    git_dir.mkdir()
    heads = git_dir / "refs" / "heads"
    heads.mkdir(parents=True)
    foreign = tmp_path / "foreign-ref"
    foreign.write_text("0123456789abcdef0123456789abcdef01234567\n", encoding="utf-8")
    (heads / "main").symlink_to(foreign)
    staging = tmp_path / "staging"
    staging.mkdir()
    fd = git_manager_ownership._open_git_dir_directory_fd(git_dir)
    assert fd is not None
    try:
        ok, held = git_manager_ownership._symlink_nested_probe_refs_store_via_fd(fd, staging)
        assert ok is False
        assert held == []
    finally:
        os.close(fd)


@pytest.mark.unit
def test_symlink_nested_probe_refs_store_materializes_regular_loose_refs(
    tmp_path: Path,
) -> None:
    """Safe loose refs are staged as directory trees with leaf file links only."""
    git_dir = tmp_path / "repo.git"
    git_dir.mkdir()
    heads = git_dir / "refs" / "heads"
    heads.mkdir(parents=True)
    (heads / "main").write_text("0123456789abcdef0123456789abcdef01234567\n", encoding="utf-8")
    staging = tmp_path / "staging"
    staging.mkdir()
    fd = git_manager_ownership._open_git_dir_directory_fd(git_dir)
    assert fd is not None
    try:
        ok, held = git_manager_ownership._symlink_nested_probe_refs_store_via_fd(fd, staging)
        assert ok is True
        assert held
        assert not (staging / "refs").is_symlink()
        assert not (staging / "refs" / "heads").is_symlink()
        assert (staging / "refs" / "heads" / "main").is_symlink()
        assert (staging / "refs" / "heads" / "main").read_text(encoding="utf-8").startswith("0123")
    finally:
        for held_fd in held:
            os.close(held_fd)
        os.close(fd)


@pytest.mark.unit
def test_symlink_object_store_tree_via_fd_fail_closed_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Materialize helper rejects FIFOs, vanished names, and open/mkdir failures."""
    root = tmp_path / "objects"
    root.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    held: list[int] = []

    fifo = root / "fifo"
    os.mkfifo(fifo)
    fd = git_manager_ownership._open_git_dir_directory_fd(root)
    assert fd is not None
    try:
        assert git_manager_ownership._symlink_object_store_tree_via_fd(fd, staging, held) is False
    finally:
        for held_fd in held:
            os.close(held_fd)
        held.clear()
        os.close(fd)
    fifo.unlink()

    (root / "ab").mkdir()
    (root / "ab" / "obj").write_bytes(b"x")
    # Race: name disappears between scandir and lstat.
    real_scandir = os.scandir
    real_stat = os.stat
    real_mkdir = Path.mkdir
    real_open_child = git_manager_ownership._open_git_dir_child_directory_fd
    real_symlink = git_manager_ownership._symlink_git_dir_child_via_fd
    fd = git_manager_ownership._open_git_dir_directory_fd(root)
    assert fd is not None
    try:

        class _GhostEntry:
            name = "ghost"

        @contextlib.contextmanager
        def _scandir_ghost(_path: str | bytes | os.PathLike[str]) -> object:
            yield [_GhostEntry()]

        monkeypatch.setattr(os, "scandir", _scandir_ghost)
        assert git_manager_ownership._symlink_object_store_tree_via_fd(fd, staging, held) is True
        monkeypatch.setattr(os, "scandir", real_scandir)

        def _stat_boom(
            path: str | bytes | os.PathLike[str], *args: object, **kwargs: object
        ) -> os.stat_result:
            if path == "ab":
                raise OSError("stat failed")
            return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "stat", _stat_boom)
        assert git_manager_ownership._symlink_object_store_tree_via_fd(fd, staging, held) is False
        monkeypatch.setattr(os, "stat", real_stat)

        def _open_child_fail(dir_fd: int, name: str) -> int | None:
            if name == "ab":
                return None
            return real_open_child(dir_fd, name)

        monkeypatch.setattr(
            git_manager_ownership, "_open_git_dir_child_directory_fd", _open_child_fail
        )
        assert git_manager_ownership._symlink_object_store_tree_via_fd(fd, staging, held) is False
        monkeypatch.setattr(
            git_manager_ownership, "_open_git_dir_child_directory_fd", real_open_child
        )

        def _mkdir_boom(self: Path, *args: object, **kwargs: object) -> None:
            if self.name == "ab":
                raise OSError("mkdir failed")
            return real_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", _mkdir_boom)
        assert git_manager_ownership._symlink_object_store_tree_via_fd(fd, staging, held) is False
        monkeypatch.setattr(Path, "mkdir", real_mkdir)

        def _symlink_fail(
            dir_fd: int,
            name: str,
            dest: Path,
            held_fds: list[int],
            *,
            expect_directory: bool | None = None,
        ) -> bool:
            if name == "obj":
                return False
            return real_symlink(dir_fd, name, dest, held_fds, expect_directory=expect_directory)

        monkeypatch.setattr(git_manager_ownership, "_symlink_git_dir_child_via_fd", _symlink_fail)
        assert git_manager_ownership._symlink_object_store_tree_via_fd(fd, staging, held) is False
        monkeypatch.setattr(git_manager_ownership, "_symlink_git_dir_child_via_fd", real_symlink)
    finally:
        for held_fd in held:
            with contextlib.suppress(OSError):
                os.close(held_fd)
        monkeypatch.setattr(os, "scandir", real_scandir)
        monkeypatch.setattr(os, "stat", real_stat)
        monkeypatch.setattr(Path, "mkdir", real_mkdir)
        monkeypatch.setattr(
            git_manager_ownership, "_open_git_dir_child_directory_fd", real_open_child
        )
        monkeypatch.setattr(git_manager_ownership, "_symlink_git_dir_child_via_fd", real_symlink)
        os.close(fd)


@pytest.mark.unit
def test_untrusted_nested_probe_snapshot_rejects_nested_loose_object_symlink(
    tmp_path: Path,
) -> None:
    """End-to-end: nested loose-object symlink must fail closed for probe snapshots."""
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
    foreign_obj = foreign_obj_dir / rest
    foreign_obj.write_bytes(local_obj.read_bytes())
    local_obj.unlink()
    local_obj.symlink_to(foreign_obj)

    # Git follows the loose-object symlink for live lookups.
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

    An existing ``alternates`` file at snapshot time often means objects already
    live only in a foreign store; fail closed before probes. The snapshot also
    omits ``objects/info`` so late-created alternates cannot reach probes
    (see ``test_untrusted_nested_probe_snapshot_ignores_late_object_alternates``).
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
def test_untrusted_nested_probe_snapshot_ignores_late_object_alternates(
    tmp_path: Path,
) -> None:
    """Bugbot 5094509768: late objects/info/alternates must not reach snapshot probes.

    The pre-check runs once; symlinking the live ``objects`` tree would still
    honor an ``alternates`` file created afterward. Snapshot ``objects`` must
    omit ``info`` so foreign object churn cannot flip fingerprint readability.
    Leaf object links pin validated inodes (PRRT_kwDOSJAM6s6ercEO), so unlinking
    the live path and planting late alternates must leave the snapshot readable
    from the held fd — not from the foreign alternate store.
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
    foreign_obj = foreign_obj_dir / rest
    foreign_obj.write_bytes(local_obj.read_bytes())

    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is not None
        assert (shadow / "objects").is_dir()
        assert not (shadow / "objects").is_symlink()
        assert not (shadow / "objects" / "info").exists()

        local_obj.unlink()
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

        # Break the late alternate store. Live resolution depends on it; the
        # snapshot must keep resolving via the pinned leaf inode instead.
        foreign_obj.unlink()
        live_after = subprocess.run(
            ["git", "rev-parse", "HEAD^{commit}"],
            cwd=nested,
            capture_output=True,
            text=True,
        )
        assert live_after.returncode != 0

        probe = subprocess.run(
            [
                "git",
                "--git-dir",
                str(shadow),
                "--work-tree",
                str(nested),
                *git_manager.UNTRUSTED_NESTED_GIT_CONFIG_ARGS,
                "rev-parse",
                "HEAD^{commit}",
            ],
            capture_output=True,
            text=True,
        )
        assert probe.returncode == 0
        assert probe.stdout.strip() == oid
        assert not (shadow / "objects" / "info").exists()
