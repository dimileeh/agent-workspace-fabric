"""Ownership-edge tests: include scans, config reads, and snapshot rewrite edges."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

import awf.node.git_manager as git_manager
import awf.node.git_manager_ownership as git_manager_ownership


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
def test_untrusted_nested_git_dir_inaccessible_config_lstat_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-ENOENT config lstat must fail closed, not treat as absent.

    Mirrors PRRT_kwDOSJAM6s6evrZl for the pathname include-scan path used by
    ``untrusted_nested_repository_local_config_has_includes``.
    """
    git_dir = tmp_path / "git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n\tfilemode = true\n", encoding="utf-8")
    real_lstat = Path.lstat

    def _lstat_permission_error(self: Path) -> os.stat_result:
        if self.name == "config":
            raise PermissionError(13, "Permission denied", str(self))
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", _lstat_permission_error)
    assert git_manager.untrusted_nested_git_dir_declares_local_includes(git_dir) is True

    nested = tmp_path / "nested"
    nested.mkdir()
    nested_git = nested / ".git"
    nested_git.mkdir()
    (nested_git / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    assert git_manager.untrusted_nested_repository_local_config_has_includes(nested) is True


@pytest.mark.unit
def test_snapshot_git_dir_local_configs_inaccessible_lstat_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pathname snapshot must fail closed on non-ENOENT config lstat (PRRT_kwDOSJAM6s6evrZl)."""
    git_dir = tmp_path / "git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n\tfilemode = true\n", encoding="utf-8")
    real_lstat = Path.lstat

    def _lstat_permission_error(self: Path) -> os.stat_result:
        if self.name == "config":
            raise PermissionError(13, "Permission denied", str(self))
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", _lstat_permission_error)
    assert git_manager_ownership._snapshot_git_dir_local_configs(git_dir) is None


@pytest.mark.unit
def test_untrusted_nested_repository_include_scan_gitfile_and_commondir(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    real_git = nested / "real.git"
    real_git.mkdir()
    common = nested / "common.git"
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
def test_untrusted_nested_escaped_gitfile_fails_closed_without_reading_foreign_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absolute gitfile targets outside the nest must not be snapshotted or scanned."""
    nested = tmp_path / "nested"
    nested.mkdir()
    foreign = tmp_path / "foreign.git"
    foreign.mkdir()
    (foreign / "config").write_text("[core]\n\tbare = true\n", encoding="utf-8")
    (nested / ".git").write_text(f"gitdir: {foreign}\n", encoding="utf-8")

    reads: list[Path] = []
    real_read = git_manager_ownership._read_git_dir_config_text

    def _track(path: Path) -> str | None:
        reads.append(Path(path))
        return real_read(path)

    monkeypatch.setattr(git_manager_ownership, "_read_git_dir_config_text", _track)
    assert git_manager.untrusted_nested_repository_local_config_has_includes(nested) is True
    assert not any(path.resolve() == (foreign / "config").resolve() for path in reads)
    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is None


@pytest.mark.unit
def test_untrusted_nested_relative_escaping_gitfile_fails_closed(tmp_path: Path) -> None:
    """Relative gitfile targets that escape the nest must fail closed."""
    nested = tmp_path / "nested"
    nested.mkdir()
    foreign = tmp_path / "foreign.git"
    foreign.mkdir()
    (foreign / "config").write_text("[core]\n\tbare = true\n", encoding="utf-8")
    (nested / ".git").write_text("gitdir: ../foreign.git\n", encoding="utf-8")
    assert git_manager.untrusted_nested_repository_local_config_has_includes(nested) is True
    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is None


@pytest.mark.unit
def test_untrusted_nested_escaped_commondir_fails_closed(tmp_path: Path) -> None:
    """Commondir targets outside the nest must fail closed even without includes."""
    nested = tmp_path / "nested"
    nested.mkdir()
    git_dir = nested / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    foreign_common = tmp_path / "foreign-common.git"
    foreign_common.mkdir()
    (foreign_common / "config").write_text("[core]\n\tbare = true\n", encoding="utf-8")
    (git_dir / "commondir").write_text(f"{foreign_common}\n", encoding="utf-8")
    assert git_manager.untrusted_nested_repository_local_config_has_includes(nested) is True
    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is None


@pytest.mark.unit
def test_untrusted_nested_gitfile_allowed_under_explicit_containment_roots(
    tmp_path: Path,
) -> None:
    """Residue probes may admit gitfile metadata under the outer worktree root."""
    worktree = tmp_path / "ws"
    nested = worktree / "vendor"
    nested.mkdir(parents=True)
    real_git = worktree / "vendor-git"
    subprocess.run(["git", "init", "--bare", str(real_git)], check=True, capture_output=True)
    (nested / ".git").write_text(f"gitdir: {real_git}\n", encoding="utf-8")
    assert git_manager.untrusted_nested_repository_local_config_has_includes(nested) is True
    assert (
        git_manager.untrusted_nested_repository_local_config_has_includes(
            nested,
            containment_roots=(worktree,),
        )
        is False
    )
    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(
        nested,
        containment_roots=(worktree,),
    ) as shadow:
        assert shadow is not None


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
    real_git = nested / "real.git"
    real_git.mkdir()
    target = nested / "common-target"
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
    real_git = nested / "real.git"
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
def test_untrusted_nested_probe_config_snapshot_rejects_symlink_packed_refs(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eqQgm: packed-refs symlink must not chain into foreign stores."""
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
    local_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=nested,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    foreign = tmp_path / "foreign"
    foreign.mkdir()
    subprocess.run(["git", "init"], cwd=foreign, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "evil@example.com"],
        cwd=foreign,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Evil"],
        cwd=foreign,
        check=True,
        capture_output=True,
    )
    (foreign / "evil.txt").write_text("evil\n", encoding="utf-8")
    subprocess.run(["git", "add", "evil.txt"], cwd=foreign, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "evil"], cwd=foreign, check=True, capture_output=True)
    foreign_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=foreign,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert foreign_head != local_head
    # Pack refs in the foreign repo so packed-refs names the foreign HEAD.
    subprocess.run(["git", "pack-refs", "--all"], cwd=foreign, check=True, capture_output=True)
    foreign_packed = (foreign / ".git" / "packed-refs").read_text(encoding="utf-8")
    assert foreign_head in foreign_packed

    packed = nested / ".git" / "packed-refs"
    if packed.exists() or packed.is_symlink():
        packed.unlink()
    packed.symlink_to(foreign / ".git" / "packed-refs")
    # Drop loose HEAD ref so a chained packed-refs would supply the tip.
    loose_head = nested / ".git" / "refs" / "heads" / "master"
    if not loose_head.exists():
        loose_head = nested / ".git" / "refs" / "heads" / "main"
    if loose_head.exists():
        loose_head.unlink()

    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is None


@pytest.mark.unit
def test_untrusted_nested_probe_config_snapshot_rejects_symlink_refs(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eqQgm: refs directory symlink must not chain into foreign stores."""
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

    foreign = tmp_path / "foreign"
    foreign.mkdir()
    subprocess.run(["git", "init"], cwd=foreign, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "evil@example.com"],
        cwd=foreign,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Evil"],
        cwd=foreign,
        check=True,
        capture_output=True,
    )
    (foreign / "evil.txt").write_text("evil\n", encoding="utf-8")
    subprocess.run(["git", "add", "evil.txt"], cwd=foreign, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "evil"], cwd=foreign, check=True, capture_output=True)

    refs = nested / ".git" / "refs"
    shutil.rmtree(refs)
    refs.symlink_to(foreign / ".git" / "refs")

    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is None


