"""Worktree path IO helpers for correction residue fingerprinting.

Leaf helpers for no-follow / nonblocking opens and entry classification. Kept
separate so ``comment_verdict_residue`` stays under the first-party line budget.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import select
import stat
import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from contextvars import ContextVar, Token
from pathlib import Path
from typing import IO, BinaryIO, Protocol


class _Hasher(Protocol):
    def update(self, data: bytes, /) -> None: ...  # pragma: no cover - Protocol declaration only.


class _RegularHashBudget:
    """Mutable aggregate byte + deadline budget for one residue fingerprint."""

    __slots__ = ("bytes_remaining", "deadline")

    def __init__(self, *, bytes_remaining: int, deadline: float) -> None:
        self.bytes_remaining = bytes_remaining
        self.deadline = deadline


class _DirectoryEnumBudget:
    """Mutable aggregate entry + depth + deadline budget for directory residue scans."""

    __slots__ = ("entries_remaining", "deadline", "max_depth")

    def __init__(self, *, entries_remaining: int, deadline: float, max_depth: int) -> None:
        self.entries_remaining = entries_remaining
        self.deadline = deadline
        self.max_depth = max_depth


class _NestedProbeDeadline:
    """Mutable nested-probe deadline shared across fingerprint ``to_thread`` workers.

    ``ContextVar.set`` inside a worker does not propagate back to the event-loop
    context (PRRT_kwDOSJAM6s6eglyo); mutating ``deadline`` on a shared holder does.
    """

    __slots__ = ("deadline",)

    def __init__(self) -> None:
        self.deadline: float | None = None


_SPECIAL_ENTRY_KINDS = frozenset({"fifo", "socket", "char", "block", "other"})
_WORKTREE_REGULAR_OPEN_FLAGS = (
    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
)
_WORKTREE_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_WORKTREE_REGULAR_TEXT_READ_LIMIT_BYTES = 4096
_WORKTREE_REGULAR_HASH_CHUNK_BYTES = 65536
# Absolute caps: open-time st_size is attacker-controlled (sparse truncate).
_WORKTREE_REGULAR_HASH_MAX_FILE_BYTES = 8 * 1024 * 1024
_WORKTREE_REGULAR_HASH_AGGREGATE_MAX_BYTES = 32 * 1024 * 1024
_WORKTREE_REGULAR_HASH_BUDGET_SECONDS = 30.0
_REGULAR_HASH_BUDGET: ContextVar[_RegularHashBudget | None] = ContextVar(
    "_regular_hash_budget",
    default=None,
)
# Empty directory trees bypass regular-file hashing; bound them separately
# (PRRT_kwDOSJAM6s6eeAsN).
_WORKTREE_DIRECTORY_ENUM_AGGREGATE_MAX_ENTRIES = 100_000
_WORKTREE_DIRECTORY_ENUM_MAX_DEPTH = 256
_WORKTREE_DIRECTORY_ENUM_BUDGET_SECONDS = 30.0
_DIRECTORY_ENUM_BUDGET: ContextVar[_DirectoryEnumBudget | None] = ContextVar(
    "_directory_enum_budget",
    default=None,
)
# Nested NUL path listings (``ls-files -o`` / ``diff --name-only``) must stream
# with the same entry scale as directory enum; byte cap bounds pathological
# path-name inflation (PRRT_kwDOSJAM6s6efXeI / PRRT_kwDOSJAM6s6ef8Fs).
_NESTED_UNTRACKED_LS_FILES_MAX_STDOUT_BYTES = 16 * 1024 * 1024
_NUL_PATH_RECORD_READ_CHUNK_BYTES = 65_536


def _terminate_capped_nul_path_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):  # pragma: no cover
            proc.wait(timeout=5)


def _popen_capped_nul_path_records(
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    max_records: int,
    max_bytes: int,
    timeout: float | None,
) -> tuple[bytes, ...] | None:
    """Popen + stream NUL path records with hard caps (nested untrusted probes)."""
    if timeout == 0.0:
        return None
    deadline = time.monotonic() + timeout if timeout is not None else None
    try:
        proc = subprocess.Popen(
            list(command),
            env=dict(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        if proc.stdout is None:
            return None
        records = _read_capped_nul_path_records(
            proc.stdout,
            max_records=max_records,
            max_bytes=max_bytes,
            deadline_monotonic=deadline,
        )
        if records is None:
            _terminate_capped_nul_path_process(proc)
            return None
        wait_timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
        try:
            returncode = proc.wait(timeout=wait_timeout)
        except subprocess.TimeoutExpired:
            _terminate_capped_nul_path_process(proc)
            return None
        if returncode != 0:
            return None
        return records
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.poll() is None:
            _terminate_capped_nul_path_process(proc)


@contextlib.contextmanager
def _residue_regular_hash_budget() -> Iterator[None]:
    """Bound aggregate regular-file hash bytes and wall time for one fingerprint."""
    if _REGULAR_HASH_BUDGET.get() is not None:
        yield
        return
    budget = _RegularHashBudget(
        bytes_remaining=_WORKTREE_REGULAR_HASH_AGGREGATE_MAX_BYTES,
        deadline=time.monotonic() + _WORKTREE_REGULAR_HASH_BUDGET_SECONDS,
    )
    token: Token[_RegularHashBudget | None] = _REGULAR_HASH_BUDGET.set(budget)
    try:
        yield
    finally:
        _REGULAR_HASH_BUDGET.reset(token)


@contextlib.contextmanager
def _residue_directory_enum_budget() -> Iterator[None]:
    """Bound aggregate directory entries, depth, and wall time for one fingerprint."""
    if _DIRECTORY_ENUM_BUDGET.get() is not None:
        yield
        return
    budget = _DirectoryEnumBudget(
        entries_remaining=_WORKTREE_DIRECTORY_ENUM_AGGREGATE_MAX_ENTRIES,
        deadline=time.monotonic() + _WORKTREE_DIRECTORY_ENUM_BUDGET_SECONDS,
        max_depth=_WORKTREE_DIRECTORY_ENUM_MAX_DEPTH,
    )
    token: Token[_DirectoryEnumBudget | None] = _DIRECTORY_ENUM_BUDGET.set(budget)
    try:
        yield
    finally:
        _DIRECTORY_ENUM_BUDGET.reset(token)


def _directory_enum_allows_descent(depth: int) -> bool:
    """Return False when depth or wall-time budget is exhausted (fail closed)."""
    budget = _DIRECTORY_ENUM_BUDGET.get()
    if budget is None:
        return True
    if depth > budget.max_depth:
        return False
    return time.monotonic() < budget.deadline


def _directory_enum_consume_entries(count: int) -> bool:
    """Consume ``count`` directory entries; return False when the budget is exhausted."""
    if count < 0:
        return False
    budget = _DIRECTORY_ENUM_BUDGET.get()
    if budget is None:
        return True
    if time.monotonic() >= budget.deadline:
        return False
    if count > budget.entries_remaining:
        return False
    budget.entries_remaining -= count
    return True


def _read_opened_regular_file_snapshot(fh: BinaryIO) -> bytes | None:
    """Return a size-bounded snapshot of an opened regular file, or ``None``.

    Reads only the ``st_size`` observed at the start and revalidates
    size/identity/change metadata afterwards so a concurrent appender cannot
    keep ``read()`` returning full chunks forever (PRRT_kwDOSJAM6s6ecabJ) and a
    same-size in-place overwrite cannot accept a torn multi-chunk mixture
    (PRRT_kwDOSJAM6s6ej31I). Absolute per-file and aggregate byte/deadline
    budgets reject attacker-sized sparse files (PRRT_kwDOSJAM6s6edfu4). Callers
    that need a Git blob SHA (``hash-object --stdin``) must use this snapshot
    instead of streaming a live descriptor: outer probes have no nested-probe
    timeout, so a never-EOF appender would otherwise block the correction
    monitor (PRRT_kwDOSJAM6s6ef8Fm).
    """
    try:
        st = os.fstat(fh.fileno())
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    if st.st_size < 0 or st.st_size > _WORKTREE_REGULAR_HASH_MAX_FILE_BYTES:
        return None
    budget = _REGULAR_HASH_BUDGET.get()
    if budget is not None:
        if time.monotonic() >= budget.deadline:
            return None
        if st.st_size > budget.bytes_remaining:
            return None
        budget.bytes_remaining -= st.st_size
    remaining = st.st_size
    chunks: list[bytes] = []
    while remaining > 0:
        if budget is not None and time.monotonic() >= budget.deadline:
            return None
        try:
            chunk = fh.read(min(_WORKTREE_REGULAR_HASH_CHUNK_BYTES, remaining))
        except OSError:
            return None
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    try:
        st_after = os.fstat(fh.fileno())
    except OSError:
        return None
    if not (
        stat.S_ISREG(st_after.st_mode)
        and st_after.st_size == st.st_size
        and st_after.st_ino == st.st_ino
        and st_after.st_dev == st.st_dev
        and st_after.st_mtime_ns == st.st_mtime_ns
        and st_after.st_ctime_ns == st.st_ctime_ns
    ):
        return None
    return b"".join(chunks)


def _hash_opened_regular_file_into(hasher: _Hasher, fh: BinaryIO) -> bool:
    """Hash a size-bounded snapshot of an opened regular file.

    Delegates to ``_read_opened_regular_file_snapshot`` so digest and Git blob
    SHA paths share the same appender / budget fail-closed rules
    (PRRT_kwDOSJAM6s6ecabJ / edfu4 / ef8Fm).
    """
    snapshot = _read_opened_regular_file_snapshot(fh)
    if snapshot is None:
        return False
    hasher.update(snapshot)
    return True


def _validate_opened_worktree_regular_fd(fd: int, *, not_regular_msg: str) -> None:
    """Re-validate an opened worktree fd is still a regular file; close on failure."""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EBADF, not_regular_msg)
    except OSError:
        os.close(fd)
        raise


@contextlib.contextmanager
def _open_worktree_regular_file(candidate: Path) -> Iterator[BinaryIO]:
    """Open a leaf worktree regular file without blocking on TOCTOU swaps.

    ``lstat`` may classify a path as regular moments before another worktree
    process replaces it with a FIFO; pathname-based ``open("rb")`` would then
    block until a writer connects. Open with ``O_NONBLOCK`` and re-validate the
    opened inode via ``fstat`` so swapped special files fail closed instead.

    Pathname ``O_NOFOLLOW`` only refuses a final-component symlink. Multi-component
    residue paths must use ``_open_worktree_regular_file_under_root`` so intermediate
    directory swaps cannot escape the worktree (PRRT_kwDOSJAM6s6ef8Fg).
    """
    fd = os.open(candidate, _WORKTREE_REGULAR_OPEN_FLAGS)
    _validate_opened_worktree_regular_fd(
        fd,
        not_regular_msg="worktree path is not a regular file after open",
    )
    with os.fdopen(fd, "rb") as fh:
        yield fh


@contextlib.contextmanager
def _open_worktree_regular_file_under_root(
    root: Path,
    path: str,
    *,
    root_dir_fd: int | None = None,
) -> Iterator[BinaryIO]:
    """Open ``root/path`` descending every component with no-follow semantics.

    Pathname ``os.open(candidate, O_NOFOLLOW)`` only refuses a final-component
    symlink. After Git reports a dirty path, a surviving agent can replace an
    intermediate directory with a symlink so the fingerprint reads a
    control-plane-accessible file outside the worktree (PRRT_kwDOSJAM6s6ef8Fg).
    When ``root_dir_fd`` is set (pinned nested worktree), descend from that
    descriptor; otherwise open ``root`` as a directory and walk each part with
    ``O_NOFOLLOW``.
    """
    rel_parts = Path(path).parts
    if not rel_parts:
        raise OSError(errno.EINVAL, "worktree regular path is empty", path)
    for part in rel_parts:
        if part in {".", ".."}:
            raise OSError(errno.EINVAL, "unsafe worktree path component", part)

    if root_dir_fd is not None:
        dir_fd = os.dup(root_dir_fd)
    else:
        dir_fd = os.open(root, _WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        for part in rel_parts[:-1]:
            child_fd = os.open(part, _WORKTREE_DIRECTORY_OPEN_FLAGS, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = child_fd
        fd = os.open(rel_parts[-1], _WORKTREE_REGULAR_OPEN_FLAGS, dir_fd=dir_fd)
    except OSError:
        os.close(dir_fd)
        raise
    os.close(dir_fd)
    _validate_opened_worktree_regular_fd(
        fd,
        not_regular_msg="worktree path is not a regular file after open",
    )
    with os.fdopen(fd, "rb") as fh:
        yield fh


def _read_worktree_symlink_under_root(
    root: Path,
    path: str,
    *,
    root_dir_fd: int | None = None,
) -> bytes:
    """Read ``root/path`` symlink text descending every component with no-follow.

    Pathname ``Path.readlink()`` follows intermediate directory components. After
    Git reports a dirty symlink path, a surviving agent can replace an intermediate
    directory with a symlink so the fingerprint reads link text from outside the
    worktree (PRRT_kwDOSJAM6s6eiJk-). When ``root_dir_fd`` is set (pinned nested
    worktree), descend from that descriptor; otherwise open ``root`` as a directory
    and walk each parent with ``O_NOFOLLOW``, then ``os.readlink(..., dir_fd=...)``.
    """
    rel_parts = Path(path).parts
    if not rel_parts:
        raise OSError(errno.EINVAL, "worktree symlink path is empty", path)
    for part in rel_parts:
        if part in {".", ".."}:
            raise OSError(errno.EINVAL, "unsafe worktree path component", part)

    if root_dir_fd is not None:
        dir_fd = os.dup(root_dir_fd)
    else:
        dir_fd = os.open(root, _WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        for part in rel_parts[:-1]:
            child_fd = os.open(part, _WORKTREE_DIRECTORY_OPEN_FLAGS, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = child_fd
        link_text = os.readlink(rel_parts[-1], dir_fd=dir_fd)
    except OSError:
        os.close(dir_fd)
        raise
    os.close(dir_fd)
    return str(link_text).encode("utf-8", errors="surrogateescape")


@contextlib.contextmanager
def _open_worktree_regular_file_at(dir_fd: int, name: str) -> Iterator[BinaryIO]:
    """Open a directory-relative regular file without pathname re-entry."""
    fd = os.open(name, _WORKTREE_REGULAR_OPEN_FLAGS, dir_fd=dir_fd)
    _validate_opened_worktree_regular_fd(
        fd,
        not_regular_msg="worktree entry is not a regular file after open",
    )
    with os.fdopen(fd, "rb") as fh:
        yield fh


def _worktree_proc_path_for_open_fd(dir_fd: int) -> Path | None:
    """Return the ``/proc/self/fd/<fd>`` path for an opened worktree directory fd."""
    try:
        os.fstat(dir_fd)
    except OSError:
        return None
    return Path(f"/proc/self/fd/{dir_fd}")


def _fresh_worktree_path_for_open_fd(dir_fd: int) -> Path | None:
    """Resolve the pinned directory pathname via ``/proc/self/fd/<fd>`` at call time."""
    proc_path = _worktree_proc_path_for_open_fd(dir_fd)
    if proc_path is None:
        return None
    try:
        return proc_path.readlink()
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


def _worktree_directory_entry_mode_token(*, kind: str, st_mode: int) -> str:
    """Return a Git-aligned mode token for directory-entry fingerprint prefixes."""
    if kind in {"regular", "symlink", "directory"}:
        return _worktree_mode_from_kind(kind=kind, st_mode=st_mode) or "<missing>"
    return oct(stat.S_IMODE(st_mode))


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
def _open_worktree_directory_path(
    directory: Path,
    *,
    outer_worktree_path: Path,
) -> Iterator[int | None]:
    """Open a contained worktree root without following any path-component symlink.

    Used to retain Git's effective ``core.worktree`` root across nested residue
    probes so pathname replacement after discovery cannot redirect reads
    (PRRT_kwDOSJAM6s6eY3eE). Pathname ``open(..., O_NOFOLLOW)`` only refuses a
    final-component symlink; after containment an agent can still replace an
    intermediate ancestor with a symlink into an external tree
    (PRRT_kwDOSJAM6s6ebFex). Descend from the outer AWF checkout so every
    ancestor is pinned with ``O_NOFOLLOW``. Yields ``None`` when the path
    cannot be opened as a directory inside the outer checkout.
    """
    try:
        relative = directory.resolve().relative_to(outer_worktree_path.resolve())
    except (OSError, ValueError):
        yield None
        return
    if not relative.parts:
        try:
            dir_fd = os.open(outer_worktree_path, _WORKTREE_DIRECTORY_OPEN_FLAGS)
        except OSError:
            yield None
            return
        try:
            if not stat.S_ISDIR(os.fstat(dir_fd).st_mode):
                yield None
                return
            yield dir_fd
        finally:
            os.close(dir_fd)
        return

    # Manual enter/exit so setup OSError yields None once (no double-yield).
    nested_cm = _open_worktree_directory(outer_worktree_path, relative.as_posix())
    try:
        dir_fd = nested_cm.__enter__()
    except OSError:
        yield None
        return
    try:
        yield dir_fd
    finally:
        nested_cm.__exit__(None, None, None)


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


def _sorted_worktree_directory_entry_names(dir_fd: int) -> list[str] | None:
    """Return sorted entry names for an opened worktree directory fd, or None.

    Enumeration is pinned to the opened inode via ``/proc/self/fd/<fd>`` because
    some platforms expose ``openat``/``lstat`` ``dir_fd`` support without a
    ``scandir(dir_fd=...)`` wrapper. Entry consumption consults the directory
    enum budget so wide empty trees fail closed mid-scan (PRRT_kwDOSJAM6s6eeAsN).
    """
    budget = _DIRECTORY_ENUM_BUDGET.get()
    if budget is not None and time.monotonic() >= budget.deadline:
        return None
    proc_path = f"/proc/self/fd/{dir_fd}"
    names: list[str] = []
    try:
        with os.scandir(proc_path) as entries:
            for entry in entries:
                if entry.name in {".", ".."}:
                    continue
                if not _directory_enum_consume_entries(1):
                    return None
                names.append(entry.name)
    except OSError:
        return None
    names.sort()
    return names


def _read_capped_nul_path_records(
    stdout: IO[bytes],
    *,
    max_records: int,
    max_bytes: int,
    deadline_monotonic: float | None,
) -> tuple[bytes, ...] | None:
    """Drain NUL-delimited path records with hard path/byte/deadline caps.

    Used for nested ``git ls-files -o -z`` and tracked ``--name-only -z`` so the
    control plane never buffers an unbounded path list in
    ``subprocess.run(capture_output=True)`` (PRRT_kwDOSJAM6s6efXeI /
    PRRT_kwDOSJAM6s6ef8Fs). Returns ``None`` to fail closed on cap exhaustion,
    wall-time deadline, empty path records, or a missing terminating NUL.
    """
    if max_records < 0 or max_bytes < 0:
        return None
    try:
        fd = stdout.fileno()
    except (AttributeError, OSError, ValueError):
        return None
    buf = bytearray()
    records: list[bytes] = []
    total_bytes = 0
    while True:
        if deadline_monotonic is not None:
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                return None
        else:
            remaining = None
        try:
            ready, _, _ = select.select([fd], [], [], remaining)
        except (OSError, ValueError):
            return None
        if not ready:
            return None
        try:
            chunk = os.read(fd, _NUL_PATH_RECORD_READ_CHUNK_BYTES)
        except OSError:
            return None
        if not chunk:
            break
        total_bytes += len(chunk)
        if total_bytes > max_bytes:
            return None
        buf.extend(chunk)
        while True:
            nul = buf.find(b"\0")
            if nul < 0:
                break
            part = bytes(buf[:nul])
            del buf[: nul + 1]
            if part == b"":
                return None
            if len(records) >= max_records:
                return None
            records.append(part)
    if buf:
        return None
    return tuple(records)
