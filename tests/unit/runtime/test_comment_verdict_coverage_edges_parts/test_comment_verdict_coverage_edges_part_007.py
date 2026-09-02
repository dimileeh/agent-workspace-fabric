"""Coverage-edge regressions for residue fingerprint fail-closed / classification paths.

Closes combined line+branch gaps that left python-full-coverage at ~98.79% after the
fingerprint API split. Prefer direct helper assertions over protocol-level churn.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import stat
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult
from awf.node.git_manager import git_env_without_object_lookup_overrides
from awf.runtime.pr_monitor_runner import (
    comment_verdict_residue,
    comment_verdict_residue_fingerprint,
    comment_verdict_residue_io,
    comment_verdict_residue_nested,
)
from tests.unit.runtime.test_comment_verdict_coverage_edges_parts._helpers import (
    init_git_worktree,
    init_git_worktree_file_replaced_by_directory,
)

_git_env = git_env_without_object_lookup_overrides


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_nested_git_probe_git_dir_fail_closed_matrix(tmp_path: Path) -> None:
    """Nested gitfile discovery must fail closed for missing / non-regular / bad payloads."""
    nested = tmp_path / "nested"
    nested.mkdir()

    assert comment_verdict_residue_nested._nested_git_probe_git_dir(nested) is None

    (nested / ".git").mkdir()
    assert comment_verdict_residue_nested._nested_git_probe_git_dir(nested) is None
    (nested / ".git").rmdir()

    fifo = nested / ".git"
    os.mkfifo(fifo, mode=0o644)
    assert comment_verdict_residue_nested._nested_git_probe_git_dir(nested) is None
    fifo.unlink()

    gitfile = nested / ".git"
    gitfile.write_text("not-a-gitdir\n", encoding="utf-8")
    assert comment_verdict_residue_nested._nested_git_probe_git_dir(nested) is None

    # Empty gitdir body resolves to the nested root (Path("") → nested_root).
    gitfile.write_text("gitdir:\n", encoding="utf-8")
    assert comment_verdict_residue_nested._nested_git_probe_git_dir(nested) == nested.resolve()

    target = tmp_path / "modules" / "x"
    target.mkdir(parents=True)
    gitfile.write_text(f"gitdir: {target}\n", encoding="utf-8")
    assert comment_verdict_residue_nested._nested_git_probe_git_dir(nested) == target.resolve()

    relative_target = nested / "rel-git"
    relative_target.mkdir()
    gitfile.write_text("gitdir: rel-git\n", encoding="utf-8")
    assert (
        comment_verdict_residue_nested._nested_git_probe_git_dir(nested)
        == relative_target.resolve()
    )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_nested_git_probe_git_dir_unreadable_and_resolve_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreadable gitfile text and resolve OSError must fail closed."""
    nested = tmp_path / "nested_unreadable"
    nested.mkdir()
    gitfile = nested / ".git"
    gitfile.write_text("gitdir: somewhere\n", encoding="utf-8")

    monkeypatch.setattr(
        comment_verdict_residue_nested,
        "_read_worktree_regular_text",
        lambda _path: None,
    )
    assert comment_verdict_residue_nested._nested_git_probe_git_dir(nested) is None

    monkeypatch.setattr(
        comment_verdict_residue_nested,
        "_read_worktree_regular_text",
        lambda _path: "gitdir: somewhere",
    )

    def _boom_resolve(self: Path) -> Path:
        raise OSError(errno.EIO, "resolve failed")

    monkeypatch.setattr(Path, "resolve", _boom_resolve)
    assert comment_verdict_residue_nested._nested_git_probe_git_dir(nested) is None


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_parse_nested_git_dir_gitfile_at_fail_closed_matrix(tmp_path: Path) -> None:
    """dir_fd gitfile parse must fail closed and keep relative paths unresolved."""
    nested = tmp_path / "nested_fd"
    nested.mkdir()
    dir_fd = os.open(nested, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert comment_verdict_residue_nested._parse_nested_git_dir_gitfile_at(dir_fd) is None

        (nested / ".git").mkdir()
        assert comment_verdict_residue_nested._parse_nested_git_dir_gitfile_at(dir_fd) is None
        (nested / ".git").rmdir()

        os.mkfifo(nested / ".git", mode=0o644)
        assert comment_verdict_residue_nested._parse_nested_git_dir_gitfile_at(dir_fd) is None
        (nested / ".git").unlink()

        (nested / ".git").write_text("nope\n", encoding="utf-8")
        assert comment_verdict_residue_nested._parse_nested_git_dir_gitfile_at(dir_fd) is None

        (nested / ".git").write_text("gitdir:\n", encoding="utf-8")
        assert comment_verdict_residue_nested._parse_nested_git_dir_gitfile_at(dir_fd) is None

        (nested / ".git").write_text("gitdir: ../modules/x\n", encoding="utf-8")
        parsed = comment_verdict_residue_nested._parse_nested_git_dir_gitfile_at(dir_fd)
        assert parsed == Path("../modules/x")
        assert not parsed.is_absolute()
    finally:
        os.close(dir_fd)


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_worktree_entry_kind_and_mode_token_specials() -> None:
    """Special entry kinds and executable regular modes must classify stably."""
    fifo_mode = stat.S_IFIFO | 0o644
    sock_mode = stat.S_IFSOCK | 0o600
    chr_mode = stat.S_IFCHR | 0o666
    blk_mode = stat.S_IFBLK | 0o660
    other_mode = 0

    assert comment_verdict_residue_io._worktree_entry_kind_from_mode(fifo_mode) == (
        "fifo",
        fifo_mode,
    )
    assert comment_verdict_residue_io._worktree_entry_kind_from_mode(sock_mode) == (
        "socket",
        sock_mode,
    )
    assert comment_verdict_residue_io._worktree_entry_kind_from_mode(chr_mode) == (
        "char",
        chr_mode,
    )
    assert comment_verdict_residue_io._worktree_entry_kind_from_mode(blk_mode) == (
        "block",
        blk_mode,
    )
    assert comment_verdict_residue_io._worktree_entry_kind_from_mode(other_mode)[0] == "other"

    assert (
        comment_verdict_residue_io._worktree_mode_from_kind(
            kind="regular", st_mode=stat.S_IFREG | 0o755
        )
        == "100755"
    )
    assert (
        comment_verdict_residue_io._worktree_mode_from_kind(
            kind="regular", st_mode=stat.S_IFREG | 0o644
        )
        == "100644"
    )
    assert (
        comment_verdict_residue_io._worktree_mode_from_kind(kind="symlink", st_mode=0) == "120000"
    )
    assert (
        comment_verdict_residue_io._worktree_mode_from_kind(kind="directory", st_mode=0) == "040000"
    )
    assert (
        comment_verdict_residue_io._worktree_mode_from_kind(kind="socket", st_mode=sock_mode)
        == "socket"
    )
    assert comment_verdict_residue_io._worktree_mode_from_kind(kind="bogus", st_mode=0) is None

    assert comment_verdict_residue_io._worktree_directory_entry_mode_token(
        kind="socket", st_mode=sock_mode
    ) == oct(stat.S_IMODE(sock_mode))
    assert (
        comment_verdict_residue_io._worktree_directory_entry_mode_token(
            kind="regular", st_mode=stat.S_IFREG | 0o755
        )
        == "100755"
    )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_digest_plain_directory_and_missing_path(tmp_path: Path) -> None:
    """Plain untracked directories must digest stably; missing / non-dir paths return None."""
    worktree = tmp_path / "ws_plain_dir"
    worktree.mkdir()
    init_git_worktree(worktree)
    plain = worktree / "artifacts"
    plain.mkdir()
    (plain / "a.txt").write_text("one\n", encoding="utf-8")

    first = comment_verdict_residue._digest_worktree_entry_bytes(
        worktree_path=worktree,
        path="artifacts",
        git_env=_git_env(),
    )
    assert first is not None
    second = comment_verdict_residue._digest_worktree_entry_bytes(
        worktree_path=worktree,
        path="artifacts",
        git_env=_git_env(),
    )
    assert second == first

    (plain / "a.txt").write_text("two\n", encoding="utf-8")
    mutated = comment_verdict_residue._digest_worktree_entry_bytes(
        worktree_path=worktree,
        path="artifacts",
        git_env=_git_env(),
    )
    assert mutated is not None and mutated != first

    assert (
        comment_verdict_residue._digest_worktree_entry_bytes(
            worktree_path=worktree,
            path="missing-entry",
            git_env=_git_env(),
        )
        is None
    )
    assert (
        comment_verdict_residue._hash_worktree_directory_residue(
            worktree_path=worktree,
            path="src/x.py",
            git_env=_git_env(),
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_digest_worktree_entry_bytes_at_symlink_and_open_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dir_fd digest must hash symlink text and fail closed on readlink/open errors."""
    worktree = tmp_path / "ws_digest_at"
    worktree.mkdir()
    init_git_worktree(worktree)
    link = worktree / "link"
    link.symlink_to("target-name")
    dir_fd = os.open(worktree, os.O_RDONLY | os.O_DIRECTORY)
    try:
        digest = comment_verdict_residue._digest_worktree_entry_bytes_at(
            dir_fd=dir_fd,
            entry_name="link",
            path="link",
            worktree_path=worktree,
        )
        assert digest is not None
        link.unlink()
        link.symlink_to("other-target")
        other = comment_verdict_residue._digest_worktree_entry_bytes_at(
            dir_fd=dir_fd,
            entry_name="link",
            path="link",
            worktree_path=worktree,
        )
        assert other is not None and other != digest

        def _boom_readlink(name: str, *, dir_fd: int | None = None) -> str:
            del name, dir_fd
            raise OSError(errno.EIO, "readlink failed")

        monkeypatch.setattr(os, "readlink", _boom_readlink)
        assert (
            comment_verdict_residue._digest_worktree_entry_bytes_at(
                dir_fd=dir_fd,
                entry_name="link",
                path="link",
                worktree_path=worktree,
            )
            is None
        )
    finally:
        os.close(dir_fd)

    regular = worktree / "blob.bin"
    regular.write_bytes(b"payload")
    dir_fd = os.open(worktree, os.O_RDONLY | os.O_DIRECTORY)
    try:

        @contextlib.contextmanager
        def _boom_open(_dir_fd: int, _name: str) -> Iterator[object]:
            raise OSError(errno.EACCES, "open failed")
            yield  # pragma: no cover

        monkeypatch.setattr(
            comment_verdict_residue,
            "_open_worktree_regular_file_at",
            _boom_open,
        )
        assert (
            comment_verdict_residue._digest_worktree_entry_bytes_at(
                dir_fd=dir_fd,
                entry_name="blob.bin",
                path="blob.bin",
                worktree_path=worktree,
            )
            is None
        )
    finally:
        os.close(dir_fd)


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_directory_residue_mid_walk_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Directory walk must fail closed when a child vanishes or nested commit fails."""
    worktree = tmp_path / "ws_dir_walk"
    worktree.mkdir()
    init_git_worktree_file_replaced_by_directory(worktree)
    path = "src/x.py"

    real_kind_at = comment_verdict_residue._worktree_entry_kind_at

    def _kind_then_none(dir_fd: int, name: str) -> tuple[str, int] | None:
        if name == "child.txt":
            return None
        return real_kind_at(dir_fd, name)

    monkeypatch.setattr(comment_verdict_residue, "_worktree_entry_kind_at", _kind_then_none)
    assert (
        comment_verdict_residue._hash_worktree_directory_residue(
            worktree_path=worktree,
            path=path,
            git_env=_git_env(),
        )
        is None
    )

    monkeypatch.setattr(comment_verdict_residue, "_worktree_entry_kind_at", real_kind_at)

    def _nested_none(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(comment_verdict_residue, "_git_nested_worktree_commit_at", _nested_none)
    nested_child = worktree / path / "nested"
    nested_child.mkdir()
    (nested_child / ".git").mkdir()
    (nested_child / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    assert (
        comment_verdict_residue._hash_worktree_directory_residue(
            worktree_path=worktree,
            path=path,
            git_env=_git_env(),
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_untracked_residue_paths_missing_vs_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENOENT after a None kind is stable <missing>; other OSError fails closed."""
    worktree = tmp_path / "ws_untracked_missing"
    worktree.mkdir()
    init_git_worktree(worktree)
    path = "ghost.bin"

    monkeypatch.setattr(comment_verdict_residue, "_worktree_entry_kind", lambda _c: None)

    first = comment_verdict_residue._hash_untracked_residue_paths(
        worktree_path=worktree,
        paths=[path],
        untracked={path},
    )
    second = comment_verdict_residue._hash_untracked_residue_paths(
        worktree_path=worktree,
        paths=[path],
        untracked={path},
    )
    assert first is not None and first == second

    present = worktree / "present.bin"
    present.write_bytes(b"x")

    def _kind_then_raise(candidate: Path) -> tuple[str, int] | None:
        if candidate.name == "present.bin":
            raise OSError(errno.EACCES, "permission denied")
        return None

    monkeypatch.setattr(comment_verdict_residue, "_worktree_entry_kind", _kind_then_raise)
    assert (
        comment_verdict_residue._hash_untracked_residue_paths(
            worktree_path=worktree,
            paths=["present.bin"],
            untracked={"present.bin"},
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_format_porcelain_z_line_rename_and_decode_empty_z() -> None:
    """Rename porcelain formatting and empty z-status without bytes must stay distinct."""
    assert (
        comment_verdict_residue_fingerprint._format_porcelain_z_line("R ", "new.py", "old.py")
        == "R  old.py -> new.py"
    )
    assert (
        comment_verdict_residue_fingerprint._format_porcelain_z_line(" M", "only.py", None)
        == " M only.py"
    )
    decoded, is_z = comment_verdict_residue_fingerprint._decode_porcelain_status_stdout(
        stdout="\0",
        stdout_bytes=None,
    )
    assert is_z is True
    assert decoded == "\0"


@pytest.mark.unit
@pytest.mark.timeout(2)
async def test_correction_fingerprint_empty_z_without_bytes_and_untracked_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty z porcelain without stdout_bytes is clean; untracked raise fails closed."""
    worktree = tmp_path / "ws_fp_edges"
    worktree.mkdir()
    init_git_worktree(worktree)

    async def _run_empty_z(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(
                returncode=0,
                stdout="   \0",
                stderr="",
                stdout_bytes=None,
            )
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run_empty_z)))
    empty = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_fp_edges",
        worktree_path=worktree,
    )
    assert empty == ""

    async def _run_dirty(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            payload = b"?? ghost.bin\0"
            return CommandResult(
                returncode=0,
                stdout=payload.decode("utf-8"),
                stderr="",
                stdout_bytes=payload,
            )
        return CommandResult(returncode=0, stdout="", stderr="")

    def _boom_untracked(**_kwargs: object) -> str:
        raise RuntimeError("untracked hash exploded")

    monkeypatch.setattr(
        comment_verdict_residue,
        "_hash_untracked_residue_paths",
        _boom_untracked,
    )
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run_dirty)))
    assert (
        await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
            runner,
            workspace_id="ws_fp_edges",
            worktree_path=worktree,
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_nested_probe_timeout_zero_when_budget_exhausted() -> None:
    """Exhausted nested-probe budget must short-circuit git probes as timed out."""
    holder = comment_verdict_residue_io._NestedProbeDeadline()
    holder.deadline = time.monotonic() - 1.0
    probe_token = comment_verdict_residue._NESTED_UNTRUSTED_GIT_PROBE.set(True)
    deadline_token = comment_verdict_residue._NESTED_UNTRUSTED_GIT_PROBE_DEADLINE.set(holder)
    try:
        assert comment_verdict_residue._nested_untrusted_git_probe_command_timeout() == 0.0
        result = comment_verdict_residue._run_git_bytes(
            worktree_path=Path("/tmp"),
            git_env={},
            args=("rev-parse", "HEAD"),
        )
        assert result.returncode == 124
        assert result.stdout == b""
        assert b"scan budget exceeded" in result.stderr
    finally:
        comment_verdict_residue._NESTED_UNTRUSTED_GIT_PROBE_DEADLINE.reset(deadline_token)
        comment_verdict_residue._NESTED_UNTRUSTED_GIT_PROBE.reset(probe_token)


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_read_worktree_regular_text_bounds_and_symlink_git_marker(tmp_path: Path) -> None:
    """Oversized regular text returns None; symlink .git is not a nested marker."""
    big = tmp_path / "big.txt"
    limit = comment_verdict_residue_io._WORKTREE_REGULAR_TEXT_READ_LIMIT_BYTES
    big.write_bytes(b"x" * (limit + 1))
    assert comment_verdict_residue_io._read_worktree_regular_text(big) is None

    missing = tmp_path / "gone.txt"
    assert comment_verdict_residue_io._read_worktree_regular_text(missing) is None

    directory = tmp_path / "dir_with_symlink_git"
    directory.mkdir()
    (directory / ".git").symlink_to("/tmp/elsewhere")
    assert comment_verdict_residue_io._has_nested_git_marker(directory) is False
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert comment_verdict_residue_io._has_nested_git_marker_at(dir_fd) is False
        assert comment_verdict_residue_io._read_worktree_regular_text_at(dir_fd, "nope") is None
        oversize = directory / "over.txt"
        oversize.write_bytes(b"y" * (limit + 1))
        assert comment_verdict_residue_io._read_worktree_regular_text_at(dir_fd, "over.txt") is None
    finally:
        os.close(dir_fd)


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_open_worktree_directory_guards(tmp_path: Path) -> None:
    """Directory open helpers must reject empty paths, ``..``, and non-directories."""
    worktree = tmp_path / "ws_open_guards"
    worktree.mkdir()
    init_git_worktree(worktree)

    with (
        pytest.raises(OSError) as empty,
        comment_verdict_residue_io._open_worktree_directory(worktree, ""),
    ):
        pass
    assert empty.value.errno == errno.EINVAL

    with (
        pytest.raises(OSError) as parent,
        comment_verdict_residue_io._open_worktree_directory(worktree, "src/../.."),
    ):
        pass
    assert parent.value.errno == errno.EINVAL

    with (
        pytest.raises(OSError) as not_dir,
        comment_verdict_residue_io._open_worktree_directory(worktree, "src/x.py"),
    ):
        pass
    assert not_dir.value.errno == errno.ENOTDIR


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_directory_enum_deadline_and_negative_count(tmp_path: Path) -> None:
    """Exhausted enum deadline and negative consume counts fail closed."""
    assert comment_verdict_residue_io._directory_enum_consume_entries(-1) is False

    worktree = tmp_path / "ws_enum_deadline"
    worktree.mkdir()
    (worktree / "a").write_text("x", encoding="utf-8")
    budget = comment_verdict_residue_io._DirectoryEnumBudget(
        entries_remaining=100,
        deadline=time.monotonic() - 1.0,
        max_depth=8,
    )
    token = comment_verdict_residue_io._DIRECTORY_ENUM_BUDGET.set(budget)
    try:
        dir_fd = os.open(worktree, os.O_RDONLY | os.O_DIRECTORY)
        try:
            assert comment_verdict_residue_io._sorted_worktree_directory_entry_names(dir_fd) is None
        finally:
            os.close(dir_fd)
    finally:
        comment_verdict_residue_io._DIRECTORY_ENUM_BUDGET.reset(token)


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_open_git_dir_path_at_fail_closed_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approved-root opens must fail closed when the nested root fd is dead or escapes."""
    worktree = tmp_path / "ws_open_git_dir"
    worktree.mkdir()
    init_git_worktree(worktree)
    nested = worktree / "nested"
    nested.mkdir()
    dir_fd = os.open(nested, os.O_RDONLY | os.O_DIRECTORY)
    try:
        monkeypatch.setattr(
            comment_verdict_residue_nested,
            "_fresh_worktree_path_for_open_fd",
            lambda _fd: None,
        )
        assert (
            comment_verdict_residue_nested._open_git_dir_path_at(
                dir_fd,
                Path("somewhere"),
                outer_worktree_path=worktree,
            )
            is None
        )
    finally:
        os.close(dir_fd)

    outside = tmp_path / "outside_git"
    outside.mkdir()
    assert (
        comment_verdict_residue_nested._open_git_dir_path_at(
            -1,
            outside,
            outer_worktree_path=worktree,
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_parse_nested_git_commondir_unreadable_raises(tmp_path: Path) -> None:
    """Non-regular or unreadable commondir must raise so callers fail closed."""
    marker = tmp_path / "marker"
    marker.mkdir()
    os.mkfifo(marker / "commondir", mode=0o644)
    marker_fd = os.open(marker, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError):
            comment_verdict_residue_nested._parse_nested_git_commondir_at(marker_fd)
    finally:
        os.close(marker_fd)

    (marker / "commondir").unlink()
    (marker / "commondir").write_text("relative-common\n", encoding="utf-8")
    marker_fd = os.open(marker, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert comment_verdict_residue_nested._parse_nested_git_commondir_at(marker_fd) == Path(
            "relative-common"
        )
    finally:
        os.close(marker_fd)

    (marker / "commondir").unlink()
    marker_fd = os.open(marker, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert comment_verdict_residue_nested._parse_nested_git_commondir_at(marker_fd) is None
    finally:
        os.close(marker_fd)


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_open_nested_git_dir_marker_at_missing_or_file(tmp_path: Path) -> None:
    """Directory-marker open must yield None when .git is missing or not a directory."""
    nested = tmp_path / "nested_marker"
    nested.mkdir()
    outer = tmp_path / "outer"
    outer.mkdir()
    dir_fd = os.open(nested, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with comment_verdict_residue_nested._open_nested_git_dir_marker_at(
            dir_fd, outer_worktree_path=outer
        ) as opened:
            assert opened is None

        (nested / ".git").write_text("gitdir: x\n", encoding="utf-8")
        with comment_verdict_residue_nested._open_nested_git_dir_marker_at(
            dir_fd, outer_worktree_path=outer
        ) as opened:
            assert opened is None
    finally:
        os.close(dir_fd)


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_fresh_worktree_path_for_open_fd_dead_and_readlink_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pinned fd path resolution must fail closed for dead fds and readlink errors."""
    assert comment_verdict_residue_io._fresh_worktree_path_for_open_fd(-1) is None

    monkeypatch.setattr(
        comment_verdict_residue_io,
        "_worktree_proc_path_for_open_fd",
        lambda _fd: Path("/proc/self/fd/999999"),
    )

    def _boom_readlink(self: Path) -> Path:
        raise OSError(errno.ENOENT, "gone")

    monkeypatch.setattr(Path, "readlink", _boom_readlink)
    assert comment_verdict_residue_io._fresh_worktree_path_for_open_fd(3) is None


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_digest_special_socket_kind_via_monkeypatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Special entry kinds (socket) must contribute a stable kind:mode digest."""
    worktree = tmp_path / "ws_socket"
    worktree.mkdir()
    init_git_worktree(worktree)
    sock_mode = stat.S_IFSOCK | 0o600

    monkeypatch.setattr(
        comment_verdict_residue,
        "_worktree_entry_kind",
        lambda _candidate: ("socket", sock_mode),
    )
    digest = comment_verdict_residue._digest_worktree_entry_bytes(
        worktree_path=worktree,
        path="sock",
        git_env=_git_env(),
    )
    assert digest is not None

    dir_fd = os.open(worktree, os.O_RDONLY | os.O_DIRECTORY)
    try:
        monkeypatch.setattr(
            comment_verdict_residue,
            "_worktree_entry_kind_at",
            lambda _dir_fd, _name: ("socket", sock_mode),
        )
        at_digest = comment_verdict_residue._digest_worktree_entry_bytes_at(
            dir_fd=dir_fd,
            entry_name="sock",
            path="sock",
            worktree_path=worktree,
        )
        assert at_digest is not None
        assert at_digest == digest
    finally:
        os.close(dir_fd)


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_resolve_nested_worktree_head_fail_closed_when_verify_diverges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HEAD resolution must fail closed when rev-parse HEAD and verify disagree."""
    worktree = tmp_path / "ws_head_resolve"
    worktree.mkdir()
    init_git_worktree(worktree)

    results = iter(
        [
            subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout=b"abc123\n", stderr=b""),
        ]
    )

    def _fake_run_git_bytes(**_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return next(results)

    monkeypatch.setattr(comment_verdict_residue, "_run_git_bytes", _fake_run_git_bytes)
    assert (
        comment_verdict_residue._resolve_nested_worktree_head(
            worktree_path=worktree,
            git_env=_git_env(),
        )
        is None
    )

    results2 = iter(
        [
            subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b""),
            subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b""),
            subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b""),
        ]
    )

    def _fake_run_git_bytes2(**_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return next(results2)

    monkeypatch.setattr(comment_verdict_residue, "_run_git_bytes", _fake_run_git_bytes2)
    assert (
        comment_verdict_residue._resolve_nested_worktree_head(
            worktree_path=worktree,
            git_env=_git_env(),
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_tracked_diffs_deleted_gitlink_uses_index_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleted gitlink worktree blobs must fingerprint with wm:160000, not <missing>."""
    worktree = tmp_path / "ws_gitlink"
    worktree.mkdir()
    init_git_worktree(worktree)

    index_blob = "a" * 40

    monkeypatch.setattr(
        comment_verdict_residue,
        "_run_git_bytes",
        lambda **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"sub\0", stderr=b""
        ),
    )
    monkeypatch.setattr(
        comment_verdict_residue,
        "_git_index_blob_sha",
        lambda **_kwargs: index_blob,
    )
    monkeypatch.setattr(
        comment_verdict_residue,
        "_git_index_mode",
        lambda **_kwargs: "160000",
    )
    monkeypatch.setattr(
        comment_verdict_residue,
        "_git_worktree_blob_sha",
        lambda **_kwargs: "<deleted>",
    )
    monkeypatch.setattr(
        comment_verdict_residue,
        "_git_worktree_mode",
        lambda **_kwargs: None,
    )

    result = comment_verdict_residue._hash_tracked_residue_diffs(
        worktree_path=worktree,
        git_env=_git_env(),
        cached=False,
    )
    assert result is not None

    expected = hashlib.sha256()
    expected.update(b"sub\0")
    expected.update(b"index:")
    expected.update(index_blob.encode("ascii"))
    expected.update(b"im:")
    expected.update(b"160000")
    expected.update(b"wt:")
    expected.update(b"<deleted>")
    expected.update(b"wm:")
    expected.update(b"160000")
    expected.update(b"\0")
    assert result == expected.hexdigest()


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_fresh_pinned_nested_fds_fail_closed_on_dead_fd_and_readlink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pinned nested git-dir / common-dir / worktree fd resolution must fail closed."""
    dead = -1
    marker_token = comment_verdict_residue._NESTED_UNTRUSTED_GIT_PROBE_GIT_MARKER_FD.set(dead)
    try:
        assert comment_verdict_residue._fresh_pinned_nested_git_dir() is None
    finally:
        comment_verdict_residue._NESTED_UNTRUSTED_GIT_PROBE_GIT_MARKER_FD.reset(marker_token)

    common_token = comment_verdict_residue._NESTED_UNTRUSTED_GIT_PROBE_GIT_COMMON_FD.set(dead)
    try:
        assert comment_verdict_residue._fresh_pinned_nested_git_common_dir() is None
    finally:
        comment_verdict_residue._NESTED_UNTRUSTED_GIT_PROBE_GIT_COMMON_FD.reset(common_token)

    wt_token = comment_verdict_residue._NESTED_UNTRUSTED_GIT_PROBE_WORKTREE_FD.set(dead)
    try:
        assert comment_verdict_residue._fresh_pinned_nested_worktree() is None
    finally:
        comment_verdict_residue._NESTED_UNTRUSTED_GIT_PROBE_WORKTREE_FD.reset(wt_token)

    # Live fd path with readlink OSError.
    monkeypatch.setattr(
        comment_verdict_residue,
        "_worktree_proc_path_for_open_fd",
        lambda _fd: Path("/proc/self/fd/3"),
    )

    def _boom_readlink(self: Path) -> Path:
        raise OSError(errno.ENOENT, "gone")

    monkeypatch.setattr(Path, "readlink", _boom_readlink)
    marker_token = comment_verdict_residue._NESTED_UNTRUSTED_GIT_PROBE_GIT_MARKER_FD.set(3)
    try:
        assert comment_verdict_residue._fresh_pinned_nested_git_dir() is None
    finally:
        comment_verdict_residue._NESTED_UNTRUSTED_GIT_PROBE_GIT_MARKER_FD.reset(marker_token)

    common_token = comment_verdict_residue._NESTED_UNTRUSTED_GIT_PROBE_GIT_COMMON_FD.set(3)
    try:
        assert comment_verdict_residue._fresh_pinned_nested_git_common_dir() is None
    finally:
        comment_verdict_residue._NESTED_UNTRUSTED_GIT_PROBE_GIT_COMMON_FD.reset(common_token)

    wt_token = comment_verdict_residue._NESTED_UNTRUSTED_GIT_PROBE_WORKTREE_FD.set(3)
    try:
        assert comment_verdict_residue._fresh_pinned_nested_worktree() is None
    finally:
        comment_verdict_residue._NESTED_UNTRUSTED_GIT_PROBE_WORKTREE_FD.reset(wt_token)


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_directory_child_open_and_digest_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-walk child open OSError and child digest None must fail closed."""
    worktree = tmp_path / "ws_child_open"
    worktree.mkdir()
    init_git_worktree_file_replaced_by_directory(worktree)
    path = "src/x.py"

    real_open = os.open

    def _open_boom(name: str | int | bytes, flags: int, *args: object, **kwargs: object) -> int:
        if isinstance(name, str) and name == "child.txt":
            raise OSError(errno.EACCES, "open failed")
        # directory children may also call open; only trip on child.txt as file open
        # When opening a subdirectory named differently, pass through.
        if kwargs.get("dir_fd") is not None and name == "child.txt":
            raise OSError(errno.EACCES, "open failed")
        return real_open(name, flags, *args, **kwargs)  # type: ignore[arg-type]

    # child.txt is a file — digest path, not open-as-dir. Force digest None instead.
    monkeypatch.setattr(
        comment_verdict_residue,
        "_digest_worktree_entry_bytes_at",
        lambda **_kwargs: None,
    )
    assert (
        comment_verdict_residue._hash_worktree_directory_residue(
            worktree_path=worktree,
            path=path,
            git_env=_git_env(),
        )
        is None
    )

    # Subdirectory open failure.
    monkeypatch.undo()
    sub = worktree / path / "subdir"
    sub.mkdir()
    (sub / "nested.txt").write_text("x\n", encoding="utf-8")

    def _open_subdir_boom(
        name: str | int | bytes, flags: int, *args: object, **kwargs: object
    ) -> int:
        if name == "subdir" and kwargs.get("dir_fd") is not None:
            raise OSError(errno.EACCES, "subdir open failed")
        return real_open(name, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", _open_subdir_boom)
    assert (
        comment_verdict_residue._hash_worktree_directory_residue(
            worktree_path=worktree,
            path=path,
            git_env=_git_env(),
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_directory_child_fstat_not_dir_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Child opened as directory but fstat says non-dir must fail closed."""
    worktree = tmp_path / "ws_fstat_not_dir"
    worktree.mkdir()
    init_git_worktree_file_replaced_by_directory(worktree)
    path = "src/x.py"
    sub = worktree / path / "subdir"
    sub.mkdir()
    (sub / "nested.txt").write_text("x\n", encoding="utf-8")

    real_fstat = os.fstat
    parent_dir_fd: int | None = None

    def _fstat_lie(fd: int) -> os.stat_result:
        nonlocal parent_dir_fd
        result = real_fstat(fd)
        if not stat.S_ISDIR(result.st_mode):
            return result
        if parent_dir_fd is None:
            parent_dir_fd = fd
            return result
        # Lie about any later directory fd (the child subdir).
        return os.stat_result((stat.S_IFREG | 0o644, *result[1:]))

    monkeypatch.setattr(os, "fstat", _fstat_lie)
    assert (
        comment_verdict_residue._hash_worktree_directory_residue(
            worktree_path=worktree,
            path=path,
            git_env=_git_env(),
        )
        is None
    )
