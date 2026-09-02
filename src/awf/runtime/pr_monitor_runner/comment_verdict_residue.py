"""Correction-attempt residue fingerprint helpers for verdict protocol retries."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import os
import stat
import subprocess
import time
from collections.abc import Iterator, Mapping
from contextvars import ContextVar, Token
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from awf.common.logging import get_logger
from awf.node.git_manager import git_env_for_untrusted_nested_repository_probe
from awf.runtime.pr_monitor_runner.git_utils import (
    git_untrusted_nested_pinned_worktree_command,
    git_untrusted_nested_worktree_command,
    git_worktree_command,
)
from awf.runtime.pr_monitor_runner.path_helpers import _changed_paths_from_name_only_z
from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError

if TYPE_CHECKING:
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner

_log = get_logger(__name__)

_SPECIAL_ENTRY_KINDS = frozenset({"fifo", "socket", "char", "block", "other"})
_WORKTREE_REGULAR_OPEN_FLAGS = (
    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
)
_WORKTREE_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_WORKTREE_REGULAR_TEXT_READ_LIMIT_BYTES = 4096
_NESTED_UNTRUSTED_GIT_PROBE: ContextVar[bool] = ContextVar(
    "_nested_untrusted_git_probe",
    default=False,
)
_NESTED_FINGERPRINT_SCAN_ACTIVE: ContextVar[int] = ContextVar(
    "_nested_fingerprint_scan_active",
    default=0,
)
_NESTED_UNTRUSTED_GIT_PROBE_DEADLINE: ContextVar[float | None] = ContextVar(
    "_nested_untrusted_git_probe_deadline",
    default=None,
)
_NESTED_UNTRUSTED_GIT_PROBE_GIT_DIR: ContextVar[Path | None] = ContextVar(
    "_nested_untrusted_git_probe_git_dir",
    default=None,
)
_NESTED_UNTRUSTED_GIT_PROBE_WORKTREE: ContextVar[Path | None] = ContextVar(
    "_nested_untrusted_git_probe_worktree",
    default=None,
)
_NESTED_UNTRUSTED_GIT_PROBE_TIMEOUT_SECONDS = 30.0
_NESTED_UNTRUSTED_GIT_PROBE_SCAN_BUDGET_SECONDS = 30.0


def _nested_untrusted_git_probe_remaining_seconds() -> float | None:
    deadline = _NESTED_UNTRUSTED_GIT_PROBE_DEADLINE.get()
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def _nested_untrusted_git_probe_past_deadline() -> bool:
    remaining = _nested_untrusted_git_probe_remaining_seconds()
    return remaining is not None and remaining <= 0.0


def _nested_untrusted_git_probe_command_timeout() -> float | None:
    if not _NESTED_UNTRUSTED_GIT_PROBE.get():
        return None
    remaining = _nested_untrusted_git_probe_remaining_seconds()
    if remaining is not None:
        if remaining <= 0.0:
            return 0.0
        return min(_NESTED_UNTRUSTED_GIT_PROBE_TIMEOUT_SECONDS, remaining)
    return _NESTED_UNTRUSTED_GIT_PROBE_TIMEOUT_SECONDS


@contextlib.contextmanager
def _residue_fingerprint_nested_scan_budget() -> Iterator[None]:
    """Bound aggregate nested embedded-repo probing for one fingerprint read."""
    token: Token[int] = _NESTED_FINGERPRINT_SCAN_ACTIVE.set(
        _NESTED_FINGERPRINT_SCAN_ACTIVE.get() + 1
    )
    try:
        yield
    finally:
        was_outermost = _NESTED_FINGERPRINT_SCAN_ACTIVE.get() == 1
        _NESTED_FINGERPRINT_SCAN_ACTIVE.reset(token)
        if was_outermost:
            _NESTED_UNTRUSTED_GIT_PROBE_DEADLINE.set(None)


@contextlib.contextmanager
def _untrusted_nested_git_probe() -> Iterator[None]:
    """Scope nested embedded-repo Git probes to sanitized config and bounded runtime."""
    token: Token[bool] = _NESTED_UNTRUSTED_GIT_PROBE.set(True)
    if (
        _NESTED_FINGERPRINT_SCAN_ACTIVE.get() > 0
        and _NESTED_UNTRUSTED_GIT_PROBE_DEADLINE.get() is None
    ):
        _NESTED_UNTRUSTED_GIT_PROBE_DEADLINE.set(
            time.monotonic() + _NESTED_UNTRUSTED_GIT_PROBE_SCAN_BUDGET_SECONDS
        )
    try:
        yield
    finally:
        _NESTED_UNTRUSTED_GIT_PROBE.reset(token)


def _git_command_for_residue_probe(worktree_path: Path, *args: str) -> list[str]:
    pinned_git_dir = _NESTED_UNTRUSTED_GIT_PROBE_GIT_DIR.get()
    pinned_worktree = _NESTED_UNTRUSTED_GIT_PROBE_WORKTREE.get()
    if pinned_git_dir is not None and pinned_worktree is not None:
        return git_untrusted_nested_pinned_worktree_command(
            pinned_git_dir,
            pinned_worktree,
            *args,
        )
    if _NESTED_UNTRUSTED_GIT_PROBE.get():
        return git_untrusted_nested_worktree_command(worktree_path, *args)
    return git_worktree_command(worktree_path, *args)


@contextlib.contextmanager
def _pinned_nested_git_probe(git_dir: Path, worktree_path: Path) -> Iterator[None]:
    """Pin nested embedded-repo probes to a specific git-dir and work-tree."""
    git_dir_token: Token[Path | None] = _NESTED_UNTRUSTED_GIT_PROBE_GIT_DIR.set(git_dir)
    worktree_token: Token[Path | None] = _NESTED_UNTRUSTED_GIT_PROBE_WORKTREE.set(worktree_path)
    try:
        yield
    finally:
        _NESTED_UNTRUSTED_GIT_PROBE_GIT_DIR.reset(git_dir_token)
        _NESTED_UNTRUSTED_GIT_PROBE_WORKTREE.reset(worktree_token)


@contextlib.contextmanager
def _without_nested_git_probe_pin() -> Iterator[None]:
    """Clear nested git-dir/work-tree pins so inner-repo discovery is not mis-scoped."""
    git_dir_token: Token[Path | None] = _NESTED_UNTRUSTED_GIT_PROBE_GIT_DIR.set(None)
    worktree_token: Token[Path | None] = _NESTED_UNTRUSTED_GIT_PROBE_WORKTREE.set(None)
    try:
        yield
    finally:
        _NESTED_UNTRUSTED_GIT_PROBE_GIT_DIR.reset(git_dir_token)
        _NESTED_UNTRUSTED_GIT_PROBE_WORKTREE.reset(worktree_token)


@contextlib.contextmanager
def _open_worktree_regular_file(candidate: Path) -> Iterator[BinaryIO]:
    """Open a worktree regular file for byte reads without blocking on TOCTOU swaps.

    ``lstat`` may classify a path as regular moments before another worktree
    process replaces it with a FIFO; pathname-based ``open("rb")`` would then
    block until a writer connects. Open with ``O_NONBLOCK`` and re-validate the
    opened inode via ``fstat`` so swapped special files fail closed instead.
    """
    fd = os.open(candidate, _WORKTREE_REGULAR_OPEN_FLAGS)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EBADF, "worktree path is not a regular file after open")
    except OSError:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as fh:
        yield fh


@contextlib.contextmanager
def _open_worktree_regular_file_at(dir_fd: int, name: str) -> Iterator[BinaryIO]:
    """Open a directory-relative regular file without pathname re-entry."""
    fd = os.open(name, _WORKTREE_REGULAR_OPEN_FLAGS, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EBADF, "worktree entry is not a regular file after open")
    except OSError:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as fh:
        yield fh


def _worktree_path_for_open_fd(dir_fd: int) -> Path | None:
    """Resolve the pathname of an opened worktree directory fd."""
    try:
        return Path(f"/proc/self/fd/{dir_fd}").readlink()
    except OSError:
        return None


def _read_worktree_regular_text(
    candidate: Path,
    *,
    max_bytes: int = _WORKTREE_REGULAR_TEXT_READ_LIMIT_BYTES,
) -> str | None:
    """Read bounded UTF-8 text from a worktree regular file without TOCTOU blocking."""
    try:
        with _open_worktree_regular_file(candidate) as fh:
            payload = fh.read(max_bytes + 1)
    except OSError:
        return None
    if len(payload) > max_bytes:
        return None
    return payload.decode("utf-8", errors="surrogateescape").strip()


def _read_worktree_regular_text_at(
    dir_fd: int,
    name: str,
    *,
    max_bytes: int = _WORKTREE_REGULAR_TEXT_READ_LIMIT_BYTES,
) -> str | None:
    """Read bounded UTF-8 text from a directory-relative regular file."""
    try:
        with _open_worktree_regular_file_at(dir_fd, name) as fh:
            payload = fh.read(max_bytes + 1)
    except OSError:
        return None
    if len(payload) > max_bytes:
        return None
    return payload.decode("utf-8", errors="surrogateescape").strip()


def _worktree_mode_from_kind(*, kind: str, st_mode: int) -> str | None:
    if kind == "symlink":
        return "120000"
    if kind == "regular":
        if stat.S_IMODE(st_mode) & stat.S_IXUSR:
            return "100755"
        return "100644"
    if kind == "directory":
        return "040000"
    if kind in _SPECIAL_ENTRY_KINDS:
        return kind
    return None


def _worktree_entry_kind_from_mode(file_mode: int) -> tuple[str, int]:
    if stat.S_ISLNK(file_mode):
        return "symlink", file_mode
    if stat.S_ISREG(file_mode):
        return "regular", file_mode
    if stat.S_ISDIR(file_mode):
        return "directory", file_mode
    if stat.S_ISFIFO(file_mode):
        return "fifo", file_mode
    if stat.S_ISSOCK(file_mode):
        return "socket", file_mode
    if stat.S_ISCHR(file_mode):
        return "char", file_mode
    if stat.S_ISBLK(file_mode):
        return "block", file_mode
    return "other", file_mode


def _worktree_entry_kind(candidate: Path) -> tuple[str, int] | None:
    """Classify a worktree path via ``lstat`` without opening or following it."""
    try:
        file_mode = candidate.lstat().st_mode
    except OSError:
        return None
    return _worktree_entry_kind_from_mode(file_mode)


def _worktree_entry_kind_at(dir_fd: int, name: str) -> tuple[str, int] | None:
    """Classify a directory entry via ``lstat`` relative to ``dir_fd`` without following it."""
    try:
        file_mode = os.lstat(name, dir_fd=dir_fd).st_mode
    except OSError:
        return None
    return _worktree_entry_kind_from_mode(file_mode)


def _has_nested_git_marker_at(dir_fd: int) -> bool:
    """True when a directory fd contains a real ``.git`` file or directory entry."""
    try:
        marker_mode = os.lstat(".git", dir_fd=dir_fd).st_mode
    except OSError:
        return False
    if stat.S_ISLNK(marker_mode):
        return False
    return stat.S_ISREG(marker_mode) or stat.S_ISDIR(marker_mode)


def _has_nested_git_marker(directory: Path) -> bool:
    """True when ``directory`` contains a real ``.git`` file or directory entry."""
    git_marker = directory / ".git"
    try:
        marker_mode = git_marker.lstat().st_mode
    except OSError:
        return False
    if stat.S_ISLNK(marker_mode):
        return False
    return stat.S_ISREG(marker_mode) or stat.S_ISDIR(marker_mode)


@contextlib.contextmanager
def _open_worktree_directory(worktree_path: Path, path: str) -> Iterator[int]:
    """Open a worktree directory for no-follow enumeration without TOCTOU symlink swaps.

    ``lstat`` may classify a path as a directory moments before another worktree
    process replaces it with a symlink; pathname-based ``os.scandir(candidate)``
    would then follow the swapped target. Descend with ``O_NOFOLLOW`` and
    re-validate the opened inode via ``fstat`` so symlink swaps fail closed.
    """
    rel_parts = Path(path).parts
    if not rel_parts:
        raise OSError(errno.EINVAL, "worktree path is the directory root", path)
    dir_fd = os.open(worktree_path, _WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        for part in rel_parts:
            if part in {".", ".."}:
                raise OSError(errno.EINVAL, "unsafe worktree directory path component", part)
            child_fd = os.open(part, _WORKTREE_DIRECTORY_OPEN_FLAGS, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = child_fd
        if not stat.S_ISDIR(os.fstat(dir_fd).st_mode):
            raise OSError(errno.ENOTDIR, "worktree path is not a directory after open")
    except OSError:
        os.close(dir_fd)
        raise
    try:
        yield dir_fd
    finally:
        os.close(dir_fd)


def _special_entry_blob_sha(*, kind: str, st_mode: int) -> str:
    hasher = hashlib.sha256()
    hasher.update(kind.encode("ascii"))
    hasher.update(b":")
    hasher.update(oct(stat.S_IMODE(st_mode)).encode("ascii"))
    return hasher.hexdigest()


def _decode_porcelain_status_stdout(
    *,
    stdout: str,
    stdout_bytes: bytes | None,
) -> tuple[str, bool]:
    """Return decoded porcelain and whether NUL-delimited ``-z`` records are present."""
    if stdout_bytes is not None:
        return stdout_bytes.decode("utf-8", errors="surrogateescape"), True
    if "\0" in stdout:
        return stdout, True
    return stdout, False


def _format_porcelain_z_line(status: str, path: str, original_path: str | None) -> str:
    if original_path:
        return f"{status} {original_path} -> {path}"
    return f"{status} {path}"


def _digest_worktree_entry_bytes(
    *,
    worktree_path: Path,
    path: str,
    git_env: Mapping[str, str],
) -> bytes | None:
    candidate = worktree_path / path
    kind_info = _worktree_entry_kind(candidate)
    if kind_info is None:
        return None
    kind, st_mode = kind_info
    hasher = hashlib.sha256()

    if kind == "symlink":
        try:
            link_text = str(candidate.readlink()).encode("utf-8", errors="surrogateescape")
        except OSError:
            return None
        hasher.update(b"symlink:")
        worktree_mode = _git_worktree_mode(worktree_path=worktree_path, path=path)
        hasher.update(b"mode:")
        hasher.update((worktree_mode or "<missing>").encode("ascii"))
        hasher.update(b"\0")
        hasher.update(link_text)
    elif kind == "regular":
        hasher.update(b"regular:")
        worktree_mode = _git_worktree_mode(worktree_path=worktree_path, path=path)
        hasher.update(b"mode:")
        hasher.update((worktree_mode or "<missing>").encode("ascii"))
        hasher.update(b"\0")
        try:
            with _open_worktree_regular_file(candidate) as fh:
                while chunk := fh.read(65536):
                    hasher.update(chunk)
        except OSError:
            return None
    elif kind == "directory":
        if _has_nested_git_marker(candidate):
            nested = _git_nested_worktree_commit(
                worktree_path=worktree_path,
                path=path,
                git_env=git_env,
            )
            if nested is None:
                return None
            hasher.update(b"nested-git:")
            hasher.update(nested.encode("ascii"))
        else:
            directory_fp = _hash_worktree_directory_residue(
                worktree_path=worktree_path,
                path=path,
                git_env=git_env,
            )
            if directory_fp is None:
                return None
            hasher.update(b"directory:")
            hasher.update(directory_fp.encode("ascii"))
    elif kind in _SPECIAL_ENTRY_KINDS:
        hasher.update(kind.encode("ascii"))
        hasher.update(b":")
        hasher.update(oct(stat.S_IMODE(st_mode)).encode("ascii"))
    else:
        return None
    return hasher.digest()


def _digest_worktree_entry_bytes_at(
    *,
    dir_fd: int,
    entry_name: str,
    path: str,
    worktree_path: Path,
) -> bytes | None:
    """Digest one directory entry without pathname re-entry through parent components."""
    kind_info = _worktree_entry_kind_at(dir_fd, entry_name)
    if kind_info is None:
        return None
    kind, st_mode = kind_info
    hasher = hashlib.sha256()

    if kind == "symlink":
        try:
            link_text = os.readlink(entry_name, dir_fd=dir_fd).encode(
                "utf-8", errors="surrogateescape"
            )
        except OSError:
            return None
        hasher.update(b"symlink:")
        worktree_mode = _worktree_mode_from_kind(kind=kind, st_mode=st_mode)
        if worktree_mode is None:
            worktree_mode = _git_worktree_mode(worktree_path=worktree_path, path=path)
        hasher.update(b"mode:")
        hasher.update((worktree_mode or "<missing>").encode("ascii"))
        hasher.update(b"\0")
        hasher.update(link_text)
    elif kind == "regular":
        hasher.update(b"regular:")
        worktree_mode = _worktree_mode_from_kind(kind=kind, st_mode=st_mode)
        if worktree_mode is None:
            worktree_mode = _git_worktree_mode(worktree_path=worktree_path, path=path)
        hasher.update(b"mode:")
        hasher.update((worktree_mode or "<missing>").encode("ascii"))
        hasher.update(b"\0")
        try:
            with _open_worktree_regular_file_at(dir_fd, entry_name) as fh:
                while chunk := fh.read(65536):
                    hasher.update(chunk)
        except OSError:
            return None
    elif kind in _SPECIAL_ENTRY_KINDS:
        hasher.update(kind.encode("ascii"))
        hasher.update(b":")
        hasher.update(oct(stat.S_IMODE(st_mode)).encode("ascii"))
    else:
        return None
    return hasher.digest()


def _sorted_worktree_directory_entry_names(dir_fd: int) -> list[str]:
    """Return sorted entry names for an already-opened worktree directory fd.

    Enumeration is pinned to the opened inode via ``/proc/self/fd/<fd>`` because
    some platforms expose ``openat``/``lstat`` ``dir_fd`` support without a
    ``scandir(dir_fd=...)`` wrapper.
    """
    proc_path = f"/proc/self/fd/{dir_fd}"
    try:
        return sorted(
            entry.name for entry in Path(proc_path).iterdir() if entry.name not in {".", ".."}
        )
    except OSError as exc:
        raise OSError(
            exc.errno,
            f"cannot enumerate opened worktree directory fd: {proc_path}",
        ) from exc


def _hash_worktree_directory_residue_at_dir_fd(
    *,
    worktree_path: Path,
    path: str,
    dir_fd: int,
    git_env: Mapping[str, str],
) -> str | None:
    hasher = hashlib.sha256()
    try:
        entry_names = _sorted_worktree_directory_entry_names(dir_fd)
    except OSError:
        return None

    for entry_name in entry_names:
        child_path = f"{path}/{entry_name}"
        hasher.update(entry_name.encode("utf-8", errors="surrogateescape"))
        hasher.update(b"\0")
        child_kind = _worktree_entry_kind_at(dir_fd, entry_name)
        if child_kind is None:
            return None
        child_kind_name, child_mode = child_kind
        hasher.update(child_kind_name.encode("ascii"))
        hasher.update(b":")
        hasher.update(oct(stat.S_IMODE(child_mode)).encode("ascii"))
        hasher.update(b"\0")
        if child_kind_name == "directory":
            try:
                child_fd = os.open(entry_name, _WORKTREE_DIRECTORY_OPEN_FLAGS, dir_fd=dir_fd)
            except OSError:
                return None
            try:
                if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                    return None
                if _has_nested_git_marker_at(child_fd):
                    nested = _git_nested_worktree_commit_at(
                        dir_fd=child_fd,
                        git_env=git_env,
                    )
                    if nested is None:
                        return None
                    hasher.update(nested.encode("ascii"))
                else:
                    nested_dir = _hash_worktree_directory_residue_at_dir_fd(
                        worktree_path=worktree_path,
                        path=child_path,
                        dir_fd=child_fd,
                        git_env=git_env,
                    )
                    if nested_dir is None:
                        return None
                    hasher.update(nested_dir.encode("ascii"))
            finally:
                os.close(child_fd)
        else:
            child_digest = _digest_worktree_entry_bytes_at(
                dir_fd=dir_fd,
                entry_name=entry_name,
                path=child_path,
                worktree_path=worktree_path,
            )
            if child_digest is None:
                return None
            hasher.update(child_digest)
        hasher.update(b"\0")
    return hasher.hexdigest()


def _hash_worktree_directory_residue(
    *,
    worktree_path: Path,
    path: str,
    git_env: Mapping[str, str],
) -> str | None:
    candidate = worktree_path / path
    kind_info = _worktree_entry_kind(candidate)
    if kind_info is None or kind_info[0] != "directory":
        return None

    try:
        with _open_worktree_directory(worktree_path, path) as dir_fd:
            return _hash_worktree_directory_residue_at_dir_fd(
                worktree_path=worktree_path,
                path=path,
                dir_fd=dir_fd,
                git_env=git_env,
            )
    except OSError:
        return None


def _hash_untracked_residue_paths(
    *,
    worktree_path: Path,
    paths: list[str],
    untracked: set[str],
    git_env: Mapping[str, str] | None = None,
) -> str | None:
    """Sync content identity for untracked PR-worthy paths.

    Intended for ``asyncio.to_thread`` so multi-gigabyte non-ignored artifacts
    do not block the monitor event loop (PRRT_kwDOSJAM6s6eLMRD). Symlinks are
    fingerprinted via link text only — never followed (PRRT_kwDOSJAM6s6eK9AB).
    """
    untracked_hasher = hashlib.sha256()
    env = dict(git_env or {})
    for path in paths:
        if path not in untracked:
            continue
        # Hash each file independently so raw bytes cannot shift across \0 path
        # delimiters (PRRT_kwDOSJAM6s6eRK93).
        file_hasher = hashlib.sha256()
        file_hasher.update(path.encode("utf-8", errors="surrogateescape"))
        file_hasher.update(b"\0")
        candidate = worktree_path / path
        try:
            kind_info = _worktree_entry_kind(candidate)
            if kind_info is None:
                try:
                    candidate.lstat()
                except OSError as exc:
                    if exc.errno == errno.ENOENT:
                        file_hasher.update(b"<missing>")
                    else:
                        return None
                else:
                    return None
            else:
                entry_digest = _digest_worktree_entry_bytes(
                    worktree_path=worktree_path,
                    path=path,
                    git_env=env,
                )
                if entry_digest is None:
                    return None
                file_hasher.update(entry_digest)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                file_hasher.update(b"<missing>")
            else:
                # Unreadable residue (e.g. mode 000) must fail closed: hashing a
                # shared <missing> marker collides across different contents when
                # the commit sink also cannot stage the file (PRRT_kwDOSJAM6s6eN7wf).
                return None
        untracked_hasher.update(file_hasher.digest())
    return untracked_hasher.hexdigest()


def _nested_untrusted_git_probe_timed_out_result(
    command: list[str],
    *,
    stderr: bytes,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=command,
        returncode=124,
        stdout=b"",
        stderr=stderr,
    )


def _run_git_bytes(
    *,
    worktree_path: Path,
    git_env: Mapping[str, str],
    args: tuple[str, ...],
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    command = _git_command_for_residue_probe(worktree_path, *args)
    env = dict(git_env)
    timeout = _nested_untrusted_git_probe_command_timeout()
    if timeout == 0.0:
        return _nested_untrusted_git_probe_timed_out_result(
            command,
            stderr=b"nested untrusted git probe scan budget exceeded",
        )
    try:
        return subprocess.run(
            command,
            env=env,
            capture_output=True,
            check=False,
            input=stdin,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _nested_untrusted_git_probe_timed_out_result(
            command,
            stderr=b"nested untrusted git probe timed out",
        )


def _git_index_blob_sha(
    *,
    worktree_path: Path,
    path: str,
    git_env: Mapping[str, str],
) -> str | None:
    result = _run_git_bytes(
        worktree_path=worktree_path,
        git_env=git_env,
        # ``:{path}`` is ambiguous when ``path`` begins with ``0:``–``3:`` (Git's
        # ``:<stage>:<path>`` index syntax); ``:0:./`` disambiguates (PRRT_kwDOSJAM6s6eQcs6).
        args=("rev-parse", "-q", "--verify", f":0:./{path}"),
    )
    if result.returncode != 0:
        return None
    return result.stdout.decode("ascii", errors="replace").strip() or None


def _git_worktree_blob_sha(
    *,
    worktree_path: Path,
    path: str,
    git_env: Mapping[str, str],
    index_mode: str | None = None,
) -> str | None:
    candidate = worktree_path / path
    kind_info = _worktree_entry_kind(candidate)
    if kind_info is None:
        return None
    kind, st_mode = kind_info

    if kind == "symlink":
        try:
            blob_bytes = str(candidate.readlink()).encode("utf-8", errors="surrogateescape")
        except OSError:
            return None
        result = _run_git_bytes(
            worktree_path=worktree_path,
            git_env=git_env,
            # ``hash-object --path`` invokes path clean filters and can block or hang
            # (PRRT_kwDOSJAM6s6eSHjC); hash raw worktree bytes via stdin instead.
            args=("hash-object", "--stdin"),
            stdin=blob_bytes,
        )
    elif kind == "regular":
        try:
            with _open_worktree_regular_file(candidate) as fh:
                # Stream worktree bytes into ``hash-object --stdin`` so multi-gigabyte
                # tracked edits do not materialize in the control-plane process
                # (PRRT_kwDOSJAM6s6eSPQL).
                hash_timeout = _nested_untrusted_git_probe_command_timeout()
                if hash_timeout == 0.0:
                    return None
                result = subprocess.run(
                    _git_command_for_residue_probe(worktree_path, "hash-object", "--stdin"),
                    env=dict(git_env),
                    capture_output=True,
                    check=False,
                    stdin=fh,
                    timeout=hash_timeout,
                )
        except (OSError, subprocess.TimeoutExpired):
            return None
    elif kind == "directory":
        if index_mode == "160000" or _has_nested_git_marker(candidate):
            return _git_nested_worktree_commit(
                worktree_path=worktree_path,
                path=path,
                git_env=git_env,
            )
        return _hash_worktree_directory_residue(
            worktree_path=worktree_path,
            path=path,
            git_env=git_env,
        )
    elif kind in _SPECIAL_ENTRY_KINDS:
        return _special_entry_blob_sha(kind=kind, st_mode=st_mode)
    else:
        return None

    if result.returncode != 0:
        return None
    return result.stdout.decode("ascii", errors="replace").strip() or None


def _nested_git_probe_git_dir(nested_root: Path) -> Path | None:
    """Return the Git metadata directory for a nested embedded repository."""
    git_marker = nested_root / ".git"
    try:
        marker_mode = git_marker.lstat().st_mode
    except OSError:
        return None
    if stat.S_ISDIR(marker_mode):
        return git_marker
    if not stat.S_ISREG(marker_mode):
        return None
    git_file = _read_worktree_regular_text(git_marker)
    if git_file is None:
        return None
    prefix = "gitdir:"
    if not git_file.startswith(prefix):
        return None
    git_dir = Path(git_file[len(prefix) :].strip())
    if not git_dir.is_absolute():
        git_dir = nested_root / git_dir
    try:
        return git_dir.resolve()
    except OSError:
        return None


def _nested_git_probe_git_dir_at(dir_fd: int) -> Path | None:
    """Return the Git metadata directory for a pinned nested embedded repository fd."""
    nested_root = _worktree_path_for_open_fd(dir_fd)
    if nested_root is None:
        return None
    try:
        marker_mode = os.lstat(".git", dir_fd=dir_fd).st_mode
    except OSError:
        return None
    if stat.S_ISDIR(marker_mode):
        return nested_root / ".git"
    if not stat.S_ISREG(marker_mode):
        return None
    git_file = _read_worktree_regular_text_at(dir_fd, ".git")
    if git_file is None:
        return None
    prefix = "gitdir:"
    if not git_file.startswith(prefix):
        return None
    git_dir = Path(git_file[len(prefix) :].strip())
    if not git_dir.is_absolute():
        git_dir = nested_root / git_dir
    try:
        return git_dir.resolve()
    except OSError:
        return None


def _nested_git_probe_worktree_root(
    *,
    nested_root: Path,
    git_env: Mapping[str, str],
) -> Path | None:
    """Return Git's effective worktree root for nested embedded-repo residue probes.

    Agent-controlled embedded repositories may set ``core.worktree`` to a path
    outside ``nested_root``; Git path listings then refer to that tree while
    naive ``nested_root / path`` reads would target decoy files
    (PRRT_kwDOSJAM6s6eWr9f).
    """
    result = _run_git_bytes(
        worktree_path=nested_root,
        git_env=git_env,
        args=("rev-parse", "--show-toplevel"),
    )
    if result.returncode != 0:
        return None
    reported = result.stdout.decode("utf-8", errors="surrogateescape").strip()
    if not reported:
        return None
    try:
        return Path(reported).resolve()
    except OSError:
        return None


def _git_nested_worktree_commit(
    *,
    worktree_path: Path,
    path: str,
    git_env: Mapping[str, str],
) -> str | None:
    """Return worktree identity for a nested Git directory (submodule or embedded repo)."""
    nested_root = worktree_path / path
    if not _has_nested_git_marker(nested_root):
        return None
    return _git_nested_worktree_commit_from_root(
        nested_root=nested_root,
        git_env=git_env,
        git_dir=_nested_git_probe_git_dir(nested_root),
    )


def _git_nested_worktree_commit_at(
    *,
    dir_fd: int,
    git_env: Mapping[str, str],
) -> str | None:
    """Return nested Git identity for a pinned directory fd without pathname re-entry."""
    if not _has_nested_git_marker_at(dir_fd):
        return None
    nested_root = _worktree_path_for_open_fd(dir_fd)
    if nested_root is None:
        return None
    return _git_nested_worktree_commit_from_root(
        nested_root=nested_root,
        git_env=git_env,
        git_dir=_nested_git_probe_git_dir_at(dir_fd),
    )


def _git_nested_worktree_commit_from_root(
    *,
    nested_root: Path,
    git_env: Mapping[str, str],
    git_dir: Path | None,
) -> str | None:
    nested_git_env = git_env_for_untrusted_nested_repository_probe(git_env)
    with _untrusted_nested_git_probe():
        if _nested_untrusted_git_probe_past_deadline():
            return None
        with _without_nested_git_probe_pin():
            probe_root = _nested_git_probe_worktree_root(
                nested_root=nested_root,
                git_env=nested_git_env,
            )
            if probe_root is None:
                return None
            if git_dir is None:
                git_dir = _nested_git_probe_git_dir(nested_root)
            if git_dir is None:
                return None

        with _pinned_nested_git_probe(git_dir, probe_root):
            head_result = _run_git_bytes(
                worktree_path=probe_root,
                git_env=nested_git_env,
                args=("rev-parse", "HEAD"),
            )
            if head_result.returncode != 0:
                return None
            head = head_result.stdout.decode("ascii", errors="replace").strip()
            if not head:
                return None

            inner_staged, inner_unstaged = _hash_tracked_residue_staged_and_unstaged(
                worktree_path=probe_root,
                git_env=nested_git_env,
            )
            if inner_staged is None or inner_unstaged is None:
                return None

            untracked_result = _run_git_bytes(
                worktree_path=probe_root,
                git_env=nested_git_env,
                # ``git status`` can invoke filter drivers; path listing alone is enough.
                args=("ls-files", "-o", "--exclude-standard", "-z"),
            )
            if untracked_result.returncode != 0:
                return None
            try:
                untracked_paths = _changed_paths_from_name_only_z(untracked_result.stdout)
            except ProtectedScopeDiffError:
                return None
            untracked = set(untracked_paths)
            if untracked:
                inner_untracked = _hash_untracked_residue_paths(
                    worktree_path=probe_root,
                    paths=sorted(untracked),
                    untracked=untracked,
                    git_env=nested_git_env,
                )
                if inner_untracked is None:
                    return None
            else:
                inner_untracked = hashlib.sha256().hexdigest()

    hasher = hashlib.sha256()
    hasher.update(b"head:")
    hasher.update(head.encode("ascii"))
    hasher.update(b"\0staged:")
    hasher.update(inner_staged.encode("ascii"))
    hasher.update(b"\0unstaged:")
    hasher.update(inner_unstaged.encode("ascii"))
    hasher.update(b"\0untracked:")
    hasher.update(inner_untracked.encode("ascii"))
    return hasher.hexdigest()


def _git_submodule_worktree_commit(
    *,
    worktree_path: Path,
    path: str,
    git_env: Mapping[str, str],
) -> str | None:
    """Return worktree identity for a tracked gitlink (submodule) path.

    Combines checked-out HEAD with inner staged/unstaged/untracked residue. Fails
    closed when the submodule worktree has no ``.git`` marker — otherwise
    ``rev-parse HEAD`` walks up to the parent repository and uncommitted inner edits
    never change a HEAD-only fingerprint (PRRT_kwDOSJAM6s6eR-GB).
    """
    return _git_nested_worktree_commit(
        worktree_path=worktree_path,
        path=path,
        git_env=git_env,
    )


def _git_index_mode(
    *,
    worktree_path: Path,
    path: str,
    git_env: Mapping[str, str],
) -> str | None:
    result = _run_git_bytes(
        worktree_path=worktree_path,
        git_env=git_env,
        args=("ls-files", "--stage", "-z", "--", path),
    )
    if result.returncode != 0:
        return None
    first_entry = result.stdout.split(b"\0", 1)[0]
    if not first_entry:
        return None
    mode = first_entry.split(b" ", 1)[0]
    return mode.decode("ascii", errors="replace") or None


def _git_worktree_mode(
    *,
    worktree_path: Path,
    path: str,
) -> str | None:
    candidate = worktree_path / path
    kind_info = _worktree_entry_kind(candidate)
    if kind_info is None:
        return None
    kind, file_mode = kind_info
    if kind == "symlink":
        return "120000"
    if kind == "regular":
        if stat.S_IMODE(file_mode) & stat.S_IXUSR:
            return "100755"
        return "100644"
    if kind == "directory":
        return "040000"
    if kind in _SPECIAL_ENTRY_KINDS:
        return kind
    return None


def _tracked_residue_changed_paths_args(*, cached: bool) -> tuple[str, ...]:
    """Return argv tail that lists changed paths without invoking filter drivers."""
    if cached:
        return ("diff", "--cached", "--name-only", "-z", "--ignore-submodules=none")
    if _NESTED_UNTRUSTED_GIT_PROBE.get():
        # ``git diff --name-only`` runs committed .gitattributes clean filters on
        # worktree bytes; ``git diff-files`` compares index to worktree without them
        # (PRRT_kwDOSJAM6s6eWICC).
        return ("diff-files", "--name-only", "-z")
    return ("diff", "--name-only", "-z", "--ignore-submodules=none")


def _hash_tracked_residue_diffs(
    *,
    worktree_path: Path,
    git_env: Mapping[str, str],
    cached: bool,
) -> str | None:
    """Hash tracked change identity without materializing full ``git diff`` patches.

    ``git diff --name-only -z`` bounds stdout to path names; per-path blob SHAs
    come from ``rev-parse :path`` / ``hash-object --stdin`` so multi-gigabyte edits
    cannot exhaust the control-plane process (PRRT_kwDOSJAM6s6eM1NH). Nested
    embedded-repo probes use ``git diff-files`` for unstaged paths so committed
    filter drivers never execute (PRRT_kwDOSJAM6s6eWICC).
    """
    diff_args = _tracked_residue_changed_paths_args(cached=cached)
    name_result = _run_git_bytes(worktree_path=worktree_path, git_env=git_env, args=diff_args)
    if name_result.returncode != 0:
        return None
    try:
        paths = _changed_paths_from_name_only_z(name_result.stdout)
    except ProtectedScopeDiffError:
        return None

    hasher = hashlib.sha256()
    for path in sorted(paths):
        hasher.update(path.encode("utf-8", errors="surrogateescape"))
        hasher.update(b"\0")
        if cached:
            index_blob = _git_index_blob_sha(
                worktree_path=worktree_path,
                path=path,
                git_env=git_env,
            )
            index_mode = _git_index_mode(
                worktree_path=worktree_path,
                path=path,
                git_env=git_env,
            )
            hasher.update(b"index:")
            hasher.update((index_blob or "<missing>").encode("ascii"))
            hasher.update(b"im:")
            hasher.update((index_mode or "<missing>").encode("ascii"))
        else:
            index_blob = _git_index_blob_sha(
                worktree_path=worktree_path,
                path=path,
                git_env=git_env,
            )
            index_mode = _git_index_mode(
                worktree_path=worktree_path,
                path=path,
                git_env=git_env,
            )
            worktree_blob = _git_worktree_blob_sha(
                worktree_path=worktree_path,
                path=path,
                git_env=git_env,
                index_mode=index_mode,
            )
            if worktree_blob is None:
                candidate = worktree_path / path
                if index_blob is not None:
                    try:
                        candidate.lstat()
                    except OSError as exc:
                        if exc.errno == errno.ENOENT:
                            # Ordinary tracked deletions are absent from the worktree but
                            # still indexed; ``hash-object --path`` returns None without
                            # being unreadable (PRRT_kwDOSJAM6s6eP-gA).
                            worktree_blob = "<deleted>"
                        else:
                            # ``Path.exists()`` also returns False on permission and other
                            # stat errors; those must fail closed, not hash ``<deleted>``
                            # (Bugbot review 5082437263).
                            return None
                    else:
                        if index_mode == "160000":
                            # Gitlinks are directories; fingerprint checked-out submodule HEAD
                            # instead of failing closed (PRRT_kwDOSJAM6s6eRyfx).
                            worktree_blob = _git_submodule_worktree_commit(
                                worktree_path=worktree_path,
                                path=path,
                                git_env=git_env,
                            )
                            if worktree_blob is None:
                                return None
                        else:
                            # Worktree path is present but ``hash-object`` failed — unreadable.
                            return None
                else:
                    return None
            worktree_mode = _git_worktree_mode(
                worktree_path=worktree_path,
                path=path,
            )
            if worktree_mode is None and index_mode == "160000":
                worktree_mode = "160000"
            hasher.update(b"index:")
            hasher.update((index_blob or "<none>").encode("ascii"))
            hasher.update(b"im:")
            hasher.update((index_mode or "<missing>").encode("ascii"))
            hasher.update(b"wt:")
            hasher.update(worktree_blob.encode("ascii"))
            hasher.update(b"wm:")
            hasher.update((worktree_mode or "<missing>").encode("ascii"))
        hasher.update(b"\0")
    return hasher.hexdigest()


def _hash_tracked_residue_staged_and_unstaged(
    *,
    worktree_path: Path,
    git_env: Mapping[str, str],
) -> tuple[str | None, str | None]:
    return (
        _hash_tracked_residue_diffs(
            worktree_path=worktree_path,
            git_env=git_env,
            cached=True,
        ),
        _hash_tracked_residue_diffs(
            worktree_path=worktree_path,
            git_env=git_env,
            cached=False,
        ),
    )


async def _read_correction_pr_worthy_residue_fingerprint(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
) -> str | None:
    """Return a fingerprint of PR-worthy dirty porcelain.

    Empty string means clean. ``None`` means the status probe failed and callers
    must fail closed. Untracked AWF-agent-runtime paths are excluded, matching
    the commit sink's dirtiness filter.

    Path names alone are not enough: when attempt 0 leaves ``src/x.py`` dirty and
    the correction edits that same file, a path-only fingerprint collides and
    attribution treats the mutation as pre-existing residue
    (PRRT_kwDOSJAM6s6eKj9D). Include staged/unstaged diff hashes and untracked
    file content identity while retaining the runtime-path exclusion.
    """
    if not worktree_path.exists():
        return ""

    from awf.node.git_manager import git_env_without_object_lookup_overrides
    from awf.runtime.pr_monitor_runner.path_parsing import (
        _changed_paths_from_porcelain,
        _changed_paths_from_porcelain_z,
        _porcelain_z_records,
        _untracked_paths_from_porcelain,
        _untracked_paths_from_porcelain_z,
    )
    from awf.runtime.validation_worktree import is_under_agent_runtime_root

    git_env = git_env_without_object_lookup_overrides()

    try:
        status = await runner._deps.runner.run(
            git_worktree_command(
                worktree_path,
                "status",
                "--porcelain",
                "-z",
                "--untracked-files=all",
                "--ignore-submodules=none",
            ),
            env=git_env,
        )
    except Exception as exc:
        # Spawn failures (e.g. OSError from create_subprocess_exec) must fail
        # closed like a non-ok status so the correction mutation path rolls back
        # unaccepted dirty edits (PRRT_kwDOSJAM6s6eJi5X).
        _log.warning(
            "monitor.agent_verdict_correction_residue_status_failed",
            workspace_id=workspace_id,
            exc_type=type(exc).__name__,
            error=str(exc)[:400],
        )
        return None
    if not status.ok:
        _log.warning(
            "monitor.agent_verdict_correction_residue_status_failed",
            workspace_id=workspace_id,
            returncode=status.returncode,
            stderr=(status.stderr or "")[:400],
        )
        return None

    status_stdout, is_z = _decode_porcelain_status_stdout(
        stdout=status.stdout or "",
        stdout_bytes=status.stdout_bytes,
    )
    if is_z:
        if status.stdout_bytes is not None and not status.stdout_bytes.strip(b"\0"):
            return ""
        if status.stdout_bytes is None and not status_stdout.strip():
            return ""
    elif not status_stdout.strip():
        return ""

    if is_z:
        untracked = set(_untracked_paths_from_porcelain_z(status_stdout))
        paths = sorted(
            path
            for path in _changed_paths_from_porcelain_z(status_stdout)
            if not (path in untracked and is_under_agent_runtime_root(path))
        )
    else:
        untracked = set(_untracked_paths_from_porcelain(status_stdout))
        paths = sorted(
            path
            for path in _changed_paths_from_porcelain(status_stdout)
            if not (path in untracked and is_under_agent_runtime_root(path))
        )
    if not paths:
        return ""

    tracked_paths = [path for path in paths if path not in untracked]

    # Status identity: keep XY codes for PR-worthy paths (not path names alone).
    path_set = set(paths)
    if is_z:
        status_lines = sorted(
            _format_porcelain_z_line(status_code, path, original_path)
            for status_code, path, original_path in _porcelain_z_records(status_stdout)
            if path in path_set or (original_path is not None and original_path in path_set)
        )
    else:
        status_lines = sorted(
            line
            for line in status_stdout.splitlines()
            if line
            and any(
                candidate in path_set for candidate in _changed_paths_from_porcelain(f"{line}\n")
            )
        )

    try:
        with _residue_fingerprint_nested_scan_budget():
            if tracked_paths:
                staged_digest, unstaged_digest = await asyncio.to_thread(
                    _hash_tracked_residue_staged_and_unstaged,
                    worktree_path=worktree_path,
                    git_env=git_env,
                )
            else:
                empty_digest = hashlib.sha256().hexdigest()
                staged_digest = unstaged_digest = empty_digest
            if staged_digest is None or unstaged_digest is None:
                _log.warning(
                    "monitor.agent_verdict_correction_residue_diff_failed",
                    workspace_id=workspace_id,
                    staged_digest=staged_digest,
                    unstaged_digest=unstaged_digest,
                )
                return None

            try:
                untracked_digest = await asyncio.to_thread(
                    _hash_untracked_residue_paths,
                    worktree_path=worktree_path,
                    paths=paths,
                    untracked=untracked,
                    git_env=git_env,
                )
            except Exception as exc:
                _log.warning(
                    "monitor.agent_verdict_correction_residue_untracked_failed",
                    workspace_id=workspace_id,
                    exc_type=type(exc).__name__,
                    error=str(exc)[:400],
                )
                return None
    except Exception as exc:
        _log.warning(
            "monitor.agent_verdict_correction_residue_diff_failed",
            workspace_id=workspace_id,
            exc_type=type(exc).__name__,
            error=str(exc)[:400],
        )
        return None
    if untracked_digest is None:
        _log.warning(
            "monitor.agent_verdict_correction_residue_untracked_unreadable",
            workspace_id=workspace_id,
        )
        return None

    return "\n".join(
        [
            *status_lines,
            f"staged:{staged_digest}",
            f"unstaged:{unstaged_digest}",
            f"untracked:{untracked_digest}",
        ]
    )


def _correction_authored_mutation_vs_start(
    *,
    attempt_start_head: str | None,
    pre_sink_head: str | None,
    correction_start_residue_fp: str | None,
    pre_sink_residue_fp: str | None,
) -> bool:
    """True when the correction agent mutated HEAD or dirt before the commit sink."""
    if pre_sink_head is None:
        # Cannot observe pre-sink HEAD — fail closed (PRRT_kwDOSJAM6s6eKoIe).
        return True
    if attempt_start_head is not None and pre_sink_head.lower() != attempt_start_head.lower():
        return True
    if pre_sink_residue_fp is None:
        # Cannot observe post-agent dirt — fail closed.
        return True
    if correction_start_residue_fp is None:
        # Unreadable baseline: dirty-to-clean correction mutations are
        # unverifiable (PRRT_kwDOSJAM6s6eU900).
        return True
    return pre_sink_residue_fp != correction_start_residue_fp


def _stranded_residue_is_correction_mutation(
    *,
    correction_start_residue_fp: str | None,
    post_residue_fp: str | None,
) -> bool:
    """True when post-sink stranded dirt is not attributable to correction-start."""
    if post_residue_fp is None:
        return True
    if correction_start_residue_fp is None:
        # Unreadable baseline: empty post-sink residue cannot prove no correction
        # mutation (PRRT_kwDOSJAM6s6eU900).
        return True
    return post_residue_fp != correction_start_residue_fp


async def _correction_attempt_left_pr_worthy_residue(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
) -> bool:
    """True when uncommitted PR-worthy dirt remains after the commit sink.

    ``_commit_dirty_worktree`` may return False after status/add/commit failure
    while leaving correction edits dirty. HEAD can stay at attempt-start with
    ``dirty_changes_committed`` False, so mutation detection must probe porcelain
    before rollback accepts a non-FIXED correction verdict. Status inspection
    failure fails closed. Untracked AWF-agent-runtime paths are excluded, matching
    the commit sink's dirtiness filter.
    """
    fingerprint = await _read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
    )
    if fingerprint is None:
        return True
    return bool(fingerprint)
