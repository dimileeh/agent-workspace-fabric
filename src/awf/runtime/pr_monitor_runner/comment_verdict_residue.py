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
from typing import TYPE_CHECKING

from awf.common.logging import get_logger
from awf.node.git_manager import git_env_for_untrusted_nested_repository_probe
from awf.runtime.pr_monitor_runner.comment_verdict_residue_io import (
    _SPECIAL_ENTRY_KINDS,
    _WORKTREE_DIRECTORY_OPEN_FLAGS,
    _fresh_worktree_path_for_open_fd,
    _has_nested_git_marker,
    _has_nested_git_marker_at,
    _open_worktree_directory,
    _open_worktree_directory_path,
    _open_worktree_regular_file,
    _open_worktree_regular_file_at,
    _read_worktree_regular_text,
    _read_worktree_regular_text_at,
    _special_entry_blob_sha,
    _worktree_directory_entry_mode_token,
    _worktree_entry_kind,
    _worktree_entry_kind_at,
    _worktree_mode_from_kind,
    _worktree_proc_path_for_open_fd,
)
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

_UNBORN_HEAD_SENTINEL = "<unborn>"
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
_NESTED_UNTRUSTED_GIT_PROBE_GIT_MARKER_FD: ContextVar[int | None] = ContextVar(
    "_nested_untrusted_git_probe_git_marker_fd",
    default=None,
)
_NESTED_UNTRUSTED_GIT_PROBE_GIT_COMMON_FD: ContextVar[int | None] = ContextVar(
    "_nested_untrusted_git_probe_git_common_fd",
    default=None,
)
_NESTED_UNTRUSTED_GIT_PROBE_WORKTREE: ContextVar[Path | None] = ContextVar(
    "_nested_untrusted_git_probe_worktree",
    default=None,
)
_NESTED_UNTRUSTED_GIT_PROBE_WORKTREE_FD: ContextVar[int | None] = ContextVar(
    "_nested_untrusted_git_probe_worktree_fd",
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
    pinned_git_dir = _fresh_pinned_nested_git_dir()
    pinned_worktree = _fresh_pinned_nested_worktree()
    if pinned_git_dir is not None and pinned_worktree is not None:
        return git_untrusted_nested_pinned_worktree_command(
            pinned_git_dir,
            pinned_worktree,
            *args,
        )
    if _NESTED_UNTRUSTED_GIT_PROBE.get():
        return git_untrusted_nested_worktree_command(worktree_path, *args)
    return git_worktree_command(worktree_path, *args)


def _fresh_pinned_nested_git_dir() -> Path | None:
    """Return the pinned nested git-dir path via an open marker or gitfile-target fd."""
    marker_fd = _NESTED_UNTRUSTED_GIT_PROBE_GIT_MARKER_FD.get()
    if marker_fd is not None:
        proc_path = _worktree_proc_path_for_open_fd(marker_fd)
        if proc_path is None:
            return None
        try:
            return proc_path.readlink()
        except OSError:
            return None
    return _NESTED_UNTRUSTED_GIT_PROBE_GIT_DIR.get()


def _fresh_pinned_nested_git_common_dir() -> Path | None:
    """Return the pinned nested common-dir path via an open approved ``commondir`` fd."""
    common_fd = _NESTED_UNTRUSTED_GIT_PROBE_GIT_COMMON_FD.get()
    if common_fd is None:
        return None
    proc_path = _worktree_proc_path_for_open_fd(common_fd)
    if proc_path is None:
        return None
    try:
        return proc_path.readlink()
    except OSError:
        return None


def _fresh_pinned_nested_worktree() -> Path | None:
    """Return the pinned nested work-tree path via an open directory fd when held.

    Resolves ``/proc/self/fd/<fd>`` through ``readlink`` for Git ``--work-tree``
    (Git rejects bare ``/proc/self/fd/<fd>`` for some worktree ops). Content
    hashing must not reuse that pathname — see
    ``_worktree_root_for_residue_byte_reads`` (PRRT_kwDOSJAM6s6eajOa).
    """
    worktree_fd = _NESTED_UNTRUSTED_GIT_PROBE_WORKTREE_FD.get()
    if worktree_fd is not None:
        proc_path = _worktree_proc_path_for_open_fd(worktree_fd)
        if proc_path is None:
            return None
        try:
            return proc_path.readlink()
        except OSError:
            return None
    return _NESTED_UNTRUSTED_GIT_PROBE_WORKTREE.get()


def _worktree_root_for_residue_byte_reads(worktree_path: Path) -> Path:
    """Return a worktree root for content reads that stays on the pinned inode.

    When a nested worktree directory fd is held, prefer ``/proc/self/fd/<fd>``
    so multi-file / multi-gigabyte hashing cannot follow a pathname replacement
    of the effective worktree (PRRT_kwDOSJAM6s6eajOa).
    """
    worktree_fd = _NESTED_UNTRUSTED_GIT_PROBE_WORKTREE_FD.get()
    if worktree_fd is not None:
        proc_path = _worktree_proc_path_for_open_fd(worktree_fd)
        if proc_path is not None:
            return proc_path
    return worktree_path


@contextlib.contextmanager
def _pinned_nested_worktree_fd(worktree_fd: int) -> Iterator[None]:
    """Retain an opened effective worktree directory fd for nested residue probes."""
    token: Token[int | None] = _NESTED_UNTRUSTED_GIT_PROBE_WORKTREE_FD.set(worktree_fd)
    try:
        yield
    finally:
        _NESTED_UNTRUSTED_GIT_PROBE_WORKTREE_FD.reset(token)


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
    """Clear nested git-dir/work-tree/marker pins so inner-repo discovery is not mis-scoped."""
    git_dir_token: Token[Path | None] = _NESTED_UNTRUSTED_GIT_PROBE_GIT_DIR.set(None)
    worktree_token: Token[Path | None] = _NESTED_UNTRUSTED_GIT_PROBE_WORKTREE.set(None)
    worktree_fd_token: Token[int | None] = _NESTED_UNTRUSTED_GIT_PROBE_WORKTREE_FD.set(None)
    marker_fd_token: Token[int | None] = _NESTED_UNTRUSTED_GIT_PROBE_GIT_MARKER_FD.set(None)
    common_fd_token: Token[int | None] = _NESTED_UNTRUSTED_GIT_PROBE_GIT_COMMON_FD.set(None)
    try:
        yield
    finally:
        _NESTED_UNTRUSTED_GIT_PROBE_GIT_COMMON_FD.reset(common_fd_token)
        _NESTED_UNTRUSTED_GIT_PROBE_GIT_MARKER_FD.reset(marker_fd_token)
        _NESTED_UNTRUSTED_GIT_PROBE_WORKTREE_FD.reset(worktree_fd_token)
        _NESTED_UNTRUSTED_GIT_PROBE_GIT_DIR.reset(git_dir_token)
        _NESTED_UNTRUSTED_GIT_PROBE_WORKTREE.reset(worktree_token)


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
    byte_root = _worktree_root_for_residue_byte_reads(worktree_path)
    candidate = byte_root / path
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
        hasher.update(
            _worktree_directory_entry_mode_token(
                kind=child_kind_name,
                st_mode=child_mode,
            ).encode("ascii")
        )
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
                        outer_worktree_path=worktree_path,
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
    byte_root = _worktree_root_for_residue_byte_reads(worktree_path)
    for path in paths:
        if path not in untracked:
            continue
        # Hash each file independently so raw bytes cannot shift across \0 path
        # delimiters (PRRT_kwDOSJAM6s6eRK93).
        file_hasher = hashlib.sha256()
        file_hasher.update(path.encode("utf-8", errors="surrogateescape"))
        file_hasher.update(b"\0")
        candidate = byte_root / path
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
    # Sanitized nested envs strip GIT_COMMON_DIR; re-pin from the retained approved
    # common-dir fd so Git does not re-read a mutable marker ``commondir``
    # (PRRT_kwDOSJAM6s6ecAB2).
    pinned_common = _fresh_pinned_nested_git_common_dir()
    if pinned_common is not None:
        env["GIT_COMMON_DIR"] = str(pinned_common)
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
    byte_root = _worktree_root_for_residue_byte_reads(worktree_path)
    candidate = byte_root / path
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
    """Return the Git metadata directory for a nested embedded repository gitfile."""
    git_marker = nested_root / ".git"
    try:
        marker_mode = git_marker.lstat().st_mode
    except OSError:
        return None
    if stat.S_ISDIR(marker_mode):
        return None
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


def _parse_nested_git_dir_gitfile_at(dir_fd: int) -> Path | None:
    """Return the git-dir path from a nested ``.git`` gitfile without resolving it."""
    try:
        marker_mode = os.lstat(".git", dir_fd=dir_fd).st_mode
    except OSError:
        return None
    if not stat.S_ISREG(marker_mode):
        return None
    git_file = _read_worktree_regular_text_at(dir_fd, ".git")
    if git_file is None:
        return None
    prefix = "gitdir:"
    if not git_file.startswith(prefix):
        return None
    git_dir = Path(git_file[len(prefix) :].strip())
    if not git_dir.parts:
        return None
    return git_dir


def _approved_git_metadata_roots(outer_worktree_path: Path) -> tuple[Path, ...]:
    """Return roots that may host nested gitfile metadata for residue probes.

    Nested gitfiles may point at a separate git-dir inside the AWF checkout or at
    linked-worktree metadata under the sibling ``mirrors/`` tree
    (``<worktrees_root>/../mirrors``). Cross-workspace and host paths are not
    approved (PRRT_kwDOSJAM6s6ebFe3).
    """
    try:
        outer = outer_worktree_path.resolve()
    except OSError:
        return ()
    roots: list[Path] = [outer]
    mirrors = outer.parent.parent / "mirrors"
    try:
        roots.append(mirrors.resolve())
    except OSError:
        roots.append(mirrors)
    return tuple(roots)


def _approved_root_for_git_dir(
    candidate: Path,
    *,
    outer_worktree_path: Path,
) -> Path | None:
    """Return the approved root containing ``candidate``, or ``None``."""
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    for root in _approved_git_metadata_roots(outer_worktree_path):
        try:
            if resolved == root or resolved.is_relative_to(root):
                return root
        except (OSError, ValueError):
            continue
    return None


def _open_git_dir_path_at(
    dir_fd: int,
    git_dir: Path,
    *,
    outer_worktree_path: Path,
) -> int | None:
    """Open a git metadata directory without following symlinks.

    Absolute and parent-escaping gitfile targets are accepted only when the
    resolved metadata directory stays under the outer AWF checkout or the
    sibling AWF ``mirrors/`` root; opens descend from that approved root rather
    than from ``/`` (PRRT_kwDOSJAM6s6ebFe3).
    """
    if git_dir.is_absolute():
        candidate = git_dir
    else:
        nested_root = _fresh_worktree_path_for_open_fd(dir_fd)
        if nested_root is None:
            return None
        candidate = nested_root / git_dir

    approved_root = _approved_root_for_git_dir(
        candidate,
        outer_worktree_path=outer_worktree_path,
    )
    if approved_root is None:
        return None
    try:
        relative = candidate.resolve().relative_to(approved_root.resolve())
    except (OSError, ValueError):
        return None

    try:
        current_fd = os.open(approved_root, _WORKTREE_DIRECTORY_OPEN_FLAGS)
    except OSError:
        return None
    try:
        for part in relative.parts:
            if part in {".", ""}:
                continue
            if part == "..":
                os.close(current_fd)
                return None
            next_fd = os.open(part, _WORKTREE_DIRECTORY_OPEN_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        if not stat.S_ISDIR(os.fstat(current_fd).st_mode):
            os.close(current_fd)
            return None
        return current_fd
    except OSError:
        os.close(current_fd)
        return None


@contextlib.contextmanager
def _open_nested_git_dir_gitfile_target_at(
    dir_fd: int,
    *,
    outer_worktree_path: Path,
) -> Iterator[int | None]:
    """Open a nested ``.git`` gitfile target with ``O_NOFOLLOW`` for pinned git-dir probes."""
    git_dir = _parse_nested_git_dir_gitfile_at(dir_fd)
    if git_dir is None:
        yield None
        return
    target_fd = _open_git_dir_path_at(
        dir_fd,
        git_dir,
        outer_worktree_path=outer_worktree_path,
    )
    if target_fd is None:
        yield None
        return
    try:
        if not stat.S_ISDIR(os.fstat(target_fd).st_mode):
            yield None
            return
        yield target_fd
    finally:
        os.close(target_fd)


def _parse_nested_git_commondir_at(marker_fd: int) -> Path | None:
    """Return the path from a nested ``.git`` ``commondir`` file, if present.

    Absent or empty ``commondir`` returns ``None`` (caller keeps marker-pin
    behavior). Unreadable or non-regular ``commondir`` raises ``OSError`` so
    callers can fail closed (review 5087582495 / PRRT_kwDOSJAM6s6ebprj).
    """
    try:
        mode = os.lstat("commondir", dir_fd=marker_fd).st_mode
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OSError(exc.errno, "nested git commondir is unreadable") from exc
    if not stat.S_ISREG(mode):
        raise OSError(errno.EINVAL, "nested git commondir is not a regular file")
    text = _read_worktree_regular_text_at(marker_fd, "commondir")
    if text is None:
        raise OSError(errno.EIO, "nested git commondir could not be read")
    if not text:
        return None
    common = Path(text)
    if not common.parts:
        return None
    return common


def _try_open_nested_git_marker_commondir_at(
    marker_fd: int,
    *,
    outer_worktree_path: Path,
) -> tuple[bool, int | None]:
    """Return ``(approved, common_fd)`` for a nested marker ``commondir``.

    ``common_fd`` is an opened approved common-directory descriptor when a
    non-empty ``commondir`` is present; the caller must retain and close it for
    the probe lifetime (PRRT_kwDOSJAM6s6ecAB2). Absent/empty ``commondir``
    returns ``(True, None)``.
    """
    try:
        common = _parse_nested_git_commondir_at(marker_fd)
    except OSError:
        return False, None
    if common is None:
        return True, None
    common_fd = _open_git_dir_path_at(
        marker_fd,
        common,
        outer_worktree_path=outer_worktree_path,
    )
    if common_fd is None:
        return False, None
    return True, common_fd


@contextlib.contextmanager
def _open_nested_git_dir_marker_at(
    dir_fd: int,
    *,
    outer_worktree_path: Path,
) -> Iterator[tuple[int, int | None] | None]:
    """Open a nested ``.git`` directory marker with ``O_NOFOLLOW`` for pinned git-dir probes.

    Yields ``(marker_fd, common_fd)`` when the marker is usable. ``common_fd`` is
    the retained approved common-directory descriptor when ``commondir`` is
    present, or ``None`` when absent/empty (review 5087582495 /
    PRRT_kwDOSJAM6s6ecAB2).
    """
    try:
        marker_mode = os.lstat(".git", dir_fd=dir_fd).st_mode
    except OSError:
        yield None
        return
    if not stat.S_ISDIR(marker_mode):
        yield None
        return
    marker_fd = os.open(".git", _WORKTREE_DIRECTORY_OPEN_FLAGS, dir_fd=dir_fd)
    common_fd: int | None = None
    try:
        if not stat.S_ISDIR(os.fstat(marker_fd).st_mode):
            yield None
            return
        approved, common_fd = _try_open_nested_git_marker_commondir_at(
            marker_fd,
            outer_worktree_path=outer_worktree_path,
        )
        if not approved:
            yield None
            return
        yield marker_fd, common_fd
    finally:
        if common_fd is not None:
            os.close(common_fd)
        os.close(marker_fd)


@contextlib.contextmanager
def _pinned_nested_git_dir_at(
    dir_fd: int,
    *,
    outer_worktree_path: Path,
) -> Iterator[bool]:
    """Yield True when nested git-dir probes are pinned to an opened marker or gitfile."""
    with _open_nested_git_dir_marker_at(
        dir_fd,
        outer_worktree_path=outer_worktree_path,
    ) as opened:
        if opened is not None:
            marker_fd, common_fd = opened
            marker_token = _NESTED_UNTRUSTED_GIT_PROBE_GIT_MARKER_FD.set(marker_fd)
            common_token = _NESTED_UNTRUSTED_GIT_PROBE_GIT_COMMON_FD.set(common_fd)
            try:
                yield True
            finally:
                _NESTED_UNTRUSTED_GIT_PROBE_GIT_COMMON_FD.reset(common_token)
                _NESTED_UNTRUSTED_GIT_PROBE_GIT_MARKER_FD.reset(marker_token)
            return
        with _open_nested_git_dir_gitfile_target_at(
            dir_fd,
            outer_worktree_path=outer_worktree_path,
        ) as gitfile_target_fd:
            if gitfile_target_fd is None:
                yield False
                return
            token = _NESTED_UNTRUSTED_GIT_PROBE_GIT_MARKER_FD.set(gitfile_target_fd)
            try:
                yield True
            finally:
                _NESTED_UNTRUSTED_GIT_PROBE_GIT_MARKER_FD.reset(token)


def _nested_git_probe_worktree_root(
    *,
    nested_root: Path,
    git_env: Mapping[str, str],
) -> Path | None:
    """Return Git's effective worktree root for nested embedded-repo residue probes.

    Agent-controlled embedded repositories may set ``core.worktree`` to a path
    outside ``nested_root``; Git path listings then refer to that tree while
    naive ``nested_root / path`` reads would target decoy files
    (PRRT_kwDOSJAM6s6eWr9f). Callers must reject roots outside the outer AWF
    checkout before opening them (PRRT_kwDOSJAM6s6eadgA).
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


def _nested_probe_root_within_outer_worktree(
    *,
    probe_root: Path,
    worktree_path: Path,
) -> bool:
    """True when the effective nested worktree root stays inside the AWF checkout."""
    try:
        resolved_probe = probe_root.resolve()
        resolved_outer = worktree_path.resolve()
    except OSError:
        return False
    return resolved_probe.is_relative_to(resolved_outer)


def _git_nested_worktree_commit(
    *,
    worktree_path: Path,
    path: str,
    git_env: Mapping[str, str],
) -> str | None:
    """Return worktree identity for a nested Git directory (submodule or embedded repo)."""
    try:
        with _open_worktree_directory(worktree_path, path) as dir_fd:
            return _git_nested_worktree_commit_at(
                dir_fd=dir_fd,
                git_env=git_env,
                outer_worktree_path=worktree_path,
            )
    except OSError:
        return None


def _git_nested_worktree_commit_at(
    *,
    dir_fd: int,
    git_env: Mapping[str, str],
    outer_worktree_path: Path,
) -> str | None:
    """Return nested Git identity for a pinned directory fd without pathname re-entry."""
    if not _has_nested_git_marker_at(dir_fd):
        return None
    if _fresh_worktree_path_for_open_fd(dir_fd) is None:
        return None
    return _git_nested_worktree_commit_from_root(
        dir_fd=dir_fd,
        git_env=git_env,
        outer_worktree_path=outer_worktree_path,
    )


def _resolve_nested_worktree_head(
    *,
    worktree_path: Path,
    git_env: Mapping[str, str],
) -> str | None:
    """Return nested HEAD SHA, ``<unborn>`` when HEAD has no commit yet, or None."""
    head_result = _run_git_bytes(
        worktree_path=worktree_path,
        git_env=git_env,
        args=("rev-parse", "HEAD"),
    )
    if head_result.returncode == 0:
        head = head_result.stdout.decode("ascii", errors="replace").strip()
        return head if head else None

    verify_result = _run_git_bytes(
        worktree_path=worktree_path,
        git_env=git_env,
        args=("rev-parse", "--verify", "HEAD^{commit}"),
    )
    if verify_result.returncode == 0:
        # HEAD resolves as a commit but ``rev-parse HEAD`` failed — fail closed.
        return None

    symref_result = _run_git_bytes(
        worktree_path=worktree_path,
        git_env=git_env,
        args=("symbolic-ref", "-q", "HEAD"),
    )
    if symref_result.returncode != 0:
        return None
    if not symref_result.stdout.strip():
        return None
    return _UNBORN_HEAD_SENTINEL


def _git_nested_worktree_commit_from_root(
    *,
    dir_fd: int,
    git_env: Mapping[str, str],
    outer_worktree_path: Path,
) -> str | None:
    nested_git_env = git_env_for_untrusted_nested_repository_probe(git_env)
    with _untrusted_nested_git_probe():
        if _nested_untrusted_git_probe_past_deadline():
            return None
        with _without_nested_git_probe_pin():
            nested_root = _fresh_worktree_path_for_open_fd(dir_fd)
            if nested_root is None:
                return None
            probe_root = _nested_git_probe_worktree_root(
                nested_root=nested_root,
                git_env=nested_git_env,
            )
            if probe_root is None:
                return None

            # Re-resolve from fd immediately before pin so path swaps cannot redirect probes.
            nested_root = _fresh_worktree_path_for_open_fd(dir_fd)
            if nested_root is None:
                return None
            probe_root = _nested_git_probe_worktree_root(
                nested_root=nested_root,
                git_env=nested_git_env,
            )
            if probe_root is None:
                return None
            # Reject agent-redirected worktrees outside the AWF checkout before open
            # (PRRT_kwDOSJAM6s6eadgA); in-checkout redirects remain allowed.
            if not _nested_probe_root_within_outer_worktree(
                probe_root=probe_root,
                worktree_path=outer_worktree_path,
            ):
                return None

        with _open_worktree_directory_path(
            probe_root,
            outer_worktree_path=outer_worktree_path,
        ) as probe_worktree_fd:
            if probe_worktree_fd is None:
                return None
            with (
                _pinned_nested_worktree_fd(probe_worktree_fd),
                _pinned_nested_git_dir_at(
                    dir_fd,
                    outer_worktree_path=outer_worktree_path,
                ) as has_pinned_git_dir,
            ):
                if not has_pinned_git_dir:
                    return None
                git_dir = _fresh_pinned_nested_git_dir()
                pinned_worktree = _fresh_pinned_nested_worktree()
                if git_dir is None or pinned_worktree is None:
                    return None
                with _pinned_nested_git_probe(git_dir, pinned_worktree):
                    worktree_path = _fresh_pinned_nested_worktree()
                    if worktree_path is None:
                        return None
                    head = _resolve_nested_worktree_head(
                        worktree_path=worktree_path,
                        git_env=nested_git_env,
                    )
                    if head is None:
                        return None

                    worktree_path = _fresh_pinned_nested_worktree()
                    if worktree_path is None:
                        return None
                    inner_staged, inner_unstaged = _hash_tracked_residue_staged_and_unstaged(
                        worktree_path=worktree_path,
                        git_env=nested_git_env,
                    )
                    if inner_staged is None or inner_unstaged is None:
                        return None

                    worktree_path = _fresh_pinned_nested_worktree()
                    if worktree_path is None:
                        return None
                    untracked_result = _run_git_bytes(
                        worktree_path=worktree_path,
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
                        worktree_path = _fresh_pinned_nested_worktree()
                        if worktree_path is None:
                            return None
                        inner_untracked = _hash_untracked_residue_paths(
                            worktree_path=worktree_path,
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
