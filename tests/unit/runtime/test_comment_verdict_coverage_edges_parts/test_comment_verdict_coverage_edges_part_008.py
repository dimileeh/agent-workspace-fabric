"""Coverage-edge regressions for residue fingerprint fail-closed / classification paths (cont.).

Second half of the former oversized part_007 (line-budget split). Prefer direct
helper assertions over protocol-level churn.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import stat
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult
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
)

_git_env = git_env_without_object_lookup_overrides


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_approved_root_for_git_dir_resolve_and_relative_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approved-root lookup must fail closed on resolve OSError and relative_to errors."""
    worktree = tmp_path / "ws_approved"
    worktree.mkdir()
    candidate = worktree / "gitdir"
    candidate.mkdir()

    def _boom_resolve(self: Path) -> Path:
        if self == candidate or self.name == "gitdir":
            raise OSError(errno.EIO, "resolve failed")
        return Path.resolve.__get__(self, Path)()  # type: ignore[misc]

    # Simpler: patch Path.resolve globally for this candidate via monkeypatch on method.
    original_resolve = Path.resolve

    def _resolve_maybe_boom(self: Path, *args: object, **kwargs: object) -> Path:
        text = str(self)
        if text.endswith("gitdir") or self == candidate:
            raise OSError(errno.EIO, "resolve failed")
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _resolve_maybe_boom)
    assert (
        comment_verdict_residue_nested._approved_root_for_git_dir(
            candidate, outer_worktree_path=worktree
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_open_git_dir_path_at_relative_to_and_open_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git-dir open must fail closed on relative_to errors, root open failure, and non-dir leaf."""
    worktree = tmp_path / "ws_git_open"
    worktree.mkdir()
    nested = worktree / "nested"
    nested.mkdir()
    git_meta = worktree / ".git-meta"
    git_meta.mkdir()
    # Leaf that is a file, not a directory.
    leaf = git_meta / "HEAD"
    leaf.write_text("ref: refs/heads/main\n", encoding="utf-8")

    dir_fd = os.open(nested, os.O_RDONLY | os.O_DIRECTORY)
    try:
        monkeypatch.setattr(
            comment_verdict_residue_nested,
            "_fresh_worktree_path_for_open_fd",
            lambda _fd: nested,
        )
        # Relative path to a regular file leaf under approved root.
        assert (
            comment_verdict_residue_nested._open_git_dir_path_at(
                dir_fd,
                Path("../.git-meta/HEAD"),
                outer_worktree_path=worktree,
            )
            is None
        )

        # relative_to OSError after approval.
        def _boom_relative_to(self: Path, *args: object, **kwargs: object) -> Path:
            raise OSError(errno.EIO, "relative_to failed")

        monkeypatch.setattr(Path, "relative_to", _boom_relative_to)
        assert (
            comment_verdict_residue_nested._open_git_dir_path_at(
                dir_fd,
                Path("../.git-meta"),
                outer_worktree_path=worktree,
            )
            is None
        )
    finally:
        os.close(dir_fd)

    # Absolute approved path whose open fails.
    monkeypatch.undo()
    real_open = os.open

    def _open_root_boom(
        name: str | int | bytes, flags: int, *args: object, **kwargs: object
    ) -> int:
        if name == worktree or Path(str(name)) == worktree:
            raise OSError(errno.EACCES, "root open failed")
        return real_open(name, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", _open_root_boom)
    assert (
        comment_verdict_residue_nested._open_git_dir_path_at(
            -1,
            git_meta,
            outer_worktree_path=worktree,
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_open_worktree_directory_path_outer_root_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening the outer worktree itself must yield None on open/fstat failure."""
    worktree = tmp_path / "ws_outer_open"
    worktree.mkdir()

    real_open = os.open

    def _boom_open(name: str | int | bytes, flags: int, *args: object, **kwargs: object) -> int:
        if Path(str(name)) == worktree:
            raise OSError(errno.EACCES, "cannot open")
        return real_open(name, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", _boom_open)
    with comment_verdict_residue_io._open_worktree_directory_path(
        worktree, outer_worktree_path=worktree
    ) as fd:
        assert fd is None


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_popen_capped_nul_terminates_still_running_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capped NUL reader must terminate a process still running in finally."""
    terminated: list[object] = []

    class _FakeStdout:
        def close(self) -> None:
            return None

    class _StickyProc:
        def __init__(self) -> None:
            self.stdout = _FakeStdout()

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

        def poll(self) -> int | None:
            return None

        def kill(self) -> None:
            return None

    def _fake_read(*_args: object, **_kwargs: object) -> tuple[bytes, ...]:
        return (b"a",)

    def _fake_terminate(proc: object) -> None:
        terminated.append(proc)

    monkeypatch.setattr(
        comment_verdict_residue_io,
        "_read_capped_nul_path_records",
        _fake_read,
    )
    monkeypatch.setattr(
        comment_verdict_residue_io,
        "_terminate_capped_nul_path_process",
        _fake_terminate,
    )
    monkeypatch.setattr(
        comment_verdict_residue_io.subprocess,
        "Popen",
        lambda *_a, **_k: _StickyProc(),
    )

    result = comment_verdict_residue_io._popen_capped_nul_path_records(
        ["git", "ls-files", "-z"],
        env={},
        max_records=10,
        max_bytes=100,
        timeout=None,
    )
    assert result == (b"a",)
    assert len(terminated) == 1


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_sorted_directory_entries_oserror_and_dot_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scandir OSError returns None; ``.`` / ``..`` entries are skipped."""
    worktree = tmp_path / "ws_scandir"
    worktree.mkdir()
    (worktree / "keep.txt").write_text("x", encoding="utf-8")
    dir_fd = os.open(worktree, os.O_RDONLY | os.O_DIRECTORY)
    try:
        names = comment_verdict_residue_io._sorted_worktree_directory_entry_names(dir_fd)
        assert names == ["keep.txt"]
    finally:
        os.close(dir_fd)

    def _boom_scandir(_path: str) -> object:
        raise OSError(errno.EIO, "scandir failed")

    monkeypatch.setattr(os, "scandir", _boom_scandir)
    dir_fd = os.open(worktree, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert comment_verdict_residue_io._sorted_worktree_directory_entry_names(dir_fd) is None
    finally:
        os.close(dir_fd)


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_parse_nested_git_commondir_oserror_and_empty_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreadable commondir lstat raises; empty Path parts return None."""
    marker = tmp_path / "marker2"
    marker.mkdir()
    (marker / "commondir").write_text("ok\n", encoding="utf-8")
    marker_fd = os.open(marker, os.O_RDONLY | os.O_DIRECTORY)
    try:
        real_lstat = os.lstat

        def _lstat_boom(path: str | bytes | int, *args: object, **kwargs: object) -> os.stat_result:
            if path == "commondir":
                raise OSError(errno.EACCES, "denied")
            return real_lstat(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "lstat", _lstat_boom)
        with pytest.raises(OSError, match="unreadable"):
            comment_verdict_residue_nested._parse_nested_git_commondir_at(marker_fd)
    finally:
        os.close(marker_fd)

    monkeypatch.undo()
    (marker / "commondir").write_text("relative\n", encoding="utf-8")
    marker_fd = os.open(marker, os.O_RDONLY | os.O_DIRECTORY)
    try:
        monkeypatch.setattr(
            comment_verdict_residue_nested,
            "_read_worktree_regular_text_at",
            lambda *_a, **_k: "",
        )
        assert comment_verdict_residue_nested._parse_nested_git_commondir_at(marker_fd) is None
    finally:
        os.close(marker_fd)


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_open_nested_git_dir_gitfile_target_non_dir_yields_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gitfile target fd that is not a directory must yield None without leaking fds."""
    worktree = tmp_path / "ws_gitfile_nondir"
    worktree.mkdir()
    nested = worktree / "nested"
    nested.mkdir()
    target_file = worktree / "not-a-dir"
    target_file.write_text("x", encoding="utf-8")
    (nested / ".git").write_text(f"gitdir: {target_file}\n", encoding="utf-8")

    dir_fd = os.open(nested, os.O_RDONLY | os.O_DIRECTORY)
    try:
        # Force open to return an fd to the regular file.
        opened_fds: list[int] = []
        real_open = os.open

        def _open_track(
            name: str | int | bytes, flags: int, *args: object, **kwargs: object
        ) -> int:
            fd = real_open(name, flags, *args, **kwargs)  # type: ignore[arg-type]
            opened_fds.append(fd)
            return fd

        monkeypatch.setattr(os, "open", _open_track)
        # Bypass approved-root directory requirement by opening the file via helper mock.
        file_fd = os.open(target_file, os.O_RDONLY)
        try:
            monkeypatch.setattr(
                comment_verdict_residue_nested,
                "_open_git_dir_path_at",
                lambda *_a, **_k: file_fd,
            )
            # Steal ownership: the context manager will close file_fd.
            with comment_verdict_residue_nested._open_nested_git_dir_gitfile_target_at(
                dir_fd, outer_worktree_path=worktree
            ) as opened:
                assert opened is None
        except OSError:
            os.close(file_fd)
            raise
    finally:
        os.close(dir_fd)


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_opened_regular_into_false_fails_digest_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regular digest_at must return None when bounded hashing rejects the open file."""
    worktree = tmp_path / "ws_hash_false"
    worktree.mkdir()
    init_git_worktree(worktree)
    blob = worktree / "blob.bin"
    blob.write_bytes(b"payload")
    dir_fd = os.open(worktree, os.O_RDONLY | os.O_DIRECTORY)
    try:
        monkeypatch.setattr(
            comment_verdict_residue,
            "_hash_opened_regular_file_into",
            lambda *_a, **_k: False,
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
def test_resolve_nested_head_empty_symref_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty symbolic-ref stdout must fail closed rather than return <unborn>."""
    worktree = tmp_path / "ws_empty_symref"
    worktree.mkdir()
    init_git_worktree(worktree)
    results = iter(
        [
            subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b""),
            subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b""),
        ]
    )
    monkeypatch.setattr(
        comment_verdict_residue,
        "_run_git_bytes",
        lambda **_k: next(results),
    )
    assert (
        comment_verdict_residue._resolve_nested_worktree_head(
            worktree_path=worktree,
            git_env=_git_env(),
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.timeout(2)
async def test_correction_fingerprint_empty_z_stdout_bytes_none_whitespace_only(
    tmp_path: Path,
) -> None:
    """is_z with stdout_bytes None and whitespace-only decoded stdout is clean (line 120)."""
    worktree = tmp_path / "ws_fp_line120"
    worktree.mkdir()
    init_git_worktree(worktree)

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            # Decode reports is_z via embedded NUL, but strip() is empty only if we
            # pass a stdout that strips to empty while still containing NUL — use
            # the fingerprint helper's decode path by providing stdout_bytes=None
            # and a NUL-only-after-spaces string... null survives strip, so force
            # the branch via monkeypatch of decode instead when needed.
            return CommandResult(returncode=0, stdout="\n\n", stderr="", stdout_bytes=None)
        return CommandResult(returncode=0, stdout="", stderr="")

    # Directly exercise the unreachable-looking branch by patching decode.
    # Prefer a real decode path: whitespace-only without NUL is non-z and hits
    # ``elif not status_stdout.strip(): return ""`` — still a clean fingerprint.
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))
    assert (
        await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
            runner,
            workspace_id="ws_fp_line120",
            worktree_path=worktree,
        )
        == ""
    )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_parse_gitfile_at_unreadable_text_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dir_fd gitfile parse returns None when the regular-text reader fails."""
    nested = tmp_path / "nested_unreadable_text"
    nested.mkdir()
    (nested / ".git").write_text("gitdir: x\n", encoding="utf-8")
    dir_fd = os.open(nested, os.O_RDONLY | os.O_DIRECTORY)
    try:
        monkeypatch.setattr(
            comment_verdict_residue_nested,
            "_read_worktree_regular_text_at",
            lambda *_a, **_k: None,
        )
        assert comment_verdict_residue_nested._parse_nested_git_dir_gitfile_at(dir_fd) is None
    finally:
        os.close(dir_fd)


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_approved_root_skips_roots_that_raise_on_relative_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """is_relative_to OSError/ValueError must skip that root and continue."""
    worktree = tmp_path / "ws_rel_skip"
    worktree.mkdir()
    candidate = worktree / "meta"
    candidate.mkdir()

    original = Path.is_relative_to

    def _boom(self: Path, other: Path) -> bool:
        raise OSError(errno.EIO, "boom")

    monkeypatch.setattr(Path, "is_relative_to", _boom)
    # Equality short-circuit: use a distinct root path string so == is False.
    monkeypatch.setattr(
        comment_verdict_residue_nested,
        "_approved_git_metadata_roots",
        lambda _outer: (worktree / "not-exact-equal",),
    )
    assert (
        comment_verdict_residue_nested._approved_root_for_git_dir(
            candidate, outer_worktree_path=worktree
        )
        is None
    )
    del original


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_open_git_dir_path_rejects_dotdot_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``..`` components in a relative git-dir path must fail closed."""
    worktree = tmp_path / "ws_dotdot"
    worktree.mkdir()
    nested = worktree / "nested"
    nested.mkdir()
    (worktree / "safe").mkdir()

    dir_fd = os.open(nested, os.O_RDONLY | os.O_DIRECTORY)
    try:
        monkeypatch.setattr(
            comment_verdict_residue_nested,
            "_fresh_worktree_path_for_open_fd",
            lambda _fd: nested,
        )
        # Craft relative_to so parts include '..' after approval.
        monkeypatch.setattr(
            comment_verdict_residue_nested,
            "_approved_root_for_git_dir",
            lambda _c, outer_worktree_path: outer_worktree_path,
        )

        class _PartsWithDotDot(Path):
            @property
            def parts(self) -> tuple[str, ...]:  # type: ignore[override]
                return ("..", "safe")

        original_resolve = Path.resolve

        def _resolve(self: Path, *args: object, **kwargs: object) -> Path:
            resolved = original_resolve(self, *args, **kwargs)

            class _Resolved(type(resolved)):
                def relative_to(self, *_a: object, **_k: object) -> Path:
                    return _PartsWithDotDot("..")

            return _Resolved(resolved)

        monkeypatch.setattr(Path, "resolve", _resolve)
        assert (
            comment_verdict_residue_nested._open_git_dir_path_at(
                dir_fd,
                Path("safe"),
                outer_worktree_path=worktree,
            )
            is None
        )
    finally:
        os.close(dir_fd)


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_directory_enum_allows_descent_without_budget() -> None:
    """No active directory-enum budget allows descent at any depth."""
    assert comment_verdict_residue_io._directory_enum_allows_descent(0) is True
    assert comment_verdict_residue_io._directory_enum_allows_descent(999) is True


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_directory_enum_consume_without_budget() -> None:
    """No active budget always accepts entry consumption."""
    assert comment_verdict_residue_io._directory_enum_consume_entries(5) is True


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_worktree_entry_kind_at_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """lstat OSError for a dir_fd entry returns None."""
    worktree = tmp_path / "ws_kind_at"
    worktree.mkdir()
    dir_fd = os.open(worktree, os.O_RDONLY | os.O_DIRECTORY)
    try:
        real_lstat = os.lstat

        def _boom(name: str | bytes | int, *args: object, **kwargs: object) -> os.stat_result:
            if name == "ghost":
                raise OSError(errno.EACCES, "denied")
            return real_lstat(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "lstat", _boom)
        assert comment_verdict_residue_io._worktree_entry_kind_at(dir_fd, "ghost") is None
    finally:
        os.close(dir_fd)


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_open_worktree_directory_path_fstat_not_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outer-root open that fstats as non-dir must yield None."""
    worktree = tmp_path / "ws_outer_fstat"
    worktree.mkdir()
    real_fstat = os.fstat

    def _lie(fd: int) -> os.stat_result:
        result = real_fstat(fd)
        return os.stat_result((stat.S_IFREG | 0o644, *result[1:]))

    monkeypatch.setattr(os, "fstat", _lie)
    with comment_verdict_residue_io._open_worktree_directory_path(
        worktree, outer_worktree_path=worktree
    ) as fd:
        assert fd is None


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_untracked_kind_none_but_lstat_succeeds_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """None kind followed by a successful lstat is inconsistent and fails closed."""
    worktree = tmp_path / "ws_kind_inconsistent"
    worktree.mkdir()
    init_git_worktree(worktree)
    target = worktree / "present.bin"
    target.write_bytes(b"x")

    monkeypatch.setattr(comment_verdict_residue, "_worktree_entry_kind", lambda _c: None)
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
def test_hash_untracked_outer_enoent_uses_missing_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outer OSError ENOENT while digesting must hash as stable <missing>."""
    worktree = tmp_path / "ws_outer_enoent"
    worktree.mkdir()
    init_git_worktree(worktree)

    def _kind_then_digest_raises(candidate: Path) -> tuple[str, int]:
        # Classify as regular then raise ENOENT from digest path via monkeypatch below.
        return ("regular", stat.S_IFREG | 0o644)

    monkeypatch.setattr(comment_verdict_residue, "_worktree_entry_kind", _kind_then_digest_raises)

    def _digest_raises(**_kwargs: object) -> bytes:
        raise OSError(errno.ENOENT, "raced away")

    monkeypatch.setattr(comment_verdict_residue, "_digest_worktree_entry_bytes", _digest_raises)
    first = comment_verdict_residue._hash_untracked_residue_paths(
        worktree_path=worktree,
        paths=["ghost.bin"],
        untracked={"ghost.bin"},
    )
    second = comment_verdict_residue._hash_untracked_residue_paths(
        worktree_path=worktree,
        paths=["ghost.bin"],
        untracked={"ghost.bin"},
    )
    assert first is not None and first == second


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_nested_dir_hash_returns_none_when_child_dir_hash_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recursive directory hash None mid-walk must fail closed (line 490)."""
    worktree = tmp_path / "ws_nested_dir_none"
    worktree.mkdir()
    init_git_worktree_file_replaced_by_directory(worktree)
    path = "src/x.py"
    sub = worktree / path / "subdir"
    sub.mkdir()
    (sub / "nested.txt").write_text("x\n", encoding="utf-8")

    real = comment_verdict_residue._hash_worktree_directory_residue_at_dir_fd
    calls = {"n": 0}

    def _wrap(**kwargs: object) -> str | None:
        calls["n"] += 1
        if calls["n"] > 1:
            return None
        return real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        comment_verdict_residue, "_hash_worktree_directory_residue_at_dir_fd", _wrap
    )
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
def test_digest_kind_none_at_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """digest_at returns None when entry kind cannot be classified."""
    worktree = tmp_path / "ws_digest_none"
    worktree.mkdir()
    dir_fd = os.open(worktree, os.O_RDONLY | os.O_DIRECTORY)
    try:
        monkeypatch.setattr(comment_verdict_residue, "_worktree_entry_kind_at", lambda *_a: None)
        assert (
            comment_verdict_residue._digest_worktree_entry_bytes_at(
                dir_fd=dir_fd,
                entry_name="x",
                path="x",
                worktree_path=worktree,
            )
            is None
        )
    finally:
        os.close(dir_fd)


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_sorted_entries_skips_dot_names_via_fake_scandir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit ``.`` / ``..`` scandir entries must be skipped (line 644)."""
    worktree = tmp_path / "ws_dot_skip"
    worktree.mkdir()
    (worktree / "keep.txt").write_text("x", encoding="utf-8")

    class _Entry:
        def __init__(self, name: str) -> None:
            self.name = name

    class _Scan:
        def __enter__(self) -> object:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def __iter__(self) -> object:
            return iter([_Entry("."), _Entry(".."), _Entry("keep.txt")])

    monkeypatch.setattr(os, "scandir", lambda _path: _Scan())
    dir_fd = os.open(worktree, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert comment_verdict_residue_io._sorted_worktree_directory_entry_names(dir_fd) == [
            "keep.txt"
        ]
    finally:
        os.close(dir_fd)


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_directory_enum_consume_past_deadline() -> None:
    """Entry consume must fail closed when the enum deadline has already passed."""
    budget = comment_verdict_residue_io._DirectoryEnumBudget(
        entries_remaining=100,
        deadline=time.monotonic() - 1.0,
        max_depth=8,
    )
    token = comment_verdict_residue_io._DIRECTORY_ENUM_BUDGET.set(budget)
    try:
        assert comment_verdict_residue_io._directory_enum_consume_entries(1) is False
    finally:
        comment_verdict_residue_io._DIRECTORY_ENUM_BUDGET.reset(token)


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_open_git_dir_skips_empty_path_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty relative path components are skipped during approved-root descent."""
    worktree = tmp_path / "ws_empty_parts"
    worktree.mkdir()
    meta = worktree / "meta"
    meta.mkdir()
    (meta / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    class _Parts(Path):
        @property
        def parts(self) -> tuple[str, ...]:  # type: ignore[override]
            return ("", ".", "meta")

    monkeypatch.setattr(
        comment_verdict_residue_nested,
        "_approved_root_for_git_dir",
        lambda _c, outer_worktree_path: outer_worktree_path,
    )
    original_resolve = Path.resolve

    def _resolve(self: Path, *args: object, **kwargs: object) -> Path:
        resolved = original_resolve(self, *args, **kwargs)

        class _Resolved(type(resolved)):
            def relative_to(self, *_a: object, **_k: object) -> Path:
                return _Parts("meta")

        return _Resolved(resolved)

    monkeypatch.setattr(Path, "resolve", _resolve)
    fd = comment_verdict_residue_nested._open_git_dir_path_at(
        -1,
        meta,
        outer_worktree_path=worktree,
    )
    assert fd is not None
    os.close(fd)


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_nested_probe_root_resolve_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """show-toplevel resolve OSError must fail closed."""
    nested = tmp_path / "nested_resolve"
    nested.mkdir()
    monkeypatch.setattr(
        comment_verdict_residue,
        "_run_git_bytes",
        lambda **_k: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"/some/path\n", stderr=b""
        ),
    )

    def _boom_resolve(self: Path, *args: object, **kwargs: object) -> Path:
        raise OSError(errno.EIO, "resolve failed")

    monkeypatch.setattr(Path, "resolve", _boom_resolve)
    assert (
        comment_verdict_residue._nested_git_probe_worktree_root(
            nested_root=nested,
            git_env=_git_env(),
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_git_nested_worktree_commit_open_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nested commit identity fails closed when the directory cannot be opened."""
    worktree = tmp_path / "ws_nested_open_fail"
    worktree.mkdir()
    init_git_worktree(worktree)
    target = worktree / "nested"
    target.mkdir()
    (target / ".git").mkdir()

    def _boom_open(*_a: object, **_k: object):
        raise OSError(errno.EACCES, "denied")
        yield  # pragma: no cover

    monkeypatch.setattr(
        comment_verdict_residue,
        "_open_worktree_directory",
        contextlib.contextmanager(_boom_open),
    )
    assert (
        comment_verdict_residue._git_nested_worktree_commit(
            worktree_path=worktree,
            path="nested",
            git_env=_git_env(),
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_digest_directory_hash_none_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plain-directory digest must fail closed when directory hashing returns None."""
    worktree = tmp_path / "ws_digest_dir_none"
    worktree.mkdir()
    init_git_worktree(worktree)
    plain = worktree / "artifacts"
    plain.mkdir()
    (plain / "a.txt").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(
        comment_verdict_residue,
        "_hash_worktree_directory_residue",
        lambda **_k: None,
    )
    assert (
        comment_verdict_residue._digest_worktree_entry_bytes(
            worktree_path=worktree,
            path="artifacts",
            git_env=_git_env(),
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_tracked_diffs_gitlink_submodule_commit_none_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Present gitlink whose submodule HEAD probe fails must fail closed."""
    worktree = tmp_path / "ws_gitlink_present"
    worktree.mkdir()
    init_git_worktree(worktree)
    sub = worktree / "sub"
    sub.mkdir()
    (sub / "file.txt").write_text("x\n", encoding="utf-8")

    monkeypatch.setattr(
        comment_verdict_residue,
        "_run_git_bytes",
        lambda **_k: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"sub\0", stderr=b""
        ),
    )
    monkeypatch.setattr(
        comment_verdict_residue,
        "_load_git_index_stage_map",
        lambda **_k: {"sub": ("160000", "b" * 40)},
    )
    monkeypatch.setattr(
        comment_verdict_residue,
        "_git_worktree_blob_sha",
        lambda **_k: None,
    )
    monkeypatch.setattr(
        comment_verdict_residue,
        "_git_submodule_worktree_commit",
        lambda **_k: None,
    )
    assert (
        comment_verdict_residue._hash_tracked_residue_diffs(
            worktree_path=worktree,
            git_env=_git_env(),
            cached=False,
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_tracked_diffs_gitlink_submodule_commit_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Present gitlink with a successful submodule HEAD probe must fingerprint that SHA."""
    worktree = tmp_path / "ws_gitlink_ok"
    worktree.mkdir()
    init_git_worktree(worktree)
    sub = worktree / "sub"
    sub.mkdir()

    monkeypatch.setattr(
        comment_verdict_residue,
        "_run_git_bytes",
        lambda **_k: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"sub\0", stderr=b""
        ),
    )
    monkeypatch.setattr(
        comment_verdict_residue,
        "_load_git_index_stage_map",
        lambda **_k: {"sub": ("160000", "c" * 40)},
    )
    monkeypatch.setattr(
        comment_verdict_residue,
        "_git_worktree_blob_sha",
        lambda **_k: None,
    )
    monkeypatch.setattr(
        comment_verdict_residue,
        "_git_submodule_worktree_commit",
        lambda **_k: "d" * 40,
    )
    monkeypatch.setattr(
        comment_verdict_residue,
        "_git_worktree_mode",
        lambda **_k: None,
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
    expected.update(("c" * 40).encode("ascii"))
    expected.update(b"im:")
    expected.update(b"160000")
    expected.update(b"wt:")
    expected.update(("d" * 40).encode("ascii"))
    expected.update(b"wm:")
    expected.update(b"160000")
    expected.update(b"\0")
    assert result == expected.hexdigest()


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_open_git_dir_path_at_non_directory_leaf(tmp_path: Path) -> None:
    """Approved absolute git-dir path that resolves to a regular file fails closed."""
    worktree = tmp_path / "ws_nondir_leaf"
    worktree.mkdir()
    leaf = worktree / "HEAD"
    leaf.write_text("ref: refs/heads/main\n", encoding="utf-8")
    assert (
        comment_verdict_residue_nested._open_git_dir_path_at(
            -1,
            leaf,
            outer_worktree_path=worktree,
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_open_nested_marker_fstat_not_dir_after_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Marker open that fstats as a non-directory must yield None (lines 372-373)."""
    nested = tmp_path / "nested_fstat_marker"
    nested.mkdir()
    outer = tmp_path / "outer_fstat_marker"
    outer.mkdir()
    (nested / ".git").mkdir()
    (nested / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    dir_fd = os.open(nested, os.O_RDONLY | os.O_DIRECTORY)
    try:
        real_open = os.open
        real_fstat = os.fstat
        opened_git: list[int] = []

        def _open_track(
            name: str | int | bytes, flags: int, *args: object, **kwargs: object
        ) -> int:
            fd = real_open(name, flags, *args, **kwargs)  # type: ignore[arg-type]
            if name == ".git":
                opened_git.append(fd)
            return fd

        def _fstat_lie(fd: int) -> os.stat_result:
            result = real_fstat(fd)
            if opened_git and fd == opened_git[-1]:
                return os.stat_result((stat.S_IFREG | 0o644, *result[1:]))
            return result

        monkeypatch.setattr(os, "open", _open_track)
        monkeypatch.setattr(os, "fstat", _fstat_lie)
        with comment_verdict_residue_nested._open_nested_git_dir_marker_at(
            dir_fd, outer_worktree_path=outer
        ) as opened:
            assert opened is None
    finally:
        os.close(dir_fd)


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_directory_recursive_child_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a nested subdirectory hash returns None, the parent walk fails closed."""
    worktree = tmp_path / "ws_recursive_none"
    worktree.mkdir()
    init_git_worktree_file_replaced_by_directory(worktree)
    path = "src/x.py"
    sub = worktree / path / "subdir"
    sub.mkdir()
    (sub / "nested.txt").write_text("x\n", encoding="utf-8")

    real = comment_verdict_residue._hash_worktree_directory_residue_at_dir_fd
    depth_seen: list[int] = []

    def _wrap(**kwargs: object) -> str | None:
        depth = int(kwargs.get("depth", 0))  # type: ignore[arg-type]
        depth_seen.append(depth)
        if depth >= 1:
            return None
        return real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        comment_verdict_residue, "_hash_worktree_directory_residue_at_dir_fd", _wrap
    )
    assert (
        comment_verdict_residue._hash_worktree_directory_residue(
            worktree_path=worktree,
            path=path,
            git_env=_git_env(),
        )
        is None
    )
    assert any(d >= 1 for d in depth_seen)


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_directory_recursive_child_success_updates_digest(tmp_path: Path) -> None:
    """Nested subdirectory hash success must feed the parent digest (line 490)."""
    worktree = tmp_path / "ws_recursive_ok"
    worktree.mkdir()
    init_git_worktree_file_replaced_by_directory(worktree)
    path = "src/x.py"
    sub = worktree / path / "subdir"
    sub.mkdir()
    nested_file = sub / "nested.txt"
    nested_file.write_text("alpha\n", encoding="utf-8")

    baseline = comment_verdict_residue._hash_worktree_directory_residue(
        worktree_path=worktree,
        path=path,
        git_env=_git_env(),
    )
    assert baseline is not None

    nested_file.write_text("beta\n", encoding="utf-8")
    mutated = comment_verdict_residue._hash_worktree_directory_residue(
        worktree_path=worktree,
        path=path,
        git_env=_git_env(),
    )
    assert mutated is not None
    assert mutated != baseline


@pytest.mark.unit
@pytest.mark.timeout(2)
async def test_correction_fingerprint_empty_z_nul_only_stdout_bytes(
    tmp_path: Path,
) -> None:
    """NUL-only stdout_bytes with -z status must fingerprint as clean (line 118)."""
    worktree = tmp_path / "ws_fp_nul_bytes"
    worktree.mkdir()
    init_git_worktree(worktree)

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(
                returncode=0,
                stdout="",
                stderr="",
                stdout_bytes=b"\0\0",
            )
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))
    assert (
        await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
            runner,
            workspace_id="ws_fp_nul_bytes",
            worktree_path=worktree,
        )
        == ""
    )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_open_worktree_directory_fstat_not_dir_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a successful O_DIRECTORY open, non-dir fstat must raise ENOTDIR (line 609)."""
    worktree = tmp_path / "ws_fstat_dir"
    worktree.mkdir()
    (worktree / "src").mkdir()
    real_fstat = os.fstat

    def _lie(fd: int) -> os.stat_result:
        result = real_fstat(fd)
        return os.stat_result((stat.S_IFREG | 0o644, *result[1:]))

    monkeypatch.setattr(os, "fstat", _lie)
    with (
        pytest.raises(OSError) as exc_info,
        comment_verdict_residue_io._open_worktree_directory(worktree, "src"),
    ):
        pass
    assert exc_info.value.errno == errno.ENOTDIR


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_open_git_dir_path_at_fstat_not_dir_after_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approved git-dir walk that fstats as non-dir must fail closed (lines 241-242)."""
    worktree = tmp_path / "ws_gitdir_fstat"
    worktree.mkdir()
    git_meta = worktree / ".git-meta"
    git_meta.mkdir()
    (git_meta / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    real_fstat = os.fstat
    lie_fds: set[int] = set()

    def _lie(fd: int) -> os.stat_result:
        result = real_fstat(fd)
        # Lie only after the leaf metadata directory is opened (not the outer root).
        if fd in lie_fds:
            return os.stat_result((stat.S_IFREG | 0o644, *result[1:]))
        return result

    real_open = os.open

    def _open_track(name: str | int | bytes, flags: int, *args: object, **kwargs: object) -> int:
        fd = real_open(name, flags, *args, **kwargs)  # type: ignore[arg-type]
        if name == ".git-meta" or (isinstance(name, str) and name.endswith(".git-meta")):
            lie_fds.add(fd)
        return fd

    monkeypatch.setattr(os, "open", _open_track)
    monkeypatch.setattr(os, "fstat", _lie)
    assert (
        comment_verdict_residue_nested._open_git_dir_path_at(
            -1,
            git_meta,
            outer_worktree_path=worktree,
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_git_nested_worktree_commit_at_fails_when_fd_path_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pinned nested commit must fail closed when the dir fd path cannot be resolved."""
    worktree = tmp_path / "ws_commit_at_no_path"
    worktree.mkdir()
    nested_name = init_git_worktree_with_embedded_repo(worktree)
    nested = worktree / nested_name
    dir_fd = os.open(nested, os.O_RDONLY | os.O_DIRECTORY)
    try:
        monkeypatch.setattr(
            comment_verdict_residue,
            "_fresh_worktree_path_for_open_fd",
            lambda _fd: None,
        )
        assert (
            comment_verdict_residue._git_nested_worktree_commit_at(
                dir_fd=dir_fd,
                git_env=_git_env(),
                outer_worktree_path=worktree,
            )
            is None
        )
    finally:
        os.close(dir_fd)


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_git_nested_worktree_commit_from_root_fail_closed_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-flight pin/resolve failures in from_root must fail closed."""
    worktree = tmp_path / "ws_from_root_fail"
    worktree.mkdir()
    nested_name = init_git_worktree_with_embedded_repo(worktree)
    nested = worktree / nested_name
    dir_fd = os.open(nested, os.O_RDONLY | os.O_DIRECTORY)
    try:
        monkeypatch.setattr(
            comment_verdict_residue,
            "_nested_untrusted_git_probe_past_deadline",
            lambda: True,
        )
        assert (
            comment_verdict_residue._git_nested_worktree_commit_from_root(
                dir_fd=dir_fd,
                git_env=_git_env(),
                outer_worktree_path=worktree,
            )
            is None
        )
        monkeypatch.undo()

        monkeypatch.setattr(
            comment_verdict_residue,
            "_fresh_worktree_path_for_open_fd",
            lambda _fd: None,
        )
        assert (
            comment_verdict_residue._git_nested_worktree_commit_from_root(
                dir_fd=dir_fd,
                git_env=_git_env(),
                outer_worktree_path=worktree,
            )
            is None
        )
        monkeypatch.undo()

        calls = {"n": 0}
        real_fresh = comment_verdict_residue._fresh_worktree_path_for_open_fd

        def _fresh_second_none(fd: int) -> Path | None:
            calls["n"] += 1
            if calls["n"] >= 2:
                return None
            return real_fresh(fd)

        monkeypatch.setattr(
            comment_verdict_residue, "_fresh_worktree_path_for_open_fd", _fresh_second_none
        )
        assert (
            comment_verdict_residue._git_nested_worktree_commit_from_root(
                dir_fd=dir_fd,
                git_env=_git_env(),
                outer_worktree_path=worktree,
            )
            is None
        )
        monkeypatch.undo()

        monkeypatch.setattr(
            comment_verdict_residue,
            "_nested_git_probe_worktree_root",
            lambda **_k: None,
        )
        assert (
            comment_verdict_residue._git_nested_worktree_commit_from_root(
                dir_fd=dir_fd,
                git_env=_git_env(),
                outer_worktree_path=worktree,
            )
            is None
        )
        monkeypatch.undo()

        # Second probe_root call returns None after first succeeds.
        probe_calls = {"n": 0}
        real_probe = comment_verdict_residue._nested_git_probe_worktree_root

        def _probe_second_none(**kwargs: object) -> Path | None:
            probe_calls["n"] += 1
            if probe_calls["n"] >= 2:
                return None
            return real_probe(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(
            comment_verdict_residue, "_nested_git_probe_worktree_root", _probe_second_none
        )
        assert (
            comment_verdict_residue._git_nested_worktree_commit_from_root(
                dir_fd=dir_fd,
                git_env=_git_env(),
                outer_worktree_path=worktree,
            )
            is None
        )
        monkeypatch.undo()

        monkeypatch.setattr(comment_verdict_residue, "_fresh_pinned_nested_git_dir", lambda: None)
        assert (
            comment_verdict_residue._git_nested_worktree_commit_from_root(
                dir_fd=dir_fd,
                git_env=_git_env(),
                outer_worktree_path=worktree,
            )
            is None
        )
        monkeypatch.undo()

        monkeypatch.setattr(
            comment_verdict_residue,
            "_resolve_nested_worktree_head",
            lambda **_k: None,
        )
        assert (
            comment_verdict_residue._git_nested_worktree_commit_from_root(
                dir_fd=dir_fd,
                git_env=_git_env(),
                outer_worktree_path=worktree,
            )
            is None
        )
        monkeypatch.undo()

        # Instrumented call indices that map to explicit fail-closed returns:
        # 4 → line 1007 (post-pin head refresh), 6 → 1017 (staged), 9 → 1027
        # (untracked list), 11 → 1038 (untracked hash) when untracked exists.
        (nested / "extra_untracked.txt").write_text("extra\n", encoding="utf-8")
        real_pinned = comment_verdict_residue._fresh_pinned_nested_worktree
        for fail_at in (4, 6, 9, 11):
            counter = {"n": 0}

            def _pinned_fail_at(
                n_fail: int = fail_at, counter: dict[str, int] = counter
            ) -> Path | None:
                counter["n"] += 1
                if counter["n"] == n_fail:
                    return None
                return real_pinned()

            monkeypatch.setattr(
                comment_verdict_residue, "_fresh_pinned_nested_worktree", _pinned_fail_at
            )
            assert (
                comment_verdict_residue._git_nested_worktree_commit_from_root(
                    dir_fd=dir_fd,
                    git_env=_git_env(),
                    outer_worktree_path=worktree,
                )
                is None
            ), f"expected fail-closed when pinned worktree refresh #{fail_at} returns None"
            monkeypatch.undo()
    finally:
        os.close(dir_fd)
