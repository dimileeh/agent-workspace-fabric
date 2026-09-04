"""Ownership nested-probe pin/rename and fail-closed snapshot edge regressions (part 4)."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

import awf.node.git_manager as git_manager
import awf.node.git_manager_ownership as git_manager_ownership


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

        def _stat_permission_error(
            path: str | bytes | os.PathLike[str], *args: object, **kwargs: object
        ) -> os.stat_result:
            if path == "config":
                raise PermissionError(13, "Permission denied", "config")
            return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "stat", _stat_permission_error)
        # Non-ENOENT stat failure must fail closed (PRRT_kwDOSJAM6s6evrZl).
        assert git_manager_ownership._snapshot_git_dir_local_configs_via_fd(primary_fd) is None
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


@pytest.mark.unit
def test_snapshot_git_dir_info_exclude_fail_closed_edges(tmp_path: Path) -> None:
    """PRRT_kwDOSJAM6s6fMMqG: info/exclude snapshot rejects symlink/non-regular entries."""
    git_dir = tmp_path / "git_symlink_info"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n\trepositoryformatversion = 0\n", encoding="utf-8")
    target = tmp_path / "foreign_info"
    target.mkdir()
    (git_dir / "info").symlink_to(target)
    assert git_manager_ownership._snapshot_git_dir_local_configs(git_dir) is None

    git_dir2 = tmp_path / "git_symlink_exclude"
    git_dir2.mkdir()
    (git_dir2 / "config").write_text("[core]\n\trepositoryformatversion = 0\n", encoding="utf-8")
    (git_dir2 / "info").mkdir()
    foreign = tmp_path / "foreign_exclude"
    foreign.write_text("secret\n", encoding="utf-8")
    (git_dir2 / "info" / "exclude").symlink_to(foreign)
    assert git_manager_ownership._snapshot_git_dir_local_configs(git_dir2) is None

    git_dir3 = tmp_path / "git_fifo_exclude"
    git_dir3.mkdir()
    (git_dir3 / "config").write_text("[core]\n\trepositoryformatversion = 0\n", encoding="utf-8")
    (git_dir3 / "info").mkdir()
    os.mkfifo(git_dir3 / "info" / "exclude", mode=0o644)
    assert git_manager_ownership._snapshot_git_dir_local_configs(git_dir3) is None

    git_dir_file_info = tmp_path / "git_file_info"
    git_dir_file_info.mkdir()
    (git_dir_file_info / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n", encoding="utf-8"
    )
    (git_dir_file_info / "info").write_text("not-a-dir\n", encoding="utf-8")
    assert git_manager_ownership._snapshot_git_dir_local_configs(git_dir_file_info) is None

    git_dir4 = tmp_path / "git_exclude_ok"
    git_dir4.mkdir()
    (git_dir4 / "config").write_text("[core]\n\trepositoryformatversion = 0\n", encoding="utf-8")
    (git_dir4 / "info").mkdir()
    (git_dir4 / "info" / "exclude").write_text("# keep\n", encoding="utf-8")
    snap = git_manager_ownership._snapshot_git_dir_local_configs(git_dir4)
    assert snap is not None
    assert snap.get(git_manager_ownership._INFO_EXCLUDE_NAME) == "# keep\n"
    assert "config" in snap