@pytest.mark.unit
def test_untrusted_nested_probe_snapshot_rejects_nested_loose_ref_symlink(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ercEL: descendant loose-ref symlink must fail closed.

    An ordinary ``.git/refs`` directory with ``refs/heads/main`` pointing at a
    foreign workspace ref still lets ``git rev-parse HEAD`` follow the symlink;
    the probe snapshot must reject that tree rather than staging the live refs
    subtree.
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
    (nested / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=nested, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c"], cwd=nested, check=True, capture_output=True)
    local_oid = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=nested,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    foreign = tmp_path / "foreign"
    foreign.mkdir()
    subprocess.run(["git", "init"], cwd=foreign, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "evil@example.com"],
        cwd=foreign,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Evil"],
        cwd=foreign,
        check=True,
        capture_output=True,
    )
    (foreign / "evil.txt").write_text("evil\n", encoding="utf-8")
    subprocess.run(["git", "add", "evil.txt"], cwd=foreign, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "evil"], cwd=foreign, check=True, capture_output=True)
    foreign_oid = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=foreign,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert foreign_oid != local_oid

    loose_head = nested / ".git" / "refs" / "heads" / "main"
    if not loose_head.exists():
        # Some Git versions default to master; resolve the current branch tip path.
        branch = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=nested,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        loose_head = nested / ".git" / "refs" / "heads" / branch
    foreign_loose = foreign / ".git" / "refs" / "heads"
    foreign_branch = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=foreign,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    foreign_ref = foreign_loose / foreign_branch
    assert foreign_ref.is_file()
    assert loose_head.is_file()
    loose_head.unlink()
    loose_head.symlink_to(foreign_ref)

    # Git follows the nested loose-ref symlink for live HEAD resolution.
    live = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=nested,
        check=True,
        capture_output=True,
        text=True,
    )
    assert live.stdout.strip() == foreign_oid

    with git_manager.untrusted_nested_probe_config_snapshot_git_dir(nested) as shadow:
        assert shadow is None


@pytest.mark.unit
def test_untrusted_nested_probe_config_snapshot_rejects_symlink_index(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eqQgm: index symlink must not chain into a foreign workspace."""
    nested = tmp_path / "nested"
    nested.mkdir()
    subprocess.run(["git", "init"], cwd=nested, check=True, capture_output=True)
    foreign_index = tmp_path / "foreign-workspace-index"
    foreign_index.write_bytes(b"DIRC" + b"\x00" * 28)
    index = nested / ".git" / "index"
    if index.exists() or index.is_symlink():
        index.unlink()
    index.symlink_to(foreign_index)

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

    # Same-line [core] worktree = … (PRRT_kwDOSJAM6s6etk6T): Git accepts this
    # form and resolves relative to --git-dir, so the snapshot must absolutize.
    same_line = "[core] worktree = ../wt\n"
    rewritten_same = git_manager_ownership._rewrite_relative_core_worktree_for_snapshot(
        same_line, git_dir
    )
    assert rewritten_same is not None
    assert str((git_dir / "../wt").resolve()) in rewritten_same
    assert "../wt" not in rewritten_same.split("worktree", 1)[1]
    assert rewritten_same.startswith("[core]")

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


@pytest.mark.unit
def test_resolved_git_metadata_within_roots_skips_unresolvable_and_escaping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    target = nested / "git"
    target.mkdir()
    outside = tmp_path / "outside.git"
    outside.mkdir()
    bad_root = tmp_path / "missing-root"
    real_resolve = Path.resolve

    def _resolve(self: Path, *, strict: bool = False) -> Path:
        del strict
        if self == bad_root:
            raise OSError("root unreadable")
        return real_resolve(self)

    monkeypatch.setattr(Path, "resolve", _resolve)
    assert (
        git_manager_ownership._resolved_git_metadata_within_roots(target, (bad_root, nested))
        == target.resolve()
    )
    monkeypatch.setattr(Path, "resolve", real_resolve)
    assert git_manager_ownership._resolved_git_metadata_within_roots(outside, (nested,)) is None
    assert git_manager_ownership._nested_git_metadata_containment_roots(nested, (nested,)) == (
        nested,
    )

    def _boom(self: Path, *, strict: bool = False) -> Path:
        del self, strict
        raise OSError("nested unreadable")

    monkeypatch.setattr(Path, "resolve", _boom)
    assert git_manager_ownership._nested_git_metadata_containment_roots(nested, None) is None
    assert git_manager_ownership._resolved_git_metadata_within_roots(target, (nested,)) is None


@pytest.mark.unit
def test_resolved_git_metadata_within_roots_skips_relative_to_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    target = nested / "git"
    target.mkdir()

    def _boom(self: Path, other: Path) -> bool:
        del self, other
        raise OSError("relative-to failed")

    monkeypatch.setattr(Path, "is_relative_to", _boom)
    assert git_manager_ownership._resolved_git_metadata_within_roots(target, (nested,)) is None
